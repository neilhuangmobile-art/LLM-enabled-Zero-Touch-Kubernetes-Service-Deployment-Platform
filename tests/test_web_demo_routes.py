"""
測試 web_demo.py 的 Flask 路由層——這是這個專案唯一的入口，卻是目前測試覆蓋率最薄弱
的地方（其他測試都集中在 agents/guardian/healer 這些純邏輯層）。用 app.test_client()
直接打路由，不需要真的啟動 HTTP server，也不需要真的連 K8s。

最重要的一組測試是「未登入打受保護的路由要回 401」——這個專案這次 session 之前就真的
抓到過 3 個路由漏了登入檢查的安全漏洞（見 docs/security_review.md 1.1 節），這裡把它
變成自動化回歸測試，之後任何人加新路由忘記寫登入檢查，測試會直接抓到。

標記 integration：import web_demo.py 會連帶 import llama_client.py，llama_client.py
會 import torch，CI 沒裝這些重依賴跑不起來。

避免碰到真正的 users.json：用 monkeypatch 把 web_demo.__file__ 指到 tmp_path 底下的
假路徑，register/login 的寫檔動作就會落在暫存目錄，不會動到專案裡真正的使用者資料。
"""
import sys
import os
import json

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = pytest.mark.integration

# collection 階段就會 import，@pytest.mark.integration 擋不住 CI 沒裝 flask/torch
# 時直接炸掉整個 pytest run（見 test_model_server_injection.py 的說明，實測驗證過）。
web_demo = pytest.importorskip("web_demo")


@pytest.fixture
def client(tmp_path, monkeypatch):
    """乾淨的 Flask test client：K8s 關閉、USERS 清空、寫檔導向暫存目錄。"""
    monkeypatch.setattr(web_demo, "K8S_ENABLED", False)
    monkeypatch.setattr(web_demo, "USERS", {})
    monkeypatch.setattr(web_demo, "__file__", str(tmp_path / "web_demo.py"))
    web_demo.app.config["TESTING"] = True
    with web_demo.app.test_client() as c:
        yield c


def _register(client, username, password, confirm=None):
    return client.post("/auth/register", data={
        "username": username, "password": password, "confirm": confirm or password,
    })


def _login(client, username, password):
    return client.post("/auth/login", data={"username": username, "password": password})


class TestProtectedRoutesRequireAuth:
    """回歸測試：這個專案之前真的抓到 3 個路由漏了登入檢查（security_review.md 1.1 節），
    這裡挑一組有代表性的路由（GET 查詢 / POST 破壞性操作 / 較新的 healer 路由）確保
    沒有登入一律 401，不能因為之後加新路由又漏寫檢查。"""

    @pytest.mark.parametrize("path,method", [
        ("/api/status", "GET"),
        ("/api/pods", "GET"),
        ("/api/deployments", "GET"),
        ("/api/healer/scan", "GET"),
        ("/api/healer/status", "GET"),
        ("/api/dataset/stats", "GET"),
        ("/api/metrics", "GET"),
        ("/api/gitops", "GET"),
    ])
    def test_get_routes_reject_anonymous(self, client, path, method):
        resp = client.get(path)
        assert resp.status_code == 401, f"{path} should require login"

    @pytest.mark.parametrize("path", [
        "/api/scale", "/api/update", "/api/rollback", "/api/delete",
        "/api/deploy", "/api/deploy/parse", "/api/chat",
        "/api/healer/fix", "/api/healer/auto_fix", "/api/dataset/run",
        "/api/intent",
    ])
    def test_post_routes_reject_anonymous(self, client, path):
        resp = client.post(path, json={})
        assert resp.status_code == 401, f"{path} should require login"


