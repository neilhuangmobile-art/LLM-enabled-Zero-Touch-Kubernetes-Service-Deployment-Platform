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

過程中發現一個相關但不同的既有問題：`prometheus_up` 這個欄位是寫死
`True`，沒有真的檢查 Prometheus 是否連得上，即使 Prometheus 現在連不到，畫面還是會
顯示「Online」——跟這次修的「namespace 寫死」是同一種模式（宣稱 vs 實際落差）。

### 22:25 — 修復：Metrics 頁面「Prometheus: Online」是寫死的，沒有真的檢查連線
使用者馬上追問要不要順便修掉上面剛記錄的問題。查證後發現不只後端寫死，前端
`loadMetrics()` 也**從沒讀取** `m.prometheus_up` 這個欄位（後端算好了但前端沒用），
畫面顯示「Online」的真正邏輯是「只要 `/api/metrics` 這個 API 路由本身沒有拋例外」，
跟 Prometheus 有沒有真的連得上完全無關——這是兩層獨立的落差疊在一起。

改成先打一次 `/api/v1/query?query=up` 做真的連線探測，這次請求「真的成功執行」
（不管有沒有查到資料）才算連得上，跟「連得上但查無資料」是不同的意思。已知連不上
時直接跳過剩下的 Prometheus 查詢（省掉 3 次 timeout=2 秒疊加的等待時間），改用
K8s API 直接查（跟原本的備援邏輯一樣，只是現在會誠實顯示「Offline」+ 資料來源
改變了，不會讓使用者以為 Prometheus 連得上）。

驗證：實測環境的 Prometheus 目前真的連不上，修好前 API 回 `prometheus_up: true`
（假的），修好後正確回 `false`、`source: "k8s-api-direct (Prometheus unreachable)"`；
Pod 數字仍然是真的（改用 K8s API 直接查，不受影響）。138 個測試沒有回歸。

### 23:40 — 新功能+稽核：真的部署 Prometheus（kube-prometheus-stack），並修好過程中發現的另外兩個「宣稱有但沒接上」落差
使用者問「裝了 Prometheus 能有什麼效果」，回答完使用者說「當然要做」，並要求做完後
全系統再稽核一次。查證發現：這個叢集從來沒有真的裝過 Prometheus，`observability/`
整個模組（查詢客戶端 + 11 條告警規則 + Grafana 儀表板）從寫進去那天起就是死代碼。

用 Helm 裝 `kube-prometheus-stack`（含 Operator/Prometheus/Alertmanager/
kube-state-metrics/node-exporter/Grafana），LoadBalancer 曝露到 `127.0.0.1:9090`。
過程中另外修好兩個獨立落差：(1) `alert_rules.yaml` 的 `PrometheusRule` 套用成功但
label 對不上 Operator 的 `ruleSelector`，Prometheus 從沒真的讀取這 11 條規則，補
`release: kube-prom` label 後 `/api/v1/rules` 確認全部載入；(2) `prometheus_client.py`
的 CPU/記憶體查詢在 Docker Desktop 上因為 `container!=""` 過濾條件永遠查不到資料
（該環境 cAdvisor 沒有 `container` label），拿掉這個過濾條件改用 Pod 層級彙總。

`web_demo.py` 的 `/api/metrics` 改用 `PrometheusClient`（拿掉重複的土砲版本）；
`/api/pods/<name>` 新增 `real_usage`，Healer Pod 詳情頁顯示真實 CPU/記憶體用量，
查不到時明確顯示「無法取得」而非留空白或誤導成 0。Grafana 儀表板透過 API 匯入成功
（過程中發現 host 3000 port 被另一個無關專案佔用，Grafana 改用 3001 port）。

全系統誠實度稽核：`grep` 過 `web_demo.py` 找其他寫死狀態旗標，確認既有的
`_check_docker`/`_check_k8s_live`/`_healer_bg_liveness`/`api_status` 都是真的即時
檢查，沒有找到新的假象。

