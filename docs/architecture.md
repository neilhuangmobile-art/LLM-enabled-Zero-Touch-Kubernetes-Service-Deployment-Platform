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
│  llama_client            │  部署小模型 Qwen2.5-3B（GPU，精度依顯存自動決定）
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
│  healer/diagnose             │  規則層 → 監控小模型 Qwen2.5-1.5B（16GB 機器 GPU，/diagnose）
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

## 多代理人部署解析（2026-11-17，`DEPLOY_PIPELINE=multiagent`，預設關閉）

```
原句 ─ 邊界預檢（多服務、副本數範圍）
     ─ ① 正規化：Gemini（失敗改用本地 Qwen3.5-4B）→ 中間 JSON（只寫使用者講過的，原句找不到根據的丟掉）
     ─ ② 意圖檢查：本地模型比對原句和 JSON，找出「講了卻沒寫進去」的，最多 3 輪
     ─ ③ 空缺檢查：推理代理補空的欄位（標 inferred，只送原句），最多 3 輪
         └ 選軟體：映像檔要在知識表裡（不在＝捏造，改問）；再用原句查 RAG 候選（BGE-M3＋reranker），本地模型從候選挑最合適的
         └ 查表補 port、資料路徑、資源（標 default；linuxserver/* 用它官方設定的 port 與資料夾）
     ─ 意圖符合檢查：系統填的值有沒有和原句矛盾（目前只檢查「要不要定時執行」，verify.CHECK_FIELDS），最多 3 輪，修不好問使用者
     ─ ④ 額外欄位對應：密碼→Secret、保存資料→PVC、指令、管理網頁 port、排程→定時執行（CronJob）；對應不到的標「不支援」
     ─ ⑤ 最後整合的格式修正：格式不合格的值用規則／本地模型修正後重新檢查，最多 3 輪；排程間隔不足 5 分鐘問使用者
     ─ 有問題 → 問題卡（全部一次問，推薦值預選，「幫我決定」）→ 回答後從同一份中間 JSON 接著跑
     ─ 沒問題 → _sanitize_deploy_payload → _prepare_deploy 原本的補全、情境、負荷檢查、三個審查代理、dry-run
```

- 程式在 `agents/pipeline/`；本地模型伺服器 `core/pipeline_model_server.py`（127.0.0.1:8766，venv `.venv-qwen35`），和 8765 的 Model Server 分開。
- 欄位來源等級：使用者講的 > 系統推的 > 預設，在 `agents/pipeline/schema.py` 統一執行。
- YAML 仍由 `gitops/manifest_writer.py` 從規格產生，模型不寫 YAML。
- 8766 沒有回應時，退回原本的 `ask_llama`。 部署單上會告知使用者。
- 定時執行（CronJob）：`gitops/manifest_writer.build_cronjob_doc` 是唯一來源（Git、部署、審查、dry-run 共用）；網站 API 在 `/api/cronjobs`；台灣時區、間隔 ≥ 5 分鐘、每次 ≤ 1 小時、不重疊執行。
- 選軟體知識表：`rag/software_catalog.json`（重抓：`scripts/build_software_catalog.py`），查詢在 `agents/pipeline/software_rag.py`。

## 客戶自帶叢集模式（2026-10-09，`CUSTOMER_CLUSTER_MODE=1`）

```
客戶瀏覽器 ──HTTPS──> Tailscale Funnel / Cloudflare Tunnel ──> web_demo（只綁 127.0.0.1，waitress）
                                                                 │ 同一套審查：orchestrator → guardian → dry-run
                                                                 ▼
                                      gitops/tenant_repo.py：commit 到 GitHub 私有 repo zt-<帳號>-<雜湊>
                                                                 ▲ 唯讀 deploy key，每 30 秒拉一次
客戶叢集 ┌ Flux（只能操作客戶專屬 namespace，Pod Security baseline）
         └ zt-reporter（唯讀）── 每 15 秒 POST ──> /agent/report ──> cluster/remote_state.py
                                                                         │
     web_demo 查詢函式遇到 zt- namespace → cluster/remote_view.py（回報 → 跟本機同格式）
     Healer 補救 → cluster/remote_ops.py → Git commit（重啟 / 恢復映像檔 / 調記憶體）
```

- 平台永遠不連進客戶叢集。所有「改變」都經過 Git，所以每個動作都有 commit 可以稽核、可以回滾。
- 引導流程：同意書 → 貼指令 → 連線檢查（`cluster/pages.py`、`cluster/onboarding.py`）。
- 本機叢集模式（管理員、或 `CUSTOMER_CLUSTER_MODE=0`）的程式碼路徑完全沒有變。
- 安全設計與實測問題見 `docs/security_review.md` 32、33 節。

## 技術棧

| 組件 | 技術 |
|------|------|
| 部署模型 | Qwen2.5-3B-Instruct（16GB 機器 bf16 GPU；小顯存 NVIDIA 4-bit，`/infer`、`/chat`） |
| 監控模型 | Qwen2.5-1.5B-Instruct（16GB 機器 bf16 GPU、小顯存 CPU，`/diagnose`） |
| 翻譯層 | Gemini API（`gemini-flash-latest`，多 key 輪換） |
| 部署引導 | RAG few-shot（`rag/deploy_index.json`，TF-IDF）＋ system prompt，不微調 |
| K8s 叢集 | Docker Desktop K8s / K3s |
| GitOps | Argo CD |
| 監控 | Prometheus + Grafana |
| 策略執法 | Kyverno / OPA（Guardian 模組簡化實現） |
| RAG 嵌入 | TF-IDF（預設）／sentence-transformers（選用，跑 CPU） |
| Web UI | Flask（:5050） |
| Model Server | FastAPI + uvicorn（:8765） |
