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

抽出共用邏輯 `_sum_cluster_resource_requests()`（加總全叢集所有 namespace 目前
Deployment 的真實資源需求），`_check_scale_risk()` 改呼叫它（行為不變，純重構）；
`_prepare_deploy()` 新增疊加檢查：現有叢集總量 + 這次請求的 cpu/mem × 副本數，
超出節點容量就 `block`（跟單一 Pod 過大同一個等級的阻擋，不是只是警告），訊息
講清楚「多出來的 Pod 會卡在 Pending 卻顯示部署成功」跟解法（降低副本數/資源，
或先移除縮小其他部署）。

驗證：直接呼叫 `_prepare_deploy()` 測試單一 Pod 放得下但兩個副本（15 核 ×2）疊加
超出 20 核節點容量的案例，確認 `rejected: true` 正確擋下；對照組（200m CPU/256Mi
正常規模部署）確認不受影響，只是照原本的邏輯給其他警告，沒有被誤擋。過程中抓到
一個測試陷阱：改完程式碼後沒重啟舊的 `web_demo.py` process，用 curl 測到的是
還在跑的舊程式碼，誤判邏輯沒生效——後來用 `python -c` 直接呼叫函式跟重啟乾淨的
process 分別驗證過，確認是舊程式殘留而不是邏輯本身的問題。138 個測試全過；
測試帳號與 namespace 已清除。

### 00:20 — 新功能：Chat 部署流程 B2 資源卡新增「叢集空間」對照表
使用者確認 Chat 的部署流程（B1 規格確認卡可填 pods/cpu/memory → B2 資源+審查卡
系統自動算出總量+成本+三方審查 → 確認才真的部署）已經是他想要的功能，不用重做，
但指出 B2 資源卡只顯示「這次要部署多少」，沒有顯示「叢集現在已經被用掉多少、
部署後還剩多少」——只有真的超量時才會靠上一筆記錄的容量檢查跳出阻擋訊息，平時
使用者看不到即時的空間狀況。

`_resource_summary()` 新增查詢 `k8s_get_node_capacity()` + `_sum_cluster_resource_requests()`
（沿用上一筆記錄抽出來的共用函式），算出節點總容量、叢集現有用量、套用這次部署後
的剩餘空間，回傳到新的 `cluster_capacity`/`cluster_used`/`cluster_remaining_after`
欄位；查不到（K8s 未連線）就整組回 `None`，前端對應顯示「叢集容量目前無法取得」
而不是留空白或顯示 0（避免看起來像「還有很多空間」的誤導）。前端 `resourceTableHTML()`
在既有資源表下方新增「叢集空間 / Cluster headroom」對照表，剩餘空間變負數時文字
變紅色提醒。

驗證：真實建立測試帳號，`/api/deploy/parse` 回應確認數字對得上（節點 20 核/15.39Gi，
叢集現有用量 0.5 核/0.51Gi，這次要求 0.2 核/0.25Gi，部署後剩餘 19.3 核/14.63Gi，
20-0.5-0.2=19.3 算式正確）；138 個測試全過；測試帳號與 namespace 已清除。

## 2026-09-29

### 拉同學進度時順手抓到的真實 bug：修復：K8s 斷線時 `--dry-run=client` 誤判成「manifest 有問題」而擋下所有部署
使用者請求拉同學（`ericsung0428`）在獨立分支 `feat/chat-unified-assistant-local`
上的最新進度，用 `git worktree` 開一個隔離目錄檢視（不動到目前的工作分支），跑
同學那邊的測試套件時發現一個失敗案例，追查根因後發現這是**兩邊分支共用、都沒
改過的既有程式碼**（`guardian/dry_run.py`）裡的真實 bug，不是同學的問題，回頭確認
自己這邊也中招。

`guardian/dry_run.py` 的 docstring 宣稱 `mode="client"` 是「只做語法檢查，不需要
連線叢集」，但實測發現新版 kubectl 的 `--dry-run=client` 仍會嘗試連到叢集下載
OpenAPI schema 做更完整驗證，這台機器 Docker Desktop 沒開時這個請求失敗
（`failed to download openapi ... dial tcp ... connectex`），導致「client 模式」
名不符實地變成需要連線——這種情況下系統只是「不知道 manifest 對不對」，卻被當成
「manifest 確定有錯」直接擋下部署（`k8s_deploy` 完全被跳過），錯誤訊息還是一整段
對新手毫無意義的原始網路堆疊錯誤，不是任何有意義的 YAML 驗證結果。

**已有的容錯設計其實預留了修法，只是從沒被走到**：`_kubectl_dry_run` 對「kubectl
未安裝」的情況本來就設計成回傳 `ok=None`（程式碼註解寫死「無法判斷」），
`web_demo.py` 的 `_prepare_deploy` 也早就針對 `ok is None` 設計成「只加警告、
不擋部署」——但「kubectl 有裝、卻連不上叢集」這個一樣屬於「無法判斷」的情況，
之前被誤歸類進 `ok=False`（判定失敗），這條 `ok=None` 的正確路徑等於死代碼直到
這次才被踩中。修法：偵測 stderr 裡的連線失敗特徵字串（`failed to download openapi`、
`dial tcp`、`connectex`、`connection refused` 等），符合就回傳 `ok=None` +
友善的雙語警告訊息（講清楚「這不代表 YAML 有問題」+ 具體該怎麼辦），不影響
「kubectl 真的驗證出 YAML 本身有錯」這種正常擋下情境。

驗證：新增 `tests/test_dry_run.py`（4 案例：連線失敗回 `ok=None`＋不擋部署、真正
的 YAML/schema 錯誤仍正確回 `ok=False`＋擋下、成功案例行為不變、kubectl 未安裝
的既有行為不受影響）；真實用 `python -c` 直接呼叫 `_prepare_deploy()` 重現「Docker
Desktop 沒開時，正常的部署請求被誤擋」的情境，修好後確認 `error: None`（不再被
擋）；全部 142 個測試（138 舊 + 4 新）全過。這個修法只套用在這個分支
（`feat/chat-unified-assistant`），同學那邊的分支要不要套用一樣的修法、以及
兩邊分支怎麼整合，是使用者要決定的更大範圍問題，這次沒有動同學的程式碼。

### 移植：Google 登入（Sign in with Google），從隊友分支移植過來
使用者明確要求把隊友（`ericsung0428`）分支上已經做好的 Google 登入功能直接移植
到這個分支（跟其他重疊功能不同，使用者這次是要「用隊友的版本」不是「重做」）。

`web_demo.py` 新增 `_google_oauth_configured()`/`_derive_username_from_google()`/
`/auth/google/login`/`/auth/google/callback`（CSRF state 核對、email 驗證檢查、
用 `google_sub` 而非 email 查找既有帳號、撞名一律加後綴絕不覆蓋/合併），`login()`
補上「這個帳號是純 Google 帳號、沒有密碼」的明確訊息。用專案已有的 `requests`
套件手動打三個 OAuth2 端點，沒有加新依賴。登入/註冊頁新增 Google 按鈕（`.env`
沒設定 `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET` 時按鈕整個不出現）。`.env.example`
補上申請步驟說明。

驗證：移植 `tests/test_google_auth.py`（18 案例），對這個分支的 `web_demo.py`
直接跑過全部確認通過，不是假設能用；`pytest`（160 個，142 舊 + 18 新）全過。
詳見 `docs/security_review.md` 15 節。需要使用者自己申請 Google OAuth 憑證才能
實測真實登入流程。

### 移植：Dashboard（監控台）——叢集空間總覽頁面，從隊友分支移植過來
使用者要求把隊友分支上已經做好的 Dashboard 移植過來。跟這個分支既有的「B2 資源卡
叢集空間對照表」（部署當下才看得到）不同，Dashboard 是獨立頁面，任何時候都能看，
兩者互補共存、沒有互相取代。

核心概念沿用 Kubecost 的 `allocation = max(usage, requests)`：叢集「已用量」在
K8s requests 加總（既有的 `_sum_cluster_resource_requests()`）與 Prometheus 實際
使用量之間取較大值，比單看 requests 更貼近真實情況。`observability/prometheus_client.py`
新增 `cluster_cpu_usage_cores()`/`cluster_memory_usage_bytes()`；`web_demo.py` 新增
`_my_deployment_resource_breakdown()`（使用者自己的部署資源明細，不擴充既有
`k8s_get_deployments()` 避免影響 Pods/Deployments 頁面）、`_dashboard_summary()`
（整合叢集容量/已用量/我的明細/費用估算，任何一段查不到都優雅降級）、新路由
`GET /api/dashboard`；側邊欄新增 Dashboard 導覽項、新增 `#page-dashboard`（容量/
已用量/剩餘空間三張卡、CPU/記憶體使用率長條圖、我的部署明細表、費用估算卡）。

**跟隊友原版的差異**：隊友的版本接了動態語言切換系統（`I18N`/`applyLang()`），
這個分支的 Metrics/Pods/Deployments 等頁面本來就是走「中英文字直接寫在一起顯示」
的靜態雙語慣例，沒有接動態切換——移植時照這個分支既有慣例改寫前端文字，沒有
把隊友那邊的動態 i18n 機制也搬過來，避免引入這個分支原本沒有的新機制、擴大改動
範圍。後端邏輯（`_dashboard_summary`/`_my_deployment_resource_breakdown`/
`cluster_cpu_usage_cores`/`cluster_memory_usage_bytes`）完全比照原版移植，沒有改動。

**過程中確認隊友原本記錄的一個真實 bug 修法沿用正確**：`_my_deployment_resource_breakdown()`
重用既有的 `_estimate_monthly_cost()` 估價公式時，該函式對「沒填資源」的容器會用
`or 100`/`or 128Mi` 頂上預設值，但監控台是加總後的總量，真的是 0（例如剛註冊、
還沒部署任何東西）時，0 在 Python 是 falsy，會被誤判成「沒填」而冒出一個不存在的
費用；移植過來的版本維持隊友已經修好的寫法（總量為 0 直接回傳 `0.0`，不呼叫
`_estimate_monthly_cost()`）。

驗證：移植 `tests/test_dashboard.py`（10 案例，涵蓋 Prometheus 用量高於/低於
requests、連不上、K8s 未連線四種情境），對這個分支直接跑過全部確認通過；
`pytest`（170 個，160 舊 + 10 新）全過。真實環境驗證：Docker Desktop 重新啟動、
K8s 跟 Prometheus 都連上後，建立測試帳號實測 `/api/dashboard`，確認回傳真實數字
（節點 20 核/15.39Gi，已用量 0.5 核 requests/1.6Gi Prometheus 實際用量——`mem`
來源正確標成 `"usage"`，證實 Kubecost 式取較大值邏輯真的在跑，不是死代碼）；
側邊欄 Dashboard 導覽項跟頁面元素確認存在；測試帳號與 namespace 已清除。

## 2026-10-03

