---
name: next-session-security-audit
description: 安全審查進度追蹤——已完成 CI/測試骨架、guardian 政策落差修復、prompt injection 雙層防線、healer 存活 bug 修復；剩 RBAC/角色分層、LLM 換模型、負載測試待資源
metadata: 
  node_type: memory
  type: project
  originSessionId: 7df9e735-1dd3-4dd3-9f9f-4efb99c934ca
  modified: 2026-09-14T12:09:34.478Z
---

使用者在 2026-09-13 收工前明確說：下次回來要我**徹底排查系統所有的漏洞跟風險**（不只幻覺，是全面的安全/風險審查）。這是下一次 session 開場就該優先處理的事，不用等使用者重新描述。

**Why：** 這次 session 花了很多時間在功能開發（Chat 統一入口、新手友善 UI、幻覺審查與修復），使用者現在想把焦點轉到系統本身的安全性/風險面，作為下一階段的重點工作。

**How to apply：**
- 開場先確認 Model Server / Web Demo 是否還在跑（`curl http://127.0.0.1:8765/health`、`curl http://127.0.0.1:5050/`），環境穩定後直接進入排查，不用重新暖場。
- 排查範圍建議至少涵蓋：
  - **Web 層**：`web_demo.py` 的 auth（`session`、密碼雜湊方式、有沒有 CSRF 防護）、所有 `/api/*` 路由有沒有一致的登入檢查（記得之前查過 `/api/status`、`/api/dataset/stats`、`/api/dataset/run` 沒有登入檢查，`/api/dataset/run` 甚至能跑 subprocess）、輸入驗證是否完整（SQL/command injection 面向，雖然這專案沒用 SQL DB，但 subprocess 呼叫要特別看）。
  - **K8s 層**：`k8s_deploy()`/`k8s_delete_deployment()` 等操作有沒有權限邊界（目前任何登入使用者都能操作整個 cluster，沒有 RBAC/角色分層）、namespace 是否只鎖 `default`、有沒有防止使用者部署惡意 image 或掛載敏感 hostPath 的機制。
  - **Guardian/Orchestrator 三層防護**：`agents/security_agent.py` 目前檢查了哪些項目，有沒有漏洞（例如特權容器、hostNetwork、無限資源請求）。
  - **翻譯層/模型層**：Gemini API key 存放方式（`.env`，已知 gitignored，但要看有沒有其他洩漏管道，例如 log 印出）、prompt injection 現況（`CLAUDE.md` 記錄過部分已知殘留漏洞，例如全形井號、`System:`/`Human:` 繞過，這次沒有再處理，需要重新確認狀態）。
  - **機密資訊管理**：`.env` 裡目前有 Anthropic/Gemini/HuggingFace token，要確認沒有意外被 print 到 log 或回應裡。
  - **`users.json`**：密碼儲存方式、是否有預設/測試帳號留在裡面（這次 session 建了不少 `halluc1`/`suite1`/`conflict1` 這類測試帳號，可能要清理）。
- 這次 session 已經做過的「幻覺審查」（見 git log，commit 訊息有詳細記錄）跟這次要做的「漏洞風險排查」是兩件不同的事，不要混在一起重複做，但可以互相參照（例如幻覈修復裡的 grounding 機制，如果被惡意輸入濫用也算風險）。
- 排查完的呈現方式：使用者過去偏好「實測 + 具體修復 + 驗證」的模式（不是空泛列風險清單），照這次幻覈審查的做法（每個發現都用實際 request 測出來、給修復、再測一次證明修好）進行。

相關記憶：[[project-overview]]（如果之後建立）、可參照 `CLAUDE.md` 的「進行中工作」歷史段落了解系統現況與已知技術債。

