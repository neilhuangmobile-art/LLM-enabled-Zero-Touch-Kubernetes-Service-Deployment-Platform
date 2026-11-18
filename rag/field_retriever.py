"""
RAG 改版：給推理代理的欄位檢索（2026-11-15，計畫 docs/plan_RAG_20261115.md）

六個資料庫（原始資料在 rag/knowledge/）：
  catalog       C1 軟體目錄（catalog.json，Docker Hub 驗證過的連接埠與資料目錄）
  examples      C2 情境範例（examples.jsonl，新手說法 → 軟體與需求）
  requirements  C2 需求通則（requirements.yaml，口語暗示 → 額外設定）
  lessons       失敗經驗（lessons.yaml）
  troubleshoot  C4 疑難排解（troubleshooting.yaml）
  knowledge     C3 知識文件（rag/k8s_docs/*.md＋knowledge/k8s_official/*.md，依章節切段）

檢索方式（method）：dense（BGE-M3 向量）、bm25（中文雙字切詞）、hybrid（兩者以 RRF 合併）、
hybrid_rerank（hybrid 再用 bge-reranker 重新排序）。預設值待評估後決定（RAG_FIELD_METHOD）。
門檻目前是暫定值，第 6 步用開發集決定。

對外介面：
  retrieve_for_field(query, field, top_k=3, version=None, method=None)
  retrieve_requirements(query, top_k=5, method=None)
  retrieve_lessons(query, stage=None, top_k=3, method=None)
  retrieve_knowledge(query, kind="concept", top_k=3, method=None)
  lookup_software(name_or_image)            # 已知軟體時直接查目錄（不經檢索）
  log_feedback(query, field, suggested, final)  # 確認卡回饋，之後多臂老虎機使用
"""
from __future__ import annotations

import json
import math
import os
import re
import time
from collections import Counter, defaultdict
from typing import Dict, List, Optional

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
KDIR = os.path.join(HERE, "knowledge")
INDEX_DIR = os.environ.get("RAG_INDEX_DIR") or os.path.join(HERE, "field_index")
CATALOG_FILE = os.environ.get("RAG_CATALOG_FILE") or os.path.join(KDIR, "catalog.json")   # 實驗時可切換成擴充版
FEEDBACK_PATH = os.path.join(os.path.dirname(HERE), "data", "rag_feedback.jsonl")

DEFAULT_METHOD = os.environ.get("RAG_FIELD_METHOD", "dense")
# 暫定門檻（各方法分數尺度不同），第 6 步以開發集校準
MIN_SCORE = {"dense": 0.45, "bm25": 0.15, "hybrid": 0.012, "hybrid_rerank": 0.15}
FIELDS_FROM_SOFTWARE = {"image", "port", "volume", "env", "memory", "cpu", "replicas"}
# 可調設定（rag/tune_fields.py 用 Optuna 搜尋；預設值＝11/15 基準評估時的做法）
#   multi_rep  軟體目錄每句情境描述各自建一筆索引（指回同一個軟體），取最高分
#   lexical    混合檢索的關鍵字那一路：bm25（中文雙字）或 m3sparse（BGE-M3 自帶的關鍵字權重）
#   cat_first  先判斷類別（前 cat_k 個候選依類別加總分數），最高類別的軟體排前面
#   prior_w    主流優先加權（0＝不加；人工目錄＞官方映像檔＞自動擴充依星數）
#   rrf_k、w_dense、w_lex   RRF 合併參數與兩路權重；pool_mult 每路取 top_k × pool_mult 筆合併
CFG = {"multi_rep": False, "lexical": "bm25", "rrf_k": 60, "w_dense": 1.0, "w_lex": 1.0, "pool_mult": 4,
       "examples": True, "prior_w": 0.0,
       "cat_first": False, "cat_k": 10, "tiered_margin": 0.0,
       "split_delta": None}


# ── 文件載入：每個資料庫轉成 [{id, text, payload}] ─────────────────────
def _yaml(name):
    return yaml.safe_load(open(os.path.join(KDIR, name), encoding="utf-8")) or []