### 修復：app_name 數字開頭時，部署到最後一步才失敗，且錯誤訊息是原始 K8s API JSON
使用者實際部署時用了純數字開頭的 app_name（「123」），畫面顯示「GitOps 已提交，但
K8s 實際部署失敗」，附一整段原始 K8s API 422 錯誤 JSON。追查發現：Deployment 名稱
本身符合 K8s 規則（DNS-1123，允許數字開頭），但這個系統一定會另外建一個
`<app_name>-svc` 的 Service，Service 名稱用更嚴格的 DNS-1035 規則（一定要英文字母
開頭）。這個格式檢查之前完全沒做，一路通過 orchestrator/guardian/dry-run 三層審核、
GitOps manifest 都寫進 git 了，最後真的呼叫 K8s API 才報錯。

兩處補上格式驗證：`web_demo.py` 的 `_prepare_deploy()`（最早期擋下，連 GitOps 都不會
碰到）+ 前端 `flowToReview()` 的 client-side 即時檢查（送出前就擋）+ `guardian/
yaml_validator.py` 的 `_check_naming()`（防護網，防止其他路徑繞過前面兩層）。三處
用同一條規則：`^[a-z]([-a-z0-9]*[a-z0-9])?$`，長度上限 59（含 `-svc` 後綴後仍在
K8s 63 字元上限內）。錯誤訊息中英雙語，講清楚原因（Service 命名規則更嚴格）跟修法
（範例「123」→「app-123」）。

順手修了使用者要求的另一件事：App name 輸入框下方新增格式提示（「必須小寫英文
字母開頭...例如 app-123」），讓使用者送出前就看得到規則跟範例，不用等失敗才知道。

也清掉了使用者實測時留下的殘留物：Deployment「123」在 `user-demo` namespace
裡建立成功但沒有對應 Service（部分失敗的殘留狀態），已確認刪除；對應的 GitOps git
commit 依專案慣例保留當稽核軌跡，沒有刪除歷史。

驗證：新增 `tests/test_yaml_validator.py` 3 案例（數字開頭擋下、結尾連字號擋下、
字母開頭含數字正常通過不誤擋）；`pytest`（173 個，170 舊 + 3 新）全過；真實重現
原本的失敗情境（app_name="123"）確認現在會在第一步就被擋下並給出正確訊息，對照組
（app_name="app-123"）確認正常放行不受影響；前端格式提示文字確認正確顯示在兩個
部署表單裡。

### 新功能：Dashboard 新增 CPU/記憶體用量趨勢折線圖
使用者實際打開 Prometheus 自己附的 Graph 查詢介面看到折線圖，問「這個怎麼用」，
確認用法後要求「這種折線圖也要出現在我自己的系統內」。用
`observability/prometheus_client.py` 早就寫好、但從沒被呼叫過的 `query_range()`，
撈使用者自己 namespace 過去 30 分鐘的 CPU/記憶體實際用量，畫成跟 Healer 頁同一套
inline SVG 折線圖風格（抽出通用版 `renderTrendChart()`，不綁死在重啟次數情境）。

`_dashboard_summary()` 新增 `cpu_trend`/`mem_trend` 欄位（各自獨立的 try 區塊，
查不到就是空陣列，不影響原本容量總覽那段邏輯、不會讓整頁掛掉）；Dashboard 頁面
新增兩張趨勢圖卡片，位置在使用率長條圖下方。

驗證：新增 `tests/test_dashboard.py` 的 `TestDashboardTrend`（3 案例：有資料時正確
轉換時間戳跟單位換算、Prometheus 連不上時優雅降級成空陣列、K8s 未連線時同樣優雅
降級）；`pytest`（176 個，173 舊 + 3 新）全過；真實環境測試：用有實際部署服務的
`user-demo` namespace 直接呼叫 `_dashboard_summary()`，確認撈到 31 個真實取樣點
（過去 30 分鐘、每分鐘一筆），CPU/記憶體數字都合理（idle nginx：CPU 趨近 0、
記憶體穩定在 60 MiB 上下）；前端頁面元素跟繪圖函式確認存在；測試帳號已清除。

### 改動：Dashboard 趨勢折線圖加上自動刷新
使用者回報「這個折線圖也要會動」——原本切到 Dashboard 頁那一刻抓一次資料畫完就
不再更新，留在頁面上看不到新資料點進來。新增每 30 秒自動重撈一次（只在 Dashboard
真的是目前作用中的分頁時才打 API，跟 `pollStatus()` 的輕量全域 interval 同一個
掛載點），對齊 Prometheus 本身約 60 秒一個取樣點的頻率，半分鐘內會看到折線圖往右
多一個點。驗證：`pytest`（176 個）全過（純前端改動）；真實環境確認自動刷新邏輯
正確渲染進頁面。

### 改動：Dashboard 用量快不足時數字變色警示
使用者要求「用量快要不足的時候會變成紅色」。新增共用函式 `_dashWarnColor(pct)`，
門檻跟使用率長條圖原本就有的顏色邏輯統一（>=80% 橘色警告、>=100% 紅色已超出）——
之前只有長條圖本身會變色，旁邊的百分比文字跟「已用量」「剩餘空間」兩張卡的數字
完全沒反應，長條圖橘了但數字還是預設黑色，不一致。現在百分比文字顏色跟長條圖同步；
「已用量」「剩餘空間」取 CPU/Memory 兩者裡比較吃緊的百分比決定要不要變色（只要
一項資源快不夠就整體警示，不用等兩項都超標）。

驗證：`pytest`（176 個）全過（純前端改動）；在 Node 裡獨立驗證 `_dashWarnColor()`
門檻（50%→無色、80%/95%→橘色、100%/150%→紅色）全部正確；真實環境確認新函式與
呼叫邏輯正確渲染進頁面。

### 修復：Chat 歡迎畫面的四張引導卡片點了完全沒反應
使用者實際點擊發現「Deploy a service」「Check cluster status」等引導卡片按下去
沒有任何反應。追查發現是 HTML 屬性引號衝突：`onclick="..."` 用雙引號包起來，裡面
卻塞進 `JSON.stringify(c.send)` 產生的雙引號字串（例如 `"deploy 3 nginx:latest..."`）
——瀏覽器解析 HTML 屬性時遇到第一個雙引號就會提前結束屬性值，onclick 實際上被
截斷成語法不完整的 JS，點擊時什麼都不會發生，畫面上也不會有任何錯誤提示（典型的
「看起來正常、實際上整個互動都死掉」的靜默失敗）。

改成外層屬性用單引號、裡面的 JS 字串一律用雙引號，跟程式碼裡 `showPodDetail` 那處
既有、已經驗證過安全的寫法一致，不是發明新寫法。

驗證：在 Node 裡模擬 4 張卡片（中英文共用同一份 `send` 文字）組出來的完整 onclick
屬性字串，確認語法合法、沒有引號衝突，也確認 4 段卡片文字都不含單引號字元（不會
反過來破壞新的單引號屬性）；`pytest`（176 個）全過（純前端改動）；真實環境確認
修好後的屬性字串正確送到瀏覽器。

### 文件：補寫 docs/security_review.md 第 18 節（Chat 引導卡片 onclick 失效）
上面那條 onclick 修復屬於評審會追問的等級（新手第一次使用就卡住的靜默失敗、現有
176 個 pytest 測試完全涵蓋不到的純前端 DOM 事件錯誤），依 AGENT_RULES.md 慣例補寫
進 `docs/security_review.md` 第 18 節（風險／為什麼重要／解決方式／驗證四段式），
並在「尚待排查」清單補一條「沒有自動化 UI／E2E 測試」的已知缺口（指向 18 節案例）。

### 工具：安裝 LibreOffice（D 槽）+ Poppler（D:\tools），補齊簡報視覺排版 QA 能力
這台機器先前完全沒有 LibreOffice／poppler，簡報只能靠結構驗證（`validate.py`）跟
文字內容比對（`markitdown`），沒辦法真的看畫面有沒有重疊/跑版。LibreOffice 的 MSI
安裝程式需要寫入 `HKEY_LOCAL_MACHINE`，一定要系統管理員權限（這個自動化環境沒有，
也沒辦法幫使用者點 UAC），改由使用者本人手動雙擊安裝程式完成（裝到
`C:\Program Files\LibreOffice`，自訂路徑欄位當下沒有生效，非 D 槽，但不影響使用）；
Poppler（`pdftoppm`）是免安裝的 zip，直接解壓縮到 `D:\tools\poppler-24.08.0`。

### 修復：簡報容量檢查頁圖表文字重疊 + 說明文字數字跟圖表對不起來
裝好 LibreOffice 後第一次對整份簡報做真正的截圖視覺 QA（之前 15 張投影片從沒被
「看過」，只做過結構驗證跟座標數學推算），抓到 `部署容量智慧檢查` 頁的堆疊長條圖
——分類軸標籤「節點 CPU 容量」跟一個太窄區塊（0.5/20 核）的置中資料標籤擠在一起
重疊糊成一團；旁邊的說明文字寫「19.3 核剩餘」，但圖表實際畫的是 19.5 核，兩者對
不上（文字多算了一個圖上沒畫出來的「0.2 核本次需求」假設情境）。改成拿掉冗餘的
分類軸、該區塊不顯示資料標籤（避免過窄溢出），說明文字改成跟圖表數字完全一致
（20 核容量－0.5 核現有用量＝19.5 核剩餘），不再引用圖表沒有呈現的數字。

### 新增：簡報補兩張量化比較頁（部署步驟數長條圖、功能覆蓋度雷達圖）
使用者要求把系統跟「一般用一個模型做部署」做比較，並畫圖表放進簡報。在既有的
「我們的平台 vs. 一般 AI 輔助部署」文字比較表後面，新增兩張圖表頁：1) 長條圖比較
「使用者需親自操作的步驟數」（一般 AI 輔助部署 6 步 vs. 我們的平台 1 步，右側列出
每一步具體內容）；2) 雷達圖比較 6 個維度的相對評分（部署前安全審核、容量/資源風險
攔截、版本控管與回滾、自我修復、多租戶隔離、新手友善度，依系統實際功能盤點給分，
不是效能測試數字）。兩張都在標題或內文明確標註「估算依據」/「非正式效能測試」，
避免被誤認成真的碼表測速或 benchmark 數據。

### 修復：簡報「170 個測試案例」是舊數字，實際已經是 176 個
簡報裡測試數量（首頁晶片、測試頁大數字跟長條圖分布、結尾頁晶片，共 4 處）還停在
170，是先前某次快照的數字；這次 session 中途又新增了 dashboard 趨勢圖測試、
app_name 命名驗證測試，實際重跑 `pytest` 確認現在是 176 個全過。重新依現有測試檔案
分類統計（代理決策邏輯 37／Healer 自癒 31／安全與審核 40／Web 路由與帳號 55／
監控 Dashboard 13，合計 176），4 處全部更新成正確數字，並重新驗證渲染正確。

### 修復：GitOps Log 的 Rollback 按鈕從沒成功回滾過（五個獨立 bug 疊加）
使用者點 Rollback 得到「no previous image annotation」這種看起來像正常限制的
錯誤，實際上是五個 bug 疊加：①`gitops/rollback.py` 的 `rollback(strategy="auto")`
判斷式寫成 `if result["ok"] is not False:`，把 `ok=None`（Argo CD 不可用，這專案
從沒真的部署過 Argo CD）跟 `ok=True`（真的成功）當同一回事，導致 auto 迴圈每次
都在第一輪就提早停下，後面真正會成功的 `git`／`kubectl` 策略永遠輪不到；
②`rollback()`／`_print_result()` 用 emoji（⏪✅⚠️❌）當 `print()` 前綴，這台
Windows 機器主控台預設 `cp950` 編碼印不出 emoji，直接丟 `UnicodeEncodeError`，
例外被外層 `try/except` 吃掉才掉進幾乎必然失敗的備援分支；③`_rollback_git()`
原本只把 manifest 檔案還原、建 revert commit，從沒呼叫 `kubectl apply` 把還原
後的設定真的套到叢集，就算「回滾成功」正在跑的 Pod 也不會變。

