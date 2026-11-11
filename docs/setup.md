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
python training/generate_finetune_data.py

# 清洗資料集
python training/clean_dataset.py

# 開始 LoRA 訓練（需 GPU）
python training/train_local.py
```

訓練完成後權重儲存於 `llama3_k8s_lora_results/`。

---

## 6. 啟動 Model Server

**建議用一鍵啟動腳本**（Windows），模型伺服器和網站都用獨立程序執行，關掉終端機或對話也不會停：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start_services.ps1           # 已經在跑的不會重複啟動
powershell -ExecutionPolicy Bypass -File scripts\start_services.ps1 -Restart  # 改了程式要重新啟動時
```

輸出記在 `logs\model_server.log`、`logs\web.log`（錯誤在 `.err`）。

**開機自動啟動**（2026-10-21）：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1          # 設定
powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1 -Remove  # 取消
```

會在 Windows「啟動」資料夾放一個捷徑（不需要系統管理員權限），登入後在背景執行 `start_services.ps1 -AtBoot`：
先啟動模型伺服器；Kubernetes 還沒就緒時，Docker Desktop 沒開就幫忙開，最多等 10 分鐘，**叢集就緒才啟動網站**
（網站只在啟動時檢查一次叢集，太早啟動會一直當成沒有叢集）。10 分鐘都沒好，網站照樣啟動並顯示 Docker 沒開的提示。
過程記在 `logs\startup.log`。Tailscale 的對外網址（`tailscale funnel --bg`）本身重開機後就會恢復，不用另外處理。

2026-10-10 曾發生模型伺服器在
AI 助理的背景工作裡啟動、對話重新開啟時跟著被結束，網站頂端出現「AI 模型伺服器沒有在執行」的紅色橫幅；
用這個腳本啟動就不會有這個問題。

只想手動跑單一個：

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

## 7.5 網站對外開放（給客戶從外網使用）

2026-10-09 起網站可以透過「通道」對外開放：家裡電腦不用開路由器 port、不用固定 IP，
HTTPS 自動處理。`web_demo.py` 已改用正式伺服器 waitress，並有頻率限制（`core/web_security.py`）。

**方法 A：Tailscale Funnel（免費、網址固定、不用網域，建議先用這個）**
1. 安裝並登入：`winget install Tailscale.Tailscale`，開啟 Tailscale 用 Google/GitHub 帳號登入。
2. 開啟對外分享：`tailscale funnel --bg 5050`。第一次執行會給一個網址，要你到管理後台
   同意開啟 HTTPS 憑證和 Funnel 功能，同意後再執行一次。
3. 終端機會顯示對外網址，例如 `https://你的電腦名.xxxx.ts.net`。把它寫進 `.env`：
   `PUBLIC_BASE_URL=https://你的電腦名.xxxx.ts.net`
4. 重啟 `python web_demo.py`。設定 `PUBLIC_BASE_URL` 之後，網站只接受本機通道送來的連線，
   區網的其他電腦不能直接連到 `:5050`，這是刻意的設計。
5. 有用 Google 登入的話，到 Google Cloud Console → 憑證 → OAuth 用戶端，把
   `<PUBLIC_BASE_URL>/auth/google/callback` 加進「已授權的重新導向 URI」。
6. 驗證：用手機關掉 Wi-Fi，改用行動網路開啟這個網址，應該看得到登入頁。