**2026-09-15 大幅推進**：使用者本來只想加「Gemini 判斷惡意誘導行為→累犯封鎖帳號→
刪除該帳號 Pod」這個功能，實作前查程式碼發現系統完全沒有記錄「Deployment 屬於哪個
帳號」，於是擴大成**每個帳號一個真正的 K8s namespace**（多租戶隔離架構，用
EnterPlanMode 走完整計畫流程，使用者核准後才動手）：
- `web_demo.py` 28 處寫死 `NS="default"` 全部改成 `namespace` 參數化；`_check_scale_risk`
  （節點容量）跟 port 衝突檢查刻意維持跨 namespace（實體資源共用）；healer 背景迴圈
  改掃全叢集，主動觸發的 healer 路由維持只看自己 namespace；Chat localStorage 加
  `CURRENT_USER` 綁定帳號。
- `core/gemini_client.py` 新增 `classify_malicious_intent()`；累積 3 次違規自動封鎖
  帳號 + `delete_namespace()`；`users.json` 新增 `violation_count`/`banned`；Flask
  `before_request` 集中攔截封鎖帳號。
- **真實環境端到端驗證過**：兩帳號互相看不到彼此資源、`kubectl` 確認 namespace 真的
  建立、舊共用資源（`auto-app`/`my-cache`/`zt-smoke`）留在 `default` 沒被搬動、
  跨 namespace port 衝突正確偵測。新增 16 個單元測試，全部 130 個測試通過，已 push
  上 GitHub 且 CI 通過（`.github/workflows/test.yml`）。
- **實測抓到一個誠實的殘留限制**：連續測 5 次同一句攻擊文字給 Gemini，只有 3 次正確
  判定惡意，另外 2 次是 Gemini 端 `503 UNAVAILABLE`（暫時性過載，不是配額或判斷不
  一致）——證實 Gemini 這層是輔助判斷、fail-open，暫時不穩定時完全依賴本地
  `_looks_like_prompt_injection()` 撐著。已寫進 `docs/security_review.md` 11 節。
- 詳見 `docs/security_review.md` 第 11 節、`CLAUDE.md` 工作慣例新增的 namespace 說明。
- 這次也順帶再次確認：安全模型（`0.0.0.0`/開放註冊/RBAC）使用者二次確認**仍然維持
  先不動**（1.3 節），之後不用再主動問，除非使用者自己重新提起。

**2026-09-14 進度**：已完成第一輪排查並修好幾項（3 個路由缺登入檢查、admin 帳號弱密碼雜湊沒升級、
部署預設沒有資源上限/安全硬化、單一 Pod 資源需求超節點容量會靜默失敗、requirements.txt 版本沒釘死）。
所有風險 + 解決方式已寫進 `docs/security_review.md`（給使用者報告/口試用），以後任何評審可能會追問的
問題都要記進那份檔案，規則已寫進 `AGENT_RULES.md`。使用者知道但決定先不修：API key 曾出現在對話紀錄
建議去後台撤銷重發、`0.0.0.0` 監聽+任何人可自行註冊+無角色分層（2026-09-14 跟 2026-09-15 兩次都問過使用者
要不要重新考慮，兩次都明確回答「仍然先不動」——這是重新確認過兩次的決定，**之後不用
再主動問**，除非使用者自己重新提起）。

**2026-09-14 同一天大量續做的進度**（同一 session 繼續，不是新的一天）：
- **測試/CI 從 0 建起來**：`tests/` 全新 pytest 骨架，涵蓋 `agents/`、`guardian/yaml_validator.py`、
  `healer/diagnose.py`（規則層）、`healer/remediate.py`（dry_run）、`core/model_server.py` 的注入偵測、
  `web_demo.py` 的路由層（含未登入 401 回歸測試）跟 healer 存活檢查，共 114 個測試（本機全裝依賴時）。
  `.github/workflows/test.yml` 建好後**第一次真的 push 上去 CI 直接失敗**（pytest collection 階段
  import 錯誤，`@pytest.mark.integration` 標記擋不住這個），已修好（改用 `pytest.importorskip`）並
  重新推上去驗證通過：https://github.com/neilhuangmobile-art/LLM-enabled-Zero-Touch-Kubernetes-Service-Deployment-Platform/actions
  **教訓寫進 CLAUDE.md「測試」章節**：本機測試全過不代表 CI 會過，本機環境裝了全部依賴會蓋掉這個問題，
  之後改 CI 相關的東西要用一個只裝 CI 會裝的套件的乾淨 venv 重現，不能只在開發環境跑過就算數。
