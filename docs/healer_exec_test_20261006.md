# Healer 端到端真實故障測試（2026-10-06）

目的：用 `kubectl exec` 直接進到 Pod 裡操作，先確認正常運作，再故意弄壞，觀察
`healer/pod_watcher` + `healer/diagnose` + `healer/remediate` 這條自癒鏈路（以
`web_demo.py` 內常駐的 `_healer_background_loop`，每 30 秒全叢集掃描一次）能不能
真的自動偵測並修復，不用任何人手動按按鈕。

測試資源：獨立的 `healer-exec-test` Deployment，部署在 QA 測試帳號專屬的
`user-qa-sweep-20261006` namespace（跟使用者自己的部署、其他帳號的資源完全隔離），
image 是 `nginx:latest`，1 個副本。

## 時間軸

| 時間 (UTC) | 事件 |
|---|---|
| 16:53:47 | 部署前基準檢查：`/api/healer/status` 確認背景迴圈 `running: true`、`recent_actions` 是空的 |
| 16:54:10 左右 | `kubectl apply` 部署 `healer-exec-test`，Pod 很快變成 `Running`，`kubectl exec` 進去跑 `nginx -v` + `curl localhost` 確認正常（`curl_status:200`） |
| 16:54:15 | 第一次嘗試弄壞：`kubectl exec -- kill 1` 失敗——minimal nginx image 裡沒有 `kill` 這個獨立執行檔（`exec: "kill": executable file not found in $PATH`），記一筆：直接呼叫外部指令行不通，要透過 shell |
| 16:54:24~16:55:59 | 改用 `kubectl exec -- nginx -s stop`（nginx 自己的 graceful stop 指令，一定會讓 PID 1 結束）反覆呼叫 5 次，每次都等 Pod 回到 `Ready` 才再呼叫一次，讓重啟次數累積到觸發 K8s 的 backoff 機制 |
| 16:55:?? | `kubectl describe` 確認狀態轉成 `Warning BackOff: Back-off restarting failed container` ——這是 `healer/diagnose.py` `_RULE_PATTERNS` 比對的 `CrashLoopBackOff` 關鍵字的真實來源 |
| **16:56:10.96** | **Healer 背景迴圈偵測到並完成處理**（`/api/healer/status` 的 `recent_actions[0].time`）——距離問題開始出現（第一次成功 stop 在 16:54:52）大約 **78 秒**，在背景迴圈 30 秒掃描間隔的合理範圍內（中間還要扣掉 K8s 本身進入 backoff 狀態需要的時間，不是單純一次 30 秒就能看到） |
| 16:56:?? | 舊 Pod `healer-exec-test-5f5fdf68cd-gsvld` 被刪除，Deployment controller 自動生出新 Pod `healer-exec-test-5f5fdf68cd-wzh2b`（`kubectl get events` 確認 `SuccessfulCreate`） |
| 16:56:41 之後 | 驗證：新 Pod `Running`、`RESTARTS: 0`，`kubectl exec` 進新 Pod 跑 `curl localhost` 回 `curl_status:200`——**服務完全恢復正常** |

## 結論：自動偵測跟修復都真的有效

**背景自動迴圈是真的在跑、不是只有手動按鈕**：`web_demo.py` 啟動時會開一條
daemon thread（每 30 秒掃全叢集 `namespace=""`），這次整個過程我完全沒有碰任何
「Fix」按鈕或 Chat 指令，從頭到尾只用 `kubectl exec` 操作 Pod，Healer 自己發現、
自己診斷、自己修復，驗證了這條自癒鏈路不是只存在於 UI 手動觸發路徑。

**修復動作正確**：`action: analyze_logs` 對應到 `CrashLoopBackOff` 的規則型分類
（`healer/diagnose.py` `_RULE_PATTERNS`），`remediate.py` 執行的補救是刪除壞掉的
Pod 讓 Deployment controller 重建——這正是這個情境該做的事（nginx 本身設定沒有
壞掉，問題是 process 一直被殺掉，重建一個乾淨的 Pod 就能解決，不需要改 image
或調資源）。

