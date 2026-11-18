"""
RAG 欄位檢索評估（2026-11-15，計畫步驟 0 的第一次量測）

用開發集（docs/bench_blind/dev_scenario.jsonl，Gemini 出題、可閱讀）比較：
  none        不用 RAG（現況：情境題沒有軟體建議）
  old_tfidf   舊的部署範例檢索（rag/retriever.retrieve_deploy_examples，TF-IDF）
  bm25 / dense / hybrid / hybrid_rerank   新資料庫＋各檢索方式（hybrid_rerank 只抽前 N 題，太慢）

指標（每題結構化存檔，AGENT_RULES 的 QA 規則）：
  image_hit1 / image_hit3     第 1 名／前 3 名候選在題目可接受清單內
  abstain_ok                  題目要求反問映像檔時，RAG 沒有給候選（門檻生效）
  wrong_answer                給了候選、但第 1 名不在可接受清單（或該反問卻給了）
  need_recall                 題目列出的額外需求，被 retrieve_requirements 找到的比例
  port_ok                     第 1 名命中時，目錄的連接埠在題目可接受清單內
  latency_ms                  一次欄位檢索＋需求檢索的時間
另外存下每個方法第 1 名的原始分數，之後畫「門檻 vs 正確率」曲線用。

用法：python rag/eval_fields.py [--rerank-n 50]
輸出：docs/rag_eval_20261115/{results.jsonl, summary.json}
"""
import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from rag import field_retriever as fr  # noqa: E402

DEV = ROOT / "docs/bench_blind/dev_scenario.jsonl"
OUT = ROOT / "docs/rag_eval_20261115"
ALIAS = {"mongodb": "mongo", "postgresql": "postgres", "postgre": "postgres", "wikijs": "wiki", "wiki.js": "wiki",
         "requarks": "wiki", "mysqlserver": "mysql", "elastic": "elasticsearch", "httpd": "httpd", "apache": "httpd"}


def norm(name: str) -> str:
    n = (name or "").lower().split(":")[0].split("/")[-1]
    n = re.sub(r"[^a-z0-9.-]", "", n)
    return ALIAS.get(n, n)


def cand_names(value: str, related: dict, source: str) -> set:
    names = {norm(value)}
    sid = source.split("#", 1)[-1] if "#" in source else ""
    if sid:
        names.add(norm(sid))
    return names


def eval_one(task, method):
    exp = task.get("expected", {})
    accept = {norm(a) for a in (exp.get("image", {}).get("accept") or [])}
    must_ask = bool(exp.get("image", {}).get("must_ask"))
    need_exp = {e.get("need") for e in (task.get("extra") or []) if e.get("need") in fr_needs()}
    t0 = time.time()
    if method == "none":
        cands, needs, top_score = [], [], None
    elif method == "old_tfidf":
        from rag.retriever import retrieve_deploy_examples
        exs = retrieve_deploy_examples(task["text"], top_k=3) or []
        cands = []
        for e in exs:
            img = (e.get("output") or {}).get("image") or e.get("image")
            if img:
                cands.append({"value": img, "related": {}, "source": "", "score": e.get("score", 0)})
        needs, top_score = [], (exs[0].get("score") if exs else None)
    else:
        raw = fr._software_hits(task["text"], 3, method)
        top_score = raw[0]["score"] if raw else None
        cands = fr.retrieve_for_field(task["text"], "image", top_k=3, method=method)
        needs = [n["need"] for n in fr.retrieve_requirements(task["text"], top_k=5, method=method)]
    ms = (time.time() - t0) * 1000
    names = [cand_names(c["value"], c.get("related", {}), c.get("source", "")) for c in cands]
    hit1 = bool(names) and bool(names[0] & accept)
    hit3 = any(n & accept for n in names[:3])
    port_ok = None
    if hit1 and cands[0].get("related", {}).get("ports") is not None:
        acc_ports = set(exp.get("port", {}).get("accept") or [])
        port_ok = bool(set(cands[0]["related"]["ports"]) & acc_ports) if acc_ports else None
    return {
        "id": task["id"], "method": method, "difficulty": task.get("difficulty"), "input": task["text"],
        "output": [c["value"] for c in cands], "accept": sorted(accept), "must_ask_image": must_ask,
        "image_hit1": hit1, "image_hit3": hit3,
        "abstain_ok": (not cands) if must_ask else None,
        "wrong_answer": (bool(cands)) if must_ask else (bool(cands) and not hit1),
        "need_expected": sorted(need_exp), "need_found": needs,
        "need_recall": (len(need_exp & set(needs)) / len(need_exp)) if need_exp else None,
        "port_ok": port_ok, "top_score": top_score, "latency_ms": round(ms, 1),
        "passed": (not cands) if must_ask else hit1,
        "pass_rule": "需反問的題目：不給候選才算通過；其餘：第 1 名映像檔在可接受清單內",
    }


