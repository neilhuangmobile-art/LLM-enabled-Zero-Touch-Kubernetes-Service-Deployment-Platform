"""選軟體的 RAG（2026-11-17，使用者 11-15 決定：選軟體用「原句」查）。

知識表 rag/software_catalog.json：Docker 官方映像檔與幾個常用的廠商映像檔，每個附一句公開的說明
（來源寫在每一筆的 source，重抓用 scripts/build_software_catalog.py）。

用在兩個地方（不改推理代理的輸入：11-17 實測推理代理的輸入多一段文字，S38、S39 就不提問了）：
1. 查證：推理代理自己推出來的映像檔不在知識表裡（例如捏造的 mikepenz/mediawiki、redis/redis），
   不直接採用，改成問題，選項是用原句查出來的候選軟體。
2. 推理代理說要問使用者用哪個軟體時，問題卡附上候選軟體當選項。

查法：BGE-M3 向量（多語言，中文原句對英文說明）先取 25 個，再用 RAG 原本的 bge-reranker-v2-m3 重排取前 4。
11-17 實測只用向量時「資料庫」查出 registry、httpd，加上重排後是 mysql、redis、memcached、couchdb。
向量模型載入失敗就不給候選，只做查證；reranker 載入失敗就用向量的排序。

2026-11-18 融合 RAG 欄位檢索（rag/field_retriever.py，黃柏碩負責）：
- 候選（candidates）預設改用 field_retriever 的混合檢索（BGE-M3 向量＋中文 BM25，RRF 合併，不重排）。
  知識庫是中文情境描述，對新手的中文句子較準：同一批題目前 3 名命中 22%→67%（419 題）、
  組員 S40 的 36 題 39%→75%，每句約 1.6 秒→20 ms（docs/rag_promptx/compare_teammate.json）。
  環境變數 RAG_BACKEND=legacy 改回原本的英文說明＋向量＋重排；field_retriever 出錯時也自動退回原本做法。
- 知識表（items）＝原本的 software_catalog.json 再加上 rag/knowledge/catalog.json 裡沒有的軟體，
  讓 field_retriever 給的候選不會被查證步驟當成捏造。同名時以原本的知識表為準（port、資料夾不變）。"""
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


FIELD_CATALOG_PATH = os.path.join(os.path.dirname(CATALOG_PATH), "knowledge", "catalog.json")


_force_legacy = False     # 測試換成假的向量函式時，固定走原本的查法（不載入真的模型）


def backend() -> str:
    if _force_legacy:
        return "legacy"
    return "legacy" if os.environ.get("RAG_BACKEND", "field").strip().lower() == "legacy" else "field"


def _field_items() -> List[Dict]:
    """rag/knowledge/catalog.json（RAG 欄位檢索的軟體目錄）轉成知識表格式。"""
    try:
        with open(FIELD_CATALOG_PATH, encoding="utf-8") as f:
            entries = json.load(f).get("entries") or []
    except Exception as e:  # noqa: BLE001
        print(f"[software_rag] 讀不到 RAG 軟體目錄：{e}")
        return []
    out = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        image = repo_of(e.get("repo") or e.get("image") or "")
        if not image:
            continue
        res = e.get("resources") if isinstance(e.get("resources"), dict) else {}
        desc = f"{e.get('category', '')}：" + "、".join((e.get("scenarios") or [])[:3])
        if e.get("notes"):
            desc += f"（{e['notes']}）"
        out.append({"image": image, "desc": desc[:300], "official": str(e.get("repo", "")).startswith("library/"),
                    "deprecated": False, "port": (e.get("ports") or [None])[0],
                    "data_path": (e.get("volumes") or [None])[0], "source": "rag/knowledge/catalog.json",
                    "_field_id": e.get("id"), "category": e.get("category", ""),
                    "memory": res.get("memory"),
                    "has_scenarios": bool(e.get("scenarios"))})
    return out


def items() -> List[Dict]:
    global _items
    if _items is None:
        try:
            with open(CATALOG_PATH, encoding="utf-8") as f:
                base = json.load(f)["items"]
        except Exception as e:  # noqa: BLE001
            print(f"[software_rag] 讀不到知識表：{e}")
            base = []
        have = {it["image"] for it in base}
        _items = base + [it for it in _field_items() if it["image"] not in have]
    return _items


_field_cache: Optional[List[Dict]] = None


