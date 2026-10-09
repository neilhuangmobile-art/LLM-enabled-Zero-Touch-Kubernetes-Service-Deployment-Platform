"""
平台事件紀錄（2026-10-10）：部署、審查判決、擴縮、換映像檔、刪除、回滾、Healer 修復、資源快照。

用途：分析報告（agents/report.py）要能回答「過去這段時間發生了什麼」，而不是只看現在的快照。
原本這些資訊只存在記憶體（重啟就消失）或散在 Git 歷史裡。

設計：
- 只用 Python 內建的 sqlite3，不新增依賴；檔案在 data/events.db（data/ 已經在 .gitignore）。
- 每筆紀錄都帶 namespace，查詢一律要指定 namespace，沿用既有的「一個帳號一個 namespace」隔離，
  不提供跨 namespace 的查詢函式。
- 寫入失敗只印 log、不丟例外：紀錄是附加功能，不能讓部署或修復因為寫紀錄失敗而中斷。
- 超過保存期限（預設 180 天）的紀錄在啟動時清掉。
"""

import json
import os
import sqlite3
import threading
import time
from typing import Dict, List, Optional

RETENTION_DAYS = 180
KINDS = {"review", "deploy", "scale", "update", "delete", "rollback", "heal", "snapshot"}

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None
_path: Optional[str] = None


def _default_path() -> str:
    # 測試用 ZT_EVENTS_DB 指到暫存位置，不要寫進開發者的正式紀錄（見 tests/conftest.py）
    if os.environ.get("ZT_EVENTS_DB"):
        return os.environ["ZT_EVENTS_DB"]
    from core.config import DATA_DIR
    return os.path.join(DATA_DIR, "events.db")


def init(path: str = None) -> None:
    """開啟（或建立）資料庫。測試可以傳暫存路徑；重複呼叫會換到新的路徑。"""
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
        _conn.execute("""CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            namespace TEXT NOT NULL,
            username TEXT,
            kind TEXT NOT NULL,
            app TEXT,
            outcome TEXT,
            data TEXT
        )""")
        _conn.execute("CREATE INDEX IF NOT EXISTS idx_events_ns_ts ON events(namespace, ts)")
        _conn.execute("DELETE FROM events WHERE ts < ?", (time.time() - RETENTION_DAYS * 86400,))
        _conn.commit()
        _path = path


def record(namespace: str, kind: str, app: str = None, outcome: str = None, data: Dict = None,
           username: str = None, ts: float = None) -> bool:
    """寫入一筆紀錄。namespace 必填；kind 必須是 KINDS 之一。"""
    if not namespace or kind not in KINDS:
        return False
    try:
        if _conn is None:
            init()
        payload = json.dumps(data or {}, ensure_ascii=False, default=str)[:20000]
        with _lock:
            _conn.execute(
                "INSERT INTO events (ts, namespace, username, kind, app, outcome, data) VALUES (?,?,?,?,?,?,?)",
                (ts or time.time(), namespace, username, kind, (app or "")[:100] or None,
                 (outcome or "")[:40] or None, payload))
            _conn.commit()
        return True
    except Exception as e:
        print(f"[事件紀錄] 寫入失敗（不影響操作本身）：{e}")
        return False


def query(namespace: str, since: float = 0, kinds: List[str] = None, limit: int = 5000) -> List[Dict]:
    """查某個 namespace 在 since 之後的紀錄（時間由舊到新）。沒有跨 namespace 的版本，刻意的。"""
    if not namespace:
        return []
    if _conn is None:
        init()
    sql = "SELECT ts, namespace, username, kind, app, outcome, data FROM events WHERE namespace = ? AND ts >= ?"
    args: list = [namespace, since]
    if kinds:
        sql += " AND kind IN (%s)" % ",".join("?" * len(kinds))
        args += list(kinds)
    sql += " ORDER BY ts ASC LIMIT ?"
    args.append(int(limit))
    with _lock:
        rows = _conn.execute(sql, args).fetchall()
    out = []
    for ts, ns, user, kind, app, outcome, data in rows:
        try:
            d = json.loads(data or "{}")
        except Exception:
            d = {}
        out.append({"ts": ts, "namespace": ns, "username": user, "kind": kind, "app": app,
                    "outcome": outcome, "data": d})
    return out


def first_ts(namespace: str) -> Optional[float]:
    """這個 namespace 最早的一筆紀錄時間，報告用來說明「從什麼時候開始有紀錄」。"""
    if not namespace:
        return None
    if _conn is None:
        init()
    with _lock:
        row = _conn.execute("SELECT MIN(ts) FROM events WHERE namespace = ?", (namespace,)).fetchone()
    return row[0] if row and row[0] else None


def last_ts(namespace: str, kind: str) -> Optional[float]:
    if _conn is None:
        init()
    with _lock:
        row = _conn.execute("SELECT MAX(ts) FROM events WHERE namespace = ? AND kind = ?",
                            (namespace, kind)).fetchone()
    return row[0] if row and row[0] else None
