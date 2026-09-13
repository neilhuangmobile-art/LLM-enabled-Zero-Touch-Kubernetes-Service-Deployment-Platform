# 安全與風險審查記錄

給報告/口試用：記錄實測發現的重大風險、影響、解決方式與驗證證據。只收「評審可能會追問」等級的問題，
單一檔案內的小 bug、typo 這種不會列在這裡（那些記在 git commit 訊息跟 `CLAUDE.md` 的工作記錄裡）。

每一條的格式固定：**風險** → **為什麼重要** → **解決方式** → **驗證**。

---

## 1. 認證與授權

### 1.1 三個 API 路由完全沒有登入檢查

**風險**：排查全部 30 個 Flask 路由，發現 `GET /api/status`、`GET /api/dataset/stats`、`POST /api/dataset/run`
沒有 `session` 登入檢查，跟其他路由不一致。

**為什麼重要**：`/api/dataset/run` 是 POST、會觸發 subprocess 執行 `enrich_dataset.py`，可以跑到 600 秒逾時、
會呼叫 model server 的 `/infer`、會寫入資料集檔案。任何連得到這台機器的人（app 監聽 `0.0.0.0`，同網段都連得到）
不需要登入就能觸發**未授權資源耗用**，或造成資料集檔案被覆寫／截斷（這個專案先前就實際發生過一次：
測試時用 `--limit 2` 不小心把 `k8s_en_enriched.jsonl` 從 20000 行截成 2 行）。另外兩個路由是讀取類，
會洩漏 model/K8s 連線狀態、資料集檔案清單與筆數（低風險資訊揭露）。

**解決方式**：三個路由都補上跟其他路由一致的 `if "username" not in session: return jsonify({"error": "Not authenticated"}), 401`。

**驗證**：`curl` 未帶登入 cookie 直接打三個路由，皆回 `401`；帶登入 cookie 的請求維持原本 `200` 正常運作。

---

### 1.2 唯一一個帳號還在用沒加鹽的舊版密碼雜湊

**風險**：`users.json` 裡除了 `admin` 以外的帳號都是 `pbkdf2_sha256$<salt>$<digest>`（加鹽、12 萬次疊代、
constant-time 比對），只有 `admin` 這個帳號還停在舊版無鹽 `sha256(password)`。

**為什麼重要**：無鹽 SHA-256 沒有疊代次數、沒有鹽值，離線暴力破解/查表攻擊的成本遠低於 PBKDF2。
原本的自動升級邏輯只在「整筆帳號記錄是純字串」時才觸發升級，沒有涵蓋「記錄已經是 dict、但裡面的雜湊值
本身還是舊格式」這種情況，所以 `admin` 一直沒被升級到。

**解決方式**：`login()` 改成同時檢查雜湊值是否以 `pbkdf2_sha256$` 開頭，登入成功那一刻密碼明文可用，
順便升級雜湊格式並寫回 `users.json`，不需要使用者自己改密碼。

**驗證**：程式碼審查確認條件判斷正確；下次任何仍在用舊格式雜湊的帳號登入成功時會自動升級（`admin`
帳號密碼未知，無法主動測試登入，但邏輯與其他帳號的既有升級路徑共用同一段程式碼）。

---

### 1.3 已知但故意不動的架構風險（使用者已決定暫緩）

- **`web_demo.py` 監聽 `0.0.0.0`**（不是只有 `127.0.0.1`），同網段的任何裝置都連得到。
- **任何人都能自行註冊帳號**，註冊後沒有角色分層——一般使用者跟管理者權限完全相同，
  能對整個叢集做部署/刪除/scale 等任何操作。
- **沒有 CSRF 防護**。JSON body 的 POST（`/api/deploy`、`/api/scale` 等）因為要求
  `Content-Type: application/json`，一般的跨站表單提交沒辦法直接偽造，天然有一定防護；
  但 `/auth/login`、`/auth/logout`、`/auth/register` 用傳統表單編碼，理論上仍可能被 login CSRF
  這類手法影響（相對低影響）。

這三項組合起來的實際意義：只要能連到這台機器的網路，註冊一個帳號就能拿到完整叢集控制權。
是否要收斂（例如改回只聽 `127.0.0.1`、加邀請碼、加角色分層）取決於這個系統的使用情境
（單機 demo vs. 開放給多人在同網段連線），目前決定先不動，記錄在此供之後決策參考。

