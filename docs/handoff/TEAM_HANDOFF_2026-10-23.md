# 組員接手說明（2026-10-23）

寫給接手的組員，以及組員電腦上的 Claude Code。這份文件交代 10/10 到 10/23 之間新增了什麼、接手要做哪些設定、
改程式時有哪些規則和陷阱，以及還沒做完的事。

交接前已經做過一次全系統檢查，下面是當時的狀態：

| 項目 | 狀態 |
|---|---|
| 測試 | `pytest` 700 項全過；跟 CI 相同的乾淨環境 346 項通過、66 項略過 |
| CI | 最新一次 commit 通過 |
| 套件 | `pip check` 沒有衝突，安裝版本與 `requirements.txt` 完全一致 |
| 服務 | 兩個網站、對外網址、模型伺服器、K8s、Prometheus、Grafana、Flux、Gemini 都正常 |
| 瀏覽器實測 | 六個頁面、一般問答、部署 → 擴縮 → 換映像檔 → 回滾 → 刪除、預約從建立到確認執行、手機監控版五個分頁、客戶叢集模式的註冊與同意書，全部正常 |

---

## 一、接手步驟

1. **更新程式**
   ```powershell
   git pull origin main
   ```
2. **安裝新套件**：這段期間只新增了一個，`pywebpush`（手機推播用）。
   ```powershell
   pip install -r requirements.txt
   ```
3. **`.env` 要不要加設定**（參考 `.env.example`）：
   - 只有你一個網站在用這台電腦的 K8s：**不用加**。
   - 同一台電腦同時開兩個網站（例如正式站加展示站）：要分工，否則兩個網站的 Healer 會同時修同一個 Pod，修復紀錄和通知也會落在不同的網站上。
     - 展示站的 `.env` 加：`HEALER_ONLY_NAMESPACES=user-showcase`
     - 正式站的 `.env` 加：`HEALER_SKIP_NAMESPACES=user-showcase`
4. **跑測試**：`pytest`，要全部通過。
5. **重新啟動服務**：`powershell -ExecutionPolicy Bypass -File scripts\start_services.ps1 -Restart`
   - **不要**把這行指令的輸出接到 `| Out-Null`，會卡住。
   - **不要**用 Claude Code 的背景工作來啟動，對話一結束服務會跟著被關掉。
6. **開機自動啟動**（可選）：`scripts\install_autostart.ps1`。要取消就加上 `-Remove`。

**`data/` 底下的新檔案不要複製到別台電腦**，跟原本的 `users.json`、`clusters.json` 一樣，每台電腦各自一份：

| 檔案 | 內容 | 刪掉會怎樣 |
|---|---|---|
| `data/push.db` | 手機推播的訂閱 | 手機要重新按「開啟通知」 |
| `data/vapid_private.pem` | 推播用的金鑰，第一次有人開啟通知時自動產生 | 所有手機都要重新開啟通知 |
| `data/schedule.db` | 預約與行事曆備註 | 預約和備註會消失 |

---

## 二、這段期間新增的功能

| 功能 | 使用者看到什麼 | 主要程式 |
|---|---|---|
| 匯入 docker-compose 與 Kubernetes YAML | 在對話框貼上或上傳檔案，每個服務都要過跟一句話部署相同的審查 | `agents/compose_import.py`、`agents/k8s_yaml_import.py` |
| 手機 App（PWA）＋推播 | 用手機打開網站並加到主畫面，Healer 處理過問題就會推播通知 | `core/push.py`、`static/push-client.js` |
| 手機監控版 `/m` | 底部五個分頁：總覽、服務、圖表、行事曆、修復紀錄；設定在右上角齒輪。**只能看，不能操作** | `core/mobile_page.py`；`web_demo.py` 的 `_readonly_guard` |
| 行事曆＋預約任務 | 對話裡說「明天早上 9 點部署 2 個 nginx」，或在確認卡上按「改成預約」。**時間到了不會自動執行**，要使用者按「確定執行」 | `core/schedule_time.py`、`core/schedule_store.py`；`web_demo.py` 的 `/api/schedule*`、`/api/calendar*` |
| 分析報告 | 在對話裡說「這個月的分析報告」 | `agents/report.py`、`core/event_store.py` |
| 開機自動啟動 | 電腦重開機後，服務約 20 秒內恢復 | `scripts/start_services.ps1`、`scripts/install_autostart.ps1` |

每一項的細節（為什麼這樣設計、怎麼驗證）寫在 `CHANGELOG.md`。評審可能會追問的風險寫在 `docs/security_review.md` 的 41 到 46 節。

---

## 三、改程式時要遵守的規則（踩過的坑）

