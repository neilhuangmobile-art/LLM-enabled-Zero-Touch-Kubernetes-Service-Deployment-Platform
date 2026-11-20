"""選軟體信心度門檻的離線掃描（2026-11-18）。

讀 run_pipeline_dev.py 的逐題結果（含 choose_scores＝每個候選的 P(Yes)），在不同門檻／差距下重新決定
「自動採用哪個軟體」或「反問」，估計通過數。選軟體以外的欄位（副本數、資源、port）沿用原本那一輪的判定，
所以這是估計值；選定門檻後一定要用 run_pipeline_dev.py 真的跑一次確認。

  python experiments/model_select/reason/sweep_choose_threshold.py --set scenario --tag conf
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from run_pipeline_dev import SETS, _base  # noqa: E402
from run_pipeline_eval import ROOT  # noqa: E402
from agents.pipeline import software_rag  # noqa: E402

DESC = {it["image"]: it["desc"] for it in software_rag.items()}


def _cat(img):
    return (DESC.get(img) or img).split("：", 1)[0]


def decide(scores, thr, margin):
    order = sorted(range(len(scores)), key=lambda i: (-scores[i][1], i))
    best_img, best = scores[order[0]]
    if best < thr:
        return None
    if margin > 0:
        for i in order[1:]:
            img, p = scores[i]
            if _cat(img) != _cat(best_img):
                if best - p < margin:
                    return None
                break
    return best_img


def evaluate(rows, tasks, thr, margin):
    ans_pass = img_ok = asked_ans = ask_pass = n_ans = n_ask = 0
    for r in rows:
        t = tasks[r["id"]]
        other_q = [q for q in r["questions"] if q["field"] != "image"]
        if r.get("choose_scores"):
            pick = decide(r["choose_scores"], thr, margin)
        else:                                              # 這題沒經過選軟體（使用者指定、自己的程式）
            pick = r["pred"].get("image") if not any(q["field"] == "image" for q in r["questions"]) else None
        asked = pick is None or bool(other_q)
        if r["score"]["kind"] == "must_ask":
            n_ask += 1
            ask_pass += asked
            continue
        n_ans += 1
        ok_img = pick is not None and _base(pick) in {_base(a) for a in t["expected"]["image"]["accept"]}
        others = all(v for k, v in r["score"]["checks"].items() if k != "image")
        img_ok += ok_img
        asked_ans += asked
        ans_pass += ok_img and others and not asked
    return {"thr": thr, "margin": margin, "answer_pass": ans_pass, "image_ok": img_ok, "false_ask": asked_ans,
            "must_ask_pass": ask_pass, "n_answer": n_ans, "n_must_ask": n_ask}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", choices=list(SETS), default="scenario")
    ap.add_argument("--tag", required=True)
    a = ap.parse_args()
    tasks = {t["id"]: t for t in (json.loads(x) for x in open(os.path.join(ROOT, "docs", "bench_blind", SETS[a.set]),
                                                              encoding="utf-8") if x.strip().startswith("{"))}
    rows = [json.loads(x) for x in open(os.path.join(HERE, "results", f"dev_{a.set}_{a.tag}.jsonl"), encoding="utf-8")]
    out = []
    for margin in (0, 0.05, 0.1, 0.2):
        for thr in (0.5, 0.6, 0.7, 0.8, 0.85, 0.9, 0.93, 0.95, 0.97, 0.98, 0.99):
            out.append(evaluate(rows, tasks, thr, margin))
    for o in out:
        print(f"margin {o['margin']:.2f} thr {o['thr']:.2f}  直接回答全對 {o['answer_pass']}/{o['n_answer']}  "
              f"軟體對 {o['image_ok']}  多問 {o['false_ask']}  該問有問 {o['must_ask_pass']}/{o['n_must_ask']}")
    path = os.path.join(HERE, "results", f"sweep_{a.set}_{a.tag}.json")
    json.dump(out, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("寫入", path)


if __name__ == "__main__":
    main()
