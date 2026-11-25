# Healer 補強方案（2026-11-20）

給接手實作的組員。內容來自四個審查角色（新方法搜尋、K8s 維運、安全與多租戶、評估方法）的報告，
整合時已逐條對照程式碼查證；代理的說法與查證結果不一致的地方，以查證結果為準並註明。

相關程式：`healer/pod_watcher.py`、`healer/diagnose.py`、`healer/remediate.py`、`cluster/remote_ops.py`、
`web_demo.py` 的 `_healer_background_loop`、`_real_heal_pod`、`_remote_healer_loop`。

**進度（2026-11-25）**：
- [x] P13（log 取結尾）
- [x] 階段 0：0.1～0.7 全部（`healer/guard.py`；記憶體上限本機與客戶叢集都是 4Gi；`default` 只通知）
- [x] 階段 1（本機）：快照、dry-run、非阻塞驗證、沒好退回（`healer/verify.py`）；客戶叢集的驗證退回還沒做
- [x] 自動修復寫進 GitOps（`gitops/heal_record.py`，使用者 11-25 決定要寫）
- [x] 評估工具與開發題組（`experiments/healer_eval/`，22 題）：舊版 17/22 → 新版 22/22，結果在 `docs/bench_record/2026-11-25_Healer階段0與1評估.md`
- [ ] 盲測題（由不調整 Healer 的人出）——出題說明與執行工具已準備（`experiments/healer_eval/BLIND_GUIDE.md`、`run_eval.py --faults`）
- [x] 診斷誤判修正（缺 ConfigMap／Secret 不再判成映像檔；新增 `fix_command`、`tune_heap`；結束碼說明）
- [x] 階段 2：擴大偵測，只通知（`healer/detect.py`，9 種）
- [x] 階段 3：剛更新完就崩潰 → 退回最後一個正常運作過的版本（使用者同意自動）；啟動太慢改用 startupProbe；heap 用完只通知
- [x] 階段 4：log 去重＋BM25、處理手冊（`healer/runbooks/`）、證據引用檢查、過去修法、推播固定模板（model server 要重啟才生效）
- [x] 階段 5：定時執行預設帶 podFailurePolicy；Pod 原地調整決定不做（原因見第 3 節）
- [x] 客戶叢集修復的驗證與退回（`cluster/remote_verify.py`）；限流與停手狀態在網站重啟後從事件紀錄還原
- 開發題組擴充到 35 題，最終版 35/35：`docs/bench_record/2026-11-25_Healer階段2到5評估.md`

---

## 1. 結論先講

- **先補洞，再變強。** 現在的 Healer 有 4 個問題會在「擴大自動修復」之後被放大（第 2 節 P1～P4），
  這 4 項要在新增任何自動動作之前做完。
- **變強的主軸是「修完要驗證、沒好就退回」**，其次才是偵測更多故障、診斷更準。
  論文（STRATUS）與實務工具（Argo Rollouts、Kubernaut）的共同結論都是：可退回＋驗證比更聰明的診斷更能提高修好率。
- **評估題組要跟著第一階段一起建**，不要最後才做；沒有題組就無法知道改動有沒有效。
- 診斷模型維持 Qwen2.5-1.5B（CPU），靠規則、log 去重、處理手冊檢索補強，不把 log 送到外部。

---

## 2. 現況：已確認的問題

每一項都已對照程式碼確認（不是只看代理報告）。

