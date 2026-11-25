"""
healer/guard.py
Healer 補強方案階段 0（docs/healer_plan_20261120.md，2026-11-25）：擴大自動修復之前先補的洞。
本機、客戶叢集、評估程式（experiments/healer_eval）共用同一套規則。

- 0.1 找出真正的上層控制器（ownerReferences），不是 Deployment 的只通知不動手
- 0.2 去重改用「namespace/Deployment/問題類型」，加冷卻、每個 namespace 每小時上限、連續失敗停手
- 0.3 記憶體上限統一（本機與客戶叢集都是 4Gi）、同一服務最多自動調 2 次、requests 一起調、沒設上限不自動加
- 0.4 會改叢集的動作只認 K8s 結構化證據（log 只用來寫說明和「只通知」類的判斷）
- 0.7 namespace 範圍：default 只通知；healer-eval- 開頭只有評估程式能碰
- P7 映像檔只退回到「曾經正常運作過」的版本（記在 Deployment annotation）

開關：HEALER_GUARD=0 關閉（回到原本的行為，對照實驗用）。
"""
from __future__ import annotations

import os
import re
import threading
import time
from typing import Dict, Optional, Tuple

from cluster.remote_ops import MAX_MEMORY_MI, _to_mi

# 會直接改叢集的動作（其他動作只列資訊、不改設定）
AUTO_ACTIONS = {"increase_memory", "fix_image", "fix_probe", "analyze_logs", "revert_recent_change"}
COOLDOWN_S = 600            # 同一服務同一問題 10 分鐘內不重複動手
PER_HOUR = 10               # 每個 namespace 每小時最多自動動手 10 次（和客戶叢集 _REMOTE_HEAL_PER_HOUR 相同）
MAX_FAILURES = 2            # 同一問題連續修不好 2 次就停手
MAX_MEMORY_BUMPS = 2        # 同一服務最多自動調高記憶體 2 次
ANN_MEMORY_BUMPS = "zerotouch.k8s/healer-memory-bumps"
ANN_LAST_GOOD_IMAGE = "zerotouch.k8s/last-good-image"
EVAL_NS_PREFIX = "healer-eval-"
NOTIFY_ONLY_NAMESPACES = {"default"}


def enabled() -> bool:
    return os.environ.get("HEALER_GUARD", "1").strip().lower() not in ("0", "false", "off", "no")


# ── 0.1 上層控制器 ─────────────────────────────────────────────────
def resolve_owner(pod, apps_api=None) -> Dict[str, Optional[str]]:
    """Pod → ReplicaSet → Deployment。回傳 {"kind": ..., "name": ...}；沒有 owner 回傳 kind=None。
    查 ReplicaSet 失敗時回傳 ReplicaSet 本身（不猜 Deployment 名稱）。"""
    meta = getattr(pod, "metadata", None)
    refs = list(getattr(meta, "owner_references", None) or [])
    ref = next((r for r in refs if getattr(r, "controller", False)), refs[0] if refs else None)
    if ref is None:
        return {"kind": None, "name": None}
    if ref.kind != "ReplicaSet":
        return {"kind": ref.kind, "name": ref.name}
    try:
        rs = apps_api.read_namespaced_replica_set(ref.name, meta.namespace)
        rrefs = list(getattr(rs.metadata, "owner_references", None) or [])
        dep = next((r for r in rrefs if r.kind == "Deployment"), None)
        if dep is not None:
            return {"kind": "Deployment", "name": dep.name}
    except Exception:  # noqa: BLE001
        pass
    return {"kind": "ReplicaSet", "name": ref.name}


