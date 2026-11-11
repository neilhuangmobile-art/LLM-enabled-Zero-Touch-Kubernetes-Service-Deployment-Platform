"""
qa_external_cloudeval_20261024_charts.py
外部基準（CloudEval-YAML）結果的統計與圖表：reports/qa_external_cloudeval_20261024_charts/
需要 matplotlib（專案 venv 沒裝，2026-10-24 用另一個暫存環境跑）。

另外算一個「題目有提到」的版本：標準答案的值沒有出現在題目文字裡（例如題目沒講名稱，標準答案卻要求
nginx-deploy），任何解析器都不可能答對，這種欄位不計分。判斷用固定規則（值的字串有沒有出現在題目裡），不是人工挑題。
"""
import json
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNS = {"修正前": os.path.join(ROOT, "docs", "qa_external_cloudeval_20261024_before_fix_results.jsonl"),
        "修正後": os.path.join(ROOT, "docs", "qa_external_cloudeval_20261024_results.jsonl")}
OUT_DIR = os.path.join(ROOT, "reports", "qa_external_cloudeval_20261024_charts")
os.makedirs(OUT_DIR, exist_ok=True)
matplotlib.rcParams["font.family"] = "Microsoft JhengHei"
matplotlib.rcParams["axes.unicode_minus"] = False
FIELDS = [("image", "映像檔"), ("replicas", "副本數"), ("container_port", "容器 port"), ("name", "名稱")]


def mentioned(row, k):
    v = str(row["expected"][k])
    if k == "image":
        v = v.split("/")[-1].split(":")[0]
    return v.lower() in row["input"].lower()


def stats(rows, fair=False):
    out = {}
    for k, _ in FIELDS:
        js = [r["field_correct"][k] for r in rows if k in r["field_correct"] and (not fair or mentioned(r, k))]
        out[k] = (sum(js), len(js))
    allc = [all(v for kk, v in r["field_correct"].items() if not fair or mentioned(r, kk)) for r in rows]
    out["all"] = (sum(allc), len(allc))
    return out


def main():
    data = {name: [json.loads(l) for l in open(p, encoding="utf-8")] for name, p in RUNS.items()}
    summary = {}
    for name, rows in data.items():
        for lang in ("en", "zh"):
            rs = [r for r in rows if r["lang"] == lang]
            summary[f"{name}/{lang}"] = {"原始": stats(rs), "題目有提到": stats(rs, fair=True)}
    excluded = sorted({(r["problem_key"], k) for r in data["修正後"] for k in r["field_correct"] if not mentioned(r, k)})
    json.dump({"summary": summary, "excluded_fields_not_in_question": excluded},
              open(os.path.join(OUT_DIR, "stats.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    # 圖：修正後，各欄位準確率（英文／中文 × 原始／題目有提到）
    fig, ax = plt.subplots(figsize=(10, 5), dpi=150)
    labels = [lbl for _, lbl in FIELDS] + ["全部欄位都對"]
    keys = [k for k, _ in FIELDS] + ["all"]
    series = [("英文（原始）", "修正後/en", "原始", "#9db8d6"), ("英文（題目有提到）", "修正後/en", "題目有提到", "#1f5f9f"),
              ("中文（原始）", "修正後/zh", "原始", "#b9d8c6"), ("中文（題目有提到）", "修正後/zh", "題目有提到", "#1f6f5c")]
    w = 0.2
    for i, (lbl, run, mode, color) in enumerate(series):
        vals = [summary[run][mode][k] for k in keys]
        rates = [c / n * 100 if n else 0 for c, n in vals]
        xs = [x + (i - 1.5) * w for x in range(len(keys))]
        ax.bar(xs, rates, width=w, color=color, label=lbl)
        for x, r, (c, n) in zip(xs, rates, vals):
            ax.text(x, r + 1, f"{c}/{n}", ha="center", fontsize=6.5)
    ax.set_xticks(range(len(keys)))
    ax.set_xticklabels(labels)
    ax.set_ylim(0, 112)
    ax.set_ylabel("正確率 (%)")
    ax.set_title("外部基準 CloudEval-YAML：單一容器 Deployment 20 題的部署解析（修正後）", fontsize=12)
    ax.legend(fontsize=8, ncol=2, loc="lower left")
    ax.grid(axis="y", linestyle="--", alpha=0.4)
    plt.tight_layout()
    fig.savefig(os.path.join(OUT_DIR, "field_accuracy.png"))
    plt.close(fig)
    for k, v in summary.items():
        print(k, {m: {f: f"{c}/{n}" for f, (c, n) in s.items()} for m, s in v.items()})
    print("不計分（題目沒提到）:", excluded)


if __name__ == "__main__":
    main()
