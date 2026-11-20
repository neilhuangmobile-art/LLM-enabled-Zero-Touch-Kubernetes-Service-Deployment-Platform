"""選軟體改成「逐一判斷」（2026-11-18，PIPELINE_SELECT_MODE=choose）：用假的候選與假模型測規則，不需要模型伺服器。"""
import json

import pytest

from agents.pipeline import gap_check, schema, software_rag
from agents.pipeline.prompts import CHOOSE_SYSTEM, FIT_SYSTEM

CANDS = ["wordpress", "ghost", "nginx"]


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("PIPELINE_SELECT_MODE", "choose")
    monkeypatch.setenv("PIPELINE_CHOOSE_STYLE", "pointwise")
    monkeypatch.setattr(software_rag, "candidates", lambda text, k=4: [{"image": c, "desc": f"{c} desc", "score": 0.5} for c in CANDS[:k]])
    monkeypatch.setattr(software_rag, "items", lambda: [{"image": c, "desc": f"{c} desc"} for c in CANDS + ["gitea/gitea"]])
    # choose_desc() 會用真的軟體目錄蓋掉說明：選軟體的測試只看假資料，避免測試結果跟著目錄內容變
    monkeypatch.setattr(software_rag, "_field_cached", lambda: [])


def _spec(image=None, source=None):
    s = schema.new_spec("系學會要架個網站放公告")
    if image:
        s["base"]["image"] = {"value": image, "source": source, "evidence": None}
    return s


def _fit_llm(yes):
    """FIT_SYSTEM：候選在 yes 裡回 fits=true；記錄問過哪些。"""
    asked = []

    def llm(system, user, max_new_tokens=768):
        assert system == FIT_SYSTEM
        img = user.split("Software: ", 1)[1].split(":", 1)[0]
        asked.append(img)
        return json.dumps({"fits": img in yes})
    llm.asked = asked
    return llm


def test_pointwise_takes_first_fit_in_rag_order(env):
    s = _spec("gitea/gitea", "inferred")
    llm = _fit_llm({"ghost", "nginx"})
    gap_check._choose_software(s, llm)
    assert schema.value(s, "image") == "ghost"
    assert llm.asked == ["wordpress", "ghost"]          # 找到第一個就停，不多問


def test_pointwise_none_fits_asks_user_with_candidates(env):
    s = _spec("gitea/gitea", "inferred")
    gap_check._choose_software(s, _fit_llm(set()))
    assert schema.value(s, "image") is None
    q = [q for q in s["questions"] if q["field"] == "image"]
    assert q and {o["value"] for o in q[0]["options"]} >= {"wordpress", "ghost"}


def test_reasoner_image_question_removed_when_choice_found(env):
    s = _spec()
    schema.add_question(s, "image", "要用什麼軟體？")
    gap_check._choose_software(s, _fit_llm({"wordpress"}))
    assert schema.value(s, "image") == "wordpress"
    assert not [q for q in s["questions"] if q["field"] == "image"]


def test_user_image_and_cron_runtime_not_touched(env):
    def boom(*a, **k):
        raise AssertionError("不該呼叫模型")
    s = _spec("nginx", "user")
    gap_check._choose_software(s, boom)
    assert schema.value(s, "image") == "nginx"
    s = _spec("python:3.12-slim", "inferred")          # 定時執行：真的是要跑一段程式
    s["workload"] = {"value": "cronjob", "source": "inferred"}
    gap_check._choose_software(s, boom)
    assert schema.value(s, "image") == "python:3.12-slim"


def test_runtime_service_switches_only_when_software_confident(conf):
    """推理代理選 node（其實要的是現成軟體）：有軟體過門檻就換；沒有就維持 node，不反問（11-19）。"""
    conf.update({"ghost": 0.95})
    s = _spec("node:18-alpine", "inferred")
    gap_check._choose_software(s, _never)
    assert schema.value(s, "image") == "ghost"
    conf.clear()
    conf.update({"ghost": 0.3})
    s = _spec("node:18-alpine", "inferred")
    gap_check._choose_software(s, _never)
    assert schema.value(s, "image") == "node:18-alpine" and not s["questions"]


def test_near_tie_prefers_rag_rank(conf):
    conf.update({"wordpress": 0.975, "ghost": 0.98})    # 差 0.005 ≤ 0.01：取 RAG 名次前面的 wordpress
    s = _spec("gitea/gitea", "inferred")
    gap_check._choose_software(s, _never)
    assert schema.value(s, "image") == "wordpress"

