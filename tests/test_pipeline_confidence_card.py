"""部署單的可靠度欄位（web_demo._pipeline_confidence，2026-11-18）。web_demo 要 Flask 等依賴，CI 沒裝就跳過。"""
import pytest

web_demo = pytest.importorskip("web_demo")
from agents.pipeline import schema  # noqa: E402


def _spec(image, source, scores):
    s = schema.new_spec("x")
    s["base"]["image"] = {"value": image, "source": source, "evidence": None}
    if scores is not None:
        s["choose_scores"] = scores
    return s


def test_inferred_image_gets_calibrated_reliability(monkeypatch):
    from agents.pipeline import calibration
    monkeypatch.setattr(calibration, "_table", {})
    c = web_demo._pipeline_confidence(_spec("ghost:5", "inferred", [["wordpress", 0.7], ["ghost", 0.985]]))
    assert c["image"]["level"] == "hi" and c["image"]["raw"] == 0.985


def test_user_image_is_100_and_missing_scores_show_nothing():
    assert web_demo._pipeline_confidence(_spec("ghost", "user", [["ghost", 0.5]]))["image"]["pct"] == 100   # 使用者講的
    assert "image" not in web_demo._pipeline_confidence(_spec("ghost", "inferred", None))
    assert "image" not in web_demo._pipeline_confidence(_spec("nginx", "inferred", [["ghost", 0.99]]))   # 選的不在分數表裡


def test_port_fallback_value_shows_no_number():
    s = _spec("unknown-app", "inferred", None)
    s["base"]["port"] = {"value": 80, "source": "default", "evidence": "預設值"}
    assert "port" not in web_demo._pipeline_confidence(s)
    s["base"]["port"] = {"value": 2368, "source": "default", "evidence": "查表：映像檔預設 port"}
    c = web_demo._pipeline_confidence(s)
    assert "port" not in c or c["port"]["pct"] <= 95
