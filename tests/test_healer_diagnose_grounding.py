"""
測試 healer/diagnose.py 的根因事實查核：2026-10-06 kubectl exec 真實故障測試發現
監控小模型（1.5B）在證據不足時仍會幻覈出具體根因（實測案例：純 `nginx -s stop`
反覆製造 CrashLoopBackOff，日誌空、事件只有通用 "Back-off restarting failed
container"，模型卻編出「容器資源不足導致 OOM Killed」）。diagnose_issue() 在規則層
命中已知模式時，只有合併文字裡真的有具體證據才信任模型的 root_cause，否則蓋成
老實版本。完整記錄見 docs/healer_exec_test_20261006.md、docs/security_review.md 23 節。

純邏輯測試，healer/diagnose.py 不 import torch/flask，不需要 pytest.importorskip。
"""
import sys
import os
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from healer.diagnose import diagnose_issue, _has_concrete_signal


class TestHasConcreteSignal:
    def test_specific_signals_detected(self):
        assert _has_concrete_signal("oom killed, out of memory") is True
        assert _has_concrete_signal("errimagepull: manifest unknown") is True
        assert _has_concrete_signal("permission denied opening /data") is True
        assert _has_concrete_signal("econnrefused connecting to db") is True

    def test_generic_backoff_text_has_no_signal(self):
        assert _has_concrete_signal("crashloopbackoff\nback-off restarting failed container") is False
        assert _has_concrete_signal("") is False


class TestDiagnoseIssueHallucinationGuard:
    """重現 2026-10-06 的真實案例：context 跟那次測試一樣（reason=CrashLoopBackOff，
    logs 空，events 只有通用重啟訊息），LLM 幻覈出 OOM 根因時必須被蓋掉。"""

    def _hallucinated_llm_result(self, *_args, **_kwargs):
        from healer import diagnose as diagnose_mod
        return {
            "reason": "CrashLoopBackOff",
            "root_cause": "容器資源不足導致 OOM Killed",
            "severity": "high",
            "action": "increase_memory",
            "suggestion": "增加記憶體上限",
            "confidence": "llm",
            "raw_llm": "",
        }

    def test_hallucinated_root_cause_overridden_when_no_concrete_signal(self):
        context = {
            "pod_name": "healer-exec-test-abc",
            "namespace": "user-qa-sweep-20261006",
            "container": "nginx",
            "reason": "CrashLoopBackOff",
            "message": "",
            "restart_count": 5,
            "logs": "",
            "events": [{"reason": "BackOff", "message": "Back-off restarting failed container"}],
        }
        with patch("healer.diagnose._llm_analyze", side_effect=self._hallucinated_llm_result):
            result = diagnose_issue(context, use_llm=True)

        assert "OOM" not in result["root_cause"]
        assert "證據不足" in result["root_cause"]
        # action/severity 仍然信任規則層（CrashLoopBackOff → analyze_logs），不是模型亂猜的 increase_memory
        assert result["action"] == "analyze_logs"
        # 2026-10-07 近失事件可見化：被蓋掉的原始猜測要保留下來，不能整個丟棄。
        assert result["llm_raw_guess"] == "容器資源不足導致 OOM Killed"

    def test_llm_root_cause_kept_when_concrete_signal_present(self):
        context = {
            "pod_name": "worker-1",
            "namespace": "default",
            "container": "app",
            "reason": "CrashLoopBackOff",
            "message": "",
            "restart_count": 6,
            "logs": "fatal: out of memory, oom killed",
            "events": [{"reason": "OOMKilling", "message": "Memory limit reached"}],
        }
        with patch("healer.diagnose._llm_analyze", side_effect=self._hallucinated_llm_result):
            result = diagnose_issue(context, use_llm=True)

        # 這次合併文字裡真的有 "oom"，模型給的根因有證據支持，應該被保留。
        assert result["root_cause"] == "容器資源不足導致 OOM Killed"
        # 沒有觸發蓋版，不應該出現 llm_raw_guess 這個多餘欄位。
        assert "llm_raw_guess" not in result

    def test_backoff_event_without_literal_reason_string_still_triggers_override(self):
        """2026-10-07 近失事件可見化實測時發現的真實 bug：用 web_demo.py 的
        api_pod_detail() 同一套邏輯對一個真實 CrashLoopBackOff pod 做 live 測試，
        抓到的那個瞬間容器的 reason 欄位是空的（state 剛好不是 waiting），退回用
        last_state.terminated.reason="Completed" 頂替，事件文字也只有 K8s 原生的
        "Back-off restarting failed container"，完全沒有出現 "CrashLoopBackOff"
        這個字面字串——原本的規則正則抓不到，導致 rule_result["confidence"] 不是
        "rule"，連帶讓整道 _has_concrete_signal() 防護被繞過，模型幻覈的 "OOM"
        直接無保護地回給使用者。這裡用同樣的欄位組合重現，確認規則層現在認得
        "back-off restarting" 這個 K8s 原生措辭，不是只認字面的 "CrashLoopBackOff"。"""
        context = {
            "pod_name": "nearmiss-crashloop-abc",
            "namespace": "user-test",
            "container": "nginx",
            "reason": "Completed",  # 不是 "CrashLoopBackOff"
            "message": "",
            "restart_count": 2,
            "logs": "",
            "events": [{"reason": "BackOff",
                        "message": "Back-off restarting failed container nginx"}],
        }
        with patch("healer.diagnose._llm_analyze", side_effect=self._hallucinated_llm_result):
            result = diagnose_issue(context, use_llm=True)

        assert "OOM" not in result["root_cause"]
        assert "證據不足" in result["root_cause"]
        assert result["llm_raw_guess"] == "容器資源不足導致 OOM Killed"
