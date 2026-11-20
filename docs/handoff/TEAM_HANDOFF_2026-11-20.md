# 情境部署交接（2026-11-20）

分支：`integrate/multiagent-llamacpp`（多代理人流程＋llama.cpp＋11/19–20 的準確率改動）。
詳細數字見 [CHANGELOG.md](../../CHANGELOG.md) 2026-11-19、2026-11-20 兩節；安全相關見 `docs/security_review.md` 68 節。

## 1. 現況

盲測（100 題，從沒看過的題目，只跑過這一次，之後不再用）：

| | 11/18 commit | 現在 |
|---|---|---|
| 映像檔 | 43.7% | 70.1% |
| 副本數 | 81.6% | 90.8% |
| port | 80.5% | 87.4% |
| 記憶體 | 92.0% | 90.8% |
| CPU | 85.1% | 81.6% |
| 不用問卻問了 | 55 題 | 12 題 |
| **整張全對** | **17.2%** | **50.6%** |
| 應該反問的有問 | 11/13 | **2/13** |

- 進步是真的（新版對、舊版錯 29 題，反過來 0 題）。
- 開發題組 dev_claude 上是 66.8%，盲測只有 50.6%：**開發題組的分數偏樂觀約 16 個百分點**。
- **最大的待修問題：該問不問**（11/13 → 2/13）。資訊不夠卻直接部署，比多問一次更危險。

## 2. 踩過的雷

**評估**
- **一直用同一套題目調，分數會虛高。** dev_claude 原本是驗證組，但每個改動都看它決定留不留，等於也拿來調了。盲測一跑就掉 16 點。
- **盲測集只能用一次**（`docs/bench_blind/blind_scenario.jsonl`，不在 git 裡，也不要推上來）。之後要驗證，請找不參與調整的人另外出一批題目，每批只用一次。
- **開發題組、盲測題組的句子都不能放進知識庫**，不然是在背答案。
- 評分要比映像檔名稱的最後一段（`gitea` ＝ `gitea/gitea`），最後一段是 docker、server 這類通用字時看前一段（`nodebb/docker`）。不改會把對的判成錯。
- 兩組開發題組的資源出題標準不同（Gemini 出的把資料庫給 500m 以上，Bitnami 預設 100m）。調高資源一組變好、另一組變差，不要追。
- 測試會讀 `.env` 的 `PIPELINE_SELECT_MODE` 而打到真的模型：`tests/test_pipeline_web.py` 有清掉環境變數的 fixture，新測試照抄。

**試過、沒用或變差的（不要再試一次）**
- 把 RAG 候選放進推理代理的提示詞：選對的從 79 題降到 67 題（小模型不太會用檢索到的內容）。
- 讓 4B 從清單選編號：有位置偏誤，不管怎麼排都選同一個位置。
- 降低選軟體門檻 0.8、分數沒過門檻但第一名領先就採用：答對多幾題，但該問的少問更多題。
- 放寬「資料庫、快取不換」的保護：映像檔多對，但 port 跟著重算，整張全對持平。
- CPU 也做下限、資源一律調高一級：持平或變差。
- 記憶體直接改用軟體建議值（會把推得比較多的也降下來）：兩組都變差，只能當下限。
- 用關鍵字判斷「使用者自己的程式」：32 題只抓到 8 題，誤判 45 題。
- hybrid 檢索＋reranker（10/08）：準確率下降、延遲變 110 倍。

**程式**
- 意圖檢查（verify）會把副本數改回 2、把記憶體改低：`runner._finish` 在 verify 後再套一次 `gap_check.adjust_resources()`。新增會改欄位的規則時，要想到後面的步驟會不會蓋掉它。
- 換軟體時，系統推的 port、服務名稱要一起重算（不然討論區換成 flarum，port 還是 ghost 的 2368）。
- 同分規則（分數差 0.01 內取 RAG 名次前面的）只能在過門檻的候選之間比，不然會把過門檻的換成沒過門檻的而變成反問。
- 正規表示式：在非 raw 字串裡寫 `\\b` 會變成退格字元；判斷「很多人」時，數字要接人、學生、訂單這類單位，不然 8080、512Mi、「千萬不要」都會被當成很多人。
- 選軟體的分數分布改變時（換說明文字、換模型、換顯卡），要重跑 `experiments/model_select/reason/calibrate_choose.py` 重算 `agents/pipeline/calibration.json`，部署單的信心度才準。
- `scripts/start_services.ps1` 不要接 `| Select-Object` 這類管線，會卡住；服務要用獨立程序啟動。
- CI 是 Python 3.9、只裝最少套件：需要 torch、numpy 的測試用 `pytest.importorskip`。

