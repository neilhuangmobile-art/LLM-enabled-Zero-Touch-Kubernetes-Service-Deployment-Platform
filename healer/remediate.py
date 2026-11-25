"""
healer/remediate.py
自動補救動作執行器 — 根據診斷結果對 K8s 資源執行具體修復操作。

補救動作清單：
  fix_image         — 嘗試回滾到上一個穩定映像版本
  increase_memory   — 動態調高 Pod 記憶體 Limit
  check_dependencies— 印出相依服務的 Endpoint 狀態（建議人工排查）
  fix_permissions   — 列出 RBAC 設定供人工確認
  create_config     — 列出缺少的 ConfigMap/Secret（建議人工補建）
  fix_probe         — 自動加大 initialDelaySeconds
  fix_port_conflict — 回報衝突連接埠（需人工修正）
  analyze_logs      — 重啟 Pod 並印出日誌（CrashLoopBackOff 常見做法）
  manual_inspect    — 只記錄診斷結果，不自動操作

研究報告依據：
    「LLM 代理可直接呼叫 kubectl 或 K8s API 執行補救，
     如刪除/重建 Pod、更新 Deployment 資源限制或回滾映像版本，
     實現真正的自主 SRE 閉環」
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import re
from datetime import datetime
from typing import Optional

try:
    from kubernetes import client as k8s_client, config as k8s_config
    K8S_AVAILABLE = True
except ImportError:
    K8S_AVAILABLE = False


# ══════════════════════════════════════════════════════════════════
# 公開 API
# ══════════════════════════════════════════════════════════════════

def remediate(issue: dict, diagnosis: dict, dry_run: bool = False) -> dict:
    """
    根據診斷結果執行對應的補救動作。

    Args:
        issue     : pod_watcher 產生的 issue dict（含 pod_name, namespace 等）
        diagnosis : diagnose_issue() 回傳的診斷 dict（含 action, severity 等）
        dry_run   : True = 只記錄，不實際操作（測試用）

    Returns:
        {
            "ok"        : bool,   # 補救是否成功（或已記錄）
            "action"    : str,    # 執行的動作代碼
            "message"   : str,    # 補救結果說明
            "details"   : dict,   # 額外細節（視動作而定）
            "timestamp" : str,    # ISO 時間戳
        }
    """
    action     = diagnosis.get("action", "manual_inspect")
    pod_name   = issue.get("pod_name",   "unknown")
    namespace  = issue.get("namespace",  "default")
    container  = issue.get("container",  "")
    severity   = diagnosis.get("severity", "unknown")

    print(f"\n🔧 補救動作：{action}  "
          f"（Pod: {namespace}/{pod_name}, 嚴重度: {severity}）"
          + ("  [dry-run]" if dry_run else ""))

    # 2026-11-25（Healer 方案 0.1）：會改叢集的動作先確認上層控制器真的是 Deployment；
    # StatefulSet、Job、DaemonSet、沒有 owner 的 Pod 只通知不動手（原本用 Pod 名稱猜，會改到別的服務）
    from healer import guard
    # 2026-11-25（Healer 方案階段 3.1，使用者同意自動）：崩潰或設定錯誤，而且 30 分鐘內剛更新過 → 先退回最後一個
    # 正常運作過的版本。依 issue 的 K8s reason 決定（結構化證據），不看 log；default 這類只通知的 namespace 不做。
    wants_revert = (not dry_run and guard.revert_recent_enabled()
                    and guard.problem_class(issue.get("reason")) in ("crash", "config")
                    and not guard.notify_only_namespace(namespace))
    if not dry_run and guard.enabled() and (action in guard.AUTO_ACTIONS or wants_revert):
        owner = _resolve_owner(issue)
        if owner.get("kind") != "Deployment" and action not in guard.AUTO_ACTIONS:
            owner, wants_revert = None, False   # 不是一般服務：不退回，照原本的只通知流程
        elif owner.get("kind") != "Deployment":
            kind = owner.get("kind") or "沒有上層控制器"
            result = {"ok": True, "action": "manual_inspect", "blocked_action": action,
                      "message": (f"「{pod_name}」不是由一般服務（Deployment）管理的（{kind}），自動修復可能改到別的東西，"
                                  f"所以只通知、不動手。診斷建議：{diagnosis.get('suggestion', '')}")}
            result.setdefault("timestamp", datetime.utcnow().isoformat())
            _log_remediation(pod_name, namespace, action, result)
            return result
        if owner:
            issue = {**issue, "deployment": owner["name"]}
    if wants_revert and issue.get("deployment"):
        r = _guarded_revert_recent(issue)
        if r is not None:
            r.setdefault("timestamp", datetime.utcnow().isoformat())
            r["diagnosed_action"] = action
            r["problem_key"] = guard.Limiter.key(namespace, issue["deployment"], guard.problem_class(issue.get("reason")))
            _log_remediation(pod_name, namespace, "revert_recent_change", r)
            return r

    # 根據 action 分派
    handler = _ACTION_HANDLERS.get(action, _handle_manual_inspect)
    result  = handler(issue, diagnosis, dry_run)

    result.setdefault("action",    action)
    result.setdefault("timestamp", datetime.utcnow().isoformat())
    if issue.get("deployment"):
        # 事件紀錄帶上限流用的 key，網站重啟後才能重建冷卻與停手狀態（guard.Limiter.restore）
        result.setdefault("problem_key", guard.Limiter.key(namespace, issue["deployment"],
                                                           guard.problem_class(issue.get("reason"))))

    _log_remediation(pod_name, namespace, action, result)
    return result


# ══════════════════════════════════════════════════════════════════
# 補救動作實作
# ══════════════════════════════════════════════════════════════════

def _handle_analyze_logs(issue: dict, diagnosis: dict, dry_run: bool) -> dict:
    """
    CrashLoopBackOff — 刪除 Pod 讓 Deployment 重建，並印出崩潰前日誌。
    Kubernetes Deployment 控制器會自動重建被刪除的 Pod。
    """
    pod_name  = issue["pod_name"]
    namespace = issue["namespace"]
    container = issue.get("container", "")

    if dry_run:
        return {"ok": True, "message": f"[dry-run] 將刪除並重建 Pod {pod_name}"}

    if not K8S_AVAILABLE:
        return {"ok": False, "message": "kubernetes 套件未安裝，無法執行自動補救"}

    try:
        _load_k8s_config()
        v1 = k8s_client.CoreV1Api()
        from healer import guard
        if guard.enabled() and issue.get("deployment"):
            # 2026-11-25（Healer 方案權限表）：只有多副本才重建；單副本重建會停機，而且反覆崩潰多半是程式本身的錯
            dep = k8s_client.AppsV1Api().read_namespaced_deployment(issue["deployment"], namespace)
            if (dep.spec.replicas or 0) <= 1:
                return {"ok": True, "action": "manual_inspect", "blocked_action": "analyze_logs",
                        "message": (f"{issue['deployment']} 只有一個副本，重建 Pod 會讓服務中斷，而且反覆崩潰多半是程式本身的錯，"
                                    "所以只通知、不重建。請看 Pod 詳情裡「最近的 log」找原因。")}

        # 取崩潰前日誌
        try:
            logs = v1.read_namespaced_pod_log(
                name=pod_name, namespace=namespace,
                container=container, tail_lines=30, previous=True,
            )
            print(f"   📋 崩潰前最後 30 行日誌：\n{logs[:1500]}")
        except Exception:
            pass

        # 刪除 Pod（Deployment 會自動重建）
        v1.delete_namespaced_pod(name=pod_name, namespace=namespace)
        if issue.get("deployment"):
            _register_verify(issue, "analyze_logs", None, rollback_allowed=False)
        return {
            "ok"     : True,
            "message": f"已刪除 Pod {pod_name}，Deployment 控制器將自動重建",
        }
    except Exception as e:
        return {"ok": False, "message": f"刪除 Pod 失敗：{e}"}


def _handle_fix_image(issue: dict, diagnosis: dict, dry_run: bool) -> dict:
    """
    ImagePullBackOff — 嘗試回滾 Deployment 到上一個 revision。
    使用 apps/v1 rollback（等同 kubectl rollout undo）。
    """
    pod_name  = issue["pod_name"]
    namespace = issue["namespace"]

    # 從 pod_name 推斷 Deployment 名稱（去掉最後兩段隨機後綴）
    deploy_name = _infer_deployment_name(pod_name)
    if not deploy_name:
        return {
            "ok"     : False,
            "message": f"找不到 {pod_name} 屬於哪個服務，沒辦法自動回滾。請到「我的服務」找到它，按「回滾到上一版」。",
        }

    if dry_run:
        return {"ok": True, "message": f"[dry-run] 將回滾 Deployment {deploy_name}"}

    if not K8S_AVAILABLE:
        return {"ok": False, "message": "kubernetes 套件未安裝，無法執行自動補救"}

    from healer import guard
    if guard.enabled() and issue.get("deployment"):
        return _guarded_fix_image(issue, diagnosis)

    try:
        _load_k8s_config()
        apps_v1 = k8s_client.AppsV1Api()

        # 取得目前 revision
        deploy = apps_v1.read_namespaced_deployment(deploy_name, namespace)
        current_rev = deploy.metadata.annotations.get(
            "deployment.kubernetes.io/revision", "?")
        current_image = ""
        try:
            current_image = deploy.spec.template.spec.containers[0].image
        except Exception:
            pass

        # 2026-10-03 實測發現的真實 bug：單純 `kubectl rollout undo`（不指定
        # --to-revision）永遠只回退「上一個」revision。如果使用者在 Healer
        # 還沒來得及處理第一次壞掉的部署之前，又手動改了第二次壞的 image，
        # 「上一個」revision 剛好也是壞的——kubectl 指令本身執行成功（回傳碼
        # 0），Healer 因此回報「已回滾」，但 Pod 其實還是同一個壞 image，
        # 下一輪又偵測到同一個問題、再 undo 一次，在兩個壞 revision 之間無限
        # 乒乓，每次都誤報修好了。改成主動往回找「image 真的跟現在不一樣」的
        # 最近一個 revision，不是盲目退一步；真的找不到才退回原本的行為。
        target_revision = None
        try:
            selector = (deploy.spec.selector.match_labels
                        if deploy.spec.selector else None) or {}
            label_selector = ",".join(f"{k}={v}" for k, v in selector.items())
            rs_list = apps_v1.list_namespaced_replica_set(
                namespace, label_selector=label_selector)
            candidates = []
            for rs in rs_list.items:
                rev_str = (rs.metadata.annotations or {}).get(
                    "deployment.kubernetes.io/revision")
                if not rev_str:
                    continue
                try:
                    rev_num = int(rev_str)
                except ValueError:
                    continue
                try:
                    rs_image = rs.spec.template.spec.containers[0].image
                except Exception:
                    continue
                candidates.append((rev_num, rs_image))
            # 由新到舊排序，找第一個 image 跟目前壞掉的不一樣的 revision
            candidates.sort(key=lambda x: x[0], reverse=True)
            for rev_num, rs_image in candidates:
                if str(rev_num) == str(current_rev):
                    continue
                if rs_image != current_image:
                    target_revision = rev_num
                    break
        except Exception:
            target_revision = None  # 查不到候選名單就退回原本「盲目回退一步」的行為

        import subprocess
        cmd = ["kubectl", "rollout", "undo",
               f"deployment/{deploy_name}", "-n", namespace]
        if target_revision is not None:
            cmd.append(f"--to-revision={target_revision}")
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

        if proc.returncode == 0:
            detail = (f"（指定回滾到 revision {target_revision}，確認過 image 跟目前壞掉的不同）"
                       if target_revision is not None
                       else "（回滾到上一個 revision）")
            return {
                "ok"     : True,
                "message": f"已回滾 Deployment {deploy_name}{detail}",
                "details": {"stdout": proc.stdout.strip(), "target_revision": target_revision},
            }
        stderr = proc.stderr.strip()
        if "no rollout history" in stderr.lower():
            # 這個 Deployment 從建立起就沒有成功過的 revision 可以回滾——這種情況下
            # 「回滾失敗」是誤導使用者的講法（聽起來像系統故障），實際上是「這個
            # image tag 本身打錯／不存在，需要人工改成正確的 tag 再重新部署」，
            # 沒有辦法自動修（沒有『正確版本』這個資訊可以自動猜）。
            image = ""
            try:
                image = deploy.spec.template.spec.containers[0].image
            except Exception:
                pass
            return {
                "ok"     : False,
                "message": (
                    f"這個 Deployment 從建立起就沒有成功過的版本可以回滾，不是「暫時性故障」。"
                    f"目前設定的映像是 {image or '未知'}，最可能的原因是 image tag 打錯或不存在。"
                    f"請到 Deployments 頁面用正確的 tag 更新映像（或重新部署），"
                    f"這一步需要人工確認正確的版本，系統無法自動猜。 / "
                    f"This Deployment has never had a working revision to roll back to — this isn't "
                    f"a transient failure. The current image is {image or 'unknown'}; the most likely "
                    f"cause is a wrong or non-existent image tag. Please update it to a correct tag "
                    f"manually — there's no earlier good version to infer from."
                ),
                "details": {"stderr": stderr},
            }
        return {
            "ok"     : False,
            "message": f"回滾失敗：{stderr}",
        }
    except FileNotFoundError:
        return {"ok": False, "message": "平台這台電腦找不到 kubectl，沒辦法執行回滾，請聯絡平台管理員。"}
    except Exception as e:
        return {"ok": False, "message": f"回滾時發生錯誤：{e}"}


def _guarded_fix_image(issue: dict, diagnosis: dict) -> dict:
    """2026-11-25（Healer 方案 P7）：只退回到「曾經正常運作過」的映像檔（背景迴圈看到服務全部可用時記在
    annotation），用 Python client patch（原本用 subprocess 執行 kubectl，可能和 Python client 指向不同叢集）。
    Docker Hub 限流（429）時映像檔本身沒問題，不退回。"""
    from healer import guard
    from healer.verify import apply_patch, SkipPatch
    namespace, name = issue["namespace"], issue["deployment"]
    struct = " ".join([str(issue.get("message") or ""), str(diagnosis.get("struct_excerpt") or "")])
    if guard.rate_limited_pull(struct):
        return {"ok": True, "action": "manual_inspect", "blocked_action": "fix_image",
                "message": "映像檔下載被 Docker Hub 限流（429 Too Many Requests），映像檔本身沒有問題，所以不退回版本；"
                           "過一陣子會自動重試，或改用有登入的映像檔倉庫。"}
    _load_k8s_config()
    apps = k8s_client.AppsV1Api()

    def mutate(d):
        good = (d.metadata.annotations or {}).get(guard.ANN_LAST_GOOD_IMAGE)
        cs = d.spec.template.spec.containers or []
        c = next((x for x in cs if x.name == issue.get("container")), cs[0] if cs else None)
        if not c:
            raise SkipPatch("找不到容器設定")
        if not good:
            raise SkipPatch(f"{name} 沒有「曾經正常運作過」的映像檔紀錄，沒辦法自動退回。目前的映像檔是 {c.image}，"
                            "最可能是 tag 打錯或不存在，或是私有映像檔需要登入。請在「我的服務」改成正確的映像檔。")
        if good == c.image:
            raise SkipPatch(f"目前的映像檔 {c.image} 曾經正常運作過，問題可能是映像檔倉庫暫時連不上或需要登入，"
                            "退回版本沒有幫助。請看 Pod 詳情裡的事件。")
        old = c.image
        c.image = good
        return {"container": c.name, "old_image": old, "new_image": good}

    r = apply_patch(apps, namespace, name, mutate)
    if r.get("skipped") or not r.get("ok"):
        return {"ok": bool(r.get("skipped")), "action": "manual_inspect" if r.get("skipped") else "fix_image",
                "blocked_action": "fix_image" if r.get("skipped") else None, "message": r["message"]}
    ch = r["change"]
    _register_verify(issue, "fix_image", r["snapshot"], rollback_allowed=True)
    gitops = _record_gitops(issue, "fix_image")
    return {"ok": True, "message": f"已把 {name} 的映像檔從 {ch['old_image']} 退回到曾經正常運作的 {ch['new_image']}，接下來會確認有沒有修好",
            "details": {"old": ch["old_image"], "new": ch["new_image"]}, "verify_pending": True, "gitops": gitops}


def _guarded_revert_recent(issue: dict) -> Optional[dict]:
    """階段 3.1：30 分鐘內剛更新過、更新後開始崩潰 → 整個 Pod 範本退回到最後一個全部可用過的版本
    （背景迴圈記在 annotation zerotouch.k8s/last-good-template）。不符合條件回傳 None，讓原本的處理接手。
    跟其他自動修復一樣：dry-run、快照、驗證（失敗就換回快照）、寫 GitOps；通知寫明退回了使用者的更新。"""
    import copy
    from healer import guard
    from healer.verify import apply_patch, SkipPatch
    namespace, name = issue["namespace"], issue["deployment"]
    try:
        _load_k8s_config()
        apps = k8s_client.AppsV1Api()
        replica_sets = apps.list_namespaced_replica_set(namespace).items
    except Exception as e:  # noqa: BLE001
        print(f"   ⚠️  查不到版本紀錄，不退回：{e}")
        return None

    def mutate(d):
        target, why = guard.recent_change_plan(d, replica_sets)
        if target is None:
            raise SkipPatch(why)
        cur = guard.current_replica_set(d, replica_sets)
        tmpl = copy.deepcopy(target.spec.template)
        labels = dict(tmpl.metadata.labels or {})
        labels.pop("pod-template-hash", None)
        tmpl.metadata.labels = labels
        old_imgs = [c.image for c in d.spec.template.spec.containers or []]
        d.spec.template = tmpl
        return {"old_image": ", ".join(old_imgs), "new_image": ", ".join(c.image for c in tmpl.spec.containers or []),
                "from_revision": (cur.metadata.annotations or {}).get(guard.ANN_REVISION) if cur else None,
                "to_revision": (target.metadata.annotations or {}).get(guard.ANN_REVISION)}

    r = apply_patch(apps, namespace, name, mutate, replace=True)
    if r.get("skipped"):
        print(f"   ↪ 不退回最近的更新：{r['message']}")
        return None
    if not r.get("ok"):
        return {"ok": False, "action": "revert_recent_change", "message": r["message"]}
    ch = r["change"]
    from healer import verify
    key = guard.Limiter.key(namespace, name, guard.problem_class(issue.get("reason")))
    verify.PENDING.register(key, namespace, name, "revert_recent_change", r["snapshot"], True, full_template=True)
    gitops = _record_gitops(issue, "revert_recent_change")
    what = (f"映像檔 {ch['old_image']} → {ch['new_image']}" if ch["old_image"] != ch["new_image"]
            else f"版本 {ch['from_revision']} → {ch['to_revision']}")
    return {"ok": True, "action": "revert_recent_change", "verify_pending": True, "gitops": gitops,
            "message": (f"{name} 在剛才的更新之後開始出問題，已退回你剛才的更新（{what}），回到最後一個正常運作過的版本。"
                        "接下來會確認有沒有修好；要再更新，請先修正問題。"),
            "details": {"old": ch["old_image"], "new": ch["new_image"], "from_revision": ch["from_revision"],
                        "to_revision": ch["to_revision"]}}


def _guarded_increase_memory(issue: dict, diagnosis: dict) -> dict:
    """2026-11-25（Healer 方案 0.3）：上限 4Gi（和客戶叢集共用）、同一服務最多 2 次、requests 依比例一起調
    （保持 QoS 類別）、沒設上限的不自動加。先 dry-run、存快照、登記驗證。"""
    from healer import guard
    from healer.verify import apply_patch, SkipPatch
    namespace, name = issue["namespace"], issue["deployment"]
    _load_k8s_config()
    apps = k8s_client.AppsV1Api()

    def mutate(d):
        cs = d.spec.template.spec.containers or []
        c = next((x for x in cs if x.name == issue.get("container")), cs[0] if cs else None)
        if not c:
            raise SkipPatch("找不到容器設定")
        res = c.resources or k8s_client.V1ResourceRequirements()
        limit = (res.limits or {}).get("memory")
        request = (res.requests or {}).get("memory")
        plan, why = guard.plan_memory_bump(limit, request, guard.memory_bumps(d))
        if not plan:
            raise SkipPatch(why)
        res.limits = {**(res.limits or {}), "memory": plan["limit"]}
        if plan["request"]:
            res.requests = {**(res.requests or {}), "memory": plan["request"]}
        c.resources = res
        ann = dict(d.metadata.annotations or {})
        ann[guard.ANN_MEMORY_BUMPS] = str(guard.memory_bumps(d) + 1)
        d.metadata.annotations = ann
        return {"container": c.name, **plan}

    r = apply_patch(apps, namespace, name, mutate)
    if r.get("skipped"):
        return {"ok": True, "action": "manual_inspect", "blocked_action": "increase_memory",
                "message": f"{name} 記憶體不足，但{r['message']}。"}
    if not r.get("ok"):
        return {"ok": False, "message": r["message"]}
    ch = r["change"]
    _register_verify(issue, "increase_memory", r["snapshot"], rollback_allowed=True)
    gitops = _record_gitops(issue, "increase_memory")
    req = f"，保留量 {ch['old_request']} → {ch['request']}" if ch.get("request") else ""
    return {"ok": True, "message": f"已把 {name} 的記憶體上限從 {ch['old_limit']} 調到 {ch['limit']}{req}，接下來會確認有沒有修好",
            "details": {"old": ch["old_limit"], "new": ch["limit"], "old_request": ch.get("old_request"),
                        "new_request": ch.get("request")}, "verify_pending": True, "gitops": gitops}


def _handle_increase_memory(issue: dict, diagnosis: dict, dry_run: bool) -> dict:
    """
    OOMKilled — 將 Deployment 的記憶體 Limit 翻倍。
    讀取當前 limits.memory，乘以 2 後 patch 回去。
    """
    pod_name  = issue["pod_name"]
    namespace = issue["namespace"]
    container = issue.get("container", "")
    deploy_name = _infer_deployment_name(pod_name)

    if not deploy_name:
        return {
            "ok"     : False,
            "message": "無法推斷 Deployment 名稱，請手動調整 resources.limits.memory",
        }

    if dry_run:
        return {"ok": True, "message": f"[dry-run] 將翻倍 {deploy_name} 的記憶體 Limit"}

    if not K8S_AVAILABLE:
        return {"ok": False, "message": "kubernetes 套件未安裝"}

    from healer import guard
    if guard.enabled() and issue.get("deployment"):
        return _guarded_increase_memory(issue, diagnosis)

    try:
        _load_k8s_config()
        apps_v1 = k8s_client.AppsV1Api()
        deploy  = apps_v1.read_namespaced_deployment(deploy_name, namespace)

        containers = deploy.spec.template.spec.containers
        patched    = False
        old_mem    = new_mem = "unknown"

        for c in containers:
            if container and c.name != container:
                continue
            if c.resources and c.resources.limits and "memory" in c.resources.limits:
                old_mem = c.resources.limits["memory"]
                new_mem = _double_memory(old_mem)
                c.resources.limits["memory"] = new_mem
                patched = True
                break

        if not patched:
            # 沒有設定 limits，直接加上預設值
            for c in containers:
                if container and c.name != container:
                    continue
                if not c.resources:
                    c.resources = k8s_client.V1ResourceRequirements()
                if not c.resources.limits:
                    c.resources.limits = {}
                c.resources.limits["memory"] = "512Mi"
                new_mem = "512Mi"
                old_mem = "（未設定）"
                patched = True
                break

        if patched:
            apps_v1.patch_namespaced_deployment(deploy_name, namespace, deploy)
            return {
                "ok"     : True,
                "message": f"已將 {deploy_name} 記憶體 Limit 從 {old_mem} 調整為 {new_mem}",
                "details": {"old": old_mem, "new": new_mem},
            }
        return {"ok": False, "message": "找不到對應的容器設定，請手動調整"}

    except Exception as e:
        return {"ok": False, "message": f"調整記憶體失敗：{e}"}


def _listen_port(c):
    """容器實際監聽的 port：已知映像檔用它固定的 port，否則用 containerPort 第一個。"""
    from agents.cost_agent import _LISTEN_PORTS, _image_name
    known = _LISTEN_PORTS.get(_image_name(getattr(c, "image", "") or ""))
    if known:
        return int(known)
    ports = getattr(c, "ports", None) or []
    return int(ports[0].container_port) if ports and getattr(ports[0], "container_port", None) else None


def _fix_probe_ports(c) -> list:
    """探針檢查的 port 跟容器實際監聽的 port 不同時，改成正確的 port。回傳改了哪些（給訊息用）。

    2026-11-07 對照實驗：健康檢查 port 設錯（nginx 聽 80、探針打 8081），原本只會加長等待時間，永遠修不好。
    只改「設定本身就對不上」的情況；兩者相同時不動，照原本的做法加長等待時間。
    """
    listen = _listen_port(c)
    changed = []
    if not listen:
        return changed
    for attr in ("liveness_probe", "readiness_probe", "startup_probe"):
        probe = getattr(c, attr, None)
        if not probe:
            continue
        for kind in ("http_get", "tcp_socket"):
            h = getattr(probe, kind, None)
            port = getattr(h, "port", None) if h else None
            if isinstance(port, int) and port != listen:
                h.port = listen
                changed.append(f"{attr} {port}→{listen}")
    return changed


def _handle_fix_probe(issue: dict, diagnosis: dict, dry_run: bool) -> dict:
    """
    健康探針失敗 — 探針的 port 跟容器實際監聽的不同時改成正確的 port；
    否則將 initialDelaySeconds 加大 30 秒（最多加到 120 秒）。
    """
    pod_name    = issue["pod_name"]
    namespace   = issue["namespace"]
    container   = issue.get("container", "")
    deploy_name = _infer_deployment_name(pod_name)

    if not deploy_name:
        return {"ok": False, "message": "無法推斷 Deployment 名稱，請手動調整探針設定"}

    if dry_run:
        return {"ok": True, "message": f"[dry-run] 將調整 {deploy_name} 的探針 initialDelaySeconds"}

    if not K8S_AVAILABLE:
        return {"ok": False, "message": "kubernetes 套件未安裝"}

    from healer import guard
    if guard.enabled() and issue.get("deployment"):
        return _guarded_fix_probe(issue)

    try:
        _load_k8s_config()
        apps_v1 = k8s_client.AppsV1Api()
        deploy  = apps_v1.read_namespaced_deployment(deploy_name, namespace)

        port_fixes = []
        for c in deploy.spec.template.spec.containers:
            if container and c.name != container:
                continue
            port_fixes += _fix_probe_ports(c)
        if port_fixes:
            apps_v1.patch_namespaced_deployment(deploy_name, namespace, deploy)
            return {"ok": True, "message": f"{deploy_name} 的健康檢查 port 設錯，已改成容器實際監聽的 port（"
                                           + "、".join(port_fixes) + "）"}

        patched = False
        for c in deploy.spec.template.spec.containers:
            if container and c.name != container:
                continue
            for probe_attr in ("liveness_probe", "readiness_probe", "startup_probe"):
                probe = getattr(c, probe_attr, None)
                if probe:
                    old_delay = probe.initial_delay_seconds or 10
                    new_delay = min(old_delay + 30, 120)
                    probe.initial_delay_seconds = new_delay
                    patched = True
                    print(f"   探針 {probe_attr}: {old_delay}s → {new_delay}s")

        if patched:
            apps_v1.patch_namespaced_deployment(deploy_name, namespace, deploy)
            return {
                "ok"     : True,
                "message": f"已調整 {deploy_name} 的探針 initialDelaySeconds（+30s）",
            }
        return {
            "ok"     : False,
            "message": "未找到探針設定，請手動在 spec.containers[].livenessProbe 加入探針",
        }
    except Exception as e:
        return {"ok": False, "message": f"調整探針失敗：{e}"}


ANN_PROBE_ADJUSTED = "zerotouch.k8s/healer-probe-adjusted"
STARTUP_ALLOW_S = 300       # startupProbe 最多等 5 分鐘
STARTUP_PERIOD_S = 10


def startup_probe_plan(c) -> Optional[str]:
    """有 startupProbe：把 failureThreshold 放大到能等 STARTUP_ALLOW_S 秒；沒有：照 liveness（沒有就 readiness）的
    檢查方式新增一個。回傳說明文字，沒有可以參考的探針回傳 None。直接改 c。"""
    import copy
    sp = getattr(c, "startup_probe", None)
    if sp is not None:
        period = sp.period_seconds or STARTUP_PERIOD_S
        need = -(-STARTUP_ALLOW_S // period)
        old = sp.failure_threshold or 3
        if old >= need:
            return None
        sp.failure_threshold = need
        return f"startupProbe 最多等 {old * period}s→{need * period}s"
    base = getattr(c, "liveness_probe", None) or getattr(c, "readiness_probe", None)
    if base is None:
        return None
    sp = copy.deepcopy(base)
    sp.initial_delay_seconds = base.initial_delay_seconds or 0
    sp.period_seconds = STARTUP_PERIOD_S
    sp.failure_threshold = -(-STARTUP_ALLOW_S // STARTUP_PERIOD_S)
    sp.success_threshold = 1
    c.startup_probe = sp
    return f"新增 startupProbe（啟動最多等 {sp.failure_threshold * STARTUP_PERIOD_S}s，啟動完才開始健康檢查）"


def _guarded_fix_probe(issue: dict) -> dict:
    """2026-11-25（Healer 方案權限表）：探針只自動調一次（port 設錯就改成正確 port，否則等待時間 +30 秒），
    先 dry-run、存快照、登記驗證。"""
    from healer.verify import apply_patch, SkipPatch
    namespace, name, container = issue["namespace"], issue["deployment"], issue.get("container", "")
    _load_k8s_config()
    apps = k8s_client.AppsV1Api()

    def mutate(d):
        if (d.metadata.annotations or {}).get(ANN_PROBE_ADJUSTED):
            raise SkipPatch("健康檢查已經自動調整過一次，不再重複調整")
        fixes, delays = [], []
        for c in d.spec.template.spec.containers or []:
            if container and c.name != container:
                continue
            fixes += _fix_probe_ports(c)
        if not fixes:
            # 2026-11-25（Healer 方案階段 3.2）：啟動太慢改用 startupProbe，不再把每個探針的 initialDelaySeconds 加 30。
            # 官方建議 failureThreshold × periodSeconds 要涵蓋最慢的啟動時間；啟動期間 liveness 不會跑，啟動後照常檢查。
            for c in d.spec.template.spec.containers or []:
                if container and c.name != container:
                    continue
                plan = startup_probe_plan(c)
                if plan:
                    delays.append(plan)
        if not fixes and not delays:
            raise SkipPatch("找不到健康檢查設定")
        d.metadata.annotations = {**(d.metadata.annotations or {}), ANN_PROBE_ADJUSTED: datetime.utcnow().isoformat()}
        return {"ports": fixes, "delays": delays}

    r = apply_patch(apps, namespace, name, mutate)
    if r.get("skipped"):
        return {"ok": True, "action": "manual_inspect", "blocked_action": "fix_probe", "message": f"{name}：{r['message']}。"}
    if not r.get("ok"):
        return {"ok": False, "message": r["message"]}
    ch = r["change"]
    _register_verify(issue, "fix_probe", r["snapshot"], rollback_allowed=True)
    gitops = _record_gitops(issue, "fix_probe")
    what = ("健康檢查 port 設錯，已改成容器實際監聽的 port（" + "、".join(ch["ports"]) + "）") if ch["ports"] \
        else ("啟動比較慢，已放寬啟動時間（" + "、".join(ch["delays"]) + "）")
    return {"ok": True, "message": f"{name}：{what}，接下來會確認有沒有修好",
            "details": {"old": None, "new": ch}, "verify_pending": True, "gitops": gitops}


def _handle_check_dependencies(issue: dict, diagnosis: dict, dry_run: bool) -> dict:
    """
    依賴服務無法連線 — 列出同 namespace 的 Service 與 Endpoints 供人工排查。
    這個動作不修改任何設定，只提供診斷資訊。
    """
    namespace = issue["namespace"]

    if dry_run:
        return {"ok": True, "message": "[dry-run] 將列出相依服務狀態"}

    if not K8S_AVAILABLE:
        return {
            "ok"     : True,
            "message": "它依賴的服務可能沒在執行，請到「我的服務」確認相關服務都是健康的。",
        }

    try:
        _load_k8s_config()
        v1       = k8s_client.CoreV1Api()
        services = v1.list_namespaced_service(namespace)
        endpoints= v1.list_namespaced_endpoints(namespace)

        svc_info = []
        for svc in services.items:
            ep = next(
                (e for e in endpoints.items if e.metadata.name == svc.metadata.name),
                None
            )
            ready = bool(ep and ep.subsets)
            svc_info.append({
                "name" : svc.metadata.name,
                "type" : svc.spec.type,
                "ready": ready,
            })
            status = "✅" if ready else "❌"
            print(f"   {status} Service: {svc.metadata.name} ({svc.spec.type})")

        not_ready = [s["name"] for s in svc_info if not s["ready"]]
        if not_ready:
            return {
                "ok"     : True,
                "message": f"發現 {len(not_ready)} 個 Service 無 Endpoint：{not_ready}，請確認這些服務是否正常啟動",
                "details": {"services": svc_info},
            }
        return {
            "ok"     : True,
            "message": f"namespace '{namespace}' 中所有 Service 均有 Endpoint，請確認 Pod 本身的連線設定",
            "details": {"services": svc_info},
        }
    except Exception as e:
        return {"ok": False, "message": f"查詢 Service 失敗：{e}"}


def _handle_fix_permissions(issue: dict, diagnosis: dict, dry_run: bool) -> dict:
    """
    權限不足 — 列出 ServiceAccount 的 ClusterRoleBinding/RoleBinding 供人工確認。
    """
    pod_name  = issue["pod_name"]
    namespace = issue["namespace"]

    if dry_run:
        return {"ok": True, "message": f"[dry-run] 將列出 {namespace} 的 RBAC 設定"}

    if not K8S_AVAILABLE:
        return {
            "ok"     : True,
            "message": "權限設定可能不足，需要調整部署設定，請聯絡平台管理員。",
        }

    try:
        _load_k8s_config()
        rbac = k8s_client.RbacAuthorizationV1Api()
        rbs  = rbac.list_namespaced_role_binding(namespace)

        rb_list = [rb.metadata.name for rb in rbs.items]
        print(f"   RoleBindings in {namespace}: {rb_list}")
        return {
            "ok"     : True,
            "message": f"已列出 RBAC 設定，請確認 Pod 的 ServiceAccount 是否有足夠權限",
            "details": {"role_bindings": rb_list},
        }
    except Exception as e:
        return {"ok": False, "message": f"查詢 RBAC 失敗：{e}"}


def _handle_create_config(issue: dict, diagnosis: dict, dry_run: bool) -> dict:
    """
    ConfigMap/Secret 不存在 — 列出目前存在的 ConfigMap/Secret 供人工比對。
    """
    namespace = issue["namespace"]

    if dry_run:
        return {"ok": True, "message": f"[dry-run] 將列出 {namespace} 的 ConfigMap/Secret"}

    if not K8S_AVAILABLE:
        return {
            "ok"     : True,
            "message": "可能缺少需要的設定檔或密碼（ConfigMap、Secret），請確認部署時有一起建立。",
        }

    try:
        _load_k8s_config()
        v1      = k8s_client.CoreV1Api()
        cms     = v1.list_namespaced_config_map(namespace)
        secrets = v1.list_namespaced_secret(namespace)

        cm_names  = [c.metadata.name for c in cms.items]
        sec_names = [s.metadata.name for s in secrets.items
                     if not s.metadata.name.startswith("default-token")]

        print(f"   ConfigMaps: {cm_names}")
        print(f"   Secrets   : {sec_names}")

        return {
            "ok"     : True,
            "message": "已列出現有的 ConfigMap 與 Secret，請確認 Pod 引用的名稱是否存在",
            "details": {"configmaps": cm_names, "secrets": sec_names},
        }
    except Exception as e:
        return {"ok": False, "message": f"查詢 ConfigMap/Secret 失敗：{e}"}


def _handle_fix_port_conflict(issue: dict, diagnosis: dict, dry_run: bool) -> dict:
    """連接埠衝突 — 僅記錄，提供排查指引。"""
    return {
        "ok"     : True,
        "message": "Port 衝突沒辦法自動修。請到服務詳情把 Port 換成其他號碼，或刪掉占用這個 Port 的服務。",
    }


def _handle_manual_inspect(issue: dict, diagnosis: dict, dry_run: bool) -> dict:
    """無法自動補救 — 記錄診斷結果，提供人工排查指引。"""
    pod_name  = issue["pod_name"]
    namespace = issue["namespace"]
    return {
        "ok"     : True,
        # 2026-10-09：不叫使用者打 kubectl，引導到平台上看得到 log 與事件的地方
        "message": (
            f"這個問題沒辦法自動修。請到 Pod「{pod_name}」的詳情看「最近的 log」和「最近事件」。"
            f"診斷建議：{diagnosis.get('suggestion', '')}"
        ),
    }


# ── 動作分派表 ────────────────────────────────────────────────────
_ACTION_HANDLERS = {
    "analyze_logs"      : _handle_analyze_logs,
    "fix_image"         : _handle_fix_image,
    "increase_memory"   : _handle_increase_memory,
    "fix_probe"         : _handle_fix_probe,
    "check_dependencies": _handle_check_dependencies,
    "fix_permissions"   : _handle_fix_permissions,
    "create_config"     : _handle_create_config,
    "fix_port_conflict" : _handle_fix_port_conflict,
    "manual_inspect"    : _handle_manual_inspect,
}


# ══════════════════════════════════════════════════════════════════
# 工具函式
# ══════════════════════════════════════════════════════════════════

def _load_k8s_config():
    try:
        k8s_config.load_incluster_config()
    except Exception:
        k8s_config.load_kube_config()


def _resolve_owner(issue: dict) -> dict:
    """呼叫端（背景迴圈、評估程式）已經查過就直接用；沒有就讀 Pod 的 ownerReferences（Healer 方案 0.1）。"""
    if issue.get("owner_kind"):
        return {"kind": issue["owner_kind"], "name": issue.get("deployment")}
    if not K8S_AVAILABLE:
        return {"kind": None, "name": None}
    try:
        from healer import guard
        _load_k8s_config()
        pod = k8s_client.CoreV1Api().read_namespaced_pod(issue["pod_name"], issue["namespace"])
        return guard.resolve_owner(pod, k8s_client.AppsV1Api())
    except Exception:  # noqa: BLE001
        return {"kind": None, "name": None}


def _register_verify(issue: dict, action: str, snap, rollback_allowed: bool) -> None:
    """動手之後登記「待驗證」，背景迴圈下一輪起檢查（Healer 方案階段 1）。key 和背景迴圈的去重 key 相同。"""
    from healer import guard, verify
    key = guard.Limiter.key(issue["namespace"], issue["deployment"], guard.problem_class(issue.get("reason") or action))
    verify.PENDING.register(key, issue["namespace"], issue["deployment"], action, snap, rollback_allowed)


def _record_gitops(issue: dict, action: str) -> dict:
    """自動修復寫進部署紀錄 repo（使用者 2026-11-25 決定）。HEALER_GITOPS=0 關閉。失敗不影響修復本身。"""
    if os.environ.get("HEALER_GITOPS", "1").strip().lower() in ("0", "false", "off", "no"):
        return {"ok": False, "skipped": "HEALER_GITOPS=0"}
    try:
        from gitops.heal_record import record_heal
        return record_heal(issue["namespace"], issue["deployment"], action)
    except Exception as e:  # noqa: BLE001
        print(f"   ⚠️  修復沒有寫進部署紀錄：{e}")
        return {"ok": False, "error": str(e)[:200]}


def _infer_deployment_name(pod_name: str) -> Optional[str]:
    """
    從 Pod 名稱推斷 Deployment 名稱。
    K8s Pod 名稱格式：<deploy>-<replicaset-hash>-<pod-hash>
    例：my-app-7d6b8c9f4-xk2pq → my-app
    """
    parts = pod_name.rsplit("-", 2)
    if len(parts) >= 3:
        return parts[0]
    if len(parts) == 2:
        return parts[0]
    return None


def _double_memory(mem_str: str) -> str:
    """
    將 K8s 記憶體字串翻倍。
    例：256Mi → 512Mi，1Gi → 2Gi，500Mi → 1000Mi
    """
    match = re.match(r"^(\d+)(Ki|Mi|Gi|Ti|k|M|G|T)?$", mem_str.strip())
    if not match:
        return mem_str
    value  = int(match.group(1)) * 2
    suffix = match.group(2) or ""
    return f"{value}{suffix}"


def _log_remediation(pod_name: str, namespace: str, action: str, result: dict):
    """記錄補救操作到 stdout（未來可改接 Prometheus/ELK）。"""
    status = "✅ 成功" if result.get("ok") else "⚠️  失敗"
    print(f"   {status} | {namespace}/{pod_name} | {action}")
    print(f"   {result.get('message', '')[:200]}")


# ══════════════════════════════════════════════════════════════════
# CLI 測試入口
# ══════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("=" * 55)
    print("  healer/remediate.py — 自動補救動作測試")
    print("=" * 55)

    test_cases = [
        {
            "name"     : "OOMKilled → increase_memory",
            "issue"    : {"pod_name": "api-7d6b8c9f4-xk2pq", "namespace": "default",
                          "container": "api", "reason": "OOMKilled", "restart_count": 3},
            "diagnosis": {"action": "increase_memory", "severity": "high",
                          "suggestion": "增加記憶體 Limit"},
        },
        {
            "name"     : "CrashLoopBackOff → analyze_logs",
            "issue"    : {"pod_name": "worker-5c6d7e8f9-abcde", "namespace": "production",
                          "container": "worker", "reason": "CrashLoopBackOff", "restart_count": 10},
            "diagnosis": {"action": "analyze_logs", "severity": "high",
                          "suggestion": "查看崩潰前日誌"},
        },
        {
            "name"     : "未知問題 → manual_inspect",
            "issue"    : {"pod_name": "db-abc123", "namespace": "staging",
                          "container": "db", "reason": "Error", "restart_count": 1},
            "diagnosis": {"action": "manual_inspect", "severity": "medium",
                          "suggestion": "請手動排查"},
        },
    ]

    for tc in test_cases:
        print(f"\n▶ {tc['name']}")
        result = remediate(tc["issue"], tc["diagnosis"], dry_run=True)
        status = "✅" if result["ok"] else "❌"
        print(f"  {status} {result['message']}")
