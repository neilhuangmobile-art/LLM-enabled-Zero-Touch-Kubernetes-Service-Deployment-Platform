---
name: 2026-10-09-next-roadmap-frontend-redesign
description: 使用者 10/9 定下的接下來順序：①客戶叢集剩餘部分接好並驗證 ②前端大改（精緻、動畫、互動、不要像 AI 寫的）③再做舊電腦留下的待辦功能
metadata:
  node_type: memory
  type: project
  originSessionId: 110ca64c-0ce9-476a-aa64-bad49bb5b63d
  modified: 2026-10-09T05:32:51.248Z
---

2026-10-09 使用者明確交代的順序（不要跳步）：

1. **先把客戶自帶叢集剩下的部分接好、確定沒問題**（見 [[2026-10-09-customer-cluster-onboarding-project]]）。
2. **前端大改**：使用者不滿意現在的網頁「寫得太像 AI、很多地方太陽春、不夠精緻」，明確點名我有
   frontend-design 卻沒用好。要求：
   - 重新調整整體審美，做得精緻
   - 加入更多**動態互動效果與動畫效果**
   - **不要出現 AI 寫網站常見的特殊符號與空格**（例如滿版 emoji ✅❌⚠️、「中文 / English」斜線並陳、
     奇怪的全形/半形空格、裝飾性符號）
   - 動手前先載入 frontend-design skill，大改動要先出計畫給使用者看
3. **前端改完之後**，再做舊電腦留下的待辦功能：[[2026-10-07-next-session-feature-backlog]]（動態 UI/動畫 +
   一句話生成歷史分析報告）、[[2026-10-08-multimodal-scenario-features-backlog]]（情境部署、省錢估算等）。

**Why**：使用者要拿去口試，前端是評審第一眼看到的東西。
**How to apply**：接續工作時照這個順序；前端改版會牽動 web_demo.py 內嵌的 6500 行 HTML 與 cluster/pages.py，
要注意 AGENT_RULES 的新手友善原則（中文在前、錯誤要講怎麼辦）跟「不要 AI 感」之間的平衡——
雙語可以保留但改成不像機器拼接的呈現方式，這點要在計畫裡跟使用者確認。