## 3. 做法（建議照這個流程）

1. 每個改動都加一個環境變數開關（例如 `PIPELINE_MEMORY_FLOOR=0` 關閉），方便對照與回退。
2. 先在 dev_scenario（調整組）跑：
   `python experiments/model_select/reason/run_pipeline_dev.py --set scenario --tag <名稱>`
3. 跟上一版逐題配對比較：`--compare <舊tag> <新tag>`（McNemar），再用
   `field_report.py scenario:<舊> scenario:<新>` 看**每個欄位跟自己比**有沒有退步。
4. 規則類的改動可以先用存下來的逐題結果（`pred`、`choose_scores`、`sources`）離線模擬，不用每次重跑模型。
5. 確定要留，再用新的驗證題組確認一次。
6. 重要改動跑 `/code-review high` 與 silent-failure-hunter，`pytest -m "not integration"` 全過才算完成。
7. 不採用的實驗也寫進 CHANGELOG（為什麼不採用），避免下一個人重試。
8. 用 Gemini 跑評估一定要加 `--gemini --max-gemini-calls N`，先估花費，先跑 5 題。

## 4. 學校電腦（NVIDIA RTX 5080 Laptop）要注意

- **llama.cpp 要用 CUDA 版**：`llama-<版本>-bin-win-cuda-12.8-x64.zip`＋同版本的 `cudart-...zip`，一起解壓到 `<LLAMACPP_DIR>\cuda\`（RTX 50 系列要 CUDA 12.8 以上）。步驟見 `docs/setup.md` 6.1 節。
  `start_services.ps1` 看到 `nvidia-smi` 就自動用 CUDA 版，不需要加 `--device`（`--device Vulkan0` 只給 AMD）。
- **GGUF 檔兩台共用**：4B Q8_0（推理，8773）、3B（部署，8771）、1.5B（監控，8772，跑 CPU）。路徑用 `.env` 的 `GGUF_DIR`、`LLAMACPP_DIR` 設定成學校電腦的位置。
- **`.env` 要有**：`LLM_ENGINE=llamacpp`、`PIPELINE_SELECT_MODE=choose`（程式預設還是舊的 select，沒設就不會用今天的選軟體方式）。其他開關預設就是開的。
- **信心度和門檻是在 AMD（Vulkan）上量的**。CUDA 算出來的機率可能有小差異。裝好後先跑一次 dev_scenario，跟 `results/summary_dev_scenario_combo.json` 比：
  - 整張全對差 2 個百分點以內：直接用。
  - 差很多：先查 llama.cpp 版本、GGUF 檔是否相同，再重跑 `calibrate_choose.py`。
- 顯存：4B Q8 約 4.6GB，加 3B 和 BGE-M3，AMD 上合計約 11GB。筆電的顯卡也要供應螢幕，啟動後用工作管理員確認「共用 GPU 記憶體」幾乎沒用到，用到了速度會掉一半。
- 三個模型要依序啟動（腳本已經這樣做），同時啟動會互搶顯存。

## 5. 下一步

1. **修「該問不問」**（最優先）。13 題應反問的只問了 2 題。不要用降低或提高門檻的方式硬調，先看沒問的題目是缺哪個欄位、是哪一步把問題吞掉的（`PIPELINE_ASK_POLICY=essential` 把推理代理的追問改成提醒，是最可能的來源）。
2. 選軟體：第一名對、分數卻不到 0.8（gitea 0.76、nginx 0.66）；靜態網頁、部落格、論壇、維基分不清。改軟體說明要依官方文件寫，不能照題目寫。
3. 定時任務會問「要跑什麼指令」：dev 上有 8 題因此被算錯，但沒有指令確實跑不起來，目前保留。
4. 準備一批新的驗證題組。
