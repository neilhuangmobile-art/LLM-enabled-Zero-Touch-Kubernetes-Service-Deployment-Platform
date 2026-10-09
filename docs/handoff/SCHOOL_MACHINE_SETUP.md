# 學校電腦安裝與設定交接（隊友使用）

寫於 2026-10-09，由專案負責人（GitHub `neilhuangmobile-art`）家裡那台電腦留下。

**這台學校電腦是給隊友用的，要有跟負責人家裡那台完全一樣的功能**：本機部署、Chat、Healer、
監控，以及網站對外開放、讓客戶接上自己的叢集。兩台各自是一個**獨立的平台**。

這份文件有三個讀者：

| 讀者 | 讀哪裡 |
|---|---|
| **專案負責人**（出發前要先做幾件事） | 第零部分 |
| **隊友**（在學校電腦上操作的人） | 第一部分，照步驟做 |
| **學校電腦上的 Claude Code** | 第二部分，再陪隊友做第一部分 |

---

## 這台電腦是什麼

| 項目 | 規格 |
|---|---|
| 獨立顯卡 | NVIDIA GeForce RTX 5080 Laptop GPU，16GB 顯存（Blackwell 架構） |
| 內建顯卡 | AMD Radeon 890M |
| 系統記憶體 | 推測約 32GB（請實際確認） |
| 作業系統 | Windows 11 |

負責人家裡那台是 AMD 顯卡。程式已經支援兩種顯卡，兩台用**同一套模型設定**（bf16、不量化），
QA 數據才對得上。差別只有 torch 的安裝檔：家裡用 `requirements-amd.txt`，**這台用 `requirements-nvidia.txt`**（CUDA 12.8）。

## 兩台的關係

兩台是兩個**互不相通**的平台，各自有：

| | 負責人家裡那台 | 學校這台（隊友） |
|---|---|---|
| 對外網址 | `https://user.tail4c2fd2.ts.net` | 隊友自己的 Tailscale 網址 |
| 網站帳號（`users.json`） | 各自一份，**不會同步** | 各自一份 |
| 客戶 repo 的 GitHub org | `zerotouch-tenants` | **隊友自己開一個新的 org** |
| Gemini、Google 登入等金鑰 | 負責人自己的 | **隊友自己的** |

所以：在家裡那台註冊的帳號，在學校這台不存在。客戶要接哪一台，就到哪一台的網址註冊。

> **為什麼金鑰不能共用**：Google APIs 服務條款規定憑證要保密，也禁止用多個帳號的額度湊在一起
> （§2(d)、§4(b)(1)，https://developers.google.com/terms）。每台用自己帳號的金鑰，就沒有這個問題，
> 用量和責任也分得清楚。

---

# 第零部分：專案負責人出發前要做的事

1. **把隊友加成 GitHub repo 的協作者**（repo 是私有的，不加他看不到）：
   1. 打開 https://github.com/neilhuangmobile-art/LLM-enabled-Zero-Touch-Kubernetes-Service-Deployment-Platform
   2. 上方 **Settings** → 左側 **Collaborators**（可能要輸入密碼確認）
   3. **Add people** → 輸入隊友的 GitHub 帳號 → 權限選 **Write** → 送出
   4. 隊友會收到 email 邀請，**他要點接受**才會生效
2. **準備要給隊友的檔案**（用隨身碟或雲端硬碟）：
   - `dataset/` 資料夾（RAG 部署範例索引會用到，不在 GitHub 上）
   - 簡報 pptx（`reports/` 底下，如果隊友需要）
   - **不要給 `.env`**：裡面是你的金鑰，隊友要建自己的（第一部分步驟 5）
   - **不要給 `data/` 資料夾**：那是你這台平台的客戶連線紀錄
3. 決定隊友那台的**網站管理員**是誰（通常是隊友自己的 Google 帳號 email），告訴隊友。

---

# 第一部分：隊友照著做

順序：**先讓專案跑起來，再申請帳號讓它對外，最後裝開發工具**。每個步驟做完，請 Claude 驗證過再往下。

## 步驟 1：安裝 Windows 基本工具

