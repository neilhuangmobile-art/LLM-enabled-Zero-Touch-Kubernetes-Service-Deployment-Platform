"""
healer/verify.py
Healer 補強方案階段 1（docs/healer_plan_20261120.md，2026-11-25）：修完要驗證、沒好就退回。

- 動手前存快照（映像檔、resources、探針、revision、generation、各 Pod 重啟次數）
- 所有 patch 先 server-side dry-run，失敗就不動手
- 非阻塞驗證：動手後登記「待驗證」，背景迴圈每一輪檢查，不原地等
- 判斷修好只看 K8s 狀態：Deployment 更新完成且全部可用、Pod Ready 持續 90 秒、
  修復後建立的 Pod 沒有重啟、修復時間之後沒有新的 BackOff／Unhealthy／FailedScheduling 事件
- 超過期限還沒好 → 退回快照（同樣先 dry-run），每個問題最多修一次、退一次

依據：STRATUS（NeurIPS 2025）——每個動作配上復原動作、先模擬、失敗就退回檢查點。
開關：HEALER_VERIFY=0 關閉（只動手不驗證，回到原本的行為）。
"""
from __future__ import annotations

import copy
import os
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

VERIFY_MIN_S = 90          # 最早動手後 90 秒才判斷
VERIFY_DEADLINE_S = 300    # 最晚 5 分鐘還沒好就退回
READY_HOLD_S = 90          # Pod Ready 要持續 90 秒
BAD_EVENT_REASONS = {"BackOff", "Unhealthy", "FailedScheduling", "Failed", "OOMKilling"}
_PROBES = ("liveness_probe", "readiness_probe", "startup_probe")


def enabled() -> bool:
    return os.environ.get("HEALER_VERIFY", "1").strip().lower() not in ("0", "false", "off", "no")


class SkipPatch(Exception):
    """mutate 判斷不需要（或不能）改：不送出任何 patch。訊息給使用者看。"""


def _now() -> float:
    return time.time()


def snapshot(deploy) -> Dict:
    """只存會被 Healer 改的欄位（映像檔、resources、探針）與判斷用的 revision／generation。"""
    containers = {}
    for c in deploy.spec.template.spec.containers or []:
        containers[c.name] = {"image": c.image, "resources": copy.deepcopy(c.resources),
                              **{p: copy.deepcopy(getattr(c, p, None)) for p in _PROBES}}
    ann = deploy.metadata.annotations or {}
    # template：整個 Pod 範本（階段 3「退回最近的更新」會換掉整個範本，失敗時要整個換回來）
    return {"containers": containers, "revision": ann.get("deployment.kubernetes.io/revision"),
            "generation": deploy.metadata.generation, "annotations": dict(ann), "time": _now(),
            "template": copy.deepcopy(deploy.spec.template)}


def restore(deploy, snap: Dict) -> List[str]:
    """把 deploy 的容器設定改回快照的值，回傳改了哪些容器。"""
    changed = []
    for c in deploy.spec.template.spec.containers or []:
        s = snap["containers"].get(c.name)
        if not s:
            continue
        c.image = s["image"]
        c.resources = copy.deepcopy(s["resources"])
        for p in _PROBES:
            setattr(c, p, copy.deepcopy(s[p]))
        changed.append(c.name)
    return changed


def apply_patch(apps_api, namespace: str, name: str, mutate: Callable, replace: bool = False) -> Dict:
    """讀 Deployment → 存快照 → mutate(deploy) 回傳變更說明 → dry-run → 真的 patch。
    回傳 {"ok", "message", "snapshot", "change", "dry_run_error"}。mutate 丟 SkipPatch 時不送任何 patch。
    replace=True：整個物件取代（換掉整個 Pod 範本時用；patch 是合併，拿不掉新版多出來的環境變數等欄位）。
    取代會帶著剛讀到的 resourceVersion，中間有人改過就會失敗，不會蓋掉別人的修改。"""
    deploy = apps_api.read_namespaced_deployment(name, namespace)
    snap = snapshot(deploy)
    try:
        change = mutate(deploy)
    except SkipPatch as e:
        return {"ok": False, "skipped": True, "message": str(e), "snapshot": snap, "change": None}
    send = apps_api.replace_namespaced_deployment if replace else apps_api.patch_namespaced_deployment
    try:
        send(name, namespace, deploy, dry_run="All")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": f"試跑（dry-run）沒有通過，沒有動手：{_short(e)}", "snapshot": snap,
                "change": change, "dry_run_error": _short(e)}
    send(name, namespace, deploy)
    return {"ok": True, "message": "", "snapshot": snap, "change": change}


def _short(e) -> str:
    """K8s client 的例外會帶整串 HTTP header，只留第一行原因。"""
    s = str(getattr(e, "reason", "") or e)
    return s.splitlines()[0][:200] if s else type(e).__name__