①②③修好、第一輪拿 `web-frontend123`（兩次部署內容剛好相同）驗證通過後，
原本以為修完了，直到拿一個「兩次部署內容真的不同」的 app 重新驗證才又抓到兩個：
④`rel = str(manifest_dir.relative_to(...))` 在 Windows 上是反斜線路徑，但
GitPython tree 物件的 `item.path` 永遠用 `/` 分隔，`startswith()` 比對在
Windows 上永遠對不上，「把上一版內容寫回磁碟」這個迴圈實測一次都沒跑過；
⑤修好④讓迴圈真的跑起來後，發現 `tree.traverse()` 連目錄節點本身都會列出來，
`rel` 剛好完全符合 `startswith()`，目錄被誤判成檔案寫入，撞上 `PermissionError`。
五處都修好：提前返回條件改成 `is True`、emoji 換成 ASCII 安全的
`[OK]`/`[WARN]`/`[FAIL]` 文字標籤、`_rollback_git()` 補上 `kubectl apply` 並在
套用失敗時誠實回報 `ok=False`、路徑比對前統一轉成 `/` 分隔、比對條件加上
`item.type == "blob"` 排除目錄節點。詳見 `docs/security_review.md` 第 19 節
（含「第一輪驗證剛好挑到會巧合通過的案例」這個教訓的完整記錄）。

驗證：新增 `tests/test_rollback.py` 6 案例（4 個鎖死 auto 迴圈的策略選擇邏輯與
`_print_result()` 輸出不含非 ASCII 字元，2 個用真的 GitPython repo 直接測④⑤：
確認回滾後磁碟上的檔案內容真的換成上一版、diff 不是空的）；`pytest`
（182 個，176 舊 + 6 新）全過；真實環境端到端驗證分兩輪：第一輪用
`web-frontend123` 驗證①②③（巧合通過，沒測到④⑤）；第二輪用全新測試帳號透過
**真正的 HTTP API** 連續部署三個內容不同的版本（`nginx:1.25→1.27→1.28`，
再追加 `1.29`），每次呼叫真正的 `POST /api/rollback`，確認 `kubectl get
deployment` 查到的 image 真的從 `1.28`／`1.29` 變回 `1.27`（不是巧合維持原狀），
revert commit 的 diff 也是真的兩行變更，不是空 commit；修復後重啟 `web_demo.py`
讓常駐程序載入修好的模組；測試帳號與 namespace 已清除。

### 修復：純中文、沒給名字的資源需求句子被誤判成有指定 app_name，記憶體/CPU 數值整段消失
使用者錄 demo 時實測「幫我部屬一個30核CPU，20GI記憶體的服務」，結果被 app_name
格式驗證擋下，錯誤訊息顯示 app_name 是從沒打過的「20gi」。追查發現
`llama_client.py` 的 `_deterministic_deploy_parse()` 大量用到 Python 預設
Unicode-aware 的 `\w`／`\b`：① app_name 候選正則 `[\w-]` 把中文字也當成「單字
字元」，「...記憶體的服務」整段被誤判成「某個東西+服務」的命名樣式，抓出
「20gi記憶體的」當候選，正規化後變成「20gi」；② 記憶體/CPU 數值正則結尾的
`\b`，在「數字+單位」後面緊接著中文字、中間沒有空格時，因為中文字跟英數字母
一樣算 `\w`，兩個 `\w` 字元之間沒有邊界，`\b` 永遠對不上，整個配對失敗，
30 核／20Gi 這兩個數值完全被默默丟掉，使用者講的容量需求根本沒被系統看到。
修好這兩點後又在即時驗證時發現第三個：使用者打的「20GI」（全大寫）被原樣保留，
後面 `web_demo.py` 的格式驗證對大小寫敏感（一定要 "Gi"），又把它擋下——改成
抓到數值後統一正規化成 K8s 要求的大小寫，不管使用者打 GI／gi／Gi 都一視同仁。

三處都修好：app_name 候選正則改成只吃 ASCII（`[A-Za-z0-9-]`，不吃中文字）＋
新增「候選字串本身是純數字/資源單位（如 20gi、500m）就跳過」的防呆；記憶體/CPU
正則結尾的 `\b` 改成「後面不能緊接 ASCII 字母」的否定預查（中文字接在後面完全
不受影響）；記憶體單位抓到後統一正規化大小寫。詳見
`docs/security_review.md` 第 20 節。

驗證：新增 `tests/test_deterministic_parse.py` 6 案例（純中文資源句子
app_name 正確落回 auto-app、記憶體/CPU 數值正確抓到、純 ASCII 的「數字+單位」
候選也不會被誤判成名字、中文單位緊接英文關鍵字不受新防呆誤傷、既有明確指定
名稱的句子行為不變、記憶體單位大小寫正規化）；`pytest`（188 個，182 舊 + 6
新）全過；真實環境端到端驗證：用使用者原句透過真正的
`POST /api/deploy/parse`，確認 HTTP 200、app_name 正確為 `auto-app`、
`cpu: "30"`、`memory: "20Gi"`（不再是 "20GI"）、容量超出正確觸發
`decision: "block"`，整段流程走完不再卡在格式驗證；修復後重啟 `web_demo.py`。

### 修復：Chat 打「幫我部署 2 個 nginx」被誤判成查詢一個叫「2」的 deployment
使用者錄 demo 時實測在 Chat 打「幫我部署 2 個 nginx，開 80 port」，結果回「找不到
deployment '2'」，不是走部署流程。追查發現 `_INTENT_RULES`（前端 `matchClientRule`
與後端 `_rule_intent` 都有一份鏡像）裡 `describe_deployment` 規則結尾的
「狀態/細節/詳情/status」關鍵字整段可省略（`(?:...)?`），導致任何「部署 <數字>」
開頭的句子——哪怕完全沒有查詢狀態的意圖——都會被這條規則搶先配對成功，把數字
「2」誤判成使用者要查的 deployment 名稱，排在清單後面真正的 `deploy` 規則前面
被攔截掉。前端 JS 跟後端 Python 兩份鏡像都把結尾的 `?` 拿掉，改成必須真的出現
狀態關鍵字才會判定成查詢意圖；拿掉之後「幫我部署 2 個 nginx」正確落到下面的
`deploy` 規則。

驗證：新增 `tests/test_web_demo_routes.py` 的 `TestRuleIntentDeployVsDescribeDeployment`
4 案例（部署句子不再被誤判成 describe_deployment、英文部署句子同樣不受影響、
真的在查狀態的句子──中英文關鍵字各一──行為不變）；`pytest`（192 個，188 舊 + 4
新）全過；真實環境端到端驗證：用使用者原句打真正的 `POST /api/intent`，確認
`action: "deploy"`（不再是 `describe_deployment`）；修復後重啟 `web_demo.py`
讓前端新版 HTML（含修好的 JS 規則）跟後端新版規則同時生效。

### 修復：app_name 候選正則又連續抓到三種誤判（同一輪 demo 連續實測發現）
使用者在同一次 demo 連續打出三句話，各自踩到 `_deterministic_deploy_parse()`
app_name 候選正則的不同誤判：
①「我要部屬2個nginx服務」——pattern 2（「候選+service/app/deployment/服務」）
把緊接在「服務」前面的 image 名稱「nginx」當成使用者另外取的名字；
②「deploy app named 123abc」——pattern 1 把「app」這個泛稱名詞也當成前綴觸發
字，後面緊接的下一個詞剛好是真正的連接詞「named」，導致「named」自己被當成
候選名稱；③（追查時另外發現）`ask_llama()` 在 Gemini 翻譯層回傳的
"### DeploySpec" 結構化區塊驗證不完整時，會把區塊原文直接丟給這支函式當
fallback，因為 `\s` 包含換行字元，pattern 2 跨行把標題「DeploySpec」跟下一行
「app_name:」的「app」接在一起誤判成候選，抓出使用者從沒打過的「deployspec」。

三處都修好：①新增「候選字串等於剛剛抓到的 image 基底名稱就跳過」的防呆；
②pattern 1 拿掉「app」/「service」這兩個泛稱名詞，只留真正明確的連接詞
（for/named/name/叫做/名稱/給），順便修好一個既有已知限制（「a service named
my-app」現在也正確抓到 my-app，不再被「service」搶先攔截）；③兩個 pattern
的候選字跟關鍵字之間的空白都限定成「同一行內」（`[ \t]` 取代 `\s`），從根本
排除結構化區塊被跨行誤判成白話文的整類問題。

驗證：新增 3 個測試（image 名稱不會被誤判成另外取的名字、named 連接詞不再被
app/service 吃掉、連帶修好的 service-named-X 既有限制、結構化 DeploySpec 區塊
不再誤判成 app_name "deployspec"）；`pytest`（196 個，192 舊 + 4 新）全過；
真實環境端到端驗證：用使用者這輪實測的兩句原話打真正的
`POST /api/deploy/parse`，確認 `app_name` 分別正確為 `auto-app`、`123abc`；
修復後重啟 `web_demo.py`。

### 修復：Healer 一次改了四個問題（使用者實測＋明確要求）
使用者錄 demo 時看到 Healer 頁面跳出「自動監控似乎卡住了」的紅色警報，另外
指出 Pod 詳情彈窗的時間軸時間不對、Pod 重建後歷史就斷掉看不出「曾經壞過又修好」
的過程、而且要求修復進行中要有明確的進度提示，不然看起來像畫大餅：

1. **時間顯示錯誤（晚 8 小時）**：這個檔案到處直接呼叫
   `datetime.utcnow().isoformat()`，產生的字串沒有時區標記。前端 JS 的
   `new Date(...)` 看到沒有時區標記的字串會當成「當地時間」解析，不是 UTC，
   這台機器在 UTC+8，Healer 時間軸因此顯示得比實際晚 8 小時。新增
   `_utc_now_iso()`／`_parse_utc_iso()` 這對 helper（結尾統一加 "Z"，Python
   3.9 的 `fromisoformat()` 不支援 "Z" 所以讀回來要先去掉），取代所有舊的
   直接呼叫。
2. **「自動監控似乎卡住了」假警報**：原本只看「多久沒更新 last_scan」
   （> interval×3 = 90 秒）判定卡住，但根因診斷固定要打一次本地 LLM，實測
   一次 2-3 分鐘跑不掉，Healer 認真修東西的時候 `last_scan` 本來就該是舊的，
   這個檢查反而在最不該報假警報的時刻跳出來。新增 `_healer_bg_state
   ["processing"]`（開始處理一個 issue 就登記，不是等整批處理完才更新），
   liveness 判斷優先看這個欄位：只要有正在處理、而且開始時間還在合理範圍內
   （interval×20=10 分鐘，涵蓋多個 issue 排隊），就不算卡住。