1. **NVIDIA 顯示卡驅動**：到 https://www.nvidia.com/zh-tw/drivers/ 下載最新版，或用 NVIDIA App 更新。RTX 50 系列需要較新的驅動。
2. **Git**：`winget install Git.Git`
3. **Python 3.12**：到 https://www.python.org/downloads/release/python-31210/ 下載
   `Windows installer (64-bit)`，安裝時**勾選「Add python.exe to PATH」**。
   - 一定要 3.12，兩台要用同一版。
   - 這個專案不用 venv，套件直接裝在這個 Python。
4. **Node.js**：`winget install OpenJS.NodeJS.LTS`
5. **Docker Desktop**：https://www.docker.com/products/docker-desktop/ 下載安裝，不需要註冊 Docker 帳號。
6. **WSL**：Docker Desktop 第一次啟動通常會要求安裝；沒有的話，用**系統管理員** PowerShell 執行
   `wsl --install --no-distribution`，裝完重新開機。
7. **Helm**：`winget install Helm.Helm`
8. **全部裝完重新開機一次**。

### 如果 Docker Desktop 說「Virtual Machine Platform not enabled」或「此機未啟用虛擬化」

負責人家裡那台遇過。**先不要進 BIOS**，用系統管理員 PowerShell 執行：

```powershell
bcdedit /enum "{current}" | findstr -i hypervisorlaunchtype
```

- 沒有輸出或顯示 `Off`：執行 `bcdedit /set hypervisorlaunchtype auto`，再執行 `wsl.exe --install --no-distribution`，重新開機。
- 顯示 `Auto` 還是不行：才需要進 BIOS 開啟虛擬化（AMD 平台叫 SVM Mode）。

### 開啟 Kubernetes

Docker Desktop → 右上角齒輪 Settings → Kubernetes → 勾選 **Enable Kubernetes** → Apply & restart，
等左下角變綠燈（第一次約 3～5 分鐘）。

## 步驟 2：下載專案

先確認你已經**接受了負責人寄來的 GitHub 協作者邀請**（第零部分）。

```powershell
cd D:\
git clone https://github.com/neilhuangmobile-art/LLM-enabled-Zero-Touch-Kubernetes-Service-Deployment-Platform.git
cd LLM-enabled-Zero-Touch-Kubernetes-Service-Deployment-Platform
```

第一次會跳出 GitHub 登入視窗，用**你自己的** GitHub 帳號登入。把負責人給你的 `dataset/` 資料夾放進專案資料夾。

## 步驟 3：安裝 Claude Code 並請它接手

```powershell
npm install -g --allow-scripts=@anthropic-ai/claude-code @anthropic-ai/claude-code@latest
```

（一定要加 `--allow-scripts`，否則安裝不完整。）用**你自己的** Claude 帳號登入。

用 VS Code 打開專案資料夾，開啟 Claude Code，跟它說：
**「我是隊友，這台是學校電腦。請讀 docs/handoff/SCHOOL_MACHINE_SETUP.md，照文件幫我設定好」**。

## 步驟 4：安裝 Python 套件（Claude 會代為執行）

```powershell
python -m pip install --timeout 20 --retries 10 -r requirements-nvidia.txt
```

## 步驟 5：申請帳號與金鑰，建立 `.env`

`.env` 是放金鑰的設定檔，**不在 GitHub 上，也絕對不能上傳**。由 Claude 幫你從 `.env.example` 複製一份，
你只要把下面申請到的值告訴它（或自己貼進去）。**金鑰不要貼在聊天以外的地方**。

下表是每一項要怎麼來：

| 欄位 | 怎麼取得 | 必要嗎 |
|---|---|---|
| `GEMINI_API_KEYS` | 申請 A（下方） | 必要 |
| `FLASK_SECRET_KEY` | Claude 幫你產生一組隨機值 | 必要 |
| `HF_HOME` | 這台電腦上的資料夾，例如 `D:/hf_cache` | 必要 |
| `ADMIN_EMAILS` | 管理員的 Google email（第零部分負責人決定的） | 必要 |
| `USE_LLM_NORMALIZE` | 填 `1` | 必要 |
| `GITOPS_REPO_PATH` | 填 `.` | 必要 |
| `PROMETHEUS_URL` | 填 `http://localhost:9090` | 必要 |
| `PUBLIC_BASE_URL` | 申請 B（下方） | 對外開放才需要 |
| `TENANT_GITHUB_TOKEN`、`TENANT_GITHUB_ORG` | 申請 C（下方） | 讓客戶接上叢集才需要 |
| `CUSTOMER_CLUSTER_MODE` | 申請 B、C 都完成後填 `1` | 同上 |
| `GOOGLE_CLIENT_ID`、`GOOGLE_CLIENT_SECRET` | 申請 D（下方） | 要用 Google 登入才需要；不填也能用帳號密碼登入 |
| `HF_TOKEN`、`HUGGINGFACE_HUB_TOKEN` | **不用填**，用到的模型都是公開的 | 不需要 |
| `ANTHROPIC_API_KEY` | **不用填**，目前沒有用到 | 不需要 |
| `ARGOCD_SERVER`、`ARGOCD_TOKEN` | **不用填**，沒有安裝 Argo CD | 不需要 |

