"""
PROMPTx-RAG 步驟 3：建立「題目 × 擾動版本 × 檢索策略」離線結果表（2026-11-15）

對應 PROMPTx-PE（Khan et al., CMC 2026）的做法：每個策略（臂）對每題的準確、穩健、效率先算好存檔，
之後的策略學習（rag/policy.py）在這張表上離線重放，不必重跑檢索。

題組：Gemini 開發集（dev_scenario.jsonl）、Claude 開發集（dev_claude.jsonl），只取不需反問的題目
（反問規則等多代理架構合併後再做）。擾動版本：dev_perturb.jsonl（錯字、極短、囉嗦、中英夾雜）。
目錄：人工 36 種＋範本版擴充（D:\\rag_sources\\stage\\catalog_tmpl.json），人工與擴充各自獨立索引，
所以「只查人工目錄」的臂與現行正式版逐題相同（tests 與 sanity 指令會檢查）。
評估時關閉分數門檻（MIN_SCORE 設為 -1）：這裡只比「找不找得到」，放棄與反問之後另外處理。

  python rag/strategy_eval.py build      # 建表（可中斷，重跑會接續）→ docs/rag_promptx/strategy_table.jsonl
  python rag/strategy_eval.py sanity     # 檢查：只查人工目錄的臂 = 現行正式版
"""
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("RAG_RERANK_DEVICE", "cuda")
from rag import eval_fields as ev  # noqa: E402
from rag import field_retriever as fr  # noqa: E402

SRC = Path(os.environ.get("RAG_SOURCES", r"D:\rag_sources"))
OUT = ROOT / "docs" / "rag_promptx"
TABLE = OUT / "strategy_table.jsonl"
FEATS = OUT / "context_features.jsonl"
REWRITE_CACHE = ROOT / "data" / "rag_rewrite_cache.json"
BB = ROOT / "docs" / "bench_blind"
SETS = {"gemini": BB / "dev_scenario.jsonl", "claude": BB / "dev_claude.jsonl"}
CATALOG = (str(SRC / "stage/catalog_tmpl.json"), str(SRC / "index_tmpl"))
SEED_ONLY = 9.0          # split_delta 大到擴充條目永遠進不來 = 只查人工目錄

# 檢索策略（臂）：名稱 → (CFG 差異, 方法, 是否先用本機 Qwen 改寫查詢)
ARMS = {
    "seed_hybrid":    ({"split_delta": SEED_ONLY}, "hybrid", False),        # 現行正式版
    "seed_hybrid_mr": ({"split_delta": SEED_ONLY, "multi_rep": True}, "hybrid", False),
    "seed_dense":     ({"split_delta": SEED_ONLY}, "dense", False),
    "seed_bm25":      ({"split_delta": SEED_ONLY}, "bm25", False),
    "seed_rerank":    ({"split_delta": SEED_ONLY}, "hybrid_rerank", False),
    "seed_rewrite":   ({"split_delta": SEED_ONLY}, "hybrid", True),
    "ext_d03":        ({"split_delta": 0.03}, "hybrid", False),             # 擴充條目向量分數高出人工 0.03 才進來
    "ext_d06":        ({"split_delta": 0.06}, "hybrid", False),
    # 11/15 事後追加（探索性）：結果表顯示 multi_rep 與 ext_d03 各自小幅有利，測兩者合併；
    # 是看過同一批題目才提出的假設，報告標成探索性，需用盲測集確認
    "ext_d03_mr":     ({"split_delta": 0.03, "multi_rep": True}, "hybrid", False),
    "merged_prior":   ({"split_delta": None, "prior_w": 1.0}, "hybrid", False),   # 全部混在同一索引＋主流優先
}
# 重排序每句約 8 秒（11/15 實測，GPU），效能分數為 0，且 10/08、11/15 兩次實驗都沒有提升準確度：
# 只跑原句、保留在報告，不列入策略學習的候選（rag/policy.py EXCLUDE_ARMS）
ORIG_ONLY = {"seed_rerank"}
REWRITE_PROMPT = ("請把使用者的需求改寫成一行「服務類型：主要用途」的簡短描述（不超過 25 字，不要推薦軟體名稱、"
                  "不要解釋），只輸出那一行。\n需求：{}")


