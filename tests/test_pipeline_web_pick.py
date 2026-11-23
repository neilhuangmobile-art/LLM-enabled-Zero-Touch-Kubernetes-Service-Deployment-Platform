"""部署單：系統代選軟體的提示與一鍵換（第 4 項）、沒用上 Gemini 的原因（第 5 項）（2026-11-23）。
假模型，不碰網路、GPU、K8s。"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = pytest.mark.integration

web_demo = pytest.importorskip("web_demo")

from agents.pipeline import llm as pllm, schema  # noqa: E402
from core import gemini_client  # noqa: E402
from tests.test_pipeline_web import _ask_llama_spy, _fake_local, _no_choose_mode, _norm, client  # noqa: E402,F401


def _spec(image="ghost", source="inferred", scores=None, trace=None, suggest=None, questions=None):
    s = schema.new_spec("系學會要架個網站放公告")
    s["base"]["image"] = {"value": image, "source": source, "evidence": None}
    if scores is not None:
        s["choose_scores"] = scores
    s["trace"] = trace or []
    if suggest:
        s["clarity"] = {"source": "gemini", "software_clear": True, "suggest": suggest}
    s["questions"] = questions or []
    return s


def test_image_pick_lists_known_alternatives():
    s = _spec("ghost", scores=[["ghost", 0.7], ["wordpress", 0.9], ["python", 0.5], ["no-such/thing", 0.95]],
              trace=[{"step": "select", "detail": "採用 Gemini 建議的 ghost（原本 wordpress）"}], suggest="ghost:5")
    p = web_demo._pipeline_image_pick(s)
    assert p["picked"] == "ghost" and "AI 建議" in p["reason"]
    assert p["alternatives"] == ["wordpress"]          # 不含自己、執行環境、不在知識表的


def test_image_pick_hides_low_confidence_alternatives():
    """信心度太低的候選不列（排隊軟體選 rabbitmq 時，mattermost、fluent-bit 只有 0.03）；Gemini 建議的照樣列。"""
    s = _spec("rabbitmq", scores=[["rabbitmq", 0.97], ["redis", 0.70], ["mattermost/mattermost-team-edition", 0.03],
                                  ["fluent/fluent-bit", 0.03]])
    assert web_demo._pipeline_image_pick(s)["alternatives"] == ["redis"]
    s = _spec("rabbitmq", scores=[["rabbitmq", 0.97], ["mattermost/mattermost-team-edition", 0.03]], suggest="redis:7")
    assert web_demo._pipeline_image_pick(s)["alternatives"] == ["redis"]


def test_image_pick_reason_without_gemini():
    p = web_demo._pipeline_image_pick(_spec("wordpress", scores=[["wordpress", 0.9], ["ghost", 0.3]]))
    assert "候選" in p["reason"] and p["alternatives"] == ["ghost"]
    assert "推出來" in web_demo._pipeline_image_pick(_spec("wordpress"))["reason"]


def test_image_pick_hidden_when_user_named_or_still_asking():
    assert web_demo._pipeline_image_pick(_spec("nginx", "user")) is None
    assert web_demo._pipeline_image_pick(_spec(questions=[{"field": "image", "question": "要哪個？"}])) is None
    s = _spec()
    s["base"]["image"] = {"value": None, "source": None, "evidence": None}
    assert web_demo._pipeline_image_pick(s) is None


def test_normalizer_reason(monkeypatch):
    s = schema.new_spec("x")
    s["backend"]["normalize"] = "gemini"
    assert web_demo._normalizer_reason(s) is None
    s["backend"]["normalize"] = "local"
    monkeypatch.setattr(gemini_client, "is_available", lambda: False)
    assert web_demo._normalizer_reason(s) == "no_key"
    monkeypatch.setattr(gemini_client, "is_available", lambda: True)
    for kind in ("quota", "budget", "error", "truncated"):
        monkeypatch.setattr(gemini_client, "_last_failure", kind)
        assert web_demo._normalizer_reason(s) == kind
    monkeypatch.setattr(gemini_client, "_last_failure", None)
    assert web_demo._normalizer_reason(s) == "bad_output"      # 有回答但看不懂


def test_gemini_client_records_failure(monkeypatch):
    monkeypatch.setattr(gemini_client, "_last_failure", None)      # 測完還原，不影響其他測試
    monkeypatch.setattr(gemini_client, "_load_keys", lambda: [])
    gemini_client._generate("s", "u", "測試")
    assert gemini_client.last_failure() == "no_key"


def test_parse_returns_reason_when_gemini_unused(client, monkeypatch):
    monkeypatch.setenv("DEPLOY_PIPELINE", "multiagent")
    _ask_llama_spy(monkeypatch)
    monkeypatch.setattr(pllm, "local_available", lambda: True)
    monkeypatch.setattr(gemini_client, "is_available", lambda: False)
    monkeypatch.setattr(pllm, "local_llm", _fake_local(_norm({"image": "mysql:8.0"}), {"workload": "service"}))
    d = client.post("/api/deploy/parse", json={"input": "幫我弄一個 mysql 8.0 的資料庫"}).get_json()
    assert d["pipeline"]["normalizer_reason"] == "no_key"
    assert d["pipeline"]["image_pick"] is None                  # 使用者自己講的軟體不顯示代選區塊


def test_software_info_endpoint(client):
    d = client.get("/api/pipeline/software_info?image=redis:7").get_json()
    assert d["image"] == "redis" and d["port"] == 6379
    r = client.get("/api/pipeline/software_info?image=no-such/thing")
    assert r.status_code == 404 and "知識表" in r.get_json()["error"]
    client.post("/auth/logout")
    with web_demo.app.test_client() as anon:
        assert anon.get("/api/pipeline/software_info?image=redis").status_code == 401


def test_page_has_pick_and_fallback_ui(client):
    html = client.get("/").get_data(as_text=True)
    for s in ("function pickHTML", "function fallbackHTML", "swapPickedImage", "/api/pipeline/software_info"):
        assert s in html, s
