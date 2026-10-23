"""
預約任務與行事曆備註（2026-10-23）。

預約到期**不會自動執行**：背景檢查只把到期的預約改成「等你確認」（due）並推播提醒，使用者在電腦上按
「確定執行」才會呼叫跟手動操作相同的 API（部署會重新審查一次）。叢集狀況在預約到執行之間可能已經變了，
執行的當下要有人在場負責。

- sqlite3，data/schedule.db（data/ 已經在 .gitignore）；查詢一律帶帳號，沒有跨帳號的版本。
- 狀態：scheduled → due（已提醒，等你確認）→ done / failed / blocked；另有 cancelled、expired。
"""

import json
import os
import sqlite3
import threading
import time
from typing import Dict, List, Optional

KINDS = ("deploy", "scale", "update_image", "delete")
ACTIVE = ("scheduled", "due")
FINAL = ("done", "failed", "blocked", "cancelled", "expired")
MIN_AHEAD = 120            # 至少 2 分鐘後
MAX_AHEAD = 90 * 86400     # 最多 90 天內
MAX_ACTIVE = 20            # 每個帳號最多 20 個還沒處理完的預約
DUE_EXPIRE = 7 * 86400     # 等你確認超過 7 天就過期
SNOOZE = 30 * 60           # 稍後再說：30 分鐘後再提醒
RUNNING_TIMEOUT = 15 * 60  # 按了確定執行 15 分鐘還沒回報結果，視為中斷
NOTE_MAX = 500

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None
_path: Optional[str] = None


class ScheduleError(ValueError):
    """給使用者看的錯誤（白話）。"""


def init(path: str = None) -> None:
    global _conn, _path
    if not path:
        from core.config import DATA_DIR
        path = os.environ.get("ZT_SCHEDULE_DB") or os.path.join(DATA_DIR, "schedule.db")
    with _lock:
        if _conn is not None and _path == path:
            return
        if _conn is not None:
            _conn.close()
        if path != ":memory:":
            os.makedirs(os.path.dirname(path), exist_ok=True)
        _conn = sqlite3.connect(path, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("""CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL, namespace TEXT NOT NULL,
            kind TEXT NOT NULL, args TEXT NOT NULL, summary TEXT NOT NULL, input TEXT,
            run_at REAL NOT NULL, created_at REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'scheduled',
            notified_at REAL, snooze_until REAL, finished_at REAL, result TEXT)""")
        _conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_user ON tasks(username, run_at)")
        _conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, run_at)")
        _conn.execute("""CREATE TABLE IF NOT EXISTS notes (
            username TEXT NOT NULL, day TEXT NOT NULL, text TEXT NOT NULL, updated_at REAL NOT NULL,
            PRIMARY KEY (username, day))""")
        _conn.commit()
        _path = path


def _db() -> sqlite3.Connection:
    if _conn is None:
        init()
    return _conn


def _row(r) -> Dict:
    d = dict(r)
    d["args"] = json.loads(d.get("args") or "{}")
    return d


def check_time(run_at: float, now: float = None) -> None:
    now = now or time.time()
    if run_at < now + MIN_AHEAD:
        raise ScheduleError("預約時間要在 2 分鐘以後。想馬上執行的話，請按「改成馬上執行」。")
    if run_at > now + MAX_AHEAD:
        raise ScheduleError("預約時間最多只能排到 90 天內。")


def create(username: str, namespace: str, kind: str, args: Dict, summary: str, run_at: float,
           text: str = "", now: float = None) -> Dict:
    if kind not in KINDS:
        raise ScheduleError("只能預約部署、擴縮、換映像檔、刪除這四種操作。")
    check_time(run_at, now)
    db = _db()
    with _lock:
        n = db.execute(f"SELECT COUNT(*) FROM tasks WHERE username=? AND status IN {ACTIVE}", (username,)).fetchone()[0]
        if n >= MAX_ACTIVE:
            raise ScheduleError(f"你已經有 {MAX_ACTIVE} 個還沒處理完的預約，請先取消或完成一些再新增。")
        cur = db.execute(
            "INSERT INTO tasks (username, namespace, kind, args, summary, input, run_at, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (username, namespace, kind, json.dumps(args, ensure_ascii=False), summary[:200], (text or "")[:500],
             run_at, now or time.time()))
        db.commit()
    return get(username, cur.lastrowid)


def get(username: str, task_id: int) -> Optional[Dict]:
    r = _db().execute("SELECT * FROM tasks WHERE id=? AND username=?", (task_id, username)).fetchone()
    return _row(r) if r else None


def list_tasks(username: str, statuses=None, since: float = None, until: float = None, limit: int = 200) -> List[Dict]:
    q, p = "SELECT * FROM tasks WHERE username=?", [username]
    if statuses:
        q += f" AND status IN ({','.join('?' * len(statuses))})"
        p += list(statuses)
    if since is not None:
        q += " AND run_at>=?"
        p.append(since)
    if until is not None:
        q += " AND run_at<?"
        p.append(until)
    q += " ORDER BY run_at LIMIT ?"
    p.append(limit)
    return [_row(r) for r in _db().execute(q, p).fetchall()]


