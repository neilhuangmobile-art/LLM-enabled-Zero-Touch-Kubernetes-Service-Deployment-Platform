"""
PROMPTx-RAG 結果分析（2026-11-15）：在 strategy_table.jsonl 上算報告要用的表格。

  python scripts/rag/promptx_analyze.py   → docs/rag_promptx/analysis.json、tables/*.csv

1. 各策略（臂）總表：準確、穩健、效率、總分（同 rag/policy.py 的定義），依出題來源拆開
2. 穩健性表：每種擾動（錯字、極短、囉嗦、中英夾雜）的前 3 名命中率
3. 配對比較：每個臂對現行正式版 seed_hybrid，逐題總分差的 bootstrap 95% CI、原句前 3 名的 McNemar 檢定
4. 重排序（只有原句）另外列
5. 交叉驗證中 fixed_best 每折挑中哪個臂
"""
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from rag import policy as P  # noqa: E402

OUT = ROOT / "docs" / "rag_promptx"
TAB = OUT / "tables"
BASE = "seed_hybrid"
KINDS = ["orig", "typo", "short", "verbose", "mixed"]


def mcnemar(a, b):
    from scipy.stats import binomtest
    n01 = sum((not x) and y for x, y in zip(a, b))
    n10 = sum(x and (not y) for x, y in zip(a, b))
    p = binomtest(min(n01, n10), n01 + n10, 0.5).pvalue if n01 + n10 else 1.0
    return {"base_only": n10, "arm_only": n01, "p": round(float(p), 4)}


def boot_ci(d, n=5000, seed=0):
    rng = np.random.default_rng(seed)
    d = np.asarray(d)
    m = [d[rng.integers(0, len(d), len(d))].mean() for _ in range(n)]
    return [round(float(np.percentile(m, 2.5)), 4), round(float(np.percentile(m, 97.5)), 4)]