# ── 待驗證清單 ─────────────────────────────────────────────────────
class Pending:
    def __init__(self):
        self._items: Dict[str, Dict] = {}
        self._lock = threading.Lock()

    def register(self, key: str, namespace: str, deployment: str, action: str, snap: Optional[Dict],
                 rollback_allowed: bool, now: Optional[float] = None, full_template: bool = False) -> None:
        if not enabled():
            return
        now = now or _now()
        with self._lock:
            self._items[key] = {"key": key, "namespace": namespace, "deployment": deployment, "action": action,
                                "snapshot": snap, "rollback_allowed": rollback_allowed, "fixed_at": now,
                                "full_template": full_template,
                                "due": now + VERIFY_MIN_S, "deadline": now + VERIFY_DEADLINE_S}

    def items(self) -> List[Dict]:
        with self._lock:
            return list(self._items.values())

    def pop(self, key: str) -> Optional[Dict]:
        with self._lock:
            return self._items.pop(key, None)

    def has(self, key: str) -> bool:
        with self._lock:
            return key in self._items


PENDING = Pending()


def _ts(t) -> Optional[float]:
    if t is None:
        return None
    if isinstance(t, (int, float)):
        return float(t)
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.timestamp()


def evaluate(apps_api, core_api, item: Dict, now: Optional[float] = None) -> Dict:
    """回傳 {"state": "ok"|"wait"|"fail", "why": 說明}。只看 K8s 狀態，不靠模型。"""
    now = now or _now()
    ns, name, fixed = item["namespace"], item["deployment"], item["fixed_at"]
    try:
        d = apps_api.read_namespaced_deployment(name, ns)
    except Exception as e:  # noqa: BLE001
        return {"state": "fail", "why": f"讀不到服務：{_short(e)}"}
    want = d.spec.replicas or 0
    st = d.status
    from healer import guard
    rolled = guard.deployment_healthy(d)   # 含「沒有卡住的舊 Pod」（2026-11-25 評估發現的漏洞）
    problems = []
    if not rolled:
        problems.append(f"更新還沒完成或還沒全部可用（可用 {st.available_replicas or 0}/{want}）")
    sel = ",".join(f"{k}={v}" for k, v in (d.spec.selector.match_labels or {}).items())
    pods = core_api.list_namespaced_pod(ns, label_selector=sel).items if sel else []
    names = set()
    for p in pods:
        names.add(p.metadata.name)
        ready = next((c for c in (p.status.conditions or []) if c.type == "Ready"), None)
        if not ready or ready.status != "True":
            problems.append(f"{p.metadata.name} 還沒就緒")
        elif now - (_ts(ready.last_transition_time) or now) < READY_HOLD_S:
            problems.append(f"{p.metadata.name} 就緒還不到 {READY_HOLD_S} 秒")
        created = _ts(p.metadata.creation_timestamp) or 0
        if created >= fixed - 1:
            restarts = sum(cs.restart_count or 0 for cs in (p.status.container_statuses or []))
            if restarts:
                return _final(item, now, f"修復後建立的 {p.metadata.name} 又重啟了 {restarts} 次")
    try:
        evs = core_api.list_namespaced_event(ns).items
    except Exception:  # noqa: BLE001
        evs = []
    for e in evs:
        obj = getattr(e, "involved_object", None)
        # 2026-11-25 評估發現：startupProbe 在程式啟動期間本來就會失敗幾次（Unhealthy: Startup probe failed），
        # 這是預期中的，不能當成「修完又壞」；真的啟動不了會超過 failureThreshold 被重啟，由上面的重啟次數抓到
        if e.reason == "Unhealthy" and (e.message or "").lower().startswith("startup probe failed"):
            continue
        if e.reason in BAD_EVENT_REASONS and obj is not None and (obj.name in names or obj.name == name):
            t = _ts(getattr(e, "last_timestamp", None) or getattr(e, "event_time", None))
            if t and t > fixed + 5:
                return _final(item, now, f"修復後又出現 {e.reason} 事件")
    if not problems and now >= item["due"]:
        return {"state": "ok", "why": "更新完成、全部可用、Pod 就緒超過 90 秒、沒有新的錯誤事件"}
    return _final(item, now, "；".join(problems[:3]) or "還在觀察") if now >= item["deadline"] else \
        {"state": "wait", "why": "；".join(problems[:3]) or "還在觀察"}


def _final(item: Dict, now: float, why: str) -> Dict:
    """有明確壞掉的跡象：過了最早判斷時間就算失敗，否則先等。"""
    return {"state": "fail", "why": why} if now >= item["due"] else {"state": "wait", "why": why}


def rollback(apps_api, item: Dict) -> Dict:
    """退回快照（先 dry-run）。沒有快照（例如重建 Pod）或不允許退回時不動手。"""
    snap = item.get("snapshot")
    if not snap or not item.get("rollback_allowed"):
        return {"ok": False, "message": "這個動作沒有可以退回的設定"}

    full = bool(item.get("full_template") and snap.get("template") is not None)

    def mutate(d):
        if full:
            d.spec.template = copy.deepcopy(snap["template"])
            return {"restored": ["template"]}
        changed = restore(d, snap)
        if not changed:
            raise SkipPatch("找不到要退回的容器")
        return {"restored": changed}

    # 2026-11-25 評估發現：合併式 patch 拿不掉修復時「新增」的欄位（例如 startupProbe），退回後還留著。
    # 退回一律整個物件取代（帶 resourceVersion，中間有人改過會失敗，不會蓋掉別人的修改）。
    r = apply_patch(apps_api, item["namespace"], item["deployment"], mutate, replace=True)
    if r.get("ok"):
        r["message"] = "已退回修復前的設定"
    return r