| # | 問題 | 位置 | 影響 |
|---|---|---|---|
| P1 | 本機調高記憶體**沒有上限、沒有次數限制**；沒設 limits 的服務會被直接加上 512Mi | `remediate.py` `_handle_increase_memory`、`_double_memory` | 遠端路徑有 4Gi 上限（`cluster/remote_ops.py:18`），本機沒有；可能超過節點容量，或替原本沒限制的服務製造新的 OOM |
| P2 | 背景迴圈的去重 key 是 `namespace/pod名稱/reason`，**沒有冷卻、每小時上限** | `web_demo.py` `_healer_background_loop` | 修復會換新 Pod 名稱，下一輪被當成新問題；CrashLoop 會每 30 秒重建一次，OOM 會一路加倍。使用者故意部署一個必壞的服務就能持續消耗資源。冷卻與上限目前只做在客戶叢集路徑（`_remote_heal_allowed`） |
| P3 | **log 內容能決定自動動作** | `diagnose.py` `diagnose_issue()` 把 logs 併進 `combined_text` 再做規則比對 | 容器自己印出 `OOMKilled`、`ImagePullBackOff` 就會命中規則，而規則決定 action。等於使用者可以挑 Healer 要做什麼 |
| P4 | 服務名稱用 Pod 名稱去掉後兩段**猜出來** | `remediate.py` `_infer_deployment_name`（`rsplit("-", 2)`）、`web_demo.py` `_app_key_for_pod_name` | StatefulSet（`db-0`）、Job、沒有 owner 的 Pod 會猜錯，可能改到同 namespace 的另一個 Deployment |
| P5 | 修復前不存快照、修完不驗證 | `remediate.py` 全部 | 沒修好不知道，修壞了無法退回 |
| P6 | CrashLoop 一律刪 Pod 重建 | `_handle_analyze_logs` | 程式本身的錯重建也沒用；單副本會停機；沒有 owner 的 Pod 刪掉就消失 |
| P7 | 映像檔退回用 `subprocess` 執行 `kubectl rollout undo` | `_handle_fix_image` | 依賴 PATH 與目前的 kubeconfig context，可能和 Python client 指向不同叢集；退回目標只看「映像檔不同」，不看「那一版是否曾經正常運作」 |
| P8 | 偵測只看容器狀態，而且每個 Pod 只回報第一個異常容器 | `pod_watcher._check_pod` | Pending、Evicted、容器在跑但一直沒就緒、Service 沒有端點、更新卡住都抓不到；多容器 Pod 的第二個容器壞了看不到 |
| P9 | `403` 規則太寬 | `diagnose.py` `_RULE_PATTERNS` | 應用程式正常的 HTTP 403 存取紀錄會被判成權限問題 |
| P10 | 本機修復不經過 guardian（dry-run）也不寫 GitOps；稽核只有 print | `remediate.py` `_log_remediation` | GitOps repo 與叢集實際設定不一致；事後查不到改了什麼、舊值是多少 |
| P11 | 背景診斷是單執行緒串行，1.5B 一次最多 2～3 分鐘 | `_healer_background_loop` | 加上「修完等 1～3 分鐘驗證」會讓整個迴圈卡住，必須改成非阻塞 |
| P13 | 交給診斷模型的 log **取的是開頭 800 字**，不是結尾 | `core/model_server.py` `/diagnose`：`str(ctx.get("logs"))[:800]`；`pod_watcher._get_pod_logs` 抓最後 50 行 | 錯誤訊息通常在 log 最後面，最關鍵的幾行反而被切掉。改成取結尾（`[-800:]`）是一行的修正，可以最先做（整合時查到，審查角色沒有發現） |
| P12 | 平台自己的 namespace 沒有預設排除 | `core/config.py` `HEALER_SKIP_NAMESPACES` 預設空白 | `default`（共用資源）、`monitoring` 等都在掃描範圍 |

已確認**沒有問題**的（代理不確定的部分）：
- 客戶叢集修復用的服務名稱有檢查路徑穿越（`gitops/tenant_repo.py` `_app_dir` 會檢查名稱格式，並確認路徑仍在 apps 資料夾內）。
- 診斷模型輸出的 action 只能從固定清單選，不在清單就降成 `manual_inspect`（`core/model_server.py` `_normalize_diagnose_action`、`remediate.py` 的分派表）。
- `/diagnose` 已淨化 ChatML 特殊標記。

---

## 3. 方案（分階段）

每個階段都要加環境變數開關（例如 `HEALER_VERIFY=0` 關閉），方便對照與退回。

