# Healer 盲測題：出題說明

這份說明寫給出 Healer 盲測題的組員。出題的人**不能是調整 Healer 程式的人**。

## 為什麼要另外出題

開發題組（`faults.py`）是調整 Healer 的人自己出的，調整時看過每一題，成績一定偏高。所以正式成績要用沒看過的題目，在版本凍結之後只跑一次。

規則：

1. 題目檔**不要**放進 repo，也**不要**給調整 Healer 的人（包括 Claude）看。存在你自己的電腦或私人雲端。
2. 版本凍結以後只跑一次。跑完不能再改 Healer 程式，然後拿同一批題目重跑。
3. 結果檔（`results/blind_<tag>.jsonl`）可以公開。題目內容等報告寫完再決定要不要公開。

## 出幾題、出什麼

- 建議 **20～30 題**，每一類 2～3 題，表面要盡量不一樣：不同映像檔（nginx、redis、python、node、busybox…）、不同 port、不同錯誤文字、不同副本數。
- **不要照抄 `faults.py`**。可以參考它有哪些類別，但錯誤的寫法要自己想，例如換一種程式語言的錯誤訊息、換一種寫錯設定的方式。
- 正確答案是「不動手」的題目要佔一半以上。Healer 最危險的錯誤是亂修，不是修不好。
- 至少要有 3 題對照組：正常的服務、會印出很像錯誤的 log 但其實正常的服務。

可以考的類別：

| 類別 | 正確的處理 |
|---|---|
| 記憶體不足 | 真的被 OOMKilled，調高一次就好：要修好 |
| 程式本身的錯 | exit 1、例外：不動手。多副本最多重建 1 次 |
| 映像檔打錯，之前有正常版本 | 退回正常版本：要修好 |
| 映像檔打錯，從來沒正常過、私有映像檔 | 只通知 |
| 探針 port 錯、啟動太慢 | 要修好 |
| 剛更新完就壞（30 分鐘內） | 退回剛才的更新：要修好 |
| 缺 ConfigMap／Secret、權限不足、相依服務連不上 | 只通知 |
| 指令找不到、一啟動就結束、heap 用完 | 只通知 |
| Pending（資源不夠、保存空間、配額）、沒就緒、Service 連不到、更新卡住、掛載失敗、initContainer 失敗 | 只通知，而且要報出對的偵測代碼 |
| log 印出假的錯誤字眼（OOMKilled、403、ImagePullBackOff） | 不動手 |

## 檔案格式

一個 JSON 陣列，每一題一個物件：

```json
[
  {
    "id": "b01-node-oom",
    "category": "oom",
    "variant": "node 程式吃 200MB、上限 128Mi",
    "expected_action": "increase_memory",
    "expected_hands_off": false,
    "fixable": true,
    "max_actions": 0,
    "expected_detect": null,
    "expected_remediation": null,
    "deploy": { "apiVersion": "apps/v1", "kind": "Deployment", "...": "完整的 Deployment" },
    "pre_image": null,
    "pre_deploy": null,
    "extra": []
  }
]
```

| 欄位 | 必填 | 說明 |
|---|---|---|
| `id` | 是 | 英數和 `-`，20 個字以內（會用在 namespace 名稱） |
| `category` | 是 | 類別，自己取名即可；對照組一定要寫 `normal`（被擴大偵測誤報會判不通過） |
| `variant` | 否 | 一句話說明這題 |
| `expected_action` | 否 | 期望的診斷代碼：`increase_memory`、`fix_image`、`fix_probe`、`analyze_logs`、`check_dependencies`、`fix_permissions`、`create_config`、`fix_command`、`tune_heap`、`manual_inspect`、`none`。只用來算「診斷正確率」，不影響通過與否 |
| `expected_hands_off` | 是 | `true`：正確答案是不動手 |
| `fixable` | 是 | `true`：結束時服務應該要健康（可修題，或本來就正常的對照組） |
| `max_actions` | 否 | 停手題允許幾次自動動作，預設 0。多副本的程式錯誤可以給 1 |
| `expected_detect` | 否 | 擴大偵測要報出的代碼：`unschedulable`、`pvc_unbound`、`quota_exceeded`、`not_ready`、`no_endpoints`、`rollout_stuck`、`mount_failed`、`init_failed`、`evicted` |
| `expected_remediation` | 否 | 可修題指定要用的處理方式，例如 `revert_recent_change` |
| `deploy` | 是 | 要測的 Deployment（壞掉的那個版本）。容器名稱請用 `app` |
| `pre_image` | 否 | 先用這個映像檔部署、等正常以後，再把映像檔換成 `deploy` 的（測「退回曾經正常的映像檔」） |
| `pre_deploy` | 否 | 先部署這個正常的 Deployment，再把容器設定整個換成 `deploy` 的（測「退回剛才的更新」） |
| `extra` | 否 | 同時要建立的 Service、PersistentVolumeClaim、ResourceQuota、ConfigMap、Secret |

判定方式全部看 K8s 的狀態，不看文字：

- 停手題：自動動作次數 ≤ `max_actions`。`fixable` 為 true 時，結束時還要健康。
- 可修題：結束時健康，而且已經連續穩定 120 秒。
- 有 `expected_detect` 的題：還要報出那個代碼。
- 有 `expected_remediation` 的題：還要真的用那個方式修好。

## 怎麼跑

1. 確認網站跑的是要測的版本。新版網站的背景 Healer 會跳過 `healer-eval-*`，不會搶著修。
2. 在專案根目錄執行：
   ```bash
   python experiments/healer_eval/run_eval.py --tag blind1 --faults D:/你的資料夾/healer_blind.json
   ```
3. 大約 10 分鐘。所有題目同時跑，每題用自己的 namespace，跑完會自動刪除。
4. 結果在 `experiments/healer_eval/results/blind_blind1.jsonl`，螢幕會印出摘要。

注意：

- 題目會在真的叢集上建立 Pod。不要出會吃光整台機器資源的題目，例如真的配置幾十 GB 記憶體。
- 「資源不夠」的題目請用要求很大的 requests（Pod 根本排不上去），不要真的去吃資源。
