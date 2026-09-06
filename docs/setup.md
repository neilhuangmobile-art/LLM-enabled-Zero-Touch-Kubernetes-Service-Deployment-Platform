# 環境建置指南

## 系統需求

| 項目 | 最低需求 |
|------|---------|
| Python | 3.9+ |
| CUDA | 11.8+（部署模型 4-bit GPU 推論用） |
| VRAM | 4GB+（部署模型 Qwen2.5-3B 4-bit 約佔 2.2GB） |
| RAM | 16GB+（監控模型 Qwen2.5-1.5B 跑 CPU，約佔 6GB） |
| 磁碟 | 15GB+（Qwen2.5-3B ≈ 6GB + 1.5B ≈ 3GB） |
| OS | Windows 10/11、Ubuntu 20.04+ |

> 2026-09-06 起改用小模型雙軌（部署 Qwen2.5-3B GPU、監控 Qwen2.5-1.5B CPU），
> 不再需要 8GB+ VRAM 跑 Llama-3.1-8B。

---

## 1. 安裝 Python 套件

```bash
pip install -r requirements.txt
```

GPU 環境需另外安裝對應 CUDA 版本的 PyTorch：

```bash
# CUDA 11.8
pip install torch==2.1.0+cu118 --index-url https://download.pytorch.org/whl/cu118

# CUDA 12.1
pip install torch==2.1.0+cu121 --index-url https://download.pytorch.org/whl/cu121
```

---

## 2. 設定環境變數

```bash
cp .env.example .env
# 編輯 .env：
#   GEMINI_API_KEYS=key1,key2,...   翻譯層（可放多把 key 自動輪換湊額度）
#   USE_LLM_NORMALIZE=1             預設開啟翻譯層
```

---

## 3. 下載模型（首次執行）

Qwen2.5 系列**不需要**登入或接受授權：

```bash
hf download Qwen/Qwen2.5-3B-Instruct     # 部署模型，約 6GB
hf download Qwen/Qwen2.5-1.5B-Instruct   # 監控模型，約 3GB
```

不下載也可以，`core/model_server.py` 首次啟動時 transformers 會自動抓。

---

## 4. 訓練 LoRA 微調模型（選用，目前不啟用）

> 2026-09-06 起部署模型改走「Gemini 正規化 → RAG few-shot → Qwen2.5-3B 生成」，
> **預設不微調**。舊的 8B LoRA（`llama3_k8s_lora_results/`）已棄用（架構對不上 Qwen）。
> 若之後要在 Qwen2.5-3B 上重訓小 LoRA，訓練完把權重路徑填進 `.env` 的 `DEPLOY_ADAPTER_PATH`。

若要自行訓練，依序執行：

```bash
# 生成訓練資料
python generate_finetune_data.py

# 清洗資料集
python clean_dataset.py

# 開始 LoRA 訓練（需 GPU）
python train_local.py
```

訓練完成後權重儲存於 `llama3_k8s_lora_results/`。

---

## 5. 啟動 Model Server

```bash
python core/model_server.py
```

首次啟動需等待模型載入（約 1-2 分鐘），之後每次呼叫幾乎即時。

驗證：

```bash
curl http://127.0.0.1:8765/health
# {"status":"ok","deploy_loaded":true,"monitor_loaded":true}
```

端點：`/infer`（部署 JSON）、`/chat`（一般問答，共用部署模型）、`/diagnose`（healer 根因分析，監控模型 CPU）。

---

## 6. 執行部署

**CLI 模式：**
```bash
python 0_touch_generate_pods.py
```

**Web UI 模式：**
```bash
python web_demo.py
# 開啟瀏覽器：http://localhost:5050
```

---

## 7. Kubernetes 設定

確認 kubeconfig 正確：

```bash
kubectl get nodes
```

若使用 Docker Desktop 或 minikube，預設 kubeconfig 路徑為 `~/.kube/config`。

---

## 8. 選用功能

### RAG 知識庫索引

```bash
python rag/build_index.py --rebuild --deploy
```

預設用 TF-IDF（零額外相依），同時建立：
- `rag/index.json`：k8s 散文知識（給 `/chat`）
- `rag/deploy_index.json`：部署範例 `input → JSON`（給 `/infer` 當 few-shot）

想要語意向量升級再 `pip install sentence-transformers chromadb`
（embedding 預設跑 CPU，`RAG_EMBED_DEVICE=cuda` 可改）。

### GitOps

```bash
pip install gitpython
```

在 `.env` 設定 `GITOPS_REPO_PATH`，部署時自動 commit YAML。

### Prometheus 整合

在 `.env` 設定 `PROMETHEUS_URL`，`observability/prometheus_client.py` 會使用該 URL。
