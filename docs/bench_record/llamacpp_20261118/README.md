# llama.cpp 4-bit 與 transformers bf16 對照（2026-11-18，AMD RX 9060 XT）

目的：AMD 不能用 bitsandbytes 4-bit，主模型伺服器的 3B＋1.5B 只能用 bf16（約 9.5GB），和多代理人流程的
4B（約 8GB）同時開會超過 16GB。測 llama.cpp（Vulkan／CPU）4-bit 能不能在不降低品質的情況下省顯存、變快。
只量測，沒有改專案程式。

## 設定
- llama.cpp b12477（Windows Vulkan 與 CPU 版），模型 Qwen 官方 GGUF Q4_K_M：3B 2.1GB、1.5B 1.1GB。
- 提示詞與輸出解析直接取自 `core/model_server.py`（INTENT／DIAGNOSE 的 system、few-shot、`_parse_output`、
  `_clean_intent`、`_normalize_diagnose_action`），兩邊都用貪婪解碼（temperature 0）、repetition penalty 1.15。
- 基準：主模型伺服器 127.0.0.1:8765（transformers bf16）。程式：`scripts/bench/llamacpp_compare.py`。

## 結果

| 任務 | 題數 | 和 bf16 一致 | bf16 延遲中位數 | 4-bit 延遲中位數 |
|---|---|---|---|---|
| 意圖判斷（3B，Vulkan） | 60 | action 98.3%、args 98.3% | 1,187 ms | 454 ms |
| 故障診斷（1.5B，Vulkan） | 12 | action 33.3%、severity 100% | 1,339 ms | 254 ms |
| 故障診斷（1.5B，只用 CPU，16 執行緒） | 12 | action 33.3%（和 Vulkan 版 11/12 相同） | 1,466 ms | 1,134 ms |

- 意圖判斷唯一不同的一題是一段很長、描述模糊的句子：bf16 判成 healer_auto_fix，4-bit 判成 qa（4-bit 較合理）。
- 故障診斷一致率低，另外依每種故障的正確處置訂了參考答案（研究者判斷，12 題，僅供參考）：
  bf16 8/12、4-bit Vulkan 8/12、4-bit CPU 8/12。兩邊錯在不同題：bf16 傾向把設定類問題判成 check_dependencies；
  4-bit 把「找不到程式檔」「資料庫沒設密碼」判成 fix_probe、把「節點記憶體不夠」判成 increase_memory。

| 故障 | 參考答案（可接受） | bf16 | 4-bit Vulkan |
|---|---|---|---|
| Cannot find module | check_dependencies／analyze_logs／fix_image／manual_inspect | ✓ | ✗ fix_probe |
| 資料庫沒設密碼 | create_config | ✗ | ✗ |
| OOMKilled | increase_memory | ✓ | ✓ |
| 映像檔不存在 | fix_image | ✓ | ✓ |
| Secret 不存在 | create_config | ✗ | ✓ |
| 健康檢查 port 錯 | fix_probe／fix_port_conflict | ✗ | ✓ |
| 權限不足 | fix_permissions | ✓ | ✓ |
| EADDRINUSE | fix_port_conflict | ✗ | ✓ |
| 節點記憶體不足（Pending） | manual_inspect | ✓ | ✗ |
| DNS 找不到服務 | check_dependencies | ✓ | ✓ |
| 正常結束 | manual_inspect／analyze_logs | ✓ | ✓ |
| 找不到執行檔 | fix_image／manual_inspect | ✓ | ✗ fix_permissions |

## 顯存
- 量測時主模型伺服器（10.1GB）仍在跑，顯卡已滿：3B 4-bit 專用 1.33GB＋共用 0.83GB、1.5B 4-bit 1.17GB。
  即使部分在共用記憶體，速度仍比 bf16 快。
- 推估換成 llama.cpp 後：3B 約 2.2GB＋1.5B 改用 CPU（0GB），主模型伺服器從約 9.5GB 降到約 2.5GB；
  加上 4B（bf16 約 8GB）與桌面程式約 2.5GB，總共約 13GB，可以同時常駐。

