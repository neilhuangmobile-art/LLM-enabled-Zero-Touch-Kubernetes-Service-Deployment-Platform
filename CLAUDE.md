# Steering Guide

給 AI 助理（Claude Code 等）在此專案中工作時的指引。

@AGENT_RULES.md

## 專案是什麼

LLM-enabled Zero-Touch Kubernetes Service Deployment Platform：使用者以自然語言描述需求，
小模型（Qwen2.5-3B）將其轉譯為 Kubernetes 部署 spec，經多代理審核與 dry-run 驗證後，
透過 GitOps 部署到叢集，並具備自動監控與自癒能力。詳細架構見 [docs/architecture.md](docs/architecture.md)。

## 資料流（由輸入到部署）

```
使用者輸入（中/英）→ core/gemini_client（翻譯層優先，正規化成 ### DeploySpec，多 key 輪換）
→ DeploySpec 齊全就直接解析（零 GPU）；不齊全 → llama_client + rag/deploy_index（few-shot）
  → core/model_server /infer（Qwen2.5-3B 4-bit GPU 生成 JSON）
→ 轉換為 YAML（Python 樣板，模型不寫 YAML）→ agents/orchestrator（security/cost/perf → approve/warn/block）
→ guardian/dry_run + guardian/yaml_validator（驗證）
→ gitops/manifest_writer + gitops/argocd_sync（寫入並同步）
→ K8s 叢集 → healer/pod_watcher + diagnose（規則層 → /diagnose：Qwen2.5-1.5B CPU）+ remediate
```

## 模組地圖

| 目錄 | 功能 |
|------|------|
| `core/` | 設定、Model Server（FastAPI 常駐，載部署 3B + 監控 1.5B 兩顆）、`gemini_client.py`（翻譯層，多 key 輪換） |
| `llama_client.py` | 推理入口：翻譯層 → DeploySpec 快路徑 → `/infer`(3B) + RAG few-shot；`diagnose_with_llm()` 打 `/diagnose`(1.5B) |
| `rag/` | `index.json`（k8s 散文，給 `/chat`）、`deploy_index.json`（部署範例 input→JSON，給 `/infer` few-shot）、`retriever.py` |
| `agents/` | security / cost / perf 代理 + `orchestrator.py` 協調決策 |
| `guardian/` | YAML 驗證、安全政策、kubectl dry-run |
| `gitops/` | 寫入 Git、Argo CD 同步、版本回滾 |
| `healer/` | Pod 監控、LLM 根因診斷、自動補救 |
| `observability/` | Prometheus 指標、Grafana Dashboard、告警規則 |
| `training/` | LoRA 微調資料生成、清洗、訓練、評估 |
| `web_demo.py` / `web_demo_new.py` | Web Dashboard（Flask） |
| `0_touch_generate_pods.py` | 零接觸部署 CLI 入口 |

## 工作慣例

- 文件與程式碼註解慣用繁體中文（見 `docs/`、既有註解），除非使用者另外指定。
- `core/config.py` 是路徑設定的單一來源，新增路徑相關設定應加在此處而非散落各檔案。
- 部署流程改動需同時考慮三層防護：`agents/orchestrator`（策略決策）→ `guardian`（格式/安全驗證）→ `gitops`（版本控管），不要繞過任一層。
- 修改 `llama_client.py` 或 `core/model_server.py` 前，先確認 Model Server 是否常駐執行（`curl http://127.0.0.1:8765/health` → `deploy_loaded` / `monitor_loaded`），避免誤判為推論邏輯問題。
- 模型設定在 `core/config.py`：`BASE_MODEL`（部署 3B）、`MONITOR_MODEL`（監控 1.5B）、`MONITOR_DEVICE`（cpu）、`DEPLOY_ADAPTER_PATH`（空=不掛 LoRA）。prompt 一律走 `tokenizer.apply_chat_template`（Qwen ChatML），不要改回 `### User` 純文字格式。
- RAG 索引變更後重跑 `python rag/build_index.py --rebuild --deploy`；`rag/index.json`、`rag/index_meta.json`、`rag/deploy_index.json` 都是可重新產生的產出物。預設 TF-IDF，裝了 chromadb+sentence-transformers 才會用語意向量（embedding 預設跑 CPU，留顯存給 3B）。
- `web_demo_backup*.py` 為歷史備份，非現行程式，修改功能請改動 `web_demo.py` / `web_demo_new.py`。

## 環境與啟動

詳見 [docs/setup.md](docs/setup.md)。快速指令：

