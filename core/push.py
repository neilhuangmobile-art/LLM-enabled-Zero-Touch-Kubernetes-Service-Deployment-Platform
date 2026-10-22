"""
手機推播通知（2026-10-22）：Healer 發現問題或修好時，推到使用者訂閱過的裝置（PWA / 瀏覽器）。

設計：
- 訂閱存在 data/push.db（sqlite3，data/ 已經在 .gitignore）。每筆訂閱帶 namespace，
  送通知一律用 namespace 查，沿用「一個帳號一個 namespace」隔離，A 帳號的事件不會推給 B。
- 伺服器要主動連到訂閱裡的 endpoint 網址，所以 endpoint 只接受各家瀏覽器推播服務的 https 網域，
  不能讓人填任意網址叫伺服器去連（SSRF）。
- 通知內容只有服務名稱和白話原因，不放 log 原文和環境變數：通知會顯示在鎖定畫面上。
- VAPID 金鑰第一次用到時產生，存在 data/vapid_private.pem，不進 Git、不進 .env。
- pywebpush 用到才 import：CI 沒裝它，這個檔案的純邏輯部分照樣能測。
- 送通知失敗只印 log，不丟例外：推播是附加功能，不能讓 Healer 因此中斷。
"""

import base64
import json
import os
import re
import sqlite3
import threading
import time
from typing import Callable, Dict, List, Optional
from urllib.parse import urlsplit

MAX_DEVICES_PER_USER = 5
MAX_TEXT = 120

# 各家瀏覽器的推播服務：Chrome/Android（FCM）、Firefox、Safari/iPhone、Edge（Windows）
_PUSH_HOST_SUFFIXES = (
    "fcm.googleapis.com",
    "push.services.mozilla.com",
    "push.apple.com",
    "notify.windows.com",
)
_B64URL_RE = re.compile(r"^[A-Za-z0-9_\-]+=*$")
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]+")

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None
_path: Optional[str] = None


def _data_dir() -> str:
    from core.config import DATA_DIR
    return DATA_DIR


def init(path: str = None) -> None:
    """開啟（或建立）訂閱資料庫。測試可以傳暫存路徑；重複呼叫會換到新的路徑。"""
    global _conn, _path
    path = path or os.environ.get("ZT_PUSH_DB") or os.path.join(_data_dir(), "push.db")
    with _lock:
        if _conn is not None and _path == path:
            return
        if _conn is not None:
            _conn.close()
        if path != ":memory:":
            os.makedirs(os.path.dirname(path), exist_ok=True)
        _conn = sqlite3.connect(path, check_same_thread=False)
        _conn.execute("""CREATE TABLE IF NOT EXISTS subscriptions (
            endpoint TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            namespace TEXT NOT NULL,
            p256dh TEXT NOT NULL,
            auth TEXT NOT NULL,
            created_at REAL NOT NULL,
            last_ok_at REAL,
            fail_count INTEGER DEFAULT 0,
            user_agent TEXT
        )""")
        _conn.execute("CREATE INDEX IF NOT EXISTS idx_push_ns ON subscriptions(namespace)")
        _conn.commit()
        _path = path


def _db() -> sqlite3.Connection:
    if _conn is None:
        init()
    return _conn


# ── 訂閱驗證 ──────────────────────────────────────────────────

def validate_subscription(sub) -> Optional[str]:
    """回傳錯誤訊息（給使用者看的白話），沒問題回 None。"""
    if not isinstance(sub, dict):
        return "訂閱資料格式不對，請重新開啟通知。"
    endpoint = sub.get("endpoint")
    keys = sub.get("keys") or {}
    if not isinstance(endpoint, str) or len(endpoint) > 1000:
        return "訂閱資料格式不對，請重新開啟通知。"
    try:
        u = urlsplit(endpoint)
        port = u.port
    except ValueError:
        return "訂閱資料格式不對，請重新開啟通知。"
    host = (u.hostname or "").lower()
    if u.scheme != "https" or port not in (None, 443) or u.username or u.password:
        return "這個瀏覽器的推播網址不是 https，平台不會送通知過去。"
    if not any(host == s or host.endswith("." + s) for s in _PUSH_HOST_SUFFIXES):
        return "這個瀏覽器使用的推播服務平台不認得，目前支援 Chrome、Edge、Firefox 與 Safari（iPhone）。"
    p256dh, auth = keys.get("p256dh"), keys.get("auth")
    for v, lo, hi in ((p256dh, 80, 100), (auth, 16, 30)):
        if not isinstance(v, str) or not (lo <= len(v) <= hi) or not _B64URL_RE.match(v):
            return "訂閱資料格式不對，請重新開啟通知。"
    return None


