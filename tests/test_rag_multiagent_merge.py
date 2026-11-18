"""2026-11-18 融合測試：多代理人流程的選軟體改用 rag/field_retriever，模型伺服器兩種顯卡都能載入。
不載入真的模型：field_retriever 用 monkeypatch 換成假的結果。"""
import importlib

import pytest

from agents.pipeline import software_rag as sr
from core.pipeline_model_server import plan_precision


# ── 模型伺服器精度：NVIDIA 用 4-bit，AMD 用 bf16 ─────────────────────────
@pytest.mark.parametrize("want,backend,bnb,expect", [
    ("auto", "cuda", True, "4bit"),      # 學校 NVIDIA
    ("auto", "rocm", False, "bf16"),     # 家裡 AMD
    ("auto", "cuda", False, "bf16"),     # NVIDIA 但沒裝 bitsandbytes
    ("4bit", "rocm", False, "bf16"),     # 要求 4-bit 但 AMD 做不到，改 bf16 並說明
    ("bf16", "cuda", True, "bf16"),
    ("fp16", "rocm", False, "fp16"),
])
def test_plan_precision(want, backend, bnb, expect):
    prec, why = plan_precision(want, backend, bnb)
    assert prec == expect and why


def test_plan_precision_no_bf16_support():
    assert plan_precision("auto", "rocm", False, bf16_ok=False)[0] == "fp16"


# ── 選軟體的 RAG ─────────────────────────────────────────────────
@pytest.fixture
def fresh(monkeypatch):
    monkeypatch.delenv("RAG_BACKEND", raising=False)
    importlib.reload(sr)
    yield sr
    importlib.reload(sr)


def test_items_include_field_catalog(fresh):
    images = {it["image"] for it in fresh.items()}
    assert "nginx" in images                      # 原本的知識表就有
    assert "requarks/wiki" in images              # 只有 RAG 軟體目錄有（wikijs）
    assert fresh.is_known("requarks/wiki:2")


def test_same_image_keeps_teammate_entry(fresh):
    nginx = [it for it in fresh.items() if it["image"] == "nginx"]
    assert len(nginx) == 1 and nginx[0].get("source") != "rag/knowledge/catalog.json"


def test_field_candidates_mapping(fresh, monkeypatch):
    from rag import field_retriever as fr
    calls = []

    def fake(text, field, top_k=3, method=None):
        calls.append(text)
        return [{"value": "nginx:1.27", "score": 0.03, "source": "catalog#nginx"},
                {"value": "requarks/wiki:2", "score": 0.02, "source": "catalog#wikijs"},
                {"value": "nginx:1.27", "score": 0.01, "source": "catalog#nginx"}]
    monkeypatch.setattr(fr, "retrieve_for_field", fake)
    out = fresh.candidates("社團介紹頁")
    assert [c["image"] for c in out] == ["nginx", "requarks/wiki"]      # 去重、只留映像檔名稱
    assert out[1]["desc"]                                               # 中文說明來自 RAG 軟體目錄
    fresh.candidates("社團介紹頁")
    assert len(calls) == 1                                              # 同一句只查一次


def test_field_error_falls_back_to_legacy(fresh, monkeypatch):
    from rag import field_retriever as fr

    def boom(*a, **k):
        raise RuntimeError("index missing")
    monkeypatch.setattr(fr, "retrieve_for_field", boom)
    monkeypatch.setattr(fresh, "_legacy_candidates", lambda text, k=4: [{"image": "httpd", "desc": "", "score": 1}])
    assert fresh.candidates("網站")[0]["image"] == "httpd"


def test_backend_switch(fresh, monkeypatch):
    monkeypatch.setenv("RAG_BACKEND", "legacy")
    assert fresh.backend() == "legacy"
    monkeypatch.setenv("RAG_BACKEND", "field")
    assert fresh.backend() == "field"
    fresh.set_embedder(lambda texts: [[1.0] for _ in texts])            # 既有測試的掛鉤：固定走原本的查法
    assert fresh.backend() == "legacy"
    fresh.set_embedder(None)
    assert fresh.backend() == "field"
