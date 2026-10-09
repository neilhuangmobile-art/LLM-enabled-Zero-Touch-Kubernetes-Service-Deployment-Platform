# 學校電腦安裝與設定交接

寫於 2026-10-09，家裡那台新電腦（AMD RX 9060 XT）留下。

這份文件有兩個讀者：
- **使用者本人**：照「第一部分」一步一步做。需要申請、登入、在網頁上點選的地方都寫在裡面。
- **學校電腦上的 Claude Code**：先讀「第二部分」，再陪使用者做第一部分。能用指令完成的步驟，由你代為執行並驗證。

---

## 這台電腦是什麼

| 項目 | 規格 |
|---|---|
| 獨立顯卡 | NVIDIA GeForce RTX 5080 Laptop GPU，16GB 顯存（Blackwell 架構） |
| 內建顯卡 | AMD Radeon 890M（CPU 推測是 Ryzen AI 9 HX 370 這一級） |
| 系統記憶體 | 推測約 32GB（從 DirectX 診斷的共用記憶體 15.9GB 推算，請實際確認） |
| 作業系統 | Windows 11 |

家裡那台是 AMD 顯卡，學校這台是 NVIDIA。程式已經支援兩種，而且兩台用**同一套設定**（模型都用 bf16、不量化），
目的是讓兩台跑出來的 QA 數據一致，口試時才對得上。差別只有 torch 的安裝來源：

| | 家裡 | 學校 |
|---|---|---|
| 安裝檔 | `requirements-amd.txt` | `requirements-nvidia.txt` |
| torch | 2.9.1 + ROCm 7.2.1 | 2.9.1 + **CUDA 12.8** |

---

# 第一部分：使用者照著做

整體順序：**先讓專案跑起來，再裝開發工具**。每個大步驟做完，都請 Claude 驗證過再往下。

## 步驟 0：從家裡那台帶過來的東西

這些**不在 GitHub 上**，要用隨身碟或雲端硬碟手動複製：

| 東西 | 在家裡電腦的位置 | 為什麼不在 GitHub |
|---|---|---|
| `.env` | 專案根目錄 | 裡面有所有金鑰，絕對不能上傳 |
| `dataset/` 資料夾 | 專案根目錄 | 訓練資料，檔案大，已排除在版控外（RAG 部署範例索引會用到） |
| 簡報 pptx | `reports/` | 已排除在版控外 |

> 不要複製 `data/` 資料夾。那是家裡這台平台的客戶連線紀錄，學校這台不該沿用（原因見第二部分「兩台不能同時當伺服器」）。

## 步驟 1：安裝 Windows 基本工具

1. **NVIDIA 顯示卡驅動**：到 https://www.nvidia.com/zh-tw/drivers/ 下載最新版，或用 NVIDIA App 更新。
   RTX 50 系列需要較新的驅動，舊驅動會讓 CUDA 抓不到顯卡。
2. **Git**：`winget install Git.Git`
3. **Python 3.12**：到 https://www.python.org/downloads/release/python-31210/ 下載
   `Windows installer (64-bit)`。安裝時**勾選「Add python.exe to PATH」**。
   - 一定要 3.12。兩台要用同一版，家裡 AMD 的 torch 只支援 3.12。
   - 使用者不使用 venv，套件直接裝在這個 Python。
4. **Node.js**：`winget install OpenJS.NodeJS.LTS`
5. **Docker Desktop**：到 https://www.docker.com/products/docker-desktop/ 下載安裝。不需要註冊 Docker 帳號，安裝畫面可以跳過登入。
6. **WSL**：Docker Desktop 第一次啟動時通常會自動要求安裝。若沒有，用系統管理員 PowerShell 執行 `wsl --install --no-distribution`，裝完**重新開機**。
7. **Helm**：`winget install Helm.Helm`
8. **全部裝完後，重新開機一次**，讓 PATH 生效。

### 如果 Docker Desktop 說「Virtual Machine Platform not enabled」或「此機未啟用虛擬化」

