"""
對話與專案（2026-10-21）：對話紀錄改存伺服器，加上「專案」（說明、專案指示、服務歸屬、上傳的檔案）。

設計跟 core/autoscale_store.py 一樣：只用內建 sqlite3，檔案在 data/workspace.db（data/ 不進版控）。
對話、專案、檔案每筆都綁 owner（登入帳號），服務歸屬綁 namespace（服務真正所在的地方）；
查詢一律指定 owner／namespace，沒有跨帳號的讀取函式。專案不做共享。
"""

import json
import os
import re
import secrets
import sqlite3
import threading
import time
from typing import Dict, List, Optional

MAX_CHAT_BYTES = 2_000_000       # 單一對話（含部署卡片 HTML）的上限
MAX_CHATS = 500                  # 每個帳號最多幾則對話
MAX_TITLE = 80
MAX_PROJECTS = 100
MAX_PROJECT_NAME = 40
MAX_PROJECT_TEXT = 2000          # 說明、專案指示各自的上限
MAX_FILE_BYTES = 100_000         # 跟 compose 上傳的上限一致
MAX_FILES_PER_PROJECT = 50

CHAT_ID_RE = re.compile(r"^chat_[A-Za-z0-9_]{1,40}$")
PROJECT_ID_RE = re.compile(r"^proj_[a-f0-9]{12}$")
FILE_ID_RE = re.compile(r"^file_[a-f0-9]{12}$")


class WorkspaceError(ValueError):
    """使用者看得懂的錯誤訊息（路由直接回給畫面）。"""


# RLock：讀寫函式拿著鎖時會呼叫 _db()，第一次使用時 _db() 會呼叫 init()，init() 也要拿同一把鎖（見 autoscale_store）
_lock = threading.RLock()
_conn: Optional[sqlite3.Connection] = None
_path: Optional[str] = None


def _default_path() -> str:
    # 測試用 ZT_WORKSPACE_DB 指到暫存位置（見 tests/conftest.py）
    if os.environ.get("ZT_WORKSPACE_DB"):
        return os.environ["ZT_WORKSPACE_DB"]
    from core.config import DATA_DIR
    return os.path.join(DATA_DIR, "workspace.db")


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
        _conn.execute("""CREATE TABLE IF NOT EXISTS chats (
            owner TEXT NOT NULL, id TEXT NOT NULL, project_id TEXT,
            title TEXT NOT NULL, messages TEXT NOT NULL, updated_at REAL NOT NULL,
            PRIMARY KEY (owner, id))""")
        _conn.execute("""CREATE TABLE IF NOT EXISTS projects (
            owner TEXT NOT NULL, id TEXT NOT NULL, name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '', instructions TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL, updated_at REAL NOT NULL,
            PRIMARY KEY (owner, id))""")
        # 2026-10-23：部署預設值（JSON）、回收桶（deleted_at、deleted_with）。舊的資料庫沒有這些欄位，補上
        for stmt in ("ALTER TABLE projects ADD COLUMN defaults TEXT NOT NULL DEFAULT '{}'",
                     "ALTER TABLE projects ADD COLUMN deleted_at REAL",
                     "ALTER TABLE chats ADD COLUMN deleted_at REAL",
                     "ALTER TABLE chats ADD COLUMN deleted_with TEXT"):
            try:
                _conn.execute(stmt)
            except sqlite3.OperationalError:
                pass
        _conn.execute("""CREATE TABLE IF NOT EXISTS project_services (
            namespace TEXT NOT NULL, app TEXT NOT NULL, owner TEXT NOT NULL, project_id TEXT NOT NULL,
            PRIMARY KEY (namespace, app))""")
        # 2026-10-23：每週自動產生的分析報告、使用者偏好（例如關掉每週報告）
        _conn.execute("""CREATE TABLE IF NOT EXISTS reports (
            owner TEXT NOT NULL, id TEXT NOT NULL, week TEXT NOT NULL, created_at REAL NOT NULL,
            data TEXT NOT NULL, seen_at REAL, PRIMARY KEY (owner, id))""")
        _conn.execute("CREATE TABLE IF NOT EXISTS prefs (owner TEXT NOT NULL, key TEXT NOT NULL, value TEXT, PRIMARY KEY (owner, key))")
        _conn.execute("""CREATE TABLE IF NOT EXISTS project_files (
            owner TEXT NOT NULL, project_id TEXT NOT NULL, id TEXT NOT NULL,
            name TEXT NOT NULL, format TEXT NOT NULL, content TEXT NOT NULL, created_at REAL NOT NULL,
            PRIMARY KEY (owner, id))""")
        _conn.commit()
        _path = path


