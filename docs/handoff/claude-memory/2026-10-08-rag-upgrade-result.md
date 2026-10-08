---
name: 2026-10-08-rag-upgrade-result
description: RAG升級結果——知識庫+BGE-M3有效採用，hybrid search+reranker實測後發現是負面退步，不採用
metadata:
  node_type: memory
  type: project
  originSessionId: 7df9e735-1dd3-4dd3-9f9f-4efb99c934ca
  modified: 2026-10-07T18:39:28.617Z
---

承接 [[2026-10-08-rag-upgrade-plan]]。計畫做完、實測完，結果跟原計畫的預期不同：

**量化對照表（最終數字，`rag/eval_retrieval.py` 24 題標註集，top_k=3）**：

| 方法 | Recall@3 | MRR | 平均延遲 |
|---|---|---|---|
| 升級前基準（舊 embedding + 舊 7 篇知識庫）chroma | 100% | 0.875 | 5.3ms |
| 升級前基準：tfidf | 91.7% | 0.799 | 8.7ms |
| **升級後：dense-only BGE-M3（15 篇知識庫，已採用）** | **95.8%** | **0.938** | ~90ms(warm)/440ms(cold) |
| 升級後：tfidf（legacy，未換） | 87.5% | 0.736 | ~39ms |
| RRF 融合（dense+tfidf，不接 reranker） | 91.7% | 0.868 | ~118ms |
| RRF 融合 + reranker（未採用） | 91.7% | 0.833 | 3.5~9.8 秒 |

**有效且已採用**：
- 知識庫 7→15 篇（815→1500+ 行），新增 networking/storage/scheduling/
  rbac_deep_dive/autoscaling/multi_container_patterns/rolling_update_strategies/
  troubleshooting_advanced 8 篇。
- embedding 模型從 `paraphrase-multilingual-MiniLM-L12-v2`（2021，118M 參數）
  換成 `BAAI/bge-m3`（568M 參數），MRR 從 0.875 進步到 0.938；延遲從 5.3ms
  變慢到 ~90ms 是模型變大的預期代價；Recall@3 數字看似從 100%→95.8%，但
  升級前知識庫只有 7 篇、問題跟文件幾乎一對一，天花板本來就容易打到 100%，
  15 篇之後語料更細分（例如新增的 autoscaling.md 跟 deployment_patterns.md
  現在會互相競爭排名），不是模型退步。

**附帶修正一個評測集過時的 bug**：第一輪測出來 Recall 從 100%→91.7%，一度
以為是模型退步，追查發現是新增的 `autoscaling.md` 對 HPA 問題搶答正確，但
舊標註只列 `deployment_patterns.md` 當正確答案，沒把新文件也列為可接受——
修正標註後重測變成 95.8%。**教訓**：知識庫內容變動後，既有標註評測集的
「正確答案」清單要重新檢視，不能看到 Recall 數字下降就直接下結論。

**實作完但決定不採用**：RRF hybrid search（dense+TF-IDF 融合）+ cross-encoder
reranker（`BAAI/bge-reranker-v2-m3`）。在這個知識庫規模（227 chunks）上，
hybrid 融合讓 Recall@3/MRR 雙雙從 dense-only 的 95.8%/0.938 掉到 91.7%/0.868
（TF-IDF 的弱訊號稀釋了已經很準的 dense 排序）；疊加 reranker 後 MRR 再掉到
0.833，且 reranker 在 CPU 上平均每題要跑 3.5~9.8 秒（比純 dense 慢一個量級
以上）。

**Why**：業界文獻講的「hybrid+reranker 是標準做法、能再提升 22-33%」是在大型、
異質、dense 本身查不準的語料庫上量出來的結論，不會自動適用於這種小而乾淨、
dense 已經很準的知識庫——疊加反而是淨負面。這次教訓：規劃技術升級時「業界
標準做法」只是起點，動手前/採用前要用專案自己的量化評測跑一次再決定，不要
直接套用文獻結論。

**How to apply**：`rag/retriever.py` 的 `retrieve()` 維持 dense-only（chroma
可用就用，不行才退回 TF-IDF），`_reciprocal_rank_fusion()`/`_fuse_candidates()`
跟 `rag/reranker.py` 的程式碼都保留（邏輯正確、有 `tests/test_rag_fusion.py`
10 個單元測試覆蓋），只是沒有被 `retrieve()` 預設呼叫——之後知識庫規模變大、
內容異質度變高，或換了查不準的 embedding 模型，才有理由重新評估要不要接上，
接上前一定要先用 `rag/eval_retrieval.py` 量一次再決定，不要憑感覺或憑文獻。
完整量化數據跟分析在 `docs/security_review.md` 28 節。
