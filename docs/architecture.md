# 系統架構說明

## 整體架構

```
使用者輸入（自然語言，中/英）
         │
         ▼
┌──────────────────────────┐
│  core/gemini_client       │  翻譯層：正規化成 ### DeploySpec 區塊
│  （多 key 輪換，優先執行）  │  只翻譯、不猜使用者沒講的欄位
└──────────┬───────────────┘
           │ DeploySpec 齊全 → 直接解析（零 GPU）
           │ 不齊全 ↓
┌──────────────────────────┐
│  llama_client            │  部署小模型 Qwen2.5-3B（4-bit GPU）
│  + rag deploy_index      │  RAG 撈相似「input→JSON」範例當 few-shot
└──────────┬───────────────┘
           │ JSON（pods, image, app_name, port, memory）
           ▼
┌─────────────────────────────────────────────┐
│              agents/orchestrator             │
│  ┌────────────┐ ┌───────────┐ ┌──────────┐  │
│  │security_ag.│ │ cost_ag.  │ │ perf_ag. │  │
│  └────────────┘ └───────────┘ └──────────┘  │
│  決策：approve / warn / block                │
└──────────┬──────────────────────────────────┘
           │ 通過
           ▼
┌─────────────────────┐
│  guardian/dry_run    │  kubectl dry-run 驗證
│  guardian/yaml_valid │  YAML 安全政策檢查
└──────────┬──────────┘
           │ 驗證通過
           ▼
┌─────────────────────┐
│  gitops/manifest_wri │  寫入 Git 倉庫
│  gitops/argocd_sync  │  觸發 Argo CD 同步
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐
│   Kubernetes 叢集    │
│   (K3s / K8s)        │
└──────────┬──────────┘
           │ 持續監控
           ▼
┌─────────────────────────────┐
│  healer/pod_watcher          │  監聽 CrashLoopBackOff 等
│  healer/diagnose             │  規則層 → 監控小模型 Qwen2.5-1.5B（CPU，/diagnose）
│  healer/remediate            │  自動補救
│  observability/prometheus    │  Prometheus 指標
└─────────────────────────────┘
```

## 模組說明

| 目錄 | 功能 | 階段 |
|------|------|------|
| `core/` | LLM 推理、設定、Model Server | Phase 1 |
| `training/` | LoRA 微調、資料生成、評估 | Phase 2-3 |
| `guardian/` | YAML 驗證、安全政策 | Phase 6 |
| `healer/` | Pod 監控、LLM 診斷、自癒 | Phase 6 |
| `gitops/` | ArgoCD 整合、版本控管 | Phase 7 |
| `observability/` | Prometheus、Grafana | Phase 7 |
| `rag/` | 知識庫向量索引、RAG 查詢 | Phase 8 |
| `agents/` | 安全/成本/效能多代理協作 | Phase 8 |

## 資料流

```
1. 使用者輸入 → Gemini 翻譯層正規化成 DeploySpec → 齊全就直接解析；不齊全交小模型 + RAG few-shot → JSON
2. JSON → build_deploy()/k8s_deploy() 用 Python 樣板 → YAML（模型不直接寫 YAML）
3. manifest dict → security_agent → cost_agent → perf_agent → 決策
4. 決策 approve/warn → dry_run 驗證 → 部署
5. 部署後 → pod_watcher 監控 → 規則層 / 監控小模型 diagnose + remediate
6. 所有變更透過 GitOps 版本控管，可隨時回滾
```

## 技術棧

| 組件 | 技術 |
|------|------|
| 部署模型 | Qwen2.5-3B-Instruct（4-bit GPU，`/infer`、`/chat`） |
| 監控模型 | Qwen2.5-1.5B-Instruct（CPU，`/diagnose`） |
| 翻譯層 | Gemini API（`gemini-flash-latest`，多 key 輪換） |
| 部署引導 | RAG few-shot（`rag/deploy_index.json`，TF-IDF）＋ system prompt，不微調 |
| K8s 叢集 | Docker Desktop K8s / K3s |
| GitOps | Argo CD |
| 監控 | Prometheus + Grafana |
| 策略執法 | Kyverno / OPA（Guardian 模組簡化實現） |
| RAG 嵌入 | TF-IDF（預設）／sentence-transformers（選用，跑 CPU） |
| Web UI | Flask（:5050） |
| Model Server | FastAPI + uvicorn（:8765） |
