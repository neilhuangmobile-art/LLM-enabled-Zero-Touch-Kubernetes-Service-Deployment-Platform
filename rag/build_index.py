"""
rag/build_index.py
建立 K8s 知識庫的向量索引，供 RAG 查詢使用。

研究報告依據：
    「檢索增強生成 (RAG)：將 LLM 與最新的 Kubernetes 文檔、內部運行手冊
     以及組織最佳實踐相結合。這能有效防止模型「想像」出不存在的 API 版本
     或欄位」

使用方式：
    python rag/build_index.py              # 建立索引
    python rag/build_index.py --rebuild    # 強制重建
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.config import ensure_utf8_output
ensure_utf8_output()

import json
import re
import hashlib
import argparse
import time
from pathlib import Path
from typing import List, Dict, Optional

# ── 路徑設定 ────────────────────────────────────────────────────
RAG_DIR    = os.path.dirname(os.path.abspath(__file__))
DOCS_DIR   = os.path.join(RAG_DIR, "k8s_docs")
INDEX_PATH = os.path.join(RAG_DIR, "index.json")

# ── 分塊參數 ─────────────────────────────────────────────────────
CHUNK_SIZE    = 400   # 每個 chunk 的最大字元數
CHUNK_OVERLAP = 80    # 相鄰 chunk 的重疊字元數


# ════════════════════════════════════════════════════════════════
# 文件載入
# ════════════════════════════════════════════════════════════════

def load_docs(docs_dir: str = DOCS_DIR) -> List[Dict]:
    """
    從 docs_dir 載入所有 .md / .txt / .yaml 文件。
    回傳：[{"source": str, "content": str}, ...]
    """
    docs = []
    docs_path = Path(docs_dir)
    if not docs_path.exists():
        print(f"[RAG] 文件目錄不存在：{docs_dir}")
        return docs

    for ext in ["*.md", "*.txt", "*.yaml", "*.yml"]:
        for f in sorted(docs_path.glob(ext)):
            try:
                text = f.read_text(encoding="utf-8")
                docs.append({"source": f.name, "content": text})
                print(f"[RAG] 載入：{f.name}（{len(text)} chars）")
            except Exception as e:
                print(f"[RAG] 跳過 {f.name}：{e}")

    print(f"[RAG] 共載入 {len(docs)} 份文件")
    return docs


# ════════════════════════════════════════════════════════════════
# 文件分塊（Chunking）
# ════════════════════════════════════════════════════════════════

def _split_by_sections(text: str) -> List[str]:
    """以 ## / # 標題分段，避免切斷語意。"""
    sections = re.split(r'\n(?=#{1,3} )', text)
    return [s.strip() for s in sections if s.strip()]


def chunk_document(doc: Dict, chunk_size: int = CHUNK_SIZE,
                   overlap: int = CHUNK_OVERLAP) -> List[Dict]:
    """
    將單份文件分成多個 chunk。
    先嘗試按 Markdown 標題分段，再按字元長度細分。
    """
    source  = doc["source"]
    content = doc["content"]
    chunks  = []

    sections = _split_by_sections(content)

    for section in sections:
        if len(section) <= chunk_size:
            chunks.append({
                "source":  source,
                "text":    section,
                "chunk_id": _make_id(source, section),
            })
        else:
            # 字元滑動視窗切分
            start = 0
            while start < len(section):
                end  = start + chunk_size
                text = section[start:end]
                chunks.append({
                    "source":   source,
                    "text":     text,
                    "chunk_id": _make_id(source, text),
                })
                start += chunk_size - overlap

    return chunks


def _make_id(source: str, text: str) -> str:
    """以 source + text 前 64 字產生穩定 ID。"""
    raw = f"{source}::{text[:64]}"
    return hashlib.md5(raw.encode()).hexdigest()[:12]


# ════════════════════════════════════════════════════════════════
# 向量嵌入（Embedding）
# ════════════════════════════════════════════════════════════════

_embed_model = None   # lazy-loaded sentence-transformer
_tfidf_data  = None   # TF-IDF fallback


def _try_load_sentence_transformer():
    """嘗試載入 sentence-transformers，失敗時回傳 None。"""
    global _embed_model
    if _embed_model is not None:
        return _embed_model
    try:
        from sentence_transformers import SentenceTransformer
        model_name = "all-MiniLM-L6-v2"  # 小型高效模型（22MB）
        print(f"[RAG] 載入 SentenceTransformer：{model_name}")
        _embed_model = SentenceTransformer(model_name)
        return _embed_model
    except ImportError:
        return None


def embed_texts_semantic(texts: List[str]) -> List[List[float]]:
    """使用 sentence-transformers 計算語意向量。"""
    model = _try_load_sentence_transformer()
    if model is None:
        raise RuntimeError("sentence-transformers 未安裝")
    vecs = model.encode(texts, show_progress_bar=True, batch_size=32)
    return vecs.tolist()


