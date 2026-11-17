"""多代理人部署解析流程（agents/pipeline，2026-11-16）。全部用假的模型回覆，不碰網路與 GPU。"""
import json

import pytest

from agents.pipeline import gap_check, intent_check, normalize, runner, schema, software_rag
from agents.pipeline.prompts import INTENT_SYSTEM, NORMALIZE_SYSTEM, REASON_SYSTEM, SELECT_SYSTEM, VERIFY_SYSTEM


def fake(normalized=None, intent_rounds=None, reason=None, calls=None, select=None, verify_rounds=None):
    """依 system prompt 分辨是哪一步，回傳事先寫好的輸出。"""
    intent_rounds = list(intent_rounds or [])
    verify_rounds = list(verify_rounds or [])

    def llm(system, user):
        if calls is not None:
            calls.append(system)
        if system == NORMALIZE_SYSTEM:
            return json.dumps(normalized) if normalized is not None else None
        if system == INTENT_SYSTEM:
            return json.dumps({"missing": intent_rounds.pop(0) if intent_rounds else []})
        if system == REASON_SYSTEM:
            return json.dumps(reason or {})
        if system == SELECT_SYSTEM:
            return json.dumps(select or {})
        if system == VERIFY_SYSTEM:
            return json.dumps({"problems": verify_rounds.pop(0) if verify_rounds else []})
        raise AssertionError("沒看過的 prompt")
    return llm


# 假的向量：每個關鍵字一維（中文原句和英文說明各自對到同一維），不載入 BGE-M3
_VOCAB = [("wiki", "維基", "知識庫"), ("database", "資料庫"), ("cach", "快取"), ("blog", "部落格", "publish")]


def fake_embed(texts):
    return [[1.0 if any(w in t.lower() for w in ws) else 0.0 for ws in _VOCAB] for t in texts]


@pytest.fixture(autouse=True)
def _fake_rag():
    software_rag.set_embedder(fake_embed)
    yield
    software_rag.set_embedder(None)


def norm(base=None, extras=None, purpose="x"):
    b = {f: None for f in schema.BASE_FIELDS}
    b.update(base or {})
    return {"purpose": purpose, "base": b, "extras": extras or []}


# ── 來源等級 ─────────────────────────────────────────────────────────
def test_lower_source_cannot_override_higher():
    s = schema.new_spec("x")
    assert schema.set_field(s, "replicas", 3, "user")
    assert not schema.set_field(s, "replicas", 1, "inferred")
    assert not schema.set_field(s, "replicas", 2, "default")
    assert schema.value(s, "replicas") == 3
    assert schema.set_field(s, "memory", "256Mi", "default")
    assert schema.set_field(s, "memory", "512Mi", "inferred")       # 高等級可以蓋低等級
    assert schema.value(s, "memory") == "512Mi"


def test_invalid_values_are_rejected():
    s = schema.new_spec("x")
    assert not schema.set_field(s, "port", 70000, "user")
    assert not schema.set_field(s, "replicas", 0, "user")
    assert not schema.set_field(s, "image", "Not An Image!", "user")
    assert schema.set_field(s, "app_name", "Club Page", "inferred") and schema.value(s, "app_name") == "club-page"
    assert schema.set_field(s, "memory", "10G", "user") and schema.value(s, "memory") == "10Gi"


# ── 正規化：沒根據的值不算使用者講的 ────────────────────────────────────
def test_normalize_drops_ungrounded_values():
    s = schema.new_spec("幫我開一個 redis 7 來做快取")
    normalize.apply_normalized(s, norm({"image": "redis:7", "app_name": "redis-cache", "port": 6379}))
    assert schema.value(s, "image") == "redis:7" and s["base"]["image"]["source"] == "user"
    assert schema.value(s, "app_name") is None        # 使用者沒取名字
    assert schema.value(s, "port") is None            # 6379 是預設 port，原句沒講


def test_extra_needs_evidence_in_text():
    s = schema.new_spec("密碼設 123456")
    assert normalize.add_extra(s, {"kind": "secret", "value": "123456", "evidence": "密碼設 123456"}, "user")
    assert not normalize.add_extra(s, {"kind": "storage", "evidence": "資料要保存"}, "user")   # 原句沒有
    assert not normalize.add_extra(s, {"kind": "secret", "value": "123456", "evidence": "密碼設 123456"}, "user")  # 重複