驗證：`kubectl get pods -n monitoring` 六個元件全部 Running；兩個測試帳號登入
`/api/metrics` 回 `prometheus_up: true`；部署真實 Pod 等待取樣後 `real_usage` 回真實
數字（CPU 0.0 核、記憶體 16.5 Mi）；Grafana 儀表板 14 個 panel 確認匯入成功；
`pytest`（138 個）全過；兩帳號互相看不到彼此的 pod，確認新增的 `monitoring`
namespace 沒有被誤撈進使用者查詢；測試帳號與測試 Pod 已清除。詳見
`docs/security_review.md` 12 節。

### 23:20 — 修復：Metrics 頁面「Full UI」連結是寫死的舊網址（使用者實測發現，稽核漏網）
使用者截圖回報 Metrics 頁面右下角「PromQL Quick Reference」卡片的「Full UI」連結寫死
`http://192.168.50.219:30922`——查證是很久以前某次遠端環境/NodePort 設定殘留下來的
舊網址，跟這次真正裝上去的 Prometheus 位址（`127.0.0.1:9090`）完全對不上，點下去會
連到不存在的地方。這是上一筆記錄的全系統稽核只查了 Python 後端的旗標邏輯，沒搜尋
前端 HTML 裡的靜態連結字面值，漏掉的同一類假象。

改成用 `/api/metrics` 回傳的真實 `url` 動態填入連結（跟畫面上 Endpoint 卡片顯示
同一個值），Prometheus 連不上時顯示「無法取得位址」而非死連結。順手 `grep` 過整份
`web_demo.py` 找其他寫死的 `http://`/`https://` 網址，確認沒有其他殘留。138 個測試
全過。記錄在 `docs/security_review.md` 12 節（「稽核漏網之魚」段落）。

### 23:33 — 新功能：Pods/Deployments 分頁（每頁 20 筆）+ 最新部署排最上面
使用者要求 Pods/Deployments 列表超過 20 筆要能換頁（上一頁/下一頁 + 第幾頁/共幾頁），
且最新部署的要排在最上面。後端 `k8s_get_pods`/`k8s_get_deployments` 改用真正的
`creation_timestamp`（不是格式化後的字串）排序，最新的排最前面；前端 `loadPods()`/
`loadDeployments()` 一次抓回全部資料後在瀏覽器端切頁（`PAGE_SIZE=20`），新增
`podsGoPage()`/`depsGoPage()` 控制翻頁，頁碼超出範圍（例如刪除後）會自動拉回最後一頁。

驗證：建立 23 個測試 Pod（超過 20），`/api/pods` 確認總數 23、排序是最新建立的在前
（同一秒內建立的多個 Pod 因為 K8s `creation_timestamp` 只有秒級精度，彼此之間順序
不保證，但跨秒的排序正確）；138 個測試全過；測試 Pod 與帳號已清除。

## 2026-09-16

### 14:20 — 修復：多租戶上線後 `_review_deployment()` 部署衝突檢查沒跟著查對 namespace
全通讀一次程式碼時發現：`web_demo.py` 的 `_review_deployment()`（`/api/deploy/parse`、
`/api/deploy` 共用的審查函式）名稱衝突檢查用 `k8s_get_deployments()`（無參數，預設查
共用的 `default`），port 衝突檢查用 `k8s_get_services()`（沒帶 `all_namespaces=True`）
——這是每人一個 namespace 上線時漏改的地方，跟同一份檔案裡新寫的 `/api/deploy/conflicts`
已經修好的寫法不一致。改成 `_review_deployment(parsed, namespace=None)`：名稱衝突查
使用者自己的 namespace，port 衝突跨所有 namespace 查；`_prepare_deploy()`/
`api_deploy_parse()`/`api_deploy()` 一併補上 namespace 參數傳遞。實際部署本身
（`k8s_deploy(..., namespace=user_ns)`）沒有查錯 namespace，只有審查階段顯示的警告
文字不準，不是資料外洩問題。

同時順手修好兩個較小的落差：`0_touch_generate_pods.py` 讀
`gitops/manifest_writer.write_manifest()` 回傳值時打錯 key 名稱（讀
`committed`/`commit_hash`/`manifest_path`，實際回傳的是
`ok`/`files`/`commit_sha`/`message`），導致確認訊息一直印出空白（commit 動作本身
沒受影響）；`observability/__init__.py` 的 `cluster_health_summary()` 還留著
`container!=""` 過濾條件（Docker Desktop 環境會讓查詢永遠回空結果，
`prometheus_client.py` 早就修過同一個問題，這裡沒跟著改；目前這個函式全專案沒有
呼叫端在用，是死代碼，先修掉避免以後接上時重踩同一個坑）。

