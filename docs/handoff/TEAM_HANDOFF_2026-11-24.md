# 情境部署交接（2026-11-24）：反問判斷、選軟體、資源大小

- **寫給**：接手情境部署準確率的組員（柏碩）。
- **寫的人**：陳冠穎（學校 RTX 5080 電腦）。
- **接續**：[TEAM_HANDOFF_2026-11-20.md](TEAM_HANDOFF_2026-11-20.md)（柏碩 11-20 的版本 `6666a36`）。
- **分支**：`integrate/multiagent-llamacpp`，commit `119e725` 起。
- **相關文件**：
  - 數字細節：[2026-11-24_實驗結果與柏碩版本比較.md](../bench_record/2026-11-24_實驗結果與柏碩版本比較.md)
  - 過程紀錄：[2026-11-23_反問與選軟體改善.md](../bench_record/2026-11-23_反問與選軟體改善.md)
  - 時間順序的清單：[工作紀錄_2026-11-21至24_反問與準確率改善.md](工作紀錄_2026-11-21至24_反問與準確率改善.md)
  - 風險：`docs/security_review.md` 第 69 節

這份文件的目的：讓你不用翻對話紀錄，就知道我改了什麼、為什麼這樣改、證據是什麼、哪些還沒做完，以及從哪裡接著做。

---

## 0. 一頁摘要

1. **你 11-20 定的三個目標，對照現況**（量法照你的定義：新的、沒調過的題目，算整張全對）：

   | 目標 | 現況 | 達成？ |
   |---|---|---|
   | 沒看過的題目，整張全對 75% 以上 | holdout 75.0%（60/80）；同一份題目用你的設定是 61.3% | 剛好達標，但只有 100 題 |
   | 「應該反問的有問」不能變差 | holdout 11/20 → 13/20 | 有進步 |
   | 各欄位不能退步 | 映像檔 70.0→83.8%、port 78.8→83.8%，其他持平 | 沒有退步 |

2. **主要改了三件事**，開關都在 `.env`：
   - **反問判斷**（`PIPELINE_CLARITY=on`）：Gemini 判斷使用者「有沒有講清楚要哪一類軟體」，決定要不要問。
   - **採用 Gemini 建議**（`PIPELINE_TRUST_SUGGEST=1`）：講清楚、而且 Gemini 建議的軟體在知識表裡，就直接用它。
   - **資源刻度**（`PIPELINE_SIZE_TIER=floor`，**還沒開**）：CPU 和記憶體改用知識表建議值高一格。
3. **一定要知道的限制**：
   - holdout 的 75.0% 是**加資源刻度、補知識表之前**的版本。最終版本要用第二組題目（holdout2）驗證，還沒跑。
   - **「該問卻沒問」還是最大的弱點**：holdout 有 7/20 沒問到；s40 從 3/4 降到 1/4。
   - **極短的句子**（例如「架個論壇」）準確率會掉大約 13 個百分點。

---

## 1. 現況數字

| 題組 | 你的設定（`cuda_school`，這台跑） | 目前最好 | 逐題比較 |
|---|---|---|---|
| holdout（沒看過，100 題） | 61.3% | 75.0%（還沒加資源刻度） | 11 對 0，p=0.001 |
| dev_claude（300 題） | 66.0%（你的機器 66.8%） | 78.6%（`tierA`） | 45 對 12，p<0.0001 |
| dev_scenario（200 題） | 40.1%（你的機器 37.6%） | 54.8%（`tierA`） | 35 對 12，p=0.001 |
| s40（組員 40 題） | 77.8% | 83.3%（還沒加資源刻度） | 3 對 1，p=0.63 |
| perturb（1900 題改寫） | 沒跑 | 64.4%（還沒加資源刻度） | — |