```bash
python core/model_server.py       # 啟動常駐 Model Server
python 0_touch_generate_pods.py   # CLI 部署
python web_demo.py                # Web UI（localhost:5000）
```

## 後續路線圖

尚待整合項目見 [docs/roadmap.md](docs/roadmap.md)。

**已確認接上、不是待辦事項**（2026-08-03 追過程式碼呼叫路徑確認，之前這裡寫錯過）：
- RAG 已接進主流程：`llama_client.py` 的 `_try_augment_with_rag(_ex)` 在呼叫 model server 前注入知識庫內容。
- Orchestrator 已接進兩條真實部署路徑，會實際擋下部署：`0_touch_generate_pods.py`（CLI，block 中止 / warn 需手動確認）與 `web_demo.py` 的 `_review_deployment` → `_prepare_deploy`（Web，guardian → orchestrator → dry-run 三層任一 block 就擋下 `/api/deploy/parse`）。

**寫程式前的提醒**：不要只看 `docs/` 底下的文件就假設某個功能還沒做，這份文件之前就是照抄 roadmap.md 的「建議後續工作」清單才寫錯——文件會過時，改動前先去追實際程式碼的呼叫路徑（例如 `grep -rn "from agents.orchestrator"`）確認現況。

## 進行中工作（2026-08-03，使用者之後會接續，先記錄現況）

**這次 session 做完的**：
- **Deploy Console 法庭 UI**：`web_demo.py` 新增 `#court-panel`（三張代理卡 security/cost/perf 依序翻牌 + 判決 banner），`doDeploy()` 改走 `/api/deploy/parse` 先預覽、使用者確認才呼叫 `/api/deploy` 真的部署。後端 `agents/orchestrator.py`、`/api/deploy(/parse)` 路由邏輯完全沒動，純前端功能。`courtRequestId` 遞增比對防止重複送出/舊 setTimeout 蓋掉新畫面。
- **`web_demo.py` 監聽 port 從 5000 改成 5050**（`app.run(..., port=5050)`）——因為使用者本機 Windows 5000 已被別的專案佔用，這是永久性的程式碼改動，不是暫時繞過；`docs/setup.md` 等文件如果還寫 5000 需要一併更新。
- **`/chat` prompt injection 緩解**（`core/model_server.py`）：新增 `STOP_MARKERS` 清單（含 `###User`/`###Assistant` 無空格變體），生成時用 `stop_strings` 攔截 + 事後字串切割雙重防護（同一份清單，兩層有共同盲點）、`no_repeat_ngram_size=6`（原訂 4，因為「命名空間」等中文詞彙剛好 4 token 會被誤傷，改成 6）、`CHAT_SYSTEM` 補強新手友善語氣與「忽略角色重定義企圖」的軟性提醒。**明確定調為緩解不是根治**——全形井號（＃＃＃）、`System:`/`Human:` 這類其他角色標記寫法實測仍會繞過，已知限制記在 `docs/roadmap.md`。
- **RAG 聊天路徑精準度**：`rag/retriever.py` 新增 `CHAT_MIN_SCORE = 0.3`（用 `rag/eval_retrieval.py` 24 筆標註測試集實測校準：正確命中分數 0.505~0.714，先前測到的雜訊文件分數 0.29~0.32，0.3 卡在中間），`llama_client.py` 的 `_try_augment_with_rag_ex` 已接上這個門檻。部署 JSON 路徑（`/infer`）維持原本 `min_score=0.05` 不變。
- **診斷發現（尚未修復，只是查清楚）**：一般聊天（非部署 JSON）品質有系統性問題，跟 prompt injection 無關，乾淨輸入就會重現。用同一套 `CHAT_SYSTEM`/生成參數，只差有沒有合併 LoRA 做了 base-vs-LoRA 對照測試（同一題 RBAC 問題各跑 3 次）：
  - **Base model（不套 LoRA）：3 次全對**，正確講到 `Role`/`ClusterRole`/`RoleBinding`/`ClusterRoleBinding`，無部署語法污染、無 `###` 殘留
  - **LoRA 合併版（現在 Model Server 實際在跑的版本）：3 次全錯**，捏造「json-policy」等不存在概念，還混進 `--image=`/`--port=` 這種部署 JSON 的語法到一般問答裡
  - **結論**：LoRA（為部署 JSON 生成任務微調）合併進同一個模型後，污染了跟部署無關的一般問答能力，且可重現，不是隨機失常。`###` 殘留現象也归因到 LoRA（base model 同樣的防護程式碼沒有出現殘留）。
  - 另外獨立記錄：一般聊天偶爾會幻覈捏造不存在的參考網址（例如 K8s 官網文件連結），這是跟 LoRA 污染不同機制的另一種幻覈，不要混在一起處理。

