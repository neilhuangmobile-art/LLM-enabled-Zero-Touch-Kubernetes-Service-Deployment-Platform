# 環境建置指南

## 系統需求

| 項目 | 最低需求 |
|------|---------|
| Python | 3.9+ |
| CUDA | 11.8+（GPU 推論必要） |
| VRAM | 8GB+（4-bit 量化） |
| RAM | 16GB+ |
| 磁碟 | 20GB+（模型權重） |
| OS | Windows 10/11、Ubuntu 20.04+ |

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
# 編輯 .env，填入 HF_TOKEN 等設定
```

---

## 3. 下載模型（首次執行）

LLaMA-3.1-8B-Instruct 需要 Hugging Face 帳號並接受授權：

1. 前往 [meta-llama/Llama-3.1-8B-Instruct](https://huggingface.co/meta-llama/Llama-3.1-8B-Instruct) 申請存取
2. 執行下列指令登入：
   ```bash
   huggingface-cli login
   ```
3. 首次推論時 transformers 會自動下載，約 16GB

---

## 4. 訓練 LoRA 微調模型（選用）

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
# {"status":"ok","model_loaded":true}
```

---

## 6. 執行部署

**CLI 模式：**
```bash
python 0_touch_generate_pods.py
```

**Web UI 模式：**
```bash
python web_demo.py
# 開啟瀏覽器：http://localhost:5000
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
pip install sentence-transformers
python rag/build_index.py
```

### GitOps

```bash
pip install gitpython
```

在 `.env` 設定 `GITOPS_REPO_PATH`，部署時自動 commit YAML。

### Prometheus 整合

在 `.env` 設定 `PROMETHEUS_URL`，`observability/prometheus_client.py` 會使用該 URL。
