"""
rag/retriever.py
向量相似度查詢，給定問題後回傳最相關的 K8s 知識片段。

使用方式：
    from rag.retriever import retrieve, augment_prompt

    # 查詢相關文件
    results = retrieve("CrashLoopBackOff 怎麼解決", top_k=3)

    # 自動增強 LLM prompt
    enhanced = augment_prompt("幫我部署一個 nginx，3 個副本")

研究報告依據：
    「將 LLM 與最新的 Kubernetes 文檔、內部運行手冊以及組織最佳實踐相結合。
     這能有效防止模型「想像」出不存在的 API 版本或欄位」
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.config import ensure_utf8_output
ensure_utf8_output()

import json
import time
from typing import List, Dict, Optional

# ── 路徑設定 ────────────────────────────────────────────────────
RAG_DIR    = os.path.dirname(os.path.abspath(__file__))
INDEX_PATH = os.path.join(RAG_DIR, "index.json")
DOCS_DIR   = os.path.join(RAG_DIR, "k8s_docs")

# ── 緩存 ─────────────────────────────────────────────────────────
_index_cache: Optional[Dict] = None
_embed_model = None


# ════════════════════════════════════════════════════════════════
# 索引載入
# ════════════════════════════════════════════════════════════════

def load_index(index_path: str = INDEX_PATH) -> Optional[Dict]:
    """載入向量索引（帶緩存，只讀一次）。"""
    global _index_cache
    if _index_cache is not None:
        return _index_cache

    if not os.path.exists(index_path):
        return None

    with open(index_path, "r", encoding="utf-8") as f:
        _index_cache = json.load(f)

    chunks = len(_index_cache.get("chunks", []))
    method = _index_cache.get("method", "unknown")
    print(f"[RAG] 索引已載入：{chunks} chunks，方法：{method}")
    return _index_cache


def _ensure_index() -> Optional[Dict]:
    """確保索引存在，否則自動建立。"""
    idx = load_index()
    if idx is not None:
        return idx

    print("[RAG] 索引不存在，自動建立中...")
    try:
        from rag.build_index import build_index
        build_index()
        global _index_cache
        _index_cache = None  # 清除緩存，重新載入
        return load_index()
    except Exception as e:
        print(f"[RAG] 自動建立索引失敗：{e}")
        return None


# ════════════════════════════════════════════════════════════════
# 向量計算
# ════════════════════════════════════════════════════════════════

def _cosine_similarity(a: List[float], b: List[float]) -> float:
    """計算兩個向量的餘弦相似度。"""
    dot = sum(x * y for x, y in zip(a, b))
    na  = sum(x * x for x in a) ** 0.5
    nb  = sum(x * x for x in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _embed_query_semantic(query: str) -> Optional[List[float]]:
    """使用 sentence-transformers 嵌入查詢。"""
    global _embed_model
    try:
        if _embed_model is None:
            from sentence_transformers import SentenceTransformer
            _embed_model = SentenceTransformer("all-MiniLM-L6-v2")
        vec = _embed_model.encode([query])
        return vec[0].tolist()
    except Exception:
        return None


def _embed_query_tfidf(query: str, vocab: List[str]) -> List[float]:
    """使用 TF-IDF 嵌入查詢（與索引詞彙表一致）。"""
    import re
    from math import log

    tokens = re.findall(r'[a-zA-Z\u4e00-\u9fff]+', query.lower())
    w2i    = {w: i for i, w in enumerate(vocab)}
    vec    = [0.0] * len(vocab)

    tf: Dict[str, float] = {}
    for w in tokens:
        tf[w] = tf.get(w, 0) + 1

    for w, cnt in tf.items():
        if w in w2i:
            vec[w2i[w]] = cnt / (len(tokens) or 1)

    # L2 正規化
    norm = sum(x * x for x in vec) ** 0.5 or 1.0
    return [x / norm for x in vec]


# ════════════════════════════════════════════════════════════════
# 主要查詢函數
# ════════════════════════════════════════════════════════════════

def retrieve(query: str,
             top_k: int = 3,
             min_score: float = 0.05,
             index_path: str = INDEX_PATH) -> List[Dict]:
    """
    給定查詢字串，回傳 top_k 個最相關的文件片段。

    回傳格式：
    [
      {
        "score":    float,    # 相似度（0-1）
        "source":   str,      # 來源文件名稱
        "text":     str,      # 文件片段內容
        "chunk_id": str,      # chunk 唯一 ID
      },
      ...
    ]
    """
    idx = _ensure_index()
    if idx is None:
        return []

    method  = idx.get("method", "tfidf")
    chunks  = idx.get("chunks", [])
    vectors = idx.get("vectors", [])

    if not chunks or not vectors:
        return []

    # 嵌入查詢
    if method == "semantic":
        q_vec = _embed_query_semantic(query)
        if q_vec is None:
            # 回退到關鍵字匹配
            return _keyword_fallback(query, chunks, top_k)
    else:
        vocab = idx.get("vocab", [])
        q_vec = _embed_query_tfidf(query, vocab)

    # 計算所有 chunk 的相似度
    scores = [
        _cosine_similarity(q_vec, vec)
        for vec in vectors
    ]

    # 排序並過濾
    ranked = sorted(
        enumerate(scores),
        key=lambda x: -x[1]
    )

    results = []
    seen_sources: Dict[str, int] = {}  # 每個來源最多取 2 個 chunk

    for idx_i, score in ranked:
        if score < min_score:
            break
        if len(results) >= top_k:
            break

        chunk  = chunks[idx_i]
        source = chunk["source"]

        # 控制同一文件的 chunk 數量（避免全部來自同一文件）
        if seen_sources.get(source, 0) >= 2:
            continue
        seen_sources[source] = seen_sources.get(source, 0) + 1

        results.append({
            "score":    round(score, 4),
            "source":   source,
            "text":     chunk["text"],
            "chunk_id": chunk["chunk_id"],
        })

    return results


def _keyword_fallback(query: str, chunks: List[Dict], top_k: int) -> List[Dict]:
    """
    關鍵字匹配後備方案（不需要向量）。
    按關鍵字出現次數排序。
    """
    import re
    keywords = re.findall(r'[a-zA-Z\u4e00-\u9fff]{2,}', query.lower())

    scored = []
    for chunk in chunks:
        text_lower = chunk["text"].lower()
        score = sum(1 for kw in keywords if kw in text_lower)
        if score > 0:
            scored.append((score, chunk))

    scored.sort(key=lambda x: -x[0])
    return [
        {
            "score":    s / max(len(keywords), 1),
            "source":   c["source"],
            "text":     c["text"],
            "chunk_id": c["chunk_id"],
        }
        for s, c in scored[:top_k]
    ]


# ════════════════════════════════════════════════════════════════
# Prompt 增強（核心功能）
# ════════════════════════════════════════════════════════════════

def augment_prompt(user_request: str,
                   top_k: int = 3,
                   max_context_chars: int = 800) -> str:
    """
    將相關 K8s 知識注入到 user_request 前，形成 RAG-augmented prompt。

    若查不到相關文件，直接回傳原始 user_request（不影響正常流程）。
    """
    docs = retrieve(user_request, top_k=top_k)
    if not docs:
        return user_request

    # 組合知識片段（控制總長度）
    context_parts = []
    total_chars   = 0
    for doc in docs:
        text = doc["text"].strip()
        if total_chars + len(text) > max_context_chars:
            # 截斷以避免 prompt 過長
            remaining = max_context_chars - total_chars
            if remaining > 80:
                context_parts.append(text[:remaining] + "...")
            break
        context_parts.append(text)
        total_chars += len(text)

    if not context_parts:
        return user_request

    context = "\n\n---\n\n".join(context_parts)
    augmented = (
        f"[參考知識]\n{context}\n\n"
        f"[使用者請求]\n{user_request}"
    )
    return augmented


def format_context_for_display(docs: List[Dict]) -> str:
    """格式化查詢結果，供 Web Dashboard 或終端顯示。"""
    if not docs:
        return "（未找到相關文件）"

    lines = []
    for i, doc in enumerate(docs, 1):
        lines.append(f"[{i}] 來源：{doc['source']}（相似度：{doc['score']:.2f}）")
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
    parser.add_argument("query", nargs="?", default="CrashLoopBackOff 怎麼解決",
                        help="查詢字串")
    parser.add_argument("--top-k",  type=int, default=3, help="回傳結果數量")
    parser.add_argument("--augment", action="store_true", help="顯示增強後的 prompt")
    args = parser.parse_args()

    print(f"\n查詢：{args.query}")
    print("=" * 60)

    if args.augment:
        result = augment_prompt(args.query, top_k=args.top_k)
        print(result)
    else:
        results = retrieve(args.query, top_k=args.top_k)
        if not results:
            print("（未找到相關文件，請先執行 build_index.py 建立索引）")
        else:
            print(format_context_for_display(results))
