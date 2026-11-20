"""各欄位的準確率報表（2026-11-19）：每個欄位跟自己比，追蹤目標 70%。

讀 run_pipeline_dev.py 的逐題結果，只看「需要直接回答」的題目：
  image / replicas / port（題目有給答案才算）/ memory / cpu：該欄位在答案範圍內的比例
  不多問：沒有多問任何問題的比例
  整張全對：上面全部都對
另外列「需要反問」的題目有反問的比例。

  python experiments/model_select/reason/field_report.py scenario:conf claude:conf [scenario:新版 claude:新版]
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from run_pipeline_dev import SETS, score  # noqa: E402
from run_pipeline_eval import ROOT  # noqa: E402

FIELDS = ("image", "replicas", "port", "memory", "cpu")


def report(set_, tag):
    tasks = {t["id"]: t for t in (json.loads(x) for x in open(os.path.join(ROOT, "docs", "bench_blind", SETS[set_]),
                                                              encoding="utf-8") if x.strip().startswith("{"))}
    rows = [json.loads(x) for x in open(os.path.join(HERE, "results", f"dev_{set_}_{tag}.jsonl"), encoding="utf-8")]
    ok = {f: [0, 0] for f in FIELDS + ("不多問", "整張全對", "該問有問")}
    for r in rows:
        t = tasks[r["id"]]
        s = score(t, r["pred"])        # 用目前的評分重算（映像檔比對最後一段名稱）
        if s["kind"] == "must_ask":
            ok["該問有問"][0] += s["passed"]
            ok["該問有問"][1] += 1
            continue
        for f in FIELDS:
            if f == "port" and not t["expected"]["port"].get("accept"):
                continue
            ok[f][0] += bool(s["checks"][f])
            ok[f][1] += 1
        ok["不多問"][0] += not s["asked"]
        ok["不多問"][1] += 1
        ok["整張全對"][0] += s["passed"]
        ok["整張全對"][1] += 1
    return {k: (a / n if n else None, a, n) for k, (a, n) in ok.items()}


def main(specs):
    reps = [(sp, report(*sp.split(":"))) for sp in specs]
    keys = list(reps[0][1])
    print("欄位".ljust(8) + "".join(sp.ljust(22) for sp, _ in reps))
    for k in keys:
        line = k.ljust(8)
        for _, r in reps:
            p, a, n = r[k]
            line += (f"{p:6.1%} ({a}/{n})" if p is not None else "-").ljust(22)
        print(line)
    out = os.path.join(HERE, "results", "field_report_" + "_".join(s.replace(":", "-") for s in specs) + ".json")
    json.dump({sp: r for sp, r in reps}, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main(sys.argv[1:] or ["scenario:conf", "claude:conf"])