驗證：三個檔案 `python -m py_compile` 通過；`pytest -m "not integration"`（66 個）
全過，無回歸。

### 16:40 — 新功能：Chat 模糊語意理解層（指代消解 + 模糊比對資源名稱 + 部署缺欄位反問 + 一般意圖信心不足也主動澄清）
使用者要求做「模糊語意處理」，確認範圍是四件事：① 指代消解（「把它擴大到 5 個」
「剛剛部署的那個」）② 模糊比對真實資源名稱（打錯字、只講暱稱/類別）③ 部署缺欄位
時判斷該猜還是該反問 ④ 一般查詢/部署意圖信心不足時也要主動澄清（原本只有破壞性
操作有這層把關）。詳細設計見計畫檔 `cheerful-roaming-canyon.md`。

新增 `core/fuzzy_match.py`（`fuzzy_match_resource()`/`pick_confident_match()`，用
stdlib `difflib` 抓打字錯誤 + 重用 `agents/cost_agent._detect_app_type()` 做類別比對，
不新增外部依賴）與 `core/deploy_ambiguity.py`（`deploy_ambiguity_check()`，判斷
image/app_name 完全沒線索時該反問而不是套用 `nginx:latest` 預設值）——兩者都刻意
獨立成不需要 torch/flask 的小模組，可以直接進 `pytest -m "not integration"`。
`llama_client.py` 的 `ask_llama()` 所有成功路徑統一經過新增的 `_finalize_deploy_result()`
套用這個判斷。

`core/model_server.py` 的 `_clean_intent()` 把「信心不足/必要 arg 缺 → 降級成 clarify」
的把關範圍從只有破壞性操作，擴大到 `describe_pod`/`pod_health`/`describe_deployment`
（`_INTENT_ARG_KEYS` 早就定義了必填 `name`，只是先前沒被檢查到）跟 `deploy`（純信心
門檻，不檢查必要 arg，因為 deploy 的欄位全部是選填的）。

`web_demo.py` 新增 `_resolve_intent_name()`：需要指名資源的動作在真正執行前，先用
新增的 `_looks_like_pronoun()` 判斷抓到的「名稱」其實是不是指代詞（換成聊天室記得的
`lastResource`，沒有可參考對象就反問「你是指哪一個」），再用 `fuzzy_match_resource()`
核對這個名字在叢集裡存不存在／夠不夠確定（前綴/打字錯誤且分數夠高才靜默修正，
分數不夠或有多個相近候選就降級成 clarify 並附候選清單，完全找不到就明講「找不到」）。
`/api/intent` 新增接受可選的 `client_intent`（前端規則已經解析出動作+名稱時，只做
核對不重新分類意圖）跟 `last_resource`（指代消解用）。前端 `sendChat()`／
`flowExecuteDeploy()`／`flowExecuteAction()`／`runReadAction()` 相應更新：追蹤每個
聊天室的 `lastResource`、需要名稱的動作一律先跟伺服器核對一次再執行、`clarify`
卡片新增候選按鈕可以直接點選（`resolveClarifyCandidate()`）。

驗證：新增 `tests/test_fuzzy_match.py`（17 案例）、`tests/test_deploy_ambiguity.py`
（8 案例）、`tests/test_intent_resolution.py`（20 案例，含 `/api/intent` 路由與
`_prepare_deploy()` 端對端測試，monkeypatch 掉 K8s 呼叫）；`pytest`（183 個，含需要
本機 torch/flask 環境的 integration 測試，這台機器剛好都裝了）全過；
`python -m compileall -q .` 全專案語法檢查通過。

