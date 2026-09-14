# 版本更新紀錄

只記錄有實質分量的改動（新功能、修好的漏洞/bug、架構調整），單一檔案內的 typo/文案
調整這種小改動不收錄——那些留在對應的 git commit 訊息裡就好。每筆記錄格式固定：

**日期時間** — 類型：改了什麼、為什麼、怎麼驗證（簡述，詳細版在對應 commit /
`docs/security_review.md`）。

從 2026-09-14 開始記錄（這之前的沒有補，只往後記）。

---

## 2026-09-14

### 00:54 — 修復：補齊路由登入檢查 + 弱密碼雜湊自動升級
`GET /api/status`、`GET /api/dataset/stats`、`POST /api/dataset/run` 三個路由原本沒做
登入檢查；`admin` 帳號還停在無鹽 SHA-256 舊格式。補上跟其他路由一致的 session 檢查，
登入成功那一刻順便把舊格式雜湊升級成 pbkdf2。（commit `fb4b36e`）

### 01:01 — 修復：部署預設資源上限 + 安全硬化、`requirements.txt` 釘死版本
`k8s_deploy()` 過去沒指定 memory/cpu 時完全不設資源限制，改成套用 `agents/cost_agent`
既有的「依 image 類型推薦資源」規則；新增 `securityContext.allowPrivilegeEscalation:
false`。`requirements.txt` 全部從 `>=` 下限改成釘死到目前實測跑得動的版本。
（commit `41ee309`）

### 01:08 — 修復：部署前檢查單一 Pod 資源需求是否超出叢集節點容量
發現一個靜默失敗：要求 64Gi/32 核的 Pod，API 回報「部署成功」，但實際上會永遠卡在
`Pending`（`FailedScheduling`）。新增 `k8s_get_node_capacity()` 查真實節點容量，
部署前偵測到單一 Pod 超出最大節點容量就直接 `block`，不是等它卡住才讓使用者發現。
（commit `e57df70`，附驗證數據的補充記錄在 `4849a3b`）

同時建立 `docs/security_review.md`（給報告/口試用的風險審查記錄，格式固定
風險→為什麼重要→解決方式→驗證），之後每次抓到評審可能會追問的問題都寫進這份檔案。
（commit `80d13af`）

### 01:41 — 新功能：Healer 自動修復真正接上診斷/補救邏輯 + 背景常駐監控
過去 Web UI 的「Fix」/「Auto Fix」只是直接刪 Pod，完全沒呼叫 `healer/diagnose.py`、
`healer/remediate.py`。新增 `_real_heal_pod()` 接上規則層/LLM 根因診斷 → 對應補救動作
（調記憶體、rollout undo、重建 Pod 等）；新增背景執行緒每 30 秒自動掃描 + 修復，
不需要使用者手動觸發。用故意建立 `ImagePullBackOff` 的測試 Deployment 驗證過完整鏈路。
（commit `8b1966b`）

### 01:55 — 新功能：pytest 測試骨架 + GitHub Actions CI
專案原本沒有任何自動化測試。新增 60 個測試覆蓋 `agents/`、`guardian/yaml_validator.py`、
`healer/diagnose.py`（規則層）、`healer/remediate.py`（dry-run）；新增
`.github/workflows/test.yml`（push/PR 自動跑，故意不裝 torch 等 GPU 依賴）。
（commit `02eb1bb`）

### 02:13 — 修復：`fix_image` 對「從未成功過」的部署給出正確診斷
`kubectl rollout undo` 在 Deployment 從建立起就沒有成功 revision 時會失敗，原本顯示成
容易誤導的「回滾失敗：<原始錯誤>」。改成偵測這個情境，明確告知使用者「不是暫時性
故障，最可能是 image tag 打錯，需要人工修正」。（commit `e78bacf`）

### 02:15 — 修復：`guardian/policy_rules.yaml` 的 naming/images 規則從未被執行過
審查 `agents/guardian` 覆蓋度時發現 `denied_names`、`max_name_length`、`denied_images`、
`allowed_registries` 這些政策從寫進去起就沒被 `yaml_validator.py` 讀取或執行——政策
文件宣稱有把關，實際上完全沒接。新增 `_check_naming()`/`_check_images()` 接進驗證流程。
（commit `ead10ca`）