_CAT_CACHE: Dict[str, List[Dict]] = {}
DROP_SOFTWARE: set = set()   # 實驗用：檢索結果中略過這些軟體（不改索引）


def _catalog_entries() -> List[Dict]:
    p = CATALOG_FILE
    if p not in _CAT_CACHE:
        _CAT_CACHE[p] = json.load(open(p, encoding="utf-8"))["entries"] if os.path.exists(p) else []
    return _CAT_CACHE[p]


def _split_sections(text: str, max_chars: int = 700) -> List[str]:
    parts, out = re.split(r"\n(?=#{1,4} )", text), []
    for p in parts:
        p = p.strip()
        while len(p) > max_chars:
            cut = p.rfind("\n", 0, max_chars)
            cut = cut if cut > max_chars // 2 else max_chars
            out.append(p[:cut].strip())
            p = p[cut:].strip()
        if len(p) > 40:
            out.append(p)
    return out


SCEN_DROP_FILE = os.environ.get("RAG_SCEN_DROP")   # 實驗用：要排除的情境描述清單（JSON 陣列，元素為 "軟體id#序號"）


def _scen(e: Dict) -> List[str]:
    if not SCEN_DROP_FILE:
        return e["scenarios"]
    if SCEN_DROP_FILE not in _CAT_CACHE:
        _CAT_CACHE[SCEN_DROP_FILE] = set(json.load(open(SCEN_DROP_FILE, encoding="utf-8")))
    drop = _CAT_CACHE[SCEN_DROP_FILE]
    kept = [s for j, s in enumerate(e["scenarios"]) if f"{e['id']}#{j}" not in drop]
    return kept or e["scenarios"][:1]


def load_docs() -> Dict[str, List[Dict]]:
    docs: Dict[str, List[Dict]] = {}
    cat = _catalog_entries()
    docs["catalog"] = [{"id": f"catalog#{e['id']}",
                        "text": f"{e['category']}：{'、'.join(_scen(e))}。{e.get('notes', '')}",
                        "payload": e} for e in cat]
    # 分層實驗用：人工目錄與自動擴充目錄各自獨立建索引（只有目錄含自動擴充條目時才產生）
    if any(e.get("generated") for e in cat):
        docs["catalog_seed"] = [d for d in docs["catalog"] if not d["payload"].get("generated")]
        docs["catalog_ext"] = [d for d in docs["catalog"] if d["payload"].get("generated")]
    # 多重表示：每句情境描述一筆（短句查詢對短句文件，比對整串描述更準）
    docs["catalog_scen"] = [{"id": f"scen#{e['id']}#{j}", "text": f"{s}（{e['category']}）", "payload": e}
                            for e in cat for j, s in enumerate(_scen(e))]
    if "catalog_seed" in docs:
        docs["catalog_scen_seed"] = [d for d in docs["catalog_scen"] if not d["payload"].get("generated")]
    ex_path = os.path.join(KDIR, "examples.jsonl")
    exs = [json.loads(x) for x in open(ex_path, encoding="utf-8") if x.strip()] if os.path.exists(ex_path) else []
    docs["examples"] = [{"id": f"example#{i}", "text": r["text"], "payload": r} for i, r in enumerate(exs)]
    reqs = []
    for r in _yaml("requirements.yaml"):
        for j, cue in enumerate(r["cues"]):
            reqs.append({"id": f"req#{r['need']}#{j}", "text": f"{cue}（{r['title']}）", "payload": r})
    docs["requirements"] = reqs
    docs["lessons"] = [{"id": f"lesson#{r['id']}", "text": f"{r['title']}。情況：{r['trigger']}。正確做法：{r['correct']}",
                        "payload": r} for r in _yaml("lessons.yaml")]
    docs["troubleshoot"] = [{"id": f"trouble#{r['id']}",
                             "text": f"{r['symptom']}：{r['plain']}。原因：{'；'.join(r['causes'])}",
                             "payload": r} for r in _yaml("troubleshooting.yaml")]
    know = []
    for folder in (os.path.join(HERE, "k8s_docs"), os.path.join(KDIR, "k8s_official")):
        if not os.path.isdir(folder):
            continue
        for fn in sorted(os.listdir(folder)):
            if not fn.endswith(".md"):
                continue
            text = open(os.path.join(folder, fn), encoding="utf-8").read()
            title = text.splitlines()[0].lstrip("# ").strip() if text else fn
            for k, sec in enumerate(_split_sections(text)):
                # Contextual Retrieval：段落前加上文件標題
                know.append({"id": f"know#{fn}#{k}", "text": f"[{title}] {sec}",
                             "payload": {"source": fn, "title": title, "text": sec}})
    docs["knowledge"] = know
    return docs


