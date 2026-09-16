"""
測試 agents/security_agent.py：這是三層防護（orchestrator → guardian → gitops）
第一層會直接拿去做 block/warn 決策的分數來源，錯了會導致「危險的部署被放過」或
「安全的部署被誤擋」，兩種方向都要覆蓋。
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.security_agent import scan


def _deployment(containers=None, host_network=False, host_pid=False):
    return {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": "test-app"},
        "spec": {
            "replicas": 1,
            "template": {
                "spec": {
                    "hostNetwork": host_network,
                    "hostPID": host_pid,
                    "containers": containers or [],
                }
            },
        },
    }


class TestSecurityScan:
    def test_privileged_container_is_critical(self):
        manifest = _deployment(containers=[{
            "name": "app", "image": "nginx:1.25",
            "securityContext": {"privileged": True},
        }])
        result = scan(manifest)
        assert result["ok"] is False
        assert any(i["rule_id"] == "SEC-001" for i in result["issues"])

    def test_host_network_is_critical(self):
        manifest = _deployment(
            containers=[{"name": "app", "image": "nginx:1.25"}],
            host_network=True,
        )
        result = scan(manifest)
        assert result["ok"] is False
        assert any(i["rule_id"] == "SEC-002" for i in result["issues"])

    def test_host_pid_is_critical(self):
        manifest = _deployment(
            containers=[{"name": "app", "image": "nginx:1.25"}],
            host_pid=True,
        )
        result = scan(manifest)
        assert result["ok"] is False
        assert any(i["rule_id"] == "SEC-003" for i in result["issues"])

    def test_latest_tag_is_only_medium_not_blocking(self):
        manifest = _deployment(containers=[{"name": "app", "image": "nginx:latest"}])
        result = scan(manifest)
        # latest tag 本身不該讓 ok 變 False（不是 critical/high），
        # 這條測試確保之後改規則權重時不會不小心讓它變成阻斷級。
        latest_issues = [i for i in result["issues"] if i["rule_id"] == "SEC-006"]
        assert len(latest_issues) == 1
        assert latest_issues[0]["severity"] == "medium"

    def test_clean_manifest_scores_high_and_passes(self):
        manifest = _deployment(containers=[{
            "name": "app", "image": "nginx:1.25-alpine",
            "securityContext": {
                "allowPrivilegeEscalation": False,
                "readOnlyRootFilesystem": True,
            },
            "resources": {
                "requests": {"cpu": "100m", "memory": "128Mi"},
                "limits": {"cpu": "500m", "memory": "256Mi"},
            },
        }])
        result = scan(manifest)
        assert result["ok"] is True
        assert result["score"] == 100

    def test_multiple_critical_issues_stack_score_deduction(self):
        manifest = _deployment(
            containers=[{
                "name": "app", "image": "alpine:latest",
                "securityContext": {"privileged": True, "runAsUser": 0},
            }],
            host_network=True,
        )
        result = scan(manifest)
        assert result["ok"] is False
        assert result["score"] < 40  # privileged(40) + hostNetwork(40) 疊加後遠低於 block 門檻

    def test_invalid_manifest_type_returns_error_result_not_exception(self):
        result = scan("not a dict, not valid yaml: [[[")
        assert result["ok"] is False
        assert result["score"] == 0