### 02:35 — 修復：Prompt injection 重新驗證，改用確定性攔截
實測發現假冒 ChatML 特殊標記能讓 Qwen2.5-3B 完整洩漏系統提示詞（3/3 命中），還能
重新打開已修好的幻覺。兩種 prompt 層修法（token 淨化、加拒絕規則）都測試失敗——
證明 3B 模型能力太弱，不是加系統提示能解決的。改用 `_looks_like_prompt_injection()`
在文字進模型前用 regex 確定性攔截已知手法。（commit `34af042`）

### 02:55 — 新功能：Chat 輸出端事實核對（第二道防線）
只擋輸入端的已知手法還不夠——新增 `_verify_grounded_reply()`，不管模型是被騙還是
自己幻覺，只要回覆對一個實際不存在的 Deployment 講出肯定的健康狀態，就用真實 K8s
清單核對、攔截並改成更正訊息。跟 6.1 節的哲學一致：不相信生成過程，只驗證結果。
（commit `d9f3b4c`，殘留限制與資源受限風險記錄在 `ce483bc`）

### 03:08 — 修復：`/api/healer/status` 不再謊報背景執行緒還活著
背景自動修復的「running」狀態原本只在啟動那一刻設一次，之後執行緒死掉也會永遠
回報「運行中」——直接違反專案自訂的「判斷即時、不用舊值唬弄」原則。改成即時查
`Thread.is_alive()` + 比對 `last_scan` 時間戳判斷有沒有卡住。（commit `072e138`）

### 03:11 — 新功能：`web_demo.py` 路由層測試
之前 77 個測試全部集中在純邏輯層，唯一的真實入口 `web_demo.py`（4800+ 行）完全零
測試覆蓋。新增 32 個測試，含關鍵的「未登入打受保護路由要回 401」回歸測試、註冊/登入
完整流程、部署與 scale 的輸入驗證。（commit `59d3587`，負載測試缺口記錄在 `4ca50c6`）

### 03:28 — 修復：修好第一次真的推上 GitHub 就壞掉的 CI
CI 設定寫好後第一次真的 push 上去就失敗——pytest 在套用 `-m "not integration"` 篩選
前會先 import 每個測試檔案做 collection，CI 沒裝 torch/flask 時裸的
`import web_demo` 直接讓整批 collection 炸掉，標記本身擋不住這個。改用
`pytest.importorskip()` 取代裸 import。（commit `26dfdf0`）

### 20:08 — 新功能：多租戶隔離（每人一個 K8s namespace）+ Gemini 惡意行為偵測與自動封鎖
原本只想加「Gemini 判斷惡意誘導行為→累犯封鎖帳號→刪除該帳號 Pod」，實作前發現系統
完全沒有記錄「Deployment 屬於哪個帳號」的缺口，擴大成完整的多租戶隔離架構：
每個帳號一個真正的 K8s namespace（`user-<帳號>`），`web_demo.py` 裡 28 處寫死
`NS="default"` 全部改成 `namespace` 參數化；節點資源容量檢查跟 port 衝突檢查刻意
維持跨 namespace（實體資源共用）；Healer 背景迴圈改掃全叢集；Chat 歷史跟帳號綁定。
`core/gemini_client.py` 新增 `classify_malicious_intent()`，累積 3 次違規自動封鎖
帳號並刪除其 namespace。真實環境端到端驗證：兩帳號互相看不到彼此資源、`kubectl`
確認 namespace 真的建立/刪除；先部署真實 Pod 再送惡意訊息，確認第 3 次違規時帳號被
封鎖、namespace 連同裡面的 Pod 整個消失、重新登入正確顯示封鎖訊息。新增 16 個測試，
全部 130 個測試通過且 CI 綠燈。（commit `2d6e52a`，補充的端到端驗證與更精確的
Gemini 不穩定性數據記錄在 `0865405`）

**實測發現的殘留限制**：Gemini 分類器對同一句已知攻擊文字的判定本身不穩定（不只是
API 偶爾 503，即使 API 正常運作也可能給出不同判定），跟本地確定性 regex 規則是完全
不同等級的可靠度——這層只能當輔助判斷，不是唯一防線。詳見
`docs/security_review.md` 第 11 節。

