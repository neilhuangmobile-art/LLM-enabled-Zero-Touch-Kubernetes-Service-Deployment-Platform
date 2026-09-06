"""
rag/vector_store.py
ChromaDB 持久化向量儲存層，取代舊版把整份索引攤平存成一個 JSON 檔的做法。

只負責「儲存與相似度查詢」，分塊/嵌入模型選擇仍由 build_index.py 決定。
若 chromadb 或 sentence-transformers 未安裝，is_available() 回傳 False，
上層 retriever.py 會自動改用 TF-IDF 後備方案（見 rag/retriever.py）。
"""
import os
from typing import Dict, List

COLLECTION_NAME = "k8s_knowledge"
EMBED_MODEL_NAME = "paraphrase-multilingual-MiniLM-L12-v2"


def _default_chroma_dir() -> str:
    # 這台伺服器的系統碟空間有限，優先把向量索引放到有空間的資料碟；
    # 其他環境（例如本機開發）就退回存在 rag/ 目錄底下。
    shared_data_cache = "/mnt/Data/capstone2025/cache"
    if os.path.isdir(shared_data_cache):
        return os.path.join(shared_data_cache, "chroma_db")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "chroma_db")


CHROMA_DIR = os.environ.get("RAG_CHROMA_DIR") or _default_chroma_dir()

_client = None
_collection = None
_embed_fn = None
_device = None


def is_available() -> bool:
    """chromadb 與 sentence-transformers 是否都已安裝。"""
    try:
        import chromadb  # noqa: F401
        import sentence_transformers  # noqa: F401
        return True
    except ImportError:
        return False


def get_device() -> str:
    global _device
    if _device is not None:
        return _device
    # 2026-09-06：本機只有 6GB VRAM，要留給部署小模型（Qwen2.5-3B）。embedding 模型
    # 預設改跑 CPU，避免跟 LLM 搶顯存。真的想用 GPU 再設 RAG_EMBED_DEVICE=cuda。
    forced = os.environ.get("RAG_EMBED_DEVICE")
    if forced:
        _device = forced
        return _device
    _device = "cpu"
    return _device


def _get_embedding_function():
    global _embed_fn
    if _embed_fn is not None:
        return _embed_fn
    from chromadb.utils import embedding_functions
    _embed_fn = embedding_functions.SentenceTransformerEmbeddingFunction(
        model_name=EMBED_MODEL_NAME,
        device=get_device(),
        normalize_embeddings=True,
    )
    return _embed_fn


def get_collection(reset: bool = False):
    """取得（或建立）持久化的 Chroma collection。"""
    global _client, _collection
    import chromadb

    if _client is None:
        os.makedirs(CHROMA_DIR, exist_ok=True)
        _client = chromadb.PersistentClient(path=CHROMA_DIR)

    if reset:
        try:
            _client.delete_collection(COLLECTION_NAME)
        except Exception:
            pass
        _collection = None

    if _collection is None:
        _collection = _client.get_or_create_collection(
            name=COLLECTION_NAME,
            embedding_function=_get_embedding_function(),
            metadata={"hnsw:space": "cosine"},
        )
    return _collection


def rebuild(chunks: List[Dict]) -> int:
    """清空並用新的 chunks 重建 collection，回傳寫入的 chunk 數量。"""
    collection = get_collection(reset=True)
    if not chunks:
        return 0

    ids = [c["chunk_id"] for c in chunks]
    documents = [c["text"] for c in chunks]
    metadatas = [{"source": c["source"]} for c in chunks]

    batch_size = 256
    for i in range(0, len(ids), batch_size):
        collection.add(
            ids=ids[i:i + batch_size],
            documents=documents[i:i + batch_size],
            metadatas=metadatas[i:i + batch_size],
        )
    return len(chunks)


def query(text: str, top_k: int = 5) -> List[Dict]:
    """回傳 [{chunk_id, source, text, score}]，score 為 0~1 的餘弦相似度。"""
    collection = get_collection()
    n = collection.count()
    if n == 0:
        return []

    result = collection.query(query_texts=[text], n_results=min(top_k, n))
    ids   = (result.get("ids") or [[]])[0]
    docs  = (result.get("documents") or [[]])[0]
    metas = (result.get("metadatas") or [[]])[0]
    dists = (result.get("distances") or [[]])[0]

    out = []
    for i, chunk_id in enumerate(ids):
        distance = dists[i] if i < len(dists) else 2.0
        # hnsw:space=cosine ⇒ distance = 1 - cosine_similarity
        score = max(0.0, min(1.0, 1.0 - distance))
        meta = metas[i] or {}
        out.append({
            "chunk_id": chunk_id,
            "source":   meta.get("source", "unknown"),
            "text":     docs[i],
            "score":    round(score, 4),
        })
    return out


def count() -> int:
    try:
        return get_collection().count()
    except Exception:
        return 0


def stats() -> Dict:
    try:
        collection = get_collection()
        return {
            "backend":        "chromadb",
            "chunk_count":    collection.count(),
            "embedding_model": EMBED_MODEL_NAME,
            "device":         get_device(),
            "path":           CHROMA_DIR,
        }
    except Exception as e:
        return {"backend": "chromadb", "error": str(e)}
