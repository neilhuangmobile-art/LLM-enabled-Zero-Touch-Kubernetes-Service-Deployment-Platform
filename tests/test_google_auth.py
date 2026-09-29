"""
測試 2026-09-16 新增的 Google 登入功能：`_derive_username_from_google()`、
`/auth/google/login`、`/auth/google/callback`，以及既有密碼登入路徑在遇到
純 Google 帳號時的行為。

安全考量是這份測試的重點（見 docs/security_review.md）：
  - CSRF state 核對（state 不符必須拒絕，不能被拿來偽造登入）
  - email 未驗證必須拒絕（不驗證等於任何人都能宣稱擁有別人的 email）
  - 衍生的 username 撞到既有帳號（不管密碼帳號還是別的 Google 帳號）時絕對不能
    覆蓋/合併，一律改用別的 username——這是防止帳號被冒用的關鍵設計

monkeypatch 掉真正打 Google API 的 `requests.post`/`requests.get`，不會真的連線
出去。標記 integration：import web_demo.py 會連帶 import llama_client.py，
llama_client.py 會 import torch，CI 沒裝這些重依賴跑不起來。
"""
import sys
import os

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = pytest.mark.integration

web_demo = pytest.importorskip("web_demo")


class _FakeResponse:
    def __init__(self, json_data, status_code=200):
        self._json = json_data
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")

    def json(self):
        return self._json


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(web_demo, "K8S_ENABLED", False)
    monkeypatch.setattr(web_demo, "USERS", {})
    monkeypatch.setattr(web_demo, "__file__", str(tmp_path / "web_demo.py"))
    monkeypatch.setattr(web_demo, "GOOGLE_CLIENT_ID", "")
    monkeypatch.setattr(web_demo, "GOOGLE_CLIENT_SECRET", "")
    web_demo.app.config["TESTING"] = True
    with web_demo.app.test_client() as c:
        yield c


@pytest.fixture
def google_configured(monkeypatch):
    monkeypatch.setattr(web_demo, "GOOGLE_CLIENT_ID", "test-client-id")
    monkeypatch.setattr(web_demo, "GOOGLE_CLIENT_SECRET", "test-client-secret")


def _mock_google_apis(monkeypatch, *, email="alice@example.com", sub="google-sub-123",
                       email_verified=True, name="Alice"):
    def fake_post(url, data=None, timeout=None, **kw):
        assert url == web_demo.GOOGLE_TOKEN_URL
        return _FakeResponse({"access_token": "fake-access-token"})

    def fake_get(url, headers=None, timeout=None, **kw):
        assert url == web_demo.GOOGLE_USERINFO_URL
        return _FakeResponse({"sub": sub, "email": email, "email_verified": email_verified, "name": name})

    monkeypatch.setattr(web_demo.requests, "post", fake_post)
    monkeypatch.setattr(web_demo.requests, "get", fake_get)


class TestDeriveUsernameFromGoogle:
    def test_simple_email_prefix_when_no_collision(self):
        web_demo.USERS.clear()
        assert web_demo._derive_username_from_google("alice@example.com") == "alice"

    def test_sanitizes_special_characters(self):
        web_demo.USERS.clear()
        assert web_demo._derive_username_from_google("alice.smith+test@example.com") == "alice-smith-test"

    def test_collision_with_existing_password_account_gets_suffixed(self, monkeypatch):
        # 絕對不能因為 email 前綴剛好跟既有密碼帳號的 username 一樣，就把兩者當成
        # 同一個人——這是帳號冒用風險的核心防線。
        monkeypatch.setattr(web_demo, "USERS", {"alice": {"password_hash": "pbkdf2_sha256$x$y"}})
        derived = web_demo._derive_username_from_google("alice@example.com")
        assert derived != "alice"
        assert derived.startswith("alice-")

    def test_collision_with_existing_google_account_also_gets_suffixed(self, monkeypatch):
        monkeypatch.setattr(web_demo, "USERS", {
            "bob": {"auth_provider": "google", "google_sub": "some-other-sub", "email": "bob@other.com"},
        })
        derived = web_demo._derive_username_from_google("bob@example.com")
        assert derived != "bob"

    def test_empty_prefix_falls_back_to_generic_name(self, monkeypatch):
        monkeypatch.setattr(web_demo, "USERS", {})
        assert web_demo._derive_username_from_google("@example.com") == "google-user"


class TestGoogleLoginButtonVisibility:
    def test_button_hidden_when_not_configured(self, client):
        resp = client.get("/")
        assert b"Sign in with Google" not in resp.data

    def test_button_shown_when_configured(self, client, google_configured):
        resp = client.get("/")
        assert b"Sign in with Google" in resp.data

    def test_register_page_also_respects_configuration(self, client, google_configured):
        resp = client.get("/auth/register")
        assert b"Sign in with Google" in resp.data