def test_unparseable_output_keeps_current_choice(env):
    s = _spec("gitea/gitea", "inferred")
    gap_check._choose_software(s, lambda *a, **k: "我覺得可以")
    assert schema.value(s, "image") == "gitea/gitea"     # 看不懂不等於「不符合」：不因模型輸出壞掉就清掉選擇


def test_model_down_keeps_current_choice(env):
    s = _spec("wordpress", "inferred")
    gap_check._choose_software(s, None)
    assert schema.value(s, "image") == "wordpress"


def test_list_style_disagreement_asks(env, monkeypatch):
    monkeypatch.setenv("PIPELINE_CHOOSE_STYLE", "list")
    calls = iter(['{"choice": 1}', '{"choice": 1}'])     # 正序選 wordpress、反序選第 1 個＝gitea/gitea → 不一致
    s = _spec("gitea/gitea", "inferred")

    def llm(system, user, max_new_tokens=768):
        assert system == CHOOSE_SYSTEM
        return next(calls)
    gap_check._choose_software(s, llm)
    assert schema.value(s, "image") is None
    q = [q for q in s["questions"] if q["field"] == "image"][0]
    assert q["recommended"] == "wordpress"


def test_select_mode_default_is_old_behaviour(monkeypatch):
    monkeypatch.delenv("PIPELINE_SELECT_MODE", raising=False)
    assert gap_check.select_mode() == "select"


# ── 信心度模式（預設）：每個候選算 P(Yes)，最高分過門檻才採用，否則反問 ─────────────
from agents.pipeline import llm as pllm  # noqa: E402


@pytest.fixture
def conf(env, monkeypatch):
    monkeypatch.setenv("PIPELINE_CHOOSE_STYLE", "confidence")
    monkeypatch.setenv("PIPELINE_CHOOSE_THRESHOLD", "0.8")
    monkeypatch.delenv("PIPELINE_CHOOSE_MARGIN", raising=False)
    from agents.pipeline import calibration
    monkeypatch.setattr(calibration, "_table", {})          # 用內建分組，不受 calibration.json 重算影響
    probs = {}
    monkeypatch.setattr(pllm, "local_yes_prob", lambda system, user: probs.get(user.split("Software: ", 1)[1].split(":", 1)[0], 0.0))
    return probs


def _never(*a, **k):
    raise AssertionError("信心度模式不該呼叫一般生成")


def test_confidence_above_threshold_fills(conf):
    conf.update({"wordpress": 0.6, "ghost": 0.93, "nginx": 0.2})
    s = _spec("gitea/gitea", "inferred")
    gap_check._choose_software(s, _never)
    assert schema.value(s, "image") == "ghost"
    assert dict(s["choose_scores"])["ghost"] == 0.93


def test_confidence_below_threshold_asks_with_best_as_recommended(conf):
    conf.update({"wordpress": 0.7, "ghost": 0.5})
    s = _spec("gitea/gitea", "inferred")
    gap_check._choose_software(s, _never)
    assert schema.value(s, "image") is None
    q = [q for q in s["questions"] if q["field"] == "image"][0]
    assert q["recommended"] == "wordpress"
    assert "低於門檻" in s["trace"][-1]["detail"]


def test_confidence_margin_between_different_kinds_asks(conf, monkeypatch):
    monkeypatch.setenv("PIPELINE_CHOOSE_MARGIN", "0.1")
    monkeypatch.setattr(software_rag, "items", lambda: [{"image": "wordpress", "desc": "部落格：x"},
                                                       {"image": "ghost", "desc": "部落格：y"},
                                                       {"image": "nginx", "desc": "靜態網站：z"}])
    conf.update({"wordpress": 0.95, "ghost": 0.94, "nginx": 0.9})
    s = _spec("gitea/gitea", "inferred")
    gap_check._choose_software(s, _never)
    assert schema.value(s, "image") is None              # 部落格 vs 靜態網站，差 0.05 < 0.1
    conf.update({"nginx": 0.5})
    s = _spec("gitea/gitea", "inferred")
    gap_check._choose_software(s, _never)
    assert schema.value(s, "image") == "wordpress"        # 同類（ghost）接近不算歧義


