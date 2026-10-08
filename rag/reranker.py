"""
rag/reranker.py
Cross-encoder 二次精排——2026-10-08 查證過 2026 年業界現況，hybrid search 撈出
候選之後再用 cross-encoder reranker 精排是生產標準流程（比單純向量排序再疊加
約 33% 準確率）。只用在 `/chat` 知識庫檢索這條路徑（見 rag/retriever.py），
不動部署 few-shot 檢索（那條路徑有自己的延遲壓力，見 docs/security_review.md）。

跟 rag/vector_store.py 同一個模式：裝不起來就優雅退化，不擋住主流程。
"""
import os
from typing import Dict, List

# BAAI/bge-reranker-v2-m3：跟 BGE-M3 同一個團隊發布，是目前自架 reranker 的
# 預設基準選擇（CPU 也能跑，不需要額外的 pip 套件，sentence-transformers 的
# CrossEncoder 類別本來就通用支援 HuggingFace 上的 cross-encoder 模型）。
RERANKER_MODEL_NAME = "BAAI/bge-reranker-v2-m3"

_reranker = None
_checked = False
_usable = False


def is_available() -> bool:
    """sentence-transformers 是否已安裝（裝不起來就回 False，不嘗試載入模型）。"""
    try:
        import sentence_transformers  # noqa: F401
        return True
    except ImportError:
        return False


def _get_reranker():
    """lazy load，第一次真的要用才載入模型（跟 vector_store.py 的
    _get_embedding_function() 同一個理由：載入模型要花時間，不要在 import 這個
    模組的時候就付出這個成本）。"""
    global _reranker
    if _reranker is not None:
        return _reranker
    from sentence_transformers import CrossEncoder
    device = os.environ.get("RAG_RERANK_DEVICE", "cpu")  # 跟 embedding 一樣預設
    # CPU，不跟兩顆 LLM 搶 6GB 顯存
    _reranker = CrossEncoder(RERANKER_MODEL_NAME, device=device)
    return _reranker


def _reranker_ready() -> bool:
    global _checked, _usable
    if _checked:
        return _usable
    _checked = True
    try:
        _get_reranker()
        _usable = True
    except Exception as e:
        print(f"[RAG] Reranker 載入失敗（{e}），精排步驟跳過，沿用融合後的排序")
        _usable = False
    return _usable


def rerank(query: str, candidates: List[Dict], top_k: int) -> List[Dict]:
    """對 candidates（retrieve() 融合後的候選列表，每筆至少要有 "text" 欄位）
    用 cross-encoder 重新評分排序，回傳 top_k 筆。reranker 不可用時原樣回傳
    candidates 的前 top_k 筆（不擋住主流程，呼叫端不需要另外判斷）。

    cross-encoder 的原始分數是未經界定範圍的 logit，不是 0~1 的機率——套 sigmoid
    轉成 0~1 區間，才能跟既有的 confidence_label()/CHAT_MIN_SCORE 這些假設分數
    落在 0~1 的邏輯相容。
    """
    if not candidates:
        return candidates
    if not _reranker_ready():
        return candidates[:top_k]

    import math
    model = _get_reranker()
    pairs = [(query, c["text"]) for c in candidates]
    raw_scores = model.predict(pairs)

    rescored = []
    for cand, raw in zip(candidates, raw_scores):
        prob = 1.0 / (1.0 + math.exp(-float(raw)))  # sigmoid
        rescored.append({**cand, "score": round(prob, 4)})

    rescored.sort(key=lambda x: -x["score"])
    return rescored[:top_k]