家裡那台遇過。**先不要急著進 BIOS**，用系統管理員 PowerShell 執行：

```powershell
bcdedit /enum "{current}" | findstr -i hypervisorlaunchtype
```

- 沒有輸出，或顯示 `Off`：執行 `bcdedit /set hypervisorlaunchtype auto`，再執行 `wsl.exe --install --no-distribution`，然後重新開機。
- 顯示 `Auto` 但還是不行：才需要進 BIOS 開啟虛擬化（AMD 平台叫 SVM Mode）。

### 開啟 Docker Desktop 的 Kubernetes

Docker Desktop → 右上角齒輪（Settings）→ Kubernetes → 勾選 **Enable Kubernetes** → Apply & restart，
等左下角變綠燈（第一次要下載，約 3～5 分鐘）。

## 步驟 2：下載專案

```powershell
cd D:\           # 或你想放的位置
git clone https://github.com/neilhuangmobile-art/LLM-enabled-Zero-Touch-Kubernetes-Service-Deployment-Platform.git
cd LLM-enabled-Zero-Touch-Kubernetes-Service-Deployment-Platform
```

repo 是私有的，第一次 clone 會跳出 GitHub 登入視窗，用 `neilhuangmobile-art` 這個帳號登入即可。
接著把步驟 0 帶來的 `.env`、`dataset/` 放進這個資料夾。

## 步驟 3：安裝 Claude Code 並請它接手

```powershell
npm install -g --allow-scripts=@anthropic-ai/claude-code @anthropic-ai/claude-code@latest
```

（一定要加 `--allow-scripts`，家裡那台沒加時安裝不完整。）

用 VS Code 打開專案資料夾，開啟 Claude Code，跟它說：
**「請讀 docs/handoff/SCHOOL_MACHINE_SETUP.md，照文件幫我把這台電腦設定好」**。
接下來的步驟 4～8，Claude 可以代你執行大部分指令，你只需要做它請你做的部分。

## 步驟 4：安裝 Python 套件（Claude 可代為執行）

```powershell
python -m pip install --timeout 20 --retries 10 -r requirements-nvidia.txt
```

會從 PyTorch 官方來源下載 CUDA 12.8 版的 torch（約 3GB）。

## 步驟 5：修改 `.env` 裡跟這台電腦有關的設定

`.env` 從家裡帶過來，大部分可以直接用，**只有以下幾項要改**：

| 欄位 | 要改成什麼 | 說明 |
|---|---|---|
| `HF_HOME` | 這台電腦上的路徑，例如 `D:/hf_cache` | 模型下載位置，家裡的路徑在這台不存在 |
| `PUBLIC_BASE_URL` | 先**刪除這一行** | 這台先不當對外伺服器，見第二部分說明 |
| `CUSTOMER_CLUSTER_MODE` | 先不要加，或設 `0` | 同上 |

其餘的金鑰（`GEMINI_API_KEYS`、`HF_TOKEN`、`FLASK_SECRET_KEY`、`GOOGLE_CLIENT_ID/SECRET`、
`TENANT_GITHUB_TOKEN`、`TENANT_GITHUB_ORG` 等）**不用重新申請**，直接沿用。

## 步驟 6：下載模型、建索引、裝監控（Claude 可代為執行）

```powershell
python scripts/download_models.py          # 三個模型約 11GB
python rag/build_index.py --rebuild --deploy
```

監控（Prometheus、Grafana）的 helm 安裝指令在 `observability/README.md`，照抄即可。

## 步驟 7：開發工具（需要你登入或申請的部分）

| 項目 | 你要做的事 | 需要申請嗎 |
|---|---|---|
| GitHub CLI 登入 | 執行 `gh auth login`，選 GitHub.com → HTTPS → Login with a web browser，到瀏覽器輸入畫面上的代碼 | 不用，用既有帳號 |
| GitHub MCP 用的 token | 見下方「申請 GitHub token」 | **要，每台電腦各申請一組** |
| Gmail、Google Calendar 連接器 | 到 claude.ai → Settings → Connectors 授權（不用也可以） | 不用 |