### 階段 0：補洞（擴大自動權限之前一定要先做）

| 項目 | 做法 | 驗收 |
|---|---|---|
| 0.1 正確找出控制器 | 讀 `ownerReferences`：Pod → ReplicaSet → Deployment。不是 Deployment（StatefulSet、Job、DaemonSet、沒有 owner）就**只通知、不動手** | 單元測試涵蓋 `db-0`、`my-app-v2-xxx-yyy`、Job Pod、無 owner Pod |
| 0.2 去重與頻率上限 | key 改成 `namespace/deployment/問題類型`；加冷卻（同一服務同一問題 10 分鐘內不重複動手）、每帳號每小時上限、同一問題連續 2 次修不好就停手並推播。本機與遠端共用同一套（照 `_remote_heal_allowed` 的寫法） | 故障注入：必壞的服務 1 小時內自動動作次數 ≤ 上限 |
| 0.3 記憶體上限統一 | 本機與遠端共用常數與函式；上限取 `min(4Gi, (節點 allocatable − 已分配 requests) 的一半)`；同一服務最多調 2 次；requests 與 limits 一起調，保持原本的 QoS 類別；**沒設 limits 的不自動加**（改成通知，或依 Prometheus 實際用量的 1.5 倍建議） | 測試：到上限後不再調；Guaranteed 的 Pod 調完仍是 Guaranteed |
| 0.4 自動動作只認結構化證據 | 會改叢集的動作，觸發條件只看 K8s 結構化欄位：`lastState.terminated.reason`、`exitCode`、`state.waiting.reason`、事件的 `reason`。log 只用來寫說明文字與「只通知」類的判斷（例如連不上資料庫） | 回歸測試：log 印出 `OOMKilled` 但容器沒有真的 OOM，不會觸發 `increase_memory` |
| 0.5 稽核紀錄 | 每次動作寫入 `event_store`（已依 namespace 隔離）：觸發時間、key、診斷來源（規則／模型）、證據摘要、動作、**舊值與新值**、驗證結果、是否退回、命中哪個上限 | 畫面上看得到「改了什麼、改前是多少」 |
| 0.6 規則修正 | `403` 收窄成需要搭配 `forbidden`／`RBAC`／`serviceaccount` 這類字；`memory.*limit` 改成只認結構化的 OOMKilled；多容器全部檢查 | 單元測試 |
| 0.7 排除平台 namespace | `HEALER_SKIP_NAMESPACES` 預設包含 `kube-system`、`monitoring`、`flux-system`；`default` 的共用資源只通知 | 設定測試 |

### 階段 1：修完驗證、沒好就退回（最重要的「變強」）

- **修之前存快照**：原映像檔、原 requests／limits、原探針設定、Deployment revision。
- **先做 server-side dry-run**（guardian 已有 dry-run，可重用），失敗就不動手。
- **非阻塞驗證**：動手後記下「驗證到期時間」，背景迴圈下一輪才檢查，不在原地等。
- **判斷修好了沒**，全部用 K8s 狀態，不靠模型：
  - Deployment `observedGeneration` 追上，且 `updatedReplicas == replicas == availableReplicas`。
  - Pod `Ready=True` 連續 90 秒以上，`restartCount` 沒有比修復前增加。
  - 修復時間之後沒有新的 `BackOff`、`Unhealthy`、`FailedScheduling` 事件（用事件的時間篩選，避免舊事件誤判）。
  - OOM 類再看 Prometheus：`container_memory_working_set_bytes / limit` 低於 85%。
- **沒好就退回快照**，再試下一個方法；每個問題最多「修一次、退一次」，之後停手並推播請使用者處理。
- **映像檔退回只退到「曾經正常運作過」的版本**（那個 ReplicaSet 曾經 availableReplicas > 0），並改用 Python client patch，不用 `subprocess`。
- 依據：STRATUS（NeurIPS 2025）把每個動作配上復原動作、執行前先模擬，失敗就退回檢查點，修復成功率至少是其他方法的 1.5 倍。