---

## 2. 機密資訊管理

**風險**：這次對話過程中，`.env` 檔的實際內容（含真實的 `ANTHROPIC_API_KEY`、`GEMINI_API_KEYS`、
`HF_TOKEN`）曾經被讀取並顯示在對話紀錄裡。

**為什麼重要**：確認過 `.env` 從未被 commit 進 git 歷史（`git log --all --full-history -- .env` 無結果），
所以不是「進版控外流」的問題；但這些金鑰的明文值已經在對話 session 的紀錄中出現過，如果對話紀錄
被匯出或分享，金鑰就等於外流。

**解決方式**：這是「使用者要不要去後台撤銷重發」的操作，AI 助理沒辦法代替使用者執行。
已建議使用者到 Anthropic Console / Google AI Studio / Hugging Face 撤銷重發這幾把金鑰，此項目前
**待使用者執行**，不是程式碼修改。

**驗證**：掃過所有 git 歷史（`git log --all -p -- '*.env' '.env.example'`）確認沒有真實金鑰字串被 commit 過，
只有目前 `.env.example` 裡的預留位置字串（`sk-ant-xxx...`、`hf_xxx...`）。

---

## 3. 部署安全與資源管理

### 3.1 部署出去的 Pod 過去預設沒有資源上限、沒有安全硬化

**風險**：使用者部署時沒指定 memory/cpu，`k8s_deploy()` 過去完全不設 `resources.requests/limits`，
容器也沒有任何 `securityContext` 設定。

**為什麼重要**：沒有資源上限的容器有機會把整個節點的資源吃光，影響同節點上其他 Pod（noisy neighbor）；
沒有基本安全硬化則放大了容器逃逸/權限提升類攻擊的影響面（雖然目前的部署規格 schema 本身沒有欄位能設
`privileged`/`hostNetwork` 這類危險設定，`agents/security_agent.py` 對這些的檢查目前用不太到，但資源限制
跟基本硬化仍是縮小攻擊面/降低誤用影響的基本盤）。

**解決方式**：
- 使用者沒填的維度，套用 `agents/cost_agent.py` 既有的「依 image 類型推薦資源」profile
  （`_detect_app_type` + `_APP_PROFILES`），跟審查卡上顯示給使用者看的建議數字是同一份資料，
  不會兩邊不一致。使用者有指定的維度維持使用者指定的值。
- 新增 `securityContext.allowPrivilegeEscalation: false`。刻意**沒有**強制 `runAsNonRoot` 或丟棄
  capabilities，因為這個平台常見的 demo image（nginx 綁 80 port 等）預設用 root 執行，硬上非 root
  會讓現有部署流程直接壞掉——這是「多一層防護」跟「不要為了防護打斷核心功能」之間刻意做的取捨。

**驗證**：部署一個沒給 memory/cpu 的 nginx，確認 `kubectl` 顯示套用了
`requests: cpu=100m,memory=128Mi`、`limits: cpu=500m,memory=256Mi`、`allowPrivilegeEscalation: false`，
Pod 正常 `1/1 Running`，沒有因為新加的安全設定而部署失敗。

---

### 3.2 單一 Pod 資源需求超出叢集節點容量時，系統會「回報成功但實際上永遠不會動」

**風險**：部署一個要求 64Gi 記憶體 / 32 CPU 的 Pod（測試機器單節點只有 ~15.7Gi / 20 核），
`/api/deploy` 回應 `k8s_deploy.ok = true`、訊息顯示「已部署成功」。

**為什麼重要**：這是一個典型的「靜默失敗」——API 回報成功，但實際上 `kubectl describe pod` 顯示 Pod
卡在 `Pending`，事件是 `FailedScheduling: 0/1 nodes are available: 1 Insufficient cpu, 1 Insufficient memory`。
不論等多久、不論建立幾個副本，這個 Pod **永遠不會變成 Running**，因為單一 Pod 一定要完整塞進某一個
節點才會被排程，跟「總共有幾個節點」無關。使用者只看部署後的成功訊息，完全看不出東西壞了，
除非另外去查 Pod 細節。這違反本專案「不能有靜默失敗」的核心設計原則。

另一個連帶發現：舊的節點數估算（`node_estimate`）用 `core/config.py` 裡一個固定假設值
（4 CPU / 8Gi）計算，跟叢集真正的節點容量沒有關係，估算結果不準（例如低估或高估需要幾個節點）。

