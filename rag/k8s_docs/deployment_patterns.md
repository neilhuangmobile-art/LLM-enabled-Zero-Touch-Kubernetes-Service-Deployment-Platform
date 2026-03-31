# Kubernetes Deployment 最佳實踐

## 基本 Deployment 結構

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: my-app
  namespace: default
  labels:
    app: my-app
    version: "1.0"
spec:
  replicas: 3
  selector:
    matchLabels:
      app: my-app
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxSurge: 1
      maxUnavailable: 0
  template:
    metadata:
      labels:
        app: my-app
        version: "1.0"
    spec:
      containers:
      - name: my-app
        image: my-app:1.0
        ports:
        - containerPort: 8080
        resources:
          requests:
            memory: "128Mi"
            cpu: "100m"
          limits:
            memory: "256Mi"
            cpu: "200m"
        readinessProbe:
          httpGet:
            path: /health
            port: 8080
          initialDelaySeconds: 10
          periodSeconds: 5
        livenessProbe:
          httpGet:
            path: /health
            port: 8080
          initialDelaySeconds: 30
          periodSeconds: 10
```

## Replicas 選擇建議

| 環境 | 建議 Replicas | 說明 |
|------|--------------|------|
| 開發 | 1 | 節省資源 |
| 測試 | 2 | 基本 HA 測試 |
| 生產 | 3+ | 保證高可用 |
| 高流量生產 | 根據 HPA 動態調整 | |

## Rolling Update 策略

- `maxUnavailable: 0` — 零停機部署（需要更多資源）
- `maxSurge: 1` — 每次多啟動 1 個新 Pod
- 適合對可用性要求高的服務

## Service 類型選擇

| 類型 | 適用場景 |
|------|---------|
| ClusterIP | 叢集內部通信（預設） |
| NodePort | 開發/測試環境對外暴露 |
| LoadBalancer | 雲端環境生產服務 |
| ExternalName | DNS 別名，指向外部服務 |

## 常見 Image Tag 策略

```yaml
# 不推薦（無法追蹤版本）
image: nginx:latest

# 推薦（固定版本，可重現）
image: nginx:1.25.3

# 推薦（SHA256 固定，最安全）
image: nginx@sha256:abc123...
```

## 健康檢查配置

### readinessProbe（就緒探針）
- 決定 Pod 是否加入 Service 的 Endpoints
- 探針失敗 → 暫時從 Service 移除，不會重啟
- 適合：等待應用程式完成初始化

### livenessProbe（存活探針）
- 探針失敗 → kubelet 重啟容器
- 必須確保探針閾值寬鬆，避免誤殺正常啟動的容器
- `initialDelaySeconds` 設定應大於應用程式啟動時間

### startupProbe（啟動探針）
- Kubernetes 1.18+
- 保護慢啟動應用程式，在啟動期間禁用 liveness/readiness
- 適合：JVM 應用、資料庫初始化等

## HPA（Horizontal Pod Autoscaler）

```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: my-app-hpa
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: my-app
  minReplicas: 2
  maxReplicas: 10
  metrics:
  - type: Resource
    resource:
      name: cpu
      target:
        type: Utilization
        averageUtilization: 70
  - type: Resource
    resource:
      name: memory
      target:
        type: Utilization
        averageUtilization: 80
```

## ConfigMap 與 Secret 掛載

```yaml
# ConfigMap 作為環境變數
envFrom:
- configMapRef:
    name: app-config

# Secret 作為環境變數
envFrom:
- secretRef:
    name: app-secrets

# ConfigMap 作為檔案掛載
volumeMounts:
- name: config-volume
  mountPath: /etc/config
volumes:
- name: config-volume
  configMap:
    name: app-config
```
