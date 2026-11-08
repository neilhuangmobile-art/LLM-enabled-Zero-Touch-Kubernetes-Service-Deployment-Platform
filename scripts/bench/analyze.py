"""把各實驗的 JSONL 彙整成摘要（summary.json）。只做計算，不下結論；結論寫在報告裡、要對得上這裡的數字。
用法：python scripts/bench/analyze.py
"""
import json, statistics as st
from collections import Counter, defaultdict
from pathlib import Path

D = Path(__file__).resolve().parents[2] / "docs" / "bench_20261106"
GEM_IN, GEM_OUT = 0.30e-6, 2.50e-6  # Gemini 3.5 Flash-Lite 每 token 美元（第三方價格追蹤，2026-08）


def load(name):
    p = D / name
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()] if p.exists() else []


def pct(a, b):
    return round(100 * a / b, 1) if b else None


def q(xs, p):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    k = (len(xs) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(xs) - 1)
    return round(xs[lo] + (xs[hi] - xs[lo]) * (k - lo), 2)


gem = load("gemini_usage.jsonl")
gem_cost = {}
for path in ("deploy", "chat"):
    rs = [r for r in gem if r["path"] == path]
    gem_cost[path] = round(st.mean(r["in"] * GEM_IN + r["out"] * GEM_OUT for r in rs), 6) if rs else None

S = {"gemini_cost_per_request_usd_upper": gem_cost, "suites": {}}

# ── A 部署 ──
A = [r for r in load("A_deploy.jsonl") if r.get("outcome") != "harness_error"]
a = {}
for sysname in ("zerotouch", "claude"):
    rs = [r for r in A if r["system"] == sysname]
    if not rs:
        continue
    by_cat = defaultdict(list)
    for r in rs:
        by_cat[r["category"]].append(r["passed"])
    by_task = defaultdict(list)
    for r in rs:
        by_task[r["task_id"]].append(r)
    pass_all = sum(1 for t, v in by_task.items() if v and all(x["passed"] for x in v))
    pass_any = sum(1 for t, v in by_task.items() if any(x["passed"] for x in v))
    # 一致性：同一題每一輪都有部署出東西時，規格指紋是否完全相同
    cons_n = cons_same = 0
    for t, v in by_task.items():
        fps = [x["snapshot"]["fingerprint"] for x in v if x["snapshot"]["spec"]]
        if len(fps) == len(v) and len(v) >= 2:
            cons_n += 1
            cons_same += len(set(fps)) == 1
    # 結果一致性：每一輪的「結果種類＋規格」是否完全相同（含沒部署的題目），比只看有部署的題目更嚴格
    out_same = sum(1 for t, v in by_task.items()
                   if len({(x["outcome"], x["snapshot"]["fingerprint"]) for x in v}) == 1)
    deployed = [r for r in rs if r["snapshot"]["spec"]]
    tri = {k: round(st.mean(r["snapshot"]["trivy"].get(k, 0) for r in deployed), 2) if deployed else None
           for k in ("CRITICAL", "HIGH", "MEDIUM", "LOW")}
    flags = {k: pct(sum(1 for r in deployed if r["snapshot"]["flags"][k]), len(deployed))
             for k in ("latest", "no_limits", "no_requests", "probes", "privileged", "hostPath", "hostNetwork")}
    costs = [r["cost_usd"] for r in rs if r.get("cost_usd") is not None]
    approvals = [sum(1 for c in r.get("commands", []) if c[0] in ("Bash", "PowerShell")) for r in rs] if sysname == "claude" else None
    a[sysname] = {
        "runs": len(rs), "pass_rate": pct(sum(r["passed"] for r in rs), len(rs)),
        "by_category": {c: {"n": len(v), "pass_rate": pct(sum(v), len(v))} for c, v in by_cat.items()},
        "tasks": len(by_task), "pass_all_rounds": pass_all, "pass_any_round": pass_any,
        "consistency": {"tasks_checked": cons_n, "identical": cons_same, "rate": pct(cons_same, cons_n)},
        "outcome_consistency": {"tasks": len(by_task), "identical": out_same, "rate": pct(out_same, len(by_task))},
        "outcomes": dict(Counter(r["outcome"] for r in rs)),
        "latency_s": {"p50": q([r.get("latency_s") for r in rs], .5), "p95": q([r.get("latency_s") for r in rs], .95),
                      "mean": round(st.mean(r["latency_s"] for r in rs if r.get("latency_s") is not None), 2)},
        "time_to_ready_s": {"p50": q([r.get("time_to_ready_s") for r in rs], .5), "p95": q([r.get("time_to_ready_s") for r in rs], .95)},
        "cost_usd": {"mean": round(st.mean(costs), 4) if costs else gem_cost["deploy"],
                     "total": round(sum(costs), 3) if costs else None,
                     "basis": "claude -p 回報的 total_cost_usd（牌價）" if costs else "Gemini 用量實測 × 牌價（上限）；本機 GPU 電費未計"},
        "trivy_mean_per_run": tri, "flags_pct_of_deployed": flags, "deployed_runs": len(deployed),
        "commands_needing_approval": {"mean": round(st.mean(approvals), 2), "max": max(approvals)} if approvals else None,
    }
    a[sysname]["per_task"] = {t: {"category": v[0]["category"], "input": v[0]["input"],
                                 "passed": [x["passed"] for x in v], "outcomes": [x["outcome"] for x in v],
                                 "why": [x["pass_rule"] for x in v],
                                 "latency": [x.get("latency_s") for x in v],
                                 "replicas": [x["snapshot"]["total_replicas"] for x in v],
                                 "images": [[s["image"] for s in x["snapshot"]["spec"]] for x in v]}
                             for t, v in sorted(by_task.items())}