3. **Pod 重建後歷史斷掉，看不出「曾經壞過又修好」**：`_pod_history` 原本用
   Pod 名稱本身當 key，但 Healer 真正的修復手段常常是讓 K8s 重建一個新 Pod
   （改 memory limit、rollout undo 都會觸發新 ReplicaSet），新 Pod 一定有
   新名稱，歷史每次都從空的重新開始，使用者點進去只看得到一小段平直綠線。
   改成用推斷出來的 Deployment／app 名稱（新增 `_app_key_for_pod_name()`，
   拿掉 Pod 名稱最後兩段隨機後綴）當 key，同一個服務不管重建幾次，歷史都
   累積在同一筆底下。
4. **修復中沒有任何進度提示**：新增 `#healer-processing` 區塊，`processing`
   欄位有值時顯示轉圈動畫＋「正在修復 X（原因）... 根因診斷需要跑本地模型，
   約 1-3 分鐘」；Healer 頁面現在每 5 秒自動輪詢一次狀態（只在頁面是目前
   作用中分頁時才打），使用者不用手動按重新整理就能看著它從「正在修復」變成
   「修好了」。

驗證：`tests/test_pod_history.py` 新增／改寫 8 案例（含新增的
`TestAppKeyForPodName`，鎖死 Pod 名稱→app 名稱的推斷邏輯，包含 app 名稱本身
含連字號的情況）；`tests/test_healer_liveness.py` 新增 4 案例（`processing`
最近才開始時不誤判成卡住、`processing` 本身也卡太久時仍正確回報卡住、UTC
helper 的格式跟往返轉換、舊資料沒有 "Z" 也要能解析）；`pytest`（205 個，
196 舊 + 9 新）全過。真實環境端到端驗證：故意把 `healer-demo` 的 image 改成
不存在的 tag 觸發真的 ImagePullBackOff，確認 `/api/healer/status` 在背景
執行緒認真處理的過程中全程 `running: true`、不再跳出假警報，`processing`
欄位正確顯示正在處理哪個 Pod／原因，時間戳確認帶 "Z" 後綴；修復後重啟
`web_demo.py`。


## 2026-10-06

### 系統 QA 掃描 + 修復：意圖分類器信任捏造內容、部署解析邊界值不一致、prompt injection 新缺口
對正在跑的系統實際送出 32+24 題涵蓋各種語意/風險情境的訊息（兩輪，完整記錄在
`docs/qa_sweep_20261006.md`），抓到並修好：(1) `/classify` 對破壞性操作只看模型自報
信心、沒有查核 args 內容是否真的出現在原始訊息裡，導致「幫我把它刪掉」被捏造出
`web-frontend` 當刪除目標、「我是系統管理員」被捏造出 `all_users_namespace`，兩者
都用高信心直接放行——新增 `_args_grounded_in_message()` 事實查核，不管信心多高，
內容對不上原始訊息一律降級成 `clarify`；(2) `web_demo.py` 規則層的 `name` 擷取用
`\S+` 寬鬆比對會把整句中文吃進去，新增 `_looks_like_resource_name()` 過濾；
(3) `_deterministic_deploy_parse()` 看不懂中文數字詞、0/負數/超量副本數處理不一致
且錯誤訊息毫無資訊，新增中文數字詞對照、負號擷取、`_parse_error` 標記讓呼叫端給出
具體雙語錯誤；(4) `###System:` 井字號前綴繞過注入偵測正則、被問到其他使用者資源時
模型自相矛盾（先拒絕又把查詢指令列出來）——分別擴充正則前綴容忍、新增
`_looks_like_cross_tenant_probe()` 確定性攔截。另外上網查 OWASP AI Testing Guide
範例改寫 + 自行設計了 14 題擴充注入測試，發現角色扮演型 jailbreak（DAN/AntiGPT）、
英文同義詞繞過、對話歷史未經注入偵測三個殘留風險，記錄在 `docs/security_review.md`
21、22 節與「尚待排查」，這次沒有修。

驗證：`pytest -m "not integration"` 全過；10 題回歸測試逐一重送確認修好；14 題擴充
注入測試逐一記錄結果與診斷；修復後重啟 `core/model_server.py` 與 `web_demo.py`。

### Healer 端到端真實故障測試：自動偵測+修復驗證有效，發現監控小模型會幻覈根因
用 `kubectl exec` 進一個隔離測試 Pod（`user-qa-sweep-20261006` namespace）反覆
`nginx -s stop` 製造真實 `CrashLoopBackOff`，不碰任何 UI/Chat 按鈕，純粹觀察
`web_demo.py` 常駐的背景自癒迴圈（每 30 秒全叢集掃描）。結果：78 秒內自動偵測、
正確判斷 `action=analyze_logs`、刪除壞 Pod 讓 Deployment controller 重建，修復後
確認服務恢復正常——自動偵測跟修復機制本身驗證有效。但發現監控小模型（1.5B，
`/diagnose`）在日誌/事件都沒有具體訊號時，仍幻覈出「容器資源不足導致 OOM
Killed」這個完全沒有證據支持的根因（真實事件只有通用的 Back-off 重啟訊息），
`DIAGNOSE_SYSTEM` 裡「證據不足要老實說」的規則對 1.5B 這個尺寸不夠可靠。因為
規則層已經蓋過模型的 action/severity，這次沒有導致錯誤的修復動作，只有根因說明
文字是錯的。完整時間軸、修法建議見 `docs/healer_exec_test_20261006.md`、
`docs/security_review.md` 23 節。這次只排查記錄，沒有動手修，留給下次。

## 2026-10-07

### 新增真實管理員身分：只歸屬於驗證過的 Google 帳號 email，密碼帳號/聊天自稱一律不算
系統原本沒有管理員概念，`users.json` 裡剛好有一個密碼帳號字面上叫 `"admin"`容易被誤認。
新增 `core/config.py` 的 `ADMIN_EMAILS`（預設含使用者本人 email，`.env` 可覆寫以後加組員）
與 `web_demo.py` 的 `_is_admin_user()`：只有 `auth_provider == "google"` 且 email 落在
允許清單才算管理員，完全不看 username 字串本身。`session["is_admin"]` 在 `_enforce_ban()`
（每個請求都跑）即時重算，前端 `#user-info` 多顯示「管理員 / Admin」標籤。另外
`core/model_server.py` 新增 `_looks_like_authority_claim()` 確定性攔截，`/chat` 收到
「我是系統管理員」之類的自稱時直接回固定拒絕文案，不進模型判斷。

### 修復 2026-10-06 QA 測試留下的 3 個殘留 prompt injection 風險
`_INJECTION_PATTERNS` 擴充角色扮演型 jailbreak（DAN/AntiGPT/STAN/DUDE 人格扮演模板）
與 `ignore` 的英文同義詞（disregard/forget/bypass/override）；`/chat` 組訊息時對
`history` 裡的每一則內容也跑注入/跨租戶/身分冒充檢查，命中的歷史訊息直接跳過不放進
模型 context（原本只檢查當次 `req.message`）。

### 修復 Healer 監控小模型（1.5B）幻覈根因
`healer/diagnose.py` 新增 `_has_concrete_signal()`：規則層命中已知模式但合併文字裡
沒有具體錯誤關鍵字（OOM/image pull/permission/connrefused/configmap/probe/port 等）時，
不再信任模型自己編出的 `root_cause`，一律蓋成「證據不足，只能確定是重複重啟，實際原因
需要人工查看完整日誌」。重現 2026-10-06 kubectl exec 實測案例（純 CrashLoopBackOff，
模型幻覈出「OOM Killed」）確認修復有效，`action`/`severity` 不受影響（一直由規則層決定）。

驗證：`pytest -m "not integration"` 全過（新增 `tests/test_healer_diagnose_grounding.py`）；
`pytest -m integration tests/test_model_server_injection.py`（新增 DAN/同義詞/
authority-claim 案例）全過；手動確認 `neilhuang-mobile` 顯示管理員標籤，`admin`/`test`
密碼帳號不顯示。詳見 `docs/security_review.md` 23、24 節。

### 修復部署解析器：「作為/當作」連接詞句型會讓 image 被靜默換成 nginx:latest
`research_benchmark.py` 量化評估腳本實測時發現：「請部署 4 個 node:20-alpine 作為
api-gateway」這種「image 作為/當作 app_name」的句型，`_deterministic_deploy_parse()`
的 image/app_name 擷取正則都沒有把「作為/當作/当作/当成」列進連接詞清單，image
擷取失敗後靜默退回預設值 `nginx:latest`（使用者明明打了 node 卻部署出 nginx），
app_name 也沒接到真正的名字、退回用 image 基底名稱頂替。`llama_client.py` 的
`image_patterns`／`app_patterns` 都補上這幾個連接詞。新增 2 個回歸測試
（`tests/test_deterministic_parse.py`）。

### 新增近失事件可見化：系統不信任模型時，讓使用者看得到這件事發生過
用 `/blind-spot-finder` 分析這個系統找到的盲點：`_clean_intent()`／`diagnose_issue()` 都
已經有「不信任模型自報信心」的規則層防護，但防護結果只有最終安全版本會給使用者看，模型
原本想做什麼、為什麼被擋下來完全不可見。`core/model_server.py` 的 `_clean_intent()` 新增
`clarify_reasons`/`ungrounded_values`（具體指名哪個值查無根據）；`healer/diagnose.py` 的
`diagnose_issue()` 蓋掉幻覈根因前，先把模型原本的版本存進新欄位 `llm_raw_guess`。前端
（clarify 卡片、Healer pod 詳情頁、Fix 按鈕、Healer 背景狀態清單、Chat 的 pod_health 查詢）
都加上可摺疊的「為什麼系統沒有直接執行」區塊，所有動態字串過 `escHtml()`（實測 XSS payload
確認有正確跳脫）。

順便用真實請求/真實 Pod 測出 3 個既有 bug 並修復：`/api/pods/<name>?diagnose=1`
（Diagnose 按鈕）原本繞過規則層直接呼叫 `diagnose_with_llm()`；`/api/healer/fix`、
`/api/healer/auto_fix` 的 `jsonify()` 白名單漏掉 `llm_raw_guess`（`/api/healer/fix`
還漏了 `severity`）；`healer/diagnose.py` 的 `CrashLoopBackOff` 規則正則只認字面字串，
K8s 實際事件是 `reason="BackOff"`/`message="Back-off restarting..."`，真實測試時抓到
`reason` 欄位恰好是 `"Completed"` 的瞬間，規則層完全比對失敗、連帶讓幻覈防護失效。

驗證：`pytest -m "not integration"`、`pytest -m integration` 全過（新增/擴充 11 個測試）；
真實端到端測試——註冊新帳號對正在跑的 server 送 2026-10-06 QA 掃描的原始案例、部署真實
bad-image Deployment 驗證 Diagnose 按鈕修好、`kubectl exec` 反覆 `nginx -s stop` 製造
真實 CrashLoopBackOff 並輪詢抓到 `llm_raw_guess` 真的捕捉到模型幻覈的「OOM」原文。詳見
`docs/security_review.md` 25 節。