**解決方式**：
- 新增 `k8s_get_node_capacity()`：真的查詢叢集目前所有節點的 allocatable 容量，取最大的那個節點，
  取代原本寫死的假設值。
- 在部署前的審查步驟新增一項判斷：如果單一 Pod 的資源需求本身就超過叢集裡最大節點的容量
  （不管是 CPU 或記憶體任一個維度），直接把這次部署判定為 `block`（阻擋），並在訊息裡列出
  實際數字（要求多少 vs. 節點實際有多少），講清楚「這不是需要更多節點的問題，是單一 Pod
  塞不進任何一個節點」，不是等它卡在 Pending 之後才讓使用者自己發現。

**驗證**：測了三種情境確認邏輯正確：
1. 64Gi/32核（超出容量）→ 正確 `block`，訊息附上實際數字
2. 256Mi/500m（正常大小）→ 正確 `warn`，沒有被誤擋
3. 50 副本 × 256Mi/200m（單個 Pod 沒超、加總起來量大）→ 正確 `warn` 不誤擋，且用真實節點容量
   算出來只需要 1 個節點（用舊的固定假設值會誤判成需要 3 個節點）

---

## 4. LLM 幻覺與可靠性（Chat 問答路徑）

系統性測了約 25 種情境（不存在的服務、假造的 K8s 概念、識別碼保真、文件連結、意圖分類、
根因診斷、部署解析），找到兩個可重現、有實際影響的幻覺模式並修好；其餘多數情境本身沒有幻覺，
或風險等級較低（例如根因診斷在證據極少時選了稍微不理想但無害的 action，屬於品質問題不是幻覈）。

### 4.1 問一個不存在的服務健康狀況時，模型會用類比推理硬掰答案

**風險**：問「`ghost-service-xyz999` 這個服務健康嗎？」（此服務根本不存在），模型回答：
「如果 ghost-service-xyz999 存在，並且它屬於 auto-app、my-cache 和 zt-smoke 這些服務都健康的範圍內，
那麼可以推斷 ghost-service-xyz999 也應該是健康的。」

**為什麼重要**：這是把「旁邊真實存在、真實健康的服務」拿來做類比，幫一個不存在的東西編出
「應該也健康」的結論——如果使用者真的照這個答案判斷生產環境的服務狀態，會做出錯誤決策。
矛盾的是：系統原本設計「把即時叢集狀態 `[現況]` 塞給模型當接地資料」是為了減少幻覈，
但實測發現**有接地資料反而讓模型更容易幻覈**，因為模型看到旁邊一堆真實數據就想「套用規律」；
完全沒有接地資料時（例如問法沒有觸發即時狀態注入），模型反而會老實回答「不確定」。

**解決方式**：在 `core/model_server.py` 的 `CHAT_SYSTEM` 系統提示裡明確加入規則：
`[現況]` 區塊是**完整清單**，沒有列出的東西就是**不存在**（不是「不確定」），禁止用類比推斷
不在清單裡的實體的狀態。

**驗證**：修好後重測 3 次，穩定回答「不在列表中」「無法確認」等誠實答案，沒有再出現套用其他
服務健康狀況去推斷不存在服務的情況。

### 4.2 服務名稱含英文字時會被模型當一般單字翻譯

**風險**：問「`my-cache` 有幾個 pod 在跑？」，模型回答把識別碼講成「**我的-cache** 這個 Deployment
當前有 2 個 Pod 正在執行」——`my` 被當成一般英文字翻譯成「我的」。3 次測試皆重現，跟系統有沒有裝
OpenCC 無關（測試機器本身沒裝這個套件），是模型生成時自己做的。

**為什麼重要**：如果使用者的服務名稱剛好用到常見英文單字，回覆會出現使用者看不懂、甚至誤以為
系統在講別的東西的變形名稱，影響對話可信度與可讀性。

**解決方式**：`CHAT_SYSTEM` 加入規則：Pod/Deployment/Service 名稱是**識別碼**，不是要翻譯的詞，
即使含英文單字也要逐字保留，不可翻譯、改寫或意譯。

**驗證**：修好後重測 2 次，皆正確保留 `my-cache` 原樣輸出。

### 4.3 一個修法上的教訓：prompt 規則不是加越多越好