# ── BM25（中文雙字＋英文單字） ─────────────────────────────────────────
def _tokens(s: str) -> List[str]:
    s = s.lower()
    toks = re.findall(r"[a-z0-9][a-z0-9_.\-]*", s)
    for run in re.findall(r"[一-鿿]+", s):
        toks += [run[i:i + 2] for i in range(len(run) - 1)] or [run]
    return toks


class _BM25:
    def __init__(self, texts: List[str], k1: float = 1.5, b: float = 0.75):
        self.docs = [Counter(_tokens(t)) for t in texts]
        self.len = [sum(d.values()) for d in self.docs]
        self.avg = (sum(self.len) / len(self.len)) if self.len else 1.0
        df = Counter()
        for d in self.docs:
            df.update(d.keys())
        n = len(self.docs)
        self.idf = {w: math.log(1 + (n - c + 0.5) / (c + 0.5)) for w, c in df.items()}
        self.k1, self.b = k1, b

    def scores(self, query: str) -> List[float]:
        q = _tokens(query)
        out = []
        for d, L in zip(self.docs, self.len):
            s = 0.0
            for w in q:
                f = d.get(w)
                if f:
                    s += self.idf.get(w, 0) * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * L / self.avg))
            out.append(s)
        # 正規化到 0～1，方便設門檻
        top = max(out) if out else 0
        qmax = sum(self.idf.get(w, 0) * (self.k1 + 1) for w in set(q)) or 1
        return [s / qmax if top else 0.0 for s in out]


def _has_collection(name: str) -> bool:
    """目前的目錄有沒有這個資料庫；結果跟著 _CACHE 一起清除（切換目錄時重新判斷）。"""
    key = f"__has__{name}"
    if key not in _CACHE:
        _CACHE[key] = {"v": name in load_docs()}
    return _CACHE[key]["v"]


# ── 向量 ─────────────────────────────────────────────────────────────
_model = None


def _embedder():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        try:
            from rag.vector_store import EMBED_MODEL_NAME, get_device
            device = get_device()
        except Exception:  # noqa: BLE001
            EMBED_MODEL_NAME, device = "BAAI/bge-m3", "cpu"
        kw: dict = {"model_kwargs": {"dtype": "float16"}} if str(device).startswith("cuda") else {}
        _model = SentenceTransformer(EMBED_MODEL_NAME, device=device, **kw)
    return _model


def embed(texts: List[str]):
    import numpy as np
    v = _embedder().encode(texts, normalize_embeddings=True, batch_size=32, show_progress_bar=False)
    return np.asarray(v, dtype="float32")


def dense_available() -> bool:
    try:
        import numpy  # noqa: F401
        import sentence_transformers  # noqa: F401
    except ImportError:
        return False
    return os.path.isdir(INDEX_DIR)


_m3 = None


def m3sparse_available() -> bool:
    try:
        import FlagEmbedding  # noqa: F401
        return True
    except ImportError:
        return False


def sparse_encode(texts: List[str]) -> List[Dict[str, float]]:
    """BGE-M3 的關鍵字權重（token id → 權重），與向量同一個模型、另外載入 FlagEmbedding 版。"""
    global _m3
    if _m3 is None:
        from FlagEmbedding import BGEM3FlagModel
        try:
            from rag.vector_store import get_device
            device = str(get_device())
        except Exception:  # noqa: BLE001
            device = "cpu"
        dev = "cuda:0" if device.startswith("cuda") else "cpu"
        _m3 = BGEM3FlagModel("BAAI/bge-m3", use_fp16=dev != "cpu", devices=dev)
    out = _m3.encode(texts, batch_size=32, return_dense=False, return_sparse=True)["lexical_weights"]
    return [{k: float(v) for k, v in d.items()} for d in out]


