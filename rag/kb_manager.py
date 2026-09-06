"""
rag/kb_manager.py
知識庫管理：讓使用者能透過網頁上傳/刪除知識文件、重建索引、查看索引狀態，
不需要 SSH 進終端機手動操作。

所有寫入操作都限制在 rag/k8s_docs/ 目錄底下，並嚴格檢查檔名，
避免路徑穿越（path traversal）寫到專案以外的地方。
"""
import json
import os
import time
from pathlib import Path
from typing import Dict, List, Optional

from rag.build_index import DOCS_DIR, META_PATH, build_index

ALLOWED_EXTENSIONS = {".md", ".txt"}
MAX_UPLOAD_BYTES   = 512 * 1024  # 512KB，知識文件不需要更大


class KBError(Exception):
    """知識庫操作的使用者可見錯誤（訊息可直接回傳給前端）。"""


def _safe_filename(filename: str) -> str:
    """
    驗證檔名安全性：只允許 .md / .txt，不能含路徑分隔符或 '..'。
    回傳乾淨的 basename；不合法時丟出 KBError。
    """
    name = os.path.basename((filename or "").strip())
    if not name or name != filename.strip() or ".." in filename:
        raise KBError("檔名不合法")
    ext = os.path.splitext(name)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise KBError(f"只允許 {', '.join(sorted(ALLOWED_EXTENSIONS))} 檔案")
    return name


def _load_meta() -> Dict:
    if not os.path.exists(META_PATH):
        return {}
    try:
        with open(META_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def list_documents() -> List[Dict]:
    """列出 k8s_docs/ 底下的所有知識文件，附上目前索引裡的 chunk 數。"""
    meta = _load_meta()
    per_source = meta.get("per_source", {})

    docs = []
    docs_path = Path(DOCS_DIR)
    if not docs_path.exists():
        return docs

    for f in sorted(docs_path.glob("*")):
        if not f.is_file() or f.suffix.lower() not in ALLOWED_EXTENSIONS:
            continue
        stat = f.stat()
        docs.append({
            "filename":    f.name,
            "size_bytes":  stat.st_size,
            "modified_at": stat.st_mtime,
            "chunk_count": per_source.get(f.name, 0),
        })
    return docs


def read_document(filename: str) -> str:
    name = _safe_filename(filename)
    path = os.path.join(DOCS_DIR, name)
    if not os.path.exists(path):
        raise KBError(f"找不到文件：{name}")
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def add_document(filename: str, content: str) -> Dict:
    """新增或覆寫一份知識文件（不會自動重建索引，需另外呼叫 rebuild）。"""
    name = _safe_filename(filename)
    if content is None or not content.strip():
        raise KBError("文件內容不能是空的")
    encoded = content.encode("utf-8")
    if len(encoded) > MAX_UPLOAD_BYTES:
        raise KBError(f"文件太大（上限 {MAX_UPLOAD_BYTES // 1024}KB）")

    os.makedirs(DOCS_DIR, exist_ok=True)
    path = os.path.join(DOCS_DIR, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return {"filename": name, "size_bytes": len(encoded)}


def delete_document(filename: str) -> Dict:
    """刪除一份知識文件（不會自動重建索引，需另外呼叫 rebuild）。"""
    name = _safe_filename(filename)
    path = os.path.join(DOCS_DIR, name)
    if not os.path.exists(path):
        raise KBError(f"找不到文件：{name}")
    os.remove(path)
    return {"filename": name, "deleted": True}


def rebuild_index() -> Dict:
    """重建整個索引（ChromaDB + TF-IDF），回傳建立狀態摘要。"""
    t0 = time.time()
    meta = build_index()
    if not meta:
        raise KBError("索引建立失敗：知識庫目錄可能是空的")
    meta["triggered_rebuild_sec"] = round(time.time() - t0, 2)
    return meta


def get_status() -> Dict:
    """回傳知識庫目前狀態，供管理介面顯示：索引方法、chunk 數、最後建立時間等。"""
    meta = _load_meta()
    try:
        from rag import vector_store
        from rag.retriever import active_method
        chroma_info = vector_store.stats() if vector_store.is_available() else {"backend": "chromadb", "error": "not installed"}
        method = active_method()
    except Exception as e:
        chroma_info = {"backend": "chromadb", "error": str(e)}
        method = "unknown"

    return {
        "active_method": method,
        "doc_count":     meta.get("doc_count", 0),
        "chunk_count":   meta.get("chunk_count", 0),
        "built_at":      meta.get("built_at"),
        "elapsed_sec":   meta.get("elapsed_sec"),
        "per_source":    meta.get("per_source", {}),
        "chroma":        chroma_info,
        "documents":     list_documents(),
    }