**2026-08-04 接續完成**：
- **查了 `training/` 微調資料組成**（實際腳本在 repo 根目錄 `merge_and_train.py`，不在 `training/` 資料夾）：`dataset/finetune_samples.jsonl`（部署樣本，約 806 筆）+ `dataset/k8s_qa_samples.jsonl`（一般問答，只有 41 筆）合併成 `dataset/merged_samples.jsonl`（847 筆）餵給 LoRA。**部署樣本佔 95.2%、一般問答只佔 4.8%**，量化確認了污染根因。
- **選了便宜修法並做完**：`core/model_server.py` 的 `/chat` 端點改用 `PeftModel.disable_adapter()`（peft 0.19.1 內建 context manager）暫時停用 LoRA，讓 `/chat` 的生成行為等同純 base model；`_model` 本來就是動態掛載 LoRA（沒呼叫 `merge_and_unload()`），所以這個做法零成本、不多佔 VRAM、不用另外載入模型。`/infer`（部署 JSON 生成）完全不動，繼續用完整 LoRA。
- **一併補了併發鎖**：加了全域 `_generate_lock = threading.Lock()`，`/chat`、`/infer` 的 `generate()` 呼叫都套上，因為 `/chat`、`/infer` 都是同步 `def`（FastAPI 預設丟進 threadpool）、`_model` 是共用物件，沒有鎖的話併發請求有機會讓 `/infer` 被拖進「LoRA 已停用」的狀態，這個風險在改動前的計畫覆核裡被抓出來，已經修掉並用「同時發送 `/chat` + `/infer`」實測驗證過兩邊都正確、沒有互相污染。
- **驗證結果**：RBAC 問題重跑 3 次全對（正確講 `Role`/`ClusterRole`/`RoleBinding`/`ClusterRoleBinding`）；6 題乾淨一般問答內容正確、無部署語法污染；`/infer` 部署解析不受影響；併發測試通過。

**還沒做決定/待接續**：
- 上述 LoRA 污染診斷結果、修復過程、`###` 殘留歸因、幻覈捏造網址，都還沒寫進 `docs/roadmap.md`，之後要補
- **新發現、還沒處理**：`/chat` 有時會把 `[參考知識]` RAG context 原文洩漏進最終回覆（沒有照 `CHAT_SYSTEM` 指示「只當靜默背景，不要引用」），這次驗證測試時觀察到兩次，跟今天的 LoRA 修法無關，是獨立問題，需要另外處理
- prompt injection 的「tokenizer 層級根治」（分段 tokenize 再串接 input_ids，讓使用者輸入不可能產生特殊 token id）已排查可行性但本次未做，記在 `docs/roadmap.md` 技術債

**2026-08-07 接續完成**：
- **翻譯層（先正規化使用者輸入再交回本地模型執行）**：原計畫用 Claude API，實測卡在帳戶額度不足；改用 Gemini API 後一度發現輸出不穩定（常把使用者明明講過的資訊搞丟/截斷，例如「兩份 nginx」正規化後變成 `web service)`）+ 免費層每分鐘只給 5 次請求。
  - **truncation 問題已診斷並修好**：查 `response.candidates[0].finish_reason` 發現是 `MAX_TOKENS`，且 `usage_metadata.thoughts_token_count` 高達 140+——`gemini-flash-latest` 預設有「思考」模式，思考過程的 token 會算進 `max_output_tokens` 裡，原本設的 150 幾乎全被思考吃光，答案從中間被截斷。試過 `thinking_config: {"thinking_budget": 0}` 想直接關閉思考，但這個模型回傳 400 INVALID_ARGUMENT 不接受；改成單純把 `max_output_tokens` 拉高到 1024（`core/gemini_client.py` 兩個函式都改），思考 + 答案都有空間，重跑「缺欄位不能亂猜」跟「資訊完整不能搞丟」兩項關鍵測試都過關（`finish_reason` 正常變成 `STOP`）。另外加了 `_extract_text()` 共用檢查：就算之後又被截斷，直接丟棄結果回傳 `None`（呼叫端 fallback 回原始輸入），不會把破碎句子往下游傳。
  - **免費層每分鐘 5 次請求的限流問題還沒解**，這是額度層級的限制，不是程式邏輯能修的，之後要嘛升級付費方案、要嘛在程式裡加請求間隔/佇列。
  - **目前仍是關閉狀態**（`.env` 的 `USE_LLM_NORMALIZE=0`）——truncation bug 修好了，但限流問題還在，開下去 demo 用還是有機會撞到 429，先不預設開啟，之後要用時手動打開並注意頻率。
  - **另外修好一個句尾標點 bug**：Gemini 正規化常在句尾加句點（"deploy 2 pods of redis."），導致本地 `_deterministic_deploy_parse` 的 image regex 抓不到，誤退回預設值 `nginx:latest`（使用者要 redis 卻被解析成 nginx）。修法：`core/gemini_client.py` 的 `_extract_text()` 去掉句尾標點（根因）+ `llama_client.py` 的 image regex lookahead 也改成對句尾標點寬容（防禦性補強，見 `llama_client.py` 的 `image_patterns`）。
  - **新增 `_chat_needs_normalize()` 呼叫量控制**：聊天路徑原本是每則訊息都打一次 Gemini，現在只有訊息超過 60 字或含 `###`/`System:` 這類可疑格式標記才會送翻譯層，短且乾淨的訊息直接跳過，省額度。
  - **新增 RAG 文件** `rag/k8s_docs/translation_layer_notes.md`：說明翻譯層各種 log 訊息/行為代表什麼（正規化成功/失敗、429 限流、截斷、句點 bug），已重建索引、確認可正確檢索到。