def test_confidence_unavailable_falls_back_to_pointwise(env, monkeypatch):
    monkeypatch.setenv("PIPELINE_CHOOSE_STYLE", "confidence")
    monkeypatch.setattr(pllm, "local_yes_prob", lambda system, user: None)
    s = _spec("gitea/gitea", "inferred")
    gap_check._choose_software(s, _fit_llm({"ghost"}))
    assert schema.value(s, "image") == "ghost"


def test_yes_prob_parses_logprobs(monkeypatch):
    import math
    import types
    monkeypatch.setattr(pllm, "ENGINE", "llamacpp")
    body = {"choices": [{"logprobs": {"content": [{"top_logprobs": [
        {"token": "Yes", "logprob": math.log(0.6)}, {"token": "No", "logprob": math.log(0.2)},
        {"token": "yes", "logprob": math.log(0.1)}]}]}}]}
    import requests
    monkeypatch.setattr(requests, "post", lambda *a, **k: types.SimpleNamespace(status_code=200, json=lambda: body))
    assert abs(pllm.local_yes_prob("s", "u") - 0.7 / 0.9) < 1e-6
    monkeypatch.setattr(requests, "post", lambda *a, **k: types.SimpleNamespace(status_code=500, text="x"))
    assert pllm.local_yes_prob("s", "u") is None
    monkeypatch.setattr(pllm, "ENGINE", "transformers")
    assert pllm.local_yes_prob("s", "u") is None


# ── 可靠度顯示（2026-11-18）：卡片顯示校準後的實測正確率，不是原始機率 ─────────────
from agents.pipeline import calibration as _cal_mod  # noqa: E402
from agents.pipeline.calibration import BINS, reliability  # noqa: E402


def test_reliability_bins(monkeypatch):
    monkeypatch.setattr(_cal_mod, "_table", {})
    assert reliability(None) is None
    assert reliability(0.99)["level"] == "高"
    assert reliability(0.96)["level"] == "中"
    assert reliability(0.85)["level"] == "低"
    assert reliability(0.3) is None                        # 低於 0.8 沒有實測，不給數字
    assert all(0 < acc <= 1 and n > 0 for _, acc, n in BINS)
    assert reliability(0.96)["accuracy"] < 0.96            # 顯示的是實測正確率，不是模型原始機率


def test_question_options_carry_reliability(conf):
    conf.update({"wordpress": 0.7, "ghost": 0.96, "nginx": 0.2})
    s = _spec("gitea/gitea", "inferred")
    import os
    os.environ["PIPELINE_CHOOSE_THRESHOLD"] = "0.99"
    try:
        gap_check._choose_software(s, _never)
    finally:
        os.environ["PIPELINE_CHOOSE_THRESHOLD"] = "0.8"
    q = [q for q in s["questions"] if q["field"] == "image"][0]
    assert [o["value"] for o in q["options"]][:3] == ["ghost", "wordpress", "nginx"]
    assert "信心度 67%" in q["options"][0]["label"] and q["options"][0]["confidence"]["level"] == "中"


from agents.pipeline import calibration as _cal  # noqa: E402


def test_field_confidence_user_is_100_and_unknown_is_none(monkeypatch):
    monkeypatch.setattr(_cal, "_table", {"fields": {"port": {"default": [0.9, 50]}}})
    assert _cal.field_confidence("cpu", "user")["pct"] == 100
    assert _cal.field_confidence("port", "default") == {"pct": 90, "level": "hi", "n": 50, "source": _cal.SOURCE}
    assert _cal.field_confidence("memory", "inferred") is None        # 沒統計過的不編數字
    assert _cal.field_confidence("image", "inferred", 0.96)["level"] == "mid"
    assert [_cal.level(x) for x in (85, 84, 65, 64)] == ["hi", "mid", "mid", "lo"]


def test_system_filled_never_shows_100(monkeypatch):
    monkeypatch.setattr(_cal, "_table", {"fields": {"cpu": {"inferred": [1.0, 197]}}})
    assert _cal.field_confidence("cpu", "inferred")["pct"] == 95
    assert _cal.field_confidence("cpu", "user")["pct"] == 100


