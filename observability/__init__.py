# observability/__init__.py
# 可觀測性模組 - 對外介面

from observability.prometheus_client import PrometheusClient, get_pod_metrics


def cluster_health_summary(
    namespace : str = "",
    prom_url  : str = "",
) -> dict:
    """
    取得叢集整體健康摘要，供 LLM 代理決策使用。

    Returns:
        {
            "prometheus_ok"  : bool,
            "running_pods"   : int | None,
            "node_cpu_pct"   : dict | None,   # {node: pct}
            "top_cpu_pods"   : list | None,
            "top_mem_pods"   : list | None,
        }
    """
    kwargs = {"url": prom_url} if prom_url else {}
    client = PrometheusClient(**kwargs)
    alive  = client.is_alive()

    result = {
        "prometheus_ok": alive,
        "running_pods" : None,
        "node_cpu_pct" : None,
        "top_cpu_pods" : None,
        "top_mem_pods" : None,
    }

    if not alive:
        return result

    result["running_pods"] = client.namespace_pod_count(namespace) if namespace \
                             else client.query("count(kube_pod_status_phase{phase='Running'})")

    result["node_cpu_pct"] = client.node_cpu_percent()

    # Top 5 CPU 使用最高的 Pod
    cpu_results = client.query(
        f'topk(5, sum by (pod, namespace) '
        f'(rate(container_cpu_usage_seconds_total{{container!=""'
        + (f',namespace="{namespace}"' if namespace else "")
        + f'}}[5m])))'
    )
    if cpu_results:
        result["top_cpu_pods"] = [
            {
                "pod"      : r["metric"].get("pod", "?"),
                "namespace": r["metric"].get("namespace", "?"),
                "cpu_cores": round(r["value"], 3),
            }
            for r in cpu_results
        ]

    # Top 5 記憶體使用最高的 Pod
    mem_results = client.query(
        f'topk(5, sum by (pod, namespace) '
        f'(container_memory_working_set_bytes{{container!=""'
        + (f',namespace="{namespace}"' if namespace else "")
        + f'}}))'
    )
    if mem_results:
        result["top_mem_pods"] = [
            {
                "pod"      : r["metric"].get("pod", "?"),
                "namespace": r["metric"].get("namespace", "?"),
                "memory_mi": round(r["value"] / 1024 / 1024, 1),
            }
            for r in mem_results
        ]

    return result
