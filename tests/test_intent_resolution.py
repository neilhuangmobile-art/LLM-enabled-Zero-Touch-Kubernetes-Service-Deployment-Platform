"""
測試 2026-09-16 新增的「模糊語意理解層」：
  - web_demo.py 的 _looks_like_pronoun() / _resolve_intent_name()（指代消解 +
    模糊比對真實資源名稱），/api/intent 路由把這兩者接起來
  - web_demo.py 的 _prepare_deploy() 對 core/deploy_ambiguity.py 判斷結果的處理
    （部署缺欄位時，猜還是反問，parsed_override 不該再反問一次）

判斷錯了的後果：指代/模糊比對該反問卻沒反問 → 破壞性操作（scale/delete/rollback）
套用到錯誤的資源；該放行卻擋住 → 每次操作都要多問一輪，體驗變差。兩個方向都要覆蓋。

標記 integration：import web_demo.py 會連帶 import llama_client.py，llama_client.py
會 import torch，CI 沒裝這些重依賴跑不起來，本機專案 Python 3.9 環境裝好了才能跑。
"""
import sys
import os

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = pytest.mark.integration

web_demo = pytest.importorskip("web_demo")


class TestLooksLikePronoun:
    def test_chinese_pronouns_detected(self):
        for word in ["它", "這個", "那個", "剛剛部署的那個", "剛才那個", "這一個"]:
            assert web_demo._looks_like_pronoun(word) is True, word

    def test_english_pronouns_detected(self):
        for word in ["it", "It", "that", "that one", "this", "this one"]:
            assert web_demo._looks_like_pronoun(word) is True, word

    def test_real_resource_names_are_not_pronouns(self):
        for name in ["web-frontend", "my-cache", "it-service", "this-app"]:
            assert web_demo._looks_like_pronoun(name) is False, name


class TestResolveIntentNameReferenceResolution:
    def test_pronoun_with_last_resource_is_substituted(self, monkeypatch):
        monkeypatch.setattr(web_demo, "k8s_get_pods", lambda namespace=None: [])
        monkeypatch.setattr(web_demo, "k8s_get_deployments", lambda namespace=None: [{"name": "web-frontend"}])
        intent = {"action": "scale", "args": {"name": "它", "replicas": 5}, "confidence": 1.0}
        result = web_demo._resolve_intent_name(intent, "user-alice", last_resource="web-frontend")
        assert result["action"] == "scale"
        assert result["args"]["name"] == "web-frontend"
        assert result["args"]["replicas"] == 5  # 其他 arg 不該被動到

    def test_pronoun_without_last_resource_asks_which_one(self, monkeypatch):
        monkeypatch.setattr(web_demo, "k8s_get_pods", lambda namespace=None: [])
        monkeypatch.setattr(web_demo, "k8s_get_deployments", lambda namespace=None: [{"name": "web-frontend"}])
        intent = {"action": "delete", "args": {"name": "那個"}, "confidence": 1.0}
        result = web_demo._resolve_intent_name(intent, "user-alice", last_resource=None)
        assert result["action"] == "clarify"
        assert result["args"]["needs_reference"] is True

    def test_resolved_deployment_name_is_not_ambiguous_with_its_own_pods(self, monkeypatch):
        # 真實回歸案例（2026-09-16 端對端手動測試在真實 K8s 叢集上發現）：部署一個
        # 名叫 cache-service 的 Deployment 後，它自己的 Pod 會叫
        # cache-service-<hash>-<hash>，兩者都會出現在 _all_resource_names() 的
        # 候選名單裡。指代消解把「它」換成 lastResource="cache-service" 之後，
        # 這個名稱本身在候選名單裡是完全相符（exact），不該因為它自己的 Pod 也是
        # 高分的前綴相符（prefix, 0.9）就被誤判成「不夠確定」而跳出候選清單。
        monkeypatch.setattr(web_demo, "k8s_get_pods", lambda namespace=None: [
            {"name": "cache-service-7fb8db84-fzqhh"},
            {"name": "cache-service-7fb8db84-m96tv"},
        ])
        monkeypatch.setattr(web_demo, "k8s_get_deployments", lambda namespace=None: [{"name": "cache-service"}])
        intent = {"action": "scale", "args": {"name": "它", "replicas": 4}, "confidence": 1.0}
        result = web_demo._resolve_intent_name(intent, "user-alice", last_resource="cache-service")
        assert result["action"] == "scale"
        assert result["args"]["name"] == "cache-service"

    def test_pod_kind_pronoun_uses_pod_name_key(self, monkeypatch):
        monkeypatch.setattr(web_demo, "k8s_get_pods", lambda namespace=None: [{"name": "web-frontend-abc12"}])
        monkeypatch.setattr(web_demo, "k8s_get_deployments", lambda namespace=None: [])
        intent = {"action": "healer_fix", "args": {"pod_name": "它"}, "confidence": 1.0}
        result = web_demo._resolve_intent_name(intent, "user-alice", last_resource="web-frontend-abc12")
        assert result["action"] == "healer_fix"
        assert result["args"]["pod_name"] == "web-frontend-abc12"