def add(username: str, namespace: str, sub: dict, user_agent: str = "") -> Optional[str]:
    """新增或更新一台裝置。回傳錯誤訊息，成功回 None。"""
    err = validate_subscription(sub)
    if err:
        return err
    endpoint = sub["endpoint"]
    db = _db()
    with _lock:
        row = db.execute("SELECT username FROM subscriptions WHERE endpoint=?", (endpoint,)).fetchone()
        if row is None:
            n = db.execute("SELECT COUNT(*) FROM subscriptions WHERE username=?", (username,)).fetchone()[0]
            if n >= MAX_DEVICES_PER_USER:
                return (f"這個帳號已經有 {MAX_DEVICES_PER_USER} 台裝置開啟通知了，"
                        "請先在不用的裝置上按「關閉通知」再試。")
        # 同一個 endpoint 換了帳號登入：歸給現在這個帳號（舊帳號不該再收到這台裝置的通知）
        db.execute("""INSERT INTO subscriptions (endpoint, username, namespace, p256dh, auth, created_at, user_agent)
                      VALUES (?,?,?,?,?,?,?)
                      ON CONFLICT(endpoint) DO UPDATE SET username=excluded.username, namespace=excluded.namespace,
                      p256dh=excluded.p256dh, auth=excluded.auth, fail_count=0, user_agent=excluded.user_agent""",
                   (endpoint, username, namespace, sub["keys"]["p256dh"], sub["keys"]["auth"],
                    time.time(), (user_agent or "")[:200]))
        db.commit()
    return None


def remove(username: str, endpoint: str) -> bool:
    db = _db()
    with _lock:
        cur = db.execute("DELETE FROM subscriptions WHERE endpoint=? AND username=?", (endpoint, username))
        db.commit()
    return cur.rowcount > 0


def has(username: str, endpoint: str) -> bool:
    row = _db().execute("SELECT 1 FROM subscriptions WHERE endpoint=? AND username=?",
                        (endpoint, username)).fetchone()
    return row is not None


def for_namespace(namespace: str) -> List[Dict]:
    if not namespace:
        return []
    rows = _db().execute("SELECT endpoint, p256dh, auth FROM subscriptions WHERE namespace=?",
                         (namespace,)).fetchall()
    return [{"endpoint": e, "keys": {"p256dh": p, "auth": a}} for e, p, a in rows]


def count_for_user(username: str) -> int:
    return _db().execute("SELECT COUNT(*) FROM subscriptions WHERE username=?", (username,)).fetchone()[0]


def _mark_ok(endpoint: str):
    db = _db()
    with _lock:
        db.execute("UPDATE subscriptions SET last_ok_at=?, fail_count=0 WHERE endpoint=?", (time.time(), endpoint))
        db.commit()


def _mark_fail(endpoint: str, gone: bool):
    db = _db()
    with _lock:
        if gone:
            # 404/410：使用者解除安裝或關掉通知了，訂閱已經失效
            db.execute("DELETE FROM subscriptions WHERE endpoint=?", (endpoint,))
        else:
            db.execute("UPDATE subscriptions SET fail_count=fail_count+1 WHERE endpoint=?", (endpoint,))
            db.execute("DELETE FROM subscriptions WHERE endpoint=? AND fail_count>=20", (endpoint,))
        db.commit()


# ── 通知內容 ──────────────────────────────────────────────────

def clean_text(s, limit: int = MAX_TEXT) -> str:
    s = _CTRL_RE.sub(" ", str(s or "")).strip()
    return s if len(s) <= limit else s[: limit - 1] + "…"


