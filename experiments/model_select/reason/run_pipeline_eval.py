"""2026-11-16 用組員的 S01～S40 測整個多代理人流程（agents/pipeline），評分沿用 run_reason.py。

和 run_reason.py（只有推理代理）的差別：先經過正規化（Gemini 或本地）、意圖檢查、空缺檢查、查表。
流程的輸出換成評分格式時：
- 背景程式／排程的 port：部署格式一定要有 port，流程給的是預設 80 且只開內部；評分時當作「沒有 port」。
- 資料庫密碼：web_demo._prepare_deploy 會替資料庫映像檔自動產生密碼（deploy_fill.db_required_env），所以看映像檔。
- 問題：流程問了任何問題就算有反問。

用法（專案 venv，要先開 core/pipeline_model_server.py）：
  python experiments/model_select/reason/run_pipeline_eval.py --normalizer gemini
  python experiments/model_select/reason/run_pipeline_eval.py --normalizer local
"""
import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

from run_reason import load_tests, score_one, summarize  # noqa: E402
from agents.pipeline import runner  # noqa: E402
from agents.pipeline.llm import gemini_llm, local_llm  # noqa: E402
from agents.deploy_fill import _DB_ENV  # noqa: E402
from agents.cost_agent import _image_name  # noqa: E402


def to_pred(spec):
    b = spec["base"]
    wl = spec["workload"]["value"]
    port = b["port"]["value"]
    if wl in ("worker", "cronjob") and b["port"]["source"] == "default" and port == 80:
        port = None
    sched = [e for e in spec["extras"] if e["kind"] == "schedule" and e["status"] != "dropped"]
    from agents.pipeline.reasoner import valid_cron
    cron = valid_cron(sched[0]["value"]) if sched else None
    vols = spec["manifest"].get("volumes") or []
    # 有保存需求但超過上限（等使用者回答）也算有辨識出來；大小用使用者要的
    st = None
    for e in spec["extras"]:
        if e["kind"] == "storage" and e["status"] in ("applied", "unsupported", "pending"):
            want = (e["value"] or {}).get("size") if isinstance(e["value"], dict) else None
            st = {"size": want or (vols[0]["size"] if vols else "1Gi")}
    return {"questions": spec["questions"], "workload": "cronjob" if sched else wl,
            "image": b["image"]["value"], "app_name": b["app_name"]["value"], "replicas": b["replicas"]["value"],
            "port": port, "memory": b["memory"]["value"], "cpu": b["cpu"]["value"],
            "expose": spec["expose"]["value"], "schedule": cron, "storage": st,
            "db_password": _image_name(b["image"]["value"] or "") in _DB_ENV}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--normalizer", choices=["gemini", "local"], default="gemini")
    ap.add_argument("--limit", type=int)
    a = ap.parse_args()
    last = [0.0]
    waited = {"s": 0.0}

    def gemini_throttled(system, user):        # 免費層每把 key 每分鐘 5 次；等待時間不算進延遲
        wait = 13 - (time.time() - last[0])
        if wait > 0:
            time.sleep(wait)
            waited["s"] += wait
        last[0] = time.time()
        return gemini_llm(system, user)

    tests = {t["id"]: t for t in load_tests()}
    items = list(tests.values())[: a.limit] if a.limit else list(tests.values())
    path = os.path.join(HERE, "results", f"pipeline_{a.normalizer}.jsonl")
    rows = []
    with open(path, "w", encoding="utf-8") as f:
        for t in items:
            waited["s"] = 0.0
            t_start = time.time()
            gem = gemini_throttled if a.normalizer == "gemini" else None
            spec = runner.run(t["text"], gem, local_llm)
            # 延遲只算流程本身：扣掉為了免費層額度刻意等待的時間
            lat = round(spec.get("total_ms", 0) / 1000 - waited["s"], 2)
            pred = to_pred(spec)
            row = {"id": t["id"], "latency_s": lat, "wall_s": round(time.time() - t_start, 2),
                   "spec": runner._public(spec), "pred": pred, "raw": json.dumps(pred, ensure_ascii=False),
                   "score": score_one(t, pred)}
            rows.append(row)
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()
            bad = [k for k, v in row["score"]["checks"].items() if not v]
            print(f"{t['id']} {lat:6.2f}s pass={int(row['score']['pass'])} 錯={bad} "
                  f"backend={spec['backend'].get('normalize')} q={len(spec['questions'])}", flush=True)
        s = summarize(rows, tests)
        s["normalizer"] = a.normalizer
        f.write(json.dumps({"summary": s}, ensure_ascii=False) + "\n")
    print(json.dumps(s, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