### 修復 2026-10-07 大規模模糊語意掃描（206 題）找到的 P0/P1/P2 問題
延續 10/6 的 QA 掃描方法，這次擴大到 206 題（部署解析/意圖分類/7 種 prompt
injection 手法/破壞性操作查核/一般問答/極端輸入/Healer 查詢），對正在跑的
`web_demo.py` 送真實請求。找到並修復：

- **P0**：`web_demo.py` 的 `_rule_intent()` 規則層命中就用假 100% 信心直接執行，
  `_looks_like_resource_name()` 只檢查字串格式不檢查語意，常見英文虛詞
  （the/of/everything/container 等）會被當成真實資源名稱（「delete everything
  in this cluster」被解析成要刪除一個叫 "everything" 的資源）。新增
  `_IMPLAUSIBLE_NAME_WORDS` 黑名單，`image` 欄位也補上同等查核（原本只查
  `name`/`pod_name`）。
- **P0**：`llama_client.py` 的 22 節邊界值/多服務保護原本只套用在 Gemini 正規化
  「之後」的文字，Gemini 正規化成功時完全繞過、失敗時檢查的是可能已遺失原始
  訊號的改寫文字。新增 `_predeploy_boundary_precheck()`，在呼叫 Gemini 之前
  先對使用者原始輸入跑一次檢查。
- **P1**：`gitops_log`/`healer_scan` 這類零參數規則會把「GitOps 是什麼」這種
  概念性問題直接當指令執行，新增 `_QUESTION_MARKERS_RE` 判斷。
- **P1**：跨租戶探測過濾器補上「朋友/同事的帳號」句型；角色扮演越獄偵測補上
  「AI called/named X」「let's play a game where you're」這類不綁定特定人格
  名稱的語序。
- **P2**：新增確定性規則攔截「解碼＋照做/執行」這種 Base64/ROT13 注入組合請求
  （原本安全是因為模型解碼解錯，不是設計出來的防護）。

新增/擴充 24 個回歸測試（`tests/test_web_demo_routes.py`、
`tests/test_deterministic_parse.py`、`tests/test_model_server_injection.py`）。
驗證：`pytest`（255 個）全過；真實端到端測試——重啟兩個服務、註冊測試帳號，對
正在跑的 `/api/intent`／`/api/chat` 重送全部 7 個原始失敗案例，確認修復生效。
完整報告見 `docs/qa_sweep_20261007_large.md`，詳見 `docs/security_review.md`
26 節。

## 2026-10-08

### 修復整理 QA 量化報告時發現的 3 個真實問題
把 2026-10-07 大規模掃描的原始資料整理成結構化 pass/fail 判定、畫成圖表給使用者
做研究報告時，重新逐題檢視細節，額外抓到 3 個當天沒找到的問題並修復：
`core/model_server.py` 的 `_find_ungrounded_args()` 原本只查核字串值，數字型參數
（replicas/pods/port）完全沒有事實查核，模型捏造的數量會直接通過，新增
`_number_grounded_in_message()` 補上（支援阿拉伯數字/中文數字詞/英文數字詞三種
表達方式）；`llama_client.py` 的 image 擷取 lookahead 補上「副本/台」（修「部署
九個 redis 副本」被解析成 nginx 的 bug）；`_MULTI_SERVICE_HINT_RE` 補上「X個...
跟/和/與...Y個」這種連接詞結構（修「起一個 nginx 跟一個 redis」被誤判成單一服務
2 個副本的 bug）。

新增/擴充 9 個回歸測試，`pytest`（266 個）全過；真實端到端測試重送 3 個原始案例
確認修復生效。詳見 `docs/security_review.md` 27 節。

### 新增 QA 測試結果的結構化量化報告 + 圖表
把 2026-10-07 兩輪掃描（261 筆真實請求）的原始資料整理成 pass/fail 判定
（`qa_sweep_20261007_classify.py`），並用 matplotlib 產出三張 PNG 圖表
（`qa_sweep_20261007_charts.py`，存於 `reports/qa_sweep_20261007_charts/`）：
各類別通過率、延遲分布、整體摘要數字，供研究報告/口試直接使用。`AGENT_RULES.md`
新增一條規則：以後做系統測試/QA 掃描，測試腳本要留結構化 pass/fail + 延遲欄位，
超過約 20 題要主動產出量化摘要跟圖表，不用使用者每次交代。

### RAG 升級：知識庫擴充 8 篇 + embedding 換 BGE-M3；hybrid search/reranker 實作完但實測後不採用
`rag/k8s_docs/` 新增 8 篇繁中 K8s 知識文件（networking/storage/scheduling/
rbac_deep_dive/autoscaling/multi_container_patterns/rolling_update_strategies/
troubleshooting_advanced，7→15 篇，815→1500+ 行）；`rag/vector_store.py` 的
`EMBED_MODEL_NAME` 從 2021 年的 `paraphrase-multilingual-MiniLM-L12-v2` 換成
`BAAI/bge-m3`（多語言/中文檢索現行黃金標準）。另外照 2026 年業界現況實作了
RRF hybrid search（`rag/retriever.py` 新增 `_reciprocal_rank_fusion()`/
`_fuse_candidates()`）跟 cross-encoder reranker（新檔 `rag/reranker.py`，
`BAAI/bge-reranker-v2-m3`），但用既有的 24 題標註測試集（`rag/eval_retrieval.py`，
過程中順手修正一題過時的標註——新增的 `autoscaling.md` 本來就該被接受為 HPA
問題的正確答案，舊標註只列了 `deployment_patterns.md`）實測後發現這兩項在
這個知識庫規模（227 chunks）上反而讓 Recall@3 從 dense-only 的 95.8% 掉到
91.7%、MRR 從 0.938 掉到 0.833，而且 reranker 在 CPU 上平均每題要跑 3.5~9.8
秒（比純 dense 的 90~440ms 慢一個量級以上）——**決定不採用**，`retrieve()`
維持 dense-only（chroma 可用就用，不行才退回 TF-IDF），融合/reranker 程式碼
保留但預設不呼叫。embedding 升級前後對照：舊基準（舊模型+7篇知識庫）
Recall@3 100%／MRR 0.875／5.3ms；升級後（BGE-M3+15篇知識庫）Recall@3
95.8%／MRR 0.938／~90ms——MRR 進步、延遲變慢是模型變大的預期代價，知識庫
變大後 Recall 看似微降其實是語料庫本身更細分、天花板更難打滿。新增
`tests/test_rag_fusion.py`（10 個單元測試）。`pytest`（276 個）全過。詳見
`docs/security_review.md` 28 節（含完整量化對照表跟「業界標準不等於適用這個
系統」的分析）。

### 03:45 — 安全：密碼/API key 外洩稽核 + gitleaks pre-commit 防線 + repo 改 private
用 gitleaks 8.30.1 掃完整 git 歷史（146 個 commit、約 76 MB）與目前已追蹤但未提交
的修改，兩者都是 `no leaks found`；`users.json` 確認存的是雜湊值不是明碼。稽核時
發現 repo 是 **PUBLIC**（fork 數 0），已由使用者改為 PRIVATE（`gh repo view` 確認）。
新增 `.git/hooks/pre-commit`：每次 commit 前用 `gitleaks git --pre-commit --staged
--redact` 掃這次要提交的內容（含新檔案），有疑似金鑰就拒絕 commit；用
`GIT_CONFIG_*` 環境變數關掉 `core.safecrlf`，避免 Windows LF/CRLF 警告被 gitleaks
誤判成錯誤而擋下所有 commit。驗證：在暫存測試 repo 裡，乾淨 commit 正常通過，
含假 GitHub token 的 commit 被擋下（commit 數維持 1）。注意：`.git/hooks/` 不進版控，
換電腦/重新 clone 要重新安裝。另把 `.playwright-mcp/`（Playwright MCP 截圖/log）
加進 `.gitignore`。詳見 `docs/security_review.md` 29 節。

### 安全：修復回滾路徑穿越 + port 衝突檢查洩漏其他帳號 app 名稱
security-guidance 外掛背景審查回報、逐行追程式碼確認屬實的兩個跨帳號隔離破口。
(1) `/api/rollback` 的 `app_name` 原本沒驗證，`../user-別人/app` 會讓
`gitops/rollback.py` 的路徑指到其他帳號的 manifest 目錄（當下只是剛好被 manifest
寫死的 namespace 擋住）；Argo CD 回滾只用 app 名稱，部署 Argo CD 後就能回滾別人的
服務。修法：路由入口與 `rollback.py` 兩層 DNS-1123 名稱白名單、`_manifest_dir_within()`
解析路徑確認不超出範圍、Argo CD 回滾前比對 Application 的目的地 namespace；順手修掉
`startswith` 前綴比對讓回滾 `app` 連帶覆寫 `app123` 的 bug。(2) `/api/deploy/conflicts`
衝突時原樣回傳別的帳號的 app 名稱，改成只回 `port_conflict_other_account: true`，
前端顯示「已被其他帳號的服務佔用」並建議換 port。驗證：新增 22 個回歸測試，換回舊程式碼
時 21 個失敗、新程式碼全過；pytest 294 個全過。詳見 `docs/security_review.md` 30 節。
端到端補驗（同日）：用新程式碼啟動 Web Demo、臨時帳號打真實 API，穿越名稱全部 400、
port 衝突不含其他帳號名稱；Playwright 確認前端 0 個 console 錯誤。過程中發現回滾不存在
的 app 時會把 K8s 原始例外（含內部 HTTP header）回給前端，改成中英雙語的「找不到這個
部署」訊息（404），其他錯誤只回通用訊息。

## 2026-10-09

### 換電腦交接 + 市場調查報告 + 整份推上 main
新增 `docs/handoff/NEW_MACHINE_HANDOFF.md`（給新電腦 Claude 的交接指示：還原記憶、安裝順序、
`.env` 要手動搬、組員未合併的分支、工作優先順序），並把 auto-memory 快照（16 檔）、
gitleaks pre-commit hook（`scripts/git-hooks/pre-commit`）、3 個手動抽出的 skill 備份進 repo；
`CLAUDE.md` 開頭加交接提示。同時納入 2026-10-08 兩份市場調查（`docs/market_research_20261008.md`
企業報告版、`docs/market_research_voc_20261008.md` 使用者原話版，原始資料在 `docs/research/`）。
依使用者指示，`feat/chat-unified-assistant` 整份快轉合併到 `main`。

### 03:07 — 架構調整：模型載入支援 AMD / NVIDIA 兩台機器，同一套設定
**為什麼**：專案搬到兩台 16GB 顯存的機器（家裡 AMD RX 9060 XT、學校 RTX 5080 Laptop）。
舊版只要偵測到 GPU 就一律 bitsandbytes 4-bit，bitsandbytes 只支援 NVIDIA，在 AMD 上
Model Server 直接載入失敗。
**改了什麼**：
- 新增 `core/hardware.py`：偵測 GPU 廠牌/顯存，`plan_loading()` 依顯存決定精度與裝置
  （>= 10GB 兩顆模型都 bf16 上 GPU；小顯存 NVIDIA 才 4-bit；沒 GPU 跑 CPU）。兩台 auto 結果完全相同。
- `core/model_server.py` 改用這個決策，bitsandbytes 改成只在 4-bit 時才 import；
  `/health` 多回報 `gpu`、`deploy_precision`、`vram_allocated_gb` 等欄位，兩台可直接對照。