def test_gemini_failure_falls_back_to_local():
    s = schema.new_spec("deploy mysql please")
    local = fake(normalized=norm({"image": "mysql"}))
    normalize.normalize(s, gemini=lambda a, b: None, local=local)
    assert s["backend"]["normalize"] == "local" and schema.value(s, "image") == "mysql"


# ── 整個流程 ─────────────────────────────────────────────────────────
def test_mysql_password_and_storage_end_to_end():
    text = "幫我弄一個 mysql 8.0 的資料庫，密碼設 123456，裡面的客戶資料很重要重開機不能不見喔！"
    gem = fake(normalized=norm({"image": "mysql:8.0"}, [
        {"kind": "secret", "desc": "密碼", "value": "123456", "evidence": "密碼設 123456"},
        {"kind": "storage", "desc": "保存", "value": {"size": None}, "evidence": "重開機不能不見"}]))
    local = fake(reason={"image": "mysql:8.0", "app_name": "customer-db", "replicas": 1, "port": 3306,
                         "memory": "512Mi", "cpu": "250m", "workload": "service", "expose": "internal"})
    p = runner.to_payload(runner.run(text, gem, local))
    assert p["image"] == "mysql:8.0" and p["app_name"] == "customer-db" and p["port"] == 3306
    assert p["env"]["MYSQL_ROOT_PASSWORD"] == "123456" and p["secret_keys"] == ["MYSQL_ROOT_PASSWORD"]
    assert p["volumes"] == [{"name": "data", "mount": "/var/lib/mysql", "kind": "pvc", "size": "1Gi", "read_only": False}]
    assert p["expose"] is False
    src = p["_pipeline"]["base"]
    assert src["image"]["source"] == "user" and src["app_name"]["source"] == "inferred"
    assert src["port"]["source"] == "default"          # 查表


def test_table_port_overrides_inferred_but_not_user():
    s = schema.new_spec("ghost")
    schema.set_field(s, "image", "ghost", "user")
    schema.set_field(s, "port", 80, "inferred")
    gap_check._table_and_defaults(s)
    assert schema.value(s, "port") == 2368
    s2 = schema.new_spec("ghost 開 8080 port")
    schema.set_field(s2, "image", "ghost", "user")
    schema.set_field(s2, "port", 8080, "user")
    gap_check._table_and_defaults(s2)
    assert schema.value(s2, "port") == 8080


def test_unknown_software_becomes_question():
    text = "欸我想在上面跑一個我自己平常會用到的服務"
    local = fake(normalized=norm(), reason={"questions": [{"question": "你想部署什麼服務？", "recommended": "nginx"}],
                                            "image": None, "replicas": 1, "workload": "service", "expose": "internal"})
    s = runner.run(text, None, local)
    assert schema.value(s, "image") is None
    assert [q["field"] for q in s["questions"]] == ["image"]
    assert s["questions"][0]["options"][-1] == {"value": "__auto__", "label": "幫我決定"}


def test_gap_check_stops_when_no_progress():
    calls = []
    local = fake(normalized=norm(), reason={"questions": [{"question": "什麼軟體？"}], "replicas": 1,
                                            "workload": "service", "expose": "public"}, calls=calls)
    runner.run("幫我弄一個", None, local)
    assert calls.count(REASON_SYSTEM) == 1             # 第一次就補不出映像檔，不重試


def test_gap_check_retries_at_most_three_times():
    calls = []
    s = schema.new_spec("x")
    gap_check.run(s, lambda sys, user: (calls.append(sys), "not json")[1])
    assert len(calls) == gap_check.MAX_ROUNDS == 3
    assert any(q["field"] == "image" for q in s["questions"])


def test_intent_check_max_three_rounds_then_asks():
    text = "要 A 設定、要 B 設定、要 C 設定、要 D 設定"
    rounds = [[{"kind": "other", "desc": x, "evidence": f"要 {x} 設定"}] for x in "ABCD"]
    s = schema.new_spec(text)
    intent_check.run(s, fake(intent_rounds=rounds))
    assert s["attempts"]["intent"] == 3 and len(s["extras"]) == 3
    assert s["questions"] and s["questions"][0]["field"].startswith("extra:")


