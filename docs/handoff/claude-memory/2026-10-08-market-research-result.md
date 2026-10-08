---
name: 2026-10-08-market-research-result
description: 拖延已久的市場需求調查已完成(docs/market_research_20261008.md)——結論：診斷/自癒是紅海不能當主打，主打改成「部署前替新手決策+有人把關的自動化」；最大風險是QA測試自己出題自己考，要補外部基準
metadata:
  node_type: memory
  type: project
  originSessionId: 8e788281-87b8-4ad7-8a83-515bd08023fe
  modified: 2026-10-08T03:57:12.435Z
---

2026-10-08 用 idea-validator skill 完成 [[next-session-market-research]] 拖了三次的市場調查，
完整報告（附來源、已核對/僅摘要/推論分開標示）在 `docs/market_research_20261008.md`
（尚未 commit）。

**核心結論**：
- 事後診斷（K8sGPT、HolmesGPT、Komodor、Metoro、AWS DevOps Agent、GKE Gemini）是**高飽和**，
  Healer 只能當閉環的一環，不能當主打。
- 證據最強、對手較少的是**部署前**：替新手推算規格（82% 工作負載過度配置、CPU 使用率 10%、
  「developer resource anxiety」）、上線前把關（79% 事故源自變更）、**有人把關的自動化**
  （Pulumi 2026：62% 要批准、完全自動只有 19%）→ 不要把「全自動」當賣點。
- 這份調查佐證了 [[2026-10-08-multimodal-scenario-features-backlog]] 的優先順序：情境解析＋省錢估算
  是主打；語音、照片只是輸入方式。
- 新點子：學中做模式、變更與事故的關聯（Healer 指出是哪次部署造成的）、把關透明度報告。

**同日第二份（使用者要求「不看舊報告」重做）**：`docs/market_research_voc_20261008.md`，用
voice-of-customer-miner 方法改抓**使用者原話**（HN 172 則 + Stack Overflow 31 題 + K8sGPT issue，
原始 JSONL 與腳本在 `docs/research/`，33 句引號原文已逐字核對）。五個需求主題：看懂為什麼一直重啟
（SO 8 題累計 125 萬瀏覽）、不知道資源給多少（OOM 8 題 20 萬瀏覽）、部署不想先學 K8s（含 compose
轉換原文）、成本看不懂、AI 要看得懂管得住（"I do get dumber for it"）。外部佐證：K8sGPT #1813
（2026-09-30）才開始設計「LLM 輸出 → 確定性動作分類」，本平台已有。**語音/照片在原文裡找不到需求
證據**。限制：Reddit API 403 沒引用、SO 高票多為 2015–2022 舊題、沒抓中文社群。

**Why**：使用者要拿去口試，需要有來源的市場定位。

**How to apply**：規劃功能或口試敘事時，以「部署前決策＋有人把關」為主軸。**最大風險是評估資料
自己出題自己考**（QA 掃描都是自己設計的題目），應該補一組外部基準實驗（例如 Ueno & Uchiumi 2024
arXiv 2408.11428 的 compose→manifest 評估方法，或公開 Helm chart/compose 當測試集，比較
「本系統」和「直接叫 LLM 寫 YAML」）。未查證：比賽評分辦法、台灣同類學生作品、CAST AI 最新年報原文。