def embed_texts_tfidf(texts: List[str], vocab: Optional[List[str]] = None
                       ) -> tuple:
    """
    TF-IDF 後備嵌入（不需要 GPU 或額外模型下載）。
    回傳：(vectors_list, vocab_list)
    """
    from math import log
    import re

    def tokenize(t: str) -> List[str]:
        return re.findall(r'[a-zA-Z\u4e00-\u9fff]+', t.lower())

    if vocab is None:
        # 建立詞彙表
        doc_freq: Dict[str, int] = {}
        tokenized = [tokenize(t) for t in texts]
        for tokens in tokenized:
            for w in set(tokens):
                doc_freq[w] = doc_freq.get(w, 0) + 1
        # 選 top-2000 詞：保留所有出現至少一次的詞（min_df=1），
        # 只排除出現在 > 90% chunks 的高頻停用詞（無資訊量）
        sorted_vocab = sorted(doc_freq.items(), key=lambda x: -x[1])
        vocab = [w for w, _ in sorted_vocab[:2000] if 1 <= doc_freq[w] <= len(texts) * 0.9]
    else:
        tokenized = [tokenize(t) for t in texts]

    w2i = {w: i for i, w in enumerate(vocab)}
    n   = len(texts)
    vectors = []

    # 計算文件頻率（用於 IDF）
    doc_freq2: Dict[str, int] = {}
    for tokens in tokenized:
        for w in set(tokens):
            doc_freq2[w] = doc_freq2.get(w, 0) + 1

    for tokens in tokenized:
        tf: Dict[str, float] = {}
        for w in tokens:
            tf[w] = tf.get(w, 0) + 1
        vec = [0.0] * len(vocab)
        for w, cnt in tf.items():
            if w in w2i:
                idf = log((n + 1) / (doc_freq2.get(w, 0) + 1)) + 1
                vec[w2i[w]] = (cnt / len(tokens)) * idf
        # L2 正規化
        norm = sum(x * x for x in vec) ** 0.5 or 1.0
        vec  = [x / norm for x in vec]
        vectors.append(vec)

    return vectors, vocab


# ════════════════════════════════════════════════════════════════
# 主要建索引函數
# ════════════════════════════════════════════════════════════════

def build_index(docs_dir: str = DOCS_DIR,
                output_path: str = INDEX_PATH,
                use_semantic: bool = True) -> Dict:
    """
    建立完整的 RAG 索引並存至 JSON。

    回傳 index dict：
    {
      "method":   "semantic" | "tfidf",
      "chunks":   [{"source", "text", "chunk_id"}, ...],
      "vectors":  [[float, ...], ...],
      "vocab":    [...] (僅 tfidf 方法),
      "built_at": unix timestamp,
      "doc_count": int,
    }
    """
    t0 = time.time()

    # 1. 載入文件
    docs = load_docs(docs_dir)
    if not docs:
        print("[RAG] 沒有找到任何文件，索引未建立。")
        return {}

    # 2. 分塊
    all_chunks: List[Dict] = []
    for doc in docs:
        all_chunks.extend(chunk_document(doc))
    print(f"[RAG] 共產生 {len(all_chunks)} 個 chunks")

    texts = [c["text"] for c in all_chunks]

    # 3. 嵌入
    method = "tfidf"
    vocab  = None

    if use_semantic:
        try:
            vectors = embed_texts_semantic(texts)
            method  = "semantic"
            print(f"[RAG] 使用 sentence-transformers 完成嵌入（{len(vectors[0])}維）")
        except Exception as e:
            print(f"[RAG] 語意嵌入失敗（{e}），改用 TF-IDF...")
            vectors, vocab = embed_texts_tfidf(texts)
            print(f"[RAG] TF-IDF 嵌入完成（詞彙量 {len(vocab)}）")
    else:
        vectors, vocab = embed_texts_tfidf(texts)
        print(f"[RAG] TF-IDF 嵌入完成（詞彙量 {len(vocab)}）")

    # 4. 組裝索引
    index = {
        "method":    method,
        "chunks":    all_chunks,
        "vectors":   vectors,
        "built_at":  time.time(),
        "doc_count": len(docs),
    }
    if vocab is not None:
        index["vocab"] = vocab

    # 5. 儲存
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)

    elapsed = time.time() - t0
    print(f"[RAG] 索引已儲存至：{output_path}")
    print(f"[RAG] 建立耗時：{elapsed:.1f}s | 方法：{method} | Chunks：{len(all_chunks)}")
    return index


# ════════════════════════════════════════════════════════════════
# CLI
# ════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="建立 RAG 向量索引")
    parser.add_argument("--rebuild",    action="store_true", help="強制重建（忽略現有索引）")
    parser.add_argument("--no-semantic",action="store_true", help="停用語意嵌入，改用 TF-IDF")
    parser.add_argument("--docs-dir",   default=DOCS_DIR,   help="文件目錄路徑")
    parser.add_argument("--output",     default=INDEX_PATH, help="索引輸出路徑")
    args = parser.parse_args()

    if not args.rebuild and os.path.exists(args.output):
        print(f"[RAG] 索引已存在（{args.output}），使用 --rebuild 強制重建")
    else:
        build_index(
            docs_dir     = args.docs_dir,
            output_path  = args.output,
            use_semantic = not args.no_semantic,
        )