def test_schedule_becomes_cronjob():
    text = "每天半夜三點跑 echo backup_done，用 alpine"
    gem = fake(normalized=norm({"image": "alpine"}, [
        {"kind": "schedule", "value": {"cron": "0 3 * * *"}, "evidence": "每天半夜三點"},
        {"kind": "command", "value": "echo backup_done", "evidence": "echo backup_done"}]))
    local = fake(reason={"app_name": "backup", "replicas": 1, "workload": "cronjob", "expose": "none",
                         "memory": "64Mi", "cpu": "50m"})
    s = runner.run(text, gem, local)
    assert s["questions"] == []
    p = runner.to_payload(s)
    assert p["workload"] == "cronjob" and p["schedule"] == "0 3 * * *" and p["pods"] == 1 and p["expose"] is False
    assert p["command"] == ["sh", "-c", "echo backup_done"]


def test_schedule_too_frequent_asks_without_auto_and_can_cancel():
    text = "每分鐘跑 echo hi，用 alpine"
    gem = fake(normalized=norm({"image": "alpine"}, [
        {"kind": "schedule", "value": {"cron": "* * * * *"}, "evidence": "每分鐘"},
        {"kind": "command", "value": "echo hi", "evidence": "echo hi"}]))
    s = runner.run(text, gem, fake(reason={"workload": "cronjob"}))
    q = [q for q in s["questions"] if q["field"] == "schedule_interval"][0]
    assert q["recommended"] is None and all(o["value"] != "__auto__" for o in q["options"])
    fixed = runner.resume(s, {q["id"]: "*/5 * * * *"}, fake(reason={}))
    assert runner.to_payload(fixed)["schedule"] == "*/5 * * * *" and not fixed["questions"]
    assert runner.resume(s, {q["id"]: "cancel"}, fake())["cancelled"]


def test_cronjob_without_command_asks_for_it():
    text = "每天早上八點用 python 自動跑一下"
    gem = fake(normalized=norm({"image": "python"}, [
        {"kind": "schedule", "value": {"cron": "0 8 * * *"}, "evidence": "每天早上八點"}]))
    s = runner.run(text, gem, fake(reason={"workload": "cronjob"}))
    q = [q for q in s["questions"] if q["field"] == "cron_command"][0]
    s2 = runner.resume(s, {q["id"]: "python -c 'print(1)'"}, fake(reason={}))
    assert runner.to_payload(s2)["command"] == ["sh", "-c", "python -c 'print(1)'"] and not s2["questions"]


def test_storage_over_limit_asks_instead_of_silently_reducing():
    text = "mongodb 6.0，硬碟給它留個 10G 存資料"
    gem = fake(normalized=norm({"image": "mongo:6.0"}, [{"kind": "storage", "value": {"size": "10Gi"}, "evidence": "留個 10G"}]))
    s = runner.run(text, gem, fake(reason={"app_name": "orders", "workload": "service", "expose": "internal"}))
    assert any(q["field"] == "storage_size" for q in s["questions"])
    cancel = runner.resume(s, {s["questions"][0]["id"]: "cancel"}, None)
    assert "error" in runner.to_payload(cancel)


def test_rabbitmq_management_page():
    text = "幫我 run 一個 rabbitmq，要有那個 management 網頁介面可以看"
    gem = fake(normalized=norm({"image": "rabbitmq"}, [{"kind": "extra_port", "evidence": "management 網頁介面"}]))
    p = runner.to_payload(runner.run(text, gem, fake(reason={"app_name": "mq", "workload": "service", "expose": "internal"})))
    assert p["image"] == "rabbitmq:management" and p["extra_ports"] == [{"port": 15672, "container": 15672}]
    assert p["port"] == 5672


def test_resume_keeps_user_fields_and_fills_image():
    local = fake(normalized=norm({"replicas": 3}), reason={"questions": [{"question": "什麼軟體？"}],
                                                           "workload": "service", "expose": "public"})
    s = runner.run("幫我開三台", None, local)
    assert schema.value(s, "replicas") == 3
    qid = [q for q in s["questions"] if q["field"] == "image"][0]["id"]
    s2 = runner.resume(s, {qid: "nginx"}, fake(reason={"app_name": "web", "replicas": 1}))
    assert schema.value(s2, "image") == "nginx" and s2["base"]["image"]["source"] == "user"
    assert schema.value(s2, "replicas") == 3          # 推理代理說 1，但使用者講的 3 不會被蓋
    assert schema.value(s2, "port") == 80