class TestAuthFlow:
    def test_register_then_status_route_works(self, client):
        resp = _register(client, "alice", "hunter22")
        assert resp.status_code == 302  # redirect to /
        status = client.get("/api/status")
        assert status.status_code == 200

    def test_register_duplicate_username_rejected(self, client):
        _register(client, "bob", "hunter22")
        with client.session_transaction() as sess:
            sess.clear()  # 模擬換一個訪客再註冊同名帳號
        resp = _register(client, "bob", "differentpw")
        assert resp.status_code == 200  # 停在註冊頁，不是 302 redirect
        assert b"already taken" in resp.data

    def test_register_short_password_rejected(self, client):
        resp = _register(client, "carol", "123")
        assert resp.status_code == 200
        assert b"at least 6 characters" in resp.data

    def test_register_password_mismatch_rejected(self, client):
        resp = _register(client, "dave", "hunter22", confirm="different")
        assert resp.status_code == 200
        assert b"do not match" in resp.data

    def test_login_wrong_password_rejected(self, client):
        _register(client, "erin", "hunter22")
        with client.session_transaction() as sess:
            sess.clear()
        resp = _login(client, "erin", "wrongpassword")
        assert resp.status_code == 200
        assert b"Invalid username or password" in resp.data

    def test_login_persists_session_across_requests(self, client):
        _register(client, "frank", "hunter22")
        with client.session_transaction() as sess:
            sess.clear()
        _login(client, "frank", "hunter22")
        resp = client.get("/api/status")
        assert resp.status_code == 200

    def test_legacy_plaintext_password_upgrades_to_pbkdf2_on_login(self, client):
        # 對應 security_review.md 1.2 節修好的那個 bug：舊格式（純字串、無鹽 sha256，
        # 不是 dict/pbkdf2）登入成功那一刻要自動升級成加鹽雜湊，不用等使用者自己改密碼。
        import hashlib
        legacy_hash = hashlib.sha256(b"plainoldpassword").hexdigest()
        web_demo.USERS["legacy_user"] = legacy_hash  # 模擬最舊的儲存格式（見 verify_password）
        resp = _login(client, "legacy_user", "plainoldpassword")
        assert resp.status_code == 302
        upgraded = web_demo.USERS["legacy_user"]
        assert isinstance(upgraded, dict)
        assert upgraded["password_hash"].startswith("pbkdf2_sha256$")


class TestDeployValidation:
    def test_deploy_parse_rejects_too_short_input(self, client):
        _register(client, "grace", "hunter22")
        resp = client.post("/api/deploy/parse", json={"input": "hi"})
        assert resp.status_code == 400

    def test_deploy_rejects_empty_input(self, client):
        _register(client, "heidi", "hunter22")
        resp = client.post("/api/deploy", json={"input": ""})
        assert resp.status_code == 400


class TestScaleValidation:
    """/api/scale 在真的碰 K8s API 之前，會先做完整的輸入驗證——這些測試把
    K8S_ENABLED 開成 True（不需要真的連線，驗證錯誤在碰到 K8s client 之前就回應了）。"""

    def test_scale_returns_503_when_k8s_disabled(self, client):
        _register(client, "ivan", "hunter22")
        resp = client.post("/api/scale", json={"name": "web", "replicas": 3})
        assert resp.status_code == 503

    def test_scale_missing_name_returns_400(self, client, monkeypatch):
        _register(client, "judy", "hunter22")
        monkeypatch.setattr(web_demo, "K8S_ENABLED", True)
        resp = client.post("/api/scale", json={"replicas": 3})
        assert resp.status_code == 400

    def test_scale_replicas_out_of_range_returns_400(self, client, monkeypatch):
        _register(client, "kevin", "hunter22")
        monkeypatch.setattr(web_demo, "K8S_ENABLED", True)
        resp = client.post("/api/scale", json={"name": "web", "replicas": 999})
        assert resp.status_code == 400

    def test_scale_non_integer_replicas_returns_400(self, client, monkeypatch):
        _register(client, "laura", "hunter22")
        monkeypatch.setattr(web_demo, "K8S_ENABLED", True)
        resp = client.post("/api/scale", json={"name": "web", "replicas": "not-a-number"})
        assert resp.status_code == 400


