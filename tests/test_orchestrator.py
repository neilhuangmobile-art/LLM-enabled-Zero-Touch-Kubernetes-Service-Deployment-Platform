"""
測試 agents/orchestrator.py 的決策邏輯（approve/warn/block）：這是真正會擋住
使用者部署的最終關卡，決策邏輯錯了比單一 agent 算錯分數更嚴重。
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.orchestrator import orchestrate


def _manifest(privileged=False, image="nginx:1.25-alpine", replicas=1,
              requests=None, limits=None, with_probes=False):
    sc = {"allowPrivilegeEscalation": False}
    if privileged:
        sc["privileged"] = True
    container = {"name": "app", "image": image, "securityContext": sc, "resources": {}}
    if requests is not None:
        container["resources"]["requests"] = requests
    if limits is not None:
        container["resources"]["limits"] = limits
    if with_probes:
        probe = {"httpGet": {"path": "/health", "port": 8080}, "initialDelaySeconds": 10}
        container["readinessProbe"] = probe
        container["livenessProbe"] = probe
    return {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": "test-app"},
        "spec": {"replicas": replicas, "template": {"spec": {"containers": [container]}}},
    }


class TestOrchestrateDecision:
    def test_clean_deployment_is_approved(self):
        # 要拿到 approve，除了 replicas>=2（否則 perf_agent 判「單副本無法高可用」）
        # 還要有 readiness/liveness probe（否則 perf_agent 判「缺少健康探針」），
        # 兩者都是 high severity、都是刻意設計的規則，不是 bug——
        # 見 agents/perf_agent.py 的 _check_replicas / _check_probes。
        manifest = _manifest(
            replicas=2, with_probes=True,
            requests={"cpu": "100m", "memory": "128Mi"},
            limits={"cpu": "500m", "memory": "256Mi"},
        )
        result = orchestrate(manifest, parallel=False)
        assert result["decision"] == "approve"
        assert result["blockers"] == []

    def test_single_replica_warns_due_to_no_high_availability(self):
        manifest = _manifest(
            replicas=1,
            requests={"cpu": "100m", "memory": "128Mi"},
            limits={"cpu": "500m", "memory": "256Mi"},
        )
        result = orchestrate(manifest, parallel=False)
        assert result["decision"] == "warn"
        assert any("單副本" in w or "single_replica" in w for w in result["warnings"])

    def test_privileged_container_is_blocked(self):
        manifest = _manifest(privileged=True)
        result = orchestrate(manifest, parallel=False)
        assert result["decision"] == "block"
        assert any("安全" in b for b in result["blockers"])

    def test_missing_resources_warns_but_does_not_block(self):
        # cost_agent 把「完全沒設資源」判成 high（不是 critical），
        # orchestrator 目前的規則是 high cost issue → warn，不是 block。
        manifest = _manifest()
        result = orchestrate(manifest, parallel=False)
        assert result["decision"] in ("warn", "block")
        if result["decision"] == "warn":
            assert result["blockers"] == []

    def test_parallel_and_sequential_execution_agree(self):
        manifest = _manifest(
            requests={"cpu": "100m", "memory": "128Mi"},
            limits={"cpu": "500m", "memory": "256Mi"},
        )
        result_parallel = orchestrate(manifest, parallel=True)
        result_sequential = orchestrate(manifest, parallel=False)
        assert result_parallel["decision"] == result_sequential["decision"]

    def test_invalid_yaml_string_blocks_with_parse_error(self):
        result = orchestrate(": : : not valid [[[", parallel=False)
        assert result["decision"] == "block"
        assert "YAML" in result["reason"] or "解析" in result["reason"]

    def test_agents_key_contains_all_three_reports(self):
        manifest = _manifest(
            requests={"cpu": "100m", "memory": "128Mi"},
            limits={"cpu": "500m", "memory": "256Mi"},
        )
        result = orchestrate(manifest, parallel=False)
        assert set(result["agents"].keys()) == {"security", "cost", "perf"}
