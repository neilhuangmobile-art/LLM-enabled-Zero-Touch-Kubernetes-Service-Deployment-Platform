---
name: 2026-10-09-customer-cluster-onboarding-project
description: 進行中大工程：客戶自帶叢集（GitOps模式C，平台在GitHub org替每客戶開私有repo，客戶叢集Flux拉取+唯讀回報器）+網站上外網+同意書/貼指令/連線檢查引導流程；計畫已核准，分6階段，全部功能一次做完
metadata:
  node_type: memory
  type: project
  originSessionId: 110ca64c-0ce9-476a-aa64-bad49bb5b63d
  modified: 2026-10-08T19:44:32.424Z
---

2026-10-09 使用者核准計畫（完整版：`C:\Users\kaddy\.claude\plans\fuzzy-orbiting-peacock.md`）。

使用者的決定：
- 模式 C（GitOps）：平台不連進客戶叢集；平台寫 GitHub 私有 repo `zt-<帳號>`（放在平台的 org），
  客戶叢集 Flux 用唯讀 deploy key 拉；另裝**唯讀回報器**每 15 秒 POST 狀態回 `/agent/report`
  （連線檢查與 Healer 都靠它）。回報器不接受任何指令。
- 選 GitHub 平台託管 repo（不是客戶自己的 repo + PR），所以「把關」在我們網站的核准步驟。
- 第一階段就要**全部功能**（部署/刪除/擴縮/更新/回滾/Healer/狀態）支援客戶叢集。
- 網域要免費：先用 Tailscale Funnel，同時申請 GitHub Student Pack（Name.com 免費網域一年）。
- 引導流程放在**登入後**（我決定）：同意書（不同意並離開/同意並繼續）→ 指令頁
  `irm <url>/onboard/<token>.ps1 | iex`（返回/繼續）→ 連線檢查（6 種狀況各給說明 + 重新檢查）。
  管理員帳號豁免、照舊用本機叢集。連線成功過的帳號之後離線仍可進站但顯示紅色橫幅。
- 使用者要測試**兩人同時使用**。

階段：0 使用者準備（Funnel、GitHub org + fine-grained token）→ 1 上外網硬化（waitress/ProxyFix/
cookie/頻率限制/PUBLIC_BASE_URL）→ 2 `gitops/tenant_repo.py` → 3 安裝包/回報器/`/agent/report`
→ 4 引導頁面 → 5 `cluster/backend.py` Local/Remote 抽象 + ~20 路由改寫 + Healer 改 Git 操作
→ 6 測試 + 兩人併發 QA（JSONL+報告+圖表）+ 文件（security_review §32）。每階段完成就 commit。

Flux 版本：v2.9.6（2026-10-01，實作時再確認）。Namespace 加 PSA baseline（restricted 會擋 nginx 等 root 映像）。

進度：
- 雙機配置 commit `4fc34c4`（repo 本地 git user 已設為 neilhuangmobile-art）
- 第 1 階段完成 `9db7158`：core/web_security.py 頻率限制、waitress、cookie 旗標、PUBLIC_BASE_URL。
  坑：waitress 預設清掉不受信任 X-Forwarded-*（ProxyFix 無效）→ 用 waitress trusted_proxy；
  測試 fixture 要每測試 new RateLimiter（否則註冊跨測試累積被 429）。真實外網待使用者設 Tailscale。
- 第 2 階段完成 `d7bdbeb`：gitops/tenant_repo.py（GitHubTenantHost + TenantRepo），16 測試。真實 GitHub 待使用者設 org/token
- 安全修正 `bebfe2e`、`cf9fa68`：tenant_slug/_user_namespace 撞名（中文帳號都變 user-unknown！）→ 乾淨名維持、其他加 sha256[:16] + 註冊時 _namespace_taken 確定性檢查；token 改走 GIT_CONFIG_* env。security_review §32 已用掉，客戶自帶叢集的總結改寫在 §33
- `6f71f86`：註冊時也保留既有帳號的舊規則 namespace（_legacy_user_namespace），防新帳號繼承殘留資源
- 第 3 階段完成 `1169d95`：cluster/{remote_state,onboarding}.py、cluster/agent/reporter.py、/onboard/* 與 /agent/report。本機叢集實跑回報器成功（host.docker.internal 回連、PSA baseline 放行、抓到 CrashLoop log）。pytest 372。
- 第 4 階段完成（見 git log onboarding commit）：cluster/pages.py、CUSTOMER_CLUSTER_MODE 開關（預設關）、安裝包單次下載、回報串流限量。
  坑：這台網路下 Flux 7 個映像檔平行拉取卡在 Pulling fs layer（連單一 docker pull 都卡）→ 請使用者重啟 Docker Desktop；本機 flux-system 已裝（replicas 已恢復 1）。playwright 用 CSS selector，snapshot ref 會失效。
- 第 5 階段完成 `2a1dc56`：cluster/remote_view.py（回報→本機同格式）、cluster/remote_ops.py（Healer→Git）、web_demo 用「同名重新綁定」包裝查詢函式（本機版不動）+ 寫入路由客戶分支 + 客戶背景修復迴圈 + 離線橫幅。真叢集外網實測全部操作通過。**踩坑**：Healer 用 revert_app 修映像檔 → 手動+背景重複觸發 → 設定來回跳 5 次；改 restore_previous_image（跳過 known-bad-images）+ 3 分鐘冷卻。patch 腳本教訓：多步指令一定用 && 串，否則前一步失敗後一步照跑。
- 第 6 階段完成 `763b22d`（另有 `7e5f737` 回報器重啟+修復上限、`a0b6eab` 中文解析修正）：兩人並行 QA 29/29（docs/qa_concurrent_20261009*，
  圖表 Artifact https://claude.ai/artifact/QRwnZQpXqTibqSACkFAnYF）；三條自癒路徑真叢集實測全過（映像檔、crash loop、OOM 8→16→32Mi）；
  Tailscale 有傳真實 IP、偽造 XFF 繞不過頻率限制；測試帳號/namespace/GitHub repo 已清除。
- **工程完成**。殘留待辦：①`.env` 尚未加 `CUSTOMER_CLUSTER_MODE=1`（使用者決定；不加則重啟後回本機模式、客戶看不到同意書）
  ②還沒用另一台電腦/網路（學校筆電）當客戶實測 ③只支援 Windows 客戶 ④本機 Docker Desktop 留有測試時裝的 Flux v2.9.6
- 接下來照 [[2026-10-09-next-roadmap-frontend-redesign]]：前端大改 → 舊電腦待辦功能
- 第 0 步完成：PUBLIC_BASE_URL=https://user.tail4c2fd2.ts.net（Funnel→127.0.0.1:5050）、GitHub org zerotouch-tenants + token。坑：新 org 預設**關閉 deploy key**（422 'Deploy keys are disabled'）→ Org Settings→Member privileges→Deploy keys→Enabled。
- **端到端實測成功（2026-10-09）**：外網註冊→同意→PowerShell 貼指令 5 步全過→Flux 用 deploy key 從 GitHub 拉→回報器經外網回報 204→連線檢查 connected→進站。測試帳號 zz-e2e-one（ns zt-zz-e2e-one-428d5dd9d91aa8c6）待第 5 階段驗完再清（users.json 備份在 %TEMP%/claude/users.json.bak3）。web_demo 目前用環境變數 CUSTOMER_CLUSTER_MODE=1 跑（.env 未改）。

相關：[[2026-10-09-dual-machine-gpu-requirement]]、[[2026-10-08-market-research-result]]
