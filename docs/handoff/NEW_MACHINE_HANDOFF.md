# 換電腦交接：給新電腦上的 Claude Code

寫於 2026-10-09，舊電腦（Windows、RTX 3060 Laptop 6GB）最後一次 session 留下。
使用者換了一台比較強的新電腦（使用者說「16 ram」，**推測是 16GB VRAM，請實際查證**），
並明確說之後**可以放手改**。

> **新電腦的 Claude，請先做這三件事，再回應使用者的第一個需求：**
> 1. 讀完這份文件
> 2. 把 `docs/handoff/claude-memory/` 的記憶還原到你的記憶資料夾（見第 1 節）
> 3. 跟使用者確認目前進行到第 2 節的哪一步

---

## 1. 還原記憶（最優先）

舊電腦的 auto-memory 存在 `~/.claude/projects/<專案路徑編碼>/memory/`，**不會跟著 git 走**。
完整快照（16 個檔案，含 `MEMORY.md` 索引）已經複製到
[`docs/handoff/claude-memory/`](claude-memory/)。

**做法**：system prompt 會告訴你這台電腦的記憶資料夾路徑。把 `docs/handoff/claude-memory/*.md`
全部複製過去（**不要覆蓋**新電腦上已經存在的同名檔案，有衝突就問使用者）。

快照是 2026-10-09 的狀態。之後新增的記憶請寫進新電腦的記憶資料夾，不用回寫到這裡。

---

## 2. 安裝順序（使用者明確交代：**先裝好 requirements 讓專案跑起來，再裝開發工具**）

完整指令在記憶快照的 [`new-machine-setup-checklist.md`](claude-memory/new-machine-setup-checklist.md)，
這裡列重點和容易踩的坑。

### 第一步：專案本體
1. 系統工具：Python（**使用者決定裝最新版，不裝 3.9**）、Node.js、uv、Docker Desktop
   （要開啟 Kubernetes 功能）、kubectl、helm、git。
2. `pip install -r requirements.txt`。**很可能卡在 torch 或 bitsandbytes**，因為版本號都釘在
   舊版，新版 Python 可能沒有對應的安裝包。遇到時**升級卡住的那個套件**，再確認
   transformers、peft、bitsandbytes 三者的版本彼此相容，最後更新 `requirements.txt`。
   **不要叫使用者回頭裝 3.9。** 詳見 [`2026-10-07-new-machine-python-version-risk.md`](claude-memory/2026-10-07-new-machine-python-version-risk.md)。
3. **`.env` 不在 git 裡**（裡面有金鑰）。請使用者從舊電腦用 USB 或雲端硬碟手動複製過來，
   **絕不能 commit**。舊電腦的 `.env` 有這些欄位（這裡只列名稱）：
   `GEMINI_API_KEYS`（3 把，逗號分隔）、`FLASK_SECRET_KEY`、`GOOGLE_CLIENT_ID`、
   `GOOGLE_CLIENT_SECRET`、`HF_TOKEN`、`HUGGINGFACE_HUB_TOKEN`、`ANTHROPIC_API_KEY`、
   `USE_LLM_NORMALIZE`、`MODEL_SERVER_HOST`、`MODEL_SERVER_PORT`、`PROMETHEUS_URL`、
   `ARGOCD_SERVER`、`ARGOCD_TOKEN`、`GITOPS_REPO_PATH`、`HF_HOME`。
   - `HF_HOME`、`GITOPS_REPO_PATH` 這類**路徑**是舊電腦專用的，要改成新電腦的路徑
   - 模板在 `.env.example`
4. **簡報 pptx 不在 git 裡**（`.gitignore` 刻意排除 `reports/*.pptx`），一樣要手動複製。
5. 模型會重新下載：`Qwen/Qwen2.5-3B-Instruct`（部署用）、`Qwen/Qwen2.5-1.5B-Instruct`（監控用）、
   `BAAI/bge-m3`（RAG embedding）。設定在 `core/config.py`。
6. **K8s 叢集狀態不會跟過來**：要重新安裝 Prometheus/Grafana（`kube-prometheus-stack`，
   做法見 [`observability/README.md`](../../observability/README.md)），使用者的 namespace
   會在第一次部署時自動建立。舊電腦的 3000 port 被別的專案佔用，所以 Grafana 改用 3001；
   新電腦不一定有這個問題。
7. 重建 RAG 索引：`python rag/build_index.py --rebuild --deploy`
8. 驗證：`pytest`（舊電腦最後是 **295 個全過**）、`python core/model_server.py`、
   `python web_demo.py`（port **5050**）。

### 第二步：Claude Code 開發工具
照 [`new-machine-setup-checklist.md`](claude-memory/new-machine-setup-checklist.md) 安裝
外掛、MCP、CLI 掃描工具（gitleaks、trivy、bandit、pip-audit）。