def _db() -> sqlite3.Connection:
    if _conn is None:
        init()
    return _conn


def _clean_text(v, limit: int) -> str:
    return str(v or "").replace("\x00", "").strip()[:limit]


# ── 對話 ──
def list_chats(owner: str) -> List[Dict]:
    with _lock:
        rows = _db().execute("SELECT id, project_id, title, messages, updated_at FROM chats WHERE owner = ? "
                             "AND deleted_at IS NULL ORDER BY updated_at", (owner,)).fetchall()
    return [{"id": i, "projectId": p, "title": t, "messages": json.loads(m), "updated_at": u}
            for i, p, t, m, u in rows]


def put_chat(owner: str, chat_id: str, title: str, messages: list, project_id: Optional[str] = None) -> None:
    if not CHAT_ID_RE.match(str(chat_id or "")):
        raise WorkspaceError("對話編號格式不對。")
    if not isinstance(messages, list):
        raise WorkspaceError("對話內容格式不對。")
    body = json.dumps(messages, ensure_ascii=False)
    if len(body.encode("utf-8")) > MAX_CHAT_BYTES:
        raise WorkspaceError("這則對話太長了，沒辦法再存。請開一則新對話繼續。")
    if project_id and not get_project(owner, project_id):
        project_id = None   # 專案不存在或不是自己的：當成一般對話
    with _lock:
        exists = _db().execute("SELECT 1 FROM chats WHERE owner = ? AND id = ?", (owner, chat_id)).fetchone()
        if not exists:
            n = _conn.execute("SELECT COUNT(*) FROM chats WHERE owner = ? AND deleted_at IS NULL", (owner,)).fetchone()[0]
            if n >= MAX_CHATS:
                raise WorkspaceError(f"對話已經有 {MAX_CHATS} 則，請先刪掉一些舊對話。")
        _conn.execute("INSERT OR REPLACE INTO chats (owner, id, project_id, title, messages, updated_at) "
                      "VALUES (?,?,?,?,?,?)",
                      (owner, chat_id, project_id or None, _clean_text(title, MAX_TITLE) or "新對話", body, time.time()))
        _conn.commit()


def delete_chat(owner: str, chat_id: str) -> bool:
    """有內容的對話移到回收桶（7 天內可以復原）；空白對話直接刪掉（2026-10-23）。"""
    with _lock:
        _db().execute("DELETE FROM chats WHERE owner = ? AND id = ? AND messages = '[]'", (owner, chat_id))
        cur = _conn.execute("UPDATE chats SET deleted_at = ?, deleted_with = NULL WHERE owner = ? AND id = ? "
                            "AND deleted_at IS NULL", (time.time(), owner, chat_id))
        _conn.commit()
        return cur.rowcount > 0


def chats_of(owner: str, project_id: str) -> List[Dict]:
    """專案裡的對話（只回標題與時間，不回內容）。"""
    with _lock:
        rows = _db().execute("SELECT id, title, updated_at FROM chats WHERE owner = ? AND project_id = ? "
                             "AND deleted_at IS NULL ORDER BY updated_at DESC", (owner, project_id)).fetchall()
    return [{"id": i, "title": t, "updated_at": u} for i, t, u in rows]


