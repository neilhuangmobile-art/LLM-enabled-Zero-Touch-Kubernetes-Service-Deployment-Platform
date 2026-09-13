"""
測試 healer/diagnose.py 的規則層根因分析（use_llm=False，不需要 model server 常駐）。
這是 web_demo.py 新的 _real_heal_pod() / 背景自動修復迴圈的第一步，判斷錯了會讓
remediate() 對 Pod 執行錯誤的補救動作（例如把 CrashLoopBackOff 誤判成 OOMKilled，
結果去調記憶體而不是重建 Pod）。
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from healer.diagnose import diagnose_issue


def _context(reason, logs="", message="", events=None):
    return {
        "pod_name": "test-pod-abc12", "namespace": "default", "container": "app",
        "reason": reason, "message": message, "logs": logs,
        "events": events or [], "restart_count": 3,
    }


class TestRuleBasedDiagnosis:
    def test_oom_killed_maps_to_increase_memory(self):
        result = diagnose_issue(_context("OOMKilled"), use_llm=False)
        assert result["action"] == "increase_memory"
        assert result["severity"] == "high"
        assert result["confidence"] == "rule"

    def test_image_pull_backoff_maps_to_fix_image(self):
        result = diagnose_issue(_context("ImagePullBackOff", message="manifest unknown"), use_llm=False)
        assert result["action"] == "fix_image"
        assert result["severity"] == "high"

    def test_crash_loop_backoff_maps_to_analyze_logs(self):
        result = diagnose_issue(_context("CrashLoopBackOff"), use_llm=False)
        assert result["action"] == "analyze_logs"

    def test_connection_refused_in_logs_maps_to_check_dependencies(self):
        result = diagnose_issue(
            _context("Error", logs="Error: connect ECONNREFUSED 10.0.0.5:5432"),
            use_llm=False,
        )
        assert result["action"] == "check_dependencies"

    def test_probe_failure_maps_to_fix_probe(self):
        result = diagnose_issue(
            _context("Unknown", events=[{"message": "Readiness probe failed: timeout"}]),
            use_llm=False,
        )
        assert result["action"] == "fix_probe"

    def test_unrecognized_reason_falls_back_to_manual_inspect(self):
        result = diagnose_issue(_context("SomeWeirdReasonNeverSeenBefore"), use_llm=False)
        assert result["action"] == "manual_inspect"
        assert result["confidence"] == "unknown"

    def test_result_always_has_required_fields(self):
        for reason in ("OOMKilled", "ImagePullBackOff", "CrashLoopBackOff", "TotallyUnknown"):
            result = diagnose_issue(_context(reason), use_llm=False)
            for field in ("reason", "root_cause", "severity", "action", "suggestion", "confidence"):
                assert field in result, f"missing '{field}' for reason={reason}"
