---
name: 2026-10-07-large-qa-sweep-and-fixes
description: 206題大規模模糊語意掃描找到2P0+3P1+1P2全部修復驗證；最重要發現是規則層假信心(英文虛詞當資源名)+Gemini正規化讓保護失效；2026-10-08整理成圖表時又發現3個新問題(數字參數無查核等)也已修復
metadata:
  type: project
  originSessionId: 7df9e735-1dd3-4dd3-9f9f-4efb99c934ca
  modified: 2026-10-07T17:35:05.598Z
---

2026-10-07 延續 10/6 的 QA 掃描方法（見 [[2026-10-06-qa-fixes-and-healer-test]]），
把規模從 32 題擴大到 206 題（`qa_sweep_20261007_large.py`），對正在跑的
`web_demo.py` 送真實請求，全部修復並驗證過。

**最重要的兩個發現**：
1. `web_demo.py` 的 `_rule_intent()` 規則層命中就用假 100% 信心直接執行，完全
   不經過事實查核——`_looks_like_resource_name()` 只檢查字串格式，常見英文
   虛詞（the/of/everything/container）剛好都符合 ASCII 格式會被當成真實資源
   名稱（「delete everything in this cluster」被解析成要刪除一個叫
   "everything" 的資源，100% 信心直接執行）。
2. `llama_client.py` 的 2026-10-06 修好的副本數邊界值/多服務保護，檢查的是
   Gemini 正規化「之後」的文字——Gemini 正規化成功時完全繞過、失敗時檢查的是
   可能已遺失原始訊號的改寫文字，導致同一類保護換個講法就隨機失效。

**修法**：
- `_looks_like_resource_name()` 新增常見英文虛詞黑名單，`image` 欄位也補上
  同等查核（原本漏掉）
- `llama_client.py` 新增 `_predeploy_boundary_precheck()`，在呼叫 Gemini 之前
  先對使用者原始輸入做檢查，不管 Gemini 怎麼處理都不會被繞過
- `gitops_log`/`healer_scan` 這類零參數規則補上疑問句判斷，避免把「GitOps 是
  什麼」這種概念問題當指令執行
- 跨租戶探測補「朋友的帳號」句型；角色扮演越獄補「AI called X」不綁定人格名稱
  的語序；新增 Base64/ROT13「解碼+照做」組合請求的確定性攔截

**Why**：過程中發現一個執行層面的教訓——Windows 終端機 cp950 編碼印 emoji 會讓
Python 腳本直接 crash（`UnicodeEncodeError`），這個專案已經在別的地方踩過同一個
坑（`core.config.ensure_utf8_output()`），新寫的測試腳本要記得呼叫這個既有修法，
不要重踩。另外部署解析測試一開始走錯端點（送 `/api/intent` 而不是
`/api/deploy/parse`），`/api/intent` 對 deploy 動作不解析細節，之後類似測試要
注意送對端點。

**How to apply**：下次要擴充 prompt injection 測試時記得，黑名單式規則永遠有
覆蓋率天花板，新發現的繞過手法持續補規則就好，不用期待一次根治。

**驗證**：`pytest`（255 個，新增 24 個回歸測試）全過；真實端到端測試——重啟
`core/model_server.py`/`web_demo.py`，註冊測試帳號，對正在跑的系統重送全部
7 個原始失敗案例，全部確認修復生效。完整報告 `docs/qa_sweep_20261007_large.md`，
`docs/security_review.md` 26 節。測試資源（namespace、測試帳號）都已清除。

---

**2026-10-08 補充：把這批資料整理成量化圖表時又抓到 3 個問題，已經修復**（見
`docs/security_review.md` 27 節）：
1. `_find_ungrounded_args()` 原本只查核字串值，數字型參數（replicas/pods/port）
   完全沒有事實查核，模型捏造的數量會直接通過——跟這份記憶一開始講的「規則層
   假信心」是同一類問題，只是換了資料型別繞過去。新增
   `_number_grounded_in_message()` 補上（支援阿拉伯數字/中文數字詞/英文數字詞）。
2. 「部署九個 redis 副本」image 擷取失敗退回 nginx——image lookahead 補「副本/台」。
3. 「起一個 nginx 跟一個 redis」多服務偵測漏掉——`_MULTI_SERVICE_HINT_RE` 補上
   「X個...跟/和/與...Y個」連接詞結構。

新增 9 個回歸測試，`pytest`（266 個）全過，真實端到端重測 3 個案例確認生效。
`AGENT_RULES.md` 也補了一條規則：以後做系統測試超過 20 題要主動產出結構化
pass/fail + 圖表，不用等使用者交代。

**How to apply（補充）**：整理測試結果成量化報告/圖表這個動作本身，意外變成了
一次有效的回歸複查方式——重新逐題檢視細節時找到了當天測試沒抓到的問題。以後
有大批測試資料，值得把「整理成結構化圖表」當成一次額外的覆核機會，不是只是
為了做報告而做報告。
