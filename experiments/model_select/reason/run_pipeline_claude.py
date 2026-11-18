"""2026-11-18 用 Claude 出的開發題組（docs/bench_blind/dev_claude.jsonl，組員流程沒看過的題目）跑多代理人流程。

目的：驗證「選軟體」規則（gap_check._select_software，PIPELINE_SELECT_RULE=agree｜always）。規則只影響選軟體這一步，
所以用 always（每次都重選）跑一輪，記下「推理代理原本的選擇、RAG 候選、重選結果」，新規則（agree：原本的選擇
也在候選裡就維持）的結果可以直接算出，不用跑兩輪。

評分（這個題組沒有組員為 S01～S40 手寫的 expose／storage／cron 檢查）：
- 不需反問的題目：映像檔在 accept 清單、副本數、port、記憶體、CPU 在範圍內；有沒有多問
- 需要反問的題目：有沒有反問

  PIPELINE_SELECT_RULE=always python experiments/model_select/reason/run_pipeline_claude.py --url http://127.0.0.1:8781
結果：results/pipeline_claude_<tag>.jsonl（逐題，含 passed 與判定依據）與 summary_claude_<tag>.json
"""
import argparse
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from run_pipeline_eval import ROOT, runner, to_pred  # noqa: E402
from run_pipeline_llamacpp import make_llm  # noqa: E402
from run_reason import _ALIAS, _int, norm_image, to_mi, to_milli  # noqa: E402
from agents.pipeline import software_rag  # noqa: E402

SET = os.path.join(ROOT, "docs", "bench_blind", "dev_claude.jsonl")
_SEL = re.compile(r"^(\S+) → (\S+)：")


def score(t, p):
    exp = t["expected"]
    asked = bool(p.get("questions"))
    if exp["image"].get("must_ask"):
        return {"kind": "must_ask", "asked": asked, "passed": asked, "rule": "需要反問：有反問才算過"}
    acc = [_ALIAS.get(a, a) for a in exp["image"]["accept"]]
    c = {"image": norm_image(p.get("image")) in acc}
    rep = _int(p.get("replicas"))
    c["replicas"] = rep is not None and exp["replicas"]["min"] <= rep <= exp["replicas"]["max"]
    port = _int(p.get("port"))
    c["port"] = port in (exp["port"].get("accept") or []) if exp["port"].get("accept") else True
    mem, cpu = to_mi(p.get("memory")), to_milli(p.get("cpu"))
    c["memory"] = mem is not None and to_mi(exp["memory"]["min"]) <= mem <= to_mi(exp["memory"]["max"])
    c["cpu"] = cpu is not None and to_milli(exp["cpu"]["min"]) <= cpu <= to_milli(exp["cpu"]["max"])
    return {"kind": "answer", "checks": c, "asked": asked, "passed": all(c.values()) and not asked,
            "rule": "映像檔在 accept、副本數／記憶體／CPU 在範圍、port 在 accept（沒給就不檢查），且沒有多問"}