1. **手機版的唯讀是由伺服器強制執行的。** 手機登入的 session 只能使用 `web_demo.py` 裡 `_READONLY_API` 白名單上的 API，**新增的 API 預設會被擋**。
   - 新功能要在手機上「看得到」：把那個唯讀的 GET 加進 `_READONLY_API`。
   - 任何會改變狀態的 API 都**不要**加進去。
2. **預約到期不能自動執行。** 這是專案負責人在計畫審核時親自決定的：叢集狀況會變，執行的當下要有人負責。之後改預約功能時，不要把自動執行加回來。
3. **兩個網站共用同一個 K8s 時，要設定 Healer 分工**（見第一部分第 3 步），否則會重複修復，通知也會亂。
4. **部署與回滾寫進 Git 時，一律用 `gitops/manifest_writer.commit_paths()`。** 不要用 GitPython 的 `index.commit()`：它在 Windows 上執行 pre-commit hook 會失敗，10/9 到 10/23 之間本機部署因此完全沒有寫進 Git，而且沒有人發現。
5. **`web_demo.py` 的 `HTML` 會經過 Jinja 處理。** JavaScript 裡不能出現 `{{`、`{%`、`{#`。改完之後，把頁面實際渲染出來，再用 `node --check` 檢查一次。
6. **Healer 判斷「是不是同一個問題」時，不能比對模型寫的文字。** 模型每次的措辭都不一樣。目前的做法是比對「服務＋處理方式」，有實際修改的處理再加上 Pod 名稱。
7. **新的測試檔如果需要 Flask 或 torch，要用 `pytest.importorskip`。** 而且要放在測試函式或 fixture 裡面，不要寫在檔案最上面，否則 CI 會把整個檔案（包括純邏輯的測試）一起略過。
8. **專案慣例**：回覆與文件一律用繁體中文。有實質改動就記進 `CHANGELOG.md`；評審可能會問的風險記進 `docs/security_review.md`。大改動要先出計畫，得到同意才能動手（見 `AGENT_RULES.md`）。

---

## 四、組員負責的功能：分時擴縮（＋休眠）與個人化

推甄報告的 4.13 節把這兩項列為「開發中」，設計內容寫在報告裡。動工前請先跟專案負責人確認以下幾點：

- **分時擴縮本質上是自動執行的**（每天在固定時段調整副本數），而預約任務則是負責人刻意要求「到期由人確認」。兩者的把關方式要先對齊。例如：
  - 是否在第一次套用時讓使用者確認一次，之後就自動執行？
  - 每次自動擴縮時要不要推播？
- **不要再做第二套排程器。** 預約的背景檢查是 `web_demo.py` 的 `_schedule_loop`，每 30 秒一次，存放在 `core/schedule_store.py`。重複性任務可以另外開一張資料表，但建議共用同一個背景迴圈與推播（`core/push.py` 的 `send_async`）。
- **自動擴縮要經過 `/api/scale` 的檢查，並寫進事件紀錄**，分析報告和行事曆才看得到。`/api/scale` 的檢查包括叢集容量（`_check_scale_risk`）與副本上限。
- **休眠（縮成 0）**：目前 `/api/scale` 只接受 1 以上的副本數，要做休眠就要調整這個範圍，並補上測試。
- **手機版**：如果要讓使用者在手機上看到「分時擴縮的設定」，只能開唯讀的 GET（見第三部分第 1 點）。

---

## 五、還沒做完、已知的問題

| 項目 | 說明 |
|---|---|
| 套件安全更新 | `pip-audit` 列出 16 個既有套件有已知漏洞，包括 flask、requests、urllib3、transformers 等。網站已經對外開放，建議盡快升級；這屬於大改動，要先出計畫，升級後要重跑全部測試 |
| iPhone 主畫面 App 的 Google 登入 | 還沒在真的 iPhone 上確認：從主畫面 App 用 Google 登入後，有可能停留在未登入的狀態。真的發生的話，請改用 Safari 分頁登入，或改用帳號密碼 |
| `yamls/deployments/*.yaml` | 每次部署都會寫一份，但不會被提交，久了會累積很多未追蹤的檔案。可以考慮加進 `.gitignore`，或確認這個目錄是否還有用途 |
| 部署規格的錯誤訊息 | 有少數輸入檢查的訊息還是英文，例如「pods must be between 1 and 100」 |
| 口試前的品質驗證 | 重跑 206 題 QA、補外部基準測試、修 `/api/deploy` 重複送出的問題（idempotency） |
| 學校電腦 | `git pull` 後照第一部分做一次；用學校筆電當第二台實體客戶叢集測試；確認 NVIDIA 顯卡可以正常跑 |
