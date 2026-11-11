---
name: 2026-10-08-multimodal-scenario-features-backlog
description: 換新電腦後要做的5個新功能(語音/情境解析部署/省多少錢/Pod活躍期分時擴縮/丟檔案照片程式碼部署)+已做完的審核結論與優先順序；新電腦資源更強(使用者說16 ram，推測16GB VRAM待確認)，可以放手改
metadata:
  node_type: memory
  type: project
  originSessionId: 8e788281-87b8-4ad7-8a83-515bd08023fe
  modified: 2026-10-08T03:38:47.466Z
---

2026-10-08 使用者提出、要求「全部記下來，過幾天換新電腦再改」。先記錄不動工。
**新電腦資源比這台強**（使用者原話「有16ram」，推測指 GPU 16GB VRAM，舊機是 RTX 3060
Laptop 6GB——**開工前先實際查硬體確認**），使用者明確說之後「可以盡情地改」，
[[2026-10-07-personalization-decision]] 裡「6GB 裝不下第三顆模型」的前提可能不再成立。

## 主線定位（審核時給的核心建議，使用者沒有反對）
五個功能串成一條主線：**多種輸入方式（語音/情境描述/檔案/照片）→ 全部正規化成同一份
DeploySpec → 走既有三層防護（orchestrator → guardian → gitops）**。新入口絕不能繞過
既有審查管線與 Chat 確認卡（B1 規格確認、B3 破壞性兩步確認）。

## 五個功能與審核結論

1. **情境解析部署**（最推薦、優先做）：「辦 500 人線上報名活動、三天後結束」→ 系統推算
   規格。用 Gemini 抽「情境屬性」（預期流量、重要程度、有狀態與否、持續時間），再用
   **確定性規則**換算副本數/資源——不能讓 3B 從情境自由猜數字（幻覺風險，同
   [[2026-10-07-admin-identity-and-residual-fixes]] 的原則）。確認卡要附推算理由。
2. **省多少錢**（推薦、成本最低）：已有 `agents/cost_agent.py` 的 `_estimate_monthly_cost`
   （寫死 $0.048/vCPU/hr、$0.006/GiB/hr）。必須先定義比較基準（vs. 固定尖峰規格 / vs.
   使用者原始要求）；Docker Desktop 單節點實際不花錢，畫面要標「依公有雲牌價估算」。
   可併入 [[2026-10-07-next-session-feature-backlog]] 的「攔截歸因」。
3. **Pod 活躍期/一般/低活動期分時擴縮**：業界已有 HPA、KEDA cron scaler、雲端預測式擴縮，
   差異化在「從歷史自動學時段 + 白話解釋 + 換算省錢」。已查到的障礙：Prometheus
   retention 只有 **10d**（`kubectl get prometheus -n monitoring` 實查），學週期規律要
   2–4 週；demo app 沒真實流量要寫流量產生器；誤判會在尖峰縮容 → 要最低副本數、處理跟
   HPA 打架。建議先做「使用者口述時段 → 排程擴縮 + 顯示省錢」，自動學習之後再做。
   `agents/perf_agent.py` 目前只「建議」HPA，沒有實際建立任何 HPA/CronJob。
4. **丟檔案/照片/程式碼就部署**，拆三種：
   - docker-compose.yml、現成 K8s YAML：推薦（只解析不執行，可參考 Kompose）
   - 照片（架構圖/白板/教學截圖）：demo 亮點，但**圖片內文字是新的 prompt injection
     攻擊面**，會繞過現有只看文字的防線，結果必須走同樣確認卡+審查；圖片送 Gemini 要告知
   - 原始程式碼自動建置 image：**審核建議不做**——Dockerfile `RUN` 可在主機 Docker 執行
     任意指令，多租戶平台嚴重安全問題；還要架 registry；Railway/Render/Cloud Run 已很成熟
     無差異化。口試可把「刻意不做的理由」當加分點
5. **語音輸入**：Web Speech API 純前端約一天（Chrome 會把音訊送 Google、Firefox 不支援）；
   定位成無障礙/新手友善輸入方式，不是主打。聽錯（nginx→engine X）靠既有確認卡兜住，
   破壞性操作絕不能語音直接執行。新電腦 VRAM 夠的話可評估本地 Whisper。

## 建議優先順序
1 情境解析+省錢 → 2 語音+compose/YAML 入口 → 3 照片 → 4 時段擴縮（先手動版）
→ 5 原始碼建置（不做）。

**Why**：使用者要拿去口試，審核結論已經做過一次（含實查 Prometheus retention、cost_agent
單價、perf_agent 現況），不要重新從零評估。

**How to apply**：換新電腦接續時，先確認硬體，再跟使用者確認這條線跟
[[2026-10-07-next-session-feature-backlog]]（資料層 → 分析報告 → 動畫）哪條先做、
提醒挑一條主線做深；每項都是大改動，動工前走 Plan Mode。市場調查結果見
[[2026-10-08-market-research-result]]（若已寫入）可用來佐證優先順序。