def _field_cached() -> List[Dict]:
    """讀取失敗（空清單）不快取，下次再讀，避免一次暫時的讀檔錯誤讓副本數／記憶體調整整個程序都失效。"""
    global _field_cache
    if _field_cache is None:
        got = _field_items()
        if not got:
            return []
        _field_cache = got
    return _field_cache


def field_catalog_ok() -> bool:
    """RAG 軟體目錄讀得到（gap_check 用來在 trace 說明為什麼沒有調整）。"""
    return bool(_field_cached())


def _field_entry(image: str) -> Dict:
    repo = repo_of(image)
    return next((it for it in _field_cached() if it["image"] == repo), {})


def recommended_memory(image: str) -> str:
    """知識庫對這套軟體建議的記憶體（例如 512Mi）；不在知識庫回空字串。"""
    return _field_entry(image).get("memory") or ""


def category(image: str) -> str:
    """知識庫裡這個軟體的類別（資料庫、快取、維基或筆記…）；不在知識庫回空字串。"""
    return _field_entry(image).get("category") or ""




def choose_desc() -> Dict[str, str]:
    """選軟體判斷（confidence／pointwise／list）給模型看的說明。

    2026-11-19：同一個映像檔在 rag/knowledge/catalog.json 有中文類別＋情境描述時，優先用它。
    原本重疊的 34 套用的是 Docker Hub 的一句話簡介（nginx＝「Official build of Nginx.」、
    nextcloud＝「Nextcloud manual docker image」），模型看不出 nginx 可以放菜單、公告這種靜態網頁，
    dev_claude 有 15 題靜態網頁被選成 wordpress／dokuwiki 或因分數太低而反問。
    只換說明文字；port、資料夾、是否官方仍以 items()（舊知識表優先）為準。
    PIPELINE_CHOOSE_DESC=legacy 恢復原本的說明（對照實驗用）。"""
    legacy = os.environ.get("PIPELINE_CHOOSE_DESC", "field").strip().lower() == "legacy"
    d = {it["image"]: it["desc"] for it in items()}
    if legacy:
        return d
    d.update({it["image"]: it["desc"] for it in _field_cached() if it["image"] in d and it.get("has_scenarios")})
    return d


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
    global _embed, _doc_vecs, _use_rerank, _force_legacy
    with _lock:
        _embed, _doc_vecs, _use_rerank = fn, None, fn is None
        _force_legacy = fn is not None


def candidates(text: str, k: int = 4) -> List[Dict]:
    """用原句查最相近的軟體。回傳 [{image, desc, score}]；查不到或模型不能用時回 []。"""
    if not str(text or "").strip():
        return []
    if backend() == "field":
        try:
            return _field_candidates(text, k)
        except Exception as e:  # noqa: BLE001
            print(f"[software_rag] RAG 欄位檢索失敗，改用原本的查法：{e}")
    return _legacy_candidates(text, k)


_cand_cache: Dict[tuple, List[Dict]] = {}


def _field_candidates(text: str, k: int) -> List[Dict]:
    """rag/field_retriever 的混合檢索（正式版設定）。同一句在一次流程裡會查兩次（選軟體、問題卡），結果快取。"""
    key = (text, k)
    if key in _cand_cache:
        return [dict(c) for c in _cand_cache[key]]
    from rag import field_retriever as fr
    desc = {it.get("_field_id") or it["image"]: it for it in items()}
    out, seen = [], set()
    for c in fr.retrieve_for_field(text, "image", top_k=k, method="hybrid"):
        image = repo_of(c["value"])
        if not image or image in seen:
            continue
        seen.add(image)
        fid = c.get("source", "").split("#", 1)[-1]
        it = desc.get(fid) or next((x for x in items() if x["image"] == image), {})
        out.append({"image": image, "desc": it.get("desc", ""), "score": c.get("score")})
    if len(_cand_cache) > 256:
        _cand_cache.clear()
    _cand_cache[key] = [dict(c) for c in out]
    return out


def _legacy_candidates(text: str, k: int = 4) -> List[Dict]:
    """原本的做法：英文說明＋BGE-M3 向量前 25，再用 reranker 取前 k（不含已停止維護的）。"""
    global _embed, _doc_vecs
    pool = [it for it in items() if not it.get("deprecated") and it.get("source") != "rag/knowledge/catalog.json"]
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
