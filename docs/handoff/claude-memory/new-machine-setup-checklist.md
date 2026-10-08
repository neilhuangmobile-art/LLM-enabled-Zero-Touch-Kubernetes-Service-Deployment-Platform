---
name: new-machine-setup-checklist
description: 換新電腦後的安裝待辦順序——先裝好 requirements.txt（專案本體），再裝 Claude Code 外掛/MCP/skill/安全掃描 CLI；附完整指令清單與每項的驗證方式
metadata:
  node_type: memory
  type: project
  originSessionId: 8e788281-87b8-4ad7-8a83-515bd08023fe
  modified: 2026-10-08T04:03:57.216Z
---

使用者 2026-10-08 交代：換新電腦後，**順序固定是「先裝好 requirements，再裝這些工具」**。
專案本體跑得起來優先，開發輔助工具其次。

## 第一步：專案本體（先做完才進第二步）

照 [[2026-10-07-new-machine-python-version-risk]]：直接裝最新版 Python，跑
`pip install -r requirements.txt`，卡住就升級卡住的套件版本（不是回頭裝 3.9）。
另外 Node.js（npx 用）、uv、Docker Desktop、kubectl、helm 也要先有。
驗證：`pytest -m "not integration"` 能跑、`python web_demo.py` 能啟動。

## 第二步：開發工具（2026-10-08 在舊電腦上裝好並逐一實測過的組合）

**claude.ai 連接器**（Context7、Google Drive、Claude Docs）跟著帳號走，不用重裝。
Gmail/Calendar 舊電腦上一直沒授權，要用才到 claude.ai Settings → Connectors 授權。

**Claude Code 外掛**（終端機執行，先 `npm install -g @anthropic-ai/claude-code@latest`，
舊版 2.1.92 曾導致 claude-security 的 hook 載入失敗）：
```
claude plugin install playwright@claude-plugins-official
claude plugin install pyright-lsp@claude-plugins-official
claude plugin install github@claude-plugins-official
claude plugin install security-guidance@claude-plugins-official
claude plugin install claude-md-management@claude-plugins-official
claude plugin install superpowers@claude-plugins-official
claude plugin install claude-security@claude-plugins-official
claude plugin install modern-web-guidance@claude-plugins-official
```
2026-10-08 建議但還沒裝的：`deepeval@claude-plugins-official`、
`webapp-testing`（`/plugin marketplace add anthropics/skills` 後
`/plugin install webapp-testing@anthropic-agent-skills`）。

**市場研究/發想 skill（2026-10-08 裝好）**：
```
claude plugin marketplace add deanpeters/Product-Manager-Skills
claude plugin install voice-of-customer-miner@pm-skills
claude plugin install jobs-to-be-done@pm-skills
claude plugin install opportunity-solution-tree@pm-skills
claude plugin marketplace add wondelai/skills
claude plugin install strategy-growth@wondelai-skills
```
另外 `~/.claude/skills/` 有 3 個從 anthropics/knowledge-work-plugins 的 product-management 抽出來的
skill（product-brainstorming、competitive-brief、synthesize-research，含 Apache-2.0 LICENSE）——
**刻意不裝整個 product-management 外掛**，因為它附帶 16 個用不到的 MCP 連接器（Slack/Linear/
Notion/Figma/Gmail…），每次開對話都會跳登入提示。新電腦要從那個 repo 複製這 3 個資料夾過來，
並把 SKILL.md 裡 `../../CONNECTORS.md` 的連結改成同層的 CONNECTORS.md。

**靈感發想/盲點分析 skill（2026-10-08 裝好）**：
```
claude plugin marketplace add danium/lateral-thinking
claude plugin install lateral-thinking@lateral-thinking
```
`tjboudreaux/cc-thinking-skills`（28 個思考框架）**直接從 GitHub 裝會失敗**：它的
`.claude-plugin/plugin.json` 寫 `"skills": ["skills/"]`，新版 Claude Code 要求 `./` 開頭
（repo issue #10，2026-09-26 回報，作者未修）。做法：`git clone --depth 1` 到
`~/.claude/local-marketplaces/cc-thinking-skills`，把那行改成 `"./skills/"`，再
`claude plugin marketplace add <本機路徑>` + `claude plugin install thinking-skills@thinking-skills-marketplace`。
裝之前先看 issue #10 是否已修好，修好就直接從 GitHub 裝。本機版不會自動更新。

**前置/CLI 工具**：
```
npm install -g pyright
winget install GitHub.cli        # 裝完開新終端機再 gh auth login
winget install AquaSecurity.Trivy
winget install Gitleaks.Gitleaks
uv tool install bandit
uv tool install pip-audit
uv tool update-shell             # 把 ~/.local/bin 加進 PATH，否則 bandit/pip-audit 找不到
```

**GitHub token**：新產生一組 fine-grained token（只選這個 repo；Actions/Contents/
Pull requests/Metadata 唯讀；設到期日，不要 No expiration），
`setx GITHUB_PERSONAL_ACCESS_TOKEN "..."`，然後**完全關閉 VS Code 再開**才讀得到。

**gitleaks pre-commit hook**：`.git/hooks/` 不進版控，重新 clone 會不見，要請 Claude
重建（內容與設計見 `docs/security_review.md` 29 節；重點是用 `GIT_CONFIG_*` 環境變數
關掉 core.safecrlf，否則 Windows LF/CRLF 警告會讓所有 commit 被擋）。

**Why**：舊電腦上這套工具是逐個踩坑才裝好的（PATH 沒刷新、終端機 Claude 版本太舊、
token 環境變數要重啟 VS Code 才生效），照順序做可以少踩一次。

**How to apply**：使用者說換好新電腦/要接續工作時，主動提醒這份清單，確認第一步完成
後再帶第二步；全部裝完照舊電腦的做法逐個實際呼叫驗證（playwright 截圖 localhost:5050、
pyright 查 symbol、github MCP 讀 commit、gh run list、gitleaks 掃歷史），不要只看
`/mcp` 顯示 Connected 就當作裝好。