修復上述兩項時，另外嘗試加了第三條規則（「優先建議使用本系統的 Chat 指令，不要教使用者打
kubectl」），結果這條規則**稀釋了前兩條的效果**——模型又開始亂編 kubectl 指令，甚至編出一個
不存在的 `--namespace=ml`。已確認並移除這條規則，只保留前兩條有實測效果的規則。這是系統提示
工程上具體遇到的「規則互相干擾」案例，寫下來供之後調整 prompt 時參考：**每加一條規則都要重新
完整測過受影響的既有案例，不能假設新規則只會加分不會扣分**。

---

## 5. 依賴與供應鏈

**風險**：`requirements.txt` 原本全部用 `>=` 下限、沒有上限，例如 `torch>=2.1.0`、`flask>=3.0.0`。

**為什麼重要**：沒有上限代表任何時候重新安裝依賴，都可能被裝到一個從未測試過的新大版本，
可能默默改變行為、甚至引入未知風險，而且完全無法重現「當初測試時用的是哪個版本」。

**解決方式**：全部改成 `==` 釘死到這台機器目前實際安裝、驗證跑得動的版本（不是憑空猜測的數字）。
`torch` 拿掉了 CUDA build tag（`+cu121`），因為一般 PyPI 沒有這個 local version identifier，
照原樣寫在 `requirements.txt` 裡會讓在別的機器上 `pip install` 直接失敗。

**驗證**：逐一用 `importlib.metadata.version()` 查出目前環境實際安裝的版本號再寫入，不是憑空指定。
**這不是「已掃過 CVE、確認沒有已知漏洞」**——這個環境沒有即時的 CVE 資料庫可查，建議之後找機會
跑一次 `pip-audit` 或 `safety` 之類的工具做實際漏洞掃描。

---

## 6. 自癒／監控系統：宣稱的能力跟實際行為有落差

### 6.1 沒有背景常駐監控，Web UI 的「Fix」其實只是刪 Pod，沒有真正診斷根因

**風險**：`CLAUDE.md` 資料流圖畫的鏈路是 `healer/pod_watcher + diagnose（規則層 →
Qwen2.5-1.5B）+ remediate`，但實測發現：
1. 機器上只有 `model_server.py`、`web_demo.py` 兩個 process 在跑，`pod_watcher.py` 的
   `watch_forever()`（持續每 30 秒掃描）從未被啟動，`/api/healer/scan` 呼叫 `scan_once()`
   也沒帶 `auto_heal=True`，等於「只偵測、不會自動處理」。
2. Web UI 的「Fix」/「Auto Fix All」按鈕打的 `/api/healer/fix`、`/api/healer/auto_fix`，
   是完全獨立寫的一套邏輯：只要 Pod 狀態在 `{CrashLoopBackOff, OOMKilled, ImagePullBackOff,
   ErrImagePull, Error}` 裡就直接 `delete_namespaced_pod`，**完全沒有呼叫**
   `healer/diagnose.py`、`healer/remediate.py`。

**為什麼重要**：「零接觸自癒」是本專案核心宣稱之一，但實際落地的是「使用者手動按鈕 →
盲目刪 Pod」，不是「系統自動偵測 → 根因分析 → 對應動作」。如果根因不是暫時性的（例如
image tag 打錯、記憶體給太少這種設定性錯誤），刪除 Pod 後 ReplicaSet 重建出來的新 Pod
會用同一份錯誤設定，**再壞一次**，使用者會看到「按了 Fix 但問題沒解決、還一直跳出來」，
這是評審最容易當場問「那你這個自癒到底做了什麼」的落差點。

**解決方式**：
- `web_demo.py` 新增 `_real_heal_pod(pod_name, namespace)`：組裝跟 `pod_watcher._trigger_heal()`
  相同的 context（pod 狀態＋日誌＋事件）→ `healer.diagnose.diagnose_issue()`（規則層優先，
  規則沒中才用 Qwen2.5-1.5B）→ `healer.remediate.remediate()`（依 action 分派：OOMKilled 
  調高記憶體 limit、ImagePullBackOff 嘗試 `kubectl rollout undo`、CrashLoopBackOff 印出崩潰前
  日誌再重建、探針失敗調大 `initialDelaySeconds` 等，只有真的無法自動處理的才回退到
  「列出診斷結果供人工排查」）。`/api/healer/fix`、`/api/healer/auto_fix` 改呼叫這個函式，
  回應附上 `root_cause`／實際執行的 `action`，不再是單純的「已刪除」。