def use_catalog():
    fr.CATALOG_FILE, fr.INDEX_DIR, fr.SCEN_DROP_FILE = CATALOG[0], CATALOG[1], None
    fr._CACHE.clear()
    fr._CAT_CACHE.clear()
    if not os.path.exists(os.path.join(CATALOG[1], "catalog_seed.ids.json")):
        print(f"  建立索引 → {CATALOG[1]}", flush=True)
        fr.build_index(verbose=False)
    os.environ["RAG_STRICT_INDEX"] = "1"
    for k in fr.MIN_SCORE:
        fr.MIN_SCORE[k] = -1.0


_RW = None


def rewrite(text):
    """本機 Qwen 3B（model server /chat）改寫查詢；結果與實際耗時一起快取。"""
    global _RW
    if _RW is None:
        _RW = json.load(open(REWRITE_CACHE, encoding="utf-8")) if REWRITE_CACHE.exists() else {}
    key = hashlib.md5(text.encode("utf-8")).hexdigest()
    if key not in _RW:
        import requests
        t0 = time.time()
        r = requests.post("http://127.0.0.1:8765/chat", json={"message": REWRITE_PROMPT.format(text),
                                                               "max_new_tokens": 48, "temperature": 0.0}, timeout=120)
        _RW[key] = {"rewrite": (r.json().get("reply") or "").strip().splitlines()[0][:60],
                    "ms": round((time.time() - t0) * 1000, 1)}
        REWRITE_CACHE.parent.mkdir(parents=True, exist_ok=True)
        json.dump(_RW, open(REWRITE_CACHE, "w", encoding="utf-8"), ensure_ascii=False)
    return _RW[key]


def run_arm(arm, text, accept, need_exp):
    delta, method, use_rw = ARMS[arm]
    fr.CFG.clear()
    fr.CFG.update({**BASE, **delta})
    extra_ms, q = 0.0, text
    if use_rw:
        rw = rewrite(text)
        q, extra_ms = f"{rw['rewrite']}。{text}", rw["ms"]
    fr._QCACHE.clear()
    t0 = time.time()
    cands = fr.retrieve_for_field(q, "image", top_k=3, method=method)
    needs = [n["need"] for n in fr.retrieve_requirements(text, top_k=5, method=method)]
    ms = (time.time() - t0) * 1000 + extra_ms
    names = [ev.cand_names(c["value"], c.get("related", {}), c.get("source", "")) for c in cands]
    return {"hit1": bool(names) and bool(names[0] & accept), "hit3": any(n & accept for n in names[:3]),
            "need_recall": (len(need_exp & set(needs)) / len(need_exp)) if need_exp else None,
            "top": [c["source"].split("#", 1)[-1] for c in cands], "latency_ms": round(ms, 1)}


BASE = dict(fr.CFG)
LATIN = re.compile(r"[A-Za-z]{2,}")


def context_features(text):
    """PROMPTx-PE 式 3 的情境（領域、任務）：只用句子本身與目錄分數，不用標準答案。"""
    fr.CFG.clear()
    fr.CFG.update(BASE)
    sd = fr.search("catalog_seed", text, top_k=2, method="dense")
    ed = fr.search("catalog_ext", text, top_k=1, method="dense")
    names = {fr._norm(e["id"]) for e in fr._catalog_entries()} | {
        fr._norm((e.get("upstream") or {}).get("name", "")) for e in fr._catalog_entries()}
    toks = {fr._norm(t) for t in LATIN.findall(text)}
    rq = fr.search("requirements", text, top_k=1, method="bm25")
    return {"len": len(text), "latin_ratio": round(sum(len(t) for t in LATIN.findall(text)) / max(1, len(text)), 3),
            "name_hit": int(bool(toks & (names - {""}))), "digits": len(re.findall(r"\d", text)),
            "seed_top": sd[0]["score"] if sd else 0.0,
            "seed_margin": (sd[0]["score"] - sd[1]["score"]) if len(sd) > 1 else 0.0,
            "ext_minus_seed": (ed[0]["score"] - sd[0]["score"]) if sd and ed else 0.0,
            "req_bm25": rq[0]["score"] if rq else 0.0}