- **兩台機器可以互相比**：同一套設定，兩台在 claude 組只差 0.8 個百分點，在你定的 2 個百分點以內。原因推測是 CUDA 12.4 和 13.5 的計算誤差，沒有驗證。紀錄在 `docs/bench_record/2026-11-22_學校電腦llamacpp對齊.md`。
- **你的盲測和我的 holdout 不能直接比高低**：題目不同。你的是 Gemini 出的，資源給得大方；我的是 Claude 出的。能比的是同一份題目上的「前 → 後」。

---

## 2. 改動放在流程的哪裡

先看清楚整條流程，改動的位置才好懂。標 ★ 的是這次新增或改過的：

```
runner.run(text)
├─ normalize.normalize()                Gemini 優先、本地備援，把原句整理成欄位
│   ├─ ★ CLARITY_ADDON                  同一次 Gemini 呼叫裡，多回一個 clarity：
│   │                                   {category, suggest, kind_clear, notes}
│   ├─ ★ apply_clarity()                存進 spec["clarity"]；矛盾、做不到的要求變成部署單提醒
│   └─ ★ _second_check()                PIPELINE_CLARITY_CHECK=gemini2（不採用，預設關）
├─ intent_check.run()
└─ _finish()
    ├─ gap_check.run()
    │   ├─ reasoner.fill_gaps()         推理代理（4B）補空欄位；★ PIPELINE_REASON_SIZING 換 prompt
    │   ├─ _verify_image()              推理代理選的映像檔不在知識表就清掉
    │   ├─ _choose_software()           RAG 前 5 名逐一算信心度（你 11-19 的做法）
    │   │   ├─ ★ _adopt_over_command()  執行環境＋「使用者給的指令」時，改用 Gemini 建議
    │   │   ├─ ★ _clarity()             講清楚了沒：Gemini → 本地機率 → 不判斷
    │   │   ├─ ★ 沒講清楚               就算信心度高也問
    │   │   ├─ ★ 講清楚＋TRUST_SUGGEST  直接用 _gemini_suggest()
    │   │   └─ ★ 講清楚、信心度低        _clear_pick()：建議或最高分，不問
    │   ├─ _table_and_defaults()        port 查表、補預設
    │   └─ adjust_resources()
    │       ├─ _stateful_replicas()     你的規則
    │       ├─ _memory_floor()          你的規則
    │       └─ ★ _tier_resources()      PIPELINE_SIZE_TIER
    ├─ verify.run()  → adjust_resources() 再套一次（冪等）
    ├─ reasoner.apply_extras()          ★ 定時執行沒指令時放範例指令（PIPELINE_CRON_COMMAND）
    └─ format_check.run()
```

- **網站**（`web_demo.py`）：`_pipeline_summary()` 多回兩個欄位：
  - `image_pick`：部署單上的「系統替你選了 X」區塊。
  - `normalizer_reason`：沒用上 Gemini 時的紅色橫幅。

  另外新增 `/api/pipeline/software_info`，給「換成」按鈕查新軟體的 port。
- **顯示文字**：`schema.add_notice`、`add_question` 會經過 `tw_terms()`，把「存儲」「信息」這類中國用語換成台灣用語。只改顯示，不影響判斷。

---

## 3. 每個開關

| 開關 | 預設 | 目前 `.env`（學校） | 建議 | 程式位置 |
|---|---|---|---|---|
| `PIPELINE_CLARITY` | off | **on** | on | `normalize.clarity_mode`、`gap_check._clarity` |
| `PIPELINE_CLARITY_LO`／`HI` | 0.1／0.9 | **0.1／0.6** | 0.1／0.6 | `gap_check.clarity_bounds` |
| `PIPELINE_TRUST_SUGGEST` | 0 | **1** | 1 | `gap_check._gemini_suggest`、`_adopt_over_command` |
| `PIPELINE_CLARITY_COMBINE` | 空 | 空 | 不開 | `gap_check._clarity` |
| `PIPELINE_CLARITY_CHECK` | 空 | 空 | **不開** | `normalize._second_check` |
| `PIPELINE_CRON_COMMAND` | example | （預設） | example | `reasoner._need_cron_command` |
| `PIPELINE_SIZE_TIER` | 空 | 空 | holdout2 確認後開 `floor` | `gap_check._tier_resources` |
| `PIPELINE_REASON_SIZING` | 空 | 空 | 評估中 | `reasoner.reason_system`、`prompts.REASON_SIZING_TIERS` |