- 新增背景常駐執行緒 `_healer_background_loop()`，`web_demo.py` 啟動時（K8s 已連線才啟動）
  自動開始，每 30 秒掃描一次，偵測到新的異常 Pod 就自動跑上述診斷＋補救鏈路，不需要使用者
  手動觸發或另開 terminal 跑 `python healer/pod_watcher.py --watch`。同一個 (namespace, pod,
  reason) 問題不會重複觸發（用 `seen` 集合去重，問題消失後才清除，避免無限重試同一個
  已知會失敗的補救）。
- Healer 頁面新增即時橫幅（綠色「自動監控中」／黃色「未啟動」，符合「不能靜默、要講現況」
  的新手友善原則），以及「自動修復紀錄」列表，讓使用者看得到系統背景到底做了什麼、對哪個
  Pod、判斷根因是什麼、採取了什麼動作。

**驗證**：故意建立一個 image tag 打錯的 Deployment（`nginx:this-tag-does-not-exist-xyz`），
確認：(1) Pod 進入 `ImagePullBackOff`；(2) 背景迴圈在下一次 30 秒 tick 內偵測到，規則層正確
判定根因為「映像拉取失敗」、action 為 `fix_image`，嘗試 `kubectl rollout undo`（因為這個
Deployment 從建立起就沒有更早的正常 revision，回滾本身會回報失敗，但這正是誠實反映
「這個問題目前無法自動修復、需要人工介入」，不是靜默假裝成功）；(3) `/api/healer/status`
可以看到這筆記錄，`root_cause`／`action`／`ok` 欄位都正確填入。

### 6.2 `scale` 只檢查單一 Deployment 的副本數上限，沒檢查會不會把整個叢集資源撐爆

**風險**：`/api/scale` 原本只驗證 `1 <= replicas <= 100`，沒有檢查「把這個 Deployment
擴大到 N 個副本之後，加上叢集裡其他所有 Deployment 的資源需求，總量會不會超出節點容量」。

**為什麼重要**：跟 3.2 節「單一 Pod 過大」是不同層次的風險——這裡是「每個 Pod 本身都不大，
但疊加起來的總量超過節點能放的量」。使用者只是把某個服務從 3 個擴到 30 個，操作本身不會
報錯，但多出來排不進去的 Pod 會卡在 `Pending`，跟 3.2 節一樣是「看起來成功、實際上沒用」
的靜默失敗，而且 scale 是使用者最容易「手滑打錯一個數字」的操作。

**解決方式**：新增 `_check_scale_risk(name, new_replicas)`：查詢叢集最大節點的 allocatable
容量，加總「套用這次變更後」所有 Deployment 的資源請求（cpu request × replicas 加總），
超出節點容量就直接擋下這次 scale（回 409），訊息裡列出「這樣做需要多少 vs. 節點只有多少」
的具體數字，並說明是「多出來的 Pod 會卡在 Pending、不會顯示錯誤」，而不是等使用者自己發現。
查不到節點容量或部署本身沒設資源請求時直接放行（不誤判——這是操作前的提醒機制，不是唯一
的安全網，寧可少擋不要錯擋）。

**驗證**：程式碼審查確認邏輯（沿用 3.2 節已驗證過的 `_parse_cpu_millicores`／
`_parse_memory_bytes`／`k8s_get_node_capacity` 這套已測過的工具函式，只是套用場景從
「單一 Pod」換成「整叢集加總」）；尚待用真實會超出節點容量的 scale 請求做端到端測試
（目前叢集內現有 Deployment 資源需求都不大，難以自然觸發，需要之後刻意建構測試情境）。

---

## 7. 沒有自動化測試／CI，改動無法快速驗證有沒有壞掉

**風險**：整個專案原本沒有 `tests/` 目錄、沒有 pytest、沒有 CI。每次改動（包含這次 session 改的
healer/scale 風險檢查）都只能靠手動 curl、手動建測試 pod、肉眼比對輸出，沒辦法在幾秒內確認
「這次改動有沒有讓別的功能壞掉」。

