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
