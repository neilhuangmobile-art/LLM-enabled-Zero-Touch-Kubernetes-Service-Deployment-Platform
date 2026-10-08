# 市場需求調查：LLM 驅動的 K8s 零接觸部署平台

調查日期：2026-10-08。方法：依 idea-validator 流程，痛點、撞題、學術現況分開搜尋；
關鍵數字盡量打開原始頁面核對，**只看到搜尋摘要、沒打開原文的會標註「摘要」**。
「推論」與「查到的事實」分開標示。

---

## 結論

**可做，但主打方向要調整。** 痛點證據很強：資源過度配置、變更引發事故、技能不足，
都有大型調查的數字支撐。問題是**事後診斷（AI 幫你查 Pod 為什麼壞掉）已經是紅海**，
Healer 不能當主要賣點。真正較空、而且有數據支撐的切入點是**部署之前**：

1. 幫不懂 K8s 的人決定「要開多少資源」
2. 在變更上線前用看得懂的方式擋下風險
3. 讓人保有批准權

這三點剛好就是這個系統的 orchestrator 三方審查、確認卡，以及候選功能裡的情境解析和省錢估算。

| 面向 | 評等 | 依據 |
|---|---|---|
| 規則對齊 | 未評 | 沒有提供比賽或口試的評分辦法，拿到後可以補 |
| 痛點證據 | **高** | 82% 的工作負載過度配置、79% 的事故來自變更、62% 的團隊要求 AI 改動前要人工批准（見下方） |
| 撞題風險 | **中**（診斷：高；部署前把關：中低） | 診斷有 K8sGPT、HolmesGPT、Komodor、Metoro、AWS DevOps Agent；「替新手決定規格＋上線前把關」較少直接對手 |
| 資料取得 | **中** | 部署語料可以用公開 Helm chart 和 manifest；但目前的 QA 測試題是自己出題自己考，評審可能質疑（見風險 2） |
| 可行性 | **高** | 核心管線已經完成；候選功能大多是在現有管線上多開輸入入口 |

---

## 一、痛點證據（誰痛、多痛、現在怎麼解、為什麼不夠）

