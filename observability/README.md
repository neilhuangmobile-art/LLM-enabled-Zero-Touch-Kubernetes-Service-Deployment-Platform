# observability/ — 可觀測性模組（Phase 8）

Prometheus 指標查詢 + Grafana 儀表板 + 告警規則，讓平台具備資料驅動的決策能力。

## 模組說明

| 檔案 | 功能 |
|------|------|
| `prometheus_client.py` | Prometheus HTTP API 查詢客戶端 |
| `alert_rules.yaml` | Prometheus Alerting Rules（K8s CRD 格式） |
| `grafana_dashboard.json` | Grafana 儀表板（可直接匯入） |
| `__init__.py` | `cluster_health_summary()` 彙整入口 |

## 快速使用

```python
from observability import cluster_health_summary
from observability.prometheus_client import PrometheusClient

# 叢集健康摘要（供 LLM 代理參考）
summary = cluster_health_summary(namespace="production")
print(summary["node_cpu_pct"])   # {"node-1": 45.2, "node-2": 30.1}
print(summary["top_cpu_pods"])   # [{"pod": "...", "cpu_cores": 0.85}, ...]

# 個別指標查詢
client = PrometheusClient("http://prometheus.monitoring:9090")
print(client.pod_cpu_usage("my-pod", "default"))        # 0.35 (cores)
print(client.pod_memory_usage_mi("my-pod", "default"))  # 128.5 (MiB)
print(client.pod_restart_count("my-pod", "default"))    # 3
print(client.deployment_available_replicas("my-deploy", "default"))  # 2

# 自訂 PromQL
results = client.query('histogram_quantile(0.95, rate(http_request_duration_seconds_bucket[5m]))')
```

## Grafana 儀表板匯入

1. 開啟 Grafana → Dashboards → Import
2. 上傳 `observability/grafana_dashboard.json`
3. 選擇 Prometheus data source
4. 儀表板包含：
   - 叢集概覽（CrashLoop / OOM / ImagePull 數量統計）
   - Pod CPU / 記憶體趨勢（Top 10）
   - LLM Model Server 效能（推論延遲 P50/P95/P99、QPS）
   - Pod 重啟次數趨勢

## 告警規則部署

```bash
# 需要 Prometheus Operator（kube-prometheus-stack）
kubectl apply -f observability/alert_rules.yaml

# 確認規則已載入
kubectl get prometheusrule -n monitoring
```

### 告警清單

| 告警名稱 | 嚴重度 | 觸發條件 |
|----------|--------|---------|
| PodCrashLooping | critical | 15 分鐘內重啟 > 0.5 次/分 |
| PodOOMKilled | warning | 容器因 OOM 被終止 |
| PodNotReady | warning | Pod NotReady 超過 10 分鐘 |
| PodImagePullFailed | critical | ImagePullBackOff 超過 2 分鐘 |
| HighCPUUsage | warning | Pod CPU > 0.9 核持續 15 分鐘 |
| HighMemoryUsage | warning | 記憶體使用率 > 85% limits |
| NodeDiskPressure | critical | Node DiskPressure 狀態 |
| DeploymentReplicasMismatch | warning | 副本數不符超過 10 分鐘 |
| DeploymentRolloutStuck | critical | 滾動更新超過 15 分鐘未完成 |
| ModelServerDown | critical | Model Server 離線超過 2 分鐘 |
| LLMInferenceLatencyHigh | warning | P95 推論延遲 > 30 秒 |

## 環境設定

```bash
export PROMETHEUS_URL=http://your-prometheus:9090

pip install requests   # Prometheus HTTP API 客戶端相依
```

Prometheus 不可用時，所有查詢回傳 `None`，平台自動降級為純規則模式，不影響核心功能。
