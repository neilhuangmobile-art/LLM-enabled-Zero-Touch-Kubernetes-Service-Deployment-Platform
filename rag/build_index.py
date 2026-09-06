"""
rag/build_index.py
建立 K8s 知識庫索引，供 RAG 查詢使用。

雙軌索引策略：
    1. ChromaDB + sentence-transformers（主要）：真正的向量資料庫，
       持久化在磁碟上，支援語意相似度搜尋。
    2. TF-IDF JSON（後備）：不需要額外模型或 GPU，任何環境都能跑，
       在 chromadb / sentence-transformers 不可用時自動接手。

研究報告依據：
    「檢索增強生成 (RAG)：將 LLM 與最新的 Kubernetes 文檔、內部運行手冊
     以及組織最佳實踐相結合。這能有效防止模型「想像」出不存在的 API 版本
     或欄位」

使用方式：
    python rag/build_index.py              # 建立索引（已存在則略過）
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
RAG_DIR      = os.path.dirname(os.path.abspath(__file__))
DOCS_DIR     = os.path.join(RAG_DIR, "k8s_docs")
INDEX_PATH   = os.path.join(RAG_DIR, "index.json")      # TF-IDF 後備索引
META_PATH    = os.path.join(RAG_DIR, "index_meta.json")  # 建索引狀態摘要
SEED_QA_PATH = os.path.join(RAG_DIR, "prompt_qa_seed.jsonl")

# 部署範例索引：input（自然語言）→ output（小 JSON spec），給小模型當 few-shot。
# 只用 dataset/finetune_samples.jsonl（乾淨的 input→spec 對），不混 prompt_qa_seed
# （那些 output 是完整 YAML manifest，shape 不同，會誤導小模型）。
DEPLOY_INDEX_PATH   = os.path.join(RAG_DIR, "deploy_index.json")
DEPLOY_SAMPLES_PATH = os.path.join(os.path.dirname(RAG_DIR), "dataset", "finetune_samples.jsonl")
DEPLOY_INDEX_MAX    = 2000

# ── 分塊參數 ─────────────────────────────────────────────────────
CHUNK_SIZE    = 400   # 每個 chunk 的最大字元數
CHUNK_OVERLAP = 80    # 相鄰 chunk 的重疊字元數


# ════════════════════════════════════════════════════════════════
# 文件載入
# ════════════════════════════════════════════════════════════════

def load_docs(docs_dir: str = DOCS_DIR) -> List[Dict]:
    """
    從 docs_dir 載入所有 .md / .txt / .yaml 文件，並附加 prompt_qa_seed.jsonl。
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

    seed_path = Path(SEED_QA_PATH)
    if seed_path.exists():
        try:
            seed_count = 0
            for line in seed_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                item = json.loads(line)
                content = item.get("rag_text") or f"Question:\n{item.get('question','')}\n\nAnswer:\n{json.dumps(item.get('answer',{}), ensure_ascii=False)}"
                source = f"prompt_qa_seed:{item.get('id', seed_count + 1)}"
                docs.append({"source": source, "content": content})
                seed_count += 1
            print(f"[RAG] 載入 seed QA：{seed_count} 筆")
        except Exception as e:
            print(f"[RAG] 跳過 seed QA：{e}")

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
# TF-IDF 後備嵌入（無需 GPU / 額外模型下載）
# ════════════════════════════════════════════════════════════════

def embed_texts_tfidf(texts: List[str], vocab: Optional[List[str]] = None
                       ) -> tuple:
    """回傳 (vectors_list, vocab_list)。"""
    from math import log

    def tokenize(t: str) -> List[str]:
        return re.findall(r'[a-zA-Z一-鿿]+', t.lower())

    tokenized = [tokenize(t) for t in texts]

    if vocab is None:
        doc_freq: Dict[str, int] = {}
        for tokens in tokenized:
            for w in set(tokens):
                doc_freq[w] = doc_freq.get(w, 0) + 1
        # 選 top-2000 詞：保留所有出現至少一次的詞（min_df=1），
        # 只排除出現在 > 90% chunks 的高頻停用詞（無資訊量）
        sorted_vocab = sorted(doc_freq.items(), key=lambda x: -x[1])
        vocab = [w for w, _ in sorted_vocab[:2000] if 1 <= doc_freq[w] <= len(texts) * 0.9]

    w2i = {w: i for i, w in enumerate(vocab)}
    n   = len(texts)

    doc_freq2: Dict[str, int] = {}
    for tokens in tokenized:
        for w in set(tokens):
            doc_freq2[w] = doc_freq2.get(w, 0) + 1

    vectors = []
    for tokens in tokenized:
        tf: Dict[str, float] = {}
        for w in tokens:
            tf[w] = tf.get(w, 0) + 1
        vec = [0.0] * len(vocab)
        for w, cnt in tf.items():
            if w in w2i:
                idf = log((n + 1) / (doc_freq2.get(w, 0) + 1)) + 1
                vec[w2i[w]] = (cnt / len(tokens)) * idf
        norm = sum(x * x for x in vec) ** 0.5 or 1.0
        vec  = [x / norm for x in vec]
        vectors.append(vec)

    return vectors, vocab