### 申請 A：Gemini API key（翻譯層，必要）

1. 用**你自己的** Google 帳號打開 https://aistudio.google.com/apikey
2. 按 **Create API key** → 選一個專案（沒有的話讓它建立新專案）→ 複製產生的 key
3. **只用這一把**，不要跟別人借 key 湊額度（違反 Google 條款，見上方說明）
4. 建議開啟付費並設定花費上限：額度會比免費高很多，送出的內容也不會被拿去改善 Google 的產品。
   做法：在 https://console.cloud.google.com 選同一個專案 → **帳單**連結帳單帳戶 →
   **預算與快訊**建立預算（例如每月 5 美元）。不開付費也能用，只是額度很快用完時，
   系統會改用本機解析，而且惡意意圖偵測會暫時失效。

### 申請 B：Tailscale（讓網站有固定的對外網址）

1. 安裝：`winget install Tailscale.Tailscale`
2. 右下角系統匣的 Tailscale 圖示 → **Log in**，用**你自己的** Google、GitHub 或 Microsoft 帳號登入
3. **開一個新的 PowerShell 視窗**，執行：
   ```powershell
   tailscale funnel --bg 5050
   ```
   第一次會給一個網址要你去開啟 HTTPS 和 Funnel，打開網址按 **Enable**，再執行一次同樣的指令。
4. 成功後畫面會顯示 `https://你的電腦名.xxxx.ts.net`，把它填進 `.env` 的 `PUBLIC_BASE_URL`（**結尾不要加 `/`**）
5. 測試：手機關掉 Wi-Fi、用行動網路打開這個網址，看到登入頁就成功了
6. 關掉對外開放：`tailscale funnel reset`

> 學校網路如果擋 Tailscale，第 5 步會打不開。先用手機熱點測試，確認是不是學校網路的問題。

### 申請 C：GitHub Organization 與 token（客戶的部署設定放這裡）

**一定要建自己的 org，不要用負責人的 `zerotouch-tenants`**，否則兩台平台可能寫到同一個客戶 repo。

1. github.com 右上角大頭貼 → **Your organizations** → **New organization** → **Create a free organization**
2. 名稱例如 `zerotouch-tenants-<你的名字>`，填 email，選 **My personal account** → Next → 跳過邀請成員
3. **開啟 deploy key**（新 org 預設是關的，沒開會失敗）：
   進入這個 org → **Settings** → 左側 **Access** 區塊的 **Member privileges** → **Deploy keys** 改成 **Enabled** → 儲存
4. **允許 fine-grained token**：org 的 **Settings** → 左側 **Personal access tokens** → **Settings** →
   選 **Allow access via fine-grained personal access tokens**，下方選 **Do not require administrator approval** → 儲存
5. 產生 token：大頭貼 → **Settings**（個人的）→ 最下方 **Developer settings** → **Personal access tokens** →
   **Fine-grained tokens** → **Generate new token**
   - Token name：`zerotouch-tenant-repos`
   - Expiration：選一個口試之後的日期
   - **Resource owner：選剛建立的 org**（不是個人帳號）
   - Repository access：**All repositories**
   - Repository permissions：**Administration** 和 **Contents** 都設 **Read and write**
   - 按 **Generate token**，**立刻複製**（只會顯示一次）
6. 填進 `.env`：`TENANT_GITHUB_TOKEN=github_pat_……`、`TENANT_GITHUB_ORG=<你的 org 名稱>`

### 申請 D：Google 登入（選做）

不做的話，網站只是沒有「使用 Google 登入」按鈕，帳號密碼登入照常可用。要做的話：

