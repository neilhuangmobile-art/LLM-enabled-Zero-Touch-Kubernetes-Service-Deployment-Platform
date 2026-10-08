"""
rag/retriever.py
給定問題，回傳最相關的 K8s 知識片段，並提供帶引用來源的 prompt 增強。

查詢優先順序：
    1. ChromaDB 向量資料庫（語意搜尋，品質最好）
    2. TF-IDF JSON 後備索引（chromadb 不可用時）
    3. 關鍵字比對（連 TF-IDF 索引都沒有時的最後防線）

使用方式：
    from rag.retriever import retrieve, augment_prompt, augment_prompt_ex

    # 查詢相關文件
    results = retrieve("CrashLoopBackOff 怎麼解決", top_k=3)

    # 自動增強 LLM prompt（不含來源，向後相容舊呼叫）
    enhanced = augment_prompt("幫我部署一個 nginx，3 個副本")

    # 同時取得增強後的 prompt 與引用來源（供前端顯示）
    enhanced, sources = augment_prompt_ex("CrashLoopBackOff 怎麼解決", history=history)
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.config import ensure_utf8_output
ensure_utf8_output()

import json
from typing import List, Dict, Optional, Tuple

# ── 路徑設定 ────────────────────────────────────────────────────
RAG_DIR    = os.path.dirname(os.path.abspath(__file__))
INDEX_PATH = os.path.join(RAG_DIR, "index.json")
DOCS_DIR   = os.path.join(RAG_DIR, "k8s_docs")
DEPLOY_INDEX_PATH = os.path.join(RAG_DIR, "deploy_index.json")

# 聊天路徑（/chat）用的相關度門檻，比 retrieve()/retrieve_with_history() 預設的 0.05 嚴格很多。
# 依據：跑 rag/eval_retrieval.py 的 24 筆標註測試集，top-1 正確命中分數落在 0.505~0.714，
# 而先前實測到被誤撈進聊天 prompt 的弱相關文件分數落在 0.29~0.32——0.3 低於全部正確命中、
# 高於已知雜訊分數，用來把「相關度太低、容易被模型拿來借題發揮幻覈」的文件擋在 RAG 增強之外。
CHAT_MIN_SCORE = 0.3

# ── 緩存 ─────────────────────────────────────────────────────────
_index_cache: Optional[Dict] = None
_chroma_checked = False
_chroma_usable  = False


# ════════════════════════════════════════════════════════════════
# ChromaDB（主要）
# ════════════════════════════════════════════════════════════════

def _chroma_ready() -> bool:
    """檢查 ChromaDB 是否可用且已有資料（帶緩存，避免每次查詢都重新檢查）。"""
    global _chroma_checked, _chroma_usable
    if _chroma_checked:
        return _chroma_usable
    _chroma_checked = True
    try:
        from rag import vector_store
        _chroma_usable = vector_store.is_available() and vector_store.count() > 0
    except Exception:
        _chroma_usable = False
    return _chroma_usable


def _raw_chroma_candidates(query_text: str, n: int) -> List[Dict]:
    """回傳 ChromaDB 的原始候選（已依分數排序，但還沒套用多樣性過濾/門檻）——
    2026-10-08 改成 hybrid search 之後，這份原始候選要跟 TF-IDF 的原始候選一起
    送進 RRF 融合，不能先各自截斷成 top_k 才融合，會漏掉融合後排名變動的候選。"""
    from rag import vector_store
    return vector_store.query(query_text, top_k=n)


def _retrieve_chroma(query_text: str, top_k: int, min_score: float) -> List[Dict]:
    """單純向量搜尋（不融合 TF-IDF），保留給需要單一方法比對的場景用
    （例如 rag/eval_retrieval.py 要分別評測 chroma 跟 tfidf 各自的品質）。"""
    results = _raw_chroma_candidates(query_text, top_k * 2)  # 多取一些，過濾後再截斷
    return _apply_diversity_filter(results, top_k, min_score)


# ════════════════════════════════════════════════════════════════
# TF-IDF JSON 後備索引
# ════════════════════════════════════════════════════════════════

def load_index(index_path: str = INDEX_PATH) -> Optional[Dict]:
    """載入 TF-IDF 後備索引（帶緩存，只讀一次）。"""
    global _index_cache
    if _index_cache is not None:
        return _index_cache

    if not os.path.exists(index_path):
        return None

    with open(index_path, "r", encoding="utf-8") as f:
        _index_cache = json.load(f)

    chunks = len(_index_cache.get("chunks", []))
    print(f"[RAG] TF-IDF 後備索引已載入：{chunks} chunks")
    return _index_cache


def _ensure_tfidf_index() -> Optional[Dict]:
    idx = load_index()
    if idx is not None:
        return idx

    print("[RAG] 索引不存在，自動建立中...")
    try:
        from rag.build_index import build_index
        build_index()
        global _index_cache
        _index_cache = None
        return load_index()
    except Exception as e:
        print(f"[RAG] 自動建立索引失敗：{e}")
        return None


def _cosine_similarity(a: List[float], b: List[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na  = sum(x * x for x in a) ** 0.5
    nb  = sum(x * x for x in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _embed_query_tfidf(query: str, vocab: List[str]) -> List[float]:
    import re
    tokens = re.findall(r'[a-zA-Z一-鿿]+', query.lower())
    w2i    = {w: i for i, w in enumerate(vocab)}
    vec    = [0.0] * len(vocab)

    tf: Dict[str, float] = {}
    for w in tokens:
        tf[w] = tf.get(w, 0) + 1

    for w, cnt in tf.items():
        if w in w2i:
            vec[w2i[w]] = cnt / (len(tokens) or 1)

    norm = sum(x * x for x in vec) ** 0.5 or 1.0
    return [x / norm for x in vec]


def _raw_tfidf_candidates(query_text: str, n: int) -> List[Dict]:
    """回傳 TF-IDF 的原始候選（已排序，未套用多樣性過濾/門檻），同上面
    `_raw_chroma_candidates()` 的理由——hybrid search 融合前兩邊都要用原始候選。
    分數完全算不出來（`any(scores)` 為 False）時退回關鍵字比對當最後防線。"""
    idx = _ensure_tfidf_index()
    if idx is None:
        return []

    chunks  = idx.get("chunks", [])
    vectors = idx.get("vectors", [])
    vocab   = idx.get("vocab", [])
    if not chunks or not vectors:
        return []

    q_vec = _embed_query_tfidf(query_text, vocab)
    scores = [_cosine_similarity(q_vec, vec) for vec in vectors]

    if not any(scores):
        return _keyword_fallback(query_text, chunks, n)

    ranked = [
        {"score": round(s, 4), "source": c["source"], "text": c["text"], "chunk_id": c["chunk_id"]}
        for c, s in zip(chunks, scores)
    ]
    ranked.sort(key=lambda x: -x["score"])
    return ranked[:n]


def _retrieve_tfidf(query_text: str, top_k: int, min_score: float) -> List[Dict]:
    """單純 TF-IDF 搜尋（不融合向量），保留給 ChromaDB 不可用時的完整 fallback
    路徑，跟 rag/eval_retrieval.py 的單一方法比對用。"""
    ranked = _raw_tfidf_candidates(query_text, top_k * 2)
    return _apply_diversity_filter(ranked, top_k, min_score)


def _keyword_fallback(query: str, chunks: List[Dict], top_k: int) -> List[Dict]:
    """關鍵字匹配後備方案（不需要向量），連 TF-IDF 都失敗時的最後防線。"""
    import re
    keywords = re.findall(r'[a-zA-Z一-鿿]{2,}', query.lower())

    scored = []
    for chunk in chunks:
        text_lower = chunk["text"].lower()
        score = sum(1 for kw in keywords if kw in text_lower)
        if score > 0:
            scored.append((score / max(len(keywords), 1), chunk))

    scored.sort(key=lambda x: -x[0])
    return [
        {"score": round(s, 4), "source": c["source"], "text": c["text"], "chunk_id": c["chunk_id"]}
        for s, c in scored[:top_k]
    ]


# ════════════════════════════════════════════════════════════════
# 共用：來源多樣性過濾
# ════════════════════════════════════════════════════════════════

def _apply_diversity_filter(ranked: List[Dict], top_k: int, min_score: float) -> List[Dict]:
    """依分數排序後取 top_k，並限制單一來源最多 2 個 chunk（避免結果全來自同一份文件）。"""
    ranked = sorted(ranked, key=lambda x: -x["score"])
    results = []
    seen_sources: Dict[str, int] = {}

    for item in ranked:
        if item["score"] < min_score:
            break
        if len(results) >= top_k:
            break
        source = item["source"]
        if seen_sources.get(source, 0) >= 2:
            continue
        seen_sources[source] = seen_sources.get(source, 0) + 1
        results.append(item)

    return results


def confidence_label(score: float) -> str:
    """把 0~1 分數轉成人看得懂的信心等級，供前端顯示徽章顏色用。"""
    if score >= 0.5:
        return "high"
    if score >= 0.25:
        return "medium"
    return "low"


def active_method() -> str:
    """目前查詢實際會用哪個 backend（chroma / tfidf / keyword）。"""
    if _chroma_ready():
        return "chroma"
    if load_index() is not None:
        return "tfidf"
    return "keyword"


# ════════════════════════════════════════════════════════════════
# 對話歷史感知（多輪對話記憶整合）
# ════════════════════════════════════════════════════════════════

def _build_context_query(message: str, history: Optional[List[Dict]] = None,
                          max_turns: int = 3) -> str:
    """
    把最近幾輪使用者發言與目前訊息合併，作為檢索用的查詢字串。
    這樣像「那個要怎麼修？」這種指代前一輪主題的追問，也能檢索到正確文件。
    純粹用來提升檢索品質，不影響最終注入 prompt 的內容（那個仍然只用 message 本身）。
    """
    if not history:
        return message

    recent_user_turns = [
        h.get("content", "") for h in history[-(max_turns * 2):]
        if h.get("role") == "user" and h.get("content")
    ]
    recent_user_turns = recent_user_turns[-max_turns:]
    if not recent_user_turns:
        return message

    return "\n".join(recent_user_turns + [message])


# ════════════════════════════════════════════════════════════════
# 主要查詢函數
# ════════════════════════════════════════════════════════════════

# ════════════════════════════════════════════════════════════════
# Hybrid Search：RRF 融合 dense（ChromaDB/BGE-M3）+ sparse（TF-IDF）
# ════════════════════════════════════════════════════════════════

def _reciprocal_rank_fusion(rank_lists: List[List[Dict]], k: int = 60) -> Dict[str, float]:
    """標準 RRF 公式：每份已排序清單裡，第 i 名（0-indexed）貢獻 1/(k+i+1) 分，
    同一個 chunk_id 在不同清單都出現時分數加總。k=60 是業界常用的預設常數
    （不太需要調參數，是 RRF 的主要優點之一）。"""
    scores: Dict[str, float] = {}
    for rank_list in rank_lists:
        for i, item in enumerate(rank_list):
            cid = item["chunk_id"]
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + i + 1)
    return scores


def _fuse_candidates(dense: List[Dict], sparse: List[Dict], pool_size: int) -> List[Dict]:
    """RRF 融合，回傳正規化過 0~1 分數、依分數排序、但還沒套用多樣性過濾跟
    min_score 門檻的候選池（交給呼叫端決定要不要先送進 reranker 再做最後過濾）。"""
    by_id = {}
    for item in dense + sparse:
        by_id.setdefault(item["chunk_id"], item)
    rrf_scores = _reciprocal_rank_fusion([dense, sparse])
    if not rrf_scores:
        return []
    max_score = max(rrf_scores.values()) or 1.0
    fused = [
        {**by_id[cid], "score": round(s / max_score, 4)}
        for cid, s in rrf_scores.items()
    ]
    fused.sort(key=lambda x: -x["score"])
    return fused[:pool_size]


def retrieve(query: str, top_k: int = 3, min_score: float = 0.05) -> List[Dict]:
    """
    給定查詢字串，回傳 top_k 個最相關的文件片段。

    2026-10-08 實測記錄（見 docs/security_review.md）：原本計畫在這裡接上
    RRF hybrid 融合 + cross-encoder reranker，但用 `rag/eval_retrieval.py`
    的 24 題標註測試集實測後發現，在這個知識庫（227 chunks，BGE-M3 dense
    檢索已經做到 91.7% Recall@3）上，疊加 TF-IDF 的 RRF 融合反而把分數拉低到
    87.5%（弱訊號稀釋了已經很準的 dense 排序），再疊加 reranker 更進一步退步
    到 MRR 0.792，而且 cross-encoder 在 CPU 上單題要跑快 10 秒（平均
    9818.6ms，比純 dense 的 88.3ms 慢了約 110 倍）——這組合在這個資料規模上
    是淨負面，所以**沒有採用**，維持「chroma 可用就用 dense-only，不行才退回
    TF-IDF」的原始邏輯。`_reciprocal_rank_fusion()`/`_fuse_candidates()` 跟
    `rag/reranker.py` 保留著（邏輯本身有測試覆蓋、沒有 bug），只是不在這裡
    預設呼叫，之後知識庫規模變大、或遇到 dense 本身就查不準的情境，可以再評估
    要不要重新接上。

    回傳格式：
    [{"score": float, "source": str, "text": str, "chunk_id": str}, ...]
    """
    if _chroma_ready():
        try:
            results = _retrieve_chroma(query, top_k, min_score)
            if results:
                return results
        except Exception as e:
            print(f"[RAG] ChromaDB 查詢失敗（{e}），改用 TF-IDF")

    return _retrieve_tfidf(query, top_k, min_score)


def retrieve_with_history(message: str, history: Optional[List[Dict]] = None,
                           top_k: int = 3, min_score: float = 0.05) -> List[Dict]:
    """考慮最近對話上下文的檢索（見 _build_context_query）。"""
    query_text = _build_context_query(message, history)
    return retrieve(query_text, top_k=top_k, min_score=min_score)


def augment_prompt(user_request: str, top_k: int = 3, max_context_chars: int = 800) -> str:
    """
    將相關 K8s 知識注入到 user_request 前，形成 RAG-augmented prompt。
    若查不到相關文件，直接回傳原始 user_request（不影響正常流程）。
    """
    augmented, _ = augment_prompt_ex(user_request, history=None, top_k=top_k,
                                      max_context_chars=max_context_chars)
    return augmented


def augment_prompt_ex(user_request: str, history: Optional[List[Dict]] = None,
                       top_k: int = 3, max_context_chars: int = 800,
                       min_score: float = 0.05
                       ) -> Tuple[str, List[Dict]]:
    """
    與 augment_prompt 相同，但同時回傳實際使用的引用來源（供前端顯示引用/信心分數）。
    """
    docs = retrieve_with_history(user_request, history=history, top_k=top_k, min_score=min_score)
    if not docs:
        return user_request, []

    context_parts = []
    total_chars   = 0
    used_docs     = []
    for doc in docs:
        text = doc["text"].strip()
        if total_chars + len(text) > max_context_chars:
            remaining = max_context_chars - total_chars
            if remaining > 80:
                context_parts.append(text[:remaining] + "...")
                used_docs.append(doc)
            break
        context_parts.append(text)
        used_docs.append(doc)
        total_chars += len(text)

    if not context_parts:
        return user_request, []

    context = "\n\n---\n\n".join(context_parts)
    augmented = f"[參考知識]\n{context}\n\n[使用者請求]\n{user_request}"
    return augmented, used_docs


def format_context_for_display(docs: List[Dict]) -> str:
    """格式化查詢結果，供 Web Dashboard 或終端顯示。"""
    if not docs:
        return "（未找到相關文件）"

    lines = []
    for i, doc in enumerate(docs, 1):
        lines.append(f"[{i}] 來源：{doc['source']}（相似度：{doc['score']:.2f}，信心：{confidence_label(doc['score'])}）")
        preview = doc["text"][:200].replace("\n", " ")
        lines.append(f"    {preview}...")
        lines.append("")

    return "\n".join(lines)


# ════════════════════════════════════════════════════════════════
# 部署範例檢索（few-shot）—— 給小模型 /infer 用
# ════════════════════════════════════════════════════════════════

_deploy_index_cache: Optional[Dict] = None
_deploy_index_missing = False


def _load_deploy_index() -> Optional[Dict]:
    global _deploy_index_cache, _deploy_index_missing
    if _deploy_index_cache is not None:
        return _deploy_index_cache
    if _deploy_index_missing:
        return None
    if not os.path.exists(DEPLOY_INDEX_PATH):
        _deploy_index_missing = True
        return None
    try:
        with open(DEPLOY_INDEX_PATH, "r", encoding="utf-8") as f:
            _deploy_index_cache = json.load(f)
        print(f"[RAG] 部署範例索引已載入：{len(_deploy_index_cache.get('samples', []))} 筆")
        return _deploy_index_cache
    except Exception as e:
        print(f"[RAG] 部署範例索引載入失敗：{e}")
        _deploy_index_missing = True
        return None


def retrieve_deploy_examples(query: str, top_k: int = 3) -> List[Dict]:
    """回傳 [{"input": str, "output": dict}, ...]，最相似的部署範例。

    deploy_index.json 不存在時回傳 []（呼叫端小模型仍可靠 system prompt 生成）。
    """
    idx = _load_deploy_index()
    if not idx:
        return []
    samples = idx.get("samples", [])
    vectors = idx.get("vectors", [])
    vocab   = idx.get("vocab", [])
    if not samples or not vectors or not vocab:
        return []

    q_vec  = _embed_query_tfidf(query, vocab)
    scores = [_cosine_similarity(q_vec, v) for v in vectors]
    ranked = sorted(zip(scores, samples), key=lambda x: -x[0])

    out = []
    for score, sample in ranked[:top_k]:
        if score <= 0:
            break
        out.append({"input": sample["input"], "output": sample["output"]})
    return out


# ════════════════════════════════════════════════════════════════
# CLI 測試工具
# ════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="RAG 查詢測試")
    parser.add_argument("query", nargs="?", default="CrashLoopBackOff 怎麼解決", help="查詢字串")
    parser.add_argument("--top-k",  type=int, default=3, help="回傳結果數量")
    parser.add_argument("--augment", action="store_true", help="顯示增強後的 prompt")
    args = parser.parse_args()

    print(f"\n查詢：{args.query}")
    print(f"使用方法：{active_method()}")
    print("=" * 60)

    if args.augment:
        result, sources = augment_prompt_ex(args.query, top_k=args.top_k)
        print(result)
        print("\n引用來源：")
        for s in sources:
            print(f"  - {s['source']}（信心：{confidence_label(s['score'])} / {s['score']:.2f}）")
    else:
        results = retrieve(args.query, top_k=args.top_k)
        if not results:
            print("（未找到相關文件，請先執行 build_index.py 建立索引）")
        else:
            print(format_context_for_display(results))
