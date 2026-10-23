"""
分時擴縮與休眠模式的設定（2026-10-11）：每個帳號的時段排程、閒置幾天就休眠、例外清單。

設計跟 core/event_store.py 一樣：只用內建 sqlite3，檔案在 data/autoscale.db（data/ 不進版控）；
每筆資料都綁 namespace，查詢一律指定 namespace，沒有跨帳號的讀取函式。
服務目前是不是在休眠、原本開幾個，記在 Deployment 自己的 annotation 上（叢集是唯一的真相），
這裡只存「使用者設定了什麼」。
"""

import json
import os
import sqlite3
import threading
import time
from typing import Dict, List, Optional

DEFAULT_IDLE_DAYS = 5
MIN_IDLE_DAYS = 1
# 2026-10-21：上限從 30 天改成 7 天。這台 Prometheus 只保留 10 天資料（而且沒有永久儲存空間），超過保留期的天數
# 永遠判斷不出來；留 3 天緩衝，避免資料剛好卡在邊界時在「夠／不夠」之間跳動。
MAX_IDLE_DAYS = 7

# RLock：讀寫函式拿著鎖時會呼叫 _db()，第一次使用時 _db() 會呼叫 init()，init() 也要拿同一把鎖。
# 用一般的 Lock 會自己卡死（2026-10-11 實測：網站第一次查排程就整個停住）。
_lock = threading.RLock()
_conn: Optional[sqlite3.Connection] = None
_path: Optional[str] = None


def _default_path() -> str:
    # 測試用 ZT_AUTOSCALE_DB 指到暫存位置（見 tests/conftest.py）
    if os.environ.get("ZT_AUTOSCALE_DB"):
        return os.environ["ZT_AUTOSCALE_DB"]
    from core.config import DATA_DIR
    return os.path.join(DATA_DIR, "autoscale.db")


def init(path: str = None) -> None:
    global _conn, _path
    path = path or _default_path()
    with _lock:
        if _conn is not None and _path == path:
            return
        if _conn is not None:
            _conn.close()
        if path != ":memory:":
            os.makedirs(os.path.dirname(path), exist_ok=True)
        _conn = sqlite3.connect(path, check_same_thread=False)
        _conn.execute("""CREATE TABLE IF NOT EXISTS schedules (
            namespace TEXT NOT NULL,
            app TEXT NOT NULL,
            plan TEXT NOT NULL,
            created_at REAL NOT NULL,
            last_target INTEGER,
            PRIMARY KEY (namespace, app)
        )""")
        _conn.execute("""CREATE TABLE IF NOT EXISTS settings (
            namespace TEXT PRIMARY KEY,
            idle_days INTEGER,
            exempt TEXT
        )""")
        # 2026-10-23：每個服務每小時收到的封包數（學時段用）。Prometheus 只保留約 10 天，這裡自己存 60 天
        _conn.execute("""CREATE TABLE IF NOT EXISTS traffic_hourly (
            namespace TEXT NOT NULL,
            app TEXT NOT NULL,
            hour INTEGER NOT NULL,
            packets REAL NOT NULL,
            pods INTEGER NOT NULL,
            PRIMARY KEY (namespace, app, hour)
        )""")
        _conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        _conn.commit()
        _path = path


def _db() -> sqlite3.Connection:
    if _conn is None:
        init()
    return _conn


# ── 時段排程 ──
def save_schedule(namespace: str, plan: Dict) -> None:
    """新增或取代某個服務的排程。last_target 清空，讓背景迴圈馬上套用一次。"""
    with _lock:
        _db().execute("INSERT OR REPLACE INTO schedules (namespace, app, plan, created_at, last_target) "
                      "VALUES (?,?,?,?,NULL)", (namespace, plan["app"], json.dumps(plan, ensure_ascii=False), time.time()))
        _conn.commit()


def delete_schedule(namespace: str, app: str) -> bool:
    with _lock:
        cur = _db().execute("DELETE FROM schedules WHERE namespace = ? AND app = ?", (namespace, app))
        _conn.commit()
        return cur.rowcount > 0


def get_schedules(namespace: str) -> List[Dict]:
    with _lock:
        rows = _db().execute("SELECT app, plan, created_at, last_target FROM schedules WHERE namespace = ? ORDER BY app",
                             (namespace,)).fetchall()
    return [{"app": a, "plan": json.loads(p), "created_at": c, "last_target": t} for a, p, c, t in rows]