_NEEDS = None


def fr_needs():
    global _NEEDS
    if _NEEDS is None:
        import yaml
        _NEEDS = {r["need"] for r in yaml.safe_load(open(ROOT / "rag/knowledge/requirements.yaml", encoding="utf-8"))}
    return _NEEDS


def summarize(rows):
    def rate(key, rs):
        v = [r[key] for r in rs if r[key] is not None]
        return round(100 * sum(v) / len(v), 1) if v else None
    out = {}
    for m in dict.fromkeys(r["method"] for r in rows):
        rs = [r for r in rows if r["method"] == m]
        nr = [r["need_recall"] for r in rs if r["need_recall"] is not None]
        lat = sorted(r["latency_ms"] for r in rs)
        out[m] = {"n": len(rs), "passed": rate("passed", rs), "image_hit1": rate("image_hit1", [r for r in rs if not r["must_ask_image"]]),
                  "image_hit3": rate("image_hit3", [r for r in rs if not r["must_ask_image"]]),
                  "abstain_ok": rate("abstain_ok", rs), "wrong_answer": rate("wrong_answer", rs),
                  "need_recall": round(100 * sum(nr) / len(nr), 1) if nr else None, "port_ok": rate("port_ok", rs),
                  "latency_p50_ms": lat[len(lat) // 2] if lat else None,
                  "by_difficulty": {d: rate("passed", [r for r in rs if r["difficulty"] == d])
                                    for d in ("簡單", "中等", "困難")}}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rerank-n", type=int, default=50)
    ap.add_argument("--methods", default="none,old_tfidf,bm25,dense,hybrid,hybrid_rerank")
    a = ap.parse_args()
    tasks = [json.loads(x) for x in open(DEV, encoding="utf-8") if x.strip().startswith("{")]
    OUT.mkdir(parents=True, exist_ok=True)
    rows = []
    for m in a.methods.split(","):
        sub = tasks[: a.rerank_n] if m == "hybrid_rerank" else tasks
        t0 = time.time()
        for t in sub:
            try:
                rows.append(eval_one(t, m))
            except Exception as e:  # noqa: BLE001
                rows.append({"id": t["id"], "method": m, "error": str(e)[:200], "passed": False, "difficulty": t.get("difficulty"),
                             "must_ask_image": False, "image_hit1": False, "image_hit3": False, "abstain_ok": None,
                             "wrong_answer": None, "need_recall": None, "port_ok": None, "latency_ms": 0})
        print(f"  {m}: {len(sub)} 題，{time.time() - t0:.0f} 秒", flush=True)
    with open(OUT / "results.jsonl", "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    s = summarize(rows)
    json.dump({"dev_set": str(DEV.name), "n_tasks": len(tasks), "thresholds": fr.MIN_SCORE, "methods": s},
              open(OUT / "summary.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"{'方法':14}{'通過':>7}{'第1名命中':>9}{'前3命中':>8}{'正確放棄':>8}{'錯誤答案':>8}{'需求召回':>8}{'連接埠':>7}{'延遲ms':>8}")
    for m, v in s.items():
        print(f"{m:14}{str(v['passed']):>7}{str(v['image_hit1']):>9}{str(v['image_hit3']):>8}{str(v['abstain_ok']):>8}"
              f"{str(v['wrong_answer']):>8}{str(v['need_recall']):>8}{str(v['port_ok']):>7}{str(v['latency_p50_ms']):>8}")


if __name__ == "__main__":
    main()
