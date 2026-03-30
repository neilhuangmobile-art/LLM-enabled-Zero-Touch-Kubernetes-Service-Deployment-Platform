"""
observability/prometheus_client.py
Prometheus 指標查詢客戶端 — 提供叢集資源使用率的結構化查詢介面。

功能：
  - 查詢 Pod / Node CPU、記憶體使用率
  - 查詢 Deployment 可用副本數
  - 查詢自訂 PromQL 表達式
  - Prometheus 不可用時優雅降級（回傳 None）

研究報告依據：
    「整合 Prometheus 指標使 LLM 代理能夠做出資料驅動的決策，
     例如根據實際 CPU 使用率自動觸發水平擴展」
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import time
from typing import Optional

try:
    import requests as _requests
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False

# 從環境變數讀取 Prometheus 位址
_DEFAULT_URL = os.environ.get("PROMETHEUS_URL", "http://localhost:9090")


# ══════════════════════════════════════════════════════════════════
# 公開 API
# ══════════════════════════════════════════════════════════════════

class PrometheusClient:
    """
    Prometheus HTTP API 查詢客戶端。

    使用方式：
        client = PrometheusClient("http://prometheus.monitoring:9090")
        cpu = client.pod_cpu_usage("my-pod", "default")
    """

    def __init__(self, url: str = _DEFAULT_URL, timeout: int = 10):
        self.url     = url.rstrip("/")
        self.timeout = timeout

    # ── 基礎查詢 ─────────────────────────────────────────────────

    def query(self, promql: str) -> Optional[list]:
        """
        執行即時 PromQL 查詢。

        Returns:
            list of {"metric": dict, "value": float} 或 None（查詢失敗）
        """
        if not REQUESTS_AVAILABLE:
            return None
        try:
            resp = _requests.get(
                f"{self.url}/api/v1/query",
                params={"query": promql},
                timeout=self.timeout,
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
            if data.get("status") != "success":
                return None
            return [
                {
                    "metric": r["metric"],
                    "value" : float(r["value"][1]),
                }
                for r in data["data"]["result"]
            ]
        except Exception:
            return None

    def query_range(
        self,
        promql    : str,
        start_mins: int = 30,
        step_secs : int = 60,
    ) -> Optional[list]:
        """
        執行範圍查詢（過去 start_mins 分鐘，每 step_secs 秒一個點）。

        Returns:
            list of {"metric": dict, "values": list[(ts, float)]} 或 None
        """
        if not REQUESTS_AVAILABLE:
            return None
        now   = time.time()
        start = now - start_mins * 60
        try:
            resp = _requests.get(
                f"{self.url}/api/v1/query_range",
                params={
                    "query": promql,
                    "start": start,
                    "end"  : now,
                    "step" : step_secs,
                },
                timeout=self.timeout,
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
            if data.get("status") != "success":
                return None
            return [
                {
                    "metric": r["metric"],
                    "values": [(float(v[0]), float(v[1])) for v in r["values"]],
                }
                for r in data["data"]["result"]
            ]
        except Exception:
            return None

    # ── 常用指標查詢 ─────────────────────────────────────────────

    def pod_cpu_usage(self, pod_name: str, namespace: str = "default") -> Optional[float]:
        """
        查詢 Pod 的 CPU 使用率（單位：核心數）。
        使用 rate() 取過去 5 分鐘平均值。
        """
        promql = (
            f'sum(rate(container_cpu_usage_seconds_total{{'
            f'pod="{pod_name}",namespace="{namespace}",container!=""'
            f'}}[5m]))'
        )
        results = self.query(promql)
        if results:
            return results[0]["value"]
        return None

    def pod_memory_usage_bytes(self, pod_name: str, namespace: str = "default") -> Optional[float]:
        """查詢 Pod 的記憶體使用量（bytes）。"""
        promql = (
            f'sum(container_memory_working_set_bytes{{'
            f'pod="{pod_name}",namespace="{namespace}",container!=""'
            f'}})'
        )
        results = self.query(promql)
        if results:
            return results[0]["value"]
        return None

    def pod_memory_usage_mi(self, pod_name: str, namespace: str = "default") -> Optional[float]:
        """查詢 Pod 的記憶體使用量（MiB）。"""
        val = self.pod_memory_usage_bytes(pod_name, namespace)
        return round(val / 1024 / 1024, 1) if val is not None else None

    def deployment_available_replicas(
        self, deploy_name: str, namespace: str = "default"
    ) -> Optional[int]:
        """查詢 Deployment 目前可用副本數。"""
        promql = (
            f'kube_deployment_status_replicas_available{{'
            f'deployment="{deploy_name}",namespace="{namespace}"'
            f'}}'
        )
        results = self.query(promql)
        if results:
            return int(results[0]["value"])
        return None

    def deployment_desired_replicas(
        self, deploy_name: str, namespace: str = "default"
    ) -> Optional[int]:
        """查詢 Deployment 目標副本數。"""
        promql = (
            f'kube_deployment_spec_replicas{{'
            f'deployment="{deploy_name}",namespace="{namespace}"'
            f'}}'
        )
        results = self.query(promql)
        if results:
            return int(results[0]["value"])
        return None

    def node_cpu_percent(self) -> Optional[dict]:
        """
        查詢所有 Node 的 CPU 使用率（百分比）。

        Returns:
            {"node_name": float_percent, ...} 或 None
        """
        promql = (
            '100 - (avg by (node) '
            '(rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)'
        )
        results = self.query(promql)
        if results is None:
            return None
        return {
            r["metric"].get("node", "unknown"): round(r["value"], 1)
            for r in results
        }

    def namespace_pod_count(self, namespace: str = "default") -> Optional[int]:
        """查詢 namespace 內執行中的 Pod 數量。"""
        promql = (
            f'count(kube_pod_status_phase{{'
            f'namespace="{namespace}",phase="Running"'
            f'}})'
        )
        results = self.query(promql)
        if results:
            return int(results[0]["value"])
        return None

    def pod_restart_count(self, pod_name: str, namespace: str = "default") -> Optional[int]:
        """查詢 Pod 容器重啟次數總和。"""
        promql = (
            f'sum(kube_pod_container_status_restarts_total{{'
            f'pod="{pod_name}",namespace="{namespace}"'
            f'}})'
        )
        results = self.query(promql)
        if results:
            return int(results[0]["value"])
        return None

    def is_alive(self) -> bool:
        """確認 Prometheus 是否可連線。"""
        if not REQUESTS_AVAILABLE:
            return False
        try:
            resp = _requests.get(f"{self.url}/-/healthy", timeout=3)
            return resp.status_code == 200
        except Exception:
            return False

    # ── 彙整報告 ─────────────────────────────────────────────────

    def pod_summary(self, pod_name: str, namespace: str = "default") -> dict:
        """
        一次取得 Pod 的所有關鍵指標，回傳彙整 dict。

        Returns:
            {
                "pod_name"     : str,
                "namespace"    : str,
                "cpu_cores"    : float | None,
                "memory_mi"    : float | None,
                "restarts"     : int   | None,
                "prometheus_ok": bool,
            }
        """
        alive = self.is_alive()
        return {
            "pod_name"     : pod_name,
            "namespace"    : namespace,
            "cpu_cores"    : self.pod_cpu_usage(pod_name, namespace)      if alive else None,
            "memory_mi"    : self.pod_memory_usage_mi(pod_name, namespace) if alive else None,
            "restarts"     : self.pod_restart_count(pod_name, namespace)  if alive else None,
            "prometheus_ok": alive,
        }


# ── 便利函式 ─────────────────────────────────────────────────────

def get_pod_metrics(pod_name: str, namespace: str = "default",
                    url: str = _DEFAULT_URL) -> dict:
    """一行取得 Pod 指標彙整。"""
    return PrometheusClient(url).pod_summary(pod_name, namespace)


# ══════════════════════════════════════════════════════════════════
# CLI 測試入口
# ══════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("=" * 55)
    print("  observability/prometheus_client.py — 指標查詢測試")
    print("=" * 55)

    client = PrometheusClient()
    alive  = client.is_alive()

    print(f"\nPrometheus ({client.url}): {'✅ 連線正常' if alive else '❌ 無法連線'}")

    if alive:
        nodes = client.node_cpu_percent()
        if nodes:
            print("\nNode CPU 使用率：")
            for node, pct in nodes.items():
                bar = "█" * int(pct / 5)
                print(f"  {node:30s} {pct:5.1f}%  {bar}")
    else:
        print("\n⚠️  Prometheus 不可用。設定環境變數後重試：")
        print("   export PROMETHEUS_URL=http://your-prometheus:9090")
        print("\n   等效 PromQL（在 Prometheus UI 執行）：")
        print('   100 - (avg by (node)(rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)')