def test_switching_software_clears_inferred_port_but_keeps_user_port(conf):
    conf.update({"wordpress": 0.2, "ghost": 0.99})
    s = _spec("gitea/gitea", "inferred")
    s["base"]["port"] = {"value": 3000, "source": "inferred", "evidence": None}
    gap_check._choose_software(s, _never)
    assert schema.value(s, "image") == "ghost" and schema.value(s, "port") is None
    s = _spec("gitea/gitea", "inferred")
    s["base"]["port"] = {"value": 8080, "source": "user", "evidence": "8080"}
    gap_check._choose_software(s, _never)
    assert schema.value(s, "port") == 8080


def test_flarum_port_known():
    from agents.pipeline import defaults
    assert defaults.listen_port("mondedie/flarum") == 8888


# ── code review（2026-11-19）修正的回歸測試 ─────────────────────────────────
def test_database_kept_when_its_own_score_passes(conf, monkeypatch):
    monkeypatch.setattr(software_rag, "candidates", lambda text, k=4: [{"image": c, "desc": "", "score": 0.5}
                                                                       for c in ["mysql", "mongo", "postgres"]])
    conf.update({"mysql": 0.99, "mongo": 0.6, "postgres": 0.9})
    s = _spec("postgres:16", "inferred")
    gap_check._choose_software(s, _never)
    assert schema.value(s, "image") == "postgres:16"        # 推理代理選的資料庫本身過門檻，不換成 mysql


def test_stale_scores_cleared_and_same_text_not_rescored(conf):
    conf.update({"ghost": 0.99})
    s = _spec("gitea/gitea", "inferred")
    s["choose_reason"] = "舊的理由"
    gap_check._choose_software(s, _never)
    assert "choose_reason" not in s and schema.value(s, "image") == "ghost"
    conf.clear()                                             # 再跑一次：同一句、同一個選擇 → 不重算
    gap_check._choose_software(s, _never)
    assert schema.value(s, "image") == "ghost"


def test_switch_resets_inferred_app_name(conf):
    conf.update({"ghost": 0.99})
    s = _spec("gitea/gitea", "inferred")
    s["base"]["app_name"] = {"value": "gitea-repo", "source": "inferred", "evidence": None}
    gap_check._choose_software(s, _never)
    assert schema.value(s, "app_name") is None


def test_pointwise_all_failures_keep_current_choice(env):
    s = _spec("wordpress", "inferred")
    gap_check._choose_software(s, lambda *a, **k: None)      # 模型完全沒回應
    assert schema.value(s, "image") == "wordpress"
    assert "沒有給出判斷" in s["trace"][-1]["detail"]


def test_low_scores_get_no_number_in_options(conf, monkeypatch):
    monkeypatch.setenv("PIPELINE_CHOOSE_THRESHOLD", "0.99")
    conf.update({"wordpress": 0.9, "ghost": 0.3, "nginx": 0.001})
    s = _spec("gitea/gitea", "inferred")
    gap_check._choose_software(s, _never)
    opts = {o["value"]: o for o in [q for q in s["questions"] if q["field"] == "image"][0]["options"]}
    assert opts["wordpress"]["confidence"] is not None
    assert opts["ghost"]["confidence"] is None and "信心度" not in opts["ghost"]["label"]


# ── silent-failure 審查（2026-11-19）修正的回歸測試 ──────────────────────────
from agents.pipeline import runner as _runner  # noqa: E402


def test_auto_answer_is_not_user_source(conf, monkeypatch):
    """按「幫我決定」採用的軟體，來源是 inferred（不顯示 100%），而且不會再重算一次。"""
    monkeypatch.setattr(_runner, "_finish", lambda spec, local, t0: None)
    s = _spec("gitea/gitea", "inferred")
    schema.add_question(s, "image", "要用哪個軟體？", [{"value": "ghost", "label": "ghost"}], recommended="ghost")
    out = _runner.resume(s, {s["questions"][0]["id"]: "__auto__"}, _never)
    assert schema.value(out, "image") == "ghost" and out["base"]["image"]["source"] == "inferred"
    assert out["choose_pick"] == "ghost"
    out2 = _runner.resume(s, {s["questions"][0]["id"]: "ghost"}, _never)
    assert out2["base"]["image"]["source"] == "user"