- `rag/vector_store.py`：BGE-M3 embedding 顯存夠就上 GPU 並用 fp16（約 1.1GB）。
- `requirements.txt` 拿掉 torch / bitsandbytes，新增 `requirements-amd.txt`（ROCm 7.2.1）、
  `requirements-nvidia.txt`（cu128，Blackwell 必需），torch 兩台都釘 2.9.1；Python 統一 3.12。
- transformers 4.44.2 → 4.45.2：4.44 生成時替整段 prompt 每個位置算完整詞彙表 logits，
  長 prompt 瞬間多吃約 3GB 顯存，4.45 只算最後一個位置。
- 新增 `scripts/download_models.py`（單線下載 + SHA256 驗證，避開家裡網路遇平行連線就卡死的問題）。
- `docs/setup.md` 改寫成兩台機器的安裝流程（含 WSL「未啟用虛擬化」的 bcdedit 修法）；
  `observability/README.md` 補完整 helm 安裝指令。
**驗證**（RX 9060 XT 實測）：`pytest` 307 個全過（新增 `tests/test_hardware.py` 12 個，
含「兩台機器載入方式必須相同」）；四個端點實際生成都正確（`/classify` 1.5s、`/diagnose` 1.5s、
`/infer` 3.3s）；長 prompt 壓測顯存 reserved 13.27GB → 10.05GB；Model Server + embedding
同時在 GPU 時整張卡高峰 14.0GB / 15.9GB；RAG 檢索 GPU fp16 與 CPU fp32 的 Recall@3（95.8%）、
MRR（0.938）完全相同，查詢延遲 1970ms → 14.8ms。學校 NVIDIA 那台尚未實機驗證。

### 04:20 — 安全強化：網站可以安全地上外網（客戶自帶叢集計畫第 1 階段）
**為什麼**：要讓客戶從外網使用平台（Tailscale Funnel / Cloudflare Tunnel），原本的 Flask
開發伺服器沒有頻率限制，任何人狂送請求就能耗光 GPU 和 Gemini 額度，也能暴力破解密碼。
**改了什麼**：
- 新增 `core/web_security.py`：token bucket 頻率限制（登入/註冊每 IP 每分鐘 10 次、Chat 每帳號 20 次、
  部署 10 次、其他 API 120 次），被擋時回中英文說明 + `Retry-After`，登入表單直接回登入頁顯示原因。
- `web_demo.py` 改用 waitress 啟動；cookie 加 HttpOnly / SameSite=Lax，https 對外網址時加 Secure。
- 新增 `PUBLIC_BASE_URL`：有設定時只綁 127.0.0.1、只信任本機通道送來的 X-Forwarded-*；
  Google 登入回呼網址改由它組出，不依賴通道帶不帶代理標頭。
**驗證**：`pytest` 317 個全過（新增 10 個）；實機啟動確認 `Server: waitress`、第 11 次登入嘗試回 429
+ 中文說明；外網模式只綁 127.0.0.1、cookie 帶 Secure、Google 回呼網址是 https 對外網址、同一個通道後
不同客戶端 IP 各自計數。過程中發現 waitress 預設會清掉不受信任的 X-Forwarded-*（所以 werkzeug
ProxyFix 無效），改用 waitress 的 trusted_proxy。真正透過通道從外網連線待使用者設定 Tailscale 後驗證。

### 05:10 — 修復：帳號名稱相似就會共用工作空間（跨帳號隔離破口）
**為什麼**：背景安全審查連續兩輪回報客戶 repo 撞名，追查發現平台既有的 `_user_namespace()` 也有
同樣問題：`alice`/`Alice`、`bob_1`/`bob-1` 共用 namespace，任何兩個中文帳號都變成 `user-unknown`，
可以互相看到、刪除對方的 Pod。
**改了什麼**：乾淨格式的帳號名稱維持原名（既有 namespace 不變），其他帳號加 SHA-256 前 16 字元
後綴；註冊（含 Google 自動產生名稱）時確定性檢查平台 namespace 與客戶 repo 名稱都不能撞到既有帳號。
客戶 repo 推送的 token 改走環境變數，不再出現在 git 指令列。
**驗證**：`pytest` 338 個全過（新增撞名、搶註、Google 名稱、token 不在指令列等回歸測試）；
現有 10 個帳號只有 1 個的 namespace 會變，且沒有任何既有資料需要搬移。
詳見 `docs/security_review.md` 32 節。

### 05:50 — 新功能：客戶叢集安裝包、唯讀回報器、連線狀態（客戶自帶叢集第 3 階段）
**為什麼**：客戶要貼一行 PowerShell 指令就把自己的叢集接上平台；平台不能連進客戶叢集，
所以需要一個只往外送狀態的回報器，連線檢查與之後的 Healer 都靠它。
**改了什麼**：
- `cluster/onboarding.py`：PowerShell 安裝腳本（檢查 kubectl/叢集 → 安裝 Flux v2.9.6，已有就略過 →
  套用安裝包 → 等回報器，每步回報進度；失敗用 return 不會關掉使用者視窗）與安裝包
  （namespace 加 Pod Security baseline、Flux 用只限該 namespace 的 SA 套用、唯讀 deploy key、
  唯讀回報器）。用 dict → yaml.safe_dump 產生，私鑰/token 不可能破壞 YAML 結構。
- `cluster/agent/reporter.py`：只用標準函式庫、只有 get/list 權限、完全不讀平台回應。
- `cluster/remote_state.py`：token 只存雜湊（安裝 token 30 分鐘、重新產生即作廢）、回報白名單與
  大小限制、連線檢查頁的 11 種狀況判斷與中英文說明。
- `web_demo.py`：`/onboard/<token>.ps1`（過期回一段只印說明的腳本）、`/onboard/<token>/bundle.yaml`、
  `/onboard/<token>/progress`、`/agent/report`；封鎖帳號時撤銷 token。本機開發時回報網址自動改用
  `host.docker.internal`（Pod 裡的 localhost 是 Pod 自己）。
**驗證**：`pytest` 372 個全過（新增 remote_state 22、onboarding 11）；安裝包 `kubectl apply --dry-run`
通過、PowerShell 腳本語法解析 0 錯誤；在本機叢集實際跑回報器：Pod Security baseline 放行、
權限沒有任何被拒絕、透過 host.docker.internal 回報成功（HTTP 204）、故意部署的 CrashLoop Pod
被抓到錯誤原因與 log。完整安裝流程（含 Flux 拉 GitHub repo）待使用者設定 GitHub org/token 後驗證。

### 06:40 — 新功能：客戶叢集引導流程（同意書 → 執行指令 → 連線檢查）（第 4 階段）
**為什麼**：客戶要先清楚知道會裝什麼、平台能做什麼，同意後貼一行指令，確認連上才能使用網站。
**改了什麼**：
- `cluster/pages.py`：三個引導頁面，跟主網站共用 CSS。同意書寫明要準備什麼、會安裝什麼、平台能/不能做什麼、
  會傳回哪些資料、風險、如何解除安裝；按鈕「不同意並離開 / 同意並繼續」。指令頁有步驟教學、一鍵複製、
  到期時間、「檢視腳本內容」；按鈕「返回 / 繼續」。檢查頁每 5 秒自動檢查，依 12 種狀況給說明與進度清單。
- `web_demo.py`：`/onboarding`、`/onboard/consent|decline|back`、`/api/onboard/status`；新增
  `CUSTOMER_CLUSTER_MODE`（預設關，維持本機叢集模式），開啟後非管理員帳號第一次連線成功前只能使用引導流程。
- 安全修正（背景安全審查）：安裝包只能下載一次（防止指令外流被重放取得 deploy key）、回報大小改從串流讀取
  （chunked 傳輸沒有 Content-Length，原本可繞過上限）。
- 安裝腳本：新增「安裝進行中」狀態（原本顯示成「回報程式沒啟動」會誤導）、等待 Flux 延長到 10 分鐘並提示、
  kubectl 錯誤不再被 PowerShell 5.1 包成紅色 NativeCommandError。
**驗證**：`pytest` 383 個全過；用 playwright 實際註冊 → 自動導向同意書 → 同意 → 指令頁，截圖確認；
**在 PowerShell 實際貼上指令執行**，檢查頁即時顯示進度。實測抓到 3 個問題（上述狀態誤導、錯誤訊息難讀、
這台電腦的網路下 Flux 映像檔平行下載卡住）都已處理；完整流程待 Docker 重啟與 GitHub 設定後驗證。

### 13:10 — 新功能：所有功能接上客戶叢集（客戶自帶叢集第 5 階段）
**為什麼**：客戶連上自己的叢集之後，部署、Pods、Healer 等功能原本還是指向平台自己的本機叢集。
**改了什麼**：
- `cluster/remote_view.py`：把回報器的報告轉成跟本機 `k8s_*` 函式完全相同的格式（Pods、Deployments、詳情、
  Services、節點容量、資源加總、Dashboard、Pod 用量與趨勢、Healer 問題清單），健康判定規則跟本機一致。
- `cluster/remote_ops.py`：Healer 補救改成 Git 操作（CrashLoop → 滾動重啟、映像檔問題 → 恢復上一個不同的
  映像檔、OOM → 記憶體加倍且上限 4Gi、其他只給建議不自動改）。
- `web_demo.py`：用「同名重新綁定」包裝查詢函式，namespace 是客戶端（`zt-`）時改讀報告，所有既有呼叫端
  （Chat 接地問答、事實核對、衝突檢查、Dashboard…）自動支援，本機版程式碼一行沒改；部署/擴縮/更新/回滾/
  刪除/修復變成對客戶 repo 的 commit；叢集離線時擋下寫入並說明；新增客戶叢集的背景自動修復迴圈；
  頁面頂端顯示「你的叢集」狀態，離線時整條紅色橫幅（平台剛重啟的一分鐘內改顯示黃色「等待回報」）。
- 回報器 v2：補 Pod IP、容器規格、Deployment 時間、Service。
**實測抓到並修好的嚴重 bug**：Healer 原本用「還原最近一次變更」修映像檔問題，修復 commit 後到叢集同步前
壞 Pod 還在，手動修復跟背景迴圈各觸發一次，**設定在好壞之間來回跳了 5 次**。改成「恢復最近一個不同、且不在
已知壞掉清單的映像檔」＋ 每個服務修復後 3 分鐘冷卻；同情境重測只產生 1 個修復 commit。
**驗證**：`pytest` 409 個全過（新增 25 個，含來回跳與冷卻的回歸測試）；**用真的客戶叢集、全程走外網網址**
實測部署（40 秒生效）、擴縮（40 秒）、壞映像檔 → Healer 35 秒偵測 → 自動修復 15 秒恢復、回滾、刪除
（Flux prune 40 秒移除）；playwright 截圖確認連線/離線狀態顯示。

### 13:40 — 修復：重跑安裝指令後回報器被拒、客戶可耗盡平台 GPU（第 6 階段實測與安全審查）
- **重跑安裝後叢集看起來離線**（實測抓到）：安裝包每次都會換回報 token，但更新 ConfigMap/Secret 不會重啟
  回報器，跑著的回報器拿舊 token 被拒（401）並退避成 5 分鐘一次。回報器 Pod 範本加上「程式碼 + token」
  的雜湊 annotation，任一個變了就自動滾動重啟（annotation 只放雜湊，不洩漏 token）。實測重跑後舊 Pod
  被換掉、新回報器 204。