1. https://console.cloud.google.com → 上方選擇專案（可以用申請 A 的同一個專案）
2. 左側 **API 和服務** → **OAuth 同意畫面**：使用者類型選 **外部**，填應用程式名稱和你的 email，
   範圍不用加，測試使用者加入要登入的 Google 帳號
3. 左側 **憑證** → **建立憑證** → **OAuth 用戶端 ID** → 應用程式類型選 **網頁應用程式**
4. **已授權的重新導向 URI** 加入兩個：
   - `http://localhost:5050/auth/google/callback`
   - `<你的 PUBLIC_BASE_URL>/auth/google/callback`
5. 建立後複製用戶端 ID 和密鑰，填進 `GOOGLE_CLIENT_ID`、`GOOGLE_CLIENT_SECRET`

## 步驟 6：下載模型、建索引、裝監控（Claude 會代為執行）

```powershell
python scripts/download_models.py          # 三個模型約 11GB
python rag/build_index.py --rebuild --deploy
```

監控（Prometheus、Grafana）照 `observability/README.md` 的 helm 指令安裝。

## 步驟 7：開發工具

| 項目 | 你要做的事 |
|---|---|
| GitHub CLI 登入 | `gh auth login` → GitHub.com → HTTPS → Login with a web browser，用**你自己的**帳號 |
| 給 Claude 讀 GitHub 的 token | 見下方 |
| 其他外掛與工具 | Claude 會照第二部分的清單安裝 |

**給 Claude 讀 GitHub 的 token**（跟申請 C 的 token 不同，這組只是讓 Claude 讀 repo）：

1. 大頭貼 → Settings → Developer settings → Fine-grained tokens → **Generate new token**
2. Resource owner 選**你的個人帳號**；Repository access 選 **Only select repositories** → 選這個專案的 repo
3. Repository permissions：**Actions**、**Contents**、**Pull requests** 都設 **Read-only**
4. 產生後複製，在 PowerShell 執行：`setx GITHUB_PERSONAL_ACCESS_TOKEN "github_pat_……"`
5. **完全關掉 VS Code 再打開**

> 如果協作者權限還沒生效，第 2 步會選不到這個 repo。先確認已接受邀請。

## 步驟 8：驗證（Claude 會執行並回報）

全部通過才算完成：

1. `python -m pytest` 全部通過（家裡那台最後是 419 個）
2. `python core/model_server.py` 啟動後，`http://127.0.0.1:8765/health` 要顯示：
   - `"gpu": "NVIDIA GeForce RTX 5080 Laptop GPU"`（**不能是 AMD Radeon 890M**）
   - `"deploy_precision": "gpu-bf16"`、`"monitor_precision": "gpu-bf16"`
3. `python web_demo.py` 後，`http://localhost:5050` 能註冊、登入、用 Chat 部署一個服務
4. 對外網址從手機行動網路打得開
5. 客戶模式：用另一個帳號走一次「同意書 → 貼指令 → 連線檢查」，成功後部署一個服務，確認它出現在叢集裡
   （這台自己就可以當客戶測試；也可以拿另一台電腦測）

## 平常怎麼啟動

每次開機後，依序開兩個 PowerShell 視窗：

```powershell
python core/model_server.py     # 視窗 1：模型（約 1 分鐘載入）
python web_demo.py              # 視窗 2：網站
```

Docker Desktop 要開著，Tailscale 要登入著。要對外時確認 `tailscale funnel status` 有顯示開啟。
電腦當伺服器時：插著電源、Windows 電源模式設「最佳效能」、睡眠設「永不」。

---

# 第二部分：給學校電腦上的 Claude Code

## 先做這些

1. **這台的使用者是隊友，不是專案負責人。** 記憶快照（`docs/handoff/claude-memory/`）是負責人那邊的記憶，
   裡面說的「使用者」指的是負責人。專案知識可以照用；**偏好**（例如一律用繁體中文）請跟隊友確認是否適用，
   不要直接套用。記憶裡「真實管理員身分僅限 neilhuang.mobile 的 Google 帳號」是負責人那台的設定，
   這台的管理員看 `.env` 的 `ADMIN_EMAILS`。
2. **還原記憶**：把 `docs/handoff/claude-memory/*.md` 複製到你的記憶資料夾（system prompt 會給路徑）。
   快照是 2026-10-09 傍晚的最新版（23 個檔）。記憶資料夾已有同名檔案時，先問隊友。