# ── 專案 ──
def _project_row(r) -> Dict:
    try:
        defaults = json.loads(r[6] or "{}") if len(r) > 6 else {}
    except ValueError:
        defaults = {}
    return {"id": r[0], "name": r[1], "description": r[2], "instructions": r[3], "created_at": r[4], "updated_at": r[5],
            "defaults": defaults if isinstance(defaults, dict) else {}}


def list_projects(owner: str) -> List[Dict]:
    with _lock:
        rows = _db().execute("SELECT id, name, description, instructions, created_at, updated_at, defaults FROM projects "
                             "WHERE owner = ? AND deleted_at IS NULL ORDER BY created_at", (owner,)).fetchall()
    return [_project_row(r) for r in rows]


def get_project(owner: str, project_id: str) -> Optional[Dict]:
    if not PROJECT_ID_RE.match(str(project_id or "")):
        return None
    with _lock:
        r = _db().execute("SELECT id, name, description, instructions, created_at, updated_at, defaults FROM projects "
                          "WHERE owner = ? AND id = ? AND deleted_at IS NULL", (owner, project_id)).fetchone()
    return _project_row(r) if r else None


def _check_name(name: str) -> str:
    name = _clean_text(name, MAX_PROJECT_NAME + 1)
    if not name:
        raise WorkspaceError("請輸入專案名稱。")
    if len(name) > MAX_PROJECT_NAME:
        raise WorkspaceError(f"專案名稱最多 {MAX_PROJECT_NAME} 個字。")
    return name


def create_project(owner: str, name: str, description: str = "") -> Dict:
    name = _check_name(name)
    with _lock:
        n = _db().execute("SELECT COUNT(*) FROM projects WHERE owner = ? AND deleted_at IS NULL", (owner,)).fetchone()[0]
        if n >= MAX_PROJECTS:
            raise WorkspaceError(f"專案最多 {MAX_PROJECTS} 個。")
        pid, now = "proj_" + secrets.token_hex(6), time.time()
        _conn.execute("INSERT INTO projects (owner, id, name, description, instructions, created_at, updated_at) "
                      "VALUES (?,?,?,?,?,?,?)", (owner, pid, name, _clean_text(description, MAX_PROJECT_TEXT), "", now, now))
        _conn.commit()
    return get_project(owner, pid)


_MEM_RE = re.compile(r"^\d+(Mi|Gi|Ki|M|G)$")
_CPU_RE = re.compile(r"^(\d{1,6}m|\d{1,3}(\.\d{1,3})?)$")


def check_defaults(d, max_replicas: int = 50) -> Dict:
    """部署預設值（2026-10-23）：至少幾個副本、每個副本的記憶體、CPU。格式不對丟 WorkspaceError；空白的欄位不存。"""
    if not isinstance(d, dict):
        raise WorkspaceError("部署預設值的格式不對。")
    out = {}
    mr = d.get("min_replicas")
    if mr not in (None, ""):
        try:
            mr = int(mr)
        except (TypeError, ValueError):
            raise WorkspaceError("「至少幾個副本」要是整數。")
        if not 1 <= mr <= max_replicas:
            raise WorkspaceError(f"「至少幾個副本」要在 1 到 {max_replicas} 之間。")
        out["min_replicas"] = mr
    mem = str(d.get("memory") or "").strip()
    if mem:
        if not _MEM_RE.match(mem):
            raise WorkspaceError("記憶體的寫法要像 256Mi 或 1Gi。")
        out["memory"] = mem
    cpu = str(d.get("cpu") or "").strip()
    if cpu:
        if not _CPU_RE.match(cpu):
            raise WorkspaceError("CPU 的寫法要像 100m 或 0.5。")
        out["cpu"] = cpu
    return out


