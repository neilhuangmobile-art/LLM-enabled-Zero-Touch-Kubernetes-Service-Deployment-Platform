"""
組員的選軟體 RAG（origin/feat/multiagent-pipeline 的 agents/pipeline/software_rag.py＋rag/software_catalog.json）
與本分支 rag/field_retriever.py 在同一批題目上比較（2026-11-17）。只讀不改組員程式：先把兩個檔案抽到
D:\\rag_sources\\teammate\\，再載入。

  python scripts/rag/compare_teammate_rag.py   → docs/rag_promptx/compare_teammate.json、compare_teammate_rows.jsonl

題組：我的 419 題（Gemini 157＋Claude 262，含四種改寫）＋組員的 S40（不需反問、有標準答案的題目）。
組員的流程是「向量前 25 → reranker 取前 4」，這裡照原樣跑，前 3 名命中只看前 3 個。
"""
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("RAG_RERANK_DEVICE", "cuda")
from rag import eval_fields as ev  # noqa: E402
from rag import field_retriever as fr  # noqa: E402
from rag import strategy_eval as se  # noqa: E402

BR = "origin/feat/multiagent-pipeline"
TM = Path(r"D:\rag_sources\teammate")
OUT = ROOT / "docs" / "rag_promptx"


def extract():
    TM.mkdir(parents=True, exist_ok=True)
    for src, dst in (("agents/pipeline/software_rag.py", "software_rag.py"),
                     ("rag/software_catalog.json", "software_catalog.json"),
                     ("experiments/model_select/reason/testset_s.jsonl", "testset_s.jsonl")):
        data = subprocess.run(["git", "show", f"{BR}:{src}"], cwd=ROOT, capture_output=True, check=True).stdout
        (TM / dst).write_bytes(data)
    spec = importlib.util.spec_from_file_location("tm_software_rag", TM / "software_rag.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.CATALOG_PATH = str(TM / "software_catalog.json")
    return mod


def s40_tasks():
    out = []
    for x in open(TM / "testset_s.jsonl", encoding="utf-8"):
        if not x.strip():
            continue
        t = json.loads(x)
        img = t.get("expected", {}).get("image", {})
        if img.get("must_ask") or not img.get("accept"):
            continue
        out.append({"id": t["id"], "text": t["text"], "_accept": {ev.norm(a) for a in img["accept"]}, "_src": "s40"})
    return out


def score(names, accept):
    return {"hit1": bool(names) and bool(names[0] & accept), "hit3": any(n & accept for n in names[:3])}


def main():
    tm = extract()
    tasks, pert = se.load_tasks()
    tasks += s40_tasks()
    se.use_catalog()
    rows = []
    t_all = time.time()
    for i, t in enumerate(tasks):
        variants = {"orig": t["text"]}   # 組員版每句要重排 25 筆，先只跑原句
        for var, text in variants.items():
            t0 = time.time()
            c = tm.candidates(text, k=4)
            ms = (time.time() - t0) * 1000
            rows.append({"id": t["id"], "src": t["_src"], "variant": var, "sys": "teammate",
                         **score([{ev.norm(x["image"])} for x in c], t["_accept"]), "latency_ms": round(ms, 1),
                         "top": [x["image"] for x in c[:3]]})
            for arm in ("seed_hybrid", "ext_d03_mr"):
                fr.CFG.clear()
                fr.CFG.update({**se.BASE, **se.ARMS[arm][0]})
                fr._QCACHE.clear()
                t0 = time.time()
                cands = fr.retrieve_for_field(text, "image", top_k=3, method="hybrid")
                ms = (time.time() - t0) * 1000
                names = [ev.cand_names(x["value"], x.get("related", {}), x.get("source", "")) for x in cands]
                rows.append({"id": t["id"], "src": t["_src"], "variant": var, "sys": arm, **score(names, t["_accept"]),
                             "latency_ms": round(ms, 1), "top": [x["source"].split("#", 1)[-1] for x in cands]})
        if (i + 1) % 50 == 0:
            print(f"  {i + 1}/{len(tasks)}，{time.time() - t_all:.0f} 秒", flush=True)
    with open(OUT / "compare_teammate_rows.jsonl", "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    summ = {}
    import numpy as np
    for s in ("teammate", "seed_hybrid", "ext_d03_mr"):
        for part in ("all419", "gemini", "claude", "s40"):
            rs = [r for r in rows if r["sys"] == s and r["variant"] == "orig" and
                  (r["src"] in ("gemini", "claude") if part == "all419" else r["src"] == part)]
            if not rs:
                continue
            summ.setdefault(s, {})[part] = {"n": len(rs), "hit1": round(float(np.mean([r["hit1"] for r in rs])), 4),
                                            "hit3": round(float(np.mean([r["hit3"] for r in rs])), 4),
                                            "lat_p50": float(np.median([r["latency_ms"] for r in rs]))}
        for k in ("typo", "short", "verbose", "mixed"):
            rs = [r for r in rows if r["sys"] == s and r["variant"] == k]
            if rs:
                summ[s][k] = round(float(np.mean([r["hit3"] for r in rs])), 4)
    json.dump(summ, open(OUT / "compare_teammate.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps(summ, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
