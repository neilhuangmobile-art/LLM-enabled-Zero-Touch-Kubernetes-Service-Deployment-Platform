"""模型寫的提醒、問題換成台灣用語（2026-11-23 網站實測：「存儲」「信息」「內存」）。"""
from agents.pipeline import schema


def test_notice_uses_taiwan_terms():
    s = schema.new_spec("x")
    schema.add_notice(s, "系統先用預設值處理，部署前請確認：這個排隊系統需要存儲用戶的排隊信息嗎？還是只需要在內存中處理？")
    assert s["notices"] == ["系統先用預設值處理，部署前請確認：這個排隊系統需要儲存用戶的排隊資訊嗎？還是只需要在記憶體中處理？"]
    schema.add_notice(s, "這個排隊系統需要儲存用戶的排隊資訊嗎？還是只需要在記憶體中處理？".replace("儲存", "存儲"))
    assert len(s["notices"]) == 2                       # 換完再比對重複


def test_question_and_options_use_taiwan_terms():
    s = schema.new_spec("x")
    schema.add_question(s, "image", "要用哪個數據庫？", [{"value": "mysql", "label": "MySQL 數據庫"}], recommended="mysql",
                        reason="默認用 mysql")
    q = s["questions"][0]
    assert q["question"] == "要用哪個資料庫？" and q["reason"] == "預設用 mysql"
    assert q["options"][0]["label"] == "MySQL 資料庫" and q["options"][-1]["value"] == "__auto__"


def test_words_common_in_taiwan_are_kept():
    assert schema.tw_terms("支持多用戶的配置") == "支持多用戶的配置"