### 18:40 — 修復：`pick_confident_match()` 對「完全相符」的判斷被鄰近的前綴相符誤擋
真實啟動 model_server.py + web_demo.py，用 Playwright 跑一輪真實瀏覽器端對端測試
（部署 → 指代消解「把它擴大到 N 個」→ 打錯字刪除 → 新聊天室裸指代詞 → 模糊部署）
時發現：部署 `cache-service` 後說「把它擴大到 4 個」，指代消解正確把「它」換成
`cache-service`，但接下來的模糊比對核對卻把這個完全相符的結果判成「不夠確定」，
彈出候選清單要使用者自己選——因為 `_all_resource_names()` 把 Deployment 名稱跟它
自己的 Pod 名稱混在同一個候選池，K8s 的 Pod 命名慣例是「Deployment 名稱 + hash
後綴」，所以 `cache-service`（exact，1.0 分）跟它自己的 Pod `cache-service-xxx`
（prefix，0.9 分）分數差距只有 0.1，低於 `pick_confident_match()` 原本設的 0.15
門檻，被誤判成「有歧義」。這個 bug 只會在真實叢集資料（Deployment + 它產生的 Pod
同時存在）上才會重現，前一輪只測 mock 資料的單元測試沒抓到。

修法：`pick_confident_match()` 對 `reason=="exact"` 的候選一律直接採用，不跟第二名
比分數差距——完全相符沒有模糊空間，其他候選只是剛好共享命名前綴的相關資源，不是
真的有歧義。修好後同一個瀏覽器流程重測：「把它擴大到 4 個」正確顯示
`cache-service：2 → 4 副本`，確認後 K8s 端實際 scale 成功（`已調整 cache-service
replicas=4`）；接著故意打錯字「刪除 cache-servic」（這次因為已經有 4 個 Pod，多個
Pod 都在前綴相符的候選之列，正確判斷成「真的有歧義」而不是誤判，列出候選清單，
沒有自動選一個去執行刪除）。

新增 `tests/test_fuzzy_match.py::test_exact_match_wins_even_with_a_close_runner_up`
跟 `tests/test_intent_resolution.py::test_resolved_deployment_name_is_not_ambiguous_with_its_own_pods`
兩個回歸測試，直接重現這個真實案例的資料形狀。`pytest`（185 個）全過。

**手動測試過程中另外發現、記錄但這次沒修的邊界情況**：在全新聊天室（沒有任何
`lastResource` 可用）直接打「它健康嗎」，`matchClientRule`/`_rule_intent` 都抓不到
（`[a-zA-Z0-9]` 開頭的正則對純中文指代詞無效），落到 `/classify`（3B 模型）分類；
模型受 few-shot 範例裡「web-frontend」這個例句名稱影響，把它當成一個真的資源名稱
硬塞進 `args.name`（沒有照 INTENT_SYSTEM 的指示留白），而不是輸出低信心或空
`args`。下游 `_resolve_intent_name()` 的模糊比對安全網有接住——`web-frontend` 在這個
測試帳號的 namespace 裡真的不存在，正確回報「找不到叫「web-frontend」的 Pod 或
Deployment」，沒有假裝查到健康狀態、也沒有執行任何動作，是安全的，但措辭對使用者
來說有點誤導（使用者會困惑「我沒有說 web-frontend」）。理想反應應該是「你是指哪一
個？」（`needs_reference`），可以透過在 `core/model_server.py` 的
`INTENT_FEWSHOT` 補一筆「裸指代詞 + 沒有 lastResource 時該留白 args」的範例來改善，
但這次先誠實記錄現況，留給下一輪處理，不臨時擴大這次的修復範圍。

### 20:15 — 新功能：Google 登入（Sign in with Google）
使用者要求加上「用 Google 帳號登入」，讓使用者不用自己想帳密。新增第二種登入方式，
跟既有帳密系統共存（既有 6 個帳號完全不受影響）。用專案已有的 `requests` 套件手動
打 Google OAuth2 三個端點（authorize → token exchange → userinfo），沒有加新依賴。

`web_demo.py` 新增：`_google_oauth_configured()`（`GOOGLE_CLIENT_ID`/
`GOOGLE_CLIENT_SECRET` 沒設定時登入頁按鈕整個不顯示）、
`_derive_username_from_google()`（從 email 前綴衍生 username，撞到任何既有帳號一律
加後綴、絕不覆蓋/合併——防止帳號冒用）、`/auth/google/login`（CSRF state 產生）、
`/auth/google/callback`（state 核對、email_verified 檢查、用 `google_sub` 查找/
建立帳號）。`login()` 順手補：純 Google 帳號被拿去試密碼登入時，給「請改用 Google
登入」的明確訊息，不是含糊的帳密錯誤。