class TestResolveIntentNameFuzzyMatching:
    def test_exact_name_passes_through_unchanged(self, monkeypatch):
        monkeypatch.setattr(web_demo, "k8s_get_pods", lambda namespace=None: [])
        monkeypatch.setattr(web_demo, "k8s_get_deployments", lambda namespace=None: [{"name": "web-frontend"}])
        intent = {"action": "delete", "args": {"name": "web-frontend"}, "confidence": 1.0}
        result = web_demo._resolve_intent_name(intent, "user-alice")
        assert result["action"] == "delete"
        assert result["args"]["name"] == "web-frontend"

    def test_typo_is_silently_corrected_when_confident(self, monkeypatch):
        monkeypatch.setattr(web_demo, "k8s_get_pods", lambda namespace=None: [])
        monkeypatch.setattr(
            web_demo, "k8s_get_deployments",
            lambda namespace=None: [{"name": "web-frontend"}, {"name": "auto-app"}],
        )
        intent = {"action": "delete", "args": {"name": "web-fronted"}, "confidence": 1.0}
        result = web_demo._resolve_intent_name(intent, "user-alice")
        assert result["action"] == "delete"
        assert result["args"]["name"] == "web-frontend"

    def test_ambiguous_name_downgrades_to_clarify_with_candidates(self, monkeypatch):
        monkeypatch.setattr(web_demo, "k8s_get_pods", lambda namespace=None: [])
        monkeypatch.setattr(
            web_demo, "k8s_get_deployments",
            lambda namespace=None: [{"name": "web-frontend"}, {"name": "web-frontend2"}],
        )
        intent = {"action": "delete", "args": {"name": "web-fronten"}, "confidence": 1.0}
        result = web_demo._resolve_intent_name(intent, "user-alice")
        assert result["action"] == "clarify"
        assert len(result["args"]["candidates"]) >= 2

    def test_name_not_found_at_all_downgrades_to_clarify_not_found(self, monkeypatch):
        monkeypatch.setattr(web_demo, "k8s_get_pods", lambda namespace=None: [])
        monkeypatch.setattr(web_demo, "k8s_get_deployments", lambda namespace=None: [{"name": "web-frontend"}])
        intent = {"action": "delete", "args": {"name": "totally-unrelated-xyz"}, "confidence": 1.0}
        result = web_demo._resolve_intent_name(intent, "user-alice")
        assert result["action"] == "clarify"
        assert result["args"]["not_found"] is True

    def test_empty_cluster_passes_through_without_blocking(self, monkeypatch):
        # 查不到叢集資源清單（K8s 未連線、或這個 namespace 真的什麼都沒有）時，
        # 這只是錦上添花的安全網，不能因為核對不了就擋住整個操作。
        monkeypatch.setattr(web_demo, "k8s_get_pods", lambda namespace=None: [])
        monkeypatch.setattr(web_demo, "k8s_get_deployments", lambda namespace=None: [])
        intent = {"action": "delete", "args": {"name": "anything"}, "confidence": 1.0}
        result = web_demo._resolve_intent_name(intent, "user-alice")
        assert result["action"] == "delete"
        assert result["args"]["name"] == "anything"

    def test_deploy_action_is_never_touched(self, monkeypatch):
        # app_name 是要建立的新名稱，不是要在既有資源裡找一個，不該被模糊比對攔截。
        monkeypatch.setattr(web_demo, "k8s_get_pods", lambda namespace=None: [])
        monkeypatch.setattr(web_demo, "k8s_get_deployments", lambda namespace=None: [{"name": "web-frontend"}])
        intent = {"action": "deploy", "args": {"app_name": "web-fronted"}, "confidence": 1.0}
        result = web_demo._resolve_intent_name(intent, "user-alice")
        assert result is intent

    def test_qa_action_is_never_touched(self, monkeypatch):
        intent = {"action": "qa", "args": {}, "confidence": 0.0}
        result = web_demo._resolve_intent_name(intent, "user-alice")
        assert result is intent


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(web_demo, "K8S_ENABLED", True)
    monkeypatch.setattr(web_demo, "USERS", {})
    monkeypatch.setattr(web_demo, "__file__", str(tmp_path / "web_demo.py"))
    web_demo.app.config["TESTING"] = True
    with web_demo.app.test_client() as c:
        yield c


