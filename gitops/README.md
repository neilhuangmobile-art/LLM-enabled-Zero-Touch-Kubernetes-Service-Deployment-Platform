# gitops/ — GitOps 部署模組（Phase 7）

將 LLM 生成的部署描述寫入 Git，並透過 Argo CD 同步到 K8s 叢集。

## 架構流程

```
ask_llama()
    │  llm_result dict
    ▼
manifest_writer.py   ← 生成 Deployment + Service YAML，git commit
    │
    ▼
argocd_sync.py       ← 觸發 Argo CD sync，輪詢等待健康
    │
    ▼
 K8s Cluster（目標狀態）

         ← 發現問題時 →

rollback.py          ← 自動回滾（Argo CD → Git revert → kubectl undo）
```

## 模組說明

| 檔案 | 功能 |
|------|------|
| `manifest_writer.py` | LLM 結果 → YAML 檔案 + git commit |
| `argocd_sync.py` | Argo CD REST API 客戶端（sync / status / list） |
| `rollback.py` | 三層回滾策略（argocd / git / kubectl） |
| `__init__.py` | `deploy()` 一鍵完整部署流程 |

## 快速使用

```python
from gitops import deploy

result = deploy(
    llm_result={"app_name": "my-app", "image": "nginx:1.25", "replicas": 2, "port": 80},
    namespace="production",
    repo_path="/path/to/gitops-repo",
    sync=True,      # 觸發 Argo CD 同步
    dry_run=False,
)
print(result["message"])
```

### 單獨使用各模組

```python
from gitops.manifest_writer import write_manifest
from gitops.argocd_sync     import ArgocdClient, sync_app
from gitops.rollback        import rollback, get_rollback_history

# 只寫 manifest
write_manifest(llm_result, repo_path=".", namespace="default")

# 查詢 Argo CD 狀態
client = ArgocdClient(server="https://argocd.example.com", token="xxx")
status = client.get_status("my-app")

# 回滾（auto = argocd → git → kubectl）
rollback("my-app", namespace="production", strategy="auto", dry_run=True)

# 查看部署歷史
history = get_rollback_history("my-app", namespace="production")
```

## 環境設定

```bash
# Argo CD 連線（argocd_sync.py 使用）
export ARGOCD_SERVER=https://your-argocd-server
export ARGOCD_TOKEN=your-api-token
export ARGOCD_INSECURE=true   # 自簽憑證時設 true

# 安裝相依套件
pip install gitpython pyyaml requests
```

## Manifest 目錄結構

`write_manifest()` 會在倉庫中建立以下結構：

```
manifests/
└── <namespace>/
    └── <app_name>/
        ├── deployment.yaml
        └── service.yaml
```

每次部署自動 git commit，提交訊息格式：
```
gitops: deploy <app_name> to <namespace> [2025-01-01T00:00:00Z]
```

## 回滾策略說明

| 策略 | 條件 | 操作 |
|------|------|------|
| `argocd` | Argo CD 可連線 | 呼叫 rollback API（回到上一 revision） |
| `git` | GitPython 可用 | 還原 manifest 目錄到上一個 commit |
| `kubectl` | kubectl 在 PATH | `kubectl rollout undo deployment/<name>` |
| `auto` | 任何環境 | 依序嘗試上述三種，第一個成功即停止 |