def select_info(spec, text, accept):
    """從 trace 取出選軟體這一步：原本的選擇、重選結果，並算出 agree 規則會得到什麼。"""
    for tr in spec["trace"]:
        if tr["step"] != "select":
            continue
        m = _SEL.match(tr["detail"])
        # 「維持 wordpress（候選：…）」或「維持 nginx（RAG 候選也有它）」：取到全形括號前
        cur = m.group(1) if m else tr["detail"].split()[1].split("（")[0]
        new = m.group(2) if m else cur
        cands = [c["image"] for c in software_rag.candidates(text)]
        agree = cur if cur in cands else new
        ok = lambda i: _ALIAS.get(norm_image(i), norm_image(i)) in accept  # noqa: E731
        return {"cur": cur, "always": new, "agree": agree, "cands": cands, "cur_ok": ok(cur), "always_ok": ok(new),
                "agree_ok": ok(agree)}
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--tag", default="q8_vulkan")
    ap.add_argument("--limit", type=int)
    a = ap.parse_args()
    if os.environ.get("PIPELINE_SELECT_RULE") != "always":
        sys.exit("請設 PIPELINE_SELECT_RULE=always（agree 的結果由 always 的紀錄算出）")
    # 先確認模型伺服器真的可用（11-18 踩過：路徑錯、伺服器沒載入模型，流程照樣跑完 300 題、全部變成「問使用者」）
    import requests
    try:
        ok = requests.get(f"{a.url}/health", timeout=5).json().get("status") == "ok"
    except Exception:  # noqa: BLE001
        ok = False
    probe = make_llm(a.url)("Reply with JSON.", '{"ping": 1}', 8) if ok else None
    if not ok or probe is None:
        sys.exit(f"模型伺服器 {a.url} 不可用，停止（不跑沒有模型的評估）")
    llm = make_llm(a.url)
    fails = {"n": 0}
    _inner = llm

    def llm(system, user, max_new_tokens=768):  # noqa: F811  呼叫失敗連續 5 次就停止
        out = _inner(system, user, max_new_tokens)
        fails["n"] = 0 if out is not None else fails["n"] + 1
        if fails["n"] >= 5:
            sys.exit("模型連續 5 次呼叫失敗，停止評估")
        return out
    tasks = [json.loads(x) for x in open(SET, encoding="utf-8") if x.strip().startswith("{")]
    tasks = tasks[: a.limit] if a.limit else tasks
    path = os.path.join(HERE, "results", f"pipeline_claude_{a.tag}.jsonl")
    rows = []
    with open(path, "w", encoding="utf-8") as f:
        for t in tasks:
            t0 = time.time()
            spec = runner.run(t["text"], None, llm)
            pred = to_pred(spec)
            acc = [_ALIAS.get(x, x) for x in (t["expected"]["image"].get("accept") or [])]
            row = {"id": t["id"], "difficulty": t.get("difficulty"), "input": t["text"], "pred": pred,
                   "latency_ms": int((time.time() - t0) * 1000), "score": score(t, pred),
                   "select": select_info(spec, t["text"], acc)}
            row["passed"] = row["score"]["passed"]
            rows.append(row)
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()
            print(f"{t['id']} {row['latency_ms'] / 1000:6.1f}s pass={int(row['passed'])} sel={row['select'] and (row['select']['cur'], row['select']['always'], row['select']['agree'])}", flush=True)
    ans = [r for r in rows if r["score"]["kind"] == "answer"]
    ask = [r for r in rows if r["score"]["kind"] == "must_ask"]
    sel = [r["select"] for r in ans if r["select"]]
    lat = sorted(r["latency_ms"] for r in rows)
    summ = {"n": len(rows), "n_answer": len(ans), "n_must_ask": len(ask),
            "pass_answer": round(sum(r["passed"] for r in ans) / max(1, len(ans)), 3),
            "image_acc_always": round(sum(r["score"]["checks"]["image"] for r in ans) / max(1, len(ans)), 3),
            "false_ask": sum(r["score"]["asked"] for r in ans),
            "must_ask_recall": round(sum(r["passed"] for r in ask) / max(1, len(ask)), 3),
            "select_steps": len(sel),
            "select_always_ok": sum(s["always_ok"] for s in sel), "select_agree_ok": sum(s["agree_ok"] for s in sel),
            "select_cur_ok": sum(s["cur_ok"] for s in sel),
            "select_changed_by_rule": sum(s["agree"] != s["always"] for s in sel),
            "latency_p50_ms": lat[len(lat) // 2] if lat else None, "latency_p90_ms": lat[int(0.9 * (len(lat) - 1))] if lat else None}
    # agree 與 always 只差在選軟體步驟：逐題配對的 McNemar
    from scipy.stats import binomtest
    b = sum(s["agree_ok"] and not s["always_ok"] for s in sel)
    c = sum(s["always_ok"] and not s["agree_ok"] for s in sel)
    summ["mcnemar_agree_vs_always"] = {"agree_only": b, "always_only": c,
                                       "p": round(float(binomtest(min(b, c), b + c, 0.5).pvalue), 4) if b + c else 1.0}
    json.dump(summ, open(os.path.join(HERE, "results", f"summary_claude_{a.tag}.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(json.dumps(summ, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
