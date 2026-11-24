"""保存空間碰到平台上限：提醒使用者、評分記實際大小、超過上限的問題卡不算多問（2026-11-24）。"""
import os
import sys

from agents.pipeline import defaults, reasoner, schema

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "experiments", "model_select", "reason"))


def _spec(text, size, source="inferred", evidence=""):
    s = schema.new_spec(text)
    schema.set_field(s, "image", "nextcloud", "inferred")
    s["extras"].append({"kind": "storage", "desc": "", "value": {"size": size}, "evidence": evidence,
                        "source": source, "status": "pending", "note": ""})
    return s


def test_inferred_size_over_limit_is_capped_with_notice():
    s = _spec("社團要放練習影片", "20Gi")
    reasoner.apply_extras(s)
    assert s["manifest"]["volumes"][0]["size"] == f"{defaults.MAX_STORAGE_GI}Gi"
    assert any(n.startswith(reasoner.STORAGE_LIMIT_NOTICE) and "20GB" in n for n in s["notices"])
    assert not s["questions"]


def test_user_size_over_limit_still_asks():
    s = _spec("我要 100GB 的空間", "100Gi", source="user", evidence="我要 100GB 的空間")
    reasoner.apply_extras(s)
    assert [q["field"] for q in s["questions"]] == ["storage_size"]


def test_big_data_hint_adds_notice_without_changing_size():
    s = _spec("熱舞社要放練舞影片給社員下載", None)
    reasoner.apply_extras(s)
    assert s["manifest"]["volumes"][0]["size"] == defaults.DEFAULT_STORAGE
    assert any(n.startswith(reasoner.STORAGE_LIMIT_NOTICE) for n in s["notices"])
    s2 = _spec("公司內部文件放一個 wiki", None)
    reasoner.apply_extras(s2)
    assert not any(n.startswith(reasoner.STORAGE_LIMIT_NOTICE) for n in s2["notices"])


def test_to_pred_records_actual_size():
    from run_pipeline_eval import to_pred
    s = _spec("社團要放練習影片", "20Gi")
    reasoner.apply_extras(s)
    st = to_pred(s)["storage"]
    assert st["size"] == f"{defaults.MAX_STORAGE_GI}Gi" and st["requested"] == "20Gi" and st["limit_notice"]


def _task(must_ask=False):
    return {"expected": {"image": {"accept": ["nextcloud"], "must_ask": must_ask}, "replicas": {"min": 1, "max": 1},
                         "port": {"accept": [80]}, "memory": {"min": "256Mi", "max": "1Gi"}, "cpu": {"min": "100m", "max": "1000m"}}}


def test_limit_question_is_not_over_asking():
    from run_pipeline_dev import score
    p = {"image": "nextcloud", "replicas": 1, "port": 80, "memory": "512Mi", "cpu": "500m",
         "questions": [{"field": "storage_size"}]}
    s = score(_task(), p)
    assert s["passed"] and not s["asked"] and s["limit_asked"]
    assert not score(_task(), dict(p, questions=[{"field": "image"}]))["passed"]
    assert not score(_task(must_ask=True), dict(p, image=None))["passed"]   # 該問的題不能靠上限問題卡過關


def test_passif_over_limit_requirement():
    from passif_score import score_passif
    t = {"pass_if": "保存空間至少 20Gi", "extra": []}
    assert not score_passif(t, {"storage": {"size": "1Gi"}}, True)["passed"]
    assert score_passif(t, {"storage": {"size": "5Gi", "limit_notice": True}}, True)["passed"]
    assert not score_passif(t, {"storage": {"size": "5Gi", "limit_notice": False}}, True)["passed"]
    assert score_passif(t, {"storage": {"size": "1Gi"}, "questions": [{"field": "storage_size"}]}, True)["passed"]
    assert score_passif({"pass_if": "保存空間至少 3Gi", "extra": []}, {"storage": {"size": "5Gi"}}, True)["passed"]