# ── 0.2 去重、冷卻、上限 ────────────────────────────────────────────
class Limiter:
    """記憶體內的狀態；網站重啟會清空（下一輪重新偵測，冷卻從頭算）。"""

    def __init__(self, cooldown_s: int = COOLDOWN_S, per_hour: int = PER_HOUR, max_failures: int = MAX_FAILURES):
        self.cooldown_s, self.per_hour, self.max_failures = cooldown_s, per_hour, max_failures
        self._last: Dict[str, float] = {}
        self._budget: Dict[str, list] = {}
        self._failures: Dict[str, int] = {}
        self._lock = threading.Lock()

    @staticmethod
    def key(namespace: str, deployment: str, problem: str) -> str:
        return f"{namespace}/{deployment}/{problem}"

    def check(self, key: str, namespace: str, now: Optional[float] = None) -> Optional[str]:
        """可以動手回傳 None；不行回傳原因（給紀錄與畫面用）。不會佔用額度。"""
        now = now or time.time()
        with self._lock:
            if self._failures.get(key, 0) >= self.max_failures:
                return f"同一個問題已經連續 {self.max_failures} 次修不好，停止自動修復，請人工處理"
            last = self._last.get(key)
            if last is not None and now - last < self.cooldown_s:
                return f"剛處理過（{int(self.cooldown_s // 60)} 分鐘內不重複動手）"
            recent = [t for t in self._budget.get(namespace, []) if now - t < 3600]
            if len(recent) >= self.per_hour:
                return f"這個帳號一小時內已經自動修復 {self.per_hour} 次，先暫停"
        return None

    def acquire(self, key: str, namespace: str, now: Optional[float] = None) -> Optional[str]:
        """檢查並佔用額度。可以動手回傳 None。"""
        now = now or time.time()
        why = self.check(key, namespace, now)
        if why:
            return why
        with self._lock:
            self._last[key] = now
            self._budget[namespace] = [t for t in self._budget.get(namespace, []) if now - t < 3600] + [now]
        return None

    def release(self, key: str, namespace: str) -> None:
        """沒有真的動手（例如只通知、dry-run 失敗）：退回額度。"""
        with self._lock:
            t = self._last.pop(key, None)
            if t is not None and t in self._budget.get(namespace, []):
                self._budget[namespace].remove(t)

    def failed(self, key: str) -> int:
        with self._lock:
            self._failures[key] = self._failures.get(key, 0) + 1
            return self._failures[key]

    def succeeded(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)

    def stopped(self, key: str) -> bool:
        with self._lock:
            return self._failures.get(key, 0) >= self.max_failures

    def restore(self, records, now: Optional[float] = None) -> int:
        """2026-11-25：網站重啟後從事件紀錄（heal，時間由舊到新）重建狀態，原本全在記憶體裡、重啟就歸零。
        - 驗證失敗或已停手（verify = fail／stopped）→ 這個問題維持停手，直到之後有驗證成功的紀錄
        - 一小時內真的動手過的自動修復 → 算進每小時額度；冷卻時間內的 → 恢復冷卻
        紀錄的 data.key 是 Limiter.key（2026-11-25 起才有；舊紀錄沒有 key 就略過）。回傳用到幾筆。"""
        now = now or time.time()
        used = 0
        with self._lock:
            for r in records or []:
                d = r.get("data") or {}
                key, ns, ts = d.get("key"), r.get("namespace"), float(r.get("ts") or 0)
                if not key or not ns:
                    continue
                v = d.get("verify")
                if v in ("fail", "stopped"):
                    self._failures[key] = self.max_failures
                elif v == "ok":
                    self._failures.pop(key, None)
                elif d.get("action") in AUTO_ACTIONS and r.get("outcome") == "ok" and now - ts < 3600:
                    self._budget.setdefault(ns, []).append(ts)
                    if now - ts < self.cooldown_s:
                        self._last[key] = max(self._last.get(key, 0), ts)
                else:
                    continue
                used += 1
        return used


LIMITER = Limiter()


def problem_type(issue: Dict, diagnosis: Dict) -> str:
    """去重用的問題類型：診斷動作代碼（不用模型寫的文字，同一問題每次措辭都不同）。"""
    return str(diagnosis.get("action") or issue.get("reason") or "unknown")