- **資源耗盡**（背景安全審查）：客戶可以不經平台直接在自己叢集建大量壞 Pod，背景自動修復會對每個都跑
  LLM 診斷 + GitHub commit。改成每位客戶每輪最多 3 個、每小時最多 10 次，手動「全部修復」每次最多 5 個。
**驗證**：`pytest` 412 個全過（新增重啟雜湊、每小時上限、全部修復上限的回歸測試）。

### 13:50 — 修復：Gemini 額度用完/過載時，中文部署指令會裝錯映像檔
**為什麼**：兩人同時使用的 QA 中，同樣的「部署 2 個 nginx:1.27」單獨送 4/4 正確、兩人同時送 8 次錯 5 次。
查 log 發現不是兩個人的資料互相干擾，而是同時送出時 Gemini 回 429（額度）/503（過載），退回本機的
`_deterministic_deploy_parse`，而它對中文輸入的映像檔比對有缺陷：要求映像檔後面先有空白才接「，」，
「名稱」也不在清單裡，結果**丟掉版本號，甚至把 redis 換成 nginx（使用者要 redis 卻部署 nginx）**。
**改了什麼**：新增兩條比對規則（帶 tag/registry 路徑的 token、常見映像檔名稱前後非英數字即可），
並排除 `port:80` 這類設定與「10:30」這類時間。QA 腳本的判定規則原本只檢查「有沒有 nginx」，
把這個錯誤算成通過，改成映像檔必須完全一致。
**驗證**：`pytest` 418 個全過（新增 7 個回歸測試）；第一輪 QA 原始資料保留為
`docs/qa_concurrent_20261009_run1.*`。

### 14:00 — 客戶叢集收尾：自癒三條路徑全部實測、頁面文字修正、兩人並行 QA 報告（第 6 階段）
**實測**（真的客戶叢集、外網網址、背景迴圈自己處理，沒有手動按修復）：
- 映像檔拉不到 → 恢復上一個可用映像檔（先前已驗證）
- **程式一直崩潰** → 自動滾動重啟。原本偵測不到「一直正常結束（exit 0）又被重啟」的服務，補上「重啟 3 次以上且目前不在執行」也算故障。
- **記憶體不足** → 8Mi → 16Mi → 32Mi 後恢復。原本「同一個服務同一種問題只處理一次」會卡在加倍一次不夠的情況，
  改成把目前的記憶體上限和映像檔也算進判斷，設定改過就算新狀況（仍有冷卻時間、每小時上限、4Gi 上限）。
**修正**：
- 平台本機的背景修復迴圈會掃到客戶 namespace，跟客戶迴圈搶同一個 Pod（冷卻機制有擋住，但不該依賴）→ 本機迴圈跳過 `zt-`。
- 「冷卻中、這次沒動作」不再寫進修復紀錄（原本會出現沒做事卻打勾的紀錄）。
- Dashboard 趨勢圖、Metrics 頁在客戶模式下原本顯示「Prometheus 需要累積」、紅字「Offline」，會讓人以為壞掉；
  改成說明客戶叢集沒有指標收集元件、資料來自回報器。
**兩人並行 QA**：29/29 通過（嚴格規則），隔離 4/4。報告 `docs/qa_concurrent_20261009.md`、
資料 `docs/qa_concurrent_20261009*.jsonl`、圖表 `docs/qa_concurrent_20261009_dashboard.html`。
**驗證**：`pytest` 419 個全過。

### 18:40 — 修復：Gemini 改用團隊付費專案、補上資料去向說明、挖礦映像改用確定性規則擋下
**為什麼**：原本 6 把 key 來自不同帳號、輪流湊免費額度，違反 Google 條款；同意書沒說輸入會送到 Gemini，
接地問答還會把叢集狀態一起送出；部署和換映像檔完全擋不住挖礦程式（`metal3d/xmrig` 只得到「沒指定版本」警告）。
**改了什麼**：`.env` 只留一把付費 key，模型改成 `gemini-3.5-flash-lite`（2.5 Flash 對新專案 404），程式預設值一起改；
同意書加上 Gemini 說明（版本 `2026-10-09.2`）；`chat_llama()` 只送問題、不送「[現況]」摘要；
`guardian/policy_rules.yaml` 新增 `denied_image_keywords`，`/api/update` 也改成先檢查映像檔；
學校電腦交接文件改成用同一個付費專案的第二把 key。
**驗證**：付費 key 實測 4 題全對，每則部署訊息約 0.00024 美元；`_review_deployment` 對 xmrig 判 `block`；
新增 6 個測試。詳見 `docs/security_review.md` 第 34 節。

### 20:00 — 前端改版第一階段：設計系統、外框、登入註冊、狀態誤報
**為什麼**：使用者認為介面太像 AI 生成（每個標籤都是「中文 / ENGLISH」、千篇一律的白色圓角卡片），
要求更精緻、更有科技感、更多互動，並只用中文（專有名詞與系統名稱維持英文）。樣稿經兩輪確認後實作。
**改了什麼**：整套 CSS 重寫成「艦橋主控台」風格（深海藍底加細格線、四角刻度模組、儀表訊號藍，
預設深色、可切換淺色並記住選擇）；左側選單固定、改成中文並加上系統狀態面板；頂端列放頁面名稱、時鐘、
說明書、深淺色切換；登入註冊頁與錯誤訊息改中文；對話欄有自己的底色擋住格線。舊的變數名稱保留成別名，
行內樣式不用逐一改；寫死的顏色換成色票，深色下才看得清楚。
**順便修的錯誤**：模型伺服器沒在執行時頂端仍顯示「Model Loading...」，新手會一直等 → 改成分三種狀態，
沒在執行時顯示整條紅色橫幅並寫出啟動指令（只對管理員顯示指令）；Deploy 頁寫著舊的「LLaMA-3.1 + LoRA」；
GitOps 頁的重新整理按鈕被拉滿整排。
**驗證**：`pytest` 全過（登入錯誤訊息改中文，6 個測試的檢查字串一起更新）；渲染後的前端 JS 用 `node --check` 檢查；
Playwright 截圖檢查登入、對話、自動修復、深淺色。各頁內容的中文化與互動模組在後續階段處理。

### 22:30 — 前端改版第二階段：Chat 互動模組、部署單、部署進度；順便修 5 個實測抓到的問題
**為什麼**：使用者要求凡是跟系統有關的回答都要有好看的排版和互動（例如 Pod 清單要是表格、每頁 10 筆、可換頁、點進去看詳情）。
**改了什麼**：
- Chat 查 Pod、服務、部署紀錄、資源、Healer 掃描、單一 Pod 或服務狀況時，回覆變成可篩選、排序、換頁的模組；
  點任一列從右側滑出詳情（狀態、Healer 診斷、即時用量、容器、事件），服務詳情裡可以直接擴縮、更新映像檔、回滾、刪除，
  每個操作都會先在對話裡出現確認卡。模組的資料與頁碼存在訊息裡，重新整理後照樣能操作。
- 部署單：規格表、三項審查依序掃描、最後蓋上「放行 / 注意 / 退回」封印；部署後顯示實際進度，每 3 秒查一次叢集，
  副本真的就緒才顯示「運作中」，重新整理後會接著追。破壞性操作的確認卡顯示變更前後（例如「2 個 → 3 個副本」）。
- 一般問答改成先跳脫再排版（條列、粗體、程式碼），不再把模型輸出直接當 HTML。
**實測抓到並修好的問題**：
- `.env` 漏了 `USE_LLM_NORMALIZE=1`，Gemini 翻譯層在部署解析上一直是關的；本機備援解析器又只認「port 8088」，
  「開 8088 port」被靜默改成 80。兩邊都修：加回設定、解析器支援數字在前的寫法（新增 6 個測試）。
- 前端與伺服器的部署規則是「開頭是 deploy」，「Deployment 跟 Service 差在哪？」被當成部署指令（加單字邊界，新增測試）。
- `users.json` 的舊格式帳號只存字串，封鎖檢查對字串呼叫 `.get()`，該帳號每個請求都 500（新增測試）。
- 審查卡把「高嚴重度」直接顯示成「擋下」，跟整體判定「可以部署」矛盾 → 改以 orchestrator 實際的阻斷原因為準。
- Healer 掃描沒列出問題時顯示「所有 Pod 都正常」，但剛開始崩潰、還沒達到自動修復門檻的 Pod 其實不正常 → 交叉比對 Pod 狀態如實說明。
**驗證**：`pytest` 全過；渲染後 JS 用 `node --check`；用真實叢集建 15 個測試 Pod（含一個一直崩潰的）走完整流程截圖，
測試帳號、namespace 已清除。

### 23:40 — 前端改版第三～五階段：頁面重整、客戶引導頁、全站只用中文
**改了什麼**：
- 選單從七項變五項：對話、我的服務（原 Pods 與 Deployments 合併）、自動修復、資源總覽（原 Dashboard 與 Metrics 合併）、部署紀錄。
  各頁直接重用對話裡的互動模組，整站同一套外觀；Healer 原本的重啟趨勢圖與健康時間軸搬進 Pod 詳情抽屜。
  我的服務每 15 秒、資源總覽每 30 秒自動更新，背景更新失敗時保留舊資料，不會整塊變成錯誤。
- 移除沒有入口的舊頁面（Deploy、知識庫、訓練資料）與只在舊確認卡裡使用的程式；舊頁面名稱導到合併後的新頁面。
- 客戶引導頁（同意書、執行指令、連線檢查）改用同一套設計，深淺色與主頁面共用設定，拿掉 emoji 和步驟圓圈符號。
- 全站只用中文：登入、部署、審查、自動修復、客戶叢集連線狀態、PowerShell 安裝腳本裡所有「中文 / English」並列的訊息
  都只留中文；訊息裡的舊頁面名稱改成新名稱；說明書改寫並拿掉 emoji；英文版說明書移除。
**驗證**：`pytest` 全過（3 個測試原本檢查英文字串或要求中英並列，改成檢查中文）；渲染後 JS 用 `node --check`，
並檢查頁面上每個被呼叫的函式都有定義（抓到 4 個殘留呼叫並清掉）；用真實叢集 15 個測試 Pod 走過五個頁面、
抽屜、說明書、對話查詢與重新整理，沒有任何 JS 錯誤；手機寬度（400px）沒有被撐寬。測試資料已清除。

### 23:59 — 修正：以客戶身分實際展示時發現的問題（零接觸、白話、版面）
**為什麼**：用展示帳號從對話框一路操作（查 Pod、問壞掉的服務、部署、擴縮、問觀念），發現幾處跟「客戶不用碰指令」的定位衝突。
**改了什麼**：
- 問答會教客戶打 `kubectl logs`；Healer 的診斷建議和修復訊息也寫「請執行 kubectl …」→ 提示詞加上零接觸規則，
  Healer 建議全部改成平台裡做得到的事（看 Pod 詳情的 log、讓 Healer 修、到我的服務改設定）。
