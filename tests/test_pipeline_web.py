"""多代理人部署解析接到網站（/api/deploy/parse，2026-11-17）。

- 開關沒開（DEPLOY_PIPELINE 沒設）：照舊呼叫 ask_llama，不碰新流程。
- 開關打開：走 agents/pipeline；有問題時回問題卡、中間 JSON 留在伺服器；回答後接著跑。
- 本地模型伺服器不在：退回 ask_llama。
全部用假的模型回覆，不碰網路、GPU、K8s。
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = pytest.mark.integration

web_demo = pytest.importorskip("web_demo")

from agents.pipeline import llm as pllm, schema, software_rag  # noqa: E402
from agents.pipeline.prompts import INTENT_SYSTEM, NORMALIZE_SYSTEM, REASON_SYSTEM, SELECT_SYSTEM, VERIFY_SYSTEM  # noqa: E402


def _fake_local(normalized, reason):
    def llm(system, user, max_new_tokens=768):
        if system == NORMALIZE_SYSTEM:
            return json.dumps(normalized)
        if system == INTENT_SYSTEM:
            return json.dumps({"missing": []})
        if system.startswith(REASON_SYSTEM):
            return json.dumps(reason)
        if system == SELECT_SYSTEM:
            return json.dumps({})
        if system == VERIFY_SYSTEM:
            return json.dumps({"problems": []})
        raise AssertionError("沒看過的 prompt")
    return llm


def _norm(base=None, extras=None):
    b = {f: None for f in schema.BASE_FIELDS}
    b.update(base or {})
    return {"purpose": "x", "base": b, "extras": extras or []}


@pytest.fixture(autouse=True)
def _no_choose_mode(monkeypatch):
    # web_demo 載入時會讀 .env，可能開了 PIPELINE_SELECT_MODE=choose（會呼叫真的模型算信心度）；
    # 這裡測網站流程，選軟體的新做法在 tests/test_choose_software.py
    monkeypatch.delenv("PIPELINE_SELECT_MODE", raising=False)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(web_demo, "K8S_ENABLED", False)
    monkeypatch.setattr(web_demo, "USERS", {})
    monkeypatch.setattr(web_demo, "__file__", str(tmp_path / "web_demo.py"))
    from core.web_security import RateLimiter
    monkeypatch.setattr(web_demo, "_rate_limiter", RateLimiter())
    monkeypatch.setattr(web_demo, "_pipeline_states", {})
    monkeypatch.setattr(pllm, "gemini_llm", lambda s, u: None)   # Gemini 失敗 → 本地模型
    software_rag.set_embedder(lambda texts: [[1.0] for _ in texts])   # 不載入 BGE-M3
    web_demo.app.config["TESTING"] = True
    with web_demo.app.test_client() as c:
        c.post("/auth/register", data={"username": "pipeuser", "password": "hunter22", "confirm": "hunter22"})
        yield c
    software_rag.set_embedder(None)


def _ask_llama_spy(monkeypatch):
    calls = []

    def fake(text):
        calls.append(text)
        return {"app_name": "web", "image": "nginx:latest", "pods": 1, "port": 80}
    monkeypatch.setattr(web_demo, "ask_llama", fake)
    return calls


def test_flag_off_uses_ask_llama(client, monkeypatch):
    monkeypatch.delenv("DEPLOY_PIPELINE", raising=False)
    calls = _ask_llama_spy(monkeypatch)
    monkeypatch.setattr(pllm, "local_llm", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不該呼叫新流程")))
    r = client.post("/api/deploy/parse", json={"input": "部署一個 nginx"})
    assert calls == ["部署一個 nginx"]
    assert "pipeline" not in r.get_json()


def test_flag_on_but_model_server_down_falls_back(client, monkeypatch):
    monkeypatch.setenv("DEPLOY_PIPELINE", "multiagent")
    calls = _ask_llama_spy(monkeypatch)
    monkeypatch.setattr(pllm, "local_available", lambda: False)
    d = client.post("/api/deploy/parse", json={"input": "部署一個 nginx"}).get_json()
    assert calls == ["部署一個 nginx"]
    assert "原本的解析方式" in d["fallback_notice"]        # 不能靜默退回，要讓使用者知道


def test_flag_off_has_no_fallback_notice(client, monkeypatch):
    monkeypatch.delenv("DEPLOY_PIPELINE", raising=False)
    _ask_llama_spy(monkeypatch)
    d = client.post("/api/deploy/parse", json={"input": "部署一個 nginx"}).get_json()
    assert "fallback_notice" not in d


def test_flag_on_full_spec_goes_to_review(client, monkeypatch):
    monkeypatch.setenv("DEPLOY_PIPELINE", "multiagent")
    calls = _ask_llama_spy(monkeypatch)
    monkeypatch.setattr(pllm, "local_available", lambda: True)
    text = "幫我弄一個 mysql 8.0 的資料庫，密碼設 abc12345，資料重開機不能不見"
    monkeypatch.setattr(pllm, "local_llm", _fake_local(
        _norm({"image": "mysql:8.0"}, [
            {"kind": "secret", "desc": "密碼", "value": "abc12345", "evidence": "密碼設 abc12345"},
            {"kind": "storage", "desc": "保存", "value": {"size": None}, "evidence": "資料重開機不能不見"}]),
        {"workload": "service", "expose": "internal"}))
    r = client.post("/api/deploy/parse", json={"input": text})
    d = r.get_json()
    assert calls == []                                  # 沒有走舊的 ask_llama
    assert r.status_code == 200 and not d.get("needs_answers")
    raw = d["raw"]
    assert raw["image"] == "mysql:8.0" and raw["port"] == 3306
    assert raw["volumes"][0]["mount"] == "/var/lib/mysql" and raw["volumes"][0]["kind"] == "pvc"
    assert raw["env"]["MYSQL_ROOT_PASSWORD"] == "abc12345" and "MYSQL_ROOT_PASSWORD" in raw["secret_keys"]
    assert raw["expose"] is False
    assert d["pipeline"]["sources"]["image"] == "user" and d["pipeline"]["sources"]["port"] == "default"


def test_questions_then_answer_resumes_from_server_state(client, monkeypatch):
    monkeypatch.setenv("DEPLOY_PIPELINE", "multiagent")
    _ask_llama_spy(monkeypatch)
    monkeypatch.setattr(pllm, "local_available", lambda: True)
    monkeypatch.setattr(pllm, "local_llm", _fake_local(_norm(), {"questions": [{"question": "要用哪個軟體？"}]}))
    text = "幫我架一個公司內部用的東西"
    d = client.post("/api/deploy/parse", json={"input": text}).get_json()
    assert d["needs_answers"] and d["questions"] and d["pipeline_id"]
    assert "spec" not in d                              # 中間 JSON 不交給前端
    qid = d["questions"][0]["id"]

    # 別的帳號拿同一個編號不能接著跑
    other = web_demo.app.test_client()
    other.post("/auth/register", data={"username": "other1", "password": "hunter22", "confirm": "hunter22"})
    r = other.post("/api/deploy/parse", json={"input": text, "pipeline_id": d["pipeline_id"], "answers": {qid: "nginx"}})
    assert r.status_code == 410

    monkeypatch.setattr(pllm, "local_llm", _fake_local(_norm(), {}))
    r = client.post("/api/deploy/parse", json={"input": text, "pipeline_id": d["pipeline_id"], "answers": {qid: "nginx"}})
    d2 = r.get_json()
    assert r.status_code == 200 and d2["raw"]["image"] == "nginx"
    assert d2["pipeline"]["sources"]["image"] == "user"
    # 用過就失效
    r = client.post("/api/deploy/parse", json={"input": text, "pipeline_id": d["pipeline_id"], "answers": {qid: "nginx"}})
    assert r.status_code == 410


def test_cancel_answer(client, monkeypatch):
    monkeypatch.setenv("DEPLOY_PIPELINE", "multiagent")
    _ask_llama_spy(monkeypatch)
    monkeypatch.setattr(pllm, "local_available", lambda: True)
    text = "每分鐘用 python 跑一次 echo hi"
    monkeypatch.setattr(pllm, "local_llm", _fake_local(
        _norm({"image": "python"}, [{"kind": "schedule", "desc": "定時", "value": {"cron": "* * * * *"}, "evidence": "每分鐘"},
                                    {"kind": "command", "desc": "指令", "value": "echo hi", "evidence": "echo hi"}]),
        {"workload": "cronjob", "schedule": "* * * * *"}))
    d = client.post("/api/deploy/parse", json={"input": text}).get_json()
    q = [q for q in d["questions"] if any(o["value"] == "cancel" for o in q["options"])][0]
    assert all(o["value"] != "__auto__" for o in q["options"])      # 超過上限不給「幫我決定」
    d2 = client.post("/api/deploy/parse", json={"input": text, "pipeline_id": d["pipeline_id"],
                                                "answers": {q["id"]: "cancel"}}).get_json()
    assert d2.get("cancelled")


def test_boundary_precheck_still_applies(client, monkeypatch):
    monkeypatch.setenv("DEPLOY_PIPELINE", "multiagent")
    monkeypatch.setattr(pllm, "local_llm", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不該呼叫模型")))
    r = client.post("/api/deploy/parse", json={"input": "部署 500 個 nginx 副本"})
    assert r.status_code == 422 and "1~100" in r.get_json()["error"]