def _update(username, task_id, sets: Dict, allowed_from) -> Optional[Dict]:
    db = _db()
    cols = ", ".join(f"{k}=?" for k in sets)
    with _lock:
        cur = db.execute(f"UPDATE tasks SET {cols} WHERE id=? AND username=? AND status IN ({','.join('?' * len(allowed_from))})",
                         list(sets.values()) + [task_id, username] + list(allowed_from))
        db.commit()
    return get(username, task_id) if cur.rowcount else None


def cancel(username: str, task_id: int, now: float = None) -> Optional[Dict]:
    return _update(username, task_id, {"status": "cancelled", "finished_at": now or time.time()}, ACTIVE)


def reschedule(username: str, task_id: int, run_at: float, now: float = None) -> Optional[Dict]:
    check_time(run_at, now)
    return _update(username, task_id, {"run_at": run_at, "status": "scheduled", "notified_at": None,
                                       "snooze_until": None}, ACTIVE)


def snooze(username: str, task_id: int, now: float = None) -> Optional[Dict]:
    return _update(username, task_id, {"snooze_until": (now or time.time()) + SNOOZE}, ("due",))


def start(username: str, task_id: int, now: float = None) -> Optional[Dict]:
    """使用者按了「確定執行」：搶到執行權才能執行，同一個預約不會被執行兩次（例如兩個分頁同時按）。
    notified_at 改記「開始執行的時間」，給 collect_due 判斷是不是卡住。"""
    return _update(username, task_id, {"status": "running", "notified_at": now or time.time()}, ACTIVE)


def finish(username: str, task_id: int, status: str, result: str, now: float = None) -> Optional[Dict]:
    if status not in ("done", "failed", "blocked"):
        raise ScheduleError("狀態不正確。")
    return _update(username, task_id, {"status": status, "result": (result or "")[:500],
                                       "finished_at": now or time.time()}, ("running",))


def collect_due(now: float = None) -> List[Dict]:
    """背景檢查：到期的 scheduled 改成 due，回傳這次新到期的（要推播的）；等太久的改成 expired。"""
    now = now or time.time()
    db = _db()
    with _lock:
        rows = db.execute("SELECT * FROM tasks WHERE status='scheduled' AND run_at<=?", (now,)).fetchall()
        newly = []
        for r in rows:
            cur = db.execute("UPDATE tasks SET status='due', notified_at=? WHERE id=? AND status='scheduled'", (now, r["id"]))
            if cur.rowcount:
                newly.append(dict(r))
        db.execute("UPDATE tasks SET status='expired', finished_at=?, result='等你確認超過 7 天，已過期' "
                   "WHERE status='due' AND run_at<=?", (now, now - DUE_EXPIRE))
        # 按了確定執行，但瀏覽器在回報結果前就關掉：不能一直卡在「執行中」
        db.execute("UPDATE tasks SET status='failed', finished_at=?, "
                   "result='執行後沒有收到結果（可能是瀏覽器被關掉），請到「我的服務」確認實際狀態' "
                   "WHERE status='running' AND COALESCE(notified_at, run_at)<=?", (now, now - RUNNING_TIMEOUT))
        db.commit()
    for d in newly:
        d["args"] = json.loads(d.get("args") or "{}")
        d["status"] = "due"
    return newly


def due_for(username: str, now: float = None, include_snoozed: bool = False) -> List[Dict]:
    now = now or time.time()
    q = "SELECT * FROM tasks WHERE username=? AND status='due'"
    if not include_snoozed:
        q += " AND (snooze_until IS NULL OR snooze_until<=?)"
        rows = _db().execute(q + " ORDER BY run_at", (username, now)).fetchall()
    else:
        rows = _db().execute(q + " ORDER BY run_at", (username,)).fetchall()
    return [_row(r) for r in rows]


def set_note(username: str, day: str, text: str, now: float = None) -> None:
    text = (text or "").strip()[:NOTE_MAX]
    db = _db()
    with _lock:
        if text:
            db.execute("INSERT INTO notes (username, day, text, updated_at) VALUES (?,?,?,?) "
                       "ON CONFLICT(username, day) DO UPDATE SET text=excluded.text, updated_at=excluded.updated_at",
                       (username, day, text, now or time.time()))
        else:
            db.execute("DELETE FROM notes WHERE username=? AND day=?", (username, day))
        db.commit()


def notes_between(username: str, first_day: str, last_day: str) -> Dict[str, str]:
    rows = _db().execute("SELECT day, text FROM notes WHERE username=? AND day>=? AND day<=?",
                         (username, first_day, last_day)).fetchall()
    return {r["day"]: r["text"] for r in rows}
