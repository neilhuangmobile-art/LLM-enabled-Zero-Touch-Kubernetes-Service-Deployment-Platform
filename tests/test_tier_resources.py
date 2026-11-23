"""資源刻度規則（PIPELINE_SIZE_TIER）與不帶錨定數字的推理 prompt（PIPELINE_REASON_SIZING）（2026-11-23）。"""
import pytest

from agents.pipeline import gap_check, reasoner, schema, software_rag
from agents.pipeline.prompts import REASON_SIZING_TIERS, REASON_SYSTEM

REC = {"rabbitmq": ("250m", "256Mi"), "nginx": ("100m", "128Mi")}


@pytest.fixture
def rec(monkeypatch):
    monkeypatch.setattr(software_rag, "recommended_cpu", lambda img: REC.get(img.split(":")[0], ("", ""))[0])
    monkeypatch.setattr(software_rag, "recommended_memory", lambda img: REC.get(img.split(":")[0], ("", ""))[1])


def _spec(image="rabbitmq", cpu="100m", mem="128Mi", src="inferred"):
    s = schema.new_spec("x")
    s["base"]["image"] = {"value": image, "source": "inferred", "evidence": None}
    s["base"]["cpu"] = {"value": cpu, "source": src, "evidence": None}
    s["base"]["memory"] = {"value": mem, "source": src, "evidence": None}
    return s


def _res(s):
    return schema.value(s, "cpu"), schema.value(s, "memory")


def test_off_by_default(rec, monkeypatch):
    monkeypatch.delenv("PIPELINE_SIZE_TIER", raising=False)
    s = _spec()
    gap_check._tier_resources(s)
    assert _res(s) == ("100m", "128Mi") and not s["notices"]


def test_floor_raises_to_one_tier_above_recommendation(rec, monkeypatch):
    monkeypatch.setenv("PIPELINE_SIZE_TIER", "floor")
    s = _spec()
    gap_check._tier_resources(s)
    assert _res(s) == ("500m", "512Mi") and any("多留一格" in n for n in s["notices"])
    gap_check._tier_resources(s)                                 # 再跑一次不變（冪等）
    assert _res(s) == ("500m", "512Mi")
    s = _spec(cpu="1000m", mem="2Gi")
    gap_check._tier_resources(s)
    assert _res(s) == ("1000m", "2Gi")                           # floor 不把比較大的調小


def test_set_uses_one_tier_above(rec, monkeypatch):
    monkeypatch.setenv("PIPELINE_SIZE_TIER", "set")
    s = _spec(cpu="1000m", mem="2Gi")
    gap_check._tier_resources(s)
    assert _res(s) == ("500m", "512Mi")
    s = _spec("nginx", cpu="250m", mem="512Mi")
    gap_check._tier_resources(s)
    assert _res(s) == ("250m", "256Mi")


def test_untouched_cases(rec, monkeypatch):
    monkeypatch.setenv("PIPELINE_SIZE_TIER", "floor")
    s = _spec(src="user")
    gap_check._tier_resources(s)
    assert _res(s) == ("100m", "128Mi")                          # 使用者講的不動
    s = _spec("python:3.12")
    gap_check._tier_resources(s)
    assert _res(s) == ("100m", "128Mi")                          # 執行環境（使用者自己的程式）不動
    s = _spec("someone/unknown")
    gap_check._tier_resources(s)
    assert _res(s) == ("100m", "128Mi")                          # 知識表沒有建議值不動


def test_tier_helpers():
    assert gap_check._tier_up(gap_check.CPU_TIERS, 250) == 500
    assert gap_check._tier_up(gap_check.CPU_TIERS, 200) == 500   # 200m 最接近 250m 那一格
    assert gap_check._tier_up(gap_check.MEM_TIERS, 4096) == 4096
    assert gap_check._fmt_mem(1024) == "1Gi" and gap_check._fmt_mem(512) == "512Mi"
    assert gap_check._mc("0.5") == 500 and gap_check._mc("x") == -1


def test_reason_prompt_switch(monkeypatch):
    monkeypatch.delenv("PIPELINE_REASON_SIZING", raising=False)
    assert reasoner.reason_system().startswith(REASON_SYSTEM)
    monkeypatch.setenv("PIPELINE_REASON_SIZING", "tiers")
    p = reasoner.reason_system()
    assert p.startswith(REASON_SIZING_TIERS) and "<e.g. 250m>" not in p and "pick one step" in p
