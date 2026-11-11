# ZeroTouch：用自然語言部署 Kubernetes 服務的平台

長庚大學資工系專題。使用者用一句話描述需求（中文或英文），例如「幫我開 3 台 nginx 1.24 的網頁伺服器」，平台把它轉成部署規格，經過安全、成本、效能三項審查與使用者確認後，部署到 Kubernetes 叢集。部署之後持續監控，Pod 出錯時自動診斷並修復。

目標使用者是**不熟 Kubernetes 的新手**：不用寫 YAML、不用下 kubectl 指令。

## 主要功能

- **自然語言部署**：Gemini 把口語需求整理成部署規格，本地模型（Qwen2.5-3B）與固定規則補位；使用者講不清楚時會反問，不亂猜。
- **部署前三項審查**：安全（特權、主機目錄等）、成本、效能代理各自評估，加上 `kubectl --dry-run` 與 YAML 政策檢查；危險要求會擋下並說明原因。
- **部署後自動修復（Healer）**：監看 CrashLoopBackOff、OOMKilled、映像檔拉不到等狀況，先用規則判斷，判斷不了再交給監控模型（Qwen2.5-1.5B），然後自動補救。
- **GitOps 版本紀錄**：每次部署寫成 manifest 並 commit 到獨立的部署紀錄 repo（預設 `data/gitops`），可以「換回上一版」。
- **多使用者隔離**：每個帳號對應一個 namespace，有資源配額；支援 Google 登入。
- **其他**：docker-compose／K8s YAML 匯入、分時擴縮與閒置休眠、每週報告、手機版監控頁。

## 系統架構

```
使用者一句話
   │
   ▼
意圖判斷（固定規則 → Gemini 附使用者的服務清單 → 固定規則查核）
   │ 部署
   ▼
部署規格解析（Gemini 翻譯層 → 固定規則 → Qwen2.5-3B＋RAG 範例）
   │
   ▼
三項審查代理（agents/：security / cost / perf）＋ guardian/（dry-run、YAML 政策）
   │ 使用者在畫面上確認
   ▼
gitops/（寫 manifest 並 commit 到部署紀錄 repo）→ Kubernetes 叢集
   │
   ▼
healer/（監看 → 診斷 → 自動補救）＋ observability/（Prometheus 指標）
```

各模組的說明見 [docs/architecture.md](docs/architecture.md)，API 見 [docs/api.md](docs/api.md)。

## 環境需求

| 項目 | 需求 |
|---|---|
| 作業系統 | Windows 11（開發與測試環境） |
| Python | 3.12 |
| GPU | 16GB 顯存實測過：NVIDIA RTX 5080 Laptop（CUDA 12.8）、AMD RX 9060 XT（ROCm 7.2.1）。顯存較小或沒有 GPU 時會自動降精度或改用 CPU（`core/hardware.py`） |
| Kubernetes | Docker Desktop 內建的 Kubernetes |
| 其他 | RAM 32GB 以上、磁碟 25GB 以上（三個模型約 13GB） |
| 外部服務 | Gemini API key（翻譯層與意圖判斷用） |

## 安裝與啟動

完整步驟與踩過的坑見 [docs/setup.md](docs/setup.md)，以下是精簡版。

```powershell
# 1. 安裝套件（依顯卡擇一）
python -m pip install -r requirements-nvidia.txt
python -m pip install -r requirements-amd.txt

# 2. 設定環境變數：複製範本，填入 GEMINI_API_KEYS、FLASK_SECRET_KEY 等
copy .env.example .env

# 3. 下載模型（Qwen2.5-3B、Qwen2.5-1.5B、BGE-M3，不需要登入）
python scripts/download_models.py

# 4. 啟動模型伺服器（port 8765）與網站（port 5050）
powershell -ExecutionPolicy Bypass -File scripts\start_services.ps1
```

開啟 http://localhost:5050 ，註冊帳號後就可以開始部署。第一次啟動要等模型載入約 1～2 分鐘；`curl http://127.0.0.1:8765/health` 可以確認 GPU 與精度。

