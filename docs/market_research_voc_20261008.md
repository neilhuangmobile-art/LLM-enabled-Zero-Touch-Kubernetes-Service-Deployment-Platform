# 市場需求報告（使用者原話版）：K8s 部署與維運的真實痛點

**調查日期**：2026-10-08
**方法**：`voice-of-customer-miner`（deanpeters/Product-Manager-Skills）流程，再用 Jobs-to-be-Done
與機會解法樹（Opportunity Solution Tree）整理。這份報告**獨立於先前的調查**，只用這次新抓到的
原始資料推導，**主要證據是使用者自己寫的話**，不是廠商報告。

**資料**：205 則原文，存在 [`docs/research/voc_raw_20261008.jsonl`](research/voc_raw_20261008.jsonl)，
抓取腳本是 [`docs/research/voc_mine_20261008.py`](research/voc_mine_20261008.py)：
- Hacker News 留言 172 則（2024-01-01 之後，透過 Algolia API 取得原文）
- Stack Overflow `kubernetes` 標籤問題 31 題（依票數排序，透過 StackExchange API 取得原文）
- K8sGPT 的 GitHub issue（透過 `gh` 取得原文）

**標示規則**：引號內一律是原文節錄（英文保留原文，連原作者的錯字也保留），附網址。
**事實／推論／假設**分開標示。

---

# Voice-of-Customer Snapshot

## 1. Scope
**調查對象**：K8s 使用者（新手、小團隊、維運人員），K8s 上的 AI 工具（K8sGPT 等），
以及「把 K8s 包成 PaaS」的工具（Canine、Coolify）
**支援的決策**：這個平台接下來該優先解決哪些需求、該怎麼定位
**資料來源**：Hacker News、Stack Overflow、GitHub issue
**截至日期**：2026-10-08

**各來源的偏差（必讀）**：
- **Hacker News**：偏向資深工程師，對 K8s 和 AI 都有不少懷疑論者。好處是講話直接，缺點是**低估新手的痛**。
- **Stack Overflow**：最能代表新手卡在哪裡，但高票問題大多是 **2015–2022 年**的舊問題，瀏覽數是多年累積的。代表「這是長期存在的痛點」，**不代表今年的比例**。
- **GitHub issue**：只有真的在用某個工具的人才會發，偏向進階使用者。
- **沒有抓到的**：Reddit（API 回 403，沒辦法拿到可以逐字核對的原文，所以**整份報告沒有引用任何 Reddit 句子**）、中文社群（PTT、iThome）、評論網站（G2 等）。
- **樣本是依查詢關鍵字抓的**：各主題的則數反映的是「我查了什麼」，**不能拿來比較哪個痛點比較多人在意**。量化指標請看 Stack Overflow 的瀏覽數。

## 2. Need Themes

