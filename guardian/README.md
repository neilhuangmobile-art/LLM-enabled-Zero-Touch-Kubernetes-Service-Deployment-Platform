# guardian/ — 驗證與防護層（Phase 6）

零接觸部署的安全護欄。確保 AI 生成的 YAML 在真正套用到 K8s 之前，通過多層驗證。

## 架構定位

```
ask_llama() → [guardian] → kubectl apply
                  ↑
          YAML 安全檢查 + kubectl dry-run
```

## 模組說明

| 檔案 | 功能 |
|------|------|
| `dry_run.py` | kubectl dry-run 驗證（語法 + schema） |
| `yaml_validator.py` | 安全性靜態分析（特權容器、hostNetwork 等） |
| `policy_rules.yaml` | 可編輯的策略規則定義 |
| `__init__.py` | `validate_all()` 統一驗證入口 |

## 快速使用

```python
from guardian import validate_all

result = validate_all(llm_result)   # llm_result = ask_llama() 的輸出
if result["ok"]:
    # 安全，執行部署
else:
    print(result["yaml_errors"])
    print(result["dry_run_errors"])
```

## 驗證層說明

### 層 1 — YAML 安全掃描（`yaml_validator.py`）
- 不需要叢集連線，純 Python 執行
- 對應 policy_rules.yaml 的規則
- 預設禁止：特權容器、hostNetwork、hostPID

### 層 2 — kubectl dry-run（`dry_run.py`）
- `client` 模式：只做 schema 驗證（不需叢集）
- `server` 模式：完整叢集驗證（需要可用的 K8s 叢集）
- kubectl 未安裝時自動降級，不阻斷流程

## 自訂策略規則

編輯 `policy_rules.yaml`：

```yaml
security:
  deny_privileged: true       # 禁止特權容器
  deny_host_network: true     # 禁止 hostNetwork
  warn_latest_tag: true       # latest tag 警告

resources:
  max_replicas: 50            # replicas 上限
  warn_no_limits: true        # 未設 limits 警告
```

## 未來擴充

- 與 Kyverno ClusterPolicy 整合（自動將 policy_rules.yaml 轉換）
- 加入 Trivy 映像漏洞掃描
- OPA Rego 規則支援