幾個容易踩的坑：
- 終端機版 Claude Code 要先更新到最新版，舊版會讓 claude-security 載入失敗。
- `uv tool update-shell`，否則 bandit、pip-audit 會找不到。
- GitHub token 用 `setx` 設定後，**要完全關掉 VS Code 再開**才讀得到。
- **gitleaks pre-commit hook**：備份在 [`scripts/git-hooks/pre-commit`](../../scripts/git-hooks/pre-commit)，
  執行 `cp scripts/git-hooks/pre-commit .git/hooks/pre-commit` 就裝好了（`.git/hooks` 不進版控）。
- **3 個手動抽出的 skill**（product-brainstorming、competitive-brief、synthesize-research）
  備份在 [`docs/handoff/user-skills/`](user-skills/)，複製到 `~/.claude/skills/` 即可。
  刻意不裝整個 product-management 外掛，因為它附帶 16 個用不到的 MCP 連接器。
- `thinking-skills`（tjboudreaux/cc-thinking-skills）直接從 GitHub 裝會失敗，原因是原作者
  的 bug（issue #10）。做法見清單；先看 issue 修好了沒，修好就直接裝。
- 全部裝完要**逐個實際呼叫驗證**，不能只看 `/mcp` 顯示綠燈。

---

## 3. 這次推送的狀態

- `main` 已經用 `feat/chat-unified-assistant` 的完整內容覆蓋（快轉合併，沒有改寫歷史）。
- **組員 ericsung0428 有兩個分支，裡面的功能沒有進 main**，使用者決定這次先不合併：
  - `origin/feat/chat-unified-assistant-claude-fixes`（91e6b0b，2026-10-03）：
    Google 帳號綁定（`/auth/google/bind`、`google_bind()`、`_start_google_oauth()`）+ Chat UX 修正。
    **抽查確認 `google_bind` 目前不在 main 裡。**
  - `origin/feat/chat-unified-assistant-local`（7 個 commit，2026-09-19～20）：Dashboard、
    模糊語意理解等。main 已經有 Dashboard（可能是手動移植時改過寫法），但 commit 內容不完全相同。
  - **之後要跟組員確認**這些功能要不要合併。`web_demo.py` 是一個 6500 行的單一檔案，前端
    HTML 也在裡面（第 1149～約 4660 行），合併時注意兩點：衝突標記如果藏在 HTML 字串裡，
    Python 照樣能執行、但網頁會壞；合併後要搜尋 `<<<<<<<` 並用 playwright 看前端有沒有錯誤。

- **待決定：`users.json` 要不要移出版控。** 2026-10-09 推送後，security-guidance 的背景審查
  回報「credential-exposure in users.json」。檔案裡是密碼雜湊（使用者確認過不是明碼），
  repo 也是私有的，所以不緊急。不過有兩點：(1) repo 萬一外流，雜湊值可以被離線暴力破解；
  (2) 9/14 的 CHANGELOG 記錄 `admin` 原本是無鹽 SHA-256，只會在**登入時**自動升級成
  pbkdf2，一直沒登入的舊帳號可能還是弱格式。建議做法是移出版控、改提供
  `users.example.json`，並加進 `.gitignore`。但這會影響組員的環境，**要先跟組員協調**，
  也應該照 AGENT_RULES 寫進 `docs/security_review.md`。

---

## 4. 換電腦後的工作優先順序

使用者還沒決定先做哪條線，**開工前要先問**。兩條主線：

1. **多模態／情境部署**（[`2026-10-08-multimodal-scenario-features-backlog.md`](claude-memory/2026-10-08-multimodal-scenario-features-backlog.md)）：
   情境解析部署 + 省錢估算（審核建議的主打）→ 語音 + docker-compose 匯入 → 照片 →
   分時擴縮（先做手動版）。**原始程式碼自動建置：建議不做**（安全理由）。
2. **資料層 → 分析報告 → 動畫**（[`2026-10-07-next-session-feature-backlog.md`](claude-memory/2026-10-07-next-session-feature-backlog.md)、
   [`2026-10-07-personalization-decision.md`](claude-memory/2026-10-07-personalization-decision.md)）。

**兩份市場調查**（`docs/market_research_20261008.md`、`docs/market_research_voc_20261008.md`）
的共同結論：主打「**部署前替新手做決策 + 有人把關的自動化**」；事後診斷（Healer）是紅海，
只能當配角；**最大風險是 QA 測試自己出題自己考**，要補一組外部基準實驗。
使用者原話版報告裡，**語音和照片找不到需求證據**。

**新電腦硬體更強**：記憶裡「6GB VRAM 裝不下第三顆模型」等限制可能不再成立，開工前先查硬體。

每一項都是大改動，照 `AGENT_RULES.md` 要先出計畫、等使用者同意。

---

## 5. 跟這位使用者合作的方式（摘要，完整內容在記憶的 feedback 檔）

- **驗證後才能說做完**：使用者常用簡短追問挖出沒真正驗證過的地方，回答前要真的去查。
- **主動維護** `CHANGELOG.md`；評審可能追問的問題記進 `docs/security_review.md`（目前到第 30 節）。
- 文件、註解用繁體中文；設計原則是「**對新手絕對友善**」（見 `AGENT_RULES.md`）。
- 使用者要拿這個專案去口試，重視有來源、有數據的證據。
