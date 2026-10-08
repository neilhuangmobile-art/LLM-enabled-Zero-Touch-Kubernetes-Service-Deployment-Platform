"""
測試 core/model_server.py 的 _clean_intent()：2026-10-07 新增的「近失事件可見化」——
破壞性操作被降級成 clarify 時，要附上具體原因（clarify_reasons）跟是哪個值查無根據
（ungrounded_values），不是只回一個通用的「不確定」。完整脈絡見 docs/security_review.md。

標記為 integration：這個檔案 import core.model_server 會連帶 import torch/transformers，
本機裝好了才能跑，CI 沒裝這些重依賴（見 tests/test_model_server_injection.py 的說明，
同一套理由）。
"""
import sys
import os

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = pytest.mark.integration

pytest.importorskip("torch")
from core.model_server import _clean_intent, _find_ungrounded_args


class TestFindUngroundedArgs:
    def test_value_present_in_message_passes(self):
        assert _find_ungrounded_args({"name": "web-frontend"}, "delete web-frontend") == []

    def test_fabricated_value_flagged(self):
        result = _find_ungrounded_args({"name": "web-frontend"}, "幫我把它刪掉")
        assert result == [{"key": "name", "value": "web-frontend"}]

    def test_fabricated_numeric_value_flagged(self):
        """2026-10-07 整理量化報告時發現的真實 bug：這個函式原本直接跳過所有
        非字串值，數字型欄位（replicas/pods/port）完全沒有查核——「scale it」
        沒講要幾個，模型捏造 replicas=5，應該要被標記成查無根據。"""
        assert _find_ungrounded_args({"replicas": 5}, "scale it") == [
            {"key": "replicas", "value": 5}
        ]

    def test_grounded_numeric_value_as_digit_passes(self):
        assert _find_ungrounded_args({"replicas": 6}, "把 shop-api 擴成 6 個副本") == []

    def test_grounded_numeric_value_as_english_word_passes(self):
        """數字用英文數字詞表達（不是阿拉伯數字字面），一樣要算有根據，不能
        因為補了數字查核反而誤殺這種正常案例。"""
        assert _find_ungrounded_args({"replicas": 0}, "scale down checkout to zero") == []

    def test_grounded_numeric_value_as_chinese_word_passes(self):
        assert _find_ungrounded_args({"replicas": 2}, "把 cache 擴成兩個副本") == []

    def test_bool_values_skipped(self):
        """bool 在 Python 是 int 的子類別，但這個意圖系統沒有 bool 型 args，
        這裡只是確認型別判斷沒有意外把 bool 當數字查核搞壞。"""
        assert _find_ungrounded_args({"flag": True}, "anything") == []

    def test_multiple_fabricated_values_all_flagged(self):
        result = _find_ungrounded_args(
            {"name": "ghost-service", "image": "made-up:latest"}, "update the image please"
        )
        keys = {r["key"] for r in result}
        assert keys == {"name", "image"}


class TestClarifyReasons:
    def test_ungrounded_fabricated_name_reports_specific_value(self):
        """2026-10-06 QA 掃描實測案例：零上下文的「幫我把它刪掉」，模型用 0.95 信心
        捏造出 web-frontend——clarify_reasons 要含 ungrounded_args，且
        ungrounded_values 要指名是 web-frontend 這個值。"""
        parsed = {"action": "delete", "args": {"name": "web-frontend"}, "confidence": 0.95}
        result = _clean_intent(parsed, "幫我把它刪掉")

        assert result["action"] == "clarify"
        assert "ungrounded_args" in result["clarify_reasons"]
        assert result["ungrounded_values"] == [{"key": "name", "value": "web-frontend"}]
        # 原本的信心沒有被要求降級成 qa 等級的 0——仍然保留模型自己回報的值，
        # 給前端呈現「AI 原本多有信心」用。
        assert result["confidence"] == 0.95

    def test_low_confidence_alone_reports_low_confidence_reason(self):
        parsed = {"action": "rollback", "args": {"name": "api"}, "confidence": 0.5}
        result = _clean_intent(parsed, "api rollback 一下")

        assert result["action"] == "clarify"
        assert result["clarify_reasons"] == ["low_confidence"]
        assert result["ungrounded_values"] == []

    def test_missing_required_args_reports_missing_args_reason(self):
        parsed = {"action": "scale", "args": {"name": "web-frontend"}, "confidence": 0.9}
        result = _clean_intent(parsed, "scale web-frontend")

        assert result["action"] == "clarify"
        assert "missing_args" in result["clarify_reasons"]

    def test_grounded_high_confidence_action_executes_without_clarify_fields(self):
        """對照組：乾淨、有根據的破壞性操作正常執行，不應該出現 clarify_reasons
        這種多餘的欄位（修法前後，正常路徑的行為跟回傳形狀不能跑掉）。"""
        parsed = {"action": "delete", "args": {"name": "web-frontend"}, "confidence": 0.95}
        result = _clean_intent(parsed, "delete web-frontend")

        assert result["action"] == "delete"
        assert "clarify_reasons" not in result
        assert "ungrounded_values" not in result