# ── 0.3 記憶體 ─────────────────────────────────────────────────────
def plan_memory_bump(limit: Optional[str], request: Optional[str], bumps: int) -> Tuple[Optional[Dict], Optional[str]]:
    """回傳 (新設定, None) 或 (None, 不調的原因)。新設定：{"limit", "request", "old_limit", "old_request"}。
    requests 依原本 requests/limits 的比例一起調：原本相等（Guaranteed）調完仍相等。"""
    if not limit:
        return None, "這個服務沒有設定記憶體上限，自動加上限可能反而造成新的記憶體不足，改成只通知"
    cur = _to_mi(limit)
    if not cur:
        return None, f"看不懂目前的記憶體上限（{limit}），不自動調整"
    if bumps >= MAX_MEMORY_BUMPS:
        return None, f"已經自動調高過 {MAX_MEMORY_BUMPS} 次還是不夠，可能是程式本身的問題，停止自動調整"
    if cur >= MAX_MEMORY_MI:
        return None, f"記憶體上限已經是 {MAX_MEMORY_MI // 1024}Gi（平台上限），不再自動調高"
    new = min(cur * 2, MAX_MEMORY_MI)
    req_mi = _to_mi(request) if request else None
    new_req = None
    if req_mi:
        new_req = new if req_mi >= cur else max(1, round(req_mi * new / cur))
    return {"limit": f"{new}Mi", "request": f"{new_req}Mi" if new_req else None,
            "old_limit": limit, "old_request": request}, None


def memory_bumps(deploy) -> int:
    ann = (getattr(deploy.metadata, "annotations", None) or {}) if getattr(deploy, "metadata", None) else {}
    try:
        return int(ann.get(ANN_MEMORY_BUMPS, "0"))
    except ValueError:
        return 0


# ── 0.4 結構化證據 ─────────────────────────────────────────────────
# 每個自動動作需要的結構化證據（K8s 的 reason、message、exitCode、事件 reason 與 message）
_STRUCT_EVIDENCE = {
    "increase_memory": re.compile(r"oomkill", re.I),
    # 2026-11-25：原本單獨的「not found」讓缺 ConfigMap 也算映像檔證據，改成要跟 image／manifest 在同一行
    "fix_image": re.compile(r"imagepullbackoff|errimagepull|pull access denied|manifest[^\n]*unknown|image[^\n]*not found", re.I),
    "fix_probe": re.compile(r"probe failed|unhealthy", re.I),
    "analyze_logs": re.compile(r"crashloopbackoff|back-?off restarting", re.I),
    # 階段 3.1：容器的 K8s reason 是崩潰類或設定錯誤（由 remediate 依 issue 的 reason 決定，不看 log）
    "revert_recent_change": re.compile(r"crashloopbackoff|back-?off restarting|createcontainerconfigerror|\berror\b", re.I),
}


def structured_text(context: Dict) -> str:
    """K8s 自己產生的欄位（不含容器 log）。"""
    events = context.get("events") or []
    parts = [str(context.get("reason") or ""), str(context.get("message") or ""),
             f"exitcode {context.get('exit_code')}" if context.get("exit_code") is not None else "",
             *(f"{e.get('reason') or ''} {e.get('message') or ''}" for e in events)]
    return "\n".join(p for p in parts if p).lower()


def supports(action: str, struct_text: str) -> bool:
    rx = _STRUCT_EVIDENCE.get(action)
    return bool(rx and rx.search(struct_text or ""))


def rate_limited_pull(struct_text: str) -> bool:
    """Docker Hub 限流（429）：映像檔本身沒問題，不應該退回版本。"""
    return bool(re.search(r"429|toomanyrequests|too many requests|rate limit", struct_text or "", re.I))


# ── 0.7 namespace ─────────────────────────────────────────────────
def notify_only_namespace(ns: str) -> bool:
    return (ns or "") in NOTIFY_ONLY_NAMESPACES


def is_eval_namespace(ns: str) -> bool:
    return (ns or "").startswith(EVAL_NS_PREFIX)


# ── P7 曾經正常的映像檔 ────────────────────────────────────────────
def deployment_healthy(deploy) -> bool:
    """更新完成而且全部可用。2026-11-25 評估發現：映像檔改壞時舊 Pod 會留著，available＝updated＝副本數
    看起來都滿了，但其實更新卡住；所以還要確認沒有多出來的舊 Pod（status.replicas＝副本數）、沒有不可用的副本。"""
    st = getattr(deploy, "status", None)
    want = getattr(getattr(deploy, "spec", None), "replicas", None)
    if st is None or not want:
        return False
    return (st.available_replicas or 0) >= want and (st.updated_replicas or 0) >= want \
        and (getattr(st, "replicas", None) or 0) == want and not getattr(st, "unavailable_replicas", None) \
        and (st.observed_generation or 0) >= (deploy.metadata.generation or 0)