### 主題 A：「看懂我的程式為什麼一直重啟」
- **頻率**：**跨來源反覆出現**。光是 Stack Overflow 抓到的 8 題 CrashLoopBackOff 問題，累計瀏覽就有 **1,256,258 次**（2015–2022 年的問題）。
- **原文**："My kubernetes pods keep crashing with "CrashLoopBackOff" but I can't find any log" — Stack Overflow 問題標題，**瀏覽 456,675 次**，[連結](https://stackoverflow.com/questions/41604499/my-kubernetes-pods-keep-crashing-with-crashloopbackoff-but-i-cant-find-any-lo)
- **原文**："Any idea to view the log files of a crashed pod in kubernetes?" — Stack Overflow，瀏覽 179,874 次，[連結](https://stackoverflow.com/questions/34084689/view-log-files-of-crashed-pods-in-kubernetes)
- **原文**："I am a beginner in kubernetes and was trying to deploy my flask application following this guide" — Stack Overflow（Flask app 一部署就 CrashLoopBackOff），[連結](https://stackoverflow.com/questions/72317854/crashloopbackoff-back-off-restarting-failed-container-for-flask-application)
- **原文**："Some days, it would be better to build a working not-kubernetes than debugging the not-working kubernetes." — HN，2026-04-26，[連結](https://news.ycombinator.com/item?id=47912572)
- **原文**（工具作者的觀察）："many Kubernetes CrashLoopBackOff issues are detectable with simple log patterns" — HN，2026-03-14，[連結](https://news.ycombinator.com/item?id=47375618)
- **誰在說**：新手跟著教學部署第一個 app 時最常卡在這裡（**推論**，依問題內容判斷）
- **解讀**：**推論**。新手卡住的往往不是「修不好」，而是**連去哪裡看原因都不知道**。而且常見原因用規則就抓得到，不一定需要大模型。

### 主題 B：「不知道要給多少資源，才不會被系統殺掉」
- **頻率**：**跨來源反覆出現**。Stack Overflow 上 OOMKilled 相關的 8 題累計瀏覽 **200,760 次**；「資源大小怎麼設」7 題累計 25,948 次。
- **原文**："How do I determine the resource requests and limits that I should set for my Pod? I tried leaving resources unconfigured, but find that my pods are frequently evicted." — Stack Overflow，[連結](https://stackoverflow.com/questions/57229853/how-do-i-determine-resource-requests-and-limits-for-my-kubernetes-deployments)
- **原文**："We're sort of new to the whole Kubernetes world" ——接著問為什麼程式明明沒到上限卻被 OOMKilled，瀏覽 19,112 次，[連結](https://stackoverflow.com/questions/58749290/process-inside-pod-is-oomkilled-even-though-pod-limits-not-reached)
- **原文**："Not all service owners possess expert knowledge of Kubernetes." — HN（Mercari 的平台團隊），2024-03-21，[連結](https://news.ycombinator.com/item?id=39777008)
- **反方原文**（專家認為常見的「浪費」算法有問題）："Memory "requests" are hints to the kube-scheduler for placement, not a target for expected usage." — HN，2025-12-13，[連結](https://news.ycombinator.com/item?id=46257467)
- **誰在說**：新手，以及大公司裡不是 K8s 專家的服務負責人（**事實**，依原文）
- **解讀**：**推論**。數字設太低會被殺掉，設太高就浪費錢，而**新手在部署當下沒有資料可以參考**。另外，專家的反方意見提醒：「用量低於申請量就算浪費」這種簡化說法會被內行人挑戰，推算邏輯要講得夠精確。

### 主題 C：「部署一個 app，不用先學會 K8s」
- **頻率**：**跨來源反覆出現**（HN 的多個討論串，時間橫跨 2024 到 2026 年）
- **原文**："If I need to know what Kubernetes is, Helm charts and whatnot, it's not really a Heroku alternative for me ." — HN，2025-06-17，[連結](https://news.ycombinator.com/item?id=44300587)
- **原文**："writing Kubernetes YAML by hand every time you want to run a container stack is a barrier that shouldn't exist." — HN，2026-03-24，[連結](https://news.ycombinator.com/item?id=47500163)
- **原文**："I _still_ think Kubernetes is insanely complex, despite all that it does." 以及 "I'd like to see more emphasis on UX for v2 for the most common operations, like deploying an app and exposing it" — HN，2025-06-19，[連結](https://news.ycombinator.com/item?id=44321088)
- **原文**："The docs for K8s are incredibly bad for solo devs or small teams" — HN，2024-11-25，[連結](https://news.ycombinator.com/item?id=42232476)
- **原文**："users who lack the relatively trivial skill to convert a Compose definition into a set of Kubernetes resources." — HN，2025-02-23，[連結](https://news.ycombinator.com/item?id=43145578)
- **誰在說**：獨立開發者、小團隊（**推論**）
- **解讀**：**推論**。大家最想簡化的是**最常做的那幾件事**：部署一個 app、讓外面連得到。另外，「手上已經有 docker-compose 檔，卻不會轉成 K8s」是一個具體、很常見的卡關點。

### 主題 D：「看得懂錢花到哪裡，不要突然被加價」
- **頻率**：**主要集中在 HN**，但出現在多個不同的討論串
- **原文**："New public IP charges and Kubernetes base pricing both doubled my bill. Spot pricing is kind of a black box, and it also makes no sense." — HN，2024-09-17，[連結](https://news.ycombinator.com/item?id=41571574)
- **原文**："I suspect a big chunk of this is simply visibility" — HN，2024-03-04，[連結](https://news.ycombinator.com/item?id=39590720)
- **原文**："The current solutions out there are too expensive and not self-serve." — HN（講的是 K8s 成本監控工具），2025-02-24，[連結](https://news.ycombinator.com/item?id=43158266)
- **反方原文**：從 Heroku 搬到 DigitalOcean 的託管 K8s 後 "saved about 75%" — HN，2024-12-02，[連結](https://news.ycombinator.com/item?id=42293057)
- **解讀**：**推論**。問題不只是「貴」，而是**看不懂、無法預測**。K8s 不一定比較貴，有人搬過去反而大省；真正的痛點是成本不透明。

### 主題 E：「讓 AI 幫忙，但我要看得懂、管得住」
- **頻率**：**跨來源反覆出現**（HN 留言和 GitHub issue 都有）
- **原文**："it runs 80%+ of my kubectl commands. It’s the most steroidal auto-complete I’ve tried. But I do get dumber for it." 接著寫 "make it my duty to own every change" — HN，2026-06-16，[連結](https://news.ycombinator.com/item?id=48556108)
- **原文**："before I turn some LLM loose on my cluster, I need to know what it supports, how it supports it and how I can integrate into my workflow." — HN（Azure K8s 維運人員），2024-08-26，[連結](https://news.ycombinator.com/item?id=41358560)
- **原文**："having ChatGPT go line by line through all of the YAML files generated for a simple web app" 之後 "I realized that Kubernetes basically takes a set of instructions on how to run your app and then follows them." — HN，2025-02-15，[連結](https://news.ycombinator.com/item?id=43063451)
- **原文**："After 30 minutes of prompts and nudges, during which it went on multiple wild goose chases, I just gave up and did it myself." — HN（用 AI 處理 K8s YAML 驗證失敗），2026-07-03，[連結](https://news.ycombinator.com/item?id=48775710)
- **原文**："I would not like to disclose any personal info to GTP/OpenAI." — K8sGPT issue #139，[連結](https://github.com/k8sgpt-ai/k8sgpt/issues/139)
- **解讀**：**推論**。使用者想要的不是「AI 全部包辦」，而是：
  - AI 動手之前，我看得到它要做什麼
  - 做完之後我能學到東西，不會越用越笨
  - 資料不要外流
  
  AI 一直繞圈子、做不好的經驗，也會快速消磨信任。

## 3. Competitor Weak Points
- **K8sGPT**：輸出是沒有結構的文字，無法直接轉成動作。原文："K8sGPT currently sends raw Kubernetes failures to a generative LLM and gets back unstructured English text"（[#1813](https://github.com/k8sgpt-ai/k8sgpt/issues/1813)，2026-09-30 的設計討論，正在補上「確定性的動作分類」這一層）。**事實**：這是社群貢獻者提的設計，還沒合併進主線。
- **K8sGPT**：資料隱私和本地模型的需求早就出現（[#139](https://github.com/k8sgpt-ai/k8sgpt/issues/139)；[#188](https://github.com/k8sgpt-ai/k8sgpt/issues/188) 要求支援本地模型，15 則留言）。後來雖然支援了本地後端，仍有「operator is not working with localai」這類回報（[#1524](https://github.com/k8sgpt-ai/k8sgpt/issues/1524)）。
- **K8sGPT**：使用者質疑它跟雲端廠商的 AI 助手有什麼差別："How its better than AI assistants provided by cloud providers"（[#1573](https://github.com/k8sgpt-ai/k8sgpt/issues/1573)）。
- **Canine**（把 K8s 包成類似 Heroku 的平台）："The docs seem to be non existent."（[HN](https://news.ycombinator.com/item?id=46977745)，2026-02-11），而且使用者仍然覺得要懂 K8s 和 Helm（見主題 C 的第一則原文，回應的就是 Canine）。
- **Coolify**（單機版的自架 PaaS）："I don't believe it supports HA deployments of Postgres with automated failover"（[HN](https://news.ycombinator.com/item?id=41489580)，2024-09-09）。Canine 的作者也說："I think the single VPS model is just too hard to get working right at that scale."（[HN](https://news.ycombinator.com/item?id=46978376)）

## 4. Switching Triggers（什麼讓人換工具）
- **離開 Heroku 的原因是價格**："getting price gouged by Heroku"（[HN](https://news.ycombinator.com/item?id=47304015)，Canine 作者談上一間新創的經驗）——**事實**
- **離開 K8s 的原因是複雜和成本**："I also recently left Kubernetes. It was a huge waste of time and money."（[HN](https://news.ycombinator.com/item?id=42042366)，改用 Cloud Run）——**事實**
- **被簡單吸引**：在單一機器上，"Coolify/Dokploy are so much simpler in that regards."（[HN](https://news.ycombinator.com/item?id=47296579)）——**事實**
- **搬家成本是阻力**："I wish _I_ had a business that was successful enough to justify multiple engineers working 7 months on porting our infrastructure from heroku to kubernetes"（[HN](https://news.ycombinator.com/item?id=41484423)）——**事實**
- **沒有雲端可用的環境**："We don’t have AWS, Google or any other cloud service here, so getting something running locally, that they can deploy code to, is part of my job."（[HN](https://news.ycombinator.com/item?id=39281567)，開發中國家的企業）——**事實**；這代表「地端加本地 AI」這類需求確實存在——**推論**

## 5. So What?

**三個機會假設**（用問題來描述，不是用功能）：
1. **新手看到「一直重啟」時，不知道原因在哪、也不知道該改什麼。** 信心：**中高**（Stack Overflow 125 萬次瀏覽，加上 HN 的佐證）
2. **在沒有任何資料之前就得決定資源大小，而且選錯的代價（被殺掉或帳單暴增）要等出事才看得到。** 信心：**中**（Stack Overflow 有 20 萬次瀏覽的佐證；成本那一半主要來自 HN）
3. **「付不起 PaaS 帳單、又養不起平台團隊」的夾心族群，想要 PaaS 的簡單，但基礎設施和資料要留在自己手上，而且自己要看得懂。** 信心：**中**（多個 HN 討論串、K8sGPT 的隱私 issue；但 HN 族群偏向專業人士）

**兩個可以拿來做競品比較的弱點**：
1. K8sGPT 的輸出是沒有結構的文字（證據品質：**高**，出自它自己 repo 的設計討論）
2. 把 K8s 包成 PaaS 的工具，仍然要求使用者懂 K8s 和 Helm，而且文件不足（證據品質：**中**，來自 HN 的少數留言）

**三個需要用真人訪談驗證的假設**：
1. 新手看到推算過程之後，會願意接受 AI 建議的資源數字（HN 上有人說寧可自己來，見主題 E）
2. 目標使用者想要「邊用邊學」，而不只是「幫我弄好就好」（HN 兩種聲音都有）
3. 「完全不把資料送到雲端」對學校、實驗室、中小企業來說，是真正的採購考量

---

# Jobs-to-be-Done（從上面的主題整理）

| # | 情境（When） | 想要（I want to） | 目的（So I can） | 來自哪個主題 |
|---|---|---|---|---|
| J1 | 剛部署的 app 一直重啟時 | 用白話知道原因，以及該改哪裡 | 不用讀 kubectl 輸出也能讓它跑起來 | A |
| J2 | 第一次部署、手上還沒有任何用量資料時 | 有人幫我挑合理的資源數字，並告訴我理由 | 不會一直被殺掉，也不會收到嚇人的帳單 | B、D |
| J3 | 規模超過單一主機、卻養不起平台團隊時 | 不用學 Helm 和 YAML 就能用 K8s | 基礎設施和成本仍然掌握在自己手上 | C |
| J4 | AI 要改動我的基礎設施時 | 先看到、能批准，而且看得懂它做了什麼 | 我仍然負得起責任，而且不會越用越笨 | E |

# 機會解法樹（Opportunity Solution Tree）

**目標成果**：不懂 K8s 的人，在**自己掌控**的叢集上，從描述需求到穩定運行，全程不用碰 YAML。

```
目標成果
├─ 機會 J1：看不懂為什麼一直重啟
│   ├─ 解法：規則優先的根因判斷 + 白話解釋 + 建議的下一步
│   └─ 實驗：拿 Stack Overflow 高票 CrashLoopBackOff 情境重現 10 個錯誤，量測「判斷出正確原因」的比例
├─ 機會 J2：資源大小只能猜
│   ├─ 解法：依使用情境推算規格、附上推算理由，並同時顯示「設太低的風險」和「預估每月成本」
│   └─ 實驗：用 OOMKilled 類情境，比較「新手自己填」和「系統建議」被殺掉的次數
├─ 機會 J3：要用 K8s 就得先學 K8s
│   ├─ 解法：自然語言部署；直接匯入 docker-compose（對應主題 C 的 Compose 原文）
│   └─ 實驗：讓 5 位沒碰過 K8s 的同學各部署一個 app，量測完成時間和卡關點
└─ 機會 J4：怕 AI 亂動、怕自己越用越笨
    ├─ 解法：每次變更前先確認；變更後用白話說明，並附上「如果自己手動做要下哪些指令」
    └─ 實驗：訪談時用 Mom Test 問法，問受訪者過去實際使用 AI 工具改動基礎設施的經驗
```

---

# 對這個平台的意義（推論，供決策參考）

**已經有的、這次找到外部佐證的**：
- **「LLM 只輸出結構化 JSON，再經過確定性的規則層」**：K8sGPT 在 2026-09-30 才開始設計同樣的方向（#1813）。這個平台的 `/infer` 嚴格 JSON、`/classify` 固定動作清單，在設計上已經走在前面。
- **Healer 的「規則層優先，LLM 補充」**：符合「many Kubernetes CrashLoopBackOff issues are detectable with simple log patterns」這個觀察（主題 A）。
- **本地模型**：直接回應 K8sGPT #139、#188 的隱私需求，以及「沒有雲端可用」的使用情境（主題 E、轉換誘因）。
- **確認卡和三方審查**：回應「先看到、能批准」的需求（J4）。

**原文指出、但還沒做到的**：
1. **「為什麼」的白話解釋**，以及「手動做要下哪些指令」的學習模式（J1、J4）
2. **資源推算附上理由**，同時顯示風險和成本（J2）
3. **docker-compose 匯入**（J3，有直接的使用者原文）

**這次沒有任何原文支持的方向**（不代表沒有價值，只是這次的資料裡找不到證據）：語音輸入、照片輸入。

---

## 下一步（skill 的四個選項）

1. 用主題 A 或 B 產生訪談問題（`discovery-interview-prep`，Mom Test 問法），去訪談 3 到 5 位修過雲端課的同學
2. 把競品弱點整理成競品比較卡（`battle-card-builder`）
3. 把最優先的機會假設展開成完整的機會解法樹（`opportunity-solution-tree`）
4. 針對單一主題（例如主題 B 資源大小）重跑一次，改用詳細模式並擴大樣本