### 申請 GitHub token（給 Claude 的 GitHub 外掛用）

這組是讓 Claude 讀 GitHub 的，跟平台用的 `TENANT_GITHUB_TOKEN` 不同。建議每台電腦各申請一組，哪台電腦遺失時，只要撤銷那一組。

1. 登入 github.com → 右上角大頭貼 → **Settings**
2. 左側最下方 **Developer settings** → **Personal access tokens** → **Fine-grained tokens** → **Generate new token**
3. 填寫：
   - Token name：`claude-code-school`
   - Expiration：選一個口試之後的日期
   - Resource owner：你的個人帳號 `neilhuangmobile-art`
   - Repository access：**Only select repositories** → 選這個專案的 repo
   - Repository permissions：**Actions**、**Contents**、**Pull requests** 都設 **Read-only**（Metadata 會自動變唯讀）
4. 按 **Generate token**，**立刻複製**（只會顯示一次）
5. 在 PowerShell 執行（換成你的 token）：
   ```powershell
   setx GITHUB_PERSONAL_ACCESS_TOKEN "github_pat_……"
   ```
6. **完全關掉 VS Code 再重新打開**，token 才會生效。

## 步驟 8：驗證（Claude 會執行並回報）

全部通過才算設定完成：

1. `python -m pytest` 全部通過（家裡最後是 419 個）
2. `python core/model_server.py` 啟動後，`http://127.0.0.1:8765/health` 要顯示：
   - `"gpu": "NVIDIA GeForce RTX 5080 Laptop GPU"`（不能是 AMD Radeon 890M）
   - `"deploy_precision": "gpu-bf16"`、`"monitor_precision": "gpu-bf16"`
3. `python web_demo.py` 後，用瀏覽器開 `http://localhost:5050` 能登入

## 選做：用學校電腦當「客戶」測試連線

這是家裡那台還沒驗證過的項目：**從另一台電腦、另一個網路連上平台**。需要家裡那台當伺服器同時開著。

1. 家裡電腦：`.env` 加 `CUSTOMER_CLUSTER_MODE=1`，啟動 `web_demo.py`，確認 Tailscale Funnel 開著。
2. 學校電腦：Docker Desktop 開著、Kubernetes 綠燈。
3. 學校電腦用瀏覽器開 `https://user.tail4c2fd2.ts.net`，**註冊一個新帳號**，照畫面讀同意書 → 複製指令 → 貼到 PowerShell → 回網頁按「繼續」。
4. 連線成功後試部署一個服務，確認它出現在學校電腦的 Docker Desktop 裡。

學校網路如果擋住某些連線，檢查頁會顯示原因，Claude 可以幫你判斷，見第二部分的「學校網路」。

---

# 第二部分：給學校電腦上的 Claude Code

## 先做這些

1. **還原記憶**：把 `docs/handoff/claude-memory/*.md` 全部複製到你的記憶資料夾（system prompt 會告訴你路徑）。
   這份快照是 2026-10-09 下午的最新版（22 個檔）。新電腦上的記憶資料夾應該是空的；如果已經有同名檔案，先問使用者。
2. 讀 `CLAUDE.md`（會自動載入）、本文件，以及記憶裡的這幾份：
   - `2026-10-09-dual-machine-gpu-requirement`：雙機設定的原則
   - `2026-10-09-customer-cluster-onboarding-project`：客戶自帶叢集的完整進度
   - `2026-10-09-next-roadmap-frontend-redesign`：使用者交代的下一步順序
   - `feedback_always_reply_in_chinese`：**所有回覆一律繁體中文，包括工具呼叫之間的進度說明**。使用者已經提醒過兩次。
3. 陪使用者走完第一部分。能用指令做的步驟由你執行，每一步都要**實際驗證**後才說完成。
   使用者會用簡短追問挖出沒驗證過的地方（記憶 `feedback_verify_before_claiming_done`）。