class TestRuleIntentDeployVsDescribeDeployment:
    """2026-10-03 使用者錄 demo 時實測發現的真實 bug：Chat 打「幫我部署 2 個
    nginx，開 80 port」被誤判成查詢一個叫「2」的 deployment（回「找不到
    deployment '2'」），使用者看不懂為什麼。根因是 `_INTENT_RULES` 裡
    describe_deployment 規則結尾的「狀態/細節/詳情/status」關鍵字整段可省略
    （`(?:...)?`），導致任何「部署 <數字>」開頭的句子都會被這條規則搶先攔截，
    排在真正的 deploy 規則前面。"""

    def test_deploy_sentence_with_count_is_not_misrouted_to_describe_deployment(self):
        result = web_demo._rule_intent("幫我部署 2 個 nginx，開 80 port")
        assert result is not None
        assert result["action"] == "deploy"

    def test_deploy_sentence_in_english_is_not_misrouted(self):
        result = web_demo._rule_intent("deploy 3 nginx pods for web-frontend")
        assert result is not None
        assert result["action"] != "describe_deployment"

    def test_genuine_describe_deployment_query_still_works(self):
        """對照組：真的在問某個 deployment 狀態的句子，行為不能被連帶改壞。"""
        result = web_demo._rule_intent("deployment web-frontend 的狀態")
        assert result is not None
        assert result["action"] == "describe_deployment"
        assert result["args"]["name"] == "web-frontend"

    def test_genuine_describe_deployment_query_english_status_keyword(self):
        result = web_demo._rule_intent("deployment web-frontend status")
        assert result is not None
        assert result["action"] == "describe_deployment"
        assert result["args"]["name"] == "web-frontend"


class TestRuleIntentImplausibleNameStopwords:
    """2026-10-07 大規模掃描（206 題）實測發現的真實 P0：規則層命中就用假 100%
    信心直接執行，完全不經過 classify_intent() 的事實查核。`_looks_like_resource_name()`
    原本只檢查字串格式合不合法（純 ASCII 字母數字），常見英文虛詞剛好都符合格式，
    會被當成真的資源名稱：「delete everything in this cluster」被解析成要刪除
    一個叫 "everything" 的資源、「remove the deployment named test-app」名稱被
    抓成 "the"（真正目標 test-app 完全沒抓到）、「update the image of
    cache-server to redis:7」名稱 "the"、映像 "of" 兩個欄位都是虛詞。"""

    def test_delete_everything_does_not_match_rule_layer(self):
        """規則層必須放棄這句話（回 None），讓它 fall through 到有事實查核能力
        的 classify_intent()，不能用假信心直接執行。"""
        result = web_demo._rule_intent("delete everything in this cluster")
        assert result is None

    def test_remove_the_deployment_named_x_does_not_match_rule_layer(self):
        result = web_demo._rule_intent("remove the deployment named test-app")
        assert result is None

    def test_update_image_with_filler_words_does_not_match_rule_layer(self):
        result = web_demo._rule_intent("update the image of cache-server to redis:7")
        assert result is None

    def test_chinese_garbage_image_value_does_not_match_rule_layer(self):
        result = web_demo._rule_intent("把 billing-api 的映像檔換新版")
        assert result is None

    def test_genuine_delete_request_still_works(self):
        """對照組：乾淨的刪除請求行為不能被連帶改壞。"""
        result = web_demo._rule_intent("delete web-frontend")
        assert result is not None
        assert result["action"] == "delete"
        assert result["args"]["name"] == "web-frontend"

    def test_genuine_update_image_request_still_works(self):
        result = web_demo._rule_intent("幫我把 worker 的 image 換成 python:3.12")
        assert result is not None
        assert result["action"] == "update_image"
        assert result["args"]["name"] == "worker"
        assert result["args"]["image"] == "python:3.12"


class TestRuleIntentQuestionFormNotMisroutedToAction:
    """2026-10-07 大規模掃描實測發現：gitops_log/healer_scan 這類只看開頭關鍵字、
    不要求任何參數的規則，會把「GitOps 是什麼」這種明顯在問概念的句子，直接當成
    指令執行（真的去撈 git log／真的去掃描 pod），答非所問。"""

    def test_gitops_concept_question_not_misrouted(self):
        result = web_demo._rule_intent("GitOps 是什麼")
        assert result is None

    def test_healer_concept_question_not_misrouted(self):
        result = web_demo._rule_intent("Healer 這個功能是做什麼用的")
        assert result is None

    def test_english_what_is_question_not_misrouted(self):
        result = web_demo._rule_intent("what is gitops")
        assert result is None

    def test_genuine_gitops_command_still_works(self):
        """對照組：真的在下指令的句子行為不能被連帶改壞。"""
        result = web_demo._rule_intent("看一下部署歷史")
        assert result is not None
        assert result["action"] == "gitops_log"

    def test_genuine_healer_scan_command_still_works(self):
        result = web_demo._rule_intent("掃描壞掉的 pod")
        assert result is not None
        assert result["action"] == "healer_scan"