def test_yes_prob_rejects_low_probability_mass(monkeypatch):
    import math
    import types
    import requests
    monkeypatch.setattr(pllm, "ENGINE", "llamacpp")
    body = {"choices": [{"logprobs": {"content": [{"top_logprobs": [
        {"token": "{", "logprob": math.log(0.9)}, {"token": "Yes", "logprob": math.log(0.004)},
        {"token": "No", "logprob": math.log(0.0004)}]}]}}]}
    monkeypatch.setattr(requests, "post", lambda *a, **k: types.SimpleNamespace(status_code=200, json=lambda: body))
    assert pllm.local_yes_prob("s", "u") is None             # 0.91 的相對比例沒有意義


def test_confidence_failure_falls_back_with_notice(env, monkeypatch):
    monkeypatch.setenv("PIPELINE_CHOOSE_STYLE", "confidence")
    monkeypatch.setattr(pllm, "local_yes_prob", lambda system, user: None)
    s = _spec("gitea/gitea", "inferred")
    gap_check._choose_software(s, _fit_llm({"ghost"}))
    assert schema.value(s, "image") == "ghost"
    assert any("信心度" in n for n in s["notices"])
    assert any("改用逐一判斷" in t["detail"] for t in s["trace"]) and "pointwise" in s["trace"][-1]["detail"]


def test_bad_threshold_env_warns_and_defaults(monkeypatch, capsys):
    monkeypatch.setenv("PIPELINE_CHOOSE_THRESHOLD", "0,8")
    gap_check._warned.clear()
    assert gap_check.choose_threshold() == 0.8
    assert "PIPELINE_CHOOSE_THRESHOLD" in capsys.readouterr().out


def test_calibration_bad_file_disabled_not_crash(monkeypatch, tmp_path, capsys):
    bad = tmp_path / "c.json"
    bad.write_text('{"fields": {"cpu": {"inferred": [0.9]}}}', encoding="utf-8")
    monkeypatch.setattr(_cal, "_PATH", str(bad))
    monkeypatch.setattr(_cal, "_table", None)
    assert _cal.field_confidence("cpu", "inferred") is None
    assert "整份停用" in capsys.readouterr().out
    monkeypatch.setattr(_cal, "_PATH", str(tmp_path / "missing.json"))
    monkeypatch.setattr(_cal, "_table", None)
    assert _cal.image_confidence(0.99)["source"] == _cal.SOURCE   # 用內建分組，出處也標內建的
    assert "找不到" in capsys.readouterr().out


def test_choose_desc_prefers_knowledge_scenarios(monkeypatch):
    """選軟體給模型看的說明：同一個映像檔有知識庫中文情境時優先用（2026-11-19，nginx 原本只有「Official build of Nginx.」）。"""
    from agents.pipeline import software_rag
    monkeypatch.delenv("PIPELINE_CHOOSE_DESC", raising=False)
    d = software_rag.choose_desc()
    assert "靜態" in d["nginx"]
    assert set(d) == {it["image"] for it in software_rag.items()}    # 候選範圍不變，只換說明文字
    monkeypatch.setenv("PIPELINE_CHOOSE_DESC", "legacy")
    legacy = software_rag.choose_desc()
    assert legacy["nginx"] == next(it["desc"] for it in software_rag.items() if it["image"] == "nginx")


def _rep_spec(text, image, replicas, source="inferred"):
    s = schema.new_spec(text)
    s["base"]["image"] = {"value": image, "source": "inferred", "evidence": None}
    s["base"]["replicas"] = {"value": replicas, "source": source, "evidence": None}
    return s


def test_stateful_replicas_set_to_one(monkeypatch):
    """會自己保存資料的軟體，系統推的副本數改成 1（2026-11-19），並在部署單提醒。"""
    monkeypatch.delenv("PIPELINE_STATEFUL_REPLICAS", raising=False)
    s = _rep_spec("家裡民宿網站常常掛掉，想要有東西一直顧著，掛掉就通知我", "louislam/uptime-kuma", 2)
    gap_check._stateful_replicas(s)
    assert s["base"]["replicas"]["value"] == 1
    assert any("副本數" in n for n in s["notices"])