## 這台電腦要特別注意的事

### RTX 5080 是 Blackwell 架構

- 一定要用 `requirements-nvidia.txt`，裡面的 torch 是 `2.9.1+cu128`。舊的 cu121/cu118 版不認得這張卡，會出現 `no kernel image is available`。
- 驗證：
  ```powershell
  python -c "import torch;print(torch.__version__, torch.cuda.is_available(), torch.cuda.get_device_name(0), torch.cuda.get_device_capability(0))"
  ```
  預期是 `2.9.1+cu128 True NVIDIA GeForce RTX 5080 Laptop GPU (12, 0)`。
- 這台還有 AMD 890M 內顯，但 CUDA 版 torch 只會看到 NVIDIA 卡，所以 device 0 應該就是 RTX 5080。仍然要確認 `/health` 的 `gpu` 欄位。
- `core/hardware.py` 會依顯存自動選 bf16，**不需要改任何設定**。如果 `/health` 顯示 `4bit`，代表偵測到的顯存小於 10GB，可能抓到了錯的裝置，要查。
- 筆電注意電源：插著電源、Windows 電源模式設成「最佳效能」，否則 GPU 會被降頻，延遲數字會跟家裡差很多。

### 兩台不能同時當對外伺服器

兩台平台會管理同一個 GitHub org（`zerotouch-tenants`）裡的客戶 repo，但 `users.json` 和 `data/clusters.json`
（客戶的連線紀錄、token 雜湊）各自存在自己的電腦上，不會同步。兩台同時對外，會發生帳號和客戶連線各說各話的情況。

- 學校這台預設當**開發機**，或當**客戶**測試，`.env` 不設 `PUBLIC_BASE_URL`、不開 `CUSTOMER_CLUSTER_MODE`。
- 真的要改由學校這台對外：先關掉家裡那台的 `web_demo.py` 和 Tailscale Funnel，再在這台登入同一個 Tailscale 帳號、
  執行 `tailscale funnel --bg 5050`。新網址會不一樣（電腦名稱不同），要把新網址寫進 `PUBLIC_BASE_URL`，
  並到 Google Cloud Console 加新的登入回呼網址 `<新網址>/auth/google/callback`。已經連線的客戶也要重跑安裝指令，因為回報網址變了。

### 學校網路可能擋的東西

學校或公司網路常會擋連線，家裡沒遇過，要留意：

| 症狀 | 可能原因 | 處理 |
|---|---|---|
| 檢查頁顯示「你的叢集拉不到部署設定」，`kubectl -n <namespace> describe gitrepository zt-apps` 出現 SSH 連線逾時 | 學校網路擋 **port 22**，Flux 用 SSH 連 GitHub | GitHub 支援走 443 的 SSH（`ssh.github.com:443`）。要改 `cluster/onboarding.py` 的 GitRepository URL 為 `ssh://git@ssh.github.com:443/<org>/<repo>.git`，known_hosts 也要加上 `[ssh.github.com]:443` 的主機金鑰。**這是程式改動，先跟使用者確認** |
| 安裝 Flux 或拉映像檔一直卡住 | 學校網路很慢，或擋了 ghcr.io | 腳本會等 10 分鐘；不行就換手機熱點測試，分辨是網路問題還是程式問題 |
| pip 或模型下載停在 0 bytes | 家裡那台的網路遇到大量平行連線會卡住（記憶 `2026-10-09-new-machine-amd-gpu-env`）；學校不一定有這個問題 | 照文件用 `pip --timeout 20 --retries 10` 和 `scripts/download_models.py`（單線下載），不要用 uv 或 `hf download` |

### 在家裡踩過的坑（這台也可能遇到）

