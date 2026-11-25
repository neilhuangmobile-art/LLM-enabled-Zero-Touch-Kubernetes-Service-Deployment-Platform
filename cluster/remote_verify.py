"""
cluster/remote_verify.py
客戶叢集的 Healer 修復驗證與退回（Healer 補強方案，2026-11-25）。

本機的驗證（healer/verify.py）直接讀叢集；客戶叢集平台連不進去，修復是送 commit、由客戶叢集的 Flux 套用，
所以這裡只用回報器（cluster/agent/reporter.py）傳回來的資料判斷：

1. 已同步：回報裡 Deployment 的映像檔／記憶體上限等於修正後的值（重新啟動沒有欄位可比，至少等 60 秒）
2. 健康：ready ＝ available ＝ 副本數、沒有 ProgressDeadlineExceeded、這個服務沒有不健康的 Pod，連續 90 秒
3. 修正送出後 10 分鐘還沒好 → 失敗；回報的設定還是我們改的值時才退回（中間使用者又改過就不碰，只停手）

純函式＋小型待驗證清單，web_demo 的 _remote_healer_loop 每輪呼叫。開關：HEALER_REMOTE_VERIFY=0。
"""
from __future__ import annotations

import os
import threading
import time
from typing import Dict, List, Optional, Set

HOLD_S = 90
DEADLINE_S = 600
RESTART_MIN_S = 60
ROLLBACK_ACTIONS = {"increase_memory", "fix_image"}   # 有改設定值、可以退回的（重新啟動沒有東西可以退）


def enabled() -> bool:
    return os.environ.get("HEALER_REMOTE_VERIFY", "1").strip().lower() not in ("0", "false", "off", "no")


class RemotePending:
    def __init__(self):
        self._items: Dict[str, Dict] = {}
        self._lock = threading.Lock()

    def register(self, key: str, namespace: str, app: str, action: str, expect: Dict, now: Optional[float] = None):
        if not enabled():
            return
        now = now or time.time()
        with self._lock:
            self._items[key] = {"key": key, "namespace": namespace, "app": app, "action": action,
                                "expect": dict(expect or {}), "sent_at": now, "deadline": now + DEADLINE_S,
                                "synced_at": None, "healthy_since": None}

    def items(self) -> List[Dict]:
        with self._lock:
            return list(self._items.values())

    def pop(self, key: str) -> Optional[Dict]:
        with self._lock:
            return self._items.pop(key, None)

    def has(self, key: str) -> bool:
        with self._lock:
            return key in self._items


PENDING = RemotePending()


def _deployment(rep: Dict, app: str) -> Optional[Dict]:
    return next((d for d in rep.get("deployments") or [] if d.get("name") == app), None)


def matches_expect(dep: Optional[Dict], expect: Dict) -> bool:
    return bool(dep) and all(dep.get(k) == v for k, v in (expect or {}).items())


def evaluate(rep: Dict, item: Dict, unhealthy_apps: Set[str], now: Optional[float] = None) -> Dict:
    """回傳 {"state": "ok"|"wait"|"fail", "why"}。會更新 item 的 synced_at／healthy_since。"""
    now = now or time.time()
    late = now >= item["deadline"]
    dep = _deployment(rep, item["app"])
    if dep is None:
        return {"state": "fail", "why": "回報裡找不到這個服務"} if late else {"state": "wait", "why": "等回報"}
    synced = matches_expect(dep, item["expect"]) if item["expect"] else now - item["sent_at"] >= RESTART_MIN_S
    if not synced:
        item["healthy_since"] = None
        return {"state": "fail", "why": "你的叢集一直沒有套用修正（Flux 可能沒有同步）"} if late else \
            {"state": "wait", "why": "等你的叢集同步"}
    item["synced_at"] = item["synced_at"] or now
    want = dep.get("replicas") or 0
    stuck = any("ProgressDeadlineExceeded" in str(c) for c in dep.get("conditions") or [])
    healthy = want > 0 and dep.get("ready") == want and dep.get("available") == want and not stuck \
        and item["app"] not in unhealthy_apps
    if not healthy:
        item["healthy_since"] = None
        why = "更新卡住" if stuck else (f"這個服務還有 Pod 不正常" if item["app"] in unhealthy_apps
                                         else f"可用 {dep.get('available') or 0}/{want}")
        return {"state": "fail", "why": why} if late else {"state": "wait", "why": why}
    item["healthy_since"] = item["healthy_since"] or now
    if now - item["healthy_since"] >= HOLD_S:
        return {"state": "ok", "why": f"同步完成、全部可用超過 {HOLD_S} 秒"}
    return {"state": "wait", "why": "觀察中"}


def can_rollback(rep: Dict, item: Dict) -> bool:
    """只有回報的設定還是我們改的值才退回；使用者中間又改過就不碰（revert_app 會把使用者的修改退掉）。"""
    return item["action"] in ROLLBACK_ACTIONS and bool(item["expect"]) and matches_expect(_deployment(rep, item["app"]),
                                                                                         item["expect"])