3. 讀 `CLAUDE.md`、本文件，以及記憶裡的：
   - `2026-10-09-dual-machine-gpu-requirement`：雙機設定原則
   - `2026-10-09-customer-cluster-onboarding-project`：客戶自帶叢集的完整進度
   - `2026-10-09-gemini-keys-tos-issue`：**為什麼這台要用隊友自己的 Gemini key**
   - `2026-10-09-next-roadmap-frontend-redesign`：專案接下來的順序
4. 陪隊友走完第一部分。能用指令做的由你執行，每一步**實際驗證**後才說完成。

## 一定要做的設定

- **git 身分用隊友自己的**（只設這個 repo）：
  ```powershell
  git config user.name "<隊友的 GitHub 帳號>"
  git config user.email "<隊友的 email>"
  ```
- **不要 commit 這台的 `users.json`**：這個檔案在版控裡，但兩台各自有自己的網站帳號。
  這台註冊帳號後檔案會變動，commit 上去會蓋掉負責人那台的帳號資料，也會把密碼雜湊推上 GitHub。執行：
  ```powershell
  git update-index --skip-worktree users.json
  ```
  之後 git 就不會把這個檔案的變動當成要 commit 的改動。（負責人那邊有一個待決事項：把 `users.json` 移出版控，
  見 `docs/handoff/NEW_MACHINE_HANDOFF.md` 第 3 節。）
- **`.env` 建立方式**：從 `.env.example` 複製，照第一部分步驟 5 的表格填。`FLASK_SECRET_KEY` 用
  `python -c "import secrets;print(secrets.token_hex(32))"` 產生。**任何金鑰都不要寫進會被 commit 的檔案**。
- gitleaks pre-commit hook 不會跟著 clone 過來：`cp scripts/git-hooks/pre-commit .git/hooks/pre-commit`
- 不要在系統環境變數設 `ANTHROPIC_API_KEY`：負責人那台設了之後，Claude Code 的 claude.ai 連接器全部被停用。

## 這台電腦要特別注意的事

### RTX 5080 是 Blackwell 架構

- 一定要用 `requirements-nvidia.txt`（torch `2.9.1+cu128`）。舊的 cu121/cu118 版不認得這張卡，會出現 `no kernel image is available`。
- 驗證：
  ```powershell
  python -c "import torch;print(torch.__version__, torch.cuda.is_available(), torch.cuda.get_device_name(0), torch.cuda.get_device_capability(0))"
  ```
  預期：`2.9.1+cu128 True NVIDIA GeForce RTX 5080 Laptop GPU (12, 0)`
- `core/hardware.py` 會依顯存自動選 bf16，不用改設定。`/health` 若顯示 `4bit`，代表偵測到的顯存小於 10GB，可能抓錯裝置，要查。
- 筆電插電、電源模式「最佳效能」，否則 GPU 降頻，延遲會跟家裡差很多。

### 學校網路可能擋的東西

| 症狀 | 可能原因 | 處理 |
|---|---|---|
| 客戶連線檢查顯示「你的叢集拉不到部署設定」，`kubectl -n <namespace> describe gitrepository zt-apps` 出現 SSH 連線逾時 | 學校網路擋 **port 22**，Flux 用 SSH 連 GitHub | GitHub 支援走 443 的 SSH（`ssh.github.com:443`）。要改 `cluster/onboarding.py` 的 GitRepository URL 為 `ssh://git@ssh.github.com:443/<org>/<repo>.git`，known_hosts 也要加 `[ssh.github.com]:443` 的主機金鑰。**這是程式改動，要先跟隊友和負責人確認**，因為兩台共用程式碼 |
| 手機行動網路打不開對外網址 | 學校網路擋 Tailscale | 先用手機熱點確認；真的被擋就只能換網路，或改用 Cloudflare Tunnel（`docs/setup.md` 7.5 節方法 B） |
| 安裝 Flux 或拉映像檔一直卡住 | 學校網路慢或擋 ghcr.io | 安裝腳本會等 10 分鐘；不行換手機熱點，分辨是網路還是程式問題 |
| pip 或模型下載停在 0 bytes | 負責人家裡的網路遇到大量平行連線會卡住，學校不一定 | 用 `pip --timeout 20 --retries 10` 和 `scripts/download_models.py`（單線下載），不要用 uv 或 `hf download` |