新增 `tests/test_google_auth.py`（18 案例）；`pytest`（203 個）全過。安全考量詳細
記錄在 `docs/security_review.md` 13 節（CSRF state、email 驗證、絕不自動合併帳號
的理由）。**待使用者接續**：需要自己申請 Google OAuth 憑證（Google Cloud Console）
填進 `.env` 才能實際測試真實登入流程，這部分沒辦法代勞。

### 23:58 — 新功能：部署時偵測「疊加其他部署後」的叢集容量，不只看單一 Pod
使用者問「如果讓別人連線使用，系統能不能算出對方電腦性能並警告」。第一版理解錯了
方向（做了瀏覽器裝置效能提示，只偵測連線者自己電腦的核心數/記憶體，未 commit 就
被使用者糾正並移除）——使用者真正要的是：**部署時**讓使用者知道「要部署的東西
（包括一次部署很多副本、或很耗資源的單一 Pod），疊加叢集目前已經被其他人用掉的
資源之後，這台跑 K8s 的電腦到底負不負荷得了」。

查證後發現原有的 `_prepare_deploy()` 只檢查「單一 Pod 大到連一個節點都放不下」
（例如要求 64Gi 但節點只有 8Gi），完全沒考慮「疊加其他使用者已經部署的東西 +
這次要求的副本數」後總量會不會超出容量——這種情況下 Pod 會卡在 Pending 卻顯示
部署成功，跟已有的 `_check_scale_risk()`（只用在事後調整 replicas，不含初次部署）
是同一種風險，只是少了初次部署這條路徑。

`_prepare_deploy()` 新增疊加檢查：現有叢集總量 + 這次請求的 cpu/mem × 副本數，
超出節點容量就 `block`（跟單一 Pod 過大同一個等級的阻擋，不是只是警告），訊息
講清楚「多出來的 Pod 會卡在 Pending 卻顯示部署成功」跟解法（降低副本數/資源，
或先移除縮小其他部署）。此功能由組員在 `feat/chat-unified-assistant` 分支開發，
這裡是移植進本機分支的紀錄；新增的共用函式 `_sum_cluster_resource_requests()`
只用於這個新檢查，既有的 `_check_scale_risk()` 維持原本已驗證過的內嵌加總寫法
不變，避免無謂改動。

驗證：直接呼叫 `_prepare_deploy()` 測試單一 Pod 放得下但兩個副本（15 核 ×2）疊加
超出 20 核節點容量的案例，確認 `rejected: true` 正確擋下；對照組（200m CPU/256Mi
正常規模部署）確認不受影響，只是照原本的邏輯給其他警告，沒有被誤擋。203 個測試
全過。

### 00:20 — 新功能：Chat 部署流程 B2 資源卡新增「叢集空間」對照表
使用者確認 Chat 的部署流程（B1 規格確認卡可填 pods/cpu/memory → B2 資源+審查卡
系統自動算出總量+成本+三方審查 → 確認才真的部署）已經是他想要的功能，不用重做，
但指出 B2 資源卡只顯示「這次要部署多少」，沒有顯示「叢集現在已經被用掉多少、
部署後還剩多少」——只有真的超量時才會靠上一筆記錄的容量檢查跳出阻擋訊息，平時
使用者看不到即時的空間狀況。

`_resource_summary()` 新增查詢 `k8s_get_node_capacity()` + `_sum_cluster_resource_requests()`
（沿用上一筆記錄新增的共用函式），算出節點總容量、叢集現有用量、套用這次部署後
的剩餘空間，回傳到新的 `cluster_capacity`/`cluster_used`/`cluster_remaining_after`
欄位；查不到（K8s 未連線）就整組回 `None`，前端對應顯示「叢集容量目前無法取得」
而不是留空白或顯示 0（避免看起來像「還有很多空間」的誤導）。前端 `resourceTableHTML()`
在既有資源表下方新增「叢集空間 / Cluster headroom」對照表，剩餘空間變負數時文字
變紅色提醒。此功能同樣由組員開發，這裡是移植進本機分支的紀錄。

驗證：真實測試確認算式正確（20-0.5-0.2=19.3）；203 個測試全過。

