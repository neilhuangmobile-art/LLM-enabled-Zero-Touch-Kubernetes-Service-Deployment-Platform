# Kubernetes 資源管理指南

## requests 與 limits 的區別

| 欄位 | 說明 | 影響 |
|------|------|------|
| `requests` | Scheduler 保證分配的資源 | 影響 Pod 調度決策 |
| `limits` | 容器可使用的最大資源 | 超過 CPU limits → throttled；超過 Memory limits → OOMKilled |

**最佳實踐：**
- 必須同時設定 requests 和 limits
- Memory limits 與 requests 建議相同（Guaranteed QoS）
- CPU limits 可比 requests 高 2-4 倍（Burstable QoS）

## 常見工作負載資源建議

### Web 服務（Node.js / Python Flask）
```yaml
resources:
  requests:
    memory: "128Mi"
    cpu: "100m"
  limits:
    memory: "256Mi"
    cpu: "500m"
```

### Java 應用（Spring Boot）
```yaml
resources:
  requests:
    memory: "512Mi"
    cpu: "250m"
  limits:
    memory: "1Gi"
    cpu: "1000m"
```

### 資料庫（PostgreSQL / MySQL）
```yaml
resources:
  requests:
    memory: "256Mi"
    cpu: "250m"
  limits:
    memory: "1Gi"
    cpu: "1000m"
```

### LLM 推論服務（如 Llama 3.1 8B）
- 記憶體公式：參數量（B） × 2 GB = 基礎需求
- 8B 模型（FP16）：最低 16GB GPU VRAM
- 8B 模型（4-bit 量化）：最低 6GB GPU VRAM
- 額外預留 20-30% 作為 KV Cache

```yaml
resources:
  requests:
    memory: "8Gi"
    cpu: "2"
    nvidia.com/gpu: "1"
  limits:
    memory: "16Gi"
    cpu: "4"
    nvidia.com/gpu: "1"
```

## CPU 資源單位

- `1` = 1 vCPU / Core
- `0.5` = 500m（millicores）= 0.5 vCPU
- `100m` = 0.1 vCPU（最小建議值）

## Memory 資源單位

| 後綴 | 說明 | 等效 |
|------|------|------|
| Ki | Kibibyte (1024 bytes) | |
| Mi | Mebibyte | 1048576 bytes |
| Gi | Gibibyte | 1073741824 bytes |
| Ti | Tebibyte | |
| K / M / G | SI 單位（少用） | |

## ResourceQuota（命名空間配額）

```yaml
apiVersion: v1
kind: ResourceQuota
metadata:
  name: compute-quota
  namespace: production
spec:
  hard:
    requests.cpu: "4"
    requests.memory: "8Gi"
    limits.cpu: "8"
    limits.memory: "16Gi"
    pods: "20"
```

## LimitRange（Pod 預設值）

```yaml
apiVersion: v1
kind: LimitRange
metadata:
  name: default-limits
  namespace: default
spec:
  limits:
  - type: Container
    default:
      memory: "256Mi"
      cpu: "200m"
    defaultRequest:
      memory: "128Mi"
      cpu: "100m"
    max:
      memory: "2Gi"
      cpu: "2"
    min:
      memory: "64Mi"
      cpu: "50m"
```

## QoS 等級

| 等級 | 條件 | 被驅逐優先度 |
|------|------|------------|
| Guaranteed | requests == limits（CPU+Memory） | 最低（最後被驅逐） |
| Burstable | requests < limits | 中等 |
| BestEffort | 未設定任何 requests/limits | 最高（最先被驅逐） |

**建議：** 生產環境使用 Guaranteed QoS，確保資源穩定性。
