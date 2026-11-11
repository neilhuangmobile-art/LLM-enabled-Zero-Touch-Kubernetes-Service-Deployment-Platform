"""
qa_sweep_20261007_charts.py
把 qa_sweep_20261007_classified.jsonl 的統計結果輸出成真正的 PNG 圖片檔
（不是網頁互動圖表），存進 reports/，供研究報告/口試直接貼圖使用。

用 matplotlib 畫、存成檔案，不依賴瀏覽器或 CDN，產出的檔案可以直接用 Read 工具看、
或拿去塞進 Word/PPT/PDF。
"""
import json
import os
import statistics

import matplotlib
matplotlib.use("Agg")  # 不需要顯示視窗，純存檔
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IN_PATH = os.path.join(ROOT, "docs", "qa_sweep_20261007_classified.jsonl")
OUT_DIR = os.path.join(ROOT, "reports", "qa_sweep_20261007_charts")
os.makedirs(OUT_DIR, exist_ok=True)

# 這台 Windows 機器有微軟正黑體，matplotlib 預設字型不含中文字會顯示成方框，
# 一定要明確指定，不然圖表上的中文全部變亂碼。
matplotlib.rcParams["font.family"] = "Microsoft JhengHei"
matplotlib.rcParams["axes.unicode_minus"] = False

GOOD, ACCENT, WARN, BAD = "#1f7a4d", "#1f6f5c", "#a6761f", "#b3401f"


def bar_color(rate):
    if rate >= 90: return GOOD
    if rate >= 75: return ACCENT
    if rate >= 50: return WARN
    return BAD


def main():
    rows = [json.loads(l) for l in open(IN_PATH, encoding="utf-8")]
    by_cat = {}
    for r in rows:
        by_cat.setdefault((r["suite"], r["category"]), []).append(r)

    cats = []
    for (suite, cat), rs in sorted(by_cat.items()):
        judged = [r for r in rs if r["passed"] is not None]
        p = sum(1 for r in judged if r["passed"])
        if judged:
            cats.append({
                "label": f"{suite}/{cat} (n={len(rs)})",
                "rate": p / len(judged) * 100,
            })
    cats.reverse()  # 畫水平長條時，由上到下跟表格順序一致

    # ── 圖 1：各類別通過率 ──────────────────────────────────────
    fig, ax = plt.subplots(figsize=(11, 9), dpi=150)
    labels = [c["label"] for c in cats]
    rates = [c["rate"] for c in cats]
    colors = [bar_color(r) for r in rates]
    ax.barh(labels, rates, color=colors)
    ax.set_xlim(0, 100)
    ax.set_xlabel("通過率 (%)")
    ax.set_title("2026-10-07 QA 掃描：各類別通過率（261 筆真實請求，修復前基準線）", fontsize=13)
    ax.grid(axis="x", linestyle="--", alpha=0.4)
    for i, r in enumerate(rates):
        ax.text(r + 1, i, f"{r:.0f}%", va="center", fontsize=8)
    plt.tight_layout()
    path1 = os.path.join(OUT_DIR, "pass_rate_by_category.png")
    fig.savefig(path1)
    plt.close(fig)
    print("saved:", path1)

    # ── 圖 2：延遲分布 ──────────────────────────────────────────
    lat = sorted(r["latency_ms"] for r in rows)
    buckets = [0, 500, 1000, 2000, 5000, 10000, 20000, 35000]
    bucket_labels = ["0-0.5s", "0.5-1s", "1-2s", "2-5s", "5-10s", "10-20s", "20-35s"]
    counts = [0] * (len(buckets) - 1)
    for v in lat:
        for i in range(len(buckets) - 1):
            if buckets[i] <= v < buckets[i + 1]:
                counts[i] += 1
                break

    fig2, ax2 = plt.subplots(figsize=(9, 5), dpi=150)
    ax2.bar(bucket_labels, counts, color=ACCENT)
    ax2.set_ylabel("題數")
    mean_v = statistics.mean(lat)
    median_v = sorted(lat)[len(lat)//2]
    ax2.set_title(f"261 筆真實呼叫延遲分布（平均 {mean_v/1000:.1f}s，中位數 {median_v/1000:.1f}s）", fontsize=13)
    for i, c in enumerate(counts):
        ax2.text(i, c + 0.8, str(c), ha="center", fontsize=9)
    ax2.grid(axis="y", linestyle="--", alpha=0.4)
    plt.tight_layout()
    path2 = os.path.join(OUT_DIR, "latency_distribution.png")
    fig2.savefig(path2)
    plt.close(fig2)
    print("saved:", path2)

    # ── 圖 3：整體摘要（大數字卡片風格，單張圖方便貼進報告封面）───
    total_judged = sum(1 for r in rows if r["passed"] is not None)
    total_passed = sum(1 for r in rows if r["passed"] is True)
    fig3, ax3 = plt.subplots(figsize=(9, 3), dpi=150)
    ax3.axis("off")
    stats_ = [
        (f"{total_passed}/{total_judged}", "可判定題目通過數"),
        (f"{total_passed/total_judged*100:.1f}%", "整體通過率"),
        (f"{len(rows)}", "真實請求總數"),
        (f"{mean_v/1000:.1f}s", "平均延遲"),
    ]
    for i, (num, lbl) in enumerate(stats_):
        x = 0.12 + i * 0.25
        ax3.text(x, 0.6, num, ha="center", fontsize=24, fontweight="bold", color=ACCENT, transform=ax3.transAxes)
        ax3.text(x, 0.25, lbl, ha="center", fontsize=11, color="#444", transform=ax3.transAxes)
    plt.tight_layout()
    path3 = os.path.join(OUT_DIR, "summary_stats.png")
    fig3.savefig(path3)
    plt.close(fig3)
    print("saved:", path3)


if __name__ == "__main__":
    main()
