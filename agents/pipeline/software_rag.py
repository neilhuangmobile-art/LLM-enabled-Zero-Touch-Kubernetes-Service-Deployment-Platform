"""選軟體的 RAG（2026-11-17，使用者 11-15 決定：選軟體用「原句」查）。

知識表 rag/software_catalog.json：Docker 官方映像檔與幾個常用的廠商映像檔，每個附一句公開的說明
（來源寫在每一筆的 source，重抓用 scripts/build_software_catalog.py）。

用在兩個地方（不改推理代理的輸入：11-17 實測推理代理的輸入多一段文字，S38、S39 就不提問了）：
1. 查證：推理代理自己推出來的映像檔不在知識表裡（例如捏造的 mikepenz/mediawiki、redis/redis），
   不直接採用，改成問題，選項是用原句查出來的候選軟體。
2. 推理代理說要問使用者用哪個軟體時，問題卡附上候選軟體當選項。

查法：BGE-M3 向量（多語言，中文原句對英文說明）先取 25 個，再用 RAG 原本的 bge-reranker-v2-m3 重排取前 4。
11-17 實測只用向量時「資料庫」查出 registry、httpd，加上重排後是 mysql、redis、memcached、couchdb。
向量模型載入失敗就不給候選，只做查證；reranker 載入失敗就用向量的排序。"""
from __future__ import annotations

import json
import os
import threading
from typing import Callable, Dict, List, Optional

CATALOG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                            "rag", "software_catalog.json")
Embed = Callable[[List[str]], List[List[float]]]

_lock = threading.Lock()
_items: Optional[List[Dict]] = None
_doc_vecs = None
_embed: Optional[Embed] = None
_use_rerank = True


def items() -> List[Dict]:
    global _items
    if _items is None:
        try:
            with open(CATALOG_PATH, encoding="utf-8") as f:
                _items = json.load(f)["items"]
        except Exception as e:  # noqa: BLE001
            print(f"[software_rag] 讀不到知識表：{e}")
            _items = []
    return _items


def repo_of(image: str) -> str:
    """「docker.io/library/redis:7@sha256:…」→「redis」；「gitea/gitea:1.22」→「gitea/gitea」。"""
    s = str(image or "").strip().lower().split("@")[0]
    last = s.rsplit("/", 1)[-1]
    if ":" in last:
        s = s[: len(s) - len(last)] + last.split(":")[0]
    for prefix in ("docker.io/", "index.docker.io/", "registry-1.docker.io/"):
        if s.startswith(prefix):
            s = s[len(prefix):]
    if s.startswith("library/"):
        s = s[len("library/"):]
    return s


def is_known(image: str) -> bool:
    repo = repo_of(image)
    return bool(repo) and any(it["image"] == repo for it in items())


def full_name(image: str) -> Optional[str]:
    """「grafana:11」→「grafana/grafana:11」：名稱不在知識表、但剛好是某個廠商映像檔的簡稱（只有一個對得上）才補；
    其他情況回傳原值。"""
    repo = repo_of(image)
    if not repo or "/" in repo or is_known(repo):
        return image
    hits = [it["image"] for it in items() if not it.get("official") and it["image"].rsplit("/", 1)[-1] == repo]
    if len(hits) != 1:
        return image
    s = str(image).strip()
    tag = s.split("@")[0].rsplit("/", 1)[-1].split(":", 1)[1] if ":" in s.split("@")[0].rsplit("/", 1)[-1] else ""
    return hits[0] + (f":{tag}" if tag else "")


def _default_embed() -> Optional[Embed]:
    try:
        from rag.vector_store import _get_embedding_function, is_available
        if not is_available():
            return None
        ef = _get_embedding_function()
        return lambda texts: [list(map(float, v)) for v in ef(texts)]
    except Exception as e:  # noqa: BLE001
        print(f"[software_rag] 向量模型載入失敗，不給候選：{e}")
        return None


def set_embedder(fn: Optional[Embed]) -> None:
    """測試用：換成假的向量函式並關掉重排（None＝恢復預設）。"""
    global _embed, _doc_vecs, _use_rerank
    with _lock:
        _embed, _doc_vecs, _use_rerank = fn, None, fn is None


def candidates(text: str, k: int = 4) -> List[Dict]:
    """用原句查最相近的軟體（不含已停止維護的）。回傳 [{image, desc, score}]；向量模型不能用時回 []。"""
    global _embed, _doc_vecs
    pool = [it for it in items() if not it.get("deprecated")]
    if not pool or not str(text or "").strip():
        return []
    with _lock:
        if _embed is None:
            _embed = _default_embed()
        if _embed is None:
            return []
        try:
            if _doc_vecs is None:
                _doc_vecs = _embed([f"{it['image']}: {it['desc']}" for it in pool])
            q = _embed([text])[0]
        except Exception as e:  # noqa: BLE001
            print(f"[software_rag] 查詢失敗：{e}")
            return []
    scored = []
    for it, v in zip(pool, _doc_vecs):
        dot = sum(a * b for a, b in zip(q, v))
        scored.append((dot, it))
    scored.sort(key=lambda x: -x[0])
    out = [{"image": it["image"], "desc": it["desc"], "score": round(s, 4), "text": f"{it['image']}: {it['desc']}"}
           for s, it in scored[:25]]
    if _use_rerank:
        try:
            from rag.reranker import rerank
            out = rerank(text, out, k)
        except Exception as e:  # noqa: BLE001
            print(f"[software_rag] 重排失敗，用向量排序：{e}")
    return [{"image": c["image"], "desc": c["desc"], "score": c["score"]} for c in out[:k]]


def options(cands: List[Dict]) -> List[Dict]:
    """問題卡的選項：軟體名稱加上一句說明。"""
    return [{"value": c["image"], "label": f"{c['image']}：{c['desc'][:70]}"} for c in cands]
