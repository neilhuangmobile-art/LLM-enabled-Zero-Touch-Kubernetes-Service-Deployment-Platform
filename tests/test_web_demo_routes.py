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
    # 2026-10-09 加了頻率限制：所有測試的來源 IP 都是 127.0.0.1，共用一份 limiter 的話
    # 註冊次數會跨測試累積，第 11 個測試起註冊就被 429 擋掉。每個測試給一份新的。
    from core.web_security import RateLimiter
    monkeypatch.setattr(web_demo, "_rate_limiter", RateLimiter())
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
        assert "已經有人用了".encode() in resp.data

    def test_register_short_password_rejected(self, client):
        resp = _register(client, "carol", "123")
        assert resp.status_code == 200
        assert "至少要 6 個字元".encode() in resp.data

    def test_register_password_mismatch_rejected(self, client):
        resp = _register(client, "dave", "hunter22", confirm="different")
        assert resp.status_code == 200
        assert "密碼不一樣".encode() in resp.data

    def test_login_wrong_password_rejected(self, client):
        _register(client, "erin", "hunter22")
        with client.session_transaction() as sess:
            sess.clear()
        resp = _login(client, "erin", "wrongpassword")
        assert resp.status_code == 200
        assert "帳號或密碼不正確".encode() in resp.data

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


class TestCrossTenantIsolation2026_10_08:
    """2026-10-08 安全審查（docs/security_review.md 30 節）：
    /api/rollback 路徑穿越、/api/deploy/conflicts 洩漏其他帳號的 app 名稱。"""

    def _login_as(self, client, name="alice"):
        _register(client, name, "password123")
        _login(client, name, "password123")

    @pytest.mark.parametrize("bad", ["../user-bob/secret-app", "..", "a/b", "Bad_Name"])
    def test_rollback_rejects_traversal_names(self, client, monkeypatch, bad):
        called = []
        import gitops.rollback  # noqa: F401 —確保子模組已載入
        import importlib
        rb = importlib.import_module("gitops.rollback")
        monkeypatch.setattr(rb, "rollback", lambda *a, **k: called.append(a) or {"ok": True})
        self._login_as(client)
        resp = client.post("/api/rollback", json={"app_name": bad})
        assert resp.status_code == 400
        assert called == []

    def test_port_conflict_with_other_account_hides_its_name(self, client, monkeypatch):
        self._login_as(client)
        monkeypatch.setattr(web_demo, "k8s_get_deployments", lambda ns=None: [])
        monkeypatch.setattr(web_demo, "k8s_get_services", lambda namespace=None, all_namespaces=False: [
            {"name": "bob-secret-svc", "app": "bob-secret-app", "port": 8080, "namespace": "user-bob"},
        ])
        data = client.get("/api/deploy/conflicts?app_name=my-app&port=8080").get_json()
        assert data["port_conflict"]
        assert data["port_conflict_other_account"] is True
        assert "bob" not in json.dumps(data)

    def test_port_conflict_with_own_app_shows_name(self, client, monkeypatch):
        self._login_as(client)
        monkeypatch.setattr(web_demo, "k8s_get_deployments", lambda ns=None: [])
        monkeypatch.setattr(web_demo, "k8s_get_services", lambda namespace=None, all_namespaces=False: [
            {"name": "web-svc", "app": "web", "port": 8080, "namespace": "user-alice"},
        ])
        data = client.get("/api/deploy/conflicts?app_name=my-app&port=8080").get_json()
        assert data["port_conflict"] == "web"
        assert data["port_conflict_other_account"] is False

    def test_same_app_name_in_other_account_still_counts_as_conflict(self, client, monkeypatch):
        """舊邏輯用 app 名稱排除「自己」，別的帳號剛好同名就被誤判成不衝突。"""
        self._login_as(client)
        monkeypatch.setattr(web_demo, "k8s_get_deployments", lambda ns=None: [])
        monkeypatch.setattr(web_demo, "k8s_get_services", lambda namespace=None, all_namespaces=False: [
            {"name": "my-app", "app": "my-app", "port": 8080, "namespace": "user-bob"},
        ])
        data = client.get("/api/deploy/conflicts?app_name=my-app&port=8080").get_json()
        assert data["port_conflict_other_account"] is True

    def test_rollback_missing_app_returns_friendly_404_without_k8s_internals(self, client, monkeypatch):
        """端到端實測發現：找不到 Deployment 時原本把 K8s client 例外（含 Audit-Id 等內部
        HTTP header）整串回給前端。"""
        import importlib
        import types
        rb = importlib.import_module("gitops.rollback")
        monkeypatch.setattr(rb, "rollback", lambda *a, **k: {"ok": False, "message": "all failed", "details": {}})

        class NotFound(Exception):
            status = 404
        class FakeApps:
            def read_namespaced_deployment(self, name, ns):
                raise NotFound("(404) HTTP response headers: {'Audit-Id': 'secret-internal-id'}")
        monkeypatch.setattr(web_demo, "K8S_ENABLED", True)
        monkeypatch.setattr(web_demo, "k8s_client", types.SimpleNamespace(AppsV1Api=FakeApps), raising=False)

        self._login_as(client)
        resp = client.post("/api/rollback", json={"app_name": "no-such-app"})
        assert resp.status_code == 404
        body = resp.get_data(as_text=True)
        assert "Audit-Id" not in body and "secret-internal-id" not in body
        assert "no-such-app" in resp.get_json()["error"]