**2026-09-19 接續**：把組員在 `feat/chat-unified-assistant` 分支上獨立開發的這兩項
功能（本節這兩筆），移植進本機這個 `feat/chat-unified-assistant-local` 分支（本機
先前一直沒有接 git，兩邊各自累積了不同的改動）。移植方式：直接比對兩邊
`web_demo.py` 差異、手動把新增的函式與 UI 區塊搬過來，而不是用 git merge 硬套
（歷史不相關、幾乎每個共用檔案都會整份衝突，逐一手動比對反而更準確）。移植後
203 個測試全過，語法檢查通過。

## 2026-09-19

### 修復：Chat 新對話預設提示方塊點了沒反應
使用者回報新開一個對話時畫面上的預設提示方塊（「部署服務」「檢視叢集狀態」等）點下去
完全沒反應。查證是 `renderChatMessages()` 組出的 `onclick` 屬性本身就是壞的 HTML：
`onclick="...value=${JSON.stringify(c.send)};..."` 裡 `JSON.stringify` 產生的字串本身
帶雙引號，直接嵌進一個同樣用雙引號包住的 HTML 屬性，瀏覽器解析到內層的雙引號就把屬性
提前截斷，`onclick` 實際上只剩下 `...value=` 這半句、`sendChat()` 那段被切掉在屬性外面
變成無效內容——不是邏輯錯誤，是純粹的字串跳脫疏忽，靜態看程式碼不容易發現，要嘛實際
點下去、要嘛檢查渲染後的 HTML 原始碼才看得出來。

修法：`.replace(/"/g,'&quot;')` 把 `JSON.stringify` 輸出的雙引號轉成 HTML 實體再嵌入，
瀏覽器解析屬性值時會自動解回雙引號，JS 字串語法維持正確。用 Playwright 實際點擊驗證：
點下「檢視叢集狀態」卡片後輸入框正確填入 `list pods` 並送出，收到伺服器回覆。

### 修復：一般問答偶爾夾雜簡體字（繁中中間穿插簡體）
使用者回報聊天回覆的繁體中文裡偶爾會出現簡體字。查證 `core/model_server.py` 早就有
`_to_tw()`（用 OpenCC `s2twp` 把小模型偶爾漏出的簡體字轉繁體，`/chat`、`/diagnose`
兩個端點都有接上）而且 `opencc-python-reimplemented` 也早就列在 `requirements.txt`，
邏輯上不該發生——但實測發現這台機器實際跑 `model_server.py` 的 Python 3.9 環境根本
沒有真的裝這個套件（`import opencc` 失敗，`try/except` 吞掉例外讓 `_to_tw()` 悄悄變成
無動作直接回傳原文），跟 12 節記錄過的「宣稱有但沒接上」是同一種模式，只是這次是
「接上了但依賴沒裝」而不是「根本沒接上」。

`pip install opencc-python-reimplemented` 裝好、重啟 `model_server.py` 後直接呼叫
`/chat` 測試多輪問答，逐字元跑過 OpenCC 字元級掃描比對，確認回覆不再出現任何簡體字。

### 修復：一般問答的 Markdown 語法（`**粗體**`、清單、標題）沒有轉成排版，直接顯示原始符號
`renderMsgHTML()` 對 AI 訊息內容完全不做任何處理就塞進 `innerHTML`，模型回覆裡的
`**文字**`、`- 項目` 這類 Markdown 語法因此原封不動顯示成一堆星號/減號，而不是真正的
粗體/清單。新增輕量的 `renderMarkdown()`（純前端小函式，不引入外部套件）：先跳脫
HTML 特殊字元避免注入風險，再轉換標題（`#`）、粗體/斜體（`**`/`*`）、行內程式碼
（`` ` ``）、清單（`-`/`1.`）、程式碼區塊（``` ``` ```）。

