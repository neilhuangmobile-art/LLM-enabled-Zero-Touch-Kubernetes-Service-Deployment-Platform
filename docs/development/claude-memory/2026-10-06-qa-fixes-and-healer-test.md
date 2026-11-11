---
name: 2026-10-06-qa-fixes-and-healer-test
description: 2026-10-06 QA掃描(32題)找到的P0/P1/P2問題已全部修復驗證；擴充prompt injection測試(OWASP改寫+自設計)留3個殘留風險；Healer kubectl exec真實故障測試確認自動偵測修復有效但監控小模型會幻覈根因
metadata:
  node_type: memory
  type: project
  originSessionId: 7df9e735-1dd3-4dd3-9f9f-4efb99c934ca
  modified: 2026-10-06T16:59:20.456Z
---

2026-10-06 完成的工作（同一天兩個 session 接續）：

**第一階段：系統 QA 掃描 + 修復**（完整記錄 `docs/qa_sweep_20261006.md`）
對運行中的 web_demo.py 送 32 題涵蓋各種語意/風險情境的訊息，找到 3 個 P0（安全/
信任）、3 個 P1（功能）、3 個 P2（次要）問題，全部已修復並用 10 題回歸測試驗證：
- 意圖分類器（`/classify`）對破壞性操作只看模型信心、不查核 args 內容是否真的
  出現在原始訊息裡——新增 `_args_grounded_in_message()` 事實查核（`core/model_server.py`）
- `web_demo.py` 規則層 `name` 擷取太寬鬆會吃進整句中文——新增 `_looks_like_resource_name()`
- 部署解析器看不懂中文數字詞、0/負數/超量副本數處理不一致且錯誤訊息無資訊——
  `llama_client.py` 的 `_deterministic_deploy_parse()` 加中文數字對照+負號擷取+
  `_parse_error` 標記
- `###System:` 繞過注入偵測、跨租戶探測問題模型自相矛盾——都已用確定性正則修復

**第二階段：擴充 prompt injection 測試**（OWASP AITG-APP-01 範例改寫 + 自行設計 14 題）
9 題被現有防線攔住，但發現 3 個殘留風險沒修（見 `docs/security_review.md` 21、22
節、「尚待排查」）：角色扮演型 jailbreak（DAN/AntiGPT）模型會順著接受人設、英文
同義詞（disregard）繞過黑名單、對話歷史（history）完全沒經過注入偵測。

**第三階段：Healer kubectl exec 真實故障測試**（完整記錄
`docs/healer_exec_test_20261006.md`，security_review.md 23 節）
部署隔離測試 Pod，`kubectl exec` 進去確認正常，反覆 `nginx -s stop` 製造真實
CrashLoopBackOff，驗證 `web_demo.py` 常駐的背景自癒迴圈（每 30 秒全叢集掃描，
不需要任何手動觸發）78 秒內自動偵測並修復成功。但發現監控小模型（1.5B，
`/diagnose`）在日誌/事件都沒有具體訊號時仍幻覈出「OOM Killed」這個完全沒有證據
的根因（真實事件只有通用 Back-off 重啟訊息）——`DIAGNOSE_SYSTEM` 裡「證據不足
要老實說」的規則對 1.5B 不夠可靠，跟 [[feedback_verify_before_claiming_done]]
是同一類「小模型指令遵循能力弱」的問題，只是這次發生在監控模型而不是部署/聊天
模型。因為規則層已經蓋過模型的 action/severity，這次沒有導致錯誤修復動作，只有
根因說明文字是錯的。

**Why**：這些都是對外展示/評審口試時容易被問到或剛好撞到的真實邊界情況，比單純
看程式碼走查更有說服力（有真實輸入輸出跟時間軸佐證）。

**How to apply**：下次要做的事——(1) 修 Healer 幻覈問題：日誌/事件沒有具體訊號時
用確定性邏輯蓋掉 root_cause，不要讓 1.5B 模型自己決定要不要承認不知道；(2) 視
時間決定要不要補 3 個殘留 prompt injection 風險（history 注入偵測優先度最高，
因為完全沒有任何防護，角色扮演偵測次之）；(3) 其他故障模式（OOMKilled、
ImagePullBackOff）還沒實測過，行為是否跟 `_RULE_PATTERNS` 寫的一致待驗證。