### 3.1 `PIPELINE_CLARITY`：講清楚了沒

- **問題**：你的選軟體信心度只看「這套軟體符不符合」，看不出「句子有沒有講清楚」。所以會有兩種錯：
  - 「照舊」「那個系統」這種句子，信心度還是 ≥ 0.8，系統直接猜。
  - 句子其實清楚，但有矛盾或一次要兩套，信心度被拉低，系統就去問。開發題組這樣多問了 39 題。
- **做法**：Gemini 正規化時，同一次呼叫多回一個 `clarity`。**不會多花一次呼叫**。
  - `kind_clear=false`（沒講清楚）→ 就算信心度高也問。
  - `kind_clear=true`（講清楚）→ 信心度低也不問，改用 Gemini 建議或最高分的候選，並在部署單提醒「系統依你的描述選了 X，請確認」。
  - 沒有 Gemini 時，讀本地模型對 `CLARITY_PROB_SYSTEM` 回答 Yes 的機率。≤ LO 就問、≥ HI 就不問，中間照你原本的做法。
- **跟使用者定的規則**：寫在 `CLARITY_ADDON` 裡，有了規則才能判對錯。
  - **該問**：沒講是哪一類軟體；自己的程式沒講語言；矛盾會改變要裝哪一套（例如「網路商店…不對，只是型錄」）。
  - **不用問**：類型清楚，只是設定矛盾（要保存又說不重要、要很多人用又沒預算），或要求做不到（不能被截圖）。這些放進 `notes`，在部署單提醒。
- **保護**：如果 Gemini 說「沒講清楚」，自己卻說得出類別、還建議了知識表裡的軟體，就是自相矛盾。這時改用本地判斷（`_gemini_contradicts`）。第一版 prompt 有 16/36 題這樣。
- **證據**：只看這一步的效果，claude 組從 173 到 183，多問從 37 降到 8。

### 3.2 `PIPELINE_TRUST_SUGGEST`：採用 Gemini 建議

- **問題**：scenario 組選錯軟體的 41 題裡，有 23 題 Gemini 給的建議其實是對的，系統卻沒用。
- **條件**：講清楚、Gemini 有建議、建議的軟體在知識表裡（`software_rag.is_known`）、而且不是執行環境，就直接用，不看信心度。
- **為什麼要在知識表裡**：試過「任何建議都採用」，結果變差，因為 Gemini 會建議根本不存在的映像檔名稱。
- **`_adopt_over_command()`**：
  - 情境：推理代理選了 alpine 這類執行環境，而 Gemini 把「一個小程式在背後默默跑，把紀錄收起來」這種**描述**，抽成「使用者給的指令」。Gemini 抽出來的額外欄位一律被標成使用者講的。
  - 原本：這種情況會提早結束，直接裝 alpine。
  - 現在：改用建議的軟體，那段描述標成「不處理」，並在部署單說明。
  - 定時執行不走這條（模擬 2 對 1 錯）。
- **證據**：claude 組多對的 8 題，幾乎都是 rabbitmq、redis、rclone 這類。

### 3.3 `PIPELINE_SIZE_TIER`：資源刻度

- **問題**：
  - dev_scenario 只要 CPU 和記憶體對了，整張全對就從 75 升到 108/157。
  - 推理 prompt 範本寫著 `"cpu": "<e.g. 250m>"`，系統有 98/157 題給 250m。這是錨定效應（arXiv 2505.15392、2511.05766）。
  - 你也記過：兩組開發題組的資源標準相反，一律調高行不通。