# 同一種故障在 K8s 會顯示成不同的 reason（崩潰的容器在 Error 和 CrashLoopBackOff 之間切換），去重要歸成同一類
_PROBLEM_CLASS = {"CrashLoopBackOff": "crash", "Error": "crash", "RunContainerError": "crash",
                  "ImagePullBackOff": "image", "ErrImagePull": "image",
                  "OOMKilled": "oom", "CreateContainerConfigError": "config"}


def problem_class(reason: str) -> str:
    return _PROBLEM_CLASS.get(reason or "", reason or "unknown")


def last_good_update(deploy, current_hash: Optional[str] = None) -> Optional[Dict[str, str]]:
    """Deployment 全部可用、而且記錄的映像檔（或 Pod 範本）跟現在不同：回傳要寫的 annotation；不用寫回傳 None。
    只改 metadata（不改 Pod template），不會觸發重建。
    current_hash：目前 ReplicaSet 的 pod-template-hash（階段 3「退回最近的更新」用，見 recent_change_plan）。"""
    if not deployment_healthy(deploy):
        return None
    containers = deploy.spec.template.spec.containers or []
    if not containers:
        return None
    image = containers[0].image
    ann = deploy.metadata.annotations or {}
    upd = {}
    if ann.get(ANN_LAST_GOOD_IMAGE) != image:
        upd[ANN_LAST_GOOD_IMAGE] = image
    if current_hash and ann.get(ANN_LAST_GOOD_TEMPLATE) != current_hash:
        upd[ANN_LAST_GOOD_TEMPLATE] = current_hash
    return upd or None


# ── 階段 3.1：最近的更新造成崩潰 → 退回最後一個正常運作過的 Pod 範本 ─────────────
ANN_LAST_GOOD_TEMPLATE = "zerotouch.k8s/last-good-template"   # 曾經全部可用的 ReplicaSet 的 pod-template-hash
ANN_REVISION = "deployment.kubernetes.io/revision"
REVERT_WINDOW_S = 1800      # 最近 30 分鐘內的更新


def revert_recent_enabled() -> bool:
    return enabled() and os.environ.get("HEALER_REVERT_RECENT", "1").strip().lower() not in ("0", "false", "off", "no")


def _owned_by(rs, deploy_name: str) -> bool:
    return any(r.kind == "Deployment" and r.name == deploy_name for r in (rs.metadata.owner_references or []))


def current_replica_set(deploy, replica_sets):
    """Deployment 目前的 ReplicaSet（revision 跟 Deployment 一樣的那個）。"""
    rev = (deploy.metadata.annotations or {}).get(ANN_REVISION)
    for rs in replica_sets or []:
        if _owned_by(rs, deploy.metadata.name) and (rs.metadata.annotations or {}).get(ANN_REVISION) == rev:
            return rs
    return None


def recent_change_plan(deploy, replica_sets, now: Optional[float] = None):
    """回傳 (要退回的 ReplicaSet, None) 或 (None, 不退回的原因)。
    條件：目前的 ReplicaSet 是 30 分鐘內建立的（＝最近有更新）、有「曾經正常」的範本紀錄、而且跟目前的不同。"""
    now = now or time.time()
    cur = current_replica_set(deploy, replica_sets)
    if cur is None:
        return None, "找不到目前的版本"
    created = cur.metadata.creation_timestamp
    created_s = created.timestamp() if hasattr(created, "timestamp") else float(created or 0)
    if now - created_s > REVERT_WINDOW_S:
        return None, "最近 30 分鐘沒有更新過"
    good = (deploy.metadata.annotations or {}).get(ANN_LAST_GOOD_TEMPLATE)
    if not good:
        return None, "沒有「曾經正常運作過」的版本紀錄"
    cur_hash = (cur.metadata.labels or {}).get("pod-template-hash")
    if cur_hash == good:
        return None, "目前的版本就是曾經正常運作過的版本，問題不是剛才的更新造成的"
    target = next((rs for rs in replica_sets or [] if _owned_by(rs, deploy.metadata.name)
                   and (rs.metadata.labels or {}).get("pod-template-hash") == good), None)
    if target is None:
        return None, "曾經正常運作過的版本已經被清掉了"
    return target, None