def update_project(owner: str, project_id: str, name=None, description=None, instructions=None,
                   defaults=None, max_replicas: int = 50) -> Dict:
    p = get_project(owner, project_id)
    if not p:
        raise WorkspaceError("找不到這個專案。")
    if defaults is not None:
        p["defaults"] = check_defaults(defaults, max_replicas)
    if name is not None:
        p["name"] = _check_name(name)
    if description is not None:
        p["description"] = _clean_text(description, MAX_PROJECT_TEXT)
    if instructions is not None:
        p["instructions"] = _clean_text(instructions, MAX_PROJECT_TEXT)
    with _lock:
        _db().execute("UPDATE projects SET name = ?, description = ?, instructions = ?, defaults = ?, updated_at = ? "
                      "WHERE owner = ? AND id = ?",
                      (p["name"], p["description"], p["instructions"], json.dumps(p.get("defaults") or {}),
                       time.time(), owner, project_id))
        _conn.commit()
    return get_project(owner, project_id)


def delete_project_row(owner: str, project_id: str) -> None:
    """專案移到回收桶（2026-10-23）；呼叫前要先處理好對話與服務（見 web_demo 的刪除專案路由）。
    還留著的一般對話變成一般對話、服務歸屬清掉（服務本身不會進回收桶）；檔案、說明、指示、部署預設值留在回收桶的專案裡。"""
    with _lock:
        _db().execute("UPDATE chats SET project_id = NULL WHERE owner = ? AND project_id = ? AND deleted_at IS NULL",
                      (owner, project_id))
        _conn.execute("DELETE FROM project_services WHERE owner = ? AND project_id = ?", (owner, project_id))
        _conn.execute("UPDATE projects SET deleted_at = ? WHERE owner = ? AND id = ?", (time.time(), owner, project_id))
        _conn.commit()


def move_chats_out(owner: str, project_id: str) -> int:
    with _lock:
        cur = _db().execute("UPDATE chats SET project_id = NULL WHERE owner = ? AND project_id = ? AND deleted_at IS NULL",
                            (owner, project_id))
        _conn.commit()
        return cur.rowcount


def delete_chats_of(owner: str, project_id: str, with_project: bool = False) -> int:
    """專案裡的對話移到回收桶。with_project=True：跟專案一起刪，復原專案時一起回來。"""
    with _lock:
        cur = _db().execute("UPDATE chats SET deleted_at = ?, deleted_with = ? WHERE owner = ? AND project_id = ? "
                            "AND deleted_at IS NULL", (time.time(), project_id if with_project else None, owner, project_id))
        _conn.commit()
        return cur.rowcount


# ── 服務歸屬（一個服務只屬一個專案）──
def assign_service(namespace: str, app: str, owner: str, project_id: str) -> None:
    if not get_project(owner, project_id):
        raise WorkspaceError("找不到這個專案。")
    with _lock:
        _db().execute("INSERT OR REPLACE INTO project_services (namespace, app, owner, project_id) VALUES (?,?,?,?)",
                      (namespace, app, owner, project_id))
        _conn.commit()


def unassign_service(namespace: str, app: str, owner: str) -> bool:
    with _lock:
        cur = _db().execute("DELETE FROM project_services WHERE namespace = ? AND app = ? AND owner = ?",
                            (namespace, app, owner))
        _conn.commit()
        return cur.rowcount > 0


def services_of(namespace: str, owner: str, project_id: str) -> List[str]:
    with _lock:
        rows = _db().execute("SELECT app FROM project_services WHERE namespace = ? AND owner = ? AND project_id = ? "
                             "ORDER BY app", (namespace, owner, project_id)).fetchall()
    return [r[0] for r in rows]


def service_projects(namespace: str, owner: str) -> Dict[str, str]:
    """這個帳號每個服務屬於哪個專案：{app: project_id}。"""
    with _lock:
        rows = _db().execute("SELECT app, project_id FROM project_services WHERE namespace = ? AND owner = ?",
                             (namespace, owner)).fetchall()
    return {a: p for a, p in rows}