def main():
    items, arms = P.load()
    rows = [json.loads(x) for x in open(OUT / "strategy_table.jsonl", encoding="utf-8")]
    by = defaultdict(dict)
    for r in rows:
        by[(r["id"], r["arm"])][r["variant"]] = r
    ids = [it["id"] for it in items]
    src = {it["id"]: it["src"] for it in items}
    TAB.mkdir(parents=True, exist_ok=True)
    res = {"n_items": len(items), "by_src": dict(Counter(src.values())),
           "n_with_perturb": sum(len(by[(q, BASE)]) > 1 for q in ids)}

    # 1. 總表
    overall = {}
    for a, arm in enumerate(arms):
        for part in ("all", "gemini", "claude"):
            cs = [it["comp"][a] for it in items if part == "all" or it["src"] == part]
            lat = sorted(c[3] for c in cs)
            overall.setdefault(arm, {})[part] = {
                "R": round(float(np.mean([P.reward(c) for c in cs])), 4),
                "acc": round(float(np.mean([c[0] for c in cs])), 4), "rob": round(float(np.mean([c[1] for c in cs])), 4),
                "eff": round(float(np.mean([c[2] for c in cs])), 4), "hit1": round(float(np.mean([c[4] for c in cs])), 4),
                "hit3": round(float(np.mean([c[5] for c in cs])), 4), "lat_p50": round(lat[len(lat) // 2], 1),
                "lat_p95": round(lat[int(0.95 * (len(lat) - 1))], 1), "n": len(cs)}
    res["overall"] = overall

    # 2. 穩健性表（只取四種擾動都有的題目，各臂同一批題）
    full = [q for q in ids if all(k in by[(q, BASE)] for k in KINDS)]
    rob = {}
    for arm in arms:
        rob[arm] = {k: round(float(np.mean([by[(q, arm)][k]["hit3"] for q in full])), 4) for k in KINDS}
        rob[arm]["worst_of_4"] = round(float(np.mean([min(by[(q, arm)][k]["hit3"] for k in KINDS[1:]) for q in full])), 4)
    res["robustness"] = {"n": len(full), "hit3": rob}

    # 3. 配對比較（對現行正式版）
    bi = arms.index(BASE)
    paired = {}
    for a, arm in enumerate(arms):
        if arm == BASE:
            continue
        d = [P.reward(it["comp"][a]) - P.reward(it["comp"][bi]) for it in items]
        h_base = [bool(it["comp"][bi][5]) for it in items]
        h_arm = [bool(it["comp"][a][5]) for it in items]
        per_kind = {}
        for k in KINDS[1:]:
            qs = [q for q in full]
            per_kind[k] = mcnemar([by[(q, BASE)][k]["hit3"] for q in qs], [by[(q, arm)][k]["hit3"] for q in qs])
        paired[arm] = {"dR": round(float(np.mean(d)), 4), "ci95": boot_ci(d),
                       "orig_hit3_mcnemar": mcnemar(h_base, h_arm), "perturb_hit3_mcnemar": per_kind}
    # 多重比較：所有臂 × 5 種句型的 McNemar 一起做 Holm 校正（臂與句型都多，單看 p<0.05 會有假陽性）
    tests = []
    for arm, v in paired.items():
        tests.append((arm, "orig", v["orig_hit3_mcnemar"]))
        tests += [(arm, k, t) for k, t in v["perturb_hit3_mcnemar"].items()]
    tests.sort(key=lambda x: x[2]["p"])
    m, run_max = len(tests), 0.0
    for i, (_, _, t) in enumerate(tests):
        run_max = max(run_max, min(1.0, (m - i) * t["p"]))
        t["p_holm"] = round(run_max, 4)
    res["holm_n_tests"] = m
    res["paired_vs_production"] = paired

    # 4. 重排序：只有原句
    rr = [q for q in ids if (q, "seed_rerank") in by and "orig" in by[(q, "seed_rerank")]]
    if rr:
        res["rerank_orig_only"] = {
            "n": len(rr),
            "seed_rerank": {"hit1": round(float(np.mean([by[(q, "seed_rerank")]["orig"]["hit1"] for q in rr])), 4),
                            "hit3": round(float(np.mean([by[(q, "seed_rerank")]["orig"]["hit3"] for q in rr])), 4),
                            "lat_p50": float(np.median([by[(q, "seed_rerank")]["orig"]["latency_ms"] for q in rr]))},
            BASE: {"hit1": round(float(np.mean([by[(q, BASE)]["orig"]["hit1"] for q in rr])), 4),
                   "hit3": round(float(np.mean([by[(q, BASE)]["orig"]["hit3"] for q in rr])), 4),
                   "lat_p50": float(np.median([by[(q, BASE)]["orig"]["latency_ms"] for q in rr]))},
            "hit3_mcnemar": mcnemar([by[(q, BASE)]["orig"]["hit3"] for q in rr],
                                    [by[(q, "seed_rerank")]["orig"]["hit3"] for q in rr])}

    # 5. fixed_best 每折挑中的臂
    picks = Counter()
    for x in open(OUT / "policy_folds.jsonl", encoding="utf-8"):
        f = json.loads(x)
        share = f["results"]["fixed_best"]["arm_share"]
        picks[max(share, key=share.get)] += 1
    res["fixed_best_picks"] = dict(picks)

    # 依難度
    diff = defaultdict(lambda: defaultdict(list))
    for r in rows:
        if r["variant"] == "orig" and r["arm"] in (BASE, "seed_hybrid_mr", "ext_d03"):
            diff[r.get("difficulty") or "?"][r["arm"]].append(r["hit3"])
    res["hit3_by_difficulty"] = {d: {a: [round(float(np.mean(v)), 4), len(v)] for a, v in x.items()} for d, x in diff.items()}

    json.dump(res, open(OUT / "analysis.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    with open(TAB / "arms_overall.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["arm", "part", "R", "acc", "rob", "eff", "hit1", "hit3", "lat_p50", "lat_p95", "n"])
        for arm, parts in overall.items():
            for part, v in parts.items():
                w.writerow([arm, part] + [v[k] for k in ("R", "acc", "rob", "eff", "hit1", "hit3", "lat_p50", "lat_p95", "n")])
    with open(TAB / "robustness.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["arm"] + KINDS + ["worst_of_4"])
        for arm, v in rob.items():
            w.writerow([arm] + [v[k] for k in KINDS + ["worst_of_4"]])
    print(json.dumps(res, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