S["suites"]["A_deploy"] = a


def simple_suite(fname, extra=None):
    rows = load(fname)
    out = {}
    for sysname in ("zerotouch", "claude"):
        rs = [r for r in rows if r["system"] == sysname]
        if not rs:
            continue
        costs = [r.get("cost_usd") for r in rs if r.get("cost_usd") is not None]
        lat = [r.get("latency_s") for r in rs if r.get("latency_s") is not None]
        by_task = defaultdict(list)
        for r in rs:
            by_task[r["task_id"]].append(r)
        out[sysname] = {"runs": len(rs), "pass_rate": pct(sum(r["passed"] for r in rs), len(rs)),
                        "latency_s": {"p50": q(lat, .5), "p95": q(lat, .95)} if lat else None,
                        "cost_usd_mean": round(st.mean(costs), 4) if costs else None,
                        "per_task": {t: {"input": v[0].get("input"), "passed": [x["passed"] for x in v],
                                         "why": [x["pass_rule"] for x in v]} for t, v in sorted(by_task.items())}}
        if extra:
            out[sysname].update(extra(rs))
    return out


S["suites"]["B_ops"] = simple_suite("B_ops.jsonl", lambda rs: {"asked_user": sum(1 for r in rs if r.get("asked_user"))})
S["suites"]["C_fault"] = simple_suite("C_fault.jsonl", lambda rs: {
    "recover_s_from_inject": [r.get("recover_s_from_inject") for r in rs if r.get("recover_s_from_inject")],
    "recover_s_after_ask": [r.get("recover_s_after_ask") for r in rs if r.get("recover_s_after_ask")],
    "human_needed": sum(1 for r in rs if r.get("human_needed"))})
S["suites"]["D_qa"] = simple_suite("D_qa.jsonl", lambda rs: {"reply_len_mean": round(st.mean(r.get("reply_len", 0) for r in rs))})
S["suites"]["E_import"] = simple_suite("E_import.jsonl")
S["suites"]["A2_followup"] = simple_suite("A2_followup.jsonl")
(D / "summary.json").write_text(json.dumps(S, ensure_ascii=False, indent=1), encoding="utf-8")
print(json.dumps({k: {s: {kk: vv for kk, vv in v.items() if kk not in ("per_task", "by_category")} for s, v in vs.items()}
                  for k, vs in S["suites"].items()}, ensure_ascii=False, indent=1))
