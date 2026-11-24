"""獨立的目標判斷（PIPELINE_TARGET_CHECK）、自己的程式不用現成軟體代替、面向問題卡（2026-11-24）。"""
import json

import pytest

from agents.pipeline import gap_check, normalize, runner, schema
from agents.pipeline.prompts import TARGET_CHECK_SYSTEM


def _gemini(reply):
    def g(system, user):
        assert system == TARGET_CHECK_SYSTEM
        return reply if isinstance(reply, str) else json.dumps(reply)
    return g


def _spec(text="表哥幫店裡寫了一個小程式，幫我讓它一直開著"):
    return schema.new_spec(text)


OWN = {"target": "own_program", "runtime_known": False, "artifact_given": False, "name_or_ref": None, "reason": "別人寫的程式"}


@pytest.mark.parametrize("mode", ["off", None])
def test_off_does_not_call(monkeypatch, mode):
    if mode:
        monkeypatch.setenv("PIPELINE_TARGET_CHECK", mode)
    else:
        monkeypatch.delenv("PIPELINE_TARGET_CHECK", raising=False)
    s = _spec()
    normalize._target_check(s, lambda *a: pytest.fail("off 不能呼叫"))
    assert "target" not in s


def test_skips_when_user_named_image(monkeypatch):
    monkeypatch.setenv("PIPELINE_TARGET_CHECK", "on")
    s = _spec("幫我跑 nginx")
    schema.set_field(s, "image", "nginx", "user")
    normalize._target_check(s, lambda *a: pytest.fail("使用者講了映像檔就不問"))
    assert "target" not in s


def test_garbage_reply_is_ignored(monkeypatch):
    monkeypatch.setenv("PIPELINE_TARGET_CHECK", "on")
    s = _spec()
    normalize._target_check(s, _gemini("不是 JSON"))
    assert "target" not in s
    normalize._target_check(s, _gemini({"target": "something_else"}))
    assert "target" not in s


def test_own_program_without_runtime_asks_for_image_or_language(monkeypatch):
    monkeypatch.setenv("PIPELINE_TARGET_CHECK", "on")
    s = _spec()
    normalize._target_check(s, _gemini(OWN))
    schema.set_field(s, "image", "alpine", "inferred")     # 推理代理猜的執行環境
    gap_check._choose_software(s, None)
    assert schema.value(s, "image") is None
    q = [q for q in s["questions"] if q["field"] == "image"][0]
    assert "映像檔" in q["question"] and any(o["value"].startswith("python") for o in q["options"])
    gap_check.facet_questions(s)
    assert [q["field"] for q in s["questions"]] == ["image", "ask:expose", "ask:storage"]


def test_own_program_with_runtime_is_not_asked(monkeypatch):
    monkeypatch.setenv("PIPELINE_TARGET_CHECK", "on")
    s = _spec("我寫了一個 python 程式要一直跑")
    s["target"] = dict(OWN, runtime_known=True)
    assert gap_check._target_ask(s) is None


def test_existing_system_question_uses_users_words(monkeypatch):
    monkeypatch.setenv("PIPELINE_TARGET_CHECK", "on")
    s = _spec("把那個報表系統架起來")
    s["target"] = dict(OWN, target="existing_system", name_or_ref="報表系統")
    gap_check._choose_software(s, None)
    q = [q for q in s["questions"] if q["field"] == "image"][0]
    assert "報表系統" in q["question"]


def test_log_mode_records_but_does_not_change(monkeypatch):
    monkeypatch.setenv("PIPELINE_TARGET_CHECK", "log")
    s = _spec()
    normalize._target_check(s, _gemini(OWN))
    assert s["target"]["target"] == "own_program"
    assert gap_check._target_ask(s) is None
    gap_check.facet_questions(s)
    assert s["questions"] == []


def test_facets_skip_what_user_said_and_need_image_question(monkeypatch):
    monkeypatch.setenv("PIPELINE_TARGET_CHECK", "on")
    s = _spec()
    gap_check.facet_questions(s)                       # 沒有要問軟體：不加
    assert s["questions"] == []
    schema.add_question(s, "image", "要部署什麼？")
    schema.set_choice(s, "expose", "internal", "user")  # 使用者講過不對外
    gap_check.facet_questions(s)
    assert [q["field"] for q in s["questions"]] == ["image", "ask:storage"]


def test_resume_applies_facet_answers(monkeypatch):
    monkeypatch.setenv("PIPELINE_TARGET_CHECK", "on")
    s = _spec()
    schema.add_question(s, "ask:expose", "要不要對外？", options=[{"value": "public", "label": "要"}], recommended="public")
    schema.add_question(s, "ask:storage", "要不要保存？", options=[{"value": "keep", "label": "要"}], recommended="no")
    ids = {q["field"]: q["id"] for q in s["questions"]}
    s2 = runner.resume(s, {ids["ask:expose"]: "internal", ids["ask:storage"]: "keep"}, None)
    assert s2["expose"] == {"value": "internal", "source": "user"}
    assert any(e["kind"] == "storage" and e["source"] == "user" for e in s2["extras"])


def test_scheduled_job_is_not_asked(monkeypatch):
    # 定時執行的工作照原本做法（執行環境＋範例指令），不因「自己的程式」改問
    monkeypatch.setenv("PIPELINE_TARGET_CHECK", "on")
    s = _spec("每週日晚上自動發信提醒大家交週報")
    s["target"] = dict(OWN)
    s["extras"].append({"kind": "schedule", "desc": "", "value": {"cron": "0 20 * * 0"}, "evidence": "",
                        "source": "user", "status": "pending", "note": ""})
    assert gap_check._target_ask(s) is None
