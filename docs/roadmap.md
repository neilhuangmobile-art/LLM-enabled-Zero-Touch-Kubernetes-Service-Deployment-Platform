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
- [x] `rag/k8s_docs/` — K8s 知識文件庫（4 份文件）
- [x] `rag/build_index.py` — 建立向量索引（語意 + TF-IDF 後備）
- [x] `rag/retriever.py` — 查詢相關文件，augment_prompt() 介面
- [x] `agents/security_agent.py` — 安全掃描代理（安全分數 0-100）
- [x] `agents/cost_agent.py` — 成本分析代理（月費估算 + 資源建議）
- [x] `agents/perf_agent.py` — 效能代理（HPA YAML 產生 + probe 建議）
- [x] `agents/orchestrator.py` — 多代理協調器（approve/warn/block 決策）

---

## 建議後續工作

### 整合 RAG 到主部署流程
將 `rag/retriever.augment_prompt()` 整合進 `llama_client.ask_llama()`：
```python
# llama_client.py
from rag.retriever import augment_prompt
enhanced = augment_prompt(user_input)
result = ask_llama(enhanced)
```

### 整合 Orchestrator 到部署流程
在 `0_touch_generate_pods.py` 中，LLM 生成 YAML 後加入代理評估：
```python
from agents.orchestrator import orchestrate
result = orchestrate(manifest, save_report=True)
if result["decision"] == "block":
    # 拒絕部署，提示用戶修復
```

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

---

## 對齊研究報告的四個開發階段

| 階段 | 研究報告目標 | 現狀 |
|------|------------|------|
| Phase 1 | 基礎自動化與 GitOps | ✅ 完成 |
| Phase 2 | AI 診斷與意圖翻譯 | ✅ 完成 |
| Phase 3 | 自主補救與安全沙盒 | ✅ 完成（沙盒待實現） |
| Phase 4 | FinOps 與效能感知 | ✅ cost_agent + perf_agent 完成 |