def test_no_models_at_all_still_returns_questions():
    s = runner.run("幫我架一個網站", None, None)
    assert any(q["field"] == "image" for q in s["questions"])
    assert runner.to_payload(s)["image"] == ""


def test_no_expose_and_schedule_need_explicit_words():
    s = schema.new_spec("讓附近客人掃 QR code 可以看，展期三天，萬一當機不能讓觀眾連不上，千萬不要暴露到外網，每天半夜三點備份")
    assert not normalize.add_extra(s, {"kind": "no_expose", "evidence": "讓附近客人掃 QR code 可以看"}, "user")
    assert not normalize.add_extra(s, {"kind": "no_expose", "evidence": "萬一當機不能讓觀眾連不上"}, "user")
    assert not normalize.add_extra(s, {"kind": "schedule", "evidence": "展期三天"}, "user")
    assert normalize.add_extra(s, {"kind": "no_expose", "evidence": "千萬不要暴露到外網"}, "user")
    assert normalize.add_extra(s, {"kind": "schedule", "evidence": "每天半夜三點備份"}, "user")


def test_valid_cron():
    from agents.pipeline.reasoner import valid_cron
    assert valid_cron("0  3 * * *") == "0 3 * * *" and valid_cron({"cron": "*/5 * * * *"}) == "*/5 * * * *"
    assert valid_cron("每 5 分鐘") is None and valid_cron("0 25 * * *") is None and valid_cron(None) is None


def test_inferred_storage_size_does_not_trigger_question():
    s = schema.new_spec("x")
    schema.set_field(s, "image", "wordpress", "user")
    s["extras"].append({"kind": "storage", "desc": "", "value": {"size": "10Gi"}, "evidence": "", "source": "inferred",
                        "status": "pending", "note": ""})
    from agents.pipeline import reasoner
    reasoner.apply_extras(s)
    assert s["questions"] == [] and s["manifest"]["volumes"][0]["size"] == "5Gi"


# ── 選軟體的 RAG（11-17）──────────────────────────────────────────────
def test_repo_of_and_known():
    assert software_rag.repo_of("docker.io/library/redis:7@sha256:abc") == "redis"
    assert software_rag.repo_of("gitea/gitea:1.22") == "gitea/gitea"
    assert software_rag.is_known("mediawiki:1.42") and software_rag.is_known("gitea/gitea")
    assert not software_rag.is_known("mikepenz/mediawiki") and not software_rag.is_known("redis/redis")


def test_candidates_use_original_sentence():
    c = software_rag.candidates("我們想架一個公司內部的維基知識庫")
    assert c and c[0]["image"] == "mediawiki"


def test_invented_image_becomes_question_with_candidates():
    text = "幫我架一個公司內部的維基知識庫"
    local = fake(normalized=norm(), reason={"image": "mikepenz/mediawiki", "replicas": 1, "workload": "service",
                                            "expose": "internal"})
    s = runner.run(text, None, local)
    assert schema.value(s, "image") is None
    q = [q for q in s["questions"] if q["field"] == "image"][0]
    assert q["recommended"] == "mediawiki" and q["options"][0]["value"] == "mediawiki"
    assert q["options"][-1]["value"] == "__auto__"
    s2 = runner.resume(s, {q["id"]: "__auto__"}, fake(reason={}))
    assert schema.value(s2, "image") == "mediawiki" and s2["base"]["image"]["source"] == "user"


def test_user_stated_image_is_not_checked_against_catalog():
    text = "部署 mycorp/attendance:1.0"
    local = fake(normalized=norm({"image": "mycorp/attendance:1.0"}), reason={"workload": "service", "expose": "public"})
    s = runner.run(text, None, local)
    assert schema.value(s, "image") == "mycorp/attendance:1.0" and not s["questions"]


def test_model_question_gets_candidates_and_its_recommendation_first():
    text = "期末專題要一個資料庫存資料"
    local = fake(normalized=norm(), reason={"questions": [{"question": "要用哪種資料庫？", "recommended": "PostgreSQL"}],
                                            "replicas": 1, "workload": "service", "expose": "internal"})
    s = runner.run(text, None, local)
    q = [q for q in s["questions"] if q["field"] == "image"][0]
    assert q["options"][0]["value"] == "postgres" and q["recommended"] == "postgres"


def test_reasoner_gets_only_the_original_sentence():
    seen = []

    def llm(system, user):
        if system == REASON_SYSTEM:
            seen.append(user)
            return json.dumps({"image": "nginx"})
        return fake(normalized=norm({"replicas": 2}))(system, user)
    runner.run("開兩個網站", None, llm)
    assert seen and all(u == "開兩個網站" for u in seen)