def _sparse_scores(qw: Dict[str, float], docs_w: List[Dict[str, float]]) -> List[float]:
    return [sum(w * d.get(t, 0.0) for t, w in qw.items()) for d in docs_w]


def build_index(verbose: bool = True) -> Dict[str, int]:
    """把六個資料庫的文字轉成向量存到 rag/field_index/（可重新產生的產出物）。"""
    import numpy as np
    os.makedirs(INDEX_DIR, exist_ok=True)
    counts = {}
    for name, ds in load_docs().items():
        if not ds:
            continue
        t0 = time.time()
        np.save(os.path.join(INDEX_DIR, f"{name}.npy"), embed([d["text"] for d in ds]))
        json.dump([d["id"] for d in ds], open(os.path.join(INDEX_DIR, f"{name}.ids.json"), "w", encoding="utf-8"))
        if m3sparse_available():
            json.dump(sparse_encode([d["text"] for d in ds]),
                      open(os.path.join(INDEX_DIR, f"{name}.sparse.json"), "w", encoding="utf-8"))
        counts[name] = len(ds)
        if verbose:
            print(f"  {name}: {len(ds)} 筆，{time.time() - t0:.1f} 秒", flush=True)
    _CACHE.clear()
    return counts


# ── 檢索核心 ─────────────────────────────────────────────────────────
_CACHE: Dict[str, Dict] = {}


def _collection(name: str) -> Dict:
    if name in _CACHE:
        return _CACHE[name]
    docs = load_docs()[name]
    col = {"docs": docs, "bm25": _BM25([d["text"] for d in docs]), "vec": None}
    npy, ids = os.path.join(INDEX_DIR, f"{name}.npy"), os.path.join(INDEX_DIR, f"{name}.ids.json")
    if os.path.exists(npy) and os.path.exists(ids):
        import numpy as np
        same = json.load(open(ids, encoding="utf-8")) == [d["id"] for d in docs]
        if not same and os.environ.get("RAG_STRICT_INDEX") == "1":
            raise RuntimeError(f"{name} 的向量索引與資料不一致（{INDEX_DIR}），請先重建索引")
        if same:
            col["vec"] = np.load(npy)
            sp = os.path.join(INDEX_DIR, f"{name}.sparse.json")
            col["sparse"] = json.load(open(sp, encoding="utf-8")) if os.path.exists(sp) else None
    _CACHE[name] = col
    return col


_QCACHE: Dict[tuple, object] = {}


def _query_vec(q: str):
    """同一句查詢在一次評估裡會查好幾個資料庫，向量只算一次。"""
    k = ("d", q)
    if k not in _QCACHE:
        _QCACHE[k] = embed([q])[0]
    return _QCACHE[k]


def _query_sparse(q: str):
    k = ("s", q)
    if k not in _QCACHE:
        _QCACHE[k] = sparse_encode([q])[0]
    return _QCACHE[k]


def search(name: str, query: str, top_k: int = 5, method: Optional[str] = None) -> List[Dict]:
    """回傳 [{id, text, payload, score}]，依分數排序；分數尺度依方法不同。"""
    method = method or DEFAULT_METHOD
    col = _collection(name)
    docs = col["docs"]
    if not docs:
        return []
    if method in ("dense", "hybrid", "hybrid_rerank") and col["vec"] is None:
        method = "bm25"   # 沒有向量索引（例如 CI）→ 退回 BM25
    pool = max(top_k * CFG["pool_mult"], 20)
    if method == "bm25":
        sc = col["bm25"].scores(query)
        order = sorted(range(len(docs)), key=lambda i: -sc[i])[:top_k]
        return [{**docs[i], "score": round(sc[i], 4)} for i in order]
    qv = _query_vec(query)
    dense = (col["vec"] @ qv).tolist()
    if method == "dense":
        order = sorted(range(len(docs)), key=lambda i: -dense[i])[:top_k]
        return [{**docs[i], "score": round(dense[i], 4)} for i in order]
    if CFG["lexical"] == "m3sparse" and col.get("sparse") is not None:
        bm = _sparse_scores(_query_sparse(query), col["sparse"])
    else:
        bm = col["bm25"].scores(query)
    rd = sorted(range(len(docs)), key=lambda i: -dense[i])[:pool]
    rb = [i for i in sorted(range(len(docs)), key=lambda i: -bm[i])[:pool] if bm[i] > 0]
    fused = defaultdict(float)
    for rank_list, w in ((rd, CFG["w_dense"]), (rb, CFG["w_lex"])):
        for r, i in enumerate(rank_list):
            fused[i] += w / (CFG["rrf_k"] + r + 1)
    order = sorted(fused, key=lambda i: -fused[i])
    if method == "hybrid":
        return [{**docs[i], "score": round(fused[i], 5)} for i in order[:top_k]]
    from rag.reranker import rerank  # hybrid_rerank
    cands = [{**docs[i], "score": fused[i]} for i in order[:pool]]
    return rerank(query, cands, top_k)


