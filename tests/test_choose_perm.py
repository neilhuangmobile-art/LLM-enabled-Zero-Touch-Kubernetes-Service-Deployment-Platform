"""選軟體的輪換投票（PIPELINE_CHOOSE_STYLE=perm）與 other 提醒合併、resume 不重設刻意不處理的項目（2026-11-24）。"""
from agents.pipeline import gap_check, reasoner, runner, schema
from agents.pipeline.prompts import CHOOSE_SYSTEM

POOL = ["nginx", "minio/minio", "nextcloud"]
DESC = {i: "" for i in POOL}


def _list_llm(prefer=None, position=None, calls=None):
    """假模型：prefer＝不管排在哪都選這個軟體；position＝永遠選第 N 個（位置偏誤）。"""
    def llm(system, user, max_new_tokens=768):
        assert system == CHOOSE_SYSTEM
        lines = [ln for ln in user.splitlines() if ln[:1].isdigit()]
        if calls is not None:
            calls.append(lines[0])
        if prefer:
            n = next(i + 1 for i, ln in enumerate(lines) if f" {prefer}:" in ln)
        else:
            n = position
        return f'{{"choice": {n}}}'
    return llm


def _spec():
    return schema.new_spec("實驗室要一個物件倉庫")


def test_rotation_puts_each_candidate_first_once():
    calls = []
    s = _spec()
    gap_check._perm_vote(s, _list_llm(prefer="minio/minio", calls=calls), POOL, DESC)
    assert [c.split(". ")[1].split(":")[0] for c in calls] == POOL


def test_consistent_preference_wins():
    s = _spec()
    assert gap_check._perm_vote(s, _list_llm(prefer="minio/minio"), POOL, DESC) == "minio/minio"
    assert s["choose_votes"] == {"minio/minio": 3}


def test_position_bias_cancels_out():
    # 永遠選第 1 個：輪換後每個候選各拿 1 票，沒有過半 → 不採用，交回信心度
    s = _spec()
    assert gap_check._perm_vote(s, _list_llm(position=1), POOL, DESC) is None


def test_none_majority_is_not_adopted():
    s = _spec()
    assert gap_check._perm_vote(s, _list_llm(position=0), POOL, DESC) is None


def _other(evidence):
    return {"kind": "other", "desc": "", "value": None, "evidence": evidence, "source": "user", "status": "pending", "note": ""}


def test_other_extras_become_one_reference_line():
    s = schema.new_spec("幫我把社團介紹的網頁放上去，新生點連結就看得到")
    schema.set_field(s, "image", "nginx", "user")
    s["extras"] += [_other("幫我把社團介紹的網頁放上去"), _other("新生點連結就看得到")]
    reasoner.apply_extras(s)
    assert not any("目前沒辦法自動處理" in n for n in s["notices"])
    lines = [n for n in s["notices"] if n.startswith(reasoner.OTHER_NOTICE)]
    assert len(lines) == 1 and "社團介紹" in lines[0]
    reasoner.apply_extras(s)                                     # 再跑一次不會多一行
    assert len([n for n in s["notices"] if n.startswith(reasoner.OTHER_NOTICE)]) == 1


def test_resume_does_not_reapply_final_extras():
    # rabbitmq 情境：描述被標成「不當指令」(final)，回答別的問題卡後不能又被當成啟動指令
    s = schema.new_spec("一個小程式在背後跑，把訊息排隊")
    schema.set_field(s, "image", "rabbitmq", "inferred")
    s["extras"].append({"kind": "command", "desc": "", "value": "把訊息排隊", "evidence": "把訊息排隊", "source": "user",
                        "status": "unsupported", "note": "已改用現成軟體 rabbitmq", "final": True})
    s["questions"] = [{"id": "q1", "field": "replicas", "question": "要幾台？", "options": []}]
    s2 = runner.resume(s, {"q1": "1"}, None)
    cmd = [e for e in s2["extras"] if e["kind"] == "command"][0]
    assert cmd["status"] == "unsupported" and "command" not in s2["manifest"]
