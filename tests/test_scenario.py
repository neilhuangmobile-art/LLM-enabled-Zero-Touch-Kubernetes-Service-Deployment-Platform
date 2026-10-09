"""情境解析部署（agents/scenario.py）的純邏輯測試：抽取、換算、省錢估算、模型數字核對。"""
import pytest

from agents.scenario import (HIGH_CRITICALITY_MIN, MAX_SCENARIO_REPLICAS, _to_int, explicit_replicas,
                             extract_by_rules, has_scenario, merge_model_attrs, plan)


def _plan(text, image="nginx", app="signup", explicit=None, model=None):
    attrs = merge_model_attrs(text, extract_by_rules(text), model)
    return attrs, plan(attrs, image, app, explicit)


@pytest.mark.parametrize("tok,expected", [
    ("500", 500), ("1,000", 1000), ("1.5k", 1500), ("5百", 500), ("兩百", 200),
    ("三十", 30), ("十五", 15), ("一萬二", 12000), ("兩百五", 250), ("一百零五", 105),
])
def test_number_parsing(tok, expected):
    assert _to_int(tok) == expected


def test_signup_event_scenario():
    attrs, r = _plan("幫我部署一個報名網站，大概 500 人報名，三天後活動就結束")
    assert attrs["total_users"] == 500 and attrs["duration_days"] == 3
    assert attrs["needs_persistence"] is True
    assert r["replicas"] == 1 and r["replicas_applied"]
    assert any("100 人" in x for x in r["reasons"])          # 500 × 兩成
    assert any("刪除 signup" in n for n in r["notes"])        # 有結束時間 → 提醒刪除
    assert any("資料" in n for n in r["notes"])               # 報名要存資料 → 提醒不會保留


def test_savings_items_add_up_to_baseline_minus_scenario():
    _, r = _plan("幫我部署一個報名網站，大概 500 人報名，三天後活動就結束")
    sv = r["savings"]
    assert sv["baseline_usd"] > sv["scenario_usd"]
    assert abs(sv["total_usd"] - (sv["baseline_usd"] - sv["scenario_usd"])) < 0.02
    assert abs(sum(i["usd"] for i in sv["items"]) - sv["total_usd"]) < 0.02
    assert "牌價" in sv["basis"]


def test_high_criticality_gets_minimum_replicas():
    attrs, r = _plan("部署一個社團用的投票網站，200個人會用，很重要不能掛")
    assert attrs["criticality"] == "high"
    assert r["replicas"] == HIGH_CRITICALITY_MIN


def test_concurrent_users_and_cap():
    attrs, r = _plan("同時在線大概 5000 人的 python 服務，要跑兩週", image="python:3.12")
    assert attrs["concurrent_users"] == 5000 and attrs["duration_days"] == 14
    assert r["replicas"] == MAX_SCENARIO_REPLICAS
    assert any("超過建議上限" in n for n in r["notes"])


def test_database_does_not_scale_out():
    _, r = _plan("部署 redis，大概 2000 人會用", image="redis")
    assert r["replicas"] == 1
    assert any("資料庫" in x for x in r["reasons"])


def test_explicit_replicas_win():
    _, r = _plan("部署 nginx 5 個副本，大概 80 人用", explicit=5)
    assert r["replicas"] == 5 and not r["replicas_applied"]
    assert any("照你指定的" in n for n in r["notes"])


def test_plain_request_has_no_scenario():
    assert not has_scenario(merge_model_attrs("部署 nginx 3 個副本", extract_by_rules("部署 nginx 3 個副本"), None))


def test_english_scenario():
    attrs, r = _plan("deploy nginx for a workshop with 80 people, only needed for 2 days")
    assert attrs["total_users"] == 80 and attrs["duration_days"] == 2
    assert r["savings"]["total_usd"] > 0


def test_model_numbers_must_appear_in_text():
    text = "老師要用來打分數的期末展示網站，下禮拜要能用"
    model = {"total_users": 300, "duration_days": 7, "criticality": "high", "needs_persistence": False}
    attrs = merge_model_attrs(text, extract_by_rules(text), model)
    assert attrs["total_users"] is None                   # 原句沒有 300，模型編的數字丟掉
    assert attrs["criticality"] == "high" and attrs["sources"]["criticality"] == "model"
    assert attrs["needs_persistence"] is False


def test_model_fills_number_that_rules_missed():
    text = "全班四十位一起用的展示網站"
    attrs = merge_model_attrs(text, {"total_users": None, "concurrent_users": None, "duration_days": None,
                                     "criticality": None, "needs_persistence": None},
                              {"total_users": 40})
    assert attrs["total_users"] == 40 and attrs["sources"]["total_users"] == "model"


def test_rules_win_over_model():
    text = "大概 500 人報名"
    attrs = merge_model_attrs(text, extract_by_rules(text), {"total_users": 5000})
    assert attrs["total_users"] == 500 and attrs["sources"]["total_users"] == "rule"


def test_people_count_is_not_replica_count():
    llama_client = pytest.importorskip("llama_client")
    assert llama_client._extract_pods_count("投票網站，200個人會用") is None
    assert llama_client._extract_pods_count("三個同學要用的網站") is None
    assert llama_client._extract_pods_count("部署 nginx 3 個副本") == 3
    assert llama_client._extract_pods_count("起兩個 redis") == 2


@pytest.mark.parametrize("text,expected", [
    ("部署一個社團用的投票網站，200個人會用", None),
    ("幫我部署一個 nginx 報名網站", None),
    ("部署 nginx 3 個副本", 3),
    ("replicas: 4", 4),
    ("副本數設 2", 2),
    ("起兩個 redis", 2),
    ("deploy 3 pods of nginx for 80 people", 3),
])
def test_explicit_replicas(text, expected):
    assert explicit_replicas(text) == expected


@pytest.mark.parametrize("evil", [
    "5" + " " * 5000 + "x", "1" * 5000 + "x", "1" + "," * 5000 + "x", "5 " * 5000,
    "一" * 5000 + "人", "5" + " 多 " * 2000 + "人", "9" * 400 + " 天",
])
def test_rules_are_fast_on_hostile_input(evil):
    """2026-10-09 安全審查：原本的數字規則遇到 500 個空白要回溯 113 秒，長數字還會報錯。"""
    import time
    t = time.perf_counter()
    extract_by_rules(evil)
    explicit_replicas(evil)
    merge_model_attrs(evil, extract_by_rules(evil), {"total_users": 10 ** 30, "duration_days": 1e308})
    assert time.perf_counter() - t < 0.5