@pytest.mark.parametrize("text,image,replicas,source", [
    ("給全校快兩千人同時用的快取", "redis", 2, "inferred"),          # 上千人同時使用
    ("會員資料庫就算壞一台也要能繼續用", "postgres", 2, "inferred"),   # 要求一台壞了也能用
    ("我要開兩份 redis", "redis", 2, "user"),                         # 使用者講的
    ("公司官網", "nginx", 3, "inferred"),                              # 不是會存資料的軟體
])
def test_stateful_replicas_exceptions(monkeypatch, text, image, replicas, source):
    monkeypatch.delenv("PIPELINE_STATEFUL_REPLICAS", raising=False)
    s = _rep_spec(text, image, replicas, source)
    gap_check._stateful_replicas(s)
    assert s["base"]["replicas"]["value"] == replicas


def test_stateful_replicas_switch_off(monkeypatch):
    monkeypatch.setenv("PIPELINE_STATEFUL_REPLICAS", "0")
    s = _rep_spec("密碼保險箱，十五個人用", "vaultwarden/server", 2)
    gap_check._stateful_replicas(s)
    assert s["base"]["replicas"]["value"] == 2


def _mem_spec(image, memory, source="inferred"):
    s = schema.new_spec("測試")
    s["base"]["image"] = {"value": image, "source": "inferred", "evidence": None}
    s["base"]["memory"] = {"value": memory, "source": source, "evidence": None}
    return s


def test_memory_floor_raises_to_recommended(monkeypatch):
    """系統推的記憶體低於知識庫建議值時拉到建議值（2026-11-19），來源維持原本的。"""
    monkeypatch.delenv("PIPELINE_MEMORY_FLOOR", raising=False)
    rec = software_rag.recommended_memory("rabbitmq:3-management")
    assert gap_check._mi(rec) > 128
    s = _mem_spec("rabbitmq:3-management", "128Mi")
    gap_check._memory_floor(s)
    assert s["base"]["memory"]["value"] == rec
    assert s["base"]["memory"]["source"] == "inferred"


@pytest.mark.parametrize("image,memory,source", [
    ("rabbitmq:3-management", "128Mi", "user"),   # 使用者講的不動
    ("rabbitmq:3-management", "4Gi", "inferred"),  # 推得比建議多，不往下調
    ("not-in-catalog/xyz", "64Mi", "inferred"),    # 不在知識庫
    ("rabbitmq:3-management", "很多", "inferred"),   # 看不懂的值
])
def test_memory_floor_leaves_alone(monkeypatch, image, memory, source):
    monkeypatch.delenv("PIPELINE_MEMORY_FLOOR", raising=False)
    s = _mem_spec(image, memory, source)
    gap_check._memory_floor(s)
    assert s["base"]["memory"]["value"] == memory


def test_memory_floor_switch_off(monkeypatch):
    monkeypatch.setenv("PIPELINE_MEMORY_FLOOR", "0")
    s = _mem_spec("rabbitmq:3-management", "128Mi")
    gap_check._memory_floor(s)
    assert s["base"]["memory"]["value"] == "128Mi"


def test_mi_parsing():
    assert gap_check._mi("1Gi") == 1024
    assert gap_check._mi("512Mi") == 512
    assert gap_check._mi("") == -1 and gap_check._mi("abc") == -1 and gap_check._mi("xMi") == -1


def test_tie_does_not_pick_below_threshold(conf):
    """最高分過門檻時，同分規則不能換成門檻以下、RAG 名次較前的軟體（2026-11-19 code review）。"""
    conf.update({"wordpress": 0.795, "ghost": 0.805})
    s = _spec("gitea/gitea", "inferred")
    gap_check._choose_software(s, _never)
    assert schema.value(s, "image") == "ghost"


def test_pointwise_no_answer_retries_next_round(env):
    """逐一判斷時模型都沒回應：維持原本選擇，但下一輪不能被「同一句話」的快取擋住。"""
    s = _spec("gitea/gitea", "inferred")
    gap_check._choose_software(s, lambda *a, **k: None)
    assert schema.value(s, "image") == "gitea/gitea"
    assert "choose_text" not in s
    gap_check._choose_software(s, _fit_llm({"ghost"}))
    assert schema.value(s, "image") == "ghost"


@pytest.mark.parametrize("text,many,ha", [
    ("千萬不要讓資料不見的 redis", False, False),
    ("萬一壞掉怎麼辦", False, False),
    ("redis 開在 port 6379，給 512Mi", False, False),
    ("網站壞掉就通知我", False, False),           # 監控需求，不是高可用
    ("全校快兩千人同時用", True, False),
    ("一百多個修課學生會用", True, False),
    ("300 users at the same time", True, False),
    ("壞一台也要能繼續用", False, True),
    ("不能隨便掛", False, True),
    ("needs high availability", False, True),
    ("we need HA", False, True),
    ("HAPPY hour 的報名表", False, False),
])
def test_stateful_exception_patterns(text, many, ha):
    assert bool(gap_check._MANY_USERS.search(text)) is many
    assert bool(gap_check._WANTS_HA.search(text)) is ha


