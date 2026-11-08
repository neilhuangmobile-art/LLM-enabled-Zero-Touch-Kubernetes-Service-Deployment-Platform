"""第二次實驗的統計（2026-11-07）：通過率附 95% Wilson 信賴區間、結果三分法、含硬體與電費的成本。

讀取（都在 BENCH_OUT 指定的資料夾）：
- A_deploy.jsonl：原本 22 題（修正後重跑）。注意：這 22 題開發端看過，報告要分開列並標明。
- blind_graded.jsonl（可以有多份，用 BLIND_PHASES 指定「階段名稱=檔名」）：盲測評分結果。
輸出 summary_r2.json，並在終端機印出摘要。

成本參數都是假設值，集中在 COST 裡，報告要寫明來源；有實測值（例如插座電力計）就改這裡。
"""
import json, math, os, sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import OUT  # noqa: E402

COST = {
    # 顯卡：AMD Radeon RX 9060 XT 16GB。功耗與價格是廠商公布的規格／建議售價，使用前請再核對一次
    "gpu_board_power_w": float(os.environ.get("COST_GPU_W", 160)),
    "gpu_price_usd": float(os.environ.get("COST_GPU_USD", 349)),
    "amortize_years": 3,
    # 台電住宅用電，取 4.5 元／度當代表值（夏月累進較高）；匯率 1 美元 = 32 元
    "electricity_twd_per_kwh": float(os.environ.get("COST_TWD_KWH", 4.5)),
    "twd_per_usd": float(os.environ.get("COST_TWD_USD", 32)),
    "deploys_per_day_scenarios": [10, 100, 1000],
}


def wilson(k, n, z=1.96):
    """二項比例的 95% Wilson 信賴區間；樣本小時比常態近似可靠。"""
    if not n:
        return None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round(100 * max(0, c - h), 1), round(100 * min(1, c + h), 1)]


def rate(rows):
    rows = [r for r in rows if r.get("passed") is not None]
    k, n = sum(1 for r in rows if r["passed"]), len(rows)
    return {"passed": k, "n": n, "pct": round(100 * k / n, 1) if n else None, "ci95": wilson(k, n)}


def three_way(r):
    """一次完成／沒有部署（反問或擋下）／失敗。讓「反問算不算過」不用再靠類別個別解釋。"""
    if r.get("passed"):
        return "通過"
    if r.get("outcome") in ("rejected", "blocked", "replied", "no_workload"):
        return "沒有部署（反問或擋下）"
    return "部署了但不符合"


def gpu_cost(rows):
    ms = [r["gpu"]["gen_ms"] for r in rows if isinstance(r.get("gpu"), dict) and r["gpu"].get("gen_ms") is not None]
    if not ms:
        return None
    avg_ms = sum(ms) / len(ms)
    kwh = COST["gpu_board_power_w"] * (avg_ms / 1000 / 3600) / 1000
    elec_usd = kwh * COST["electricity_twd_per_kwh"] / COST["twd_per_usd"]
    amort_day = COST["gpu_price_usd"] / (COST["amortize_years"] * 365)
    return {
        "n": len(ms), "avg_gpu_ms": round(avg_ms, 1),
        "used_gpu_share": round(sum(1 for m in ms if m > 0) / len(ms), 3),
        "electricity_usd_per_deploy": round(elec_usd, 8),
        "hardware_usd_per_deploy": {str(d): round(amort_day / d, 5) for d in COST["deploys_per_day_scenarios"]},
        "assumptions": COST,
    }


def load(path):
    p = Path(path)
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()] if p.exists() else []


if __name__ == "__main__":
    summary = {}
    a = load(OUT / "A_deploy.jsonl")
    if a:
        by = defaultdict(list)
        for r in a:
            by[r["system"]].append(r)
        summary["original_22"] = {
            "note": "原本 22 題，開發端看過題目，修正是針對這些題目做的，不能當成泛化能力的證據",
            **{s: {"overall": rate(rs),
                   "by_category": {c: rate([r for r in rs if r.get("category") == c]) for c in sorted({r.get("category") for r in rs})},
                   "three_way": dict(sorted(((k, sum(1 for r in rs if three_way(r) == k)) for k in {three_way(r) for r in rs}))),
                   "gpu_cost": gpu_cost(rs)} for s, rs in by.items()}}
    phases = os.environ.get("BLIND_PHASES") or "blind=blind_graded.jsonl"
    for item in phases.split(","):
        name, fn = item.split("=")
        g = load(OUT / fn)
        if not g:
            continue
        raw = {(r["task_id"], r["system"], r["round"]): r for r in load(OUT / fn.replace("graded", "raw"))}
        for r in g:
            r["outcome"] = (raw.get((r["task_id"], r["system"], r["round"])) or {}).get("outcome")
        by = defaultdict(list)
        for r in g:
            by[r["system"]].append(r)
        summary[name] = {s: {"overall": rate(rs),
                             "by_category": {c: rate([r for r in rs if r.get("category") == c]) for c in sorted({str(r.get("category")) for r in rs})},
                             "three_way": dict(sorted(((k, sum(1 for r in rs if three_way(r) == k)) for k in {three_way(r) for r in rs}))),
                             "ungraded": sum(1 for r in rs if r.get("passed") is None),
                             "gpu_cost": gpu_cost(rs) if s == "zerotouch" else None,
                             "claude_api_usd_per_task": round(sum(r.get("cost_usd") or 0 for r in rs) / len(rs), 4) if s == "claude" else None}
                         for s, rs in by.items()}
    (OUT / "summary_r2.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    for sec, v in summary.items():
        print(f"== {sec}")
        for s, d in v.items():
            if isinstance(d, dict) and "overall" in d:
                o = d["overall"]
                print(f"   {s:9} {o['passed']}/{o['n']} = {o['pct']}%  95% CI {o['ci95']}  {d.get('three_way')}")
    print(f"寫入 {OUT / 'summary_r2.json'}")
