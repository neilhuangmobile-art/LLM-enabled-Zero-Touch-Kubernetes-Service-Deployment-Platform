# 開發路線圖

## 已完成

### Phase 1-3：核心 LLM + 微調
- [x] `llama_client.py` — Llama 3.1 推理模組，HTTP first + 本地後備
- [x] `core/model_server.py` — FastAPI 常駐 Model Server（避免每次重載）
- [x] `core/config.py` — 統一路徑設定
- [x] `0_touch_generate_pods.py` — 零接觸部署 CLI
- [x] `web_demo.py` — Web Dashboard
- [x] `training/` — LoRA 微調訓練流程（generate, clean, train, eval）
- [x] `dataset/finetune_samples.jsonl` — 訓練資料集（持續累積）

### Phase 6：驗證與防護層
- [x] `guardian/dry_run.py` — kubectl dry-run 自動驗證
- [x] `guardian/yaml_validator.py` — YAML 格式與安全檢查
- [x] `guardian/policy_rules.yaml` — 自訂部署規則

### Phase 6：自癒模組
- [x] `healer/pod_watcher.py` — 監聽 Pod 事件
- [x] `healer/diagnose.py` — LLM 根因分析（規則 + LLM 雙層）
- [x] `healer/remediate.py` — 自動補救執行器

### Phase 7：GitOps + 可觀測性
- [x] `gitops/manifest_writer.py` — YAML 推送至 Git
- [x] `gitops/argocd_sync.py` — Argo CD 同步觸發
- [x] `gitops/rollback.py` — 版本回滾
- [x] `observability/prometheus_client.py` — Prometheus 指標抓取
- [x] `observability/grafana_dashboard.json` — Grafana Dashboard
- [x] `observability/alert_rules.yaml` — 告警規則

### Phase 8：RAG + 多代理
- [x] `rag/k8s_docs/` — K8s 知識文件庫（5 份文件，含英文版 `zerotouch_user_guide.md`）+ `rag/prompt_qa_seed.jsonl` 人工精選 QA 種子資料
- [x] `rag/build_index.py` — 建立向量索引（chromadb 語意搜尋 + TF-IDF 後備，已安裝 chromadb 並確認實際跑語意搜尋而非退化到 TF-IDF）
- [x] `rag/retriever.py` — 查詢相關文件，augment_prompt() / augment_prompt_ex() 介面
- [x] RAG 已接進主流程：`llama_client.py`（`_try_augment_with_rag` / `_try_augment_with_rag_ex`）於 chat 與 JSON 推論前注入知識庫內容
- [x] `agents/security_agent.py` — 安全掃描代理（安全分數 0-100）
- [x] `agents/cost_agent.py` — 成本分析代理（月費估算 + 資源建議）
- [x] `agents/perf_agent.py` — 效能代理（HPA YAML 產生 + probe 建議）
- [x] `agents/orchestrator.py` — 多代理協調器（approve/warn/block 決策）
- [x] Orchestrator 已接進兩條真實部署路徑並會實際阻擋部署：`0_touch_generate_pods.py`（CLI，block 直接中止、warn 需手動確認）與 `web_demo.py` 的 `_review_deployment` → `_prepare_deploy`（Web，guardian → orchestrator → dry-run 三層任一 block 就擋下 `/api/deploy/parse`）。已於 2026-08-03 用合成 manifest（benign / privileged+hostNetwork / 超額 replicas）端對端驗證 block/warn/approve 邏輯正確。

---

## 建議後續工作

> 2026-08-03 更新：以下兩項原本列為待辦，經追查程式碼確認其實已經完成並接上真實部署路徑，移到「已完成」；本文件先前沒跟上實際進度，已修正。

### 擴充 RAG 知識庫
- 加入更多 K8s 官方文件（可爬取 kubernetes.io/docs）
- 加入組織內部 Runbook（如常見故障解決手冊）
- 使用 `python rag/build_index.py --rebuild` 重建索引

### 連接真實 Prometheus
修改 `observability/prometheus_client.py` 的 `PROMETHEUS_URL`，
連接到實際 Prometheus 後，`cost_agent` 和 `perf_agent` 可取得即時指標。