**同一天也二次確認的決定（未變動程式碼）**：安全模型三項（`0.0.0.0` 監聽、開放
自行註冊、無 RBAC）使用者主動問過要不要重新考慮，明確決定仍然維持先不動。
（commit `7dc6181`）

### 21:33 — 新功能：CHANGELOG.md 版本更新紀錄
建立這份檔案本身，並在 `AGENT_RULES.md` 新增規則：之後每次有實質分量的改動都要
主動補一條進來，不用等使用者交代。（commit `3dfaf9f`）

### 21:55 — 新功能：Healer 視覺化——一行式 Pod 清單 + 點擊進入的趨勢圖詳情頁
取代原本只有掃描/修復按鈕的 Healer 頁面。清單顯示使用者自己 namespace 底下**所有**
Pod（不只異常的），每個 Pod 一行文字、CSS 用 `flex + overflow:hidden + ellipsis`
確保不換行、長名稱自動截斷；點擊進入詳情頁看容器狀態、最近事件、（不健康時）診斷
根因跟 Fix 按鈕。

新建一套時間序列記錄機制：`_healer_background_loop()` 每 30 秒 tick 額外查一次全
叢集所有 Pod（`list_pod_for_all_namespaces()`），存輕量快照（phase/restarts/
ready/total）進記憶體內的 `_pod_history`（key 是 `namespace/pod_name`，上限 120
筆 ≈ 1 小時）。新增 `GET /api/pods/<name>/history`（照多租戶隔離規則，只回傳使用者
自己 namespace 的資料）。詳情頁用手寫 inline SVG（沒有另外裝圖表套件）畫兩張圖：
重啟次數趨勢折線圖、健康狀態時間軸色帶；資料點少於 2 筆時顯示「觀察中」而不是
空圖或報錯。

已知限制：K8s Pod 名稱不是永久的，Healer 真的刪掉重建壞 Pod 後，新 Pod 會有新
名稱、歷史自然斷掉重新開始（沒有跨 Pod 名稱拼接歷史，那是要比對 `app` label 的
更大工程，這次沒做）。

驗證：真實環境部署測試 Pod，等 2-3 個 30 秒 tick 後確認 `/api/pods/<name>/history`
正確累積出多筆取樣點；確認 `/api/pods/<name>` 詳情跟頁面渲染都正常、說明書 modal
沒有受影響；新增 8 個測試覆蓋 `_pod_light_snapshot`/`_get_pod_history`（含跨帳號
隔離不外洩），既有 130 個測試沒有回歸，全部 138 個測試通過。

### 22:15 — 修復：`/api/metrics` 沒有跟上多租戶隔離、Healer 橫幅簡體字殘留
使用者用瀏覽器實測時直接發現：Healer 頁面顯示自己只有 3 個 Pod，但 Metrics 頁面
顯示 9 個——`/api/metrics` 的 Prometheus 查詢跟 k8s fallback 都還寫死
`namespace="default"`（多租戶隔離改成每人一個 namespace 之前留下的），從沒更新過，
一直顯示舊的共用 `default` namespace 的加總（`auto-app`(5)+`my-cache`(2)+`zt-smoke`(2)=9），
跟使用者自己實際部署的數量完全無關。改成查詢使用者自己的 namespace；不分 namespace
的整叢集數字改名成 `cluster_pods`、跟「你自己的」數字分開標示，不再混在一起看。
順手更新了 Namespace 詞彙解釋（中英文兩份），原本寫「本系統將所有資源建立於 default
namespace」已經不是事實，改成正確描述每人一個 namespace 的隔離機制。

另外修好 Healer 自動監控橫幅裡兩個簡體字（「扫描」→「掃描」），是這次 session
唯一漏掉沒轉繁體的地方，已全文掃描確認沒有其他遺漏。

過程中發現一個相關但不同的既有問題（這次沒有動）：`prometheus_up` 這個欄位是寫死
`True`，沒有真的檢查 Prometheus 是否連得上，即使 Prometheus 現在連不到，畫面還是會
顯示「Online」——跟這次修的「namespace 寫死」是同一種模式（宣稱 vs 實際落差），
但範圍不同，先記錄不修，之後有空再處理。