def heal_message(app: str, outcome: str, root_cause: str, action_label: str, pod: str) -> Dict:
    """Healer 紀錄 → 通知。只用服務名稱、原因、處理方式這幾個欄位，不碰 log 和 env。"""
    app = clean_text(app, 60) or "你的服務"
    cause = clean_text(root_cause, 60) or "原因還在確認"
    if outcome == "ok":
        # 「已處理」不是「已修好」：處理動作成功了，但服務是否恢復要看之後的狀況，不能說得太滿
        title, body = f"🔧 {app} 已自動處理", f"{cause}；已{clean_text(action_label, 30)}。如果還是不正常，Healer 會繼續處理並通知你。"
    elif outcome == "fail":
        title, body = f"❌ {app} 自動修復失敗", f"{cause}。打開看看建議的處理方式。"
    else:
        title, body = f"⚠️ {app} 需要你處理", f"{cause}。Healer 沒辦法自動修，打開看看怎麼處理。"
    pod = re.sub(r"[^a-z0-9\-.]", "", str(pod or "").lower())[:253]
    url = "/?page=healer" + (f"&pod={pod}" if pod else "")
    return {"title": clean_text(title), "body": clean_text(body), "url": url, "tag": f"heal-{app}"}


# ── VAPID 金鑰與送出 ──────────────────────────────────────────

def _vapid_path() -> str:
    return os.environ.get("ZT_VAPID_KEY") or os.path.join(_data_dir(), "vapid_private.pem")


def _vapid():
    from py_vapid import Vapid
    path = _vapid_path()
    with _lock:
        if not os.path.exists(path):
            v = Vapid()
            v.generate_keys()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            v.save_key(path)
    return Vapid.from_file(path)


def public_key() -> Optional[str]:
    """前端 pushManager.subscribe 要的 applicationServerKey（base64url，未壓縮 EC 點）。"""
    try:
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
        raw = _vapid().public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    except Exception as e:
        print(f"[推播] 無法取得 VAPID 公鑰：{e}")
        return None


def available() -> bool:
    try:
        import pywebpush  # noqa: F401
        return public_key() is not None
    except Exception:
        return False


def _claims_sub() -> str:
    # VAPID 規定要附聯絡方式（mailto: 或 https 網址）。用平台網址，不把個人信箱送給推播服務。
    base = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
    return base if base.startswith("https://") else "https://github.com/neilhuangmobile-art"


def _default_sender(sub: dict, data: str):
    from pywebpush import webpush
    webpush(subscription_info=sub, data=data, vapid_private_key=_vapid_path(),
            vapid_claims={"sub": _claims_sub()}, ttl=3600, timeout=10)


def send_one(sub: dict, message: Dict, sender: Callable = None) -> str:
    """送給一台裝置。回傳 "sent"、"removed"（訂閱已失效，已刪掉）或 "failed"。"""
    sender = sender or _default_sender
    if sender is _default_sender and not os.path.exists(_vapid_path()):
        _vapid()
    try:
        sender(sub, json.dumps(message, ensure_ascii=False))
        _mark_ok(sub["endpoint"])
        return "sent"
    except Exception as e:
        status = getattr(getattr(e, "response", None), "status_code", None)
        gone = status in (404, 410)
        _mark_fail(sub["endpoint"], gone)
        if not gone:
            print(f"[推播] 送出失敗（{status or type(e).__name__}），不影響 Healer：{str(e)[:200]}")
        return "removed" if gone else "failed"


def subscription(username: str, endpoint: str) -> Optional[Dict]:
    """這個帳號自己的某一台裝置（測試通知只送這一台）。"""
    row = _db().execute("SELECT endpoint, p256dh, auth FROM subscriptions WHERE endpoint=? AND username=?",
                        (endpoint, username)).fetchone()
    return {"endpoint": row[0], "keys": {"p256dh": row[1], "auth": row[2]}} if row else None


def send_to_namespace(namespace: str, message: Dict, sender: Callable = None) -> Dict:
    """送給這個 namespace 底下的所有裝置。回傳 {sent, failed, removed}。"""
    result = {"sent": 0, "failed": 0, "removed": 0}
    for sub in for_namespace(namespace):
        result[send_one(sub, message, sender)] += 1
    return result


def send_async(namespace: str, message: Dict) -> None:
    """背景送出，呼叫端（Healer）不用等推播服務回應。"""
    if not for_namespace(namespace):
        return
    threading.Thread(target=send_to_namespace, args=(namespace, message), daemon=True).start()