- **做法**：
  - 數字不交給模型，改用你 `rag/knowledge/catalog.json` 的 `resources` 建議值，往上一格。
  - 刻度：CPU 50／100／250／500／1000／2000m，記憶體 64／128／256／512Mi／1／2Gi。
  - `floor` 只往上調，不把推得比較大的調小；`set` 直接設成高一格。
  - 使用者講的、執行環境、知識表沒有建議值的，都不動。
  - 思路參考 LASER（arXiv 2512.19701）：模型判斷類別，數字交給查表和規則。
- **為什麼用刻度、不用倍數**：模擬時「建議值 ×2」最好（scenario 90、claude 208），但 ×1.75 和 ×2.25 都崩（63／194、51／108）。原因是 ×2 剛好把 250m 變成 500m、256Mi 變成 512Mi，碰到標準答案的邊界。這不是真的規則，所以改用刻度。
- **證據**（實跑，Gemini 讀快取）：
  - scenario：75 → 86（19 對 8，p=0.052）。
  - claude：202 → 206（8 對 4）。
- **還沒開的原因**：門檻是看開發題組定的，要等 holdout2 確認。

### 3.4 `PIPELINE_REASON_SIZING=tiers`：拿掉錨定數字（實驗中）

- **做法**：`REASON_SIZING_TIERS` 用字串替換，從 REASON_SYSTEM 產生：拿掉 `250m`／`256Mi`，改成「依負載選刻度」。原本的 REASON_SYSTEM 一字不改。
- **狀態**：結果檔 `dev_claude_tierB.jsonl` 跑到一半，還沒 commit。scenario 組還沒跑。
- **採用條件**：兩組都不變差，而且至少一組明顯變好。

---

## 4. 評估方法（請照這個做，不然數字會虛高）

### 4.1 題組

| 題組 | `--set` | 用途 | 注意 |
|---|---|---|---|
| dev_scenario（Gemini 出題，200 題） | `scenario` | 調整用 | 資源標準偏大方 |
| dev_claude（300 題） | `claude` | 調整用（實際上也被拿來調了） | 偏樂觀 |
| holdout（Claude 出題，100 題） | `holdout` | **已用過**，不能再用它證明新的修正 | 不在 git；我沒讀過題目 |
| **holdout2**（100 題） | `holdout2` | **還沒用**，最終驗證只跑一次 | 不在 git；等使用者抽查 |
| s40 | `s40` | 組員原本的題目 | 該問的只有 4 題 |
| perturb（1900 題） | `perturb` | 同一題換說法，看穩定性 | 標準答案沿用原題 |
| blind_scenario、r4 | — | **不要打開** | — |

### 4.2 獨立驗證題組的做法

1. 另開一個**看不到程式、開發題組、對話**的 Claude（Agent 工具的全新 general-purpose，不要用 fork）。只給它出題規則和欄位格式，並明確禁止讀專案資料夾。出題說明的原文在 commit 的對話紀錄裡；holdout2 用的是和 holdout 一字不差的說明。
2. 用程式隨機抽 20 題寫成抽查檔，讓人工確認。**改程式的人不讀題目**。
3. 鎖定後，每種設定只跑一次。錯誤分析也只看統計（例如 `err_stats.py` 那種只印數字的寫法）。
4. 錯誤統計一旦被拿來決定修什麼，這組題目就不能再用來證明下一輪的進步。

### 4.3 指令

```bash
R=experiments/model_select/reason
# 目前最好的設定（Gemini 讀快取，0 次呼叫）
PIPELINE_CLARITY=on PIPELINE_CLARITY_LO=0.1 PIPELINE_CLARITY_HI=0.6 PIPELINE_TRUST_SUGGEST=1 \
PIPELINE_SIZE_TIER=floor PIPELINE_SELECT_MODE=choose \
  python $R/run_pipeline_dev.py --set claude --tag 新名稱 \
  --gemini-cache $R/results/gemini_cache_claude_clarity_v2.jsonl
# 你的設定當對照（一定要明確關掉，因為 .env 已經開了）
PIPELINE_CLARITY=off PIPELINE_TRUST_SUGGEST=0 PIPELINE_SIZE_TIER= \
  python $R/run_pipeline_dev.py --set claude --tag 對照
# 逐題比較
python $R/run_pipeline_dev.py --set claude --compare cuda_school 新名稱
```