### 階段 2：擴大偵測（先只通知＋給建議，確認準確後才考慮自動修）

| 故障 | 偵測方式 | 給新手的建議 |
|---|---|---|
| Pending：資源不夠 | `FailedScheduling` 事件含 `Insufficient cpu/memory` | 縮小 requests（單節點加機器不實際） |
| Pending：儲存空間沒綁上 | `unbound immediate PersistentVolumeClaims`；PVC 狀態 | 檢查 StorageClass；`WaitForFirstConsumer` 不是錯誤 |
| Pending：配額用完 | ResourceQuota 超額事件 | 刪掉不用的服務或調低資源 |
| 容器在跑但一直沒就緒 | Running 且 `Ready=false` 超過 15 分鐘（kubernetes-mixin `KubePodNotReady` 的標準） | readiness 探針的 port 或路徑可能錯 |
| Service 沒有端點 | EndpointSlice 為空；比對 selector 與 Pod label、targetPort 與 containerPort | 指出哪個 label 或 port 對不上（新手最常見的錯） |
| 更新卡住 | Deployment condition `ProgressDeadlineExceeded` | 新版起不來，可以退回 |
| 卡在建立容器 | `ContainerCreating` 太久＋`FailedMount` | 缺 ConfigMap／Secret／PVC |
| initContainer 失敗 | `Init:Error`、`Init:CrashLoopBackOff` | 看 initContainer 的 log |
| 程式一啟動就正常結束 | 非 Job 的 Pod `Completed`、exitCode 0 一直重啟 | 這個映像檔不是常駐服務，可能要改成定時執行或補啟動指令 |
| 映像檔拉不下來的三種原因 | 事件文字分 `not found`／`unauthorized`／`429 Too Many Requests` | 打錯名稱／私有倉庫要登入／Docker Hub 限流，處理方式不同；429 不應該退回映像檔 |
| 結束碼分類 | `exitCode`：137（被殺，再看 reason 是不是 OOMKilled）、1／2（程式錯）、126／127（指令找不到） | 137 不一定是 OOM，也可能是 liveness 殺掉或被驅逐 |
| Evicted | Pod `status.reason == Evicted` | 清掉殘骸；節點記憶體壓力要提醒 |

以上幾乎都能用固定規則判斷，不需要模型。

### 階段 3：CrashLoop 改成依序處理，並比對最近的改動

1. **最近 30 分鐘有改過映像檔或設定**（看 ReplicaSet 建立時間與 Deployment revision，不用另外存）→ 退回上一個曾經正常的版本。但如果是使用者剛主動更新的版本，要在通知裡講清楚「已退回你剛才的更新」。
2. **啟動太慢**（探針失敗、而且之前跑得起來）→ 加 `startupProbe`。官方建議 `failureThreshold × periodSeconds` 要涵蓋最慢的啟動時間；不要再對所有探針一起加 `initialDelaySeconds`。只做一次，並告訴使用者已放寬。
3. **其他**：多副本才重建 Pod，而且最多一次；單副本或非 Deployment 只通知。
4. **程式本身的錯**（exit 1／2、log 有 exception）→ 不動手，給根因與 log 片段。
5. JVM／Node 的 OOM：log 有 `java.lang.OutOfMemoryError`（heap 不夠）跟容器 OOMKilled 是兩回事；前者加容器記憶體沒用，要提示設定 `-Xmx` 或 `--max-old-space-size`。

依據：RCACopilot（Microsoft，653 起事件）與 MetaKube（2026）都顯示「最近改了什麼、過去怎麼修」是最強的線索。

### 階段 4：診斷品質（小模型專用的做法）

1. **log 先去重再交給模型**：去掉時間戳、ID、IP 後把重複行收斂成「模板＋次數」，再挑最相關的幾行（BM25）。1.5B 的上下文有限，現在只給 800 字（而且是開頭，見 P13），去重後同樣的長度能涵蓋更多關鍵錯誤。
   依據：TriFleetRCA（2026，單張本機 GPU，去重＋BM25 挑證據，中位延遲 1.6 秒）。
