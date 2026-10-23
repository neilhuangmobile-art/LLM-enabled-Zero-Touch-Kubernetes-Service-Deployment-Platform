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
        # 2026-10-23：部署預設值（JSON）。舊的資料庫沒有這一欄，補上
        try:
            _conn.execute("ALTER TABLE projects ADD COLUMN defaults TEXT NOT NULL DEFAULT '{}'")
        except sqlite3.OperationalError:
            pass
        _conn.execute("""CREATE TABLE IF NOT EXISTS project_services (
            namespace TEXT NOT NULL, app TEXT NOT NULL, owner TEXT NOT NULL, project_id TEXT NOT NULL,
            PRIMARY KEY (namespace, app))""")
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
                             "ORDER BY updated_at", (owner,)).fetchall()
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
            n = _conn.execute("SELECT COUNT(*) FROM chats WHERE owner = ?", (owner,)).fetchone()[0]
            if n >= MAX_CHATS:
                raise WorkspaceError(f"對話已經有 {MAX_CHATS} 則，請先刪掉一些舊對話。")
        _conn.execute("INSERT OR REPLACE INTO chats (owner, id, project_id, title, messages, updated_at) "
                      "VALUES (?,?,?,?,?,?)",
                      (owner, chat_id, project_id or None, _clean_text(title, MAX_TITLE) or "新對話", body, time.time()))
        _conn.commit()


def delete_chat(owner: str, chat_id: str) -> bool:
    with _lock:
        cur = _db().execute("DELETE FROM chats WHERE owner = ? AND id = ?", (owner, chat_id))
        _conn.commit()
        return cur.rowcount > 0


def chats_of(owner: str, project_id: str) -> List[Dict]:
    """專案裡的對話（只回標題與時間，不回內容）。"""
    with _lock:
        rows = _db().execute("SELECT id, title, updated_at FROM chats WHERE owner = ? AND project_id = ? "
                             "ORDER BY updated_at DESC", (owner, project_id)).fetchall()
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
                             "WHERE owner = ? ORDER BY created_at", (owner,)).fetchall()
    return [_project_row(r) for r in rows]


def get_project(owner: str, project_id: str) -> Optional[Dict]:
    if not PROJECT_ID_RE.match(str(project_id or "")):
        return None
    with _lock:
        r = _db().execute("SELECT id, name, description, instructions, created_at, updated_at, defaults FROM projects "
                          "WHERE owner = ? AND id = ?", (owner, project_id)).fetchone()
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
        n = _db().execute("SELECT COUNT(*) FROM projects WHERE owner = ?", (owner,)).fetchone()[0]
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
    """只刪專案本身；呼叫前要先處理好裡面的對話、服務、檔案（見 web_demo 的刪除專案路由）。
    保險起見，還留著的對話變成一般對話、服務歸屬與檔案清掉。"""
    with _lock:
        _db().execute("UPDATE chats SET project_id = NULL WHERE owner = ? AND project_id = ?", (owner, project_id))
        _conn.execute("DELETE FROM project_services WHERE owner = ? AND project_id = ?", (owner, project_id))
        _conn.execute("DELETE FROM project_files WHERE owner = ? AND project_id = ?", (owner, project_id))
        _conn.execute("DELETE FROM projects WHERE owner = ? AND id = ?", (owner, project_id))
        _conn.commit()


def move_chats_out(owner: str, project_id: str) -> int:
    with _lock:
        cur = _db().execute("UPDATE chats SET project_id = NULL WHERE owner = ? AND project_id = ?", (owner, project_id))
        _conn.commit()
        return cur.rowcount


def delete_chats_of(owner: str, project_id: str) -> int:
    with _lock:
        cur = _db().execute("DELETE FROM chats WHERE owner = ? AND project_id = ?", (owner, project_id))
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