### Agent Sandbox（進階）
實現 Kubernetes SIG-Apps 的 Agent Sandbox CRD，
利用 gVisor 隔離 LLM 生成的不受信任程式碼。

### 2026-09-06：換小模型 + 翻譯層優先 + 雙模型（部署/監控）+ 修 OOM

專案搬到本機 Windows（RTX 3060 Laptop，6GB VRAM），Llama-3.1-8B 4-bit 佔 97% 顯存、
長 context 會 OOM。改動：

- **部署模型 → Qwen/Qwen2.5-3B-Instruct**（4-bit GPU，約 2.2GB），**監控模型 → Qwen/Qwen2.5-1.5B-Instruct**（CPU）。
  `core/model_server.py` 一個 process 載兩顆、各一把 lock；新增 `/diagnose` 端點。
- **8B LoRA 棄用**（`llama3_k8s_lora_results/` 架構對不上 Qwen）。部署改走
  「Gemini 正規化 → RAG few-shot（`rag/deploy_index.json`，1802 筆 input→JSON）→ 3B 生成」，**不微調**。
  之後若要在 3B 上重訓小 LoRA，權重路徑填 `.env` 的 `DEPLOY_ADAPTER_PATH`。
- **翻譯層改為優先執行**：`USE_LLM_NORMALIZE=1` 預設開，部署/聊天路徑一律先過 Gemini
  正規化成 `### DeploySpec` 區塊。`core/gemini_client.py` 支援 `GEMINI_API_KEYS` 多 key
  逗號分隔，遇 429/RESOURCE_EXHAUSTED 自動輪換。
- **prompt injection 的 `### User` 結構性風險順帶消解**：改用 `tokenizer.apply_chat_template()`
  （Qwen ChatML），角色邊界是特殊 token，使用者字串偽造不出來。舊的 `STOP_MARKERS` /
  `disable_adapter()` 邏輯一併移除。
- **`eval_*.py` 已遷移**（2026-09-06）：5 個離線 benchmark（eval_model / eval_hard / eval_simple /
  eval_speed / eval_baseline）改成從 `core.config` 引用模型設定、ChatML prompt、移除 8B LoRA。
  smoke 測過可跑；完整 100 筆 benchmark 數字尚未重跑（舊的 8B 數字還在 `reports/`，跟 3B 不能直接比）。
- **監控模型 CPU 延遲**：1.5B 在 CPU 上單次診斷約 5~15 秒；healer 規則層仍先跑，只有規則
  比不到才呼叫模型，可接受。

### 技術債：`/api/deploy` 沒有 idempotency 保證，重試可能造成重複 GitOps commit

已知限制：`gitops.manifest_writer.write_manifest()` 每次呼叫都會產生新的 commit，`/api/deploy`
本身沒有任何機制防止同一個部署請求被重複送出兩次（例如網路逾時後使用者手動重試、或前端重複
點擊）。過去這個問題觸發機率接近零，因為 K8s 部署失敗一直是無聲的（見下面「部署顯示成功但
Pods/Deployments 沒有實際建立」的修復說明）——使用者根本沒機會看到失敗、也就不會想重試。

**2026-08-07 這條技術債的優先度應該調高**：`/api/deploy` 已經改成同步呼叫 `k8s_deploy()`
並把真實成敗回傳給前端（`web_demo.py:2802-2809`），使用者現在第一次能真的看到「K8s 部署失敗」
的訊息，重試會變成自然而然的下一步動作——idempotency 缺口的觸發機率從「幾乎零」變成「使用者
的第一直覺反應」。這次沒有一併修，之後排優先順序時要記得這個連動關係。

---

## 對齊研究報告的四個開發階段

| 階段 | 研究報告目標 | 現狀 |
|------|------------|------|
| Phase 1 | 基礎自動化與 GitOps | ✅ 完成 |
| Phase 2 | AI 診斷與意圖翻譯 | ✅ 完成 |
| Phase 3 | 自主補救與安全沙盒 | ✅ 完成（沙盒待實現） |
| Phase 4 | FinOps 與效能感知 | ✅ cost_agent + perf_agent 完成 |