# ── 對外介面 ─────────────────────────────────────────────────────────
def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


_ALIASES = {"mongodb": "mongo", "postgre": "postgres", "postgresql": "postgres", "pg": "postgres",
            "wiki.js": "wikijs", "wikijs": "wikijs", "es": "elasticsearch", "rabbit": "rabbitmq"}


def lookup_software(name_or_image: str) -> Optional[Dict]:
    """已知軟體或映像檔名稱時直接查目錄。"""
    key = _norm(name_or_image.split(":")[0].split("/")[-1])
    key = _norm(_ALIASES.get(key, key))
    for e in _catalog_entries():
        if key in (_norm(e["id"]), _norm(e["image"].split(":")[0].split("/")[-1])):
            return e
    return None


def _value_for(field: str, e: Dict):
    if field == "image":
        return e["image"]
    if field == "port":
        return e["ports"][0] if e.get("ports") else None
    if field == "volume":
        return e["volumes"][0] if e.get("volumes") else None
    if field == "env":
        return {"required": e.get("env_required", []), "common": e.get("env_common", {}),
                "standalone_env": e.get("standalone_env", {})}
    if field in ("memory", "cpu"):
        return (e.get("resources") or {}).get(field)
    if field == "replicas":
        return {"per_replica_users": e.get("per_replica_users"), "stateful": e.get("per_replica_users") == 0}
    return None