def test_short_name_becomes_publisher_full_name():
    assert software_rag.full_name("grafana") == "grafana/grafana"
    assert software_rag.full_name("gitea:1.22") == "gitea/gitea:1.22"
    assert software_rag.full_name("nginx:1.27") == "nginx:1.27"          # 官方映像檔不動
    assert software_rag.full_name("mycorp/app") == "mycorp/app"          # 有命名空間的不動
    text = "幫我開一個 grafana 看監控"
    s = runner.run(text, None, fake(normalized=norm({"image": "grafana"}), reason={"workload": "service", "expose": "public"}))
    assert schema.value(s, "image") == "grafana/grafana" and s["base"]["image"]["source"] == "user"
    assert schema.value(s, "port") == 3000
    assert any("grafana/grafana" in n for n in s["notices"])


# ── 選軟體（推理代理之後的第二步，11-17）────────────────────────────
def test_select_step_can_switch_to_a_listed_candidate():
    text = "公司要一個內部的維基知識庫"
    local = fake(normalized=norm(), reason={"image": "nextcloud", "workload": "service", "expose": "internal"},
                 select={"image": "mediawiki", "reason": "wiki"})
    s = runner.run(text, None, local)
    assert schema.value(s, "image") == "mediawiki" and s["base"]["image"]["source"] == "inferred"
    assert any(t["step"] == "select" for t in s["trace"])


def test_select_step_cannot_invent_or_touch_user_choice():
    text = "公司要一個內部的維基知識庫"
    s = runner.run(text, None, fake(normalized=norm(), reason={"image": "nextcloud", "workload": "service"},
                                    select={"image": "someone/fake-wiki"}))
    assert schema.value(s, "image") == "nextcloud"          # 清單外的不採用
    s = runner.run("部署 nextcloud 當知識庫", None, fake(normalized=norm({"image": "nextcloud"}), reason={},
                                                     select={"image": "mediawiki"}))
    assert schema.value(s, "image") == "nextcloud"          # 使用者講的不換


def test_select_step_skips_databases_and_runtimes():
    calls = []
    runner.run("期末專題要一個資料庫", None, fake(normalized=norm(), reason={"image": "postgres", "workload": "service"},
                                             calls=calls))
    assert SELECT_SYSTEM not in calls


def test_linuxserver_port_and_data_path_from_catalog():
    from agents.pipeline import defaults
    assert defaults.listen_port("linuxserver/wikijs") == 3000
    assert defaults.listen_port("linuxserver/nextcloud") == 443 and defaults.listen_port("nextcloud") == 80
    assert defaults.data_path("linuxserver/bookstack") == "/config"


# ── 意圖符合檢查（verify，11-17）──────────────────────────────────────
# 檢查機制（修正、上限、來回改、問題卡）用「全部項目都檢查」測；預設只檢查 schedule，另外測
from agents.pipeline import verify as _verify  # noqa: E402


@pytest.fixture
def verify_all(monkeypatch):
    monkeypatch.setattr(_verify, "CHECK_FIELDS", _verify.FIELDS)


_BG = "每小時自動跑一個背景程式整理資料，沒有人會從外面連它"


def test_verify_fixes_contradiction_with_valid_suggestion(verify_all):
    local = fake(normalized=norm(), reason={"image": "python", "workload": "service", "expose": "public"},
                 verify_rounds=[[{"field": "expose", "issue": "沒有人從外面連", "evidence": "沒有人會從外面連它",
                                  "suggest": "internal"}]])
    s = runner.run(_BG, None, local)
    assert s["expose"]["value"] == "internal"
    assert s["verified"] and s["verified"][0]["field"] == "expose"
    assert any("public" in n and "internal" in n for n in s["notices"])


def test_verify_ignores_evidence_not_in_text_and_unknown_fields(verify_all):
    local = fake(normalized=norm(), reason={"image": "python", "workload": "service", "expose": "public"},
                 verify_rounds=[[{"field": "expose", "issue": "x", "evidence": "原句沒有這段", "suggest": "internal"},
                                 {"field": "hostNetwork", "issue": "x", "evidence": "背景程式", "suggest": True}]])
    s = runner.run(_BG, None, local)
    assert s["expose"]["value"] == "public" and not s["verified"]


