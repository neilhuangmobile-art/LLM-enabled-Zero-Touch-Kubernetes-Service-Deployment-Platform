"""產生部署單信心度的統計表 agents/pipeline/calibration.json（2026-11-18）。

讀 run_pipeline_dev.py 的逐題結果（需要直接回答的題目），統計「系統自己填的欄位」實測正確率：
- 映像檔：依選軟體時該軟體的模型分數 P(Yes) 分三組（≥0.98、0.95–0.98、0.8–0.95）
- 副本數／port／記憶體／CPU：依來源（inferred 推理代理推的、default 程式預設值）分組
port 只算題目有給 port 答案的；樣本少於 MIN_N 的組不放（前端就不顯示數字，不用少量樣本唬人）。

  python experiments/model_select/reason/calibrate_choose.py scenario:conf claude:conf claude:cat57
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from run_pipeline_dev import SETS, _base  # noqa: E402
from run_pipeline_eval import ROOT  # noqa: E402
from agents.pipeline import defaults, software_rag  # noqa: E402

EDGES = (0.98, 0.95, 0.8)   # 採用門檻 0.8 以下沒有被採用的樣本，不量、不顯示
MIN_N = 20
OUT = os.path.join(ROOT, "agents", "pipeline", "calibration.json")


def main(specs):
    img = {e: [0, 0] for e in EDGES}
    fields = {}
    used = []
    for spec in specs:
        set_, tag = spec.split(":")
        tasks = {t["id"]: t for t in (json.loads(x) for x in open(os.path.join(ROOT, "docs", "bench_blind", SETS[set_]),
                                                                  encoding="utf-8") if x.strip().startswith("{"))}
        rows = [json.loads(x) for x in open(os.path.join(HERE, "results", f"dev_{set_}_{tag}.jsonl"), encoding="utf-8")]
        used.append(f"{set_}:{tag}（{len(rows)} 題）")
        for r in rows:
            t = tasks[r["id"]]
            if r["score"]["kind"] != "answer" or not r["pred"].get("image"):
                continue
            src = r.get("sources") or {}
            # 映像檔：用選到的那個軟體的分數
            scores = dict((i, p) for i, p in (r.get("choose_scores") or []))
            repo = software_rag.repo_of(r["pred"]["image"])
            if repo in scores and src.get("image", "inferred") == "inferred":
                e = next((x for x in EDGES if scores[repo] >= x), None)
                if e is None:
                    continue
                img[e][0] += _base(r["pred"]["image"]) in {_base(a) for a in t["expected"]["image"]["accept"]}
                img[e][1] += 1
            for f, key in (("replicas", "replicas"), ("port", "port"), ("memory", "memory"), ("cpu", "cpu")):
                s = src.get(f)
                if s not in ("inferred", "default"):
                    continue
                if f == "port" and s == "default":
                    # 查表得到的 port 和查不到時的備用值 80 分開算（部署單只替查表的顯示數字）
                    s = "default_table" if defaults.listen_port(r["pred"]["image"]) else "default_fallback"
                if f == "port" and not t["expected"]["port"].get("accept"):
                    continue
                c = fields.setdefault(f, {}).setdefault(s, [0, 0])
                c[0] += bool(r["score"]["checks"][key])
                c[1] += 1
    table = {
        "built": __import__("time").strftime("%Y-%m-%d %H:%M"),
        "source": "開發題組實測：" + "、".join(used),
        "image_bins": [[e, round(ok / n, 3), n] for e, (ok, n) in img.items() if n >= MIN_N],
        "fields": {f: {s: [round(ok / n, 3), n] for s, (ok, n) in d.items() if n >= MIN_N} for f, d in fields.items()},
    }
    if len(table["image_bins"]) < len(EDGES):
        print("警告：映像檔有分組樣本不足，整份映像檔分組不放；calibration.py 會改用內建分組")
        table.pop("image_bins")
    json.dump(table, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps(table, ensure_ascii=False, indent=1))
    print("寫入", OUT)


if __name__ == "__main__":
    main(sys.argv[1:] or ["scenario:conf"])
