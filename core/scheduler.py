"""
共用背景排程器（2026-10-21）：分時擴縮、休眠／喚醒，以及之後的行事曆排程，都掛在同一條背景執行緒上，
不要各自再開一條「時間到就執行」的迴圈（兩套排程器會互相打架，見 CLAUDE.md「分時擴縮與閒置休眠」）。

怎麼掛一個新的定時工作（例如柏碩的行事曆）：

    from core import scheduler

    def _calendar_tick():
        ...  # 檢查有沒有到時間的行程，到了就執行；單次執行要快，耗時的工作自己開執行緒

    scheduler.register("行事曆", _calendar_tick, every_seconds=30, needs_k8s=True)

web_demo.py 啟動時會呼叫 scheduler.start(...)；register 在 start 之前或之後都可以。

規則：
- 排程器每 TICK_SECONDS（10 秒）醒來一次，距離上次執行超過 every_seconds 的工作才會執行；every_seconds=0 代表每次醒來都執行。
- needs_k8s=True 的工作在 Kubernetes 沒有連線時跳過（下次醒來再看）。
- 單一工作出錯只會印一行紀錄，不影響其他工作，也不會讓排程器停掉。
- 同名的工作重複 register 會取代舊的，不會變成兩份。
"""

import threading
import time
from typing import Callable, Dict, List, Optional

TICK_SECONDS = 10

_lock = threading.Lock()
_tasks: Dict[str, Dict] = {}
_thread: Optional[threading.Thread] = None


def register(name: str, fn: Callable[[], None], every_seconds: float = 0, needs_k8s: bool = True) -> None:
    with _lock:
        _tasks[name] = {"name": name, "fn": fn, "every": max(0.0, float(every_seconds)),
                        "needs_k8s": bool(needs_k8s), "last": 0.0}


def unregister(name: str) -> None:
    with _lock:
        _tasks.pop(name, None)


def tasks() -> List[str]:
    with _lock:
        return list(_tasks)


def run_due(now: float = None, k8s_ready: bool = True) -> List[str]:
    """執行所有到時間的工作，回傳這次執行了哪些（測試用，也是背景迴圈每次醒來做的事）。"""
    now = time.time() if now is None else now
    with _lock:
        due = [t for t in _tasks.values()
               if (k8s_ready or not t["needs_k8s"]) and now - t["last"] >= t["every"]]
        for t in due:
            t["last"] = now
    ran = []
    for t in due:
        try:
            t["fn"]()
            ran.append(t["name"])
        except Exception as e:
            print(f"[背景排程] {t['name']} 失敗：{e}")
    return ran


def start(is_k8s_ready: Callable[[], bool] = lambda: True) -> None:
    """啟動背景執行緒（重複呼叫不會開第二條）。"""
    global _thread
    with _lock:
        if _thread is not None and _thread.is_alive():
            return

        def _loop():
            while True:
                time.sleep(TICK_SECONDS)
                try:
                    ready = bool(is_k8s_ready())
                except Exception:
                    ready = False
                run_due(k8s_ready=ready)

        _thread = threading.Thread(target=_loop, name="zt-scheduler", daemon=True)
        _thread.start()
