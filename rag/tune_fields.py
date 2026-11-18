"""
RAG 欄位檢索調參與對照實驗（2026-11-15）

開發集 200 題依題號雜湊固定分成：調整用 140 題（tune）、驗證用 60 題（holdout）。只用 tune 挑設定，
holdout 只拿來確認沒有過度貼合；盲測集不碰。反問規則不在這裡調（等多代理架構合併後再做），
所以只算不需反問的題目。

指標（每題）：hit1／hit3（第 1／前 3 名在標準答案清單內）、cat1（第 1 名與標準答案同類別，
類別一律查 278 種擴充目錄＋人工目錄，標準答案不在任何目錄的題目不計）、需求召回。
判定規則（寫死在程式，避免事後挑結果）：
  採用 ⇔ tune 的 hit1 或 hit3 比基準高，且該指標的 McNemar 精確檢定 p < 0.05，
         且 holdout 同一指標不低於基準，且另一個指標在 tune 不顯著變差。

  python rag/tune_fields.py ablation
  python rag/tune_fields.py optuna --trials 60 --catalog 278
輸出：docs/rag_tune_20261115/{ablation.jsonl, ablation_rows.jsonl, optuna.jsonl, best.json}
"""
import argparse
import hashlib
import json
import math
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from rag import eval_fields as ev  # noqa: E402
from rag import field_retriever as fr  # noqa: E402

OUT = ROOT / "docs" / "rag_tune_20261115"
SRC = Path(os.environ.get("RAG_SOURCES", r"D:\rag_sources"))
BASE = dict(fr.CFG)
CATALOGS = {  # 名稱: (目錄檔, 索引資料夾, 要排除的描述清單)
    "36": (str(ROOT / "rag/knowledge/catalog.json"), str(ROOT / "rag/field_index"), None),
    "278": (str(SRC / "stage/catalog_278.json"), str(SRC / "index_278"), None),
    "278_f20": (str(SRC / "stage/catalog_278.json"), str(SRC / "index_278_f20"), str(SRC / "filter/scen_drop_0.2.json")),
    "278_f35": (str(SRC / "stage/catalog_278.json"), str(SRC / "index_278_f35"), str(SRC / "filter/scen_drop_0.35.json")),
}
# 看過開發集缺哪些軟體才人工指定映像檔的條目（rag/expand_catalog.py OVERRIDES）：另外列出貢獻
OVERRIDE_IDS = {"owncloud", "gitlab", "nodebb", "bookstack", "flarum", "limesurvey", "gogs", "phpbb"}


def use_catalog(name):
    cat, idx, drop = CATALOGS[name]
    fr.CATALOG_FILE, fr.INDEX_DIR, fr.SCEN_DROP_FILE = cat, idx, drop
    fr._CACHE.clear()
    fr._CAT_CACHE.clear()
    if not os.path.exists(os.path.join(idx, "catalog.ids.json")):
        print(f"  建立索引 {name} → {idx}", flush=True)
        fr.build_index(verbose=False)
    os.environ["RAG_STRICT_INDEX"] = "1"


_CATMAP = None


def category_of(sid):
    global _CATMAP
    if _CATMAP is None:
        _CATMAP = {}
        for path in (CATALOGS["278"][0], CATALOGS["36"][0]):
            for e in json.load(open(path, encoding="utf-8"))["entries"]:
                _CATMAP[ev.norm(e["id"])] = e.get("category")
                _CATMAP[ev.norm(e["image"])] = e.get("category")
    return _CATMAP.get(ev.norm(sid))


def split():
    tasks = [json.loads(x) for x in open(ev.DEV, encoding="utf-8") if x.strip().startswith("{")]
    tune, hold = [], []
    for t in tasks:
        (hold if int(hashlib.md5(t["id"].encode()).hexdigest(), 16) % 10 < 3 else tune).append(t)
    return tune, hold


def run(tasks, cfg, method="hybrid"):
    fr.CFG.clear()
    fr.CFG.update(cfg)
    fr._QCACHE.clear()          # 每組設定重新算查詢向量，延遲才可比較
    rows = []
    for t in tasks:
        if t.get("expected", {}).get("image", {}).get("must_ask"):
            continue
        r = ev.eval_one(t, method)
        top = fr._software_hits(t["text"], 3, method)
        tcat = top[0]["entry"].get("category") if top else None
        acats = {category_of(a) for a in r["accept"]} - {None}
        r["cat1"] = (tcat in acats) if acats else None
        r["top1"] = top[0]["entry"]["id"] if top else None
        rows.append(r)
    return rows