## 發現的真實問題：監控用的小模型（1.5B）幻覈出不存在的根因

`/api/healer/status` 記錄的 `root_cause` 是「**容器資源不足導致 OOM Killed**」——
但這整個測試從頭到尾都沒有碰過記憶體/資源限制，我是用 `nginx -s stop` 反覆讓
process 自己正常退出，`kubectl get events` 查到的真實事件只有
`Back-off restarting failed container`，完全沒有任何 OOM / memory 相關字樣。

這是監控小模型（Qwen2.5-1.5B，`/diagnose` 端點）的**真實幻覈案例**，不是理論推演：

- `healer/diagnose.py` 的流程是「規則層先比對 `CrashLoopBackOff` 關鍵字 → 正確
  判斷 `action=analyze_logs`」，但 `root_cause`／`suggestion` 的敘述文字交給
  1.5B 小模型補充（`_llm_analyze()`），模型自己編出一個具體、聽起來合理、但
  完全沒有證據支持的原因。
- `core/model_server.py` 的 `DIAGNOSE_SYSTEM` 其實已經有一條規則在防這件事：
  「若日誌/事件沒有具體訊號，不要瞎猜具體原因，要說證據不足」——但這次 `logs`
  是空的（`nginx -s stop` 是乾淨退出，容器本身沒留下 crash log），模型沒有照
  這條規則說「證據不足」，反而選了一個常見但這次完全不適用的猜測（OOM）。
  小尺寸模型對這種「該不該承認不知道」的指令遵循度不夠高，跟
  `docs/security_review.md` 第 9 節已經記錄過的「3B 模型指令遵循能力弱」是同一類
  問題，只是這次發生在監控模型（1.5B，比部署模型更小）身上。

**影響範圍**：因為規則層已經把正確的 `action`（analyze_logs，不是
increase_memory）跟 `severity` 蓋過模型的版本（`diagnose_issue()` 本來就有這個
保護：規則命中時，只採用模型的敘述文字，不採用模型的 action/severity），**這次
幻覈沒有導致錯誤的修復動作被執行**，只有「寫給使用者看的根因說明」是錯的。如果
使用者或評審在 Healer 頁面看到這段文字，會被誤導以為是資源不足，進而去調高
`resources.limits.memory`——雖然沒有實際傷害（調高資源上限本身無害），但解決的
不是真正的問題，下次同樣的「process 被 kill 導致重啟」情境還是會重演，只是
使用者會以為自己已經修好了。

**建議修法（這次沒有動手改，留給下次）**：`DIAGNOSE_SYSTEM` 的「證據不足就老實
說」規則需要更強制的落地方式，單純寫在 prompt 裡對 1.5B 這個尺寸不夠可靠——
比照這次對 3B 模型（intent 分類／chat）已經驗證有效的做法，應該在 `logs` 跟
`events` 都是空字串/只有 `Back-off restarting` 這類通用重啟訊息、沒有任何具體
錯誤關鍵字時，**用確定性邏輯直接把 `root_cause` 蓋成「證據不足，只能確定是重複
重啟，實際原因需要人工查看完整日誌」**，不要讓模型自己決定要不要承認不知道。

## 殘留限制

- 這次只測了 `CrashLoopBackOff`（透過重複 graceful stop 觸發）這一種故障模式，
  `OOMKilled`、`ImagePullBackOff` 等其他 `_RULE_PATTERNS` 涵蓋的情境這次沒有
  實測，行為是否跟規則表寫的一致還沒驗證過。
- 沒有測試「Healer 連續修復同一個問題多次」的情境（`_healer_background_loop`
  用 `seen` 集合避免同一個 key 重複觸發，這次只讓它發生一輪就被徹底解決，沒有
  測到這個防重複觸發機制本身）。
