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


def _retrieve_chroma(query_text: str, top_k: int, min_score: float) -> List[Dict]:
    from rag import vector_store
    results = vector_store.query(query_text, top_k=top_k * 2)  # 多取一些，過濾後再截斷
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


def _retrieve_tfidf(query_text: str, top_k: int, min_score: float) -> List[Dict]:
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
        return _keyword_fallback(query_text, chunks, top_k)

    ranked = [
        {"score": round(s, 4), "source": c["source"], "text": c["text"], "chunk_id": c["chunk_id"]}
        for c, s in zip(chunks, scores)
    ]
    ranked.sort(key=lambda x: -x["score"])
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

def retrieve(query: str, top_k: int = 3, min_score: float = 0.05) -> List[Dict]:
    """
    給定查詢字串，回傳 top_k 個最相關的文件片段。

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