class TestRateLimit2026_10_09:
    """網站上外網前加的頻率限制（core/web_security.py）在真實路由上要生效，
    而且被擋時要給新手看得懂的訊息，不是裸的 429。"""

    def test_login_bruteforce_gets_friendly_429_page(self, client):
        codes = [_login(client, "nobody", "wrong").status_code for _ in range(10)]
        assert 429 not in codes
        resp = _login(client, "nobody", "wrong")
        assert resp.status_code == 429
        assert "操作太頻繁" in resp.get_data(as_text=True)
        assert int(resp.headers["Retry-After"]) >= 1

    def test_api_429_is_json_with_retry_after(self, client):
        # 用便宜的 /api/pods（測試裡 K8s 關閉、立刻回應）打到「其他 API 總上限」120 次；
        # 不要用 /api/chat，那會真的呼叫 Gemini 和模型，又慢又依賴外部服務。
        _register(client, "ivan", "hunter22")
        codes = [client.get("/api/pods").status_code for _ in range(120)]
        assert 429 not in codes
        last = client.get("/api/pods")
        assert last.status_code == 429
        body = last.get_json()
        assert "Too many requests" in body["error"] and body["retry_after"]

    def test_login_page_get_is_never_limited(self, client):
        assert all(client.get("/").status_code == 200 for _ in range(30))


def test_legacy_string_user_does_not_crash_every_request(client):
    """2026-10-09：users.json 裡的舊格式帳號只存一個雜湊字串（不是 dict），封鎖檢查對字串
    呼叫 .get() 直接拋例外，這個帳號的每一個請求都回 500。"""
    web_demo.USERS["legacy-user"] = "240be518fabd2724ddb6f04eeb1da5967448d7e831c08c8fa822809f74c720a9"
    with client.session_transaction() as sess:
        sess["username"] = "legacy-user"
    resp = client.get("/api/status")
    assert resp.status_code != 500


def test_question_starting_with_deployment_is_not_a_deploy_command():
    """2026-10-09：部署規則原本是「開頭是 deploy」，問題「Deployment 跟 Service 差在哪？」
    也被當成部署指令，使用者問概念卻跳出部署單。"""
    r = web_demo._rule_intent("Deployment 跟 Service 差在哪？")
    assert r is None or r["action"] != "deploy"
    assert web_demo._rule_intent("deploy 3 nginx pods for web-frontend")["action"] == "deploy"
    assert web_demo._rule_intent("run nginx with 2 replicas")["action"] == "deploy"


def test_shell_commands_are_stripped_from_chat_replies():
    """2026-10-09 展示時發現：問 CrashLoopBackOff 怎麼辦，回覆教使用者打 kubectl。零接觸平台不該叫新手開終端機。"""
    reply = ("CrashLoopBackOff 代表容器一直重啟。\n"
             "1. 到 Pod 詳情看最近的 log\n"
             "2. 使用 kubectl exec 進容器測試\n"
             "```bash\nkubectl logs orders-api --previous\n```\n"
             "也可以讓 Healer 自動修復，或執行 `docker ps` 看看。")
    out = web_demo._strip_shell_commands("CrashLoopBackOff 怎麼辦", reply)
    assert "kubectl" not in out and "docker" not in out
    assert "到 Pod 詳情看最近的 log" in out and "CrashLoopBackOff 代表容器一直重啟" in out
    # 使用者本來就在問 kubectl 時不過濾
    assert web_demo._strip_shell_commands("kubectl 是什麼？", "kubectl 是命令列工具") == "kubectl 是命令列工具"


def test_internal_markers_and_placeholders_are_stripped():
    reply = ("CrashLoopBackOff 代表容器一直重啟。\n"
             "可以查一下 [現況] 中列出的所有 Pod 名稱。\n"
             "如果知道 <pod-name> 的名稱的話。\n"
             "也可以讓 Healer 自動修復。")
    out = web_demo._strip_shell_commands("CrashLoopBackOff 怎麼辦", reply)
    assert "[現況]" not in out and "<pod-name>" not in out
    assert "讓 Healer 自動修復" in out