def test_verify_does_not_check_user_stated_fields(verify_all):
    seen = []
    base = fake(normalized=norm({"image": "redis", "replicas": 3}), reason={"workload": "service", "expose": "internal"})

    def llm(system, user):
        if system == VERIFY_SYSTEM:
            seen.append(user)
        return base(system, user)
    runner.run("開 3 個 redis", None, llm)
    assert seen and '"replicas"' not in seen[0] and '"image"' not in seen[0]


def test_verify_rejects_invented_image_and_refills(verify_all):
    text = "公司要一個內部的維基知識庫"
    local = fake(normalized=norm(), reason={"image": "nextcloud", "workload": "service", "expose": "internal"},
                 verify_rounds=[[{"field": "image", "issue": "要的是維基", "evidence": "維基知識庫", "suggest": "someone/wiki"}]])
    s = runner.run(text, None, local)
    # 建議的映像檔不存在 → 不採用；清空重推還是 nextcloud → 不再白跑，改問使用者
    assert any(q["field"] == "verify:image" for q in s["questions"])
    assert schema.value(s, "image") == "nextcloud"


def _three_round_local():
    r1 = [{"field": "replicas", "issue": "只有自己用", "evidence": "背景程式", "suggest": 1}]
    r2 = [{"field": "memory", "issue": "整理資料很小", "evidence": "整理資料", "suggest": "128Mi"}]
    r3 = [{"field": "cpu", "issue": "每小時才跑", "evidence": "每小時", "suggest": "100m"}]
    return fake(normalized=norm(), reason={"image": "python", "workload": "worker", "expose": "internal", "replicas": 3,
                                           "memory": "512Mi", "cpu": "500m"}, verify_rounds=[r1, r2, r3])


def test_verify_asks_after_three_rounds(verify_all):
    s = runner.run(_BG, None, _three_round_local())
    q = [q for q in s["questions"] if q["field"] == "verify:cpu"][0]
    assert q["options"][-1]["value"] == "__auto__" and s["attempts"]["verify"] == 3
    assert schema.value(s, "replicas") == 1 and schema.value(s, "memory") == "128Mi"


def test_verify_flip_flop_restores_original_without_asking(verify_all):
    p = {"field": "replicas", "issue": "只有自己用", "evidence": "背景程式"}
    local = fake(normalized=norm(), reason={"image": "python", "workload": "worker", "expose": "internal", "replicas": 3},
                 verify_rounds=[[dict(p, suggest=2)], [dict(p, suggest=3)], [dict(p, suggest=2)]])
    s = runner.run(_BG, None, local)
    assert schema.value(s, "replicas") == 3 and not s["questions"]
    assert not s["verified"] and not any("replicas" in n for n in s["notices"])


def test_verify_drops_wrong_schedule_so_no_cronjob_question():
    text = "監控中心要自己定時去抓各台機器的數據並存起來，給工程師查"
    local = fake(normalized=norm({}, [{"kind": "schedule", "desc": "定時", "value": "*/5 * * * *", "evidence": "定時去抓各台機器的數據"}]),
                 reason={"image": "prom/prometheus", "workload": "cronjob", "expose": "internal", "schedule": "*/5 * * * *"},
                 verify_rounds=[[{"field": "schedule", "issue": "定時抓資料是軟體本身的功能", "evidence": "定時去抓各台機器的數據",
                                  "suggest": "none"}]])
    s = runner.run(text, None, local)
    assert not any(q["field"] == "schedule" for q in s["questions"])
    assert s["workload"]["value"] == "service"


def test_verify_no_problem_calls_once(verify_all):
    calls = []
    runner.run(_BG, None, fake(normalized=norm(), reason={"image": "python", "workload": "worker", "expose": "internal"},
                               calls=calls))
    assert calls.count(VERIFY_SYSTEM) == 1


def test_verify_resume_keep_and_accept(verify_all):
    s = runner.run(_BG, None, _three_round_local())
    qid = [q for q in s["questions"] if q["field"] == "verify:cpu"][0]["id"]
    kept = runner.resume(s, {qid: "keep"}, fake(verify_rounds=[[{"field": "cpu", "issue": "x", "evidence": "每小時", "suggest": "50m"}]]))
    assert kept["base"]["cpu"]["source"] == "user" and schema.value(kept, "cpu") == "500m" and not kept["questions"]
    took = runner.resume(s, {qid: "__auto__"}, fake())
    assert schema.value(took, "cpu") == "100m" and took["base"]["cpu"]["source"] == "user"