### 家裡踩過的坑

- 改了 PATH 後 VS Code 的終端機還是舊 PATH，`python` 可能跑到別的版本：**完全關掉 VS Code 再開**。
- 設定 `PUBLIC_BASE_URL`（https）後，cookie 帶 Secure 旗標，用 Python requests 打 `http://127.0.0.1` 會一直未登入；
  **測試用對外網址**，瀏覽器不受影響。
- 新的 GitHub org 預設關閉 deploy key（申請 C 第 3 步）。
- 多步驟的修改腳本一定用 `&&` 串起來，否則前一步失敗後一步照跑。

## 開發工具安裝清單

```powershell
# 外掛來源要先加，否則每個外掛都會顯示 not found
claude plugin marketplace add anthropics/claude-plugins-official
claude plugin install pyright-lsp@claude-plugins-official
claude plugin install github@claude-plugins-official
claude plugin install security-guidance@claude-plugins-official
claude plugin install claude-md-management@claude-plugins-official
claude plugin install superpowers@claude-plugins-official
claude plugin install modern-web-guidance@claude-plugins-official
# playwright、claude-security、frontend-design 若隊友的 claude.ai 帳號沒有自動同步，也從 claude-plugins-official 安裝

claude plugin marketplace add deanpeters/Product-Manager-Skills
claude plugin install voice-of-customer-miner@pm-skills
claude plugin install jobs-to-be-done@pm-skills
claude plugin install opportunity-solution-tree@pm-skills
claude plugin marketplace add wondelai/skills
claude plugin install strategy-growth@wondelai-skills
claude plugin marketplace add danium/lateral-thinking
claude plugin install lateral-thinking@lateral-thinking

npm install -g pyright
winget install GitHub.cli
winget install AquaSecurity.Trivy
winget install Gitleaks.Gitleaks
winget install astral-sh.uv
uv tool install bandit
uv tool install pip-audit
uv tool update-shell
```

- `thinking-skills`（tjboudreaux/cc-thinking-skills）：先看 GitHub issue #10 修好了沒。沒修好就 clone 到本機、
  把 `.claude-plugin/plugin.json` 的 `"skills/"` 改成 `"./skills/"` 再用本機路徑安裝。
- 3 個手動 skill：把 `docs/handoff/user-skills/` 底下的資料夾複製到 `~/.claude/skills/`。
- 裝完**逐個實際呼叫驗證**，不能只看 `/mcp` 顯示綠燈。

## 兩台共用程式碼時的規則

- 兩台都會 push 到同一個 GitHub repo 的 `main`。**開工前先 `git pull`**，避免衝突。
- `web_demo.py` 是 7000 多行的單一檔案，前端 HTML 也在裡面。兩人同時改它很容易衝突；改大功能前先跟負責人說好誰改哪裡，
  或開分支再合併。合併後搜尋 `<<<<<<<`，衝突標記藏在 HTML 字串裡時 Python 照樣能跑，但網頁會壞。
- 有實質分量的改動寫進 `CHANGELOG.md`；評審可能追問的問題寫進 `docs/security_review.md`（目前到第 33 節）。

## 目前專案狀態（2026-10-09 下午）

- `main` 已推上 GitHub，CI 通過（171 passed、15 skipped），負責人本機 `pytest` 419 個全過。
- **已完成**：雙機模型設定（AMD 已實測，**NVIDIA 這台待你驗證**）、網站上外網、客戶自帶叢集
  （同意書、貼指令、連線檢查、部署、擴縮、回滾、刪除、三種自癒都在真的客戶叢集上實測過）、兩人並行測試 29/29。
- **專案接下來的順序**（負責人交代的）：
  1. 這台設定完成並驗證 NVIDIA 路徑
  2. 處理 Gemini key 的合規問題（記憶 `2026-10-09-gemini-keys-tos-issue`）
  3. 前端大改（記憶 `2026-10-09-next-roadmap-frontend-redesign`），動手前先出計畫給負責人看
  4. 舊電腦留下的待辦功能
- 隊友要做哪一部分，以負責人和隊友的分工為準。不確定時先問。

## 合作方式

- 驗證過才能說做完；說「可能」「應該」之前先去查。
- 大改動先出計畫，等同意再動手（`AGENT_RULES.md`）。
- 這個專案要拿去口試，重視有實測數據、有來源的說法。