def build_deploy_index(samples_path: str = DEPLOY_SAMPLES_PATH,
                       output_path: str = DEPLOY_INDEX_PATH,
                       limit: int = DEPLOY_INDEX_MAX) -> Dict:
    """建立部署範例的 TF-IDF 索引（input → output JSON），供小模型 few-shot 檢索。"""
    if not os.path.exists(samples_path):
        print(f"[RAG] 找不到部署範例檔：{samples_path}，跳過部署索引")
        return {}

    seen = set()
    samples: List[Dict] = []
    for line in Path(samples_path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except Exception:
            continue
        inp = str(item.get("input", "")).strip()
        out = item.get("output")
        if not inp or not isinstance(out, dict) or "pods" not in out:
            continue
        if inp in seen:
            continue
        seen.add(inp)
        samples.append({"input": inp, "output": out})
        if len(samples) >= limit:
            break

    if not samples:
        print("[RAG] 部署範例檔沒有可用樣本，跳過部署索引")
        return {}

    vectors, vocab = embed_texts_tfidf([s["input"] for s in samples])
    index = {
        "method":   "tfidf",
        "samples":  samples,
        "vectors":  vectors,
        "vocab":    vocab,
        "built_at": time.time(),
    }
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False)
    print(f"[RAG] 部署範例索引已建立：{output_path}（{len(samples)} 筆）")
    return {"sample_count": len(samples), "path": output_path}


def build_tfidf_index(all_chunks: List[Dict], output_path: str = INDEX_PATH) -> Dict:
    """建立 TF-IDF JSON 後備索引（永遠會執行，成本很低）。"""
    texts = [c["text"] for c in all_chunks]
    vectors, vocab = embed_texts_tfidf(texts)
    index = {
        "method":   "tfidf",
        "chunks":   all_chunks,
        "vectors":  vectors,
        "vocab":    vocab,
        "built_at": time.time(),
    }
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)
    return index


# ════════════════════════════════════════════════════════════════
# 主要建索引函數
# ════════════════════════════════════════════════════════════════

def build_index(docs_dir: str = DOCS_DIR, output_path: str = INDEX_PATH) -> Dict:
    """
    建立完整的 RAG 索引：
      1. 一律建立 TF-IDF JSON 後備索引（快速、零額外相依）
      2. 若 chromadb + sentence-transformers 可用，額外建立向量資料庫
         （查詢時優先使用，效果比 TF-IDF 好很多）

    回傳建索引狀態摘要（同時寫入 rag/index_meta.json）。
    """
    t0 = time.time()

    docs = load_docs(docs_dir)
    if not docs:
        print("[RAG] 沒有找到任何文件，索引未建立。")
        return {}

    all_chunks: List[Dict] = []
    for doc in docs:
        all_chunks.extend(chunk_document(doc))
    print(f"[RAG] 共產生 {len(all_chunks)} 個 chunks")

    # 1. TF-IDF 後備索引（一定會建立）
    build_tfidf_index(all_chunks, output_path)
    print(f"[RAG] TF-IDF 後備索引已建立：{output_path}")

    # 2. ChromaDB 向量資料庫（可用時建立）
    chroma_ok = False
    chroma_error = None
    try:
        from rag import vector_store
        if vector_store.is_available():
            n = vector_store.rebuild(all_chunks)
            chroma_ok = n > 0
            print(f"[RAG] ChromaDB 向量索引已建立：{n} chunks，device={vector_store.get_device()}")
        else:
            print("[RAG] chromadb / sentence-transformers 未安裝，跳過向量索引（僅用 TF-IDF）")
    except Exception as e:
        chroma_error = str(e)
        print(f"[RAG] ChromaDB 索引建立失敗（{e}），僅使用 TF-IDF 後備索引")

    per_source: Dict[str, int] = {}
    for c in all_chunks:
        per_source[c["source"]] = per_source.get(c["source"], 0) + 1

    elapsed = time.time() - t0
    meta = {
        "built_at":     time.time(),
        "elapsed_sec":  round(elapsed, 2),
        "doc_count":    len(docs),
        "chunk_count":  len(all_chunks),
        "chroma_ready": chroma_ok,
        "chroma_error": chroma_error,
        "active_method": "chroma" if chroma_ok else "tfidf",
        "per_source":   per_source,
    }
    with open(META_PATH, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    print(f"[RAG] 建立耗時：{elapsed:.1f}s | 使用方法：{meta['active_method']} | Chunks：{len(all_chunks)}")
    return meta


# ════════════════════════════════════════════════════════════════
# CLI
# ════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="建立 RAG 索引（ChromaDB + TF-IDF 後備）")
    parser.add_argument("--rebuild",  action="store_true", help="強制重建（忽略現有索引）")
    parser.add_argument("--docs-dir", default=DOCS_DIR,   help="文件目錄路徑")
    parser.add_argument("--output",   default=INDEX_PATH, help="TF-IDF 索引輸出路徑")
    parser.add_argument("--deploy",   action="store_true", help="同時重建部署範例索引（deploy_index.json）")
    args = parser.parse_args()

    if not args.rebuild and os.path.exists(args.output) and os.path.exists(META_PATH):
        print(f"[RAG] 索引已存在（{args.output}），使用 --rebuild 強制重建")
    else:
        build_index(docs_dir=args.docs_dir, output_path=args.output)

    if args.deploy or args.rebuild or not os.path.exists(DEPLOY_INDEX_PATH):
        build_deploy_index()