- **修好「部署顯示成功但 Pods/Deployments 沒有實際建立」的 bug**：`web_demo.py` 的 `/api/deploy` 原本用背景執行緒呼叫 `k8s_deploy()`、回傳值完全沒人接，HTTP response 在背景執行緒跑完前就先回了，導致「顯示成功」但實際上 K8s 那端可能還沒建立、甚至已經失敗。改成同步呼叫、把 `(ok, message)` 放進回應（`k8s_deploy` 欄位），前端兩處確認送出的地方（`courtProceedDeploy()`、`confirmDeploy()`）都改成先檢查 `d.k8s_deploy.ok === false`（用 `=== false` 而不是 `!ok`，正確區分「模擬模式沒執行」跟「真的執行過但失敗」）。
  - **改同步之前先補了 timeout，且中途發現 kubernetes python client 預設會重試 3 次**：實測指向一個會被丟包、不主動拒絕連線的位址，光是 `_request_timeout=10` 沒用，因為 `configuration.retries` 預設 `None` 會 fallback 到 urllib3 預設重試，3 次疊加起來要等 **80 秒**才失敗。修法：`k8s_deploy()` 內建一份獨立的 `Configuration`（`retries = 0`，只影響這個函式的 client，不動全域設定），`_request_timeout` 改用 `(連線5秒, 讀取10秒)` tuple，重測降到 10 秒（兩次嘗試 replace→create fallback 各 5 秒連線）。
  - **連動記錄（已寫進 `docs/roadmap.md`）**：這個修復上線後使用者第一次能真的看到失敗訊息、會想重試，`/api/deploy` 沒有 idempotency 保證（重試可能造成重複 GitOps commit）這條舊技術債的觸發機率因此從「幾乎零」變成「使用者的第一直覺反應」，這次沒有一併修，只記錄連動關係。
- **重大基礎設施問題排除：HF 模型快取所在的 NTFS 磁碟局部損毀**：`~/.cache/huggingface` 是符號連結指到 `/mnt/Data/capstone2025/cache/huggingface`（這台機器用 `ntfs3` 掛載一顆 NTFS 磁碟，`/etc/fstab` 裡是永久設定，這顆磁碟 Windows 那邊應該也在用，這是雙系統機器）。`models--meta-llama--Llama-3.1-8B-Instruct` 這個資料夾在 NTFS 層級壞掉，`stat`/`rm`/`umount` 全部回傳 `Invalid argument`，**Linux 的 `ntfs3` 驅動沒有修復能力**（不像 ext4 有 `fsck`），真正的修復工具是 Windows `chkdsk`，需要重開機進 Windows 才能跑，這邊沒有 sudo、也沒有 Windows 存取權限，完全碰不到。
  - **解法：不修，直接繞開**——把 `HF_HOME` 改指到 ext4（本機系統磁碟）上的新路徑 `~/.cache/huggingface_new`，讓模型重新下載一份乾淨的，完全不碰 NTFS 那顆磁碟。
  - **順便根治了一個 import 順序陷阱**：`core/model_server.py` 原本是先 `from transformers import ...`（會連帶 import `huggingface_hub`，這時就已經根據當下環境變數決定好快取路徑常數）才 `from core.config import ...`（這行才會觸發讀 `.env`）——所以原本只把 `HF_HOME` 寫進 `.env` 沒有用。查過 `core/config.py` 只 import `os`，沒有重依賴、也沒有其他模組反過來 import `model_server.py`，確認範圍夠小可以直接修：把 `from core.config import ...` 挪到檔案最前面、`transformers`/`peft` 之前。改完驗證過：完全不帶任何 shell 層級 `HF_HOME`、純靠 `.env`，`huggingface_hub` 的 `HF_HUB_CACHE` 常數跟實際啟動都正確指向新路徑。現在 `CLAUDE.md` 原本寫的 `python core/model_server.py` 這個啟動指令不用加任何東西就能正常運作，是真的根治，不是繞過。
  - 已確認：`ADAPTER_PATH`（LoRA 權重）是專案內的本機路徑（`llama3_k8s_lora_results`），不在 HF 快取裡，完全不受這次事件影響。

