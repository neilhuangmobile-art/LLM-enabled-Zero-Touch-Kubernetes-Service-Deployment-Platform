# 環境建置指南

## 系統需求

| 項目 | 最低需求 |
|------|---------|
| Python | **3.9.x（實測跑在 3.9.13，不是「3.9 以上皆可」，見下方警告）** |
| CUDA | 11.8+（部署模型 4-bit GPU 推論用） |
| VRAM | 4GB+（部署模型 Qwen2.5-3B 4-bit 約佔 2.2GB） |
| RAM | 16GB+（監控模型 Qwen2.5-1.5B 跑 CPU，約佔 6GB） |
| 磁碟 | 15GB+（Qwen2.5-3B ≈ 6GB + 1.5B ≈ 3GB） |
| OS | Windows 10/11、Ubuntu 20.04+ |

> 2026-09-06 起改用小模型雙軌（部署 Qwen2.5-3B GPU、監控 Qwen2.5-1.5B CPU），
> 不再需要 8GB+ VRAM 跑 Llama-3.1-8B。

> **⚠️ 2026-10-07 新增警告，換機器/重灌環境前一定要看**：`requirements.txt`
> 裡的套件版本是**釘死的舊版**（例如 `torch==2.4.1`，當下 PyPI 最新已經到
> 2.8.0），這些舊版在新的 Python 版本上**不保證有預編譯的 wheel**——已知
> PyTorch 在 Windows 上對 Python 3.13 目前還沒有官方 wheel，`bitsandbytes`
> 這類有編譯後端的套件也有同樣風險。最安全的做法是裝跟這台機器一樣的
> Python 3.9.x（見下方「安裝正確的 Python 版本」）。**使用者決定新機器直接
> 裝最新版 Python**，接受「先裝裝看，卡住再修」——如果套件裝到一半報
> `ERROR: Could not find a version that satisfies the requirement`，代表
> 新版 Python 沒有對應的舊版 wheel，這時要把 `requirements.txt` 裡卡住的
> 那個套件（通常是 torch／bitsandbytes 這類有編譯後端的）**升級成新版 Python
> 有支援的版本**，不是回頭硬裝 3.9——升級之後要重新確認整組依賴能不能一起
> 跑（尤其 transformers/peft/bitsandbytes 彼此的版本相容性），不要只改壞掉
> 的那一個就當作沒事。

---

## 1. 安裝 Python（新機器第一步，順序在裝套件之前）

最穩妥的做法是裝跟這台機器一樣的 3.9.x（下面方法 A/B）。**如果決定直接裝最新版
Python**（2026-10-07 的決定：先裝裝看，卡住再升級卡住的套件版本），可以跳過這節，
但遇到 `pip install -r requirements.txt` 裝不起來時回頭看上面的警告框。

**方法 A（建議，之後也方便管理多個版本）：用 [pyenv-win](https://github.com/pyenv-win/pyenv-win)**
```powershell
# 安裝 pyenv-win 後：
pyenv install 3.9.13
pyenv local 3.9.13   # 在這個專案目錄下固定用 3.9.13
python --version     # 確認是 3.9.13
```

**方法 B（簡單，但以後想裝別的版本要手動管理）：直接從官網裝 3.9.13**
官網首頁只會給最新版下載連結，要裝 3.9.13 得到這個固定網址：
`https://www.python.org/downloads/release/python-3913/`，下載對應作業系統的
安裝檔。安裝時記得勾選「Add python.exe to PATH」，裝完後如果系統還有其他
Python 版本，用 `py -3.9` 或建立 venv 時明確指定路徑，確保這個專案用的是
3.9.13，不是跟系統預設版本混在一起（這台機器本身就同時裝了
`Python39`/`anaconda3`/`WindowsApps` 三個 Python，是真實會發生的問題）。

**建議在專案目錄建立 venv，不要裝進系統全域環境**：
```bash
python -m venv venv
# Windows: venv\Scripts\activate
# Linux/macOS: source venv/bin/activate
```

---

## 2. 安裝 Python 套件

確認 venv 裡的 `python --version` 是 3.9.13 之後：

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

如果裝到一半某個套件報 `ERROR: Could not find a version that satisfies the
requirement`，九成是 Python 版本不對（不是 3.9.x）導致沒有對應 wheel，回頭檢查
第 1 步，不要急著把 `requirements.txt` 的版本號改鬆（改鬆版本等於放棄這台機器
已經驗證過能動的組合，之後任何壞掉都很難判斷是不是版本升級造成的）。

---

## 3. 設定環境變數

```bash
cp .env.example .env
# 編輯 .env：
#   GEMINI_API_KEYS=key1,key2,...   翻譯層（可放多把 key 自動輪換湊額度）
#   USE_LLM_NORMALIZE=1             預設開啟翻譯層
```

---

## 4. 下載模型（首次執行）

Qwen2.5 系列**不需要**登入或接受授權：

```bash
hf download Qwen/Qwen2.5-3B-Instruct     # 部署模型，約 6GB
hf download Qwen/Qwen2.5-1.5B-Instruct   # 監控模型，約 3GB
```

不下載也可以，`core/model_server.py` 首次啟動時 transformers 會自動抓。

---

## 5. 訓練 LoRA 微調模型（選用，目前不啟用）

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

## 6. 啟動 Model Server

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

## 7. 執行部署

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

## 8. Kubernetes 設定

確認 kubeconfig 正確：

```bash
kubectl get nodes
```

若使用 Docker Desktop 或 minikube，預設 kubeconfig 路徑為 `~/.kube/config`。

---

## 9. 選用功能

> 2026-10-07 更新：chromadb/sentence-transformers（語意向量 RAG）跟 gitpython
> （GitOps）現在都已經是 `requirements.txt` 裡**不需要另外安裝**的固定依賴，
> 步驟 2 的 `pip install -r requirements.txt` 就會一併裝好。下面只是這些功能
> 的設定方式，不是安裝指令（舊版文件這裡寫的另外 `pip install` 已經過時）。

### RAG 知識庫索引

```bash
python rag/build_index.py --rebuild --deploy
```

建立：
- `rag/index.json`：k8s 散文知識（給 `/chat`）
- `rag/deploy_index.json`：部署範例 `input → JSON`（給 `/infer` 當 few-shot）

預設用 TF-IDF；裝了 chromadb+sentence-transformers（已經在 `requirements.txt`
裡）才會用語意向量，embedding 預設跑 CPU，`RAG_EMBED_DEVICE=cuda` 可改。

### GitOps

在 `.env` 設定 `GITOPS_REPO_PATH`，部署時自動 commit YAML。

### Prometheus 整合

在 `.env` 設定 `PROMETHEUS_URL`，`observability/prometheus_client.py` 會使用該 URL。