2. **處理手冊檢索**：把官方除錯文件（Debug Pods、Debug Services、探針設定）與 prometheus-operator runbooks 整理成知識庫，診斷時檢索相關段落一起給模型。
   依據：HolmesGPT 的案例中，同一個模型有 runbook 得 4.6/5，沒有得 3.6/5（單一案例，僅供參考）。
3. **結論必須引用證據**：模型輸出要指出引用的 log 行或事件，程式檢查那段文字確實存在，引用不到就改成「證據不足」。這是把現有 `_has_concrete_signal`、`_unsupported_tech` 的檢查做完整。
4. **同一服務過去的修法**：只查同一個 namespace；只存結構化欄位（問題類型、動作、結果、時間），**不存 log 原文**（避免被當成可持久化的注入）。
5. 模型寫的 root_cause、suggestion 顯示在網頁時一律 escape、限長度；手機推播只用固定模板，不放模型原文。

### 階段 5：選做

- **Pod 原地調整資源**（Kubernetes 1.35 正式版；本機是 v1.36.1）：修 OOM 時可以先原地調高、不重建。
  **限制**：只改 Pod，不會改 Deployment 的設定，下次更新又會回到舊值，所以仍要同步改 Deployment。
  官方文件的例子把記憶體設成需要重啟容器。客戶叢集的版本不一定支援，要先偵測。
  效益有限，放最後。
  **2026-11-25 決定不做**（使用者同意）：Healer 修 OOM 一定要同步改 Deployment（不然下次更新又回到舊值），
  而一改 Deployment 的 Pod 範本，K8s 就會用新設定重建 Pod，原地調整省下的那一次重建就沒有了。
  只有「只改 Pod、不改 Deployment」才有好處，但那樣設定會跟部署紀錄不一致，違反 P10 的原則。
- **定時執行（CronJob）預設帶 `podFailurePolicy`**：依結束碼決定要重試還是直接失敗，從部署時就減少無意義的重跑。這要改部署樣板，不是 Healer。
  **2026-11-25 已做**：exit 126／127 直接判定失敗、被驅逐（DisruptionTarget）不算重試次數；`restartPolicy` 改成 `Never`
  （podFailurePolicy 的要求）。只影響之後新部署的定時執行。

---

## 4. 自動修復權限

原則：**能退回、而且修完會驗證的才自動做；會動到資料、或難以退回的，一律先問使用者。**
四個審查角色對這張表的意見一致。

| 動作 | 自動或先問 | 條件 |
|---|---|---|
| 退回上一個曾經正常的映像檔 | 自動 | 30 分鐘內有改動，而且新版從來沒有正常運作過 |
| 調高記憶體 | 自動 | 有上限（第 3 節 0.3）、同一服務最多 2 次、requests 一起調 |
| 加 startupProbe、放寬探針等待時間 | 自動 | 只改等待時間、只做一次，並通知 |
| 重建 Pod | 自動 | 只限多副本的 Deployment，同一問題最多一次 |
| 改環境變數、密碼、設定檔 | 先問 | 可能讓程式連到錯的地方 |
| 動到儲存空間（PVC） | 先問 | 可能弄丟資料 |
| 改副本數、刪除任何資源 | 先問 | 影響費用與可用性 |
| 非 Deployment（StatefulSet、Job、無 owner） | 只通知 | — |

所有自動動作：修完驗證、沒好退回、推播通知、寫入稽核紀錄。

---

## 5. 客戶自帶叢集

