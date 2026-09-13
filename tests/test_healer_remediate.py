"""
測試 healer/remediate.py 在 dry_run=True 下的分派邏輯與純函式工具（_infer_deployment_name、
_double_memory）。dry_run 模式故意不連 K8s，所以這些測試在沒有叢集的環境也能跑，
CI 不需要 kubeconfig。真正會呼叫 K8s API 的路徑標成 integration，另外處理。
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from healer.remediate import remediate, _infer_deployment_name, _double_memory


def _issue(pod_name="api-7d6b8c9f4-xk2pq", namespace="default", container="api"):
    return {"pod_name": pod_name, "namespace": namespace, "container": container}


class TestInferDeploymentName:
    def test_standard_pod_name_format(self):
        assert _infer_deployment_name("my-app-7d6b8c9f4-xk2pq") == "my-app"

    def test_single_word_deployment_name(self):
        assert _infer_deployment_name("api-7d6b8c9f4-xk2pq") == "api"

    def test_short_name_without_enough_segments(self):
        # 不足三段時的行為要明確，不能讓呼叫端拿到 None 卻沒檢查就繼續操作 K8s
        result = _infer_deployment_name("onlyoneword")
        assert result is None


class TestDoubleMemory:
    def test_mebibytes(self):
        assert _double_memory("256Mi") == "512Mi"

    def test_gibibytes(self):
        assert _double_memory("1Gi") == "2Gi"

    def test_unrecognized_format_returned_unchanged(self):
        assert _double_memory("not-a-memory-string") == "not-a-memory-string"


class TestRemediateDryRun:
    def test_increase_memory_dry_run_does_not_touch_k8s(self):
        result = remediate(
            _issue(),
            {"action": "increase_memory", "severity": "high", "suggestion": "..."},
            dry_run=True,
        )
        assert result["ok"] is True
        assert result["action"] == "increase_memory"

    def test_unknown_action_falls_back_to_manual_inspect_handler(self):
        result = remediate(
            _issue(),
            {"action": "this_action_does_not_exist", "severity": "low", "suggestion": "x"},
            dry_run=True,
        )
        assert result["ok"] is True
        assert "kubectl" in result["message"]

    def test_result_includes_timestamp_and_action(self):
        result = remediate(
            _issue(), {"action": "analyze_logs", "severity": "high"}, dry_run=True,
        )
        assert "timestamp" in result
        assert result["action"] == "analyze_logs"

    def test_fix_port_conflict_and_manual_inspect_never_touch_k8s_even_without_dry_run(self):
        # 這兩個動作本身設計上就是「只給建議、不操作」，不需要 dry_run 保護，
        # 確認即使 dry_run=False 也不會嘗試連線（不會丟出跟 K8s client 相關的例外）。
        result = remediate(_issue(), {"action": "fix_port_conflict"}, dry_run=False)
        assert result["ok"] is True
        result2 = remediate(_issue(), {"action": "manual_inspect", "suggestion": "check logs"}, dry_run=False)
        assert result2["ok"] is True