刻意**不**在 `renderMsgHTML()` 裡統一套用——`appendMsg()` 的 `content` 參數有兩種完全
不同的來源：一種是模型/後端回的純文字（需要轉 Markdown），另一種是流程卡片
（`renderFlowCard()` 等）自己組好、已經是可信任的 HTML（部署確認卡、資源審查卡等），
如果統一跳脫會把卡片的 `<div>`/`<button>` 標籤整個顯示成逃脫後的文字，等於打斷整條
部署確認流程。改成只在確定是純文字的呼叫端（`runQA()` 的 `d.reply`、
`fmtPodHealth`/`fmtPodDetail`/`fmtDeployDetail`/`healer_scan` 等查詢結果）各自呼叫
`renderMarkdown()`，流程卡片維持原樣直接輸出。用 Playwright 實測一則要求「用條列、
粗體標重點」的問答，確認回覆正確渲染出真正的標題/清單/粗體，且部署流程的規格卡、
資源審查卡、Working 忙碌卡都沒有被誤傷。

### 改動：載入動畫改成放射狀刻度旋轉樣式
使用者提供設計參考圖（放射狀刻度、旋轉淡出的風格），希望整體畫面更和諧一致。原本
Chat「思考中」提示跟部署流程忙碌卡（Reviewing/Deploying/Working）用的是兩顆獨立定義、
样式不一致的圓環 spinner（`border-top-color` 那種只有一段顏色的旋轉圈）。改成共用的
`spinnerRadialHTML(size, light)`：8 根刻度以 45° 間隔排列、`opacity` 從 1 淡出到 .15、
用負值 `animation-delay` 讓一開始就分散在動畫不同進度上（避免第一幀全部同時全亮的
閃爍感），`size` 分 `sm`（16px，聊天思考中提示）/`lg`（32px，流程忙碌卡）兩種，
`light` 參數給深色/彩色背景上用（白色刻度）。順手把登入/註冊按鈕也接上（見下一條）。

### 新功能：登入／註冊按鈕點下去到頁面刷新之間補上載入提示
使用者回報按登入之後要等幾秒頁面才刷新，中間畫面看起來像沒反應。這兩個表單是傳統
HTML 表單直接 POST，沒有任何 JS 接手，使用者能看到的只有瀏覽器原生的頁面載入指示，
不容易注意到。加上 `onsubmit` 處理：按下送出的當下立即把按鈕改成 disabled、內容換成
（白色版）放射狀 spinner + 「Signing in…」/「Creating account…」文字，讓等待有明確的
視覺回饋，同時防止使用者手滑連點造成重複送出。`.btn-primary` 補上
`display:flex;align-items:center;justify-content:center;gap:8px` 讓圖示+文字排版正確。

### 修復（使用者代辦）：B1228016 帳號密碼重設
使用者在筆記中提到 B1228016 忘記密碼，且正確理解到「`users.json` 存的是單向雜湊，
沒辦法反推回原密碼」——這是設計上刻意如此（不可逆雜湊是密碼儲存的基本要求，不是
缺陷）。用 `web_demo.py` 自己的 `hash_password()` 邏輯（`pbkdf2_sha256`，120000 次
迭代）產生新密碼 `qwerty123` 對應的雜湊，直接寫回 `users.json` 對應帳號的
`password_hash` 欄位（`created_at` 等其他欄位不動），重啟服務後用該帳密實際登入
一次確認成功。這是一次性的資料修復，不是新增「忘記密碼」自助流程——自助重設功能
（例如綁定 Google 信箱後可用 Google 帳號登入取代忘記的密碼）使用者另外提出構想，
屬於會修改登入流程的新功能，需要先出計畫，這次沒有動。

驗證：Python 語法檢查、抽出內嵌 `<script>` 用 Node.js 語法檢查（`{{ }}`/`{% %}`
Jinja 佔位符先替換掉再檢查）、`pytest`（203 個）全過；上述每一項都額外用 Playwright
啟動真實瀏覽器操作驗證過（點提示卡片、送出會夾雜 Markdown 的問答、觸發部署流程看
忙碌卡動畫、實際用重設後的密碼登入），測試帳號與對應 K8s namespace 已清除。

## 2026-09-20

### 改動：側邊欄「Chats」區塊移到 Main/Tools 下方
使用者回報 MAIN 底下的「Chat」導覽按鈕點下去只是導向緊貼在正上方、已經看得到的聊天室
（原本 Chats 區塊排在 MAIN 上面），視覺上感覺重複。把 Chats 區塊移到 Main、Tools
兩個區塊下方，`#chat-room-list` 用 `getElementById` 存取、CSS 用 id 選擇器，跟 DOM
位置無關，純粹搬動 HTML 順序，不影響任何既有邏輯。用 Playwright 截圖確認新順序
（Main → Tools → Chats）正確顯示。