- 平台不連進客戶叢集，修正寫進客戶的 Git repo，由 Flux 套用（`_remote_healer_loop`，已有冷卻、每次掃描最多 3 個、每小時 10 次、記憶體上限 4Gi）。
- 回報器有送 log（最後 8000 字）與事件，所以**診斷、規則、上限、依序處理都可以共用同一套**；只有執行動作分成「直接改叢集」和「寫進 Git」兩種。
- 驗證要多等 Flux 拉取加回報器下一次回報，大約多 1～2 分鐘。
- Service 端點、ResourceQuota 這類偵測，回報器目前沒有送資料；要做得改 `cluster/agent/reporter.py` 並遞增 `AGENT_VERSION`，客戶重跑安裝指令才會更新。建議放在本機版驗證有效之後。
- commit 訊息不要放回報器提供的自由文字。

---

## 6. 評估方法（跟階段 0 同時開始建）

**題組**
- 14 類故障 × 每類 6 題，另加 20 題「正常或暫時性故障」對照（量誤修）。
- 其中 3 類的正確答案是**停手**：程式本身的錯、缺 ConfigMap／Secret、權限不足。量 Healer 會不會亂修。
- 每類至少一半的題目換掉「表面」：不同映像檔（nginx、redis、python、busybox）、不同 port、不同錯誤文字、中英文 log。
- 另外 5～8 題是規則表沒涵蓋、只能靠模型的故障，單獨報告。

**避免自己出題自己考**（情境部署的教訓：開發題組 66.8%，盲測只有 50.6%）
- 盲測題由**不調整 Healer 的人**出題；開發題的故障文字不能寫進處理手冊。
- 盲測前凍結：commit、規則表、手冊、模型與 prompt、上限參數、等待秒數。盲測只跑一次；之後要再調就要換新的盲測題。

**安全的注入方式**
- 每次評估用專用 namespace `healer-eval-<編號>`，結束整個刪掉；Healer 測試時用 `HEALER_ONLY_NAMESPACES` 限制只掃這個 namespace。
- Pending 類題目注入前先量節點剩餘容量，避免擠掉其他服務。
- 測試時可以縮短掃描間隔，正式環境不要改。

**指標**（`passed` 全部由 K8s 狀態自動判定，判定規則跟結果一起存檔）
- 偵測率、診斷正確率（比對預期的 action 代碼，不比文字）、修復成功率（連續 120 秒穩定）、修好所需時間、誤修率、該停手有沒有停手、**修了反而更糟的次數**、各階段延遲。
- 每題 JSONL 欄位：`id, category, variant, split, expected_action, expected_hands_off, detected, diag_action, acted, passed, fail_reason, detect_ms, diag_ms, fix_ms, stable_s, made_worse, judge_rule`。

**對照**
- 盲測只比兩組：目前的 Healer vs 改完的 Healer，同一批題目配對比較（McNemar）。
- 「只有規則」「規則＋模型」「規則＋手冊檢索」這類拆解只在開發題組上做。
- 時間估計：每題 2～4 分鐘，84 題一組約 3～5 小時，建議晚上跑。

**圖表**：各類故障偵測／診斷／修復率（開發組與盲測組並列）、前後版本配對結果、延遲分布、誤修與修壞次數、失敗原因堆疊、診斷混淆矩陣。JSONL、文字報告、圖表三者都存在 `docs/`。

外部參考：Cloud-OpsBench（452 個故障案例、40 種故障類型）、AIOpsLab 把任務拆成偵測、定位、根因、緩解分開計分。只借用做法，不直接用它們的題目，因為那些多半是微服務系統，和新手部署單一服務的情境差很多。

---

## 7. 整合時的取捨（我和審查角色意見不同的地方）

- **原地調整資源**：新方法搜尋把它排在前三。但它不會改 Deployment 的設定，Healer 仍要改 Deployment，而改 Deployment 本身就會換新 Pod，效益比看起來小。放到階段 5。
- **評估規模**：評估角色建議四組對照、每組重跑 3 次。這樣要跑十幾個小時，而且多組比較會增加「挑結果」的機會。盲測只比新舊兩版，拆解只在開發題組做。
- **重建 Pod**：維運角色建議單副本不要自動重建。我同意，已寫進權限表。
- **所有自動修復都走 GitOps**：安全角色建議自動修復也要寫進 GitOps。本機目前沒有裝 Argo CD Application，不寫 GitOps 也不會被改回去，但 repo 和叢集會不一致。這是取捨，列在第 8 節請大家決定。
- **ContainerRestartRules（依結束碼決定重啟）**：各來源對它在哪一版轉成 beta 說法不一，不依賴新功能，只用結束碼本身做規則判斷。