- **Gemini 快取**（`--gemini-cache`）：
  - 用「系統提示詞＋原句」的 sha256 當 key，同一句話第二次直接讀檔。
  - 開關的開／關比較只花一次額度。
  - 改了 `NORMALIZE_SYSTEM` 或 `CLARITY_ADDON`，key 就會變，要重新呼叫。
  - 快取不在 git 裡（`.gitignore`），換電腦要重新呼叫。
- **第一次用 Gemini 跑**：一定要加 `--gemini --max-gemini-calls N`。dev 兩組約 500 次，perturb 約 1900 次。
- **規則類的改動，先離線模擬**：用結果檔裡的 `pred`、`choose_scores`、`clarity`、`sources`，不用重跑模型。資源刻度、採用建議都是先這樣模擬，再實跑確認。

### 4.4 結果檔代號

| 代號 | 內容 |
|---|---|
| `cuda_school` | 你的設定，這台跑 |
| `clarity_c2` | 只有本地判斷 C（範例改成跟題組無關的版本） |
| `gem2_on` | Gemini 判斷講清楚了沒 |
| `gem2t_on` | ＋採用建議 |
| `gem2tc_on` | ＋本地合併判斷（不採用） |
| `fix5b` | ＋知識表補充與流程修正 |
| `fix5check` | ＋Gemini 二次判斷（不採用） |
| `tierA` | ＋資源刻度 floor（**目前最好**） |
| `tierB` | ＋拿掉錨定數字（評估中） |
| holdout 的 `base_local`／`base_gemini`／`final` | 你的設定／網站改動前／Gemini＋採用建議 |
| s40 的 `base_local`／`base_gemini`／`final` | 同上 |
| perturb 的 `final` | `fix5b` 的設定 |

---

## 5. 這次踩過的雷

**評估**
- **prompt 範例照錯題寫會灌水**：本地判斷 C 的第一版範例，模仿了開發題組的錯題（C215、C076…）。換成跟題組無關的句子後，claude 組從 185 降到 179，代表第一版約一半的進步是範例造成的。prompt 範例一律自己寫，不要參考題組。
- **倍數剛好碰到標準答案的邊界**：見 3.3。看到某個參數特別好，先試附近的值。
- **評估程式會讀 `.env`**：`core/config.py` 只補環境變數裡還沒有的值。學校 `.env` 已經開了新開關，所以跑對照時一定要明確設成關閉。測試也一樣：`tests/conftest.py` 已經把所有新開關預設關掉，新測試照抄。
- **兩個評估同時跑同一張顯示卡**：耗時數字不準，llama.cpp 平行請求的結果也可能有極小的差異。比較正式的數字時，一次只跑一組。
- **工作階段重啟後，背景程序可能還在跑**：先查 `Get-CimInstance Win32_Process` 裡有沒有 `run_pipeline_dev`，不要以為停了。

**程式**
- **`runner.resume` 會把「不處理」的額外欄位改回「待處理」**：這是**還沒修的風險**。
  - `_adopt_over_command` 把描述型的「指令」標成不處理。如果同一句話還有別的問題卡，使用者回答後 resume 會把它改回待處理。那時映像檔已經是 rabbitmq，不會再走 `_adopt_over_command`，`apply_extras` 就可能把描述當成啟動指令塞給 rabbitmq。
  - 只影響「選了現成軟體，又有其他問題卡」的情況，不影響評估分數，但真的部署可能起不來。
  - 修法建議：被 `_adopt_over_command` 標記的 extra 多記一個 `kept_unsupported` 之類的旗標，讓 resume 跳過它。要加測試。