- Pod 詳情直接顯示有狀況 Pod 的最近 log，Healer 診斷也改用這份 log。原本診斷只拿到一行狀態訊息，
  log 裡明明寫著「cannot connect to database」卻被判成證據不足。
- 部署紀錄頁說「擴縮也會寫進 Git」，但本機模式的擴縮其實沒有 → 改成寫實的說法。
- 「已調整 shop-web replicas=3」「deploy cache to user-showcase」這類程式用語改白話；審查摘要開頭的 ✓ 拿掉；
  問答裡的 `_pod_` 底線改成斜體。
- 部署單與服務詳情的輸入框沒套到主題；大螢幕上對話欄加寬。
**驗證**：`pytest` 全過（1 個測試原本要求訊息含 kubectl，改成要求引導到 Pod 詳情且不含 kubectl）。

### 23:59 — 修正：部署前審查看的設定跟實際部署的不一致
**為什麼**：審查一直提醒「沒設定資源上限」，但實際部署其實會自動補上，把關的結論跟實際部署的東西對不上。
追查發現三處各用各的預設值：審查的設定沒有資源欄位、本機部署依映像檔類型補預設、客戶叢集 Git 設定固定 100m/128Mi。
**改了什麼**：新增 `agents/cost_agent.recommended_resources()`，審查、本機部署、Git 設定三處共用，安全設定也一併對齊。
審查卡也改版：安全、成本、效能做成標題，判斷到的問題逐條列點並依嚴重程度標色。
**驗證**：`pytest` 全過，新增回歸測試；實測同一個 nginx 部署，審查的問題從 4 項降到 1 項，且剩下那項是真的。

## 2026-10-10

### 03:30 — 新功能：情境解析部署＋省錢估算、Chat 語音輸入
**為什麼**：市場調查的結論是主打「部署前替新手決策」。新手講不出副本數和記憶體，但講得出「500 人報名、三天後結束」。
**改了什麼**：
- 新增 `agents/scenario.py`：人數、天數用固定規則從原句抽；Gemini 只補重要程度、要不要存資料，它給的數字必須在原句出現才採用。
  副本數依「尖峰兩成同時在線」「每副本可服務人數（依服務類型）」「重要服務至少 2 個」換算，上限 10 個。
  使用者明講副本數時照使用者的，只附建議。
- 部署單新增情境推算區塊：依據（標出哪些是 AI 判讀）、推算步驟、提醒（要存資料、用完記得刪除）、費用比較條。
  省錢估算的比較基準寫明：「照總人數開副本、整月不關」對比「照情境推算、只開活動那幾天」，依 AWS 牌價，兩項加總等於兩端差額。
- Chat 輸入框加麥克風（瀏覽器內建語音辨識，zh-TW），辨識結果放進輸入框、不自動送出；等待權限、被擋、沒聲音、找不到麥克風、
  不支援的瀏覽器都有對應說明。
- 修正既有 bug：「200個人會用」被當成 200 個副本而直接報錯。
- 安全審查抓到情境規則的 ReDoS（500 個空白要回溯 113 秒），所有比對改成有長度上限、輸入只看前 600 字。
**驗證**：新增 `tests/test_scenario.py` 36 項（含惡意輸入 0.5 秒內完成）；`pytest` 全過；
用展示帳號在瀏覽器實測三種情境句、審查帶入推算的副本數、麥克風收音與結束。（commit `ef960fc`、`eaf68fa`）

### 03:50 — 新功能：平台事件紀錄層＋一句話產生分析報告（含 PDF）
**為什麼**：要能回答「這個月平台幫我做了什麼、擋下什麼、修了什麼」，原本這些資訊只在記憶體（重啟就消失）或散在 Git 歷史裡。
這也是續訂的理由：用具體數字說明平台的價值。
**改了什麼**：
- `core/event_store.py`：內建 SQLite（`data/events.db`，不新增依賴），記錄部署、審查判決、擴縮、換映像檔、刪除、回滾、
  Healer 修復，以及每 10 分鐘一筆的資源快照（本機叢集與客戶叢集回報都有）。每筆帶 namespace，查詢只能指定單一 namespace；保存 180 天。
  用 `after_request` 依操作結果記錄，Healer 在最外層包一層，手動、一鍵、背景修復都會記到；寫入失敗不影響操作本身。
- `agents/report.py`：統計、每日趨勢、規則觀察；`/api/report` 只查目前帳號的 namespace。
  AI 解讀（Gemini）只拿統計結果，句子裡的數字查不到就整句刪掉並告知刪了幾句；Gemini 沒回應時明說，統計不受影響。
- Chat 說「給我這個月的分析報告」「週報」「過去 14 天的報表」就產生報告模組：統計卡（數字跳動）、每日活動堆疊圖、
  擋下原因與審查提醒分布、修復原因與出狀況的服務、系統觀察、AI 解讀、最近紀錄；統計、規則、AI 三種內容分開標示。
  可切換 1／7／30／90 天；「下載 PDF」切成淺色列印版，用瀏覽器「另存為 PDF」。
- 實測時抓到並修正：Healer 只查出原因、沒有實際改動（例如人工檢查）也回傳成功，報告原本會算成「修好」。
  現在依處理方式區分「自動修好」與「需要你處理」。
**驗證**：新增 `tests/test_report.py` 17 項（namespace 隔離、過期清除、統計數字、AI 句子數字核對、期間解析、路由隔離與自動記錄）；
`pytest` 全過；展示帳號實際操作（被擋下的超大部署、審查、部署、擴縮）後在 Chat 產生報告，背景 Healer 的真實修復也有被記到；
列印版截圖確認。（commit `09622fd`、`116469d`）

### 04:05 — 修正：Healer 全部判成「證據不足」、對連不上資料庫的服務無限重建 Pod
**為什麼**：使用者看自動修復紀錄發現每一筆都是「證據不足」。追查有兩個原因疊在一起：
1. Docker Desktop 很快就清掉結束的舊容器，讀「崩潰前的日誌」不會丟例外，而是回一句「unable to retrieve container logs…」。
   `_get_pod_logs` 把這句話當成日誌，不再改抓目前容器的日誌，診斷看不到真正的錯誤「FATAL: cannot connect to database」；
   監控模型還從這句話編出「Docker 頻繁拉取映像」。
2. 規則層只認 connection refused；而且「反覆崩潰」這條只描述症狀的規則排在前面，崩潰的 Pod 一定有重啟事件，
   於是永遠命中它、處理方式是刪 Pod 重建。資料庫連不上時重建沒用，Healer 每 30 秒重建一次。
**改了什麼**：日誌拿到空的或「沒抓到」的訊息就改抓目前容器的，兩個都沒有回固定字串，不把錯誤原文給模型；
依賴服務規則補上 cannot connect、could not connect、連線逾時、找不到主機等寫法；「反覆崩潰」規則移到最後。
**驗證**：對實際的 orders-api Pod 重跑：拿到真正日誌、判成依賴服務無法連線、處理方式改成檢查相依服務（不刪 Pod）。
新增 `tests/test_healer_logs.py` 6 項；`pytest` 全過。

### 12:55 — 修正：組員極限測試（user-aaa nginx）發現的問題
**為什麼**：組員對 2 副本 nginx 做存活與極限測試（13 項，結果有 passed 判定），找出 5 個問題。
**改了什麼**：
- 擴展與部署前檢查加上叢集 Pod 位置（節點 allocatable pods）。原本要求 100 個副本只排得下 95 個，其餘卡在 Pending 卻沒有警告。
- 擴展的副本數上限改成跟部署審查同一個設定值（policy_rules.yaml 的 50）。原本擴展寫死 1～100，組員因此能直接擴到 100。
- Healer 也看容器上一次結束的原因：30 分鐘內 OOMKilled 但已自動重啟的也會抓到（原本 3 秒恢復的 OOM 完全沒發現）。
- nginx 映像檔自動加 `NGINX_ENTRYPOINT_WORKER_PROCESSES_AUTOTUNE=1`，依 CPU 上限決定 worker 數；審查、本機部署、Git 設定三處共用 `recommended_env()`。
- 報告的第 5 項「Git 100m/128Mi 跟叢集 500m/256Mi 不一致」：最新版已統一（14817eb），兩個數字分別是需求量與上限，請學校那台更新後再確認。
- 第 1 項「Pod 卡住偵測不到（缺探針）」影響所有部署，做法待決定。
**驗證**：實測 0.5 核 nginx 的 worker 數 32 → 1；真實叢集上擴到 100 個回 409 並說明「目前 39 個、最多 110 個」，部署 100 個副本的審查也被擋下；
新增 `tests/test_stress_findings.py` 15 項，`pytest` 全過。（commit `928bd3f` 與後續）

### 13:10 — 修正：自動健康檢查、對外 port 與容器 port 分開、常見服務預設 port
**為什麼**：極限測試第 1 項（Pod 卡住偵測不到），使用者選「依類型自動加」。動手時發現兩個既有 bug：
「部署 redis」會拿到 port 80（redis 聽 6379）；「nginx 開 8085 port」會把容器 port 也設成 8085（nginx 只聽 80），
這兩種服務其實一直連不上，畫面卻顯示部署完成、副本就緒。加了健康檢查後會直接露出來，所以一起修。
**改了什麼**：
- `recommended_probes()`：nginx、httpd 用 HTTP 檢查（卡住約 10 秒停止分流量、超過 30 秒才重啟）；其他映像檔只加連線檢查、不設重啟條件。
- `container_port()`：使用者講的 port 是對外的 port，已知映像檔在容器裡用它實際監聽的 port，Service 轉過去；不認識的映像檔照舊。
- 使用者沒講 port 時，redis、postgres、mysql 等用自己的預設 port。
- 以上都由審查、本機部署、Git 設定三處共用。
**驗證**：真實叢集：nginx 對外 8085 → 容器 80，連線 200（修正前連不上）；redis 沒講 port 拿到 6379 並就緒；
重現組員測試（暫停一個 Pod 的 worker 25 秒、每 0.2 秒一次請求）：失敗率 11.8%（組員測得 43.5%，但壓測方式不同），
第 11 秒移出流量，恢復後自動回到就緒、沒有被重啟。`tests/test_stress_findings.py` 增至 23 項，`pytest` 全過。（commit `8c2f9e9`、`a8a7ad4`）
既有部署要重新部署才會套用（例如 user-showcase/nginx 仍是 8080 → 8080）。

### 13:25 — 修正：部署時指定的服務名稱被丟掉
**為什麼**：「部署 redis，名稱叫 rpt-cache」部署出來還是叫 redis。追查發現 Gemini 翻譯層其實正確抓到名稱，
但它的輸出沒有副本數就被判成「不完整」，整份丟掉改用本機規則重解析；本機規則又規定關鍵字後面要接空白，
「叫」「名字」「命名為」也不在清單裡。
**改了什麼**：Gemini 結果不完整時，它抓到的使用者欄位（名稱、port、記憶體、CPU、副本數）蓋到最後的解析結果上；
本機規則補上「叫／名字／命名為／取名／名為」，名稱前可接「是／為／：」，英文關鍵字加單字邊界。
**驗證**：四種說法在有 Gemini 與只用規則兩種情況下都抓到名稱；「給 500 人用」不會被誤當名稱；新增 6 項測試，`pytest` 全過。