def _register(client, username, password="hunter22"):
    return client.post("/auth/register", data={"username": username, "password": password, "confirm": password})


class TestApiIntentRoute:
    def test_client_intent_with_pronoun_and_last_resource_resolves(self, client, monkeypatch):
        _register(client, "alice")
        monkeypatch.setattr(web_demo, "k8s_get_pods", lambda namespace=None: [])
        monkeypatch.setattr(web_demo, "k8s_get_deployments", lambda namespace=None: [{"name": "web-frontend"}])
        resp = client.post("/api/intent", json={
            "message": "把它擴大到 5 個",
            "client_intent": {"action": "scale", "args": {"name": "它", "replicas": 5}},
            "last_resource": "web-frontend",
        })
        data = resp.get_json()
        assert data["action"] == "scale"
        assert data["args"]["name"] == "web-frontend"

    def test_client_intent_with_pronoun_and_no_last_resource_asks(self, client, monkeypatch):
        _register(client, "bob")
        monkeypatch.setattr(web_demo, "k8s_get_pods", lambda namespace=None: [])
        monkeypatch.setattr(web_demo, "k8s_get_deployments", lambda namespace=None: [{"name": "web-frontend"}])
        resp = client.post("/api/intent", json={
            "message": "刪除那個",
            "client_intent": {"action": "delete", "args": {"name": "那個"}},
        })
        data = resp.get_json()
        assert data["action"] == "clarify"
        assert data["args"]["needs_reference"] is True

    def test_rule_matched_intent_without_name_check_still_works(self, client):
        _register(client, "carol")
        resp = client.post("/api/intent", json={"message": "list pods"})
        data = resp.get_json()
        assert data["action"] == "list_pods"
        assert data["source"] == "rule"

    def test_unauthenticated_request_rejected(self, client):
        resp = client.post("/api/intent", json={"message": "list pods"})
        assert resp.status_code == 401


class TestPrepareDeployAmbiguityGate:
    def test_ambiguous_flag_short_circuits_before_full_review(self, monkeypatch):
        monkeypatch.setattr(web_demo, "K8S_ENABLED", False)
        monkeypatch.setattr(web_demo, "ask_llama", lambda text: {
            "pods": 1, "image": "nginx:latest", "app_name": "auto-app", "port": 80,
            "_ambiguous": True, "_ambiguous_reason": "看不出想部署什麼服務",
        })
        parsed, enriched, review, error = web_demo._prepare_deploy("幫我起一個服務")
        assert error is not None
        payload, status = error
        assert payload["needs_clarification"] is True
        assert status == 200
        assert review is None  # 不該浪費一次完整審查在純猜測上

    def test_non_ambiguous_deploy_runs_the_full_review(self, monkeypatch):
        monkeypatch.setattr(web_demo, "K8S_ENABLED", False)
        monkeypatch.setattr(web_demo, "ask_llama", lambda text: {
            "pods": 3, "image": "redis:latest", "app_name": "cache-service", "port": 6379,
        })
        parsed, enriched, review, error = web_demo._prepare_deploy("deploy 3 redis pods for cache-service")
        assert error is None
        assert review is not None
        assert review.get("decision") in ("approve", "warn", "block")

    def test_ambiguous_flag_ignored_when_parsed_override_given(self, monkeypatch):
        # parsed_override 代表使用者自己在確認卡片上編輯過、按下一步送回來的值，
        # 走 _sanitize_deploy_payload（本來就不會帶 _ambiguous 這個 key），
        # 就算硬塞也不該讓這個分支生效——確認整條路徑不會被誤判成要反問。
        monkeypatch.setattr(web_demo, "K8S_ENABLED", False)
        parsed, enriched, review, error = web_demo._prepare_deploy(
            "deploy redis",
            parsed_override={
                "app_name": "auto-app", "image": "nginx:latest", "pods": 1, "port": 80,
                "_ambiguous": True,
            },
        )
        if error is not None:
            payload, _ = error
            assert payload.get("needs_clarification") is not True