def _software_hits(query: str, top_k: int, method: str) -> List[Dict]:
    """軟體目錄與情境範例一起查，依軟體彙整：分數取兩者較高者。"""
    agg: Dict[str, Dict] = {}
    for h in search("catalog", query, top_k=top_k * 2, method=method):
        e = h["payload"]
        agg[e["id"]] = {"entry": e, "score": h["score"], "evidence": f"軟體目錄 {e['id']}：{'、'.join(e['scenarios'][:3])}"}
    if CFG["multi_rep"]:
        for h in search("catalog_scen", query, top_k=top_k * 4, method=method):
            e = h["payload"]
            if e["id"] not in agg or h["score"] > agg[e["id"]]["score"]:
                agg[e["id"]] = {"entry": e, "score": h["score"], "evidence": f"軟體目錄 {e['id']}：{h['text']}"}
    for h in (search("examples", query, top_k=top_k * 3, method=method) if CFG["examples"] else []):
        sid = h["payload"].get("image_id")
        e = next((c for c in _catalog_entries() if c["id"] == sid), None)
        if e and (sid not in agg or h["score"] > agg[sid]["score"]):
            agg[sid] = {"entry": e, "score": h["score"], "evidence": f"相近範例：「{h['text'][:40]}」"}
    if CFG.get("split_delta") is not None and CFG.get("split_delta") >= 0 and _has_collection("catalog_seed"):
        # 分層（獨立索引）：人工目錄的排序與只有人工目錄時完全相同；
        # 擴充條目只有在向量相似度比人工最佳結果高出 split_delta 時，才排到最前面
        agg = {}
        for h in search("catalog_seed", query, top_k=top_k * 2, method=method):
            e = h["payload"]
            agg[e["id"]] = {"entry": e, "score": h["score"], "evidence": f"軟體目錄 {e['id']}：{'、'.join(e['scenarios'][:3])}"}
        for h in (search("examples", query, top_k=top_k * 3, method=method) if CFG["examples"] else []):
            sid = h["payload"].get("image_id")
            e = next((c for c in _catalog_entries() if c["id"] == sid), None)
            if e and (sid not in agg or h["score"] > agg[sid]["score"]):
                agg[sid] = {"entry": e, "score": h["score"], "evidence": f"相近範例：「{h['text'][:40]}」"}
        if CFG["multi_rep"]:
            for h in search("catalog_scen_seed", query, top_k=top_k * 4, method=method):
                e = h["payload"]
                if e["id"] not in agg or h["score"] > agg[e["id"]]["score"]:
                    agg[e["id"]] = {"entry": e, "score": h["score"], "evidence": f"軟體目錄 {e['id']}：{h['text']}"}
        seed_rank = sorted(agg.values(), key=lambda x: -x["score"])
        sd = search("catalog_seed", query, top_k=1, method="dense")
        best_seed = sd[0]["score"] if sd else 0.0
        ext = [h for h in search("catalog_ext", query, top_k=top_k, method="dense")
               if h["score"] >= best_seed + CFG["split_delta"] and h["payload"]["id"] not in DROP_SOFTWARE]
        return [{"entry": h["payload"], "score": h["score"], "evidence": f"軟體目錄 {h['payload']['id']}（擴充）"}
                for h in ext] + seed_rank
    if CFG.get("tiered_margin"):
        # 分層：人工目錄另外查一次（候選不會被擴充條目擠掉），擴充條目要比人工最佳分數高出 margin 倍才保留
        seed_ids = {e["id"] for e in _catalog_entries() if not e.get("generated")}
        for h in search("catalog_scen" if CFG["multi_rep"] else "catalog", query, top_k=200, method=method):
            e = h["payload"]
            if e["id"] in seed_ids and (e["id"] not in agg or h["score"] > agg[e["id"]]["score"]):
                agg[e["id"]] = {"entry": e, "score": h["score"], "evidence": f"軟體目錄 {e['id']}"}
            if len([k for k in agg if k in seed_ids]) >= top_k * 2:
                break
        best_seed = max((h["score"] for k, h in agg.items() if k in seed_ids), default=0)
        for k in [k for k, h in agg.items() if k not in seed_ids and h["score"] < best_seed * CFG["tiered_margin"]]:
            agg.pop(k)
    for sid in DROP_SOFTWARE:
        agg.pop(sid, None)
    if CFG.get("prior_w"):
        # 主流優先：人工目錄 1.0、官方映像檔 0.6、自動擴充依星數 0～0.5（10 萬星約 0.5）
        for h in agg.values():
            e = h["entry"]
            if not e.get("generated"):
                p = 1.0
            elif e.get("official"):
                p = 0.6
            else:
                p = min(0.5, math.log10(((e.get("upstream") or {}).get("stars") or 0) + 1) / 10)
            h["score"] = h["score"] * (1 + CFG["prior_w"] * p)
    ranked = sorted(agg.values(), key=lambda x: -x["score"])
    if CFG.get("cat_first") and ranked:
        # 先判斷類別：前 cat_k 個候選依類別加總分數，最高的類別排前面，類別內仍依分數
        votes: Dict[str, float] = defaultdict(float)
        for h in ranked[:CFG.get("cat_k", 10)]:
            votes[h["entry"].get("category", "")] += h["score"]
        best = max(votes, key=votes.get)
        ranked = [h for h in ranked if h["entry"].get("category") == best] +                  [h for h in ranked if h["entry"].get("category") != best]
    return ranked