def forget_service(namespace: str, app: str) -> None:
    """服務被刪除時清掉歸屬，之後同名的新服務不會繼承舊的專案。"""
    with _lock:
        _db().execute("DELETE FROM project_services WHERE namespace = ? AND app = ?", (namespace, app))
        _conn.commit()


# ── 專案檔案 ──
def add_file(owner: str, project_id: str, name: str, fmt: str, content: str) -> Dict:
    if not get_project(owner, project_id):
        raise WorkspaceError("找不到這個專案。")
    if len(str(content or "").encode("utf-8")) > MAX_FILE_BYTES:
        raise WorkspaceError(f"檔案太大（上限 {MAX_FILE_BYTES // 1000} KB）。")
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", _clean_text(name, 120)) or "upload.yaml"
    with _lock:
        n = _db().execute("SELECT COUNT(*) FROM project_files WHERE owner = ? AND project_id = ?",
                          (owner, project_id)).fetchone()[0]
        if n >= MAX_FILES_PER_PROJECT:
            raise WorkspaceError(f"每個專案最多存 {MAX_FILES_PER_PROJECT} 個檔案。")
        fid = "file_" + secrets.token_hex(6)
        _conn.execute("INSERT INTO project_files (owner, project_id, id, name, format, content, created_at) "
                      "VALUES (?,?,?,?,?,?,?)", (owner, project_id, fid, name, _clean_text(fmt, 20) or "yaml",
                                                 str(content), time.time()))
        _conn.commit()
    return {"id": fid, "name": name}


def list_files(owner: str, project_id: str) -> List[Dict]:
    with _lock:
        rows = _db().execute("SELECT id, name, format, LENGTH(content), created_at FROM project_files "
                             "WHERE owner = ? AND project_id = ? ORDER BY created_at DESC", (owner, project_id)).fetchall()
    return [{"id": i, "name": n, "format": f, "size": s, "created_at": c} for i, n, f, s, c in rows]


def get_file(owner: str, project_id: str, file_id: str) -> Optional[Dict]:
    if not FILE_ID_RE.match(str(file_id or "")):
        return None
    with _lock:
        r = _db().execute("SELECT id, name, format, content FROM project_files WHERE owner = ? AND project_id = ? AND id = ?",
                          (owner, project_id, file_id)).fetchone()
    return {"id": r[0], "name": r[1], "format": r[2], "content": r[3]} if r else None


def delete_file(owner: str, project_id: str, file_id: str) -> bool:
    with _lock:
        cur = _db().execute("DELETE FROM project_files WHERE owner = ? AND project_id = ? AND id = ?",
                            (owner, project_id, file_id))
        _conn.commit()
        return cur.rowcount > 0


def delete_files_of(owner: str, project_id: str) -> int:
    with _lock:
        cur = _db().execute("DELETE FROM project_files WHERE owner = ? AND project_id = ?", (owner, project_id))
        _conn.commit()
        return cur.rowcount


# ── 回收桶（2026-10-23）：刪除的對話與專案保留 7 天，期間可以復原。服務不會進回收桶（刪服務是真的從叢集刪掉）。──
TRASH_DAYS = 7


def purge_trash(owner: Optional[str] = None, now: float = None) -> int:
    """把回收桶裡超過 7 天的真的刪掉（owner=None：所有帳號，背景用）。回傳刪了幾筆。"""
    cutoff = (now or time.time()) - TRASH_DAYS * 86400
    who, args = ("AND owner = ?", (owner,)) if owner else ("", ())
    with _lock:
        old = _db().execute(f"SELECT owner, id FROM projects WHERE deleted_at IS NOT NULL AND deleted_at < ? {who}",
                            (cutoff, *args)).fetchall()
        for o, pid in old:
            _conn.execute("DELETE FROM project_files WHERE owner = ? AND project_id = ?", (o, pid))
            _conn.execute("DELETE FROM chats WHERE owner = ? AND deleted_with = ?", (o, pid))
        n = _conn.execute(f"DELETE FROM projects WHERE deleted_at IS NOT NULL AND deleted_at < ? {who}", (cutoff, *args)).rowcount
        n += _conn.execute(f"DELETE FROM chats WHERE deleted_at IS NOT NULL AND deleted_at < ? {who}", (cutoff, *args)).rowcount
        _conn.commit()
    return n


