"""
測試 2026-09-20 新增的監控台（Dashboard）功能：`_my_deployment_resource_breakdown()`、
`_dashboard_summary()`、`/api/dashboard`。

這個功能的核心是 Kubecost 的 allocation = max(usage, requests) 概念：叢集「已用量」
在 K8s requests 加總與 Prometheus 實際使用量之間取較大值，比單看 requests 更貼近真實
情況。重點測三種情況：Prometheus 回報的實際用量比 requests 高（用 usage）、比 requests
低（用 requests，不能低估排程佔用）、Prometheus 整個連不上（優雅退回 requests，不當機）。

標記 integration：import web_demo.py 會連帶 import llama_client.py，llama_client.py
會 import torch，CI 沒裝這些重依賴跑不起來。
"""
import sys
import os
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = pytest.mark.integration

web_demo = pytest.importorskip("web_demo")


def _fake_deployment(name, replicas, cpu=None, memory=None):
    requests = {}
    if cpu is not None:
        requests["cpu"] = cpu
    if memory is not None:
        requests["memory"] = memory
    container = SimpleNamespace(resources=SimpleNamespace(requests=requests) if requests else None)
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name),
        spec=SimpleNamespace(
            replicas=replicas,
            template=SimpleNamespace(spec=SimpleNamespace(containers=[container])),
        ),
    )


class _FakeAppsV1Api:
    def __init__(self, deployments):
        self._deployments = deployments

    def list_namespaced_deployment(self, namespace):
        return SimpleNamespace(items=self._deployments)


class _FakePromClient:
    """monkeypatch 掉真正連線的 PrometheusClient，模擬三種情境。"""
    def __init__(self, alive, cpu_cores=None, mem_bytes=None):
        self._alive = alive
        self._cpu_cores = cpu_cores
        self._mem_bytes = mem_bytes

    def is_alive(self):
        return self._alive

    def cluster_cpu_usage_cores(self):
        return self._cpu_cores

    def cluster_memory_usage_bytes(self):
        return self._mem_bytes


class TestMyDeploymentResourceBreakdown:
    def test_sums_requests_and_estimates_cost(self, monkeypatch):
        monkeypatch.setattr(web_demo, "K8S_ENABLED", True)
        deployments = [
            _fake_deployment("web", 2, cpu="500m", memory="256Mi"),
            _fake_deployment("cache", 1, cpu="200m", memory="128Mi"),
        ]
        monkeypatch.setattr(web_demo.k8s_client, "AppsV1Api", lambda: _FakeAppsV1Api(deployments))
        result = web_demo._my_deployment_resource_breakdown("user-alice")
        assert len(result["deployments"]) == 2
        web = next(d for d in result["deployments"] if d["name"] == "web")
        assert web["replicas"] == 2
        assert web["cpu_cores"] == 1.0  # 500m * 2
        # total = web(1000m + 512Mi) + cache(200m + 128Mi) = 1200m, 640Mi
        assert result["total_cpu_cores"] == 1.2
        assert result["monthly_usd"] is not None and result["monthly_usd"] > 0

    def test_missing_resource_requests_counts_as_zero_not_error(self, monkeypatch):
        monkeypatch.setattr(web_demo, "K8S_ENABLED", True)
        deployments = [_fake_deployment("unspecified", 1)]  # 沒有填 cpu/mem
        monkeypatch.setattr(web_demo.k8s_client, "AppsV1Api", lambda: _FakeAppsV1Api(deployments))
        result = web_demo._my_deployment_resource_breakdown("user-bob")
        assert result["deployments"][0]["cpu_cores"] == 0
        assert result["total_cpu_cores"] == 0
        # 迴歸測試：_estimate_monthly_cost() 對「沒填資源」的容器會套用 `or 100`/
        # `or 128Mi` 這種單一容器的預設值假設，但這裡加總後真的是 0，不能被那個
        # `or` 誤判成「沒填」而冒出一個不存在的費用（0 在 Python 是 falsy）。
        assert result["monthly_usd"] == 0

    def test_zero_deployments_gives_zero_cost_not_fallback_default(self, monkeypatch):
        """同一個迴歸測試，但情境是「完全沒有部署」（deployments=[]），這是使用者
        剛註冊、還沒部署任何東西時最常見的狀態，監控台不該顯示一個假的月費。"""
        monkeypatch.setattr(web_demo, "K8S_ENABLED", True)
        monkeypatch.setattr(web_demo.k8s_client, "AppsV1Api", lambda: _FakeAppsV1Api([]))
        result = web_demo._my_deployment_resource_breakdown("user-newuser")
        assert result["deployments"] == []
        assert result["monthly_usd"] == 0

    def test_k8s_disabled_returns_empty_not_error(self, monkeypatch):
        monkeypatch.setattr(web_demo, "K8S_ENABLED", False)
        result = web_demo._my_deployment_resource_breakdown("user-carol")
        assert result["deployments"] == []
        assert result["total_cpu_cores"] == 0
        assert result["monthly_usd"] == 0


