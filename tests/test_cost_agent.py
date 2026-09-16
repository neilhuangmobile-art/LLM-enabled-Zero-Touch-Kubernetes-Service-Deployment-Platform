"""
測試 agents/cost_agent.py 的純邏輯部分（單位轉換、app type 偵測、node 數量估算、
manifest 資源分析）。不需要 K8s 連線、不需要 model server，純函式輸入輸出比對。

這些函式是整個「部署前資源審查」的計算根基（node_estimate、_check_scale_risk 都
依賴 estimate_node_count / _parse_*，見 web_demo.py），錯了會直接影響「這個 Pod
會不會被判定超出節點容量」這種會擋住使用者部署的關鍵決策，值得優先覆蓋測試。
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.cost_agent import (
    _parse_memory_bytes,
    _parse_cpu_millicores,
    _detect_app_type,
    estimate_node_count,
    analyze,
)


class TestParseMemoryBytes:
    def test_mebibytes(self):
        assert _parse_memory_bytes("256Mi") == 256 * 1024 ** 2

    def test_gibibytes(self):
        assert _parse_memory_bytes("2Gi") == 2 * 1024 ** 3

    def test_decimal_gigabytes(self):
        assert _parse_memory_bytes("1G") == 1000 ** 3

    def test_no_unit_defaults_to_bytes(self):
        assert _parse_memory_bytes("512") == 512

    def test_empty_or_none_returns_none(self):
        assert _parse_memory_bytes("") is None
        assert _parse_memory_bytes(None) is None

    def test_garbage_returns_none(self):
        assert _parse_memory_bytes("not-a-size") is None


class TestParseCpuMillicores:
    def test_millicores_suffix(self):
        assert _parse_cpu_millicores("500m") == 500

    def test_whole_cores(self):
        assert _parse_cpu_millicores("2") == 2000

    def test_fractional_cores(self):
        assert _parse_cpu_millicores("0.5") == 500

    def test_empty_or_none_returns_none(self):
        assert _parse_cpu_millicores("") is None
        assert _parse_cpu_millicores(None) is None


class TestDetectAppType:
    def test_web_image(self):
        assert _detect_app_type("nginx:1.25", "web-frontend") == "web"

    def test_database_image(self):
        assert _detect_app_type("postgres:16", "db") == "database"

    def test_llm_image(self):
        assert _detect_app_type("vllm/vllm-openai:latest", "inference") == "llm"

    def test_unknown_image_falls_back_to_default(self):
        assert _detect_app_type("some-random-thing:1.0", "mystery") == "default"


class TestEstimateNodeCount:
    def test_single_pod_fits_one_node(self):
        result = estimate_node_count(
            "200m", "256Mi", replicas=1,
            node_capacity={"cpu": "4", "memory": "8Gi"},
        )
        assert result["node_count"] == 1

    def test_replicas_multiply_before_dividing(self):
        # 50 副本 × 256Mi/200m，單個沒超節點，但加總起來要算對，
        # 這是之前真的抓到過的 bug（用固定假設值時算錯節點數）。
        result = estimate_node_count(
            "200m", "256Mi", replicas=50,
            node_capacity={"cpu": "20", "memory": "16Gi"},
        )
        # 記憶體：50 * 256Mi = 12.8Gi，節點 16Gi 一個放得下
        # CPU：50 * 200m = 10000m，節點 20000m 一個放得下
        assert result["node_count"] == 1

    def test_needs_multiple_nodes_when_total_exceeds_one(self):
        result = estimate_node_count(
            "1000m", "1Gi", replicas=10,
            node_capacity={"cpu": "4", "memory": "8Gi"},
        )
        # CPU: 10000m / 4000m = 3（無條件進位）；memory: 10Gi / 8Gi = 2
        assert result["node_count"] == 3
        assert result["cpu_bound"] == 3
        assert result["memory_bound"] == 2

    def test_missing_cpu_and_memory_still_returns_at_least_one_node(self):
        result = estimate_node_count(None, None, replicas=1, node_capacity={"cpu": "4", "memory": "8Gi"})
        assert result["node_count"] == 1

    def test_falls_back_to_core_config_when_no_capacity_given(self):
        # 不帶 node_capacity 時要能跑（走 core.config.NODE_CAPACITY 這條路），
        # 不應該炸掉——這是舊行為的回歸測試，之後改動不能讓這條路徑掛掉。
        result = estimate_node_count("100m", "128Mi", replicas=1)
        assert result["node_count"] >= 1


class TestAnalyzeManifest:
    def _manifest(self, replicas=1, requests=None, limits=None, image="nginx:1.25"):
        containers = [{"name": "app", "image": image, "resources": {}}]
        if requests is not None:
            containers[0]["resources"]["requests"] = requests
        if limits is not None:
            containers[0]["resources"]["limits"] = limits
        return {
            "apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": "test-app"},
            "spec": {"replicas": replicas, "template": {"spec": {"containers": containers}}},
        }

    def test_no_resources_set_flags_high_severity_issue(self):
        result = analyze(self._manifest())
        types = [i["type"] for i in result["issues"]]
        assert "missing_resources" in types
        assert result["ok"] is False  # missing_resources 是 high severity

    def test_requests_exceed_limits_is_critical(self):
        manifest = self._manifest(
            requests={"memory": "512Mi", "cpu": "100m"},
            limits={"memory": "128Mi", "cpu": "500m"},
        )
        result = analyze(manifest)
        critical = [i for i in result["issues"] if i["severity"] == "critical"]
        assert any(i["type"] == "requests_exceed_limits" for i in critical)

    def test_well_configured_web_app_has_no_high_severity_issues(self):
        manifest = self._manifest(
            requests={"memory": "128Mi", "cpu": "100m"},
            limits={"memory": "256Mi", "cpu": "500m"},
        )
        result = analyze(manifest)
        high_or_critical = [i for i in result["issues"] if i["severity"] in ("high", "critical")]
        assert high_or_critical == []
        assert result["ok"] is True

    def test_cost_estimate_scales_with_replicas(self):
        manifest = self._manifest(
            replicas=3,
            requests={"memory": "128Mi", "cpu": "100m"},
            limits={"memory": "256Mi", "cpu": "500m"},
        )
        result = analyze(manifest)
        cost = result["cost_estimate"]
        assert cost["replicas"] == 3
        # _estimate_monthly_cost() 內部用 round(x, 2)，跟純數學算出來的 0.375 會差在小數點，
        # 用 pytest.approx 容許這個已知的四捨五入誤差，不要求逐位元一致。
        import pytest
        assert cost["memory_gib"] == pytest.approx(3 * 128 / 1024, abs=0.01)
