"""實驗 A2：ZeroTouch 回「還缺少：映像檔」的題目，模擬新手補一句「映像檔用 nginx 就好」再送一次
（網頁上是跳出部署單讓使用者填；這裡用補一句話代替，等於使用者多做一步、而且要知道填什麼）。
只跑實驗 A 裡 ZeroTouch 被退回缺資料的題目；Claude 那邊這些題目本來就不用補。
用法：python scripts/bench/bench_followup.py [輪數=3]
"""
import json, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bench_deploy import TASKS  # noqa: E402
from common import OUT, ZT_NS, ZeroTouch, append, clean, snapshot, wait_ready  # noqa: E402

FOLLOW = "。映像檔用 nginx 就好"

if __name__ == "__main__":
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    rows = [json.loads(l) for l in (OUT / "A_deploy.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    need = sorted({r["task_id"] for r in rows if r["system"] == "zerotouch" and r["outcome"] == "rejected"
                   and "還缺少" in str((r.get("detail") or {}).get("message"))})
    print("需要補資料的題目：", need, flush=True)
    zt = ZeroTouch()
    for rnd in range(1, rounds + 1):
        for tid, cat, text, check in TASKS:
            if tid not in need:
                continue
            clean(ZT_NS)
            rec = zt.deploy(text + FOLLOW)
            ready, ready_s, state = wait_ready(ZT_NS) if rec["outcome"] in ("deployed", "deploy_failed") else (False, 0, "not-deployed")
            rec.update(ready=ready, ready_wait_s=ready_s, ready_state=state)
            snap = snapshot(ZT_NS, f"{tid}_zerotouch_followup_r{rnd}")
            passed, why = check(snap, rec)
            out = {"suite": "A2_followup", "task_id": tid, "category": cat, "input": text + FOLLOW, "system": "zerotouch",
                   "round": rnd, "outcome": rec["outcome"], "passed": bool(passed), "pass_rule": why,
                   "latency_s": rec.get("total_s"), "cost_usd": None, "extra_user_turns": 1, "snapshot": snap,
                   "detail": rec, "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
            append(OUT / "A2_followup.jsonl", out)
            print(f"[{out['ts']}] {tid} r{rnd} {rec['outcome']:10} pass={passed} 副本={snap['total_replicas']} | {why}", flush=True)
    clean(ZT_NS)
