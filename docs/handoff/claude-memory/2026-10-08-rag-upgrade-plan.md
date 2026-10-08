---
name: 2026-10-08-rag-upgrade-plan
description: RAG升級分兩階段——先做知識庫擴充+BGE-M3+hybrid search+reranker（地基），再做Agentic深度模式（蓋在地基上）；深度模式建議放在Healer深度診斷跟分析報告生成，不是泛用聊天，理由是直接解決這次session發現的監控模型幻覈問題
metadata:
  type: project
  originSessionId: 7df9e735-1dd3-4dd3-9f9f-4efb99c934ca
  modified: 2026-10-07T18:05:08.727Z
---

2026-10-08 查了業界現況（hybrid search/reranker/BGE-M3/RAGAS/Agentic RAG）後，使用者
想把這些都套用，加碼問「能不能做一個 Claude 用的那種多階段、每階段會問問題的 RAG 當
第二種模式」。

**查到的關鍵技術決策（帶來源，見下方對話記錄）**：
- Hybrid search（dense+BM25 用 RRF 融合）是生產標準，比純向量搜尋 NDCG 提升 22-28%
- Reranker（cross-encoder 二次精排）再疊加約 33% 準確率，標準流程：hybrid 撈 top-50
  → reranker 精排成 top-5 → 才給生成模型
- **BGE-M3 是關鍵發現**：一個模型同時輸出 dense/sparse/multi-vector 三種表示，不用
  維護兩套系統就能做 hybrid search，中文效果比現在用的
  `paraphrase-multilingual-MiniLM-L12-v2`（2021年的輕量模型）好很多
- CPU 能跑的 reranker：`bge-reranker-v2-m3-onnx-o3-cpu` 或更輕的 `Qwen3-Reranker-0.6B`
- RAGAS：reference-free 評測框架，不用像現在的 24 題手寫評測集那樣每題人工標答案

**Agentic RAG（多階段、每階段問問題）的決策**：
- 概念：Query Decomposition（拆成子問題）/ Corrective RAG（查完評分夠不夠相關，
  不夠就重查）/ Self-RAG（模型自己判斷要不要查、查得夠不夠）
- **不建議套用在一般 Chat 問答**：每多一階段就多一次模型呼叫，這個系統延遲已經是
  痛點（p95 19 秒、`/diagnose` 平均 34.7 秒），套用在簡單問題上只會更慢、不會更準，
  知識庫內容量也還沒大到需要多輪推理才能回答
- **建議放兩個地方**：
  1. **Healer 深度診斷模式**——這是最有價值的落點。直接對應這次 session 花最多
     力氣修的「監控小模型證據不足時仍幻覈具體根因」問題（見
     [[2026-10-07-large-qa-sweep-and-fixes]] 跟 `docs/security_review.md` 23 節）。
     深度模式讓診斷流程變成「先看日誌 → 形成假設 → 查事件有沒有支持 → 查依賴服務 →
     查知識庫類似案例 → 才下結論」，不只是更慢，是真正解決證據不足就下定論這個
     根本問題。
  2. **分析報告生成**（[[2026-10-07-next-session-feature-backlog]] 的待做項目 2）——
     使用者對報告本來就預期要等，agentic 的交叉驗證可以換來報告真的站得住腳。

**Why**：延遲代價要花在「真的需要多輪推理才能答對」的地方，不是泛用套用。

**How to apply**：**分兩階段做，順序不能反**——先做知識庫擴充 + BGE-M3 +
hybrid search + reranker（地基），這次 session 已經開始動工（見當次對話記錄）；
Agentic 深度模式要等地基穩了才做，而且第一個落點選 Healer 深度診斷，不是聊天模式，
因為沒有好的檢索機制，agentic 模式只是「查錯東西查很多次」。
