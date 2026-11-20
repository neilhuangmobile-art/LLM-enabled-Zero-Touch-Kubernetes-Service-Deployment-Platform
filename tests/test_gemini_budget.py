"""Gemini 測試花費上限（2026-11-18）：設了 GEMINI_MAX_CALLS／GEMINI_MAX_TOKENS 就不能超過；沒設不受影響。
不連網：用假的 client 取代真的 Gemini。"""
import types

import pytest

gc = pytest.importorskip("core.gemini_client")


class _FakeModels:
    def __init__(self):
        self.n = 0

    def generate_content(self, model, contents, config):
        self.n += 1
        um = types.SimpleNamespace(prompt_token_count=100, candidates_token_count=20, thoughts_token_count=30)
        cand = types.SimpleNamespace(finish_reason="STOP")
        return types.SimpleNamespace(text="ok", usage_metadata=um, candidates=[cand])


@pytest.fixture
def fake(monkeypatch):
    models = _FakeModels()
    monkeypatch.setattr(gc, "_load_keys", lambda: ["k"])
    monkeypatch.setattr(gc, "_current_client", lambda: types.SimpleNamespace(models=models))
    monkeypatch.setattr(gc, "_rotate_key", lambda: False)
    for k in gc._usage:
        monkeypatch.setitem(gc._usage, k, 0)
    monkeypatch.delenv("GEMINI_MAX_CALLS", raising=False)
    monkeypatch.delenv("GEMINI_MAX_TOKENS", raising=False)
    return models


def test_no_cap_means_no_limit(fake):
    for _ in range(5):
        assert gc._generate("s", "u", "t") == "ok"
    assert fake.n == 5
    assert gc.usage()["calls"] == 5 and gc.usage()["prompt_tokens"] == 500 and gc.usage()["thought_tokens"] == 150


def test_call_cap_blocks_further_calls(fake, monkeypatch):
    monkeypatch.setenv("GEMINI_MAX_CALLS", "2")
    assert gc._generate("s", "u", "t") == "ok"
    assert gc._generate("s", "u", "t") == "ok"
    assert gc._generate("s", "u", "t") is None      # 第 3 次被擋，沒有真的送出
    assert fake.n == 2 and gc.usage()["blocked"] == 1


def test_token_cap_blocks(fake, monkeypatch):
    monkeypatch.setenv("GEMINI_MAX_TOKENS", "200")  # 一次 150 token：第 1 次後 150，第 2 次後 300，第 3 次擋
    assert gc._generate("s", "u", "t") == "ok"
    assert gc._generate("s", "u", "t") == "ok"
    assert gc._generate("s", "u", "t") is None
    assert fake.n == 2


def test_bad_cap_value_fails_closed(fake, monkeypatch):
    monkeypatch.setenv("GEMINI_MAX_CALLS", "abc")
    assert gc._generate("s", "u", "t") is None
    assert fake.n == 0


def test_cap_holds_across_key_retries(fake, monkeypatch):
    """換 key 重試也要算：上限 2 次、3 把 key 都回 429，最多只送出 2 次。"""
    monkeypatch.setenv("GEMINI_MAX_CALLS", "2")
    monkeypatch.setattr(gc, "_load_keys", lambda: ["k1", "k2", "k3"])
    monkeypatch.setattr(gc, "_is_quota_error", lambda e: True)

    def boom(model, contents, config):
        fake.n += 1
        raise RuntimeError("429")
    fake.generate_content = boom
    assert gc._generate("s", "u", "t") is None
    assert fake.n == 2