- **補知識表不能覆蓋你的資料**：第一次補表沒排除你的 `rag/knowledge/catalog.json`，syncthing 的 port 從 8384 被蓋成 22000，開發題組 C094 因此變錯。已經修好：`scripts/build_software_catalog.py` 的 `_field_catalog_repos()`，並加了測試。
- **補表時不能用「下載次數最多」**：會抓到別人重包的映像檔，例如 `fulhambuilds/vaultwarden`、`dyrnq/open-webui`，部署出去是供應鏈風險。現在只收原作者發布的（Docker Hub 帳號對得上 GitHub 作者、專案或軟體名稱），或 bitnami。
- **Docker Hub 網站 API 每分鐘只能 180 次**：補表腳本已經限速並處理 429 和逾時。重抓一次約 15 分鐘。

**網站**
- **重啟要用專案 venv**：`C:\zt-k8s\.venv\Scripts\python.exe web_demo.py`。工作管理員看到的是系統 Python 的路徑（Windows venv 會轉交），但直接用系統 Python 啟動會少 `requests`，網站起不來。我在這裡停了 3 分鐘。
- **改了 `agents/pipeline`、`web_demo.py`，網站要重啟才會生效。**

---

## 6. 試過、沒用或變差的（不要再試一次）

| 做法 | 結果 | 原因 |
|---|---|---|
| 本地判斷和 Gemini 合併（`CLARITY_COMBINE=or`） | claude 195 → 192，scenario 75 → 73 | 多問變多 |
| Gemini 再獨立判斷一次（`CLARITY_CHECK=gemini2`） | claude 201 → 170、scenario 75 → 66（p<0.01）；多用 453 次 Gemini | 每多問到 1 題該問的，約多錯 3 題 |
| 資源一律提高下限（CPU 500m、記憶體 512Mi） | 兩組都變差 | 兩組標準相反（你也試過） |
| 直接用知識表建議值（不加一格） | scenario 75 → 64 | 建議值偏小 |
| 建議值 ×2、高兩格、人多再高一格 | 碰巧好或變差 | 見 3.3 |
| 任何 Gemini 建議都採用（不管在不在知識表） | 變差 | Gemini 會建議不存在的名稱 |
| Gemini 第一版判斷（欄位叫 `software_clear`） | 太嚴 | Gemini 把它理解成「使用者有沒有講產品名」 |

---

## 7. 網站上看得到的改變（學校電腦已開）

- **「系統替你選了 X」區塊**：系統沒問就選了軟體時，部署單最上方會出現。附 2～3 個候選，只列信心度 ≥ 0.3 的，或 Gemini 的建議；按一下就換掉映像檔和 port。
- **紅色橫幅**：沒用上 Gemini 時出現，依原因給不同的下一步（`core/gemini_client.last_failure()`）：沒有 key、額度用完、測試上限、錯誤、被截斷、回答看不懂。實測沒有 Gemini 時準確度約 61%，有 Gemini 約 75%，橫幅裡有寫。
- **定時執行沒給指令**：先放範例指令，部署單提醒使用者改。部署出去的程式在使用者填指令前不會做事，這是使用者選的取捨。
- **測試帳號 `test1123`**：學校電腦上還沒刪。

---

## 8. 下一步（依優先順序，附起點）

1. **跑 holdout2，取得最終數字**
   - 等陳冠穎抽查桌面的 `新題組2抽查20題_2026-11-24.txt`。
   - 用最終設定跑一次（含 `PIPELINE_SIZE_TIER=floor`，以及實驗 B 有效的話），再加你的設定當對照。約 200 次 Gemini。
   - 這個數字才是對外報告的準確率。只看統計，不要讀題目。
