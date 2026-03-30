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

    # 根據 action 分派
    handler = _ACTION_HANDLERS.get(action, _handle_manual_inspect)
    result  = handler(issue, diagnosis, dry_run)

    result.setdefault("action",    action)
    result.setdefault("timestamp", datetime.utcnow().isoformat())

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
            "message": f"無法從 {pod_name} 推斷 Deployment 名稱，請手動執行 kubectl rollout undo",
        }

    if dry_run:
        return {"ok": True, "message": f"[dry-run] 將回滾 Deployment {deploy_name}"}

    if not K8S_AVAILABLE:
        return {"ok": False, "message": "kubernetes 套件未安裝，無法執行自動補救"}

    try:
        _load_k8s_config()
        apps_v1 = k8s_client.AppsV1Api()

        # 取得目前 revision
        deploy = apps_v1.read_namespaced_deployment(deploy_name, namespace)
        current_rev = deploy.metadata.annotations.get(
            "deployment.kubernetes.io/revision", "?")

        # 觸發回滾（設定 rollbackTo revision=0 = 回到上一個）
        patch = {
            "spec": {
                "template": {
                    "metadata": {
                        "annotations": {
                            "kubectl.kubernetes.io/restartedAt":
                                datetime.utcnow().isoformat()
                        }
                    }
                }
            }
        }
        # 直接 patch template annotation 觸發滾動更新（若映像已修正）
        # 正確回滾需要使用 kubectl rollout undo，這裡用 subprocess 呼叫
        import subprocess
        cmd = ["kubectl", "rollout", "undo",
               f"deployment/{deploy_name}", "-n", namespace]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

        if proc.returncode == 0:
            return {
                "ok"     : True,
                "message": f"已回滾 Deployment {deploy_name}（從 revision {current_rev}）",
                "details": {"stdout": proc.stdout.strip()},
            }
        else:
            return {
                "ok"     : False,
                "message": f"回滾失敗：{proc.stderr.strip()}",
            }
    except FileNotFoundError:
        return {"ok": False, "message": "kubectl 未安裝，無法執行回滾"}
    except Exception as e:
        return {"ok": False, "message": f"回滾時發生錯誤：{e}"}


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


def _handle_fix_probe(issue: dict, diagnosis: dict, dry_run: bool) -> dict:
    """
    健康探針失敗 — 將 initialDelaySeconds 加大 30 秒（最多加到 120 秒）。
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

    try:
        _load_k8s_config()
        apps_v1 = k8s_client.AppsV1Api()
        deploy  = apps_v1.read_namespaced_deployment(deploy_name, namespace)

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
            "message": "請執行：kubectl get svc,endpoints -n " + namespace,
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
            "message": f"請執行：kubectl get rolebinding,clusterrolebinding -n {namespace}",
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
            "message": f"請執行：kubectl get configmap,secret -n {namespace}",
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
        "message": (
            "連接埠衝突需人工排查。\n"
            "建議：\n"
            "  1. kubectl get pods -o wide 確認同節點的 Pod\n"
            "  2. 修改 Deployment 的 containerPort 使用其他連接埠\n"
            "  3. 或刪除佔用該連接埠的 Pod"
        ),
    }


def _handle_manual_inspect(issue: dict, diagnosis: dict, dry_run: bool) -> dict:
    """無法自動補救 — 記錄診斷結果，提供人工排查指引。"""
    pod_name  = issue["pod_name"]
    namespace = issue["namespace"]
    return {
        "ok"     : True,
        "message": (
            f"此問題需要人工排查。\n"
            f"建議執行：\n"
            f"  kubectl describe pod {pod_name} -n {namespace}\n"
            f"  kubectl logs {pod_name} -n {namespace} --previous\n"
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