**為什麼重要**：這個專案的核心價值在於「多代理審核會不會正確 block/warn/approve」「Pod 壞了會不會
被正確判斷根因」，這些都是純邏輯計算（`agents/cost_agent.py`、`agents/security_agent.py`、
`agents/orchestrator.py`、`guardian/yaml_validator.py`、`healer/diagnose.py` 的規則層），完全不需要
真實 K8s/GPU 就能測，卻一直沒有自動化覆蓋——代表每次改 prompt、改規則、改門檻值，都只能憑印象
判斷有沒有影響到其他情境，跟這次 session 修 hallucination 時「多加一條規則、意外讓已修好的案例
退步」是同一種風險（見 4.3 節），差別是那次是 LLM prompt、這次會是純邏輯層。

**解決方式**：
- 新增 `tests/`，針對上述純邏輯模組寫了 60 個測試案例，涵蓋：單位轉換（`_parse_memory_bytes`／
  `_parse_cpu_millicores`）、node 數量估算（含「50 副本疊加不能算錯」這種之前真的抓到過的 bug 情境）、
  安全掃描的 8 條規則（privileged/hostNetwork/hostPID 等 critical 判定）、orchestrator 的最終決策
  邏輯（approve/warn/block，含「單副本高可用警告」「缺健康探針警告」這些容易被忽略的真實規則）、
  healer 規則層根因判斷（OOMKilled/ImagePullBackOff/CrashLoopBackOff 等 8 種模式）、remediate 的
  dry-run 分派邏輯與工具函式（`_infer_deployment_name`、`_double_memory`）。
- 新增 `.github/workflows/test.yml`：push/PR 時自動跑 `python -m compileall`（全專案語法檢查）
  + `pytest -m "not integration"`。故意不裝 `torch`/`transformers`/`bitsandbytes`（CI runner 沒
  GPU），只裝 `pytest`+`pyyaml`，讓 CI 快、穩、不受本地 GPU 環境影響。
- 新增 `requirements-dev.txt`（只放 `pytest`），跟正式依賴的 `requirements.txt` 分開，不影響
  上次剛釘死的版本號。

**驗證**：60 個測試全部通過（`pytest tests/ -v` → `60 passed`）；過程中這套測試**真的抓到兩個
我自己寫測試時對系統行為的錯誤假設**（不是程式碼 bug，是我以為「乾淨的部署」會被 approve，
實際是 perf_agent 的「單副本無高可用」「缺健康探針」兩條 high severity 規則會讓它變成
warn——這正是自動化測試的價值：把「隱性的系統行為」變成「寫下來、可驗證的規格」，下次有人
改動 `PERF_HIGH_ISSUE_WARN_LIMIT` 之類的門檻值，測試會立刻告訴他影響了什麼）；`python -m
compileall -q .` 對整個專案跑過確認語法零錯誤。

**尚未覆蓋（誠實記錄，不要在報告裡假裝已經完整）**：`web_demo.py` 本身（Flask 路由、多步部署
確認流程、healer 背景自動修復迴圈）、`core/model_server.py`、`llama_client.py` 這些需要真實
K8s 連線或 model server 常駐的路徑，目前完全沒有自動化測試，只能靠手動驗證（這次 session 驗證
healer 自動修復、`/api/scale` 風險檢查都是手動建測試 pod/呼叫 API 完成的）。這些之後可以用
`unittest.mock` 假造 `kubernetes.client` 的回應來測，標記 `@pytest.mark.integration`，是明確的
下一步工作，不是「做不到」。

---

## 尚待排查（可作為報告中「未來工作」的項目）

- K8s ServiceAccount 實際權限範圍（目前用 Docker Desktop 的 kubeconfig，很可能是 cluster-admin
  等級，沒有做 RBAC 範圍限制，配合 1.3 節「任何註冊使用者都有完整權限」一起看是比較大的風險）
- `agents/guardian` 其他規則的覆蓋度（目前只抽查過 `security_agent.py` 的幾條規則）
- prompt injection 現況重新驗證（`CLAUDE.md` 歷史記錄過部分已知殘留漏洞，換模型後沒有重新測試）
- `healer/remediate.py` 的 `fix_image`（`kubectl rollout undo`）在「這個 Deployment 從建立起
  就沒有正常過的 revision」時必然失敗，這種情況下比較合理的動作其實是「提示使用者這個
  image tag 本身打錯，需要人工改正確的 tag」而不是嘗試回滾；目前 6.1 節的修復讓這種情況
  誠實回報失敗，但還沒有針對「從未成功過」這個特例給更精準的建議文字，可以之後補強。