---

## 8. 需要決定的事

1. **自動修復要不要同步寫進 GitOps repo？** 寫的話 repo 和叢集一致、可追溯，但每次修復多一個 commit、變慢；不寫就只記在稽核紀錄。
2. **盲測題誰出？** 要不參與調整 Healer 的人。
3. **記憶體上限**：本機和客戶叢集要不要都用 4Gi（另外再受節點剩餘容量限制）？
4. **時程**：12/02 前要做到哪個階段？建議至少完成階段 0、階段 1 與評估題組。
5. 階段 2 的偵測（Pending、Service 沒有端點等）先只通知，等評估確認準確再開放自動修，可以嗎？

---

## 9. 來源

論文與研究
- STRATUS（NeurIPS 2025）：https://arxiv.org/abs/2506.02009 ；IBM 說明：https://researcher.draco.res.ibm.com/blog/undo-agent-for-cloud
- RCACopilot（Microsoft）：https://arxiv.org/abs/2305.15778
- TriFleetRCA（2026，本機 GPU 的 K8s 根因分析）：https://arxiv.org/pdf/2609.23766
- MetaKube（2026，參考過去經驗的 K8s 診斷）：https://arxiv.org/pdf/2603.23580
- Flow-of-Action（用標準作業流程引導多代理根因分析）：https://arxiv.org/pdf/2502.08224
- Cloud-OpsBench（2026，K8s 根因分析基準）：https://arxiv.org/abs/2603.00468
- The Vision of Autonomic Computing（K8s 上的 LLM 自主管理）：https://arxiv.org/abs/2407.14402
- SynergyRCA（K8s 狀態圖＋LLM）：https://arxiv.org/abs/2506.02490

Kubernetes 官方
- 探針設定與 startupProbe：https://kubernetes.io/docs/tasks/configure-pod-container/configure-liveness-readiness-startup-probes/
- Debug Pods：https://kubernetes.io/docs/tasks/debug/debug-application/debug-pods/
- Debug Services（Service 沒有端點）：https://kubernetes.io/docs/tasks/debug/debug-application/debug-service/
- 原地調整資源（1.35 正式版）：https://kubernetes.io/blog/2025/12/19/kubernetes-v1-35-in-place-pod-resize-ga/
- Job 失敗政策：https://kubernetes.io/docs/tasks/job/pod-failure-policy
- 常見錯誤：https://kubernetes.io/blog/2025/10/20/seven-kubernetes-pitfalls-and-how-to-avoid/

工具與實務
- prometheus-operator runbooks（KubePodNotReady、KubePodCrashLooping）：https://runbooks.prometheus-operator.dev/runbooks/kubernetes/kubepodnotready/
- HolmesGPT（CNCF）：https://www.cncf.io/blog/2026/04/21/auto-diagnosing-kubernetes-alerts-with-holmesgpt-and-cncf-tools/
- K8sGPT 與 HolmesGPT 比較：https://dev.to/siddharth_singh_409bd5267/holmesgpt-vs-k8sgpt-a-2026-head-to-head-comparison-for-sre-teams-2aco
- Kubernaut（爆炸半徑、冷卻）：https://jordigilh.github.io/kubernaut-docs/v1.4/

以下由審查代理提供，**整合時沒有讀原文**，引用前請自行確認：
- Red Hat LogAn（Drain 分群＋小模型分析 log）：https://developers.redhat.com/articles/2026/05/28/logan-large-scale-log-analysis-small-language-models
- EFParser：https://arxiv.org/pdf/2601.22590
- ARBITER（提案與授權分開）：https://arxiv.org/pdf/2607.19182 ；GuardedAct：https://arxiv.org/pdf/2609.11264