def retrieve_for_field(query: str, field: str, top_k: int = 3, version: Optional[str] = None,
                       method: Optional[str] = None) -> List[Dict]:
    """依使用者描述找欄位的候選值。分數低於門檻回傳 []（推理代理改為反問）。
    回傳 [{"value", "field", "score", "evidence", "source", "related"}]。app_name 不由 RAG 決定。"""
    if field not in FIELDS_FROM_SOFTWARE:
        return []
    method = method or DEFAULT_METHOD
    if version:  # 呼叫端已知軟體與版本 → 直接查目錄
        e = lookup_software(version)
        if e:
            return [{"value": _value_for(field, e), "field": field, "score": 1.0, "evidence": f"軟體目錄 {e['id']}",
                     "source": f"catalog#{e['id']}", "related": _related(e)}]
    thr = MIN_SCORE.get(method, 0.0)
    out = []
    for h in _software_hits(query, top_k, method):
        if h["score"] < thr:
            continue
        e = h["entry"]
        out.append({"value": _value_for(field, e), "field": field, "score": round(h["score"], 4),
                    "evidence": h["evidence"], "source": f"catalog#{e['id']}", "related": _related(e)})
        if len(out) >= top_k:
            break
    return out


def _related(e: Dict) -> Dict:
    return {"image": e["image"], "ports": e.get("ports"), "volumes": e.get("volumes"), "standalone": e.get("standalone"),
            "requires": e.get("requires", []), "env_required": e.get("env_required", []),
            "standalone_env": e.get("standalone_env", {}), "resources": e.get("resources"),
            "per_replica_users": e.get("per_replica_users"), "notes": e.get("notes", "")}


def retrieve_requirements(query: str, top_k: int = 5, method: Optional[str] = None) -> List[Dict]:
    """句子暗示了哪些額外需求。回傳 [{"need", "title", "score", "evidence", "setting", "k8s", "values", "confirm"}]。"""
    method = method or DEFAULT_METHOD
    thr = MIN_SCORE.get(method, 0.0)
    best: Dict[str, Dict] = {}
    for h in search("requirements", query, top_k=top_k * 4, method=method):
        r = h["payload"]
        if h["score"] >= thr and (r["need"] not in best or h["score"] > best[r["need"]]["score"]):
            best[r["need"]] = {"need": r["need"], "title": r["title"], "score": round(h["score"], 4),
                               "evidence": f"需求通則：{h['text']}", "setting": r["setting"], "k8s": r["k8s"],
                               "values": r.get("values"), "confirm": r.get("confirm", True)}
    return sorted(best.values(), key=lambda x: -x["score"])[:top_k]


def retrieve_lessons(query: str, stage: Optional[str] = None, top_k: int = 3, method: Optional[str] = None) -> List[Dict]:
    """與目前情況相近的失敗經驗，給推理代理避免重犯。"""
    out = []
    for h in search("lessons", query, top_k=top_k * 3, method=method):
        r = h["payload"]
        if stage and r.get("stage") != stage:
            continue
        out.append({"id": r["id"], "title": r["title"], "correct": r["correct"], "wrong": r["wrong"],
                    "fields": r.get("fields", []), "status": r.get("status"), "score": round(h["score"], 4)})
        if len(out) >= top_k:
            break
    return out


def retrieve_knowledge(query: str, kind: str = "concept", top_k: int = 3, method: Optional[str] = None) -> List[Dict]:
    """一般知識（concept）或疑難排解（troubleshoot）。"""
    if kind == "troubleshoot":
        return [{"symptom": h["payload"]["symptom"], "plain": h["payload"]["plain"], "causes": h["payload"]["causes"],
                 "fix": h["payload"]["fix"], "score": round(h["score"], 4), "source": h["id"]}
                for h in search("troubleshoot", query, top_k=top_k, method=method)]
    return [{"text": h["payload"]["text"], "title": h["payload"]["title"], "source": h["payload"]["source"],
             "score": round(h["score"], 4)} for h in search("knowledge", query, top_k=top_k, method=method)]


def log_feedback(query: str, field: str, suggested, final, method: Optional[str] = None) -> None:
    """確認卡回饋：建議值被使用者接受（final == suggested）或修改。之後多臂老虎機的獎勵來源。"""
    os.makedirs(os.path.dirname(FEEDBACK_PATH), exist_ok=True)
    with open(FEEDBACK_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "query": query, "field": field,
                            "suggested": suggested, "final": final, "accepted": suggested == final,
                            "method": method or DEFAULT_METHOD}, ensure_ascii=False) + "\n")