**2026-08-07 收工狀態**：Model Server（`model_loaded:true`）、Web Demo、K8s（SSH tunnel 連著）三個都跑著且驗證正常，環境是穩定的，下次可以直接接續，不用重新排查連線問題。

## 進行中工作（2026-09-06：換小模型 + 翻譯層優先 + 雙模型 + 修 OOM）

專案搬到本機 Windows（RTX 3060 Laptop，6GB VRAM），8B 跑不動。計畫檔：`C:\Users\neil-\.claude\plans\inherited-finding-lobster.md`。已做完：

- **模型雙軌**：`core/config.py` 的 `BASE_MODEL` → `Qwen/Qwen2.5-3B-Instruct`（4-bit GPU，約 3GB VRAM），新增 `MONITOR_MODEL`=`Qwen/Qwen2.5-1.5B-Instruct`（CPU）、`DEPLOY_ADAPTER_PATH`（空=不掛 LoRA）。`core/model_server.py` 一個 process 載兩顆、各一把 lock、prompt 改 `apply_chat_template`（ChatML），新增 `/diagnose`，`/health` 回 `deploy_loaded`/`monitor_loaded`。8B LoRA 棄用。
- **翻譯層優先**：`core/gemini_client.py` 改輸出 `### DeploySpec` 區塊 + `GEMINI_API_KEYS` 多 key 逗號分隔輪換（遇 429 換下一把）。`llama_client.ask_llama()` 重排成「Gemini 正規化 → `parse_deploy_spec()` 快路徑（零 GPU）→ deterministic → 小模型 + RAG few-shot」。`.env` 的 `USE_LLM_NORMALIZE=1` 預設開。
- **RAG 部署範例索引**：`rag/build_index.py` 新增 `build_deploy_index()`（讀 `dataset/finetune_samples.jsonl`，1802 筆 → `rag/deploy_index.json` TF-IDF）；`rag/retriever.py` 新增 `retrieve_deploy_examples()`；`rag/vector_store.py` embedding 預設跑 CPU。重建：`python rag/build_index.py --rebuild --deploy`。
- **監控接 healer**：`healer/diagnose.py` `_llm_analyze()` 改呼叫 `llama_client.diagnose_with_llm()`（打 `/diagnose`），規則層順序不變。
- **UI 雙語（選項 B）**：`web_demo.py` 法庭判決 banner / 部署成功失敗訊息、`0_touch_generate_pods.py` 提示，改中英並陳。
- **文件**：`docs/setup.md`、`docs/architecture.md`、`docs/roadmap.md`、`.env.example`、`requirements.txt` 都更新了。
- **prompt injection `### User` 結構性風險順帶消解**（改 ChatML）。

**還沒做決定/待接續**：
- `_chat_needs_normalize()` 已不使用（保留定義未刪）；`_DEPLOY_INTENT_RE` 仍給 `_try_augment_with_rag_ex`（chat 路徑）用
- 使用者要跟隊友借 Gemini key 填進 `GEMINI_API_KEYS`（目前只有 1 把）
- RAG 部署索引是 TF-IDF（keyword-ish），要語意檢索得裝 chromadb+sentence-transformers
- `eval_*.py` 還硬編碼 8B，待遷移
- 舊技術債仍在：`/api/deploy` idempotency、`/chat` 洩漏 `[參考知識]`、`K8S_ENABLED` 啟動時才檢查