def all_schedules() -> List[Dict]:
    """背景迴圈用：所有帳號的排程（每筆都帶自己的 namespace，套用時只動那個 namespace）。"""
    with _lock:
        rows = _db().execute("SELECT namespace, app, plan, last_target FROM schedules").fetchall()
    return [{"namespace": n, "app": a, "plan": json.loads(p), "last_target": t} for n, a, p, t in rows]


def set_last_target(namespace: str, app: str, target: Optional[int]) -> None:
    with _lock:
        _db().execute("UPDATE schedules SET last_target = ? WHERE namespace = ? AND app = ?", (target, namespace, app))
        _conn.commit()


# ── 休眠設定 ──
def get_settings(namespace: str) -> Dict:
    with _lock:
        row = _db().execute("SELECT idle_days, exempt FROM settings WHERE namespace = ?", (namespace,)).fetchone()
    idle_days = row[0] if row and row[0] else DEFAULT_IDLE_DAYS
    idle_days = max(MIN_IDLE_DAYS, min(MAX_IDLE_DAYS, int(idle_days)))   # 舊版存過超過 7 天的設定也當 7 天
    exempt = json.loads(row[1]) if row and row[1] else []
    return {"idle_days": int(idle_days), "exempt": sorted(set(exempt))}


def _put_settings(namespace: str, idle_days: int, exempt: List[str]) -> None:
    with _lock:
        _db().execute("INSERT OR REPLACE INTO settings (namespace, idle_days, exempt) VALUES (?,?,?)",
                      (namespace, int(idle_days), json.dumps(sorted(set(exempt)))))
        _conn.commit()


def set_idle_days(namespace: str, days: int) -> int:
    days = max(MIN_IDLE_DAYS, min(MAX_IDLE_DAYS, int(days)))
    s = get_settings(namespace)
    _put_settings(namespace, days, s["exempt"])
    return days


def set_exempt(namespace: str, app: str, exempt: bool) -> List[str]:
    s = get_settings(namespace)
    names = set(s["exempt"])
    if exempt:
        names.add(app)
    else:
        names.discard(app)
    _put_settings(namespace, s["idle_days"], sorted(names)[:200])
    return sorted(names)


def forget_app(namespace: str, app: str) -> None:
    """服務被刪除時清掉它的排程、例外與流量紀錄，避免之後同名的新服務繼承舊設定或舊的流量規律。"""
    delete_schedule(namespace, app)
    with _lock:
        _db().execute("DELETE FROM traffic_hourly WHERE namespace = ? AND app = ?", (namespace, app))
        _conn.commit()
    s = get_settings(namespace)
    if app in s["exempt"]:
        set_exempt(namespace, app, False)


# ── 每小時流量（學時段用，2026-10-23）──
TRAFFIC_KEEP_DAYS = 60


def add_traffic(rows: List[tuple]) -> int:
    """rows：[(namespace, app, 整點的 UTC 秒, 封包數, Pod 數)]。同一小時重複寫入會取代，不會變兩筆。順便清掉 60 天前的。"""
    with _lock:
        _db().executemany("INSERT OR REPLACE INTO traffic_hourly (namespace, app, hour, packets, pods) VALUES (?,?,?,?,?)",
                          [(n, a, int(h), float(p), max(1, int(c))) for n, a, h, p, c in rows])
        _conn.execute("DELETE FROM traffic_hourly WHERE hour < ?", (int(time.time()) - TRAFFIC_KEEP_DAYS * 86400,))
        _conn.commit()
    return len(rows)


def traffic_for(namespace: str, app: str, since: float = 0) -> List[tuple]:
    """[(整點的 UTC 秒, 封包數, Pod 數)]，舊的在前。"""
    with _lock:
        return _db().execute("SELECT hour, packets, pods FROM traffic_hourly WHERE namespace = ? AND app = ? AND hour >= ? "
                             "ORDER BY hour", (namespace, app, int(since))).fetchall()


def has_traffic_hour(hour: int) -> bool:
    with _lock:
        return _db().execute("SELECT 1 FROM traffic_hourly WHERE hour = ? LIMIT 1", (int(hour),)).fetchone() is not None


def meta_get(key: str) -> Optional[str]:
    with _lock:
        row = _db().execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def meta_set(key: str, value: str) -> None:
    with _lock:
        _db().execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))
        _conn.commit()