### 新功能：監控台（Dashboard）——叢集空間總覽頁面
使用者想做一個類似儀表板的監控台，讓自己部署前就能看到叢集還剩多少空間。討論後確定
方向：不做 per-user 配額（維持現有「隔離可見性、共用實體資源」的多租戶設計，個人配額
系統列入未來工作），做法採用 Kubecost 的 `allocation = max(usage, requests)` 概念——
叢集「已用量」在 K8s requests 加總與 Prometheus 實際使用量之間取較大值，比單看
requests 更貼近真實情況。

新增 `observability/prometheus_client.py` 的 `cluster_cpu_usage_cores()`/
`cluster_memory_usage_bytes()`（比照既有 `pod_cpu_usage()` 的寫法，不加
`container!=""` 過濾條件，這台機器的 cAdvisor 沒有這個 label）；`web_demo.py` 新增
`_my_deployment_resource_breakdown(namespace)`（列出使用者自己的 Deployment 資源
明細，不擴充既有的 `k8s_get_deployments()` 避免影響 Pods/Deployments 頁面既有呼叫端）
與 `_dashboard_summary(namespace)`（整合叢集容量、Prometheus 校正後的已用量、使用者
明細、費用估算，任何一段查不到都優雅降級，不讓整頁掛掉）；新路由 `GET /api/dashboard`；
側邊欄 MAIN 新增 Dashboard 導覽項；新增 `#page-dashboard`（容量/已用量/剩餘空間三張卡、
CPU/記憶體使用率長條圖、我的部署明細表、費用估算卡）與 `loadDashboard()`。

**過程中抓到一個真實 bug**：費用估算重用既有的 `_estimate_monthly_cost()`（設計給
單一容器用，沒填 cpu/mem 時用 `or 100`/`or 128Mi` 頂上預設值），但監控台是把「使用者
全部部署加總」餵進去，加總後真的是 0（例如剛註冊、還沒部署任何東西）時，0 在 Python
是 falsy，會被那個 `or` 誤判成「沒填」，冒出一個不存在的月費——手動 Playwright 測試時
發現「還沒部署任何東西」卻顯示 $4.05，之後真的部署一個 100m/64Mi 的服務後費用反而
「下降」到 $3.78，這個不合理的方向立刻暴露問題。修法：加總結果為 0 時直接回傳 `0.0`，
不呼叫 `_estimate_monthly_cost()`。新增迴歸測試涵蓋這個情境（`tests/test_dashboard.py`
的 `test_zero_deployments_gives_zero_cost_not_fallback_default` 等）。

**額外發現、記錄但這次沒動**：這台機器的 Prometheus（`monitoring` namespace 裡的
`prometheus` Deployment，NodePort 30922）目前連不上（`curl` 直接逾時，跟 2026-09-15
記錄裝上去的 `kube-prometheus-stack`/Grafana 完全對不上，很可能中間某次環境變動被
換掉或移除了）。監控台的 Prometheus 校正邏輯已經確認在「連不上」情境下會正確優雅
降級（退回純 requests 加總，`cluster_used_source` 標成 `"requests"`，不會卡住或報錯），
但無法實測「Prometheus 實際用量 > requests」這個分支在真實環境的行為，只能靠
`tests/test_dashboard.py` 的 monkeypatch 測試涵蓋。這是環境/維運層級的落差，不是這次
改動造成的，留給使用者之後視需要重新確認 Prometheus 部署狀態。

驗證：新增 `tests/test_dashboard.py`（10 案例，涵蓋 Prometheus 用量高於/低於 requests、
連不上、K8s 未連線四種情境）；`pytest`（213 個）全過；Python 語法檢查 + Node.js 語法
檢查內嵌 `<script>`；Playwright 實際登入、部署一個真實測試服務（100m CPU/64Mi 記憶體），
確認部署前後「已用量」「我的部署」「費用估算」三處數字都正確反映變化（0 cores→0.1
cores，$0→$3.78）。測試帳號、K8s namespace、測試部署已清除。