## 限制
- 診斷只有 12 題、參考答案是自己訂的；意圖判斷的「一致」不等於「正確」。
- 聊天問答（/chat）沒有測：它有停止字串、prompt injection 防護、輸出清理，換引擎時要另外驗證。
- llama.cpp 的 repeat_penalty 實作和 transformers 不完全相同。

## 多代理人流程推理模型（Qwen3.5-4B）：Q8 與 Q4、選軟體規則（11-18 下午）

原始資料在 `pipeline/`（逐題 JSONL 含 `passed` 與判定依據）。RAG 一律用欄位檢索（`RAG_BACKEND=field`）。
程式在 `D:/zt-review`（分支 feat/rag-into-multiagent）：`experiments/model_select/reason/llamacpp_grid.py`、
`run_pipeline_claude.py`、`run_claude_grid.py`。

### 挑選規則（實驗前寫死）
S40 通過 ≥ 22 題（組員 v8 是 23）、顯存 ≤ 6GB、不當掉，符合的取 p90 最低；
Q8 與 Q4 的取捨與選軟體規則另用沒看過的 Claude 題組（300 題）做逐題配對 McNemar。

### S40（組員題組）
| 設定 | 通過 | 選對軟體 | 多問 | p50／p90 | 顯存 |
|---|---|---|---|---|---|
| Q8 Vulkan、舊規則 always | 21/40 | 25/36 | 19 | 12.3／16.9 秒 | 4.6GB |
| Q4 Vulkan、舊規則 always | 21/40 | 23/36 | 17 | 8.4／11.0 秒 | 3.25GB |
| **Q8 Vulkan、新規則 agree** | **23/40** | 26/36 | 19 | 12.1／17.1 秒 | 4.6GB |
| Q8 CPU | 太慢（7.5 tok/s，一題約 86 秒），未跑完 | | | | |

### Claude 題組（300 題：262 題要直接答、38 題要反問）
| | Q8 | Q4 |
|---|---|---|
| 全部欄位都對（agree／always） | **57**／51 | 41／39 |
| 選對軟體（agree／always） | **118**／114 | 97／94 |
| 該反問的有反問 | 37/38 | 36/38 |
| 不該問卻問了 | 159 | 182 |
| p50／p90 | 11.6／15.3 秒 | 8.4／11.2 秒 |

- **Q8 vs Q4**（agree 規則、全部欄位都對）：只有 Q8 對 21 題、只有 Q4 對 5 題，p = 0.002 → 選 Q8。
- **agree vs always**（選對軟體）：Q8 +9／−5（p = 0.42）、Q4 +11／−8（p = 0.65）、S40 淨 +1。
  三組方向一致但都不顯著；採用 agree 的理由是不變差且兩邊意見一致時少一次模型呼叫，不是「顯著較好」。
- agree／cur 欄位：Q8 那次的程式在解析「維持 X（…」時有錯，已離線用每題紀錄的候選清單重算，兩組用同一套規則。

### 發現：組員流程在沒看過的題目上問太多
Q8 的 159 題多問裡，問的內容：推理代理自己加的追問（`model:N`）145 個、核對步驟把句子片段直接反問（`extra:`）26 個、
只問軟體 37 題。依難度：簡單 25/75、中等 89/133、困難 45/54。這和引擎、精度、RAG 無關，是流程的提問設計，
在組員自己的 S40 上看不出來（流程是照 S40 調的）。

### 結論
推理模型用 **Qwen3.5-4B Q8_0（llama.cpp Vulkan）＋ agree 規則**，S40 23/40 達到門檻、與組員 NVIDIA 4-bit 的 v8 持平。

### 限制
- Claude 題組的答案範圍是出題程序訂的，評分沒有 S40 的 expose／storage／cron 檢查。
- 只在 AMD Vulkan 測過；NVIDIA CUDA 版要在學校電腦另測。

## 正式配置與改造後的驗證（11-18 下午，worktree `D:/zt-review`）