def list_trash(owner: str, now: float = None) -> List[Dict]:
    """自己的回收桶：一般刪除的對話＋刪除的專案（跟專案一起刪的對話算在專案裡）。"""
    now = now or time.time()
    purge_trash(owner, now)
    with _lock:
        chats = _db().execute("SELECT id, title, deleted_at, project_id FROM chats WHERE owner = ? AND deleted_at IS NOT NULL "
                              "AND deleted_with IS NULL ORDER BY deleted_at DESC", (owner,)).fetchall()
        projs = _conn.execute("SELECT id, name, deleted_at FROM projects WHERE owner = ? AND deleted_at IS NOT NULL "
                              "ORDER BY deleted_at DESC", (owner,)).fetchall()
        counts = {pid: (_conn.execute("SELECT COUNT(*) FROM chats WHERE owner = ? AND deleted_with = ?", (owner, pid)).fetchone()[0],
                        _conn.execute("SELECT COUNT(*) FROM project_files WHERE owner = ? AND project_id = ?", (owner, pid)).fetchone()[0])
                  for pid, _, _ in projs}
    left = lambda t: max(0, int((t + TRASH_DAYS * 86400 - now) // 86400) + 1)
    items = [{"type": "chat", "id": i, "title": t, "deleted_at": d, "days_left": left(d)} for i, t, d, _p in chats]
    items += [{"type": "project", "id": i, "title": n, "deleted_at": d, "days_left": left(d),
               "chats": counts[i][0], "files": counts[i][1]} for i, n, d in projs]
    return sorted(items, key=lambda x: -x["deleted_at"])


def restore_chat(owner: str, chat_id: str) -> Dict:
    """復原對話：原本的專案還在就回到專案，不在了就變成一般對話。"""
    with _lock:
        r = _db().execute("SELECT project_id, title FROM chats WHERE owner = ? AND id = ? AND deleted_at IS NOT NULL",
                          (owner, chat_id)).fetchone()
    if not r:
        raise WorkspaceError("回收桶裡找不到這則對話，可能已經超過 7 天被刪掉了。")
    pid = r[0] if r[0] and get_project(owner, r[0]) else None
    with _lock:
        _db().execute("UPDATE chats SET deleted_at = NULL, deleted_with = NULL, project_id = ? WHERE owner = ? AND id = ?",
                      (pid, owner, chat_id))
        _conn.commit()
    return {"title": r[1], "project_id": pid}


def restore_project(owner: str, project_id: str) -> Dict:
    """復原專案：說明、指示、部署預設值、檔案、跟它一起刪的對話都回來；服務不會回來。名稱撞到現有專案時加「（復原）」。"""
    with _lock:
        r = _db().execute("SELECT name FROM projects WHERE owner = ? AND id = ? AND deleted_at IS NOT NULL",
                          (owner, project_id)).fetchone()
    if not r:
        raise WorkspaceError("回收桶裡找不到這個專案，可能已經超過 7 天被刪掉了。")
    name = r[0]
    taken = {p["name"] for p in list_projects(owner)}
    if name in taken:
        name = (name[:MAX_PROJECT_NAME - 4] + "（復原）")
    if len(list_projects(owner)) >= MAX_PROJECTS:
        raise WorkspaceError(f"專案已經有 {MAX_PROJECTS} 個，請先刪掉一些再復原。")
    with _lock:
        _db().execute("UPDATE projects SET deleted_at = NULL, name = ?, updated_at = ? WHERE owner = ? AND id = ?",
                      (name, time.time(), owner, project_id))
        n = _conn.execute("UPDATE chats SET deleted_at = NULL, deleted_with = NULL, project_id = ? WHERE owner = ? AND deleted_with = ?",
                          (project_id, owner, project_id)).rowcount
        _conn.commit()
    return {"name": name, "chats": n}


def purge_item(owner: str, kind: str, item_id: str) -> bool:
    """馬上永久刪除回收桶裡的一筆（只刪得到自己的、而且在回收桶裡的）。"""
    with _lock:
        if kind == "chat":
            cur = _db().execute("DELETE FROM chats WHERE owner = ? AND id = ? AND deleted_at IS NOT NULL", (owner, item_id))
        elif kind == "project":
            cur = _db().execute("DELETE FROM projects WHERE owner = ? AND id = ? AND deleted_at IS NOT NULL", (owner, item_id))
            if cur.rowcount:
                _conn.execute("DELETE FROM project_files WHERE owner = ? AND project_id = ?", (owner, item_id))
                _conn.execute("DELETE FROM chats WHERE owner = ? AND deleted_with = ?", (owner, item_id))
        else:
            return False
        _conn.commit()
        return cur.rowcount > 0


# ── 每週報告（2026-10-23）──
MAX_REPORTS = 12


def has_report(owner: str, week: str) -> bool:
    with _lock:
        return _db().execute("SELECT 1 FROM reports WHERE owner = ? AND week = ?", (owner, week)).fetchone() is not None


def add_report(owner: str, week: str, data: Dict) -> str:
    """存一份每週報告；同一週已經有就不重複。每個帳號只留最近 MAX_REPORTS 份。"""
    rid = "rep_" + secrets.token_hex(6)
    with _lock:
        if _db().execute("SELECT 1 FROM reports WHERE owner = ? AND week = ?", (owner, week)).fetchone():
            return ""
        _conn.execute("INSERT INTO reports (owner, id, week, created_at, data, seen_at) VALUES (?,?,?,?,?,NULL)",
                      (owner, rid, week, time.time(), json.dumps(data, ensure_ascii=False)))
        _conn.execute("DELETE FROM reports WHERE owner = ? AND id NOT IN (SELECT id FROM reports WHERE owner = ? "
                      "ORDER BY created_at DESC LIMIT ?)", (owner, owner, MAX_REPORTS))
        _conn.commit()
    return rid


def latest_report(owner: str) -> Optional[Dict]:
    with _lock:
        r = _db().execute("SELECT id, week, created_at, data, seen_at FROM reports WHERE owner = ? ORDER BY created_at DESC LIMIT 1",
                          (owner,)).fetchone()
    return {"id": r[0], "week": r[1], "created_at": r[2], "data": json.loads(r[3]), "seen": r[4] is not None} if r else None


def mark_report_seen(owner: str, report_id: str) -> bool:
    with _lock:
        cur = _db().execute("UPDATE reports SET seen_at = ? WHERE owner = ? AND id = ? AND seen_at IS NULL",
                            (time.time(), owner, report_id))
        _conn.commit()
        return cur.rowcount > 0


def delete_report(owner: str, report_id: str) -> bool:
    with _lock:
        cur = _db().execute("DELETE FROM reports WHERE owner = ? AND id = ?", (owner, report_id))
        _conn.commit()
        return cur.rowcount > 0


def pref_get(owner: str, key: str, default: str = None) -> Optional[str]:
    with _lock:
        r = _db().execute("SELECT value FROM prefs WHERE owner = ? AND key = ?", (owner, key)).fetchone()
    return r[0] if r else default


def pref_set(owner: str, key: str, value: str) -> None:
    with _lock:
        _db().execute("INSERT OR REPLACE INTO prefs (owner, key, value) VALUES (?,?,?)", (owner, key, str(value)))
        _conn.commit()
