"""方案 C（本地判斷「軟體講清楚了沒」）門檻的離線掃描（2026-11-22）。

第 1 步（--score）：對整份題組算本地模型回答 Yes 的機率 p（agents/pipeline/prompts.py 的 CLARITY_PROB_SYSTEM），
         存到 results/clarity_p_<set>.jsonl。每題 1 次本地呼叫，不用 Gemini。
第 2 步（預設）：讀 p 和某一輪的逐題結果（--tag，含 choose_scores），在不同上下門檻下重新決定
         「問軟體／不問」，估計整張全對、該問有問、多問。
  - p ≤ 下門檻 → 問；p ≥ 上門檻 → 不問軟體，用信心度最高的候選；中間 → 沿用那一輪的結果
  - 定時指令的問題卡（cron_command）一律當作沒有（2026-11-22 預設改成範例指令）
  - 選軟體以外的欄位（副本數、資源、port）沿用那一輪的判定，所以是估計值；定了門檻要真的跑一次確認

  python experiments/model_select/reason/sweep_clarity.py --set scenario --score
  python experiments/model_select/reason/sweep_clarity.py --set scenario --tag cuda_school
"""
import argparse
import contextlib
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from run_pipeline_dev import SETS, _base  # noqa: E402
from run_pipeline_eval import ROOT  # noqa: E402
from agents.pipeline import llm as pllm  # noqa: E402
from agents.pipeline.prompts import CLARITY_PROB_SYSTEM  # noqa: E402

LOS = (0.0, 0.02, 0.05, 0.1, 0.2, 0.3, 0.4, 0.5)
HIS = (0.5, 0.6, 0.7, 0.8, 0.9, 0.95, 1.01)


def _tasks(set_):
    return [json.loads(x) for x in open(os.path.join(ROOT, "docs", "bench_blind", SETS[set_]), encoding="utf-8")
            if x.strip().startswith("{")]


def score_all(set_):
    if not pllm.local_available():
        sys.exit(f"本地模型 {pllm.LOCAL_URL} 沒有回應")
    out = os.path.join(HERE, "results", f"clarity_p_{set_}.jsonl")
    n_fail = 0
    with open(out, "w", encoding="utf-8") as f:
        for t in _tasks(set_):
            with contextlib.redirect_stdout(io.StringIO()):
                p = pllm.local_yes_prob(CLARITY_PROB_SYSTEM, f"User request: {t['text']}")
            n_fail += p is None
            f.write(json.dumps({"id": t["id"], "p": None if p is None else round(p, 4)}) + "\n")
    print(f"寫入 {out}；拿不到機率 {n_fail} 題")


def simulate(set_, tag):
    tasks = {t["id"]: t for t in _tasks(set_)}
    P = {j["id"]: j["p"] for j in (json.loads(x) for x in open(os.path.join(HERE, "results", f"clarity_p_{set_}.jsonl"), encoding="utf-8"))}
    rows = [json.loads(x) for x in open(os.path.join(HERE, "results", f"dev_{set_}_{tag}.jsonl"), encoding="utf-8")]
    ma = [r for r in rows if r["score"]["kind"] == "must_ask"]
    an = [r for r in rows if r["score"]["kind"] == "answer"]
    print(f"{set_}／{tag}：需要反問 {len(ma)} 題、需直接回答 {len(an)} 題；p 拿不到 {sum(P.get(r['id']) is None for r in rows)} 題（照原本結果）")

    def other_q(r):
        return [q for q in r["questions"] if q["field"] not in ("image", "cron_command")]

    def outcome(r, lo, hi):
        """回傳 (有沒有問, 映像檔對不對)。"""
        p = P.get(r["id"])
        old_img_q = any(q["field"] == "image" for q in r["questions"])
        img = r["pred"].get("image")
        if p is not None and p <= lo:
            return True, False
        if p is not None and p >= hi and old_img_q and r.get("choose_scores"):
            img = max(r["choose_scores"], key=lambda x: x[1])[0]
            old_img_q = False
        asked = old_img_q or bool(other_q(r))
        acc = {_base(x) for x in tasks[r["id"]]["expected"]["image"]["accept"]}
        return asked, _base(img) in acc

    base_hit = sum(r["score"]["passed"] for r in ma)
    base_pass = sum(r["score"]["passed"] for r in an)
    print(f"原本：整張全對 {base_pass}/{len(an)}、該問有問 {base_hit}/{len(ma)}、多問 {sum(r['score']['asked'] for r in an)}")
    print("下門檻  上門檻   整張全對(估)   該問有問   多問")
    best = []
    for lo in LOS:
        for hi in HIS:
            if hi <= lo:
                continue
            hit = sum(outcome(r, lo, hi)[0] for r in ma)
            passed = fa = 0
            for r in an:
                asked, img_ok = outcome(r, lo, hi)
                fa += asked
                ch = dict(r["score"]["checks"], image=img_ok)
                passed += (not asked) and all(ch.values())
            best.append((passed + hit, lo, hi, passed, hit, fa))
            print(f"{lo:6.2f}  {hi:6.2f}   {passed:4d}/{len(an)}      {hit:3d}/{len(ma)}    {fa:3d}")
    best.sort(reverse=True)
    print("整張全對＋該問有問 最高的 5 組：", [(b[1], b[2], b[3], b[4], b[5]) for b in best[:5]])


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", choices=[s for s in SETS if s not in ("blind", "holdout")], default="scenario")
    ap.add_argument("--score", action="store_true", help="先算每題的本地機率")
    ap.add_argument("--tag", default="cuda_school")
    a = ap.parse_args()
    if a.score:
        score_all(a.set)
    else:
        simulate(a.set, a.tag)