def test_stateful_exception_leaves_trace(monkeypatch):
    monkeypatch.delenv("PIPELINE_STATEFUL_REPLICAS", raising=False)
    s = _rep_spec("會員資料庫就算壞一台也要能繼續用", "postgres", 2)
    gap_check._stateful_replicas(s)
    assert s["base"]["replicas"]["value"] == 2
    assert any("副本數維持 2" in t["detail"] for t in s["trace"])


def test_switch_values(monkeypatch):
    for v in ("0", "false", "OFF", "no"):
        monkeypatch.setenv("PIPELINE_MEMORY_FLOOR", v)
        assert gap_check._switch_on("PIPELINE_MEMORY_FLOOR") is False
    for v in ("1", "true", "on"):
        monkeypatch.setenv("PIPELINE_MEMORY_FLOOR", v)
        assert gap_check._switch_on("PIPELINE_MEMORY_FLOOR") is True


def test_memory_floor_adds_notice(monkeypatch):
    monkeypatch.delenv("PIPELINE_MEMORY_FLOOR", raising=False)
    s = _mem_spec("rabbitmq:3-management", "128Mi")
    gap_check._memory_floor(s)
    assert any("記憶體" in n and "128Mi" in n for n in s["notices"])


def test_adjust_resources_idempotent_and_after_verify(monkeypatch):
    """verify 之後再跑一次：已修正過的不再變、不重複加提醒。"""
    for k in ("PIPELINE_MEMORY_FLOOR", "PIPELINE_STATEFUL_REPLICAS"):
        monkeypatch.delenv(k, raising=False)
    s = _rep_spec("家裡民宿網站常常掛掉，想要有東西一直顧著", "louislam/uptime-kuma", 2)
    s["base"]["memory"] = {"value": "16Mi", "source": "inferred", "evidence": None}
    gap_check.adjust_resources(s)
    snap = (s["base"]["replicas"]["value"], s["base"]["memory"]["value"], len(s["notices"]))
    s["base"]["replicas"]["value"] = 2            # 假裝 verify 改回 2
    gap_check.adjust_resources(s)
    assert s["base"]["replicas"]["value"] == 1
    gap_check.adjust_resources(s)
    assert (s["base"]["replicas"]["value"], s["base"]["memory"]["value"]) == snap[:2]


def test_catalog_failure_not_cached(monkeypatch):
    """軟體目錄讀取失敗不能被永久快取；目錄讀不到時 adjust_resources 留下 trace、不改值。"""
    monkeypatch.setattr(software_rag, "_field_cache", None)
    monkeypatch.setattr(software_rag, "_field_items", lambda: [])
    assert software_rag.field_catalog_ok() is False
    assert software_rag._field_cache is None
    s = _rep_spec("密碼保險箱", "vaultwarden/server", 2)
    gap_check.adjust_resources(s)
    assert s["base"]["replicas"]["value"] == 2
    assert any("軟體目錄讀取失敗" in t["detail"] for t in s["trace"])


def test_catalog_consistency():
    """STATEFUL_CATEGORIES 每一類都真的存在於軟體目錄（目錄改類別名稱時規則不會悄悄失效）；建議記憶體格式都看得懂。"""
    from agents.pipeline import defaults
    items = software_rag._field_items()
    cats = {it["category"] for it in items}
    assert defaults.STATEFUL_CATEGORIES <= cats, defaults.STATEFUL_CATEGORIES - cats
    for it in items:
        if it.get("memory"):
            assert gap_check._mi(it["memory"]) > 0, it


def test_memory_floor_skips_runtime_images(monkeypatch):
    """python、node 跑的是使用者自己的程式，不套軟體目錄的下限。"""
    monkeypatch.delenv("PIPELINE_MEMORY_FLOOR", raising=False)
    s = _mem_spec("python:3.12-slim", "64Mi")
    gap_check._memory_floor(s)
    assert s["base"]["memory"]["value"] == "64Mi"