### 痛點 1：沒人知道該開多少資源，所以一律開太多（證據最強）
- **Komodor 2025 Enterprise Kubernetes Report**（2025-09-17，已核對原文）：**82%** 的工作負載
  過度配置，只有 **7%** 配置準確；**65%** 的工作負載用不到申請量的一半。
  [來源](https://komodor.com/blog/komodor-2025-enterprise-kubernetes-report-finds-nearly-80-of-production-outages/)
- **CAST AI 2025 Kubernetes Cost Benchmark**（2025-03-05 發布，2024 全年資料，2,100+ 組織，
  已核對新聞稿）：平均 CPU 使用率只有 **10%**、記憶體 **23%**。
  [來源](https://cast.ai/press-release/new-kubernetes-cost-benchmark-report-reveals-persistent-cloud-waste/)
  （搜尋摘要提到更新一年的報告是 8%，但那份報告在下載表單後面，沒有打開核對，所以不引用）
- **根因有名字：「developer resource anxiety」**。開發者怕記憶體不夠，所以一律開大。
  出處是 CAST AI 2024 報告，ITPro 2024-03-04 的報導。
  [來源](https://itpro.com/cloud/cloud-management/kubernetes-over-provisioning-is-causing-cloud-bills-to-skyrocket-and-developer-resource-anxiety-is-a-key-factor)
- **Spectro Cloud 2025 State of Production Kubernetes**（455 人，2025 年 5 月，摘要）：
  **成本首度成為第一大痛點（42%）**，超過技能和安全；88% 的人說總持有成本年增。
  [來源](https://www.businesswire.com/news/home/20250804240622/en/Spectro-Clouds-2025-State-of-Production-Kubernetes-Report-Finds-AI-Driving-Growth-as-Cost-Pressures-Bite)
- **現有解法為什麼不夠（推論）**：CAST AI、Gemini Cloud Assist 這類工具是**部署之後**看用量
  再建議縮小，適合已經上線、有用量資料的企業。新手在**部署當下**沒有資料可看，只能猜。
  情境解析部署正好補這個空缺：先用情境推算一個合理的起點，而不是事後才修正。

### 痛點 2：事故大多是自己的變更造成的
- Komodor 2025（已核對）：**79%** 的正式環境問題來自最近的系統變更；高影響事故偵測時間中位數
  約 **40 分鐘**、修復時間超過 **50 分鐘**；團隊每年在偵測和修復上花掉 **64 個以上工作天**；
  只有 **20%** 的事故不需要往上升級處理。
- **推論**：既然大部分事故源自變更，**在變更上線前擋住**的效益，理論上比事後診斷更大。
  但市面上的 AI 工具大多擠在事後診斷（見第三節）。

### 痛點 3：大家要 AI 幫忙，但不放心讓 AI 自己動手
- **Pulumi State of Agentic Infrastructure 2026**（510 位平台/DevOps 工程師，已核對原文）：
  81% 允許 AI 改動正式環境，但其中 **62% 要經過批准**，完全自動只有 **19%**；**61%** 使用
  人工審核關卡；在監控方面，只有 **12%** 讓 AI 完全自主，37% 只讓 AI 建議修法，
  31% 讓 AI 修但要先批准。
  [來源](https://www.pulumi.com/state-of-agentic-infrastructure/)
- **對本專案的意義**：市場要的是**「有人把關的自動化」**，不是全自動。這個系統的
  approve / warn / block 判決、規格確認卡、破壞性操作兩步確認，剛好符合市場實際採用的模式。
  **口試時不要把「全自動」當賣點**，「AI 做、人批准、每一步都看得懂」更符合數據。

### 痛點 4：技能不足，小團隊乾脆不用 K8s
- **CNCF 2025 年度調查**（628 人，2025 年 9 月，2026-01-20 發布，摘要）：82% 的容器使用者在
  正式環境跑 K8s；最大阻礙是組織文化（47%），接著是 CI/CD（40%）、**缺乏訓練（38%）**、
  安全（37%）。
  [來源](https://cioinfluence.com/cloud/kubernetes-established-as-the-de-facto-operating-system-for-ai-as-production-use-hits-82-in-2025-cncf-annual-cloud-native-survey/)
- 小團隊常見的結論是「K8s 對我們太複雜」，改用 Render、Railway、Fly.io 這類 PaaS，
  或是在 K8s 上再加一層平台（例如 Qovery）。
  [來源](https://www.qovery.com/blog/kubernetes-alternatives-small-team-no-cluster-management)
- **推論**：PaaS 把 K8s 藏起來，但資料和機器在別人手上。對**有自己的機器、要資料留在
  本地**的使用者（學校、實驗室、中小企業、地端或邊緣環境）來說，「像 PaaS 一樣好用、但跑在
  自己的叢集上、AI 模型也在本地」是一個空缺。Spectro 2025 的摘要也提到有企業把雲端服務搬回
  地端，以及 **50%** 已經在邊緣環境跑正式 K8s，可以佐證這個族群存在。

---

## 二、撞題／飽和度

| 類別 | 主要對手 | 飽和度 | 對本專案的意義 |
|---|---|---|---|
| AI 事後診斷／根因分析 | [K8sGPT、HolmesGPT、Komodor Klaudia、Metoro](https://metoro.io/comparisons/ai-sre/holmesgpt-alternatives)、AWS DevOps Agent（2026 年正式推出，摘要）、[GKE Gemini Cloud Assist](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/optimize-gke-gemini?hl=zh-tw) | **高** | Healer 只能當「完整閉環」的一環，不能當主打創新 |
| 學術：AI 自癒 | 2025 年已有多篇論文，例如根因分析準確率 96% 的多代理 SRE 系統（[摘要](https://race.reva.edu.in/race-lab/autonomous-multi-agent-system-for-integrated-sre-and-self-healing-in-cloud-native-environments)） | **高** | 同上 |
| 自然語言 → K8s manifest | 學術上已有研究：一項評估發現 ChatGPT 產生的 98 份 manifest 中 35.8% 有 lint 錯誤（摘要）；[Ueno & Uchiumi 2024](https://arxiv.org/abs/2408.11428) 發現「意圖不明確」的輸入準確度大幅下降。產品面有 CodeWords（自然語言 → serverless，摘要）、[kagent](https://www.shakudo.io/blog/deploy-ai-agents-on-kubernetes)（CNCF Sandbox，2025-05） | **中** | 學術已經證明「LLM 直接寫 YAML 會出錯」，這正好是本系統「模型只產 JSON、YAML 由程式樣板產生、再經過 guardian + dry-run」設計的理由，口試可以直接引用 |
| 部署前把關＋替新手決定規格 | 雲端廠商的建議功能都是部署之後才給；搜尋沒有找到專門替新手在部署當下推算規格並解釋理由的工具 | **中低** | **最值得主打的區塊** |
| 離峰排程縮容 | KEDA cron scaler、kube-downscaler、CronJob 自己寫（[範例](https://notes.kodekloud.com/docs/Kubernetes-Autoscaling/Kubernetes-Event-Driven-Autoscaling-KEDA/KEDA-CRON-Scaling)） | **高**（工具），**中**（白話＋自動判斷時段＋換算省錢） | 技術本身不新，差異化在呈現方式 |
| docker-compose → K8s | Kompose（輸出還需要專家手動修改）、2024 年的 LLM 轉換論文 | **中** | 跟既有管線結合、轉出來的結果仍經過審查，這點有差異 |
| 語音操作 K8s | 2017 年就有 Alexa VoiceOps 的報導（[Fortune](https://fortune.com/2017/02/21/amazon-alexa-voiceops-data-centers)）、[google-home-k8s](https://github.com/plutov/google-home-k8s)、黑客松作品 | **中**（概念不新） | 只能當輔助的輸入方式，不能當創新點 |
| 照片 → 部署 | 搜尋沒有找到直接對手 | **低** | 新鮮，但圖片 prompt injection 是已知攻擊：研究顯示攻擊成功率最高 82%（[CSA 2026](https://labs.cloudsecurityalliance.org/research/csa-research-note-image-prompt-injection-multimodal-llm-2026/)、[arXiv 2603.03637](https://arxiv.org/abs/2603.03637v1)，摘要） |

---

## 三、候選功能的優先順序（依證據排序）

| 優先 | 功能 | 對應的痛點證據 | 撞題程度 | 判斷 |
|---|---|---|---|---|
| 1 | **情境解析部署**（依情境推算規格並說明理由） | 痛點 1（82% 過度配置、resource anxiety）、痛點 4（缺乏訓練 38%） | 中低 | **主打**。直接回應「新手不知道要開多少」這個有名字的問題 |
| 1 | **省多少錢估算** | 痛點 1（成本是第一大痛點 42%、CPU 使用率 10%） | 中 | 跟情境解析一起做。一定要標明比較基準，以及「依公有雲牌價估算」 |
| 2 | **部署前把關的說服力**（強化現有三方審查） | 痛點 2（79% 事故來自變更）、痛點 3（62% 要求批准） | 中低 | 不是新功能，而是**重新包裝敘事**，搭配既有的攔截歸因 |
| 3 | **compose / YAML 檔案輸入** | 痛點 4、Kompose 輸出需要專家修改 | 中 | 便宜，跟既有管線結合 |
| 3 | **分時擴縮（先做手動設定時段）** | 非正式環境離峰縮容可以省 60–70%（[OneUptime](https://oneuptime.com/blog/post/2026-02-09-scheduled-node-pool-scaling/view)，摘要；一週 168 小時縮到約 55 小時的算式可以自己驗算） | 工具面高 | 用來佐證省錢估算，不宜當主打 |
| 4 | **照片輸入** | 沒有直接痛點證據，屬於 demo 亮點 | 低 | 要先設計圖片 injection 防線；防線本身反而可以當成口試的安全亮點 |
| 4 | **語音輸入** | 沒有痛點證據 | 中（概念不新） | 包裝成無障礙、新手友善的輸入方式 |
| ✗ | 原始程式碼自動建置 | — | 高（Railway、Render、Cloud Run） | 不做，理由寫進 `security_review.md` |

---

## 四、新發現的創新點子（推論，附依據）

1. **「學中做」模式**（對應缺乏訓練 38%）：每次部署或修復後，用白話解釋系統做了什麼、為什麼，
   還有「如果你自己手動做，要下哪些指令」。使用者用得越久越懂 K8s。這也呼應 AGENT_RULES 的
   新手友善原則。
2. **變更與事故的關聯**（對應 79% 的事故來自變更）：Healer 偵測到 Pod 出問題時，自動指出
   「這是 X 分鐘前那次部署之後開始的」，並提供一鍵回滾。Komodor 這類企業產品有做，但
   面向新手、而且整合在同一個對話入口裡的做法，搜尋時沒有找到。可以跟分析報告共用資料層。
3. **把關透明度報告**（對應 62% 要批准、61% 人工關卡）：分析報告裡列出「這個月 AI 提議了
   幾次、你批准幾次、擋下幾次、擋下的理由」。這是**可稽核性**，企業採用 AI 時在意的就是這個。

---

## 五、主要風險（最多 3 點）

1. **把 Healer 或「全自動」當主打會撞題，也跟市場數據相反。**
   解法：主打改成「部署前替新手決策＋有人把關的自動化」，Healer 定位成閉環裡的一環。
2. **評估資料是自己出題自己考。** 目前的 QA 掃描（32 題、206 題）都是自己設計的，評審可能質疑。
   解法：拿外部基準來量。例如用 [Ueno & Uchiumi 2024](https://arxiv.org/abs/2408.11428) 的
   compose→manifest 評估方法，或用公開的 Helm chart 和 docker-compose 範例當測試集，
   比較「本系統」和「直接叫 LLM 寫 YAML」的錯誤率。學術上已經有「ChatGPT 產生的 manifest
   35.8% 有 lint 錯誤」這種對照數字可以引用。
3. **引用的是企業調查，但目標使用者是新手，族群不一致。** 這些報告的受訪者大多是 250 人以上
   企業的平台團隊。
   解法：口試時講清楚因果——「連專業團隊都有 82% 過度配置，新手只會更嚴重」，並用 CNCF
   缺乏訓練 38% 和小團隊轉用 PaaS 的現象，補上新手族群的證據。

---

## 建議

- **保留**：自然語言部署、三層防護、GitOps、Healer（降為配角）、多租戶隔離。
- **調整主打**：「替不懂 K8s 的人在部署當下做出合理決策，並在上線前用看得懂的方式把關」，
  對應功能是情境解析、省錢估算和三方審查。
- **補強評估**：加一組外部基準實驗，把「自己出題」的質疑轉成可以比較的客觀數字。
- **刻意不做**：原始程式碼自動建置，並把理由寫成安全設計決策。

### 尚未查證、可以之後補的
- 比賽或口試的評分辦法（規則對齊這一項目前沒有評等）
- 台灣同類學生作品：用中文搜尋只找到國外的研討會和工作坊，沒有找到台灣的得獎作品，
  但這不代表沒有，可以再查獎金獵人或各校專題成果展
- CAST AI 最新一年（2025 年資料）的報告原文