def summary(rows):
    def pct(k):
        v = [r[k] for r in rows if r.get(k) is not None]
        return round(100 * sum(v) / len(v), 1) if v else None
    nr = [r["need_recall"] for r in rows if r["need_recall"] is not None]
    lat = sorted(r["latency_ms"] for r in rows)
    return {"n": len(rows), "hit1": pct("image_hit1"), "hit3": pct("image_hit3"), "cat1": pct("cat1"),
            "need_recall": round(100 * sum(nr) / len(nr), 1) if nr else None,
            "latency_p50_ms": lat[len(lat) // 2] if lat else None}


def mcnemar(base_rows, rows, key):
    """配對精確 McNemar：b＝基準對新錯，c＝基準錯新對；回傳 (b, c, 雙尾 p)。"""
    bm = {r["id"]: r[key] for r in base_rows}
    b = sum(1 for r in rows if bm.get(r["id"]) and not r[key])
    c = sum(1 for r in rows if (r["id"] in bm) and not bm[r["id"]] and r[key])
    n = b + c
    if n == 0:
        return b, c, 1.0
    k = min(b, c)
    p = sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n * 2
    return b, c, round(min(1.0, p), 4)


def decide(bt, vt, bh, vh):
    out = {}
    for key, name in (("image_hit1", "hit1"), ("image_hit3", "hit3")):
        out[name] = mcnemar(bt, vt, key)
    st_b, st_v, sh_b, sh_v = summary(bt), summary(vt), summary(bh), summary(vh)
    adopt = False
    for k, other in (("hit1", "hit3"), ("hit3", "hit1")):
        b, c, p = out[k]
        worse_other = out[other][0] > out[other][1] and out[other][2] < 0.05
        if st_v[k] > st_b[k] and p < 0.05 and sh_v[k] >= sh_b[k] and not worse_other:
            adopt = True
    return adopt, out


def ablation():
    tune, hold = split()
    variants = [  # (名稱, 目錄, 設定差異, 方法)；基準＝11/15 正式版
        ("基準：36 種目錄", "36", {}, "hybrid"),
        ("36＋每句描述各自建索引", "36", {"multi_rep": True}, "hybrid"),
        ("36＋關鍵字改用 BGE-M3 權重", "36", {"lexical": "m3sparse"}, "hybrid"),
        ("36＋只用向量", "36", {}, "dense"),
        ("278 種目錄", "278", {}, "hybrid"),
        ("278＋主流優先 0.2", "278", {"prior_w": 0.2}, "hybrid"),
        ("278＋主流優先 1.0", "278", {"prior_w": 1.0}, "hybrid"),
        ("278＋先判斷類別", "278", {"cat_first": True}, "hybrid"),
        ("278＋過濾籠統描述 20%", "278_f20", {}, "hybrid"),
        ("278＋過濾籠統描述 35%", "278_f35", {}, "hybrid"),
        ("278＋關鍵字權重 2 倍", "278", {"w_lex": 2.0}, "hybrid"),
        ("278＋主流優先 1.0＋先判斷類別", "278", {"prior_w": 1.0, "cat_first": True}, "hybrid"),
        ("278 去掉人工指定的 8 種＋主流優先 1.0", "278", {"prior_w": 1.0, "_drop_overrides": True}, "hybrid"),
        ("278＋分層 1.1 倍", "278", {"tiered_margin": 1.1}, "hybrid"),
        ("278＋分層 1.3 倍", "278", {"tiered_margin": 1.3}, "hybrid"),
        ("278＋分層 1.0 倍", "278", {"tiered_margin": 1.0}, "hybrid"),
        ("278＋獨立索引分層 差距 0.00", "278", {"split_delta": 0.0}, "hybrid"),
        ("278＋獨立索引分層 差距 0.03", "278", {"split_delta": 0.03}, "hybrid"),
        ("278＋獨立索引分層 差距 0.06", "278", {"split_delta": 0.06}, "hybrid"),
    ]
    OUT.mkdir(parents=True, exist_ok=True)
    base_t = base_h = None
    with open(OUT / "ablation.jsonl", "w", encoding="utf-8") as f, \
            open(OUT / "ablation_rows.jsonl", "w", encoding="utf-8") as fr_rows:
        for name, cat, delta, method in variants:
            use_catalog(cat)
            fr.DROP_SOFTWARE = OVERRIDE_IDS if delta.get("_drop_overrides") else set()
            cfg = {**BASE, **{k: v for k, v in delta.items() if not k.startswith("_")}}
            t0 = time.time()
            rt, rh = run(tune, cfg, method), run(hold, cfg, method)
            fr.DROP_SOFTWARE = set()
            if base_t is None:
                base_t, base_h = rt, rh
            adopt, tests = decide(base_t, rt, base_h, rh)
            rec = {"variant": name, "catalog": cat, "method": method, "cfg": cfg, "tune": summary(rt),
                   "holdout": summary(rh), "mcnemar_vs_base": tests, "adopt": adopt and name != variants[0][0]}
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            for r in rt + rh:
                fr_rows.write(json.dumps({"variant": name, "split": "tune" if r in rt else "holdout",
                                          **{k: r.get(k) for k in ("id", "difficulty", "accept", "top1", "output",
                                                                   "image_hit1", "image_hit3", "cat1", "need_recall",
                                                                   "latency_ms")}}, ensure_ascii=False) + "\n")
            print(f"{name:28} 調整 {rec['tune']}\n{'':28} 驗證 {rec['holdout']}\n{'':28} McNemar {tests}  "
                  f"{'→ 採用' if rec['adopt'] else ''}（{time.time() - t0:.0f} 秒）", flush=True)
    fr.CFG.clear()
    fr.CFG.update(BASE)


def run_optuna(trials, catalog):
    import optuna
    tune, hold = split()
    use_catalog(catalog)
    OUT.mkdir(parents=True, exist_ok=True)
    log = open(OUT / f"optuna_{catalog}.jsonl", "w", encoding="utf-8")

    def objective(trial):
        cfg = {**BASE,
               "multi_rep": trial.suggest_categorical("multi_rep", [True, False]),
               "lexical": "bm25",
               "examples": True,
               "rrf_k": trial.suggest_int("rrf_k", 5, 120),
               "w_lex": trial.suggest_float("w_lex", 0.2, 4.0, log=True),
               "pool_mult": trial.suggest_int("pool_mult", 2, 10),
               "prior_w": trial.suggest_float("prior_w", 0.0, 3.0),
               "cat_first": trial.suggest_categorical("cat_first", [True, False]),
               "cat_k": trial.suggest_int("cat_k", 3, 15)}
        s = summary(run(tune, cfg))
        obj = 0.5 * s["hit3"] + 0.5 * s["hit1"]
        log.write(json.dumps({"trial": trial.number, "cfg": cfg, "objective": obj, **s}, ensure_ascii=False) + "\n")
        log.flush()
        return obj

    study = optuna.create_study(direction="maximize", sampler=optuna.samplers.TPESampler(seed=20261115))
    study.enqueue_trial({"multi_rep": False, "rrf_k": 60, "w_lex": 1.0, "pool_mult": 4, "prior_w": 0.0,
                         "cat_first": False, "cat_k": 10})
    study.optimize(objective, n_trials=trials)
    best = {**BASE, **study.best_params, "lexical": "bm25", "examples": True}
    bt, vt = run(tune, BASE), run(tune, best)
    bh, vh = run(hold, BASE), run(hold, best)
    adopt, tests = decide(bt, vt, bh, vh)
    res = {"catalog": catalog, "trials": trials, "best_cfg": best, "tune": summary(vt), "holdout": summary(vh),
           "baseline_tune": summary(bt), "baseline_holdout": summary(bh), "mcnemar_vs_base": tests, "adopt": adopt}
    json.dump(res, open(OUT / f"best_{catalog}.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps(res, ensure_ascii=False, indent=1))
    fr.CFG.clear()
    fr.CFG.update(BASE)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["ablation", "optuna"])
    ap.add_argument("--trials", type=int, default=60)
    ap.add_argument("--catalog", default="278")
    a = ap.parse_args()
    ablation() if a.mode == "ablation" else run_optuna(a.trials, a.catalog)