### 顯存：這張 16GB 顯卡在 Windows 上實際只給約 12.8GB
三顆模型同時啟動時，4B 有 1.03GB 被放到共用記憶體，生成速度從 57 tok/s 掉到 28 tok/s，S40 的 p50 從 12.1 秒變成 23.2 秒，
但輸出完全一樣，不會報錯。改成依序啟動後，換成最後啟動的 1.5B 溢出。可用上限約 12.8GB（含桌面程式約 3.5GB）。
因此正式配置改成：

| 模型 | 位置 | 顯存 | 依據 |
|---|---|---|---|
| 4B Q8（多代理人） | GPU，第一個啟動，context 16384 | 4.86GB | Claude 題組 Q8 > Q4，p = 0.002 |
| 3B Q4（部署、聊天、意圖） | GPU，context 16384 | 2.49GB | 意圖與 bf16 一致 98.3%（Q8 是 96.7%） |
| 1.5B Q4（Healer 診斷） | CPU 8 執行緒 | 0 | 診斷 8/12，與 GPU、bf16 同分，約 1.7 秒 |
| BGE-M3 | GPU | 約 1.5GB | 檢索 16 ms |

三顆加 BGE-M3 同時常駐，顯卡用 10.4GB，llama-server 的共用記憶體都在 0.03GB。

### 經由正式程式路徑的驗證（`scripts/bench/verify_llamacpp_engine.py`，結果 `verify_*.json`）
| 項目 | 結果 | 門檻 | |
|---|---|---|---|
| S40（`agents/pipeline/llm.local_llm`） | 23/40，p50 11.85 秒 | ≥ 22 | 過 |
| Claude 300 題（同上，舊規則 always） | 與單獨執行逐題相同 300/300，沒有失敗、沒有當機，p50 11.5 秒 | 不當機 | 過 |
| `/classify` 與 bf16 | 59/60（98.3%） | ≥ 95% | 過 |
| `/diagnose` 參考答案 | 8/12 | ≥ 8 | 過 |
| `/infer` 同一句送 5 次 | 完全相同 | 相同 | 過 |
| `/chat` 10 題 | 內容合理（與 transformers 版程度相近） | 人工檢查 | 過 |
| 攻擊句（2026-09-14） | 3 次都擋下 | 不洩漏 | 過 |
| **`/infer` 與 bf16 欄位一致率（20 句）** | **45%** | ≥ 95% | **沒過** |

**`/infer` 沒過的分析**：
- 這 20 句是很難的情境描述，而且沒有 RAG 範例，兩種引擎都在猜映像檔。對照開發集答案（需直接回答的 18 題），兩邊都很差，也差不多：
  - llama.cpp Q4：映像檔對 4 題、副本數在範圍內 5 題
  - llama.cpp Q8：映像檔對 3 題、副本數在範圍內 4 題
  - transformers bf16：映像檔對 3 題、副本數在範圍內 8 題
- 不是量化造成的：3B 改用 Q8，一致率也只有 48%。
- 主因之一是 repeat_penalty 的範圍：transformers 套用在整段，llama.cpp 預設只套用最後 64 個 token。改成整段之後，一致率提高到 58%，但正確率沒有變好（映像檔 2、副本數 8），診斷反而從 8 掉到 6，所以維持預設。剩下的差異來自數值精度：貪婪解碼時，只要某一步機率差一點，後面的輸出就整個分岔。
- 實際流程中 `/infer` 是最後的退路：多代理人流程（4B）、DeploySpec 快路徑、固定規則都排在它前面，而且呼叫時會附 RAG 範例。

### 特殊標記注入（直接打 llama-server，繞過淨化）
用 `/apply-template` 加 `/tokenize` 計算角色標記數：

| 模型 | 一般訊息 | 含假冒標記 | 淨化後 |
|---|---|---|---|
| 3B | 5 | 8 | 5 |
| 4B | 7 | 12 | 7 |

llama-server 會把內容裡的特殊字串當成真的角色標記，所以 model_server 與多代理人流程的淨化都是必要的。
