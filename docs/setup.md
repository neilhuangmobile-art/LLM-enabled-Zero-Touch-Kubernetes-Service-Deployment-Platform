# 環境建置指南

## 系統需求

2026-10-09 起專案在兩台 16GB 顯存的機器上跑，**兩台用同一套設定**（QA 數據要拿去口試，
設定不同結果會對不上）：

| 項目 | 家裡（AMD） | 學校（NVIDIA） |
|------|------------|---------------|
| 顯卡 | RX 9060 XT 16GB | RTX 5080 Laptop 16GB（Blackwell） |
| torch | 2.9.1 + ROCm 7.2.1 | 2.9.1 + CUDA 12.8（**Blackwell 一定要 cu128**） |
| 安裝檔 | `requirements-amd.txt` | `requirements-nvidia.txt` |
| Python | **3.12**（全域安裝，不用 venv） | **3.12**（同左） |
| 模型載入 | 3B + 1.5B 都 bf16 上 GPU | 同左 |
| RAG embedding | BGE-M3 fp16 上 GPU | 同左 |

其他：RAM 32GB+、磁碟 25GB+（三個模型約 13GB）、Windows 11、Docker Desktop（開 Kubernetes）。

**精度/裝置是自動決定的**（`core/hardware.py`），不用手動切換：顯存 >= 10GB 全部 bf16 上 GPU；
小顯存 NVIDIA（例如舊的 6GB 筆電）部署模型 4-bit、監控模型 CPU；沒有 GPU 全部跑 CPU。
要強制指定用 `.env` 的 `MODEL_PRECISION` / `MONITOR_DEVICE` / `RAG_EMBED_DEVICE`。
啟動後 `curl http://127.0.0.1:8765/health` 會回報實際的 `gpu`、`deploy_precision`、
`vram_allocated_gb`，兩台可以直接對照。

2026-10-09 在 RX 9060 XT 實測顯存（Model Server + RAG embedding 同時在 GPU）：
載入後 8.7GB，長 prompt 壓測高峰整張卡 14.0GB / 15.9GB。

---

## 1. 安裝 Python 3.12

**為什麼是 3.12**：AMD 官方 Windows 版 ROCm torch 只有 Python 3.12 的安裝包（cp312），
為了兩台一致，NVIDIA 那台也用 3.12。

1. 到 python.org 下載 3.12.x 的 Windows 安裝檔（例如 `python-3.12.10-amd64.exe`），
   安裝時勾選「Add python.exe to PATH」。
2. 電腦上如果還有別的 Python（例如 3.14），要讓 3.12 排在 PATH 最前面，並讓 `py` 預設用 3.12：
   ```powershell
   $py="$env:LOCALAPPDATA\Programs\Python\Python312"
   $u=[Environment]::GetEnvironmentVariable('Path','User')
   [Environment]::SetEnvironmentVariable('Path',"$py\Scripts;$py;$u",'User')
   [Environment]::SetEnvironmentVariable('PY_PYTHON','3.12','User')
   ```
3. **完全關掉 VS Code（或重開機）再開**，不然終端機還是舊的 PATH。確認：`python --version` 要是 3.12.x。

> ⚠️ 踩過的坑：改完 PATH 沒重開 VS Code，`python` 還是指到 3.14，pip 裝 cp312 的
> torch 會報 `is not a supported wheel on this platform`。

---

## 2. 安裝 Python 套件

依顯卡**擇一**（兩份都會先裝共用的 `requirements.txt`）：

```powershell
# AMD 顯卡
python -m pip install --timeout 20 --retries 10 -r requirements-amd.txt
# NVIDIA 顯卡
python -m pip install --timeout 20 --retries 10 -r requirements-nvidia.txt
```

`--timeout 20 --retries 10` 是故意加的：家裡那台的網路遇到下載大檔會卡住，加了才會自動重試。
**不要用 uv 或其他會同時開很多條連線的下載工具**——2026-10-09 實測這台網路遇到大量平行連線
就整個卡在 0 bytes（uv、`huggingface_hub` 都中），單一連線的 pip / curl 才正常。

裝完驗證 GPU：
```powershell
python -c "import torch;print(torch.__version__, torch.cuda.is_available(), torch.cuda.get_device_name(0))"
```
要看到 `True` 和你的顯卡名稱（AMD 的 ROCm 版 torch 一樣是用 `torch.cuda` 介面）。

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

三個模型都**不需要**登入或接受授權，下載到 `.env` 的 `HF_HOME`：

```powershell
python scripts/download_models.py
# Qwen2.5-3B-Instruct（部署，約 6GB）、Qwen2.5-1.5B-Instruct（監控，約 3GB）、BAAI/bge-m3（RAG，約 2.2GB）
```

這支腳本單一連線下載、逐檔驗 SHA256，中斷重跑會跳過已下載的檔案。**不用 `hf download`**：
它一次開幾十條連線，家裡那台網路遇到這種下載會卡在 0 bytes（2026-10-09 實測）。

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
# {"status":"ok","deploy_loaded":true,"monitor_loaded":true,
#  "gpu":"AMD Radeon RX 9060 XT","gpu_backend":"rocm",
#  "deploy_precision":"gpu-bf16","monitor_precision":"gpu-bf16",
#  "vram_allocated_gb":8.73,"vram_reserved_gb":9.06}
```

兩台機器的 `deploy_precision`/`monitor_precision` 應該一樣（都是 `gpu-bf16`），不一樣代表
有一台的 torch 沒抓到 GPU，回頭看第 2 步的驗證指令。

端點：`/infer`（部署 JSON）、`/chat`（一般問答，共用部署模型）、`/classify`（意圖分類）、
`/diagnose`（healer 根因分析，監控模型）。

2026-10-09 RX 9060 XT 實測延遲：`/classify` 1.5s、`/diagnose` 1.5s、`/infer` 3.3s、`/chat` 2s（短答）～
15s（512 token 長答）。

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

Docker Desktop：Settings → Kubernetes → 勾 **Enable Kubernetes** → Apply & restart。

**Docker Desktop 顯示「Virtual Machine Platform not enabled」/ `wsl --status` 說「此機未啟用虛擬化」**
（2026-10-09 新電腦實際遇到）：先別急著進 BIOS。查 `Win32_Processor.VirtualizationFirmwareEnabled`
如果是 True（BIOS 已開）、「虛擬機器平台」功能也已開，根因多半是開機設定少了 hypervisor 啟動項。
用**系統管理員** PowerShell：

```powershell
bcdedit /enum "{current}" | findstr -i hypervisorlaunchtype   # 沒輸出或 Off 就是這個問題
bcdedit /set hypervisorlaunchtype auto
wsl.exe --install --no-distribution
```

重開機後 Docker Desktop 就能啟動。WSL 說「沒有已安裝的發佈」不用理，Docker 會自己建 `docker-desktop`。

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
裡）才會用語意向量，embedding 顯存 >= 10GB 自動用 GPU fp16（否則 CPU），`RAG_EMBED_DEVICE=cpu|cuda` 可強制。

### GitOps

在 `.env` 設定 `GITOPS_REPO_PATH`，部署時自動 commit YAML。

### Prometheus 整合

在 `.env` 設定 `PROMETHEUS_URL`，`observability/prometheus_client.py` 會使用該 URL。