def load_tasks():
    tasks = []
    for src, path in SETS.items():
        if not path.exists():
            continue
        for x in open(path, encoding="utf-8"):
            if not x.strip().startswith("{"):
                continue
            t = json.loads(x)
            if t.get("expected", {}).get("image", {}).get("must_ask"):
                continue
            accept = {ev.norm(a) for a in (t["expected"].get("image", {}).get("accept") or [])}
            if not accept:
                continue
            t["_src"], t["_accept"] = src, accept
            t["_needs"] = {e.get("need") for e in (t.get("extra") or []) if e.get("need") in ev.fr_needs()}
            tasks.append(t)
    pert = {}
    pp = BB / "dev_perturb.jsonl"
    if pp.exists():
        for x in open(pp, encoding="utf-8"):
            if x.strip():
                r = json.loads(x)
                pert.setdefault(r["id"], {})[r["kind"]] = r["text"]
    return tasks, pert


def build():
    use_catalog()
    OUT.mkdir(parents=True, exist_ok=True)
    tasks, pert = load_tasks()
    done = set()
    if TABLE.exists():
        for x in open(TABLE, encoding="utf-8"):
            r = json.loads(x)
            done.add((r["id"], r["variant"], r["arm"]))
    fdone = {json.loads(x)["key"] for x in open(FEATS, encoding="utf-8")} if FEATS.exists() else set()
    print(f"題目 {len(tasks)}（有擾動版本 {sum(t['id'] in pert for t in tasks)}），臂 {len(ARMS)}", flush=True)
    t0 = time.time()
    with open(TABLE, "a", encoding="utf-8") as ft, open(FEATS, "a", encoding="utf-8") as ff:
        for i, t in enumerate(tasks):
            variants = {"orig": t["text"], **pert.get(t["id"], {})}
            for var, text in variants.items():
                key = f"{t['id']}|{var}"
                if key not in fdone:
                    ff.write(json.dumps({"key": key, "id": t["id"], "variant": var, "src": t["_src"],
                                         **context_features(text)}, ensure_ascii=False) + "\n")
                for arm in ARMS:
                    if var != "orig" and arm in ORIG_ONLY:
                        continue
                    if (t["id"], var, arm) in done:
                        continue
                    r = run_arm(arm, text, t["_accept"], t["_needs"])
                    ft.write(json.dumps({"id": t["id"], "src": t["_src"], "difficulty": t.get("difficulty"),
                                         "variant": var, "arm": arm, **r}, ensure_ascii=False) + "\n")
            ft.flush()
            ff.flush()
            if (i + 1) % 20 == 0:
                print(f"  {i + 1}/{len(tasks)}，{time.time() - t0:.0f} 秒", flush=True)


def sanity():
    """只查人工目錄的臂必須與現行正式版（36 種目錄、rag/field_index）逐題相同。"""
    tasks, _ = load_tasks()
    tasks = tasks[:60]
    cur = []
    fr.CATALOG_FILE, fr.INDEX_DIR = str(ROOT / "rag/knowledge/catalog.json"), str(ROOT / "rag/field_index")
    fr._CACHE.clear()
    fr._CAT_CACHE.clear()
    for k in fr.MIN_SCORE:
        fr.MIN_SCORE[k] = -1.0
    for mr in (False, True):
        fr.CFG.clear()
        fr.CFG.update({**BASE, "multi_rep": mr})
        cur.append([[c["source"] for c in fr.retrieve_for_field(t["text"], "image", top_k=3, method="hybrid")]
                    for t in tasks])
    use_catalog()
    ok = True
    for idx, arm in enumerate(("seed_hybrid", "seed_hybrid_mr")):
        fr.CFG.clear()
        fr.CFG.update({**BASE, **ARMS[arm][0]})
        new = [[c["source"] for c in fr.retrieve_for_field(t["text"], "image", top_k=3, method="hybrid")]
               for t in tasks]
        same = sum(a == b for a, b in zip(cur[idx], new))
        print(f"{arm}：{same}/{len(tasks)} 題前 3 名與現行版本相同")
        ok &= same == len(tasks)
    print("通過" if ok else "不一致，請先查程式")
    return ok


if __name__ == "__main__":
    {"build": build, "sanity": sanity}[sys.argv[1]]()