def test_verify_default_only_checks_schedule():
    calls = []
    local = fake(normalized=norm(), reason={"image": "python", "workload": "service", "expose": "public"}, calls=calls,
                 verify_rounds=[[{"field": "expose", "issue": "x", "evidence": "沒有人會從外面連它", "suggest": "internal"}]])
    s = runner.run(_BG, None, local)
    assert VERIFY_SYSTEM not in calls                  # 沒有排程：不呼叫模型
    assert s["expose"]["value"] == "public"


def test_verify_default_checks_schedule_when_present():
    text = "監控中心要自己定時去抓各台機器的數據並存起來，給工程師查"
    calls = []
    local = fake(normalized=norm({}, [{"kind": "schedule", "desc": "定時", "value": "*/5 * * * *", "evidence": "定時去抓各台機器的數據"}]),
                 reason={"image": "prom/prometheus", "workload": "cronjob", "expose": "internal", "schedule": "*/5 * * * *"},
                 calls=calls, verify_rounds=[[{"field": "schedule", "issue": "軟體本身的功能", "evidence": "定時去抓各台機器的數據",
                                               "suggest": "none"}]])
    s = runner.run(text, None, local)
    assert calls.count(VERIFY_SYSTEM) >= 1
    assert not any(q["field"] == "schedule" for q in s["questions"]) and s["workload"]["value"] == "service"



# ── 格式修正（format_check，11-17）────────────────────────────────────
from agents.pipeline import format_check as _fmt  # noqa: E402
from agents.pipeline.prompts import FORMAT_FIX_SYSTEM  # noqa: E402


def test_format_rule_fixes():
    assert _fmt.rule_fix("memory", "0.5Gi") == "512Mi" and _fmt.rule_fix("memory", "1.5GB") == "1536Mi"
    assert _fmt.rule_fix("memory", "2 GB") == "2Gi" and _fmt.rule_fix("memory", "512") is None   # 沒單位不猜
    assert _fmt.rule_fix("cpu", "0.5") == "500m" and _fmt.rule_fix("cpu", "500 millicores") == "500m"
    assert _fmt.rule_fix("cpu", "2 cores") == "2"
    assert _fmt.rule_fix("port", "8080/tcp") == "8080"
    assert _fmt.rule_fix("app_name", "123 shop") == "app-123-shop"


def test_rejected_value_is_repaired_by_rules_and_rechecked():
    text = "開一個 redis，記憶體 0.5Gi"
    s = runner.run(text, None, fake(normalized=norm({"image": "redis"}), reason={"memory": "0.5Gi", "workload": "service"}))
    assert schema.value(s, "memory") == "512Mi" and s["base"]["memory"]["source"] == "inferred"
    assert s["format_fixed"][0]["from"] == "0.5Gi" and any("0.5Gi" in n for n in s["notices"])


def test_rejected_value_repaired_by_model_when_rules_fail():
    seen = []
    base = fake(normalized=norm({"image": "redis"}), reason={"replicas": "three", "workload": "service"})

    def llm(system, user):
        if system == FORMAT_FIX_SYSTEM:
            seen.append(user)
            return json.dumps({"value": 3})
        return base(system, user)
    s = runner.run("開 redis", None, llm)
    assert seen and schema.value(s, "replicas") == 3


def test_unfixable_value_asks_after_three_rounds():
    calls = []
    base = fake(normalized=norm({"image": "redis"}), reason={"memory": "很多", "workload": "service"})

    def llm(system, user):
        calls.append(system)
        if system == FORMAT_FIX_SYSTEM:
            return json.dumps({"value": None})
        return base(system, user)
    s = runner.run("開 redis", None, llm)
    assert calls.count(FORMAT_FIX_SYSTEM) == 3
    q = [q for q in s["questions"] if q["field"] == "format:memory"][0]
    s2 = runner.resume(s, {q["id"]: "1G"}, fake())
    assert schema.value(s2, "memory") == "1Gi" and s2["base"]["memory"]["source"] == "user"


def test_app_name_must_start_with_letter():
    s = runner.run("開 redis", None, fake(normalized=norm({"image": "redis"}), reason={"app_name": "123-cache"}))
    assert schema.value(s, "app_name") == "app-123-cache"