class TestDashboardSummary:
    def _patch_capacity_and_requests(self, monkeypatch, node_cpu="4000m", node_mem="8Gi",
                                      req_cpu_mc=1000, req_mem_b=1024**3):
        monkeypatch.setattr(web_demo, "K8S_ENABLED", True)
        monkeypatch.setattr(web_demo, "k8s_get_node_capacity", lambda: {"cpu": node_cpu, "memory": node_mem})
        monkeypatch.setattr(web_demo, "_sum_cluster_resource_requests", lambda: (req_cpu_mc, req_mem_b))
        monkeypatch.setattr(web_demo, "_my_deployment_resource_breakdown",
                             lambda ns: {"deployments": [], "total_cpu_cores": 0, "total_mem_gib": 0, "monthly_usd": 0})

    def test_uses_actual_usage_when_higher_than_requests(self, monkeypatch):
        # requests = 1000m/1GiB；Prometheus 回報實際用量 3000m/3GiB，遠高於 requests
        self._patch_capacity_and_requests(monkeypatch, req_cpu_mc=1000, req_mem_b=1024**3)
        fake_client = _FakePromClient(alive=True, cpu_cores=3.0, mem_bytes=3 * 1024**3)
        monkeypatch.setattr("observability.prometheus_client.PrometheusClient", lambda *a, **kw: fake_client)
        result = web_demo._dashboard_summary("user-alice")
        assert result["cluster_used"]["cpu_cores"] == 3.0
        assert result["cluster_used_source"]["cpu"] == "usage"
        assert result["cluster_used_source"]["mem"] == "usage"

    def test_uses_requests_when_actual_usage_lower(self, monkeypatch):
        # requests = 2000m/2GiB；Prometheus 回報實際用量只有 0.2 核，遠低於 requests
        # ——不能低估排程佔用，必須維持顯示 requests 的量。
        self._patch_capacity_and_requests(monkeypatch, req_cpu_mc=2000, req_mem_b=2 * 1024**3)
        fake_client = _FakePromClient(alive=True, cpu_cores=0.2, mem_bytes=0.1 * 1024**3)
        monkeypatch.setattr("observability.prometheus_client.PrometheusClient", lambda *a, **kw: fake_client)
        result = web_demo._dashboard_summary("user-alice")
        assert result["cluster_used"]["cpu_cores"] == 2.0
        assert result["cluster_used_source"]["cpu"] == "requests"
        assert result["cluster_used_source"]["mem"] == "requests"

    def test_falls_back_to_requests_when_prometheus_unreachable(self, monkeypatch):
        self._patch_capacity_and_requests(monkeypatch, req_cpu_mc=1500, req_mem_b=1024**3)
        fake_client = _FakePromClient(alive=False)
        monkeypatch.setattr("observability.prometheus_client.PrometheusClient", lambda *a, **kw: fake_client)
        result = web_demo._dashboard_summary("user-alice")
        assert result["prometheus_up"] is False
        assert result["cluster_used"]["cpu_cores"] == 1.5
        assert result["cluster_used_source"]["cpu"] == "requests"
        # 沒有整頁掛掉，剩餘空間算得出來
        assert result["cluster_remaining"] is not None

    def test_k8s_disabled_returns_none_capacity_not_error(self, monkeypatch):
        monkeypatch.setattr(web_demo, "K8S_ENABLED", False)
        result = web_demo._dashboard_summary("user-alice")
        assert result["k8s_enabled"] is False
        assert result["cluster_capacity"] is None


class TestDashboardRoute:
    @pytest.fixture
    def client(self, tmp_path, monkeypatch):
        monkeypatch.setattr(web_demo, "K8S_ENABLED", False)
        monkeypatch.setattr(web_demo, "USERS", {})
        monkeypatch.setattr(web_demo, "__file__", str(tmp_path / "web_demo.py"))
        web_demo.app.config["TESTING"] = True
        with web_demo.app.test_client() as c:
            yield c

    def test_requires_login(self, client):
        resp = client.get("/api/dashboard")
        assert resp.status_code == 401

    def test_returns_summary_structure_when_logged_in(self, client, monkeypatch):
        with client.session_transaction() as sess:
            sess["username"] = "alice"
        resp = client.get("/api/dashboard")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "cluster_capacity" in data
        assert "my_deployments" in data
        assert data["k8s_enabled"] is False