2. **把實驗 B 跑完**：`tierB` 的 claude 組補跑完、scenario 組跑一次，跟 `tierA` 逐題比較。
3. **修 `runner.resume` 的風險**（見第 5 節）：改動小，但真的部署會遇到。
4. **該問卻沒問**（你 11-20 列的第一優先，還沒解決）：
   - 現況：holdout 有 6/7 題沒問到，Gemini 都判斷成「講清楚了」。s40 的 4 題只問到 1 題。
   - 已經排除：再問一次 Gemini、跟本地判斷合併。
   - 還沒試：讓 Gemini 回「可能的軟體種類」清單，有兩種以上差很多的就問（Kuhn et al.《CLAM》、選擇性反問的思路）。也可以只針對「自己的程式沒講語言」這一類寫規則，dev_scenario 的 D040、D065 就是這類（Gemini 判斷錯）。
   - 起點：`gap_check._clarity`、`prompts.CLARITY_ADDON`。
5. **極短的句子**：perturb 的極短版本，整張全對掉約 13 個百分點，主要是選錯軟體，claude 部分的多問也從 8 變 23。起點：`dev_perturb_final.jsonl` 裡 id 結尾是 `_short` 的題目。
6. **映像檔固定版本**：現在部署 `rabbitmq` 這種沒有 tag 的名稱，安全審查會提醒。可以用你知識表裡的 `image` 欄位（已經帶 tag，例如 `nginx:1.27`）。要想清楚對評分的影響（評分只比最後一段名稱，不影響分數）。
7. **重複問已經回答過的事**：使用者說了「排隊中的不能掉」，推理代理的提醒還是問「要不要保存」。起點：`reasoner` 產生提醒的地方，對照已套用的 extras。
8. **scenario 組的上限**：就算資源全對也只到 68.8%，剩下的是軟體和 port。不建議為了這組再調資源。

---

## 9. 檔案清單

| 類別 | 檔案 | 這次改了什麼 |
|---|---|---|
| 流程 | `agents/pipeline/normalize.py` | `clarity_mode`、`apply_clarity`、`_second_check` |
| | `agents/pipeline/gap_check.py` | `_clarity`、`_gemini_contradicts`、`_gemini_suggest`、`_clear_pick`、`_adopt_over_command`、`_tier_resources` 與刻度工具 |
| | `agents/pipeline/prompts.py` | `CLARITY_ADDON`、`CLARITY_PROB_SYSTEM`、`CLARITY_CHECK_SYSTEM`、`REASON_SIZING_TIERS` |
| | `agents/pipeline/reasoner.py` | 範例指令、`reason_system()` 換 prompt |
| | `agents/pipeline/schema.py` | `tw_terms()` |
| | `agents/pipeline/software_rag.py` | `recommended_cpu()`、知識表讀 cpu |
| | `agents/pipeline/runner.py` | 對外欄位保留 `clarity` |
| 網站 | `web_demo.py` | `_pipeline_image_pick`、`_normalizer_reason`、`/api/pipeline/software_info`、前端 `pickHTML`／`fallbackHTML`／`swapPickedImage` |
| Gemini | `core/gemini_client.py` | `last_failure()` |
| 知識表 | `rag/software_catalog.json`、`scripts/build_software_catalog.py` | 331 → 450 筆；`--add-selfhosted [--out 路徑]` |
| 評估 | `experiments/model_select/reason/run_pipeline_dev.py`、`sweep_clarity.py` | 快取、新題組、`load_tasks()` |
| 測試 | `tests/test_clarity.py`、`test_pipeline_web_pick.py`、`test_build_catalog_selfhosted.py`、`test_tw_terms.py`、`test_tier_resources.py`、`conftest.py` | |
| 文件 | CHANGELOG 11-23、11-24；`docs/security_review.md` 第 69 節；`docs/bench_record/2026-11-22`、`11-23`、`11-24` | |

**報告網頁**（圖表版，私人，要分享才看得到）：https://claude.ai/artifact/Ruhh8nF3BkhNcyJDyswNyw。它的數字停在修知識表之前的 `fix5b`，還沒更新 `tierA`、s40、perturb。
