# Kubernetes 安全最佳實踐

## Pod 安全設定（SecurityContext）

### 禁止特權容器
```yaml
securityContext:
  privileged: false          # 絕對不能為 true
  allowPrivilegeEscalation: false
  readOnlyRootFilesystem: true
  runAsNonRoot: true
  runAsUser: 1000
  runAsGroup: 3000
  fsGroup: 2000
  capabilities:
    drop:
    - ALL
    add:
    - NET_BIND_SERVICE  # 只在需要綁定 80/443 時加入
```

### 禁止 hostNetwork / hostPID / hostIPC
```yaml
spec:
  hostNetwork: false  # 禁止使用主機網路
  hostPID: false      # 禁止訪問主機 PID namespace
  hostIPC: false      # 禁止訪問主機 IPC namespace
```

**理由：** hostNetwork=true 讓容器繞過 Kubernetes 網路策略，能直接訪問節點網路，是重大安全風險。

## 映像安全

### 避免使用 latest tag
```yaml
# 危險：不可重現，安全掃描結果無效
image: nginx:latest

# 安全：版本固定，可重現
image: nginx:1.25.3-alpine
```

### 使用最小化基礎映像
- `alpine` — 極小（5MB），缺少除錯工具（生產推薦）
- `distroless` — Google 提供，只含執行時依賴
- `scratch` — 最小化，適合靜態二進位

### 映像掃描（Trivy）
```bash
trivy image nginx:1.25.3
trivy image --severity HIGH,CRITICAL my-app:1.0
```

## RBAC（角色存取控制）

### 最小權限原則
```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  namespace: default
  name: pod-reader
rules:
- apiGroups: [""]
  resources: ["pods"]
  verbs: ["get", "list", "watch"]
  # 不給 create, delete, patch 等寫入權限
```

### ServiceAccount 設定
```yaml
spec:
  serviceAccountName: my-app-sa
  automountServiceAccountToken: false  # 不需要 API 存取時禁用
```

## NetworkPolicy（網路隔離）

### 預設拒絕所有
```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: default-deny-all
  namespace: default
spec:
  podSelector: {}
  policyTypes:
  - Ingress
  - Egress
```

### 只允許特定流量
```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-frontend-to-backend
spec:
  podSelector:
    matchLabels:
      app: backend
  ingress:
  - from:
    - podSelector:
        matchLabels:
          app: frontend
    ports:
    - port: 8080
```

## Secret 管理

### 不要在 YAML 中放明文 Secret
```yaml
# 危險：Secret 以 base64 儲存，非加密
apiVersion: v1
kind: Secret
metadata:
  name: db-secret
type: Opaque
data:
  password: cGFzc3dvcmQ=  # base64("password") — 不安全！
```

**推薦方案：**
1. External Secrets Operator（從 AWS Secrets Manager / Vault 動態注入）
2. Sealed Secrets（加密後可安全存入 Git）
3. HashiCorp Vault Agent Injector

## Pod Security Admission（Kubernetes 1.25+）

```yaml
# 在 namespace 層級強制 baseline 安全策略
apiVersion: v1
kind: Namespace
metadata:
  name: production
  labels:
    pod-security.kubernetes.io/enforce: baseline
    pod-security.kubernetes.io/audit: restricted
    pod-security.kubernetes.io/warn: restricted
```

| 等級 | 說明 |
|------|------|
| privileged | 無限制（僅 kube-system） |
| baseline | 禁止 privileged、hostNetwork、hostPID |
| restricted | 最嚴格，強制 runAsNonRoot、seccomp 等 |

## Kyverno 策略範例

```yaml
apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata:
  name: disallow-privileged
spec:
  validationFailureAction: enforce
  rules:
  - name: deny-privileged-containers
    match:
      resources:
        kinds: [Pod]
    validate:
      message: "特權容器不被允許"
      pattern:
        spec:
          containers:
          - =(securityContext):
              =(privileged): false
```
