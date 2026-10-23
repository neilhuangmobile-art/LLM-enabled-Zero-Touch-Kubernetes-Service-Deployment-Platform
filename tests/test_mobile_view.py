"""手機監控版 /m：手機導向、不走同意書、頁面沒有寫入功能、唯讀 session 由伺服器擋下寫入 API。"""
import json
import re

import pytest

web_demo = pytest.importorskip("web_demo")

IPHONE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
          "(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1")
ANDROID = ("Mozilla/5.0 (Linux; Android 15; Pixel 9) AppleWebKit/537.36 (KHTML, like Gecko) "
           "Chrome/140.0 Mobile Safari/537.36")
DESKTOP = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"
IPAD = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15"


@pytest.fixture
def client():
    return web_demo.app.test_client()


def _login(c, user="alice", readonly=False):
    with c.session_transaction() as s:
        s["username"] = user
        s["readonly"] = readonly


@pytest.mark.parametrize("ua", [IPHONE, ANDROID], ids=["iphone", "android"])
def test_phone_goes_to_monitor_view(client, ua):
    r = client.get("/?page=healer&pod=orders-api", headers={"User-Agent": ua})
    assert r.status_code == 302 and r.headers["Location"].endswith("/m?pod=orders-api")
    # 帶奇怪字元的 pod 參數不會被帶進網址
    r = client.get("/?pod=x%22%3E%3Cscript%3E", headers={"User-Agent": ua})
    assert r.headers["Location"].endswith("/m")


@pytest.mark.parametrize("ua", [DESKTOP, IPAD], ids=["desktop", "ipad"])
def test_desktop_and_tablet_keep_full_site(client, ua):
    assert client.get("/", headers={"User-Agent": ua}).status_code == 200


def test_phone_login_page_has_monitor_mode(client):
    html = client.get("/m", headers={"User-Agent": IPHONE}).get_data(as_text=True)
    assert 'name="mode" value="monitor"' in html


def test_monitor_page_has_no_write_actions(client):
    _login(client, readonly=True)
    html = client.get("/m", headers={"User-Agent": IPHONE}).get_data(as_text=True)
    for bad in ("/api/deploy'", "/api/deploy\"", "/api/deploy/", "/api/scale", "/api/delete", "/api/update",
                "/api/rollback", "/api/healer/fix", "/api/healer/auto", "/api/chat", "/api/intent",
                "/api/import", "diagnose=1"):
        assert bad not in html, bad
    for tab in ("overview", "services", "charts", "history", "settings"):
        assert f'data-tab="{tab}"' in html


def test_phone_session_becomes_readonly(client):
    _login(client, readonly=False)
    client.get("/m", headers={"User-Agent": IPHONE})
    with client.session_transaction() as s:
        assert s["readonly"] is True


def test_phone_password_login_is_readonly(client, monkeypatch):
    monkeypatch.setitem(web_demo.USERS, "zz-mobile", {"password_hash": web_demo.hash_password("pw-123456")})
    monkeypatch.setattr(web_demo, "_ensure_user_namespace", lambda ns: None)
    r = client.post("/auth/login", data={"username": "zz-mobile", "password": "pw-123456", "mode": "monitor"})
    assert r.headers["Location"].endswith("/m")
    with client.session_transaction() as s:
        assert s["readonly"] is True
    # 手機就算不是從監控版登入頁登入，也一樣是唯讀
    c2 = web_demo.app.test_client()
    c2.post("/auth/login", data={"username": "zz-mobile", "password": "pw-123456"}, headers={"User-Agent": ANDROID})
    with c2.session_transaction() as s:
        assert s["readonly"] is True
    # 電腦登入不受影響
    c3 = web_demo.app.test_client()
    r = c3.post("/auth/login", data={"username": "zz-mobile", "password": "pw-123456"}, headers={"User-Agent": DESKTOP})
    assert r.headers["Location"].endswith("/")
    with c3.session_transaction() as s:
        assert s["readonly"] is False


@pytest.mark.parametrize("method,path", [
    ("POST", "/api/deploy"), ("POST", "/api/deploy/parse"), ("POST", "/api/scale"), ("POST", "/api/delete"),
    ("POST", "/api/update"), ("POST", "/api/rollback"), ("POST", "/api/healer/fix"), ("POST", "/api/healer/auto-fix"),
    ("POST", "/api/chat"), ("POST", "/api/intent"), ("POST", "/api/import/parse"), ("GET", "/api/report"),
    ("GET", "/api/pods/web?diagnose=1"), ("GET", "/api/some-future-endpoint"), ("GET", "/api/gitops"),
], ids=lambda v: re.sub(r"\W+", "_", v))
def test_readonly_session_blocks_writes(client, method, path):
    """用白名單：寫入 API、會觸發模型的診斷、以及之後新增但沒列進白名單的 API，全部擋下。"""
    _login(client, readonly=True)
    r = client.open(path, method=method, data=json.dumps({}), content_type="application/json",
                    headers={"User-Agent": DESKTOP})   # 改 User-Agent 也繞不過
    assert r.status_code == 403 and r.get_json().get("readonly") is True


@pytest.mark.parametrize("path", ["/api/deployments", "/api/pods", "/api/healer/status", "/api/status", "/api/dashboard",
                                  "/api/pods/web", "/api/pods/web-1-2/history", "/api/push/status"])
def test_readonly_session_can_read(client, monkeypatch, path):
    monkeypatch.setattr(web_demo, "_session_ns", lambda: "user-alice")
    _login(client, readonly=True)
    r = client.get(path)
    assert r.status_code != 403, (path, r.get_data(as_text=True)[:200])


def test_readonly_session_cannot_open_full_site(client):
    _login(client, readonly=True)
    for p in ("/", "/onboarding", "/auth/register"):
        r = client.get(p, headers={"User-Agent": DESKTOP})
        assert r.status_code == 302 and "/m" in r.headers["Location"], p


def test_full_session_not_affected(client, monkeypatch):
    _login(client, readonly=False)
    r = client.post("/api/chat", data=json.dumps({"message": ""}), content_type="application/json",
                    headers={"User-Agent": DESKTOP})
    assert r.status_code == 400   # 進到路由本身（空訊息），沒有被唯讀檢查擋下


def test_phone_skips_consent(client, monkeypatch):
    """客戶模式下還沒連線的帳號：手機不跳同意書，/m 顯示「請先用電腦完成連線」。"""
    monkeypatch.setattr(web_demo, "_needs_onboarding", lambda u: True)
    _login(client, readonly=True)
    r = client.get("/m", headers={"User-Agent": IPHONE})
    assert r.status_code == 200
    assert "NEEDS_SETUP = true" in r.get_data(as_text=True)
    r = client.get("/", headers={"User-Agent": IPHONE})
    assert "/onboarding" not in r.headers.get("Location", "")


def test_manifest_starts_at_monitor_view(client):
    assert json.loads(client.get("/manifest.webmanifest").get_data())["start_url"] == "/m"