`.env` 不進版控，裡面的 key 不要 commit。帳號資料 `users.json` 也不進版控，第一次註冊時會自動建立。

### 測試

```powershell
python -m pytest
```

## 目錄結構

| 位置 | 內容 |
|---|---|
| `web_demo.py` | 網站與所有 API（Flask） |
| `llama_client.py` | 部署需求解析（翻譯層、規則、本地模型） |
| `0_touch_generate_pods.py` | 命令列版的部署入口 |
| `core/` | 設定、模型伺服器、Gemini 用戶端、各種資料儲存 |
| `agents/` | 審查代理、意圖判斷、部署補全、情境推算、compose 匯入 |
| `guardian/` | 部署前驗證（dry-run、YAML 政策） |
| `gitops/` | manifest 產生、commit、回滾、客戶叢集 repo |
| `healer/` | Pod 監看、診斷、自動修復 |
| `rag/` | 檢索用的文件與索引 |
| `observability/` | Prometheus 指標 |
| `static/` | 網頁前端資源 |
| `tests/` | 自動化測試 |
| `scripts/` | 啟動、模型下載、對照實驗（`scripts/bench/`）等工具 |
| `training/` | 訓練資料產生與 LoRA 微調（目前預設不啟用微調） |
| `experiments/` | 歷次測試與評估腳本，結果在 `docs/` |
| `docs/` | 架構、安裝、資安審查、測試報告、交接文件 |
| `docs/development/` | AI 輔助開發的規則與紀錄（見下方「開發方式」） |

## 測試結果與已知限制

我們做過兩次「ZeroTouch 與 Claude Code」的對照實驗，完整紀錄在 [docs/bench_record/題目與結果總表.md](docs/bench_record/題目與結果總表.md)。

| 測試 | ZeroTouch | Claude Code |
|---|---|---|
| 第一次（11/06，開發端出題）部署題 | 59.1% | 93.9% |
| 第二次盲測（Gemini 出題、模型評分，30 題） | 36.7% | 75.0% |
| r3 新盲測（11/08，意圖判斷修正後，15 題） | 26.7% | 未測 |

已知限制：

- **口語需求的理解仍是主要弱點**：只描述情境、沒講要用什麼軟體的需求常被退回；部署規格目前只有副本數、映像檔、名稱、port、記憶體、CPU 六個欄位，使用者講的啟動指令、密碼、要保存資料（PVC）、定時執行（CronJob）會被丟掉。改進計畫在 GitHub issue #1。
- 一次只能部署一個服務。
- 實驗規模小（每次 15～30 題），題目與評分都由模型產生，沒有真人使用者測試。
- 只在單一節點的 Docker Desktop Kubernetes 上測試過。

資安相關的檢查與修正紀錄在 [docs/security_review.md](docs/security_review.md)。

## 訓練資料集

原始資料集（約 35 MB）不放在 repo 裡，請從 [v0.4 Release](https://github.com/neilhuangmobile-art/LLM-enabled-Zero-Touch-Kubernetes-Service-Deployment-Platform/releases/tag/v0.4) 下載，放到 `dataset/`：

- `k8s_prompts_20000.jsonl`
- `k8s_prompts_20000_zh_tw.jsonl`
- `trash_talk_prompts_5000.jsonl`

只有重新產生訓練資料（`training/enrich_dataset.py`）時才需要；平台平常運作不用。

## 開發方式

開發過程有使用 AI 程式助理（Claude Code）輔助撰寫程式與文件。所有改動由組員確認、跑完整自動化測試後才 commit；重要的設計決定與風險記在 [CHANGELOG.md](CHANGELOG.md) 和 [docs/security_review.md](docs/security_review.md)。

給 AI 助理的專案規則與開發紀錄放在 [docs/development/](docs/development/)；根目錄的 `CLAUDE.md` 只是讓工具自動載入這些規則的入口。

## 版本

- `v0.4`：第四次進度報告（2026-11）的版本。
