"""反問改善（2026-11-22）：PIPELINE_CLARITY（軟體講清楚了沒）與定時指令的範例指令。假模型，不碰網路與 GPU。"""
import json

import pytest

from agents.pipeline import gap_check, normalize, reasoner, schema, software_rag
from agents.pipeline import llm as pllm
from agents.pipeline.prompts import CLARITY_ADDON, CLARITY_PROB_SYSTEM, NORMALIZE_SYSTEM

CANDS = ["wordpress", "ghost", "nginx"]


@pytest.fixture
def conf(monkeypatch):
    """選軟體信心度模式；probs＝每個候選的信心度，clar["p"]＝本地判斷「講清楚」的機率（None＝拿不到）。"""
    monkeypatch.setenv("PIPELINE_SELECT_MODE", "choose")
    monkeypatch.setenv("PIPELINE_CHOOSE_STYLE", "confidence")
    monkeypatch.setenv("PIPELINE_CHOOSE_THRESHOLD", "0.8")
    for k in ("PIPELINE_CHOOSE_MARGIN", "PIPELINE_CLARITY", "PIPELINE_CLARITY_LO", "PIPELINE_CLARITY_HI", "PIPELINE_TRUST_SUGGEST", "PIPELINE_CLARITY_COMBINE",
              "PIPELINE_CLARITY_CHECK"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(software_rag, "candidates", lambda text, k=4: [{"image": c, "desc": f"{c} desc", "score": 0.5} for c in CANDS[:k]])
    monkeypatch.setattr(software_rag, "items", lambda: [{"image": c, "desc": f"{c} desc"} for c in CANDS + ["gitea/gitea"]])
    monkeypatch.setattr(software_rag, "_field_cached", lambda: [])
    from agents.pipeline import calibration
    monkeypatch.setattr(calibration, "_table", {})
    probs, clar = {}, {"p": None, "calls": 0}

    def yes_prob(system, user):
        if system == CLARITY_PROB_SYSTEM:
            clar["calls"] += 1
            return clar["p"]
        return probs.get(user.split("Software: ", 1)[1].split(":", 1)[0], 0.0)
    monkeypatch.setattr(pllm, "local_yes_prob", yes_prob)
    return probs, clar


def _never(*a, **k):
    raise AssertionError("不該呼叫一般生成")


def _spec(image=None, source=None, clarity=None):
    s = schema.new_spec("系學會要架個網站放公告")
    if image:
        s["base"]["image"] = {"value": image, "source": source, "evidence": None}
    if clarity is not None:
        s["clarity"] = dict({"source": "gemini", "category": None, "suggest": None, "notes": []}, **clarity)
    return s


def _img_q(s):
    return [q for q in s["questions"] if q["field"] == "image"]


def test_off_is_unchanged_and_never_asks_local(conf):
    probs, clar = conf
    probs.update({"ghost": 0.95})
    s = _spec(clarity={"software_clear": False})        # 就算有判斷，關著也不看
    gap_check._choose_software(s, _never)
    assert schema.value(s, "image") == "ghost" and not _img_q(s) and clar["calls"] == 0


def test_unclear_asks_even_when_confident(conf, monkeypatch):
    monkeypatch.setenv("PIPELINE_CLARITY", "on")
    probs, _ = conf
    probs.update({"ghost": 0.97})
    s = _spec(clarity={"software_clear": False})
    gap_check._choose_software(s, _never)
    q = _img_q(s)
    assert schema.value(s, "image") is None and q and q[0]["recommended"] == "ghost"


def test_clear_but_low_confidence_uses_gemini_suggestion(conf, monkeypatch):
    monkeypatch.setenv("PIPELINE_CLARITY", "on")
    probs, _ = conf
    probs.update({"wordpress": 0.3, "ghost": 0.4})
    s = _spec(clarity={"software_clear": True, "suggest": "gitea/gitea:1.22"})    # 在知識表裡，雖然不在候選
    gap_check._choose_software(s, _never)
    assert schema.value(s, "image") == "gitea/gitea" and not _img_q(s)
    assert any("gitea/gitea" in n and "請確認" in n for n in s["notices"])


def test_clear_but_unknown_suggestion_uses_top_score(conf, monkeypatch):
    monkeypatch.setenv("PIPELINE_CLARITY", "on")
    probs, _ = conf
    probs.update({"wordpress": 0.3, "ghost": 0.4})
    for sug in ("jenkins", "python", None):             # 不在知識表／執行環境／沒給
        s = _spec(clarity={"software_clear": True, "suggest": sug})
        gap_check._choose_software(s, _never)
        assert schema.value(s, "image") == "ghost" and not _img_q(s), sug


def test_user_image_untouched(conf, monkeypatch):
    monkeypatch.setenv("PIPELINE_CLARITY", "on")
    s = _spec("nginx", "user", clarity={"software_clear": False})
    gap_check._choose_software(s, _never)
    assert schema.value(s, "image") == "nginx" and not _img_q(s)


def test_local_fallback_when_no_gemini_judgement(conf, monkeypatch):
    monkeypatch.setenv("PIPELINE_CLARITY", "on")
    monkeypatch.setenv("PIPELINE_CLARITY_LO", "0.1")
    monkeypatch.setenv("PIPELINE_CLARITY_HI", "0.9")
    probs, clar = conf
    probs.update({"ghost": 0.95})
    clar["p"] = 0.05                                     # 本地說沒講清楚 → 問
    s = _spec()
    gap_check._choose_software(s, _never)
    assert _img_q(s) and s["clarity"]["source"] == "local" and s["clarity"]["software_clear"] is False
    probs.clear()
    probs.update({"ghost": 0.4})
    clar["p"] = 0.95                                     # 本地說清楚 → 信心度低也不問
    s = _spec()
    gap_check._choose_software(s, _never)
    assert schema.value(s, "image") == "ghost" and not _img_q(s)
    clar["p"] = 0.5                                      # 中間 → 照原本（信心度低就問）
    s = _spec()
    gap_check._choose_software(s, _never)
    assert _img_q(s) and s["clarity"]["software_clear"] is None
    clar["p"] = None                                     # 拿不到機率 → 照原本
    s = _spec()
    gap_check._choose_software(s, _never)
    assert _img_q(s) and "clarity" not in s


def test_gemini_unclear_but_names_known_software_falls_back_to_local(conf, monkeypatch):
    """Gemini 說沒講清楚、卻說得出類別又建議知識表裡的軟體（前後矛盾）：改用本地判斷。"""
    monkeypatch.setenv("PIPELINE_CLARITY", "on")
    probs, clar = conf
    probs.update({"ghost": 0.95})
    clar["p"] = 0.9
    s = _spec(clarity={"software_clear": False, "category": "blog", "suggest": "ghost"})
    gap_check._choose_software(s, _never)
    assert schema.value(s, "image") == "ghost" and not _img_q(s)
    assert s["clarity"]["source"] == "local" and s["gemini_clarity"]["suggest"] == "ghost"
    s = _spec(clarity={"software_clear": False, "category": None, "suggest": None})     # 真的說不出來：照 Gemini 問
    gap_check._choose_software(s, _never)
    assert _img_q(s) and s["clarity"]["source"] == "gemini"


def test_trust_suggest_overrides_confident_choice(conf, monkeypatch):
    """PIPELINE_TRUST_SUGGEST=1：講清楚而且 Gemini 建議在知識表裡，就用建議，就算別的候選信心度很高。"""
    monkeypatch.setenv("PIPELINE_CLARITY", "on")
    probs, _ = conf
    probs.update({"wordpress": 0.97})
    s = _spec(clarity={"software_clear": True, "suggest": "ghost:5"})
    gap_check._choose_software(s, _never)
    assert schema.value(s, "image") == "wordpress"                 # 開關沒開：照信心度
    monkeypatch.setenv("PIPELINE_TRUST_SUGGEST", "1")
    s = _spec(clarity={"software_clear": True, "suggest": "ghost:5"})
    gap_check._choose_software(s, _never)
    assert schema.value(s, "image") == "ghost" and not _img_q(s)
    s = _spec("python:3.11-slim", "inferred", clarity={"software_clear": True, "suggest": "gitea/gitea"})
    gap_check._choose_software(s, _never)                           # 推理代理選執行環境、Gemini 建議現成軟體：換
    assert schema.value(s, "image") == "gitea/gitea"


def test_trust_suggest_ignores_unknown_runtime_user_and_unclear(conf, monkeypatch):
    monkeypatch.setenv("PIPELINE_CLARITY", "on")
    monkeypatch.setenv("PIPELINE_TRUST_SUGGEST", "1")
    probs, _ = conf
    probs.update({"wordpress": 0.97})
    for sug in ("jenkins", "python:3.12", None):                   # 不在知識表／執行環境／沒給：照信心度
        s = _spec(clarity={"software_clear": True, "suggest": sug})
        gap_check._choose_software(s, _never)
        assert schema.value(s, "image") == "wordpress", sug
    s = _spec("nginx", "user", clarity={"software_clear": True, "suggest": "ghost"})
    gap_check._choose_software(s, _never)
    assert schema.value(s, "image") == "nginx"                     # 使用者講的不動
    s = _spec(clarity={"software_clear": False, "suggest": None})
    gap_check._choose_software(s, _never)
    assert _img_q(s)                                                # 沒講清楚：照樣問


def _cmd_spec(workload="worker", suggest="gitea/gitea", clear=True, image="alpine"):
    """推理代理選了執行環境，Gemini 又把一段描述抽成「使用者給的指令」（2026-11-23 發現的漏洞）。"""
    s = _spec(image, "inferred", clarity={"software_clear": clear, "suggest": suggest})
    s["workload"]["value"] = workload
    s["base"]["port"] = {"value": 3000, "source": "inferred", "evidence": None}
    s["extras"].append({"kind": "command", "desc": "", "value": "一個小程式在背後默默跑", "evidence": "背後默默跑",
                        "source": "user", "status": "pending", "note": ""})
    return s


def test_described_command_no_longer_blocks_suggestion(conf, monkeypatch):
    """背景程式＋描述型「指令」：開關開、Gemini 講清楚、建議在知識表 → 換成建議的軟體，指令標成不處理（不塞給現成軟體）。"""
    monkeypatch.setenv("PIPELINE_CLARITY", "on")
    s = _cmd_spec()
    gap_check._choose_software(s, _never)
    assert schema.value(s, "image") == "alpine"                     # 開關沒開：跟原本一樣
    monkeypatch.setenv("PIPELINE_TRUST_SUGGEST", "1")
    s = _cmd_spec()
    gap_check._choose_software(s, _never)
    assert schema.value(s, "image") == "gitea/gitea" and s["base"]["image"]["source"] == "inferred"
    assert schema.value(s, "port") is None                          # 舊軟體的 port 清掉，讓查表重填
    cmd = [e for e in s["extras"] if e["kind"] == "command"][0]
    assert cmd["status"] == "unsupported" and "gitea/gitea" in cmd["note"]
    reasoner.apply_extras(s)
    assert "command" not in s["manifest"]                            # 描述沒有被當成指令


def test_described_command_keeps_runtime_when_not_safe(conf, monkeypatch):
    monkeypatch.setenv("PIPELINE_CLARITY", "on")
    monkeypatch.setenv("PIPELINE_TRUST_SUGGEST", "1")
    for kw in ({"workload": "cronjob"}, {"suggest": "python:3.12"}, {"suggest": "jenkins"}, {"clear": False}, {"suggest": None}):
        s = _cmd_spec(**kw)
        gap_check._choose_software(s, _never)
        assert schema.value(s, "image") == "alpine", kw              # 定時執行／建議是執行環境、不在表、沒講清楚、沒建議：維持
        assert [e for e in s["extras"] if e["kind"] == "command"][0]["status"] == "pending", kw
    s = _cmd_spec()
    s["clarity"]["source"] = "local"
    gap_check._choose_software(s, _never)
    assert schema.value(s, "image") == "alpine"                     # 不是 Gemini 的判斷：維持


def test_combine_or_asks_when_local_is_sure_unclear(conf, monkeypatch):
    """PIPELINE_CLARITY_COMBINE=or：Gemini 說清楚，但本地很確定沒講清楚（≤ 下門檻）→ 照樣問；沒設就照 Gemini。"""
    monkeypatch.setenv("PIPELINE_CLARITY", "on")
    monkeypatch.setenv("PIPELINE_TRUST_SUGGEST", "1")
    monkeypatch.setenv("PIPELINE_CLARITY_LO", "0.1")
    probs, clar = conf
    probs.update({"ghost": 0.95})
    clar["p"] = 0.05
    s = _spec(clarity={"software_clear": True, "suggest": "ghost"})
    gap_check._choose_software(s, _never)
    assert schema.value(s, "image") == "ghost" and clar["calls"] == 0          # 沒設：不問本地
    monkeypatch.setenv("PIPELINE_CLARITY_COMBINE", "or")
    s = _spec(clarity={"software_clear": True, "suggest": "ghost"})
    gap_check._choose_software(s, _never)
    assert _img_q(s) and s["clarity"]["local_p"] == 0.05
    clar["p"] = 0.5                                                             # 本地不確定：照 Gemini
    s = _spec(clarity={"software_clear": True, "suggest": "ghost"})
    gap_check._choose_software(s, _never)
    assert schema.value(s, "image") == "ghost" and not _img_q(s)


def test_kind_clear_field_name(monkeypatch):
    monkeypatch.setenv("PIPELINE_CLARITY", "on")
    s = schema.new_spec("架個部落格")
    normalize.normalize(s, _gemini({"category": "blog", "suggest": "ghost", "kind_clear": False, "notes": []}, []), None)
    assert s["clarity"]["software_clear"] is False


def test_log_mode_records_but_does_not_change(conf, monkeypatch):
    monkeypatch.setenv("PIPELINE_CLARITY", "log")
    probs, clar = conf
    probs.update({"ghost": 0.95})
    clar["p"] = 0.01
    s = _spec()
    gap_check._choose_software(s, _never)
    assert schema.value(s, "image") == "ghost" and not _img_q(s) and s["clarity"]["software_clear"] is False


def test_runtime_with_unclear_program_asks(conf, monkeypatch):
    """推理代理選了 python（使用者自己的程式），但沒講是什麼程式：原本維持 python 不問，沒講清楚時要問。"""
    monkeypatch.setenv("PIPELINE_CLARITY", "on")
    s = _spec("python:3.11-slim", "inferred", clarity={"software_clear": False})
    gap_check._choose_software(s, _never)
    assert _img_q(s)
    s = _spec("python:3.11-slim", "inferred", clarity={"software_clear": True})
    gap_check._choose_software(s, _never)
    assert schema.value(s, "image") == "python:3.11-slim" and not _img_q(s)


def _gemini(clarity, seen):
    def g(system, user):
        seen.append(system)
        out = {"purpose": "x", "base": {f: None for f in schema.BASE_FIELDS}, "extras": []}
        if clarity is not None:
            out["clarity"] = clarity
        return json.dumps(out)
    return g


def test_normalize_adds_addon_only_for_gemini_when_on(monkeypatch):
    seen = []
    monkeypatch.delenv("PIPELINE_CLARITY", raising=False)
    normalize.normalize(schema.new_spec("架個部落格"), _gemini({"software_clear": True}, seen), None)
    assert seen[-1] == NORMALIZE_SYSTEM                  # 關著：prompt 一字不差
    monkeypatch.setenv("PIPELINE_CLARITY", "on")
    s = schema.new_spec("架個部落格，不對外又要吸引粉絲")
    normalize.normalize(s, _gemini({"software_clear": True, "category": "blog", "suggest": "ghost",
                                    "notes": ["不對外和吸引粉絲互相矛盾"]}, seen), None)
    assert seen[-1] == NORMALIZE_SYSTEM + CLARITY_ADDON
    assert s["clarity"]["software_clear"] is True and s["clarity"]["suggest"] == "ghost"
    assert any("互相矛盾" in n for n in s["notices"])
    local_seen = []
    normalize.normalize(schema.new_spec("架個部落格"), None, _gemini(None, local_seen))
    assert local_seen[-1] == NORMALIZE_SYSTEM            # 本地正規化照舊


def test_bad_gemini_clarity_is_ignored(monkeypatch):
    monkeypatch.setenv("PIPELINE_CLARITY", "on")
    for bad in (None, "yes", {"software_clear": "true"}):
        s = schema.new_spec("架個部落格")
        normalize.normalize(s, _gemini(bad, []), None)
        assert "clarity" not in s


def _gemini2(first, second, seen):
    """正規化回 first 的 clarity；第二次判斷（CLARITY_CHECK_SYSTEM）回 second（None＝沒回應）。"""
    from agents.pipeline.prompts import CLARITY_CHECK_SYSTEM
    norm = _gemini(first, seen)

    def g(system, user):
        if system == CLARITY_CHECK_SYSTEM:
            seen.append("check")
            return None if second is None else json.dumps(second)
        return norm(system, user)
    return g


def test_second_check_only_when_switch_on_and_first_says_clear(monkeypatch):
    monkeypatch.setenv("PIPELINE_CLARITY", "on")
    monkeypatch.delenv("PIPELINE_CLARITY_CHECK", raising=False)
    seen = []
    s = schema.new_spec("架個部落格")
    normalize.normalize(s, _gemini2({"kind_clear": True}, {"kind_clear": False}, seen), None)
    assert "check" not in seen and "check_clear" not in s["clarity"]           # 沒開：不多呼叫
    monkeypatch.setenv("PIPELINE_CLARITY_CHECK", "gemini2")
    seen = []
    s = schema.new_spec("架個部落格")
    normalize.normalize(s, _gemini2({"kind_clear": False}, {"kind_clear": True}, seen), None)
    assert "check" not in seen                                                 # 第一次就說沒講清楚：不用再問
    s = schema.new_spec("架個東西")
    normalize.normalize(s, _gemini2({"kind_clear": True}, {"kinds": ["wiki", "forum"], "kind_clear": False}, seen), None)
    assert s["clarity"]["check_clear"] is False and s["clarity"]["check_kinds"] == ["wiki", "forum"]
    s = schema.new_spec("架個東西")
    normalize.normalize(s, _gemini2({"kind_clear": True}, None, seen), None)
    assert "check_clear" not in s["clarity"] and s["clarity"]["software_clear"] is True   # 沒回應：維持第一次


def test_second_check_unclear_asks_and_blocks_trust(conf, monkeypatch):
    monkeypatch.setenv("PIPELINE_CLARITY", "on")
    monkeypatch.setenv("PIPELINE_TRUST_SUGGEST", "1")
    probs, _ = conf
    probs.update({"wordpress": 0.97})
    s = _spec(clarity={"software_clear": True, "suggest": "ghost", "check_clear": False})
    gap_check._choose_software(s, _never)
    assert _img_q(s)                                                           # 第二次說沒講清楚：問
    s = _spec(clarity={"software_clear": True, "suggest": "ghost", "check_clear": True})
    gap_check._choose_software(s, _never)
    assert schema.value(s, "image") == "ghost"
    s = _cmd_spec()
    s["clarity"]["check_clear"] = False
    gap_check._choose_software(s, _never)
    assert schema.value(s, "image") == "alpine"                               # 描述型指令那條也不採用建議


def test_log_mode_does_not_add_notes(monkeypatch):
    monkeypatch.setenv("PIPELINE_CLARITY", "log")
    s = schema.new_spec("x")
    normalize.normalize(s, _gemini({"software_clear": True, "notes": ["矛盾"]}, []), None)
    assert s["clarity"]["notes"] == ["矛盾"] and not s["notices"]


# ── 定時執行沒有指令 ─────────────────────────────────────────────────
def _cron_spec():
    s = schema.new_spec("每天早上八點用 python 自動跑一下")
    s["workload"] = {"value": "cronjob", "source": "inferred"}
    return s


def test_cron_without_command_gets_example_and_notice(monkeypatch):
    monkeypatch.delenv("PIPELINE_CRON_COMMAND", raising=False)
    s = _cron_spec()
    reasoner._need_cron_command(s, "python:3.11-slim")
    assert s["manifest"]["command"] == list(reasoner.CRON_EXAMPLE_COMMAND)
    assert not s["questions"] and any("範例指令" in n for n in s["notices"])
    reasoner._need_cron_command(s, "python:3.11-slim")   # 再跑一次（回答問題後）不重複
    assert len([n for n in s["notices"] if "範例指令" in n]) == 1


def test_cron_ask_mode_keeps_question(monkeypatch):
    monkeypatch.setenv("PIPELINE_CRON_COMMAND", "ask")
    s = _cron_spec()
    reasoner._need_cron_command(s, "python:3.11-slim")
    assert [q["field"] for q in s["questions"]] == ["cron_command"] and not s["manifest"].get("command")