class TestGoogleLoginRoute:
    def test_shows_config_error_when_not_configured(self, client):
        resp = client.get("/auth/google/login")
        assert resp.status_code == 200  # 停在錯誤頁，不是裸的 404/500
        assert b"not configured" in resp.data or "尚未設定".encode() in resp.data

    def test_redirects_to_google_and_sets_state_when_configured(self, client, google_configured):
        resp = client.get("/auth/google/login")
        assert resp.status_code == 302
        assert resp.headers["Location"].startswith(web_demo.GOOGLE_AUTH_URL)
        assert "client_id=test-client-id" in resp.headers["Location"]
        with client.session_transaction() as sess:
            assert sess.get("google_oauth_state")


class TestGoogleCallbackRoute:
    def test_shows_config_error_when_not_configured(self, client):
        resp = client.get("/auth/google/callback?code=abc&state=xyz")
        assert resp.status_code == 200
        assert b"not configured" in resp.data or "尚未設定".encode() in resp.data

    def test_rejects_state_mismatch(self, client, google_configured):
        with client.session_transaction() as sess:
            sess["google_oauth_state"] = "expected-state"
        resp = client.get("/auth/google/callback?code=abc&state=wrong-state")
        assert b"state mismatch" in resp.data or "state 不符".encode() in resp.data
        with client.session_transaction() as sess:
            assert "username" not in sess

    def test_rejects_missing_code_with_google_error_param(self, client, google_configured):
        with client.session_transaction() as sess:
            sess["google_oauth_state"] = "s1"
        resp = client.get("/auth/google/callback?state=s1&error=access_denied")
        assert b"access_denied" in resp.data

    def test_rejects_unverified_email(self, client, google_configured, monkeypatch):
        with client.session_transaction() as sess:
            sess["google_oauth_state"] = "s1"
        _mock_google_apis(monkeypatch, email_verified=False)
        resp = client.get("/auth/google/callback?code=abc&state=s1")
        assert b"not verified" in resp.data or "尚未驗證".encode() in resp.data
        assert "alice" not in web_demo.USERS

    def test_creates_new_account_on_first_login(self, client, google_configured, monkeypatch):
        with client.session_transaction() as sess:
            sess["google_oauth_state"] = "s1"
        _mock_google_apis(monkeypatch, email="carol@example.com", sub="sub-carol")
        resp = client.get("/auth/google/callback?code=abc&state=s1")
        assert resp.status_code == 302
        assert resp.headers["Location"] == "/"
        assert "carol" in web_demo.USERS
        assert web_demo.USERS["carol"]["auth_provider"] == "google"
        assert web_demo.USERS["carol"]["google_sub"] == "sub-carol"
        with client.session_transaction() as sess:
            assert sess["username"] == "carol"

    def test_second_login_reuses_existing_account_not_duplicate(self, client, google_configured, monkeypatch):
        web_demo.USERS["dave"] = {
            "auth_provider": "google", "google_sub": "sub-dave",
            "email": "dave@example.com", "created_at": "x", "violation_count": 0, "banned": False,
        }
        with client.session_transaction() as sess:
            sess["google_oauth_state"] = "s1"
        _mock_google_apis(monkeypatch, email="dave@example.com", sub="sub-dave")
        resp = client.get("/auth/google/callback?code=abc&state=s1")
        assert resp.status_code == 302
        assert len(web_demo.USERS) == 1  # 沒有重複建立
        with client.session_transaction() as sess:
            assert sess["username"] == "dave"

    def test_banned_google_account_rejected(self, client, google_configured, monkeypatch):
        web_demo.USERS["erin"] = {
            "auth_provider": "google", "google_sub": "sub-erin",
            "email": "erin@example.com", "created_at": "x", "violation_count": 3, "banned": True,
        }
        with client.session_transaction() as sess:
            sess["google_oauth_state"] = "s1"
        _mock_google_apis(monkeypatch, email="erin@example.com", sub="sub-erin")
        resp = client.get("/auth/google/callback?code=abc&state=s1")
        assert b"banned" in resp.data
        with client.session_transaction() as sess:
            assert "username" not in sess


class TestPasswordLoginOnGoogleOnlyAccount:
    def test_gives_helpful_message_instead_of_invalid_password(self, client):
        web_demo.USERS["frank"] = {
            "auth_provider": "google", "google_sub": "sub-frank",
            "email": "frank@example.com", "created_at": "x", "violation_count": 0, "banned": False,
        }
        resp = client.post("/auth/login", data={"username": "frank", "password": "whatever"})
        assert resp.status_code == 200  # 停在登入頁，不是 302
        assert b"has no password" in resp.data or "沒有密碼".encode() in resp.data