- **`agents/guardian` 覆蓋度審查**：抓到 `policy_rules.yaml` 的 `naming`/`images` 兩個規則區塊
  （denied_names、max_name_length、denied_images、allowed_registries）從寫進去起就沒被
  `guardian/yaml_validator.py` 讀取或執行過，是完全沒用的裝飾設定——已修復（新增 `_check_naming`/
  `_check_images`），跟 healer「Fix 按鈕其實只是刪 pod」是同一種「宣稱 vs 實際」落差模式。
- **prompt injection 重新驗證**：實測發現用假冒 ChatML 特殊標記（`<|im_start|>system...`）能讓
  Qwen2.5-3B 完整洩漏系統提示詞（3/3 命中），還能拿去重新打開已修好的幻覈（讓模型對不存在的服務講
  「健康」）。試了兩種 prompt 層修法都失敗（token 淨化、加拒絕規則），證明 3B 模型能力太弱、不是
  多寫系統提示能解決的。最後用兩層**確定性**防線解決：(1) 輸入端 `_looks_like_prompt_injection()`
  用 regex 攔截已知手法，命中直接拒絕不送進模型；(2) 輸出端 `_verify_grounded_reply()` 跟真實 K8s
  狀態核對，回覆對不存在的服務講「健康」就攔截改成更正訊息（這個是使用者自己選的「方法1」，來自
  「三個治本方法裡哪個最強」的討論，結論是方法1效果最強因為是保證不是機率）。
- **Healer 背景自動修復的關鍵 bug**：使用者本人以「使用者角度嚴厲評判」的方式親自抓到——`/api/healer/status`
  的 `running` 只在啟動那一刻設一次 True，之後永遠不會再檢查，如果背景執行緒中途死掉會永遠謊報
  「運行中」，直接違反 `AGENT_RULES.md` 自己訂的「判斷即時、不用舊值唬弄」原則。已修好（改成即時查
  `Thread.is_alive()` + 比對 `last_scan` 時間戳判斷有沒有卡住）。
- **`web_demo.py` 零測試覆蓋的落差**也是使用者親自抓到後才補的（32 個測試，含未登入 401 回歸測試）。
- **`docs/security_review.md` 新增第 10 節「目前受限於資源，暫時解決不了的風險」**：跟一般的「尚待
  排查」清單刻意分開——3B 模型抵抗新型注入的能力上限（治本需要換更強模型/API，卡在 6GB VRAM 已被
  兩顆模型佔滿、或 API 預算限流）、K8s RBAC/角色分層（需要先設計角色系統，額外開發工作量）、
  `_verify_grounded_reply()` 覆蓋率上限（只抓連字號命名+健康類斷言）。每項都寫清楚為什麼現在做不到
  （卡在哪個資源）跟未來解決方向，供之後有資源時直接對照處理。
- **負載/規模測試**：記錄成獨立缺口、故意不做——不是資源受限，是這台機器單節點容量有限，拿真負載
  測會排擠使用者其他工作，建議之後獨立規劃時間做。
- 使用者對這次工作的互動模式：會主動要求「以使用者角度嚴厲評判」，我給的評判要真的挑出具體、可驗證
  的問題（不是空泛批評），使用者收到評判後會直接要求動手修，修完會追問「測試紀錄有沒有留下來」——
  這種追問通常會挖出更深一層的落差（這次就是這樣發現 CI 從沒真的跑過），之後遇到類似追問要認真查證，
  不要只憑印象回答「有」。**下次回來可以直接問使用者要往哪個方向繼續**（LLM 換更強模型、角色分層、
  負載測試都已經記錄在 security_review.md 第 10 節/尚待排查，供選擇）。