**方法 B：自己的網域 + Cloudflare Tunnel（比較正式）**
學生可以用 [GitHub Student Developer Pack](https://education.github.com/pack) 在 Name.com 免費領一個
網域（第一年免費，要綁付款方式但不扣款；記得關掉自動續約）。把網域加進 Cloudflare 免費方案後，
用 `cloudflared tunnel` 把網域指到 `http://localhost:5050`，再把 `PUBLIC_BASE_URL` 改成新網域即可，
程式不用改。

**注意**：家裡電腦關機或睡眠，網站就會停。Windows 的「睡眠」要改成「永不」。

---

## 7.6 客戶自帶叢集模式（客戶用自己電腦的資源部署）

開啟後，非管理員帳號登入時會先看到同意書，接著在 PowerShell 貼一行指令，把自己的 Kubernetes
接上平台；連線成功後才能使用網站。之後所有部署、擴縮、回滾、Healer 修復都會變成平台替客戶在
GitHub 開的私有 repo 的 commit，再由客戶叢集裡的 Flux 自動套用（約 30~60 秒生效）。
設計與安全考量見 `docs/security_review.md` 33 節。

**平台端要準備的（一次性）**：
1. 先完成 7.5 節，讓網站有對外網址（`PUBLIC_BASE_URL`），因為客戶叢集要連回這個網址回報狀態。
2. GitHub 建立一個免費 Organization（例如 `zerotouch-tenants`），專門放客戶的 repo。
3. **Organization → Settings → Member privileges → Deploy keys 改成 Enabled**。
   2024 年 10 月後新建立的 organization 預設是關閉的；沒開的話，建立 deploy key 會失敗（HTTP 422
   「Deploy keys are disabled for this repository」）。
4. Organization → Settings → Personal access tokens → 允許 fine-grained token，並且不需要核准。
5. 個人 Settings → Developer settings → Fine-grained tokens → 產生 token：
   - Resource owner 選這個 organization，選 All repositories
   - Administration 和 Contents 都設成 Read and write
6. `.env` 加入：
   ```
   TENANT_GITHUB_TOKEN=github_pat_...
   TENANT_GITHUB_ORG=zerotouch-tenants
   CUSTOMER_CLUSTER_MODE=1
   ```
7. 重啟 `python web_demo.py`。管理員帳號不受影響，仍然使用平台自己的叢集。

**客戶端需要**：Windows PowerShell、開啟 Kubernetes 的 Docker Desktop、能連上網路。
安裝腳本會自動判斷叢集是否已經有 Flux：沒有就安裝 v2.9.6，有就沿用，不會更動原本的版本。

**驗證（2026-10-09 實測）**：透過外網網址完成註冊 → 同意書 → 執行指令 → 連線檢查；部署和擴縮
都在 40 秒內生效；換成壞掉的映像檔後，Healer 35 秒內偵測到，自動修復 15 秒後恢復。

**疑難排解**：
- 檢查頁顯示「叢集拉不到部署設定」：通常是第 3 步的 deploy key 沒開，或 token 權限不足。
- 客戶重跑安裝指令是安全的。回報器會自動換新 token 並重啟。
- 解除安裝：`kubectl delete namespace <客戶的 zt-… namespace>`，再刪除對應的
  `clusterrole`/`clusterrolebinding zt-reporter-nodes-<namespace>`。

## 7.7 手機 App 與推播通知（2026-10-22）

不用額外設定：裝好 `requirements.txt`（含 `pywebpush`）後，第一次有人開啟通知時，平台會自動產生推播用的金鑰，
存在 `data/vapid_private.pem`。**不要刪這個檔案**，刪掉後所有已開啟通知的裝置都要重新開啟一次。換電腦時不要複製它
（跟 `data/` 其他檔案一樣），新電腦會產生新的，使用者重新開啟通知即可。

手機上的步驟（必須用 https 的對外網址，見 7.5 節）：
- **Android（Chrome）**：打開對外網址並登入 → 選單「加到主畫面」或「安裝應用程式」→ 從主畫面打開 →
  「自動修復」頁 → 「開啟通知」→ 允許。會馬上收到一則測試通知。
- **iPhone／iPad**（iOS 16.4 以上）：一定要用 Safari 打開 → 分享 → 「加入主畫面」→ 從主畫面的圖示打開 → 「自動修復」頁 →
  「開啟通知」。直接在 Safari 分頁裡收不到通知，畫面上也會這樣說明。

注意：平台主機關機時不會送通知；同一個問題 6 小時內只通知一次。圖示要改設計就改 `scripts/make_icons.py` 再執行一次。

**手機端只能看狀態**（2026-10-22 起）：手機（iPhone、Android 手機）打開平台會自動進入監控版 `/m`，
有總覽、服務、修復紀錄、設定四個分頁，不會出現同意書，也沒有部署、擴縮、刪除等功能。手機登入的 session
在伺服器端是唯讀的，就算直接呼叫 API 也會被拒絕。iPad 和電腦照舊是完整版。帳號還沒完成叢集連線的，
手機上會提示「請先用電腦完成連線設定」。

**行事曆與預約**（2026-10-23 起）：在對話裡說「明天早上 9 點部署 2 個 nginx」，或在確認卡按「改成預約」。時間到了**不會自動執行**：
手機收到推播、電腦打開網站時跳出提示，按「確定執行」才會執行（部署會重新審查）。預約資料存在 `data/schedule.db`，跟 `data/` 其他檔案一樣不要複製到別台電腦。
手機版的行事曆只能看。

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