- 改了 PATH 之後，VS Code 開的終端機還是舊的 PATH，`python` 可能跑到其他版本。**完全關掉 VS Code 再開**。
- 設定 `PUBLIC_BASE_URL`（https）之後，cookie 會帶 Secure 旗標。用 Python 的 requests 打 `http://127.0.0.1` 會一直是未登入，**測試要用對外網址**；瀏覽器不受影響。
- npm 安裝 Claude Code 要加 `--allow-scripts`。
- 新的 git clone 沒有 commit 身分：在 repo 裡執行
  `git config user.name "neilhuangmobile-art"` 和 `git config user.email "neilhuang.mobile@gmail.com"`（只設這個 repo，不改全域）。
- gitleaks pre-commit hook 不會跟著 clone 過來：`cp scripts/git-hooks/pre-commit .git/hooks/pre-commit`。

## 開發工具安裝清單

照記憶 `new-machine-setup-checklist` 安裝。要點：

```powershell
# 外掛來源要先加，否則每個外掛都會顯示 not found
claude plugin marketplace add anthropics/claude-plugins-official
claude plugin install pyright-lsp@claude-plugins-official
claude plugin install github@claude-plugins-official
claude plugin install security-guidance@claude-plugins-official
claude plugin install claude-md-management@claude-plugins-official
claude plugin install superpowers@claude-plugins-official
claude plugin install modern-web-guidance@claude-plugins-official
# playwright、claude-security、frontend-design 會從 claude.ai 帳號自動同步，先用 claude plugin list 確認

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
uv tool install bandit      # 先 winget install astral-sh.uv
uv tool install pip-audit
uv tool update-shell
```

- `thinking-skills`（tjboudreaux/cc-thinking-skills）：先看 GitHub issue #10 修好了沒。沒修好就照記憶裡的做法，clone 到本機、
  把 `.claude-plugin/plugin.json` 的 `"skills/"` 改成 `"./skills/"` 再安裝。
- 3 個手動 skill：把 `docs/handoff/user-skills/` 底下的資料夾複製到 `~/.claude/skills/`。
- 裝完要**逐個實際呼叫驗證**，不能只看 `/mcp` 顯示綠燈。

## 目前專案狀態（2026-10-09 下午）

- `main` 最新 commit：`763b22d`，已推上 GitHub，CI 通過（171 passed、15 skipped）。本機 `pytest` 419 個全過。
- **已完成**：
  - 雙機模型設定（AMD 已實測；**NVIDIA 這台待你驗證**）
  - 網站上外網（Tailscale Funnel、頻率限制、waitress）
  - **客戶自帶叢集**：同意書 → 貼指令 → 連線檢查；部署、擴縮、回滾、刪除、三種自癒都在真的客戶叢集上實測過
  - 兩人並行測試 29/29（`docs/qa_concurrent_20261009.md`）
- **待辦**（照使用者交代的順序，不要跳步）：
  1. 這台電腦設定完成並驗證 NVIDIA 路徑
  2. 選做：用這台當客戶，從不同網路連家裡的平台
  3. **前端大改**：使用者覺得現在的網頁太像 AI 寫的、不夠精緻。要重新調整審美，加入動態互動與動畫，
     拿掉 AI 網站常見的特殊符號（滿版 emoji、「中文 / English」斜線並陳、怪異空格）。動手前載入 frontend-design skill，
     **先出計畫給使用者看，同意後才動手**。細節見記憶 `2026-10-09-next-roadmap-frontend-redesign`。
  4. 前端改完之後，再做舊電腦留下的待辦功能（記憶 `2026-10-07-next-session-feature-backlog`、
     `2026-10-08-multimodal-scenario-features-backlog`）。

## 跟這位使用者合作

- 一律繁體中文，包括工具呼叫之間的每一句說明。
- 驗證過才能說做完。說「可能」「應該」之前，先去查。
- 大改動先出計畫，等使用者同意（`AGENT_RULES.md`）。
- 有實質分量的改動主動寫進 `CHANGELOG.md`；評審可能追問的問題寫進 `docs/security_review.md`（目前到第 33 節）。
- 使用者要拿這個專案口試，重視有實測數據、有來源的說法。
