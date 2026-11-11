---
name: 2026-10-07-admin-identity-and-residual-fixes
description: 新增真實管理員身分(僅neilhuang.mobile這個Google帳號)；修復2026-10-06 QA測試留下的3個殘留prompt injection風險與Healer監控模型幻覈根因問題，全部已驗證
metadata:
  type: project
  originSessionId: 7df9e735-1dd3-4dd3-9f9f-4efb99c934ca
  modified: 2026-10-06T17:15:55.893Z
---

2026-10-07 完成並驗證的工作（接續 [[2026-10-06-qa-fixes-and-healer-test]] 留下的待辦）：

**管理員身分**：`core/config.py` 新增 `ADMIN_EMAILS`（預設含 neilhuang.mobile@gmail.com，
之後加組員改 `.env` 即可）。`web_demo.py` 新增 `_is_admin_user()`：只有 Google 登入且
email 在允許清單才算管理員，密碼帳號（含 users.json 裡那個字面叫 "admin" 的舊測試帳號）
永遠不算。`session["is_admin"]` 在 `_enforce_ban()` 每次請求即時重算。前端顯示「管理員 /
Admin」標籤。`core/model_server.py` 新增 `_looks_like_authority_claim()`，`/chat` 聊天裡
自稱「我是系統管理員」會被固定拒絕文案擋下，不進模型判斷——身分只能由登入系統決定。

**3 個殘留 prompt injection 風險已修**：DAN/AntiGPT 角色扮演 jailbreak、ignore 的英文
同義詞（disregard/forget/bypass/override）、`/chat` 的 `history` 完全沒過注入檢查
（現在對每個 history item 也跑 injection/cross-tenant/authority-claim 三個檢查，命中
就跳過不放進模型 context）。

**Healer 幻覈根因已修**：`healer/diagnose.py` 新增 `_has_concrete_signal()`——規則層
命中已知模式但合併文字沒有具體錯誤關鍵字時，不信任模型的 root_cause，蓋成「證據不足」
老實版本。用 2026-10-06 kubectl exec 真實測試案例（純 CrashLoopBackOff，模型幻覈出
OOM Killed）重現驗證修復有效，新增 `tests/test_healer_diagnose_grounding.py`。

**Why**：這些都是上次 QA 掃描/實測留下的已知待辦，不是新發現；管理員身分是使用者主動
要求補的身分治理機制，動機是「聊天自稱身分不該被信任」跟「密碼帳號命名不該被誤認權限」
兩個真實攻擊面。

**驗證**：`pytest -m "not integration"`（98 個）全過；`pytest -m integration
tests/test_model_server_injection.py`（11 個，含新增的 DAN/同義詞/authority-claim 案例）
全過；`web_demo._is_admin_user()` 對 `neilhuang-mobile` 回 True，對 `admin`/`test`/
不存在帳號都回 False。`docs/security_review.md` 23 節更新為已修復、新增 24 節記錄
管理員身分設計；`CHANGELOG.md` 補 2026-10-07 條目。

**How to apply**：下次如果要讓某個功能真的只給管理員用（目前還沒有任何功能掛在
`is_admin` 底下，這次只是建好身分判斷的地基），檢查 `session.get("is_admin")`
即可，不用再重新設計身分判斷邏輯。改動還沒推到正在跑的 `model_server.py`/
`web_demo.py`——程式碼改了要重啟兩個服務才會生效，下次接續時記得先確認有沒有重啟過。
