"""agents/intent_route.py：Gemini 意圖判斷結果的固定規則查核（2026-11-08）。"""
from agents.intent_route import services_brief, validate_routed_intent

SVCS = [{"name": "web", "image": "nginx:1.24", "replicas": 3},
        {"name": "test-redis", "image": "redis:7", "replicas": 1}]


def test_bad_format_or_unknown_action_falls_back():
    assert validate_routed_intent(None, "x", []) is None
    assert validate_routed_intent({"action": "rm_rf"}, "x", []) is None
    assert validate_routed_intent("deploy", "x", []) is None


def test_deploy_ignores_model_args():
    r = validate_routed_intent({"action": "deploy", "args": {"image": "made-up:1"}, "confidence": 0.9}, "幫我弄個網站", [])
    assert r == {"action": "deploy", "args": {}, "confidence": 0.9, "source": "gemini"}


def test_qa_passes():
    assert validate_routed_intent({"action": "qa", "confidence": 0.8}, "什麼是 Pod", [])["action"] == "qa"


def test_scale_resolved_from_service_list():
    r = validate_routed_intent({"action": "scale", "args": {"name": "web", "replicas": 6}, "confidence": 0.9},
                               "剛剛那個 nginx 網站幫我改成 6 台", SVCS)
    assert r["action"] == "scale" and r["args"] == {"name": "web", "replicas": 6}


def test_scale_replicas_not_in_message_clarifies():
    r = validate_routed_intent({"action": "scale", "args": {"name": "web", "replicas": 9}, "confidence": 0.9},
                               "nginx 網站加大一點", SVCS)
    assert r["action"] == "clarify" and "ungrounded_args" in r["clarify_reasons"]


def test_chinese_numeral_replicas():
    r = validate_routed_intent({"action": "scale", "args": {"name": "web", "replicas": 3}, "confidence": 0.9},
                               "web 改成三台", SVCS)
    assert r["action"] == "scale"


def test_invented_name_clarifies():
    r = validate_routed_intent({"action": "delete", "args": {"name": "web-frontend"}, "confidence": 0.95}, "幫我刪掉它", SVCS)
    assert r["action"] == "clarify" and r["ungrounded_values"] == ["web-frontend"]


def test_name_from_message_allowed_even_if_not_listed():
    r = validate_routed_intent({"action": "delete", "args": {"name": "shop"}, "confidence": 0.9}, "刪除 shop", SVCS)
    assert r["action"] == "delete"


def test_missing_name_clarifies():
    r = validate_routed_intent({"action": "rollback", "args": {}, "confidence": 0.9}, "退回上一版", SVCS)
    assert r["action"] == "clarify" and "missing_args" in r["clarify_reasons"]


def test_low_confidence_destructive_clarifies():
    r = validate_routed_intent({"action": "delete", "args": {"name": "web"}, "confidence": 0.5}, "web 刪掉好了", SVCS)
    assert r["action"] == "clarify" and "low_confidence" in r["clarify_reasons"]


def test_update_image_tag_must_be_in_message():
    ok = validate_routed_intent({"action": "update_image", "args": {"name": "web", "image": "nginx:1.25"}, "confidence": 0.9},
                                "把 nginx 網站升級成 1.25 版", SVCS)
    assert ok["action"] == "update_image"
    bad = validate_routed_intent({"action": "update_image", "args": {"name": "web", "image": "nginx:1.27"}, "confidence": 0.9},
                                 "把 nginx 網站升級", SVCS)
    assert bad["action"] == "clarify"


def test_update_image_base_from_current_service():
    r = validate_routed_intent({"action": "update_image", "args": {"name": "web", "image": "nginx:1.26"}, "confidence": 0.9},
                               "web 換成 1.26 版", SVCS)
    assert r["action"] == "update_image"


def test_update_image_unrelated_base_rejected():
    r = validate_routed_intent({"action": "update_image", "args": {"name": "web", "image": "evil/miner:1.26"}, "confidence": 0.9},
                               "web 換成 1.26 版", SVCS)
    assert r["action"] == "clarify"


def test_real_name_case_used():
    r = validate_routed_intent({"action": "delete", "args": {"name": "TEST-REDIS"}, "confidence": 0.9}, "把 redis 刪掉", SVCS)
    assert r["args"]["name"] == "test-redis"


def test_services_brief():
    assert "name=web image=nginx:1.24 replicas=3" in services_brief(SVCS)
    assert services_brief([]) == "(none)"
