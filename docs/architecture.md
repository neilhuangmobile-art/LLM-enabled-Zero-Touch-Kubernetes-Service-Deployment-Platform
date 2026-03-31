# 系統架構說明

## 整體架構

```
使用者輸入（自然語言）
         │
         ▼
┌─────────────────────┐
│   core/llama_client  │  LLM 推理（Llama 3.1 8B + LoRA）
│   + rag/retriever    │  RAG 知識注入（防幻覺）
└──────────┬──────────┘
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
│  healer/diagnose             │  LLM 根因分析
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
1. 使用者輸入 → RAG 增強 → LLM 生成 JSON
2. JSON → build_k8s_manifests() → YAML
3. YAML → security_agent → cost_agent → perf_agent → 決策
4. 決策 approve/warn → dry_run 驗證 → 部署
5. 部署後 → pod_watcher 監控 → 發現問題 → diagnose + remediate
6. 所有變更透過 GitOps 版本控管，可隨時回滾
```

## 技術棧

| 組件 | 技術 |
|------|------|
| LLM 模型 | Meta Llama 3.1 8B Instruct |
| 微調方法 | LoRA (PEFT) + 4-bit 量化 |
| K8s 叢集 | K3s（輕量，適合實驗） |
| GitOps | Argo CD |
| 監控 | Prometheus + Grafana |
| 策略執法 | Kyverno / OPA（Guardian 模組簡化實現） |
| RAG 嵌入 | sentence-transformers (all-MiniLM-L6-v2) |
| Web UI | Flask |
| Model Server | FastAPI + uvicorn |
