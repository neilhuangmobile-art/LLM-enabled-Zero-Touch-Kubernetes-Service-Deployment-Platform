# healer/ — 自主 SRE 自癒模組（Phase 6）

監聽 K8s Pod 異常，自動診斷根因並執行補救動作，實現閉環自治。

## 架構流程

```
K8s Cluster
    │
    ▼
pod_watcher.py          ← 偵測異常 Pod（CrashLoopBackOff、OOMKilled 等）
    │  收集日誌 + 事件
    ▼
diagnose.py             ← 規則型（毫秒）+ LLM 深度分析
    │  輸出 action code
    ▼
remediate.py            ← 執行具體補救（K8s API + kubectl）
    │
    ▼
 ✅ Pod 恢復正常
```

## 模組說明

| 檔案 | 功能 |
|------|------|
| `pod_watcher.py` | 監聽 Pod 狀態，偵測 7 種異常類型 |
| `diagnose.py` | 兩層診斷：規則型 + LLM 根因分析 |
| `remediate.py` | 9 種補救動作執行器 |
| `__init__.py` | `heal_once()` 統一入口 |

## 快速使用

### Python API

```python
from healer import heal_once

# 掃描並自動補救（dry_run=True 僅模擬）
results = heal_once(namespace="default", dry_run=False)

for r in results:
    print(r["diagnosis"]["root_cause"])
    print(r["remediation"]["message"])
```

### CLI

```bash
# 掃描一次（不自動補救）
python healer/pod_watcher.py --once -n default

# 持續監聽 + 自動補救
python healer/pod_watcher.py --watch --heal -n production

# 測試診斷邏輯（不需要 K8s）
python healer/diagnose.py

# 測試補救邏輯（dry-run 模式）
python healer/remediate.py
```

## 支援的異常類型（pod_watcher）

| 異常狀態 | 說明 |
|----------|------|
| `CrashLoopBackOff` | 容器反覆崩潰 |
| `OOMKilled` | 記憶體不足被殺 |
| `ImagePullBackOff` | 映像拉取失敗 |
| `ErrImagePull` | 映像不存在或無權限 |
| `Error` | 容器以非零狀態碼退出 |
| `RunContainerError` | 容器無法啟動 |
| `CreateContainerConfigError` | 容器設定錯誤 |

## 補救動作清單（remediate）

| Action Code | 觸發條件 | 操作 |
|-------------|----------|------|
| `analyze_logs` | CrashLoopBackOff | 取崩潰日誌 + 刪除 Pod（Deployment 自動重建） |
| `fix_image` | ImagePullBackOff | `kubectl rollout undo deployment/<name>` |
| `increase_memory` | OOMKilled | 將 `resources.limits.memory` 翻倍 |
| `fix_probe` | 探針失敗 | `initialDelaySeconds` +30s（上限 120s） |
| `check_dependencies` | 連線拒絕 | 列出 Service 與 Endpoint 狀態 |
| `fix_permissions` | 權限不足 | 列出 RBAC RoleBinding |
| `create_config` | ConfigMap/Secret 缺失 | 列出現有 ConfigMap/Secret |
| `fix_port_conflict` | 連接埠衝突 | 提供排查指引 |
| `manual_inspect` | 未知原因 | 記錄診斷結果，提供人工指引 |

## 診斷層說明（diagnose）

### 層 1 — 規則型（毫秒級）
不需 LLM，正規表示式快速比對已知錯誤模式：
- OOMKilled、ImagePullBackOff、CrashLoopBackOff
- ECONNREFUSED、permission denied、ConfigMap not found
- probe failed、port already in use

### 層 2 — LLM 深度分析
需要 `model_server.py` 在線。將日誌、事件、錯誤狀態傳給 LLaMA，
要求回傳 JSON 格式的根因分析（root_cause、severity、action、suggestion）。

```
confidence = "rule"    # 規則型命中
confidence = "llm"     # LLM 分析
confidence = "unknown" # 兩層都無法識別
```

## 環境需求

```bash
pip install kubernetes   # K8s Python client
# kubectl 需在 PATH 中（供 fix_image 使用）
```

無 K8s 環境時，pod_watcher 自動進入模擬模式，remediate 以 dry_run 邏輯處理。
