"""
測試 web_demo.py 的 _verify_grounded_reply()：Chat 回覆的「輸出端事實核對」（2026-09-14
新增，方法 1，見 docs/security_review.md 9 節）。不管模型是被 prompt injection 說服、
還是自己單純幻覈，只要回覆對一個實際不存在的 Deployment 講出肯定的健康狀態，這裡都要
用真實 K8s 清單核對、攔截並改成更正訊息。

標記 integration：import web_demo.py 會連帶 import llama_client.py，llama_client.py
會 import torch（fallback 本地推論路徑用），CI 沒裝這些重依賴跑不起來，本機專案
Python 3.9 環境裝好了才能跑：pytest -m integration tests/test_chat_grounding.py
"""
import sys
import os

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = pytest.mark.integration

# collection 階段就會 import，@pytest.mark.integration 擋不住 CI 沒裝 flask/torch
# 時直接炸掉整個 pytest run（見 test_model_server_injection.py 的說明，實測驗證過）。
web_demo = pytest.importorskip("web_demo")


@pytest.fixture
def fake_cluster(monkeypatch):
    """假造一個叢集：只有 auto-app、my-cache、zt-smoke 三個真實存在的 Deployment。"""
    monkeypatch.setattr(web_demo, "K8S_ENABLED", True)
    monkeypatch.setattr(
        web_demo, "k8s_get_deployments",
        lambda: [{"name": "auto-app"}, {"name": "my-cache"}, {"name": "zt-smoke"}],
    )
    yield


class TestVerifyGroundedReply:
    def test_ghost_service_claimed_healthy_gets_corrected(self, fake_cluster):
        message = "ghost-service-xyz999 這個服務健康嗎？"
        reply = "Ghost service xyz999 現在是健康的，一切正常。"
        result = web_demo._verify_grounded_reply(message, reply)
        assert result != reply
        assert "ghost-service-xyz999" in result
        assert "不存在" in result

    def test_ghost_service_correctly_reported_as_not_found_is_untouched(self, fake_cluster):
        message = "ghost-service-xyz999 這個服務健康嗎？"
        reply = "ghost-service-xyz999 在目前叢集中並不存在，無法確認其狀態。"
        # 這句話本身沒有對一個不存在的服務講「健康」，不該被誤判成需要更正。
        result = web_demo._verify_grounded_reply(message, reply)
        assert result == reply

    def test_real_deployment_claimed_healthy_is_untouched(self, fake_cluster):
        message = "my-cache 健康嗎？"
        reply = "my-cache 目前健康，2 個 Pod 都在正常執行。"
        result = web_demo._verify_grounded_reply(message, reply)
        assert result == reply

    def test_k8s_disabled_skips_check_without_crashing(self, monkeypatch):
        monkeypatch.setattr(web_demo, "K8S_ENABLED", False)
        message = "ghost-service-xyz999 健康嗎？"
        reply = "ghost-service-xyz999 現在是健康的。"
        result = web_demo._verify_grounded_reply(message, reply)
        assert result == reply

    def test_no_hyphenated_candidate_names_is_untouched(self, fake_cluster):
        message = "什麼是 Kubernetes 的 Pod？"
        reply = "Pod 是 Kubernetes 中最小的部署單位，通常運作正常且健康。"
        result = web_demo._verify_grounded_reply(message, reply)
        assert result == reply
