"""
qa_rerun_20261024_charts.py
把 qa_rerun_20261024.py 的結果畫成 PNG（存進 reports/qa_rerun_20261024_charts/），跟 10/07 基準線並排比較。
需要 matplotlib（專案 venv 沒裝；2026-10-24 是用另一個暫存環境跑的）：

    <有 matplotlib 的 python> qa_rerun_20261024_charts.py
"""
import json
import os
import statistics

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = os.path.dirname(os.path.abspath(__file__))
NOW = os.path.join(ROOT, "docs", "qa_rerun_20261024_classified.jsonl")
BASE = os.path.join(ROOT, "docs", "qa_sweep_20261007_classified.jsonl")
OUT_DIR = os.path.join(ROOT, "reports", "qa_rerun_20261024_charts")
os.makedirs(OUT_DIR, exist_ok=True)

matplotlib.rcParams["font.family"] = "Microsoft JhengHei"
matplotlib.rcParams["axes.unicode_minus"] = False
OLD_C, NEW_C, ACCENT = "#b8b2a7", "#1f6f5c", "#1f6f5c"


def load(p):
    return [json.loads(l) for l in open(p, encoding="utf-8")]


def rates(rows, field="passed"):
    by = {}
    for r in rows:
        by.setdefault(f"{r['suite']}/{r['category']}", []).append(r)
    out = {}
    for k, rs in by.items():
        j = [r for r in rs if r.get(field) is not None]
        if j:
            out[k] = (sum(1 for r in j if r.get(field)) / len(j) * 100, len(j))
    return out


def main():
    now, base = load(NOW), load(BASE)
    rn, rb, rm = rates(now), rates(base), rates(now, "passed_with_moderation")
    keys = sorted(rn)[::-1]

    # 圖 1：各類別通過率，10/07 與現在並排
    fig, ax = plt.subplots(figsize=(11, 10), dpi=150)
    y = range(len(keys))
    ax.barh([i + 0.2 for i in y], [rb.get(k, (0, 0))[0] for k in keys], height=0.4, color=OLD_C, label="2026-10-07（修正前）")
    ax.barh([i - 0.2 for i in y], [rn[k][0] for k in keys], height=0.4, color=NEW_C, label="2026-10-24（現在）")
    ax.set_yticks(list(y))
    ax.set_yticklabels([f"{k} (n={rn[k][1]})" for k in keys], fontsize=8)
    ax.set_xlim(0, 112)
    ax.set_xlabel("通過率 (%)")
    ax.set_title("QA 重跑：各類別通過率（同一批題目、同一套判分規則）", fontsize=13)
    ax.grid(axis="x", linestyle="--", alpha=0.4)
    diff = [k for k in keys if abs(rm[k][0] - rn[k][0]) > 0.01]
    ax.barh([keys.index(k) - 0.2 for k in diff], [rm[k][0] - rn[k][0] for k in diff], left=[rn[k][0] for k in diff],
            height=0.4, color="#8fc3b3", label="補充判定：被誘導偵測擋下也算拒絕")
    for i, k in enumerate(keys):
        v = rm[k][0]
        ax.text(v + 1, i - 0.2, f"{rn[k][0]:.0f}%" + (f" → {v:.0f}%" if k in diff else ""), va="center", fontsize=7)
    ax.legend(loc="upper center", bbox_to_anchor=(0.4, -0.05), ncol=3, fontsize=9)
    plt.tight_layout()
    fig.savefig(os.path.join(OUT_DIR, "pass_rate_by_category.png"))
    plt.close(fig)

    # 圖 2：延遲分布（不含次數上限的等待時間）
    def hist(rows):
        b = [0, 500, 1000, 2000, 5000, 10000, 20000, 10 ** 9]
        c = [0] * (len(b) - 1)
        for r in rows:
            v = r.get("latency_ms") or 0
            for i in range(len(b) - 1):
                if b[i] <= v < b[i + 1]:
                    c[i] += 1
                    break
        return c
    labels = ["0-0.5s", "0.5-1s", "1-2s", "2-5s", "5-10s", "10-20s", "20s+"]
    cn, cb = hist(now), hist(base)
    fig2, ax2 = plt.subplots(figsize=(10, 5), dpi=150)
    x = range(len(labels))
    ax2.bar([i - 0.2 for i in x], cb, width=0.4, color=OLD_C, label="2026-10-07")
    ax2.bar([i + 0.2 for i in x], cn, width=0.4, color=NEW_C, label="2026-10-24")
    ax2.set_xticks(list(x))
    ax2.set_xticklabels(labels)
    ax2.set_ylabel("題數")
    ln = [r["latency_ms"] for r in now if r.get("latency_ms")]
    ax2.set_title(f"延遲分布（現在：平均 {statistics.mean(ln) / 1000:.1f}s，中位數 {sorted(ln)[len(ln) // 2] / 1000:.1f}s）", fontsize=13)
    ax2.grid(axis="y", linestyle="--", alpha=0.4)
    ax2.legend()
    plt.tight_layout()
    fig2.savefig(os.path.join(OUT_DIR, "latency_distribution.png"))
    plt.close(fig2)

    # 圖 3：整體摘要
    def total(rows):
        j = [r for r in rows if r["passed"] is not None]
        return sum(1 for r in j if r["passed"]), len(j)
    pn, jn = total(now)
    pb, jb = total(base)
    pm = sum(1 for r in now if r.get("passed_with_moderation"))
    fig3, ax3 = plt.subplots(figsize=(10, 3), dpi=150)
    ax3.axis("off")
    cards = [(f"{pb / jb * 100:.1f}%", f"10/07 修正前（{pb}/{jb}）"), (f"{pn / jn * 100:.1f}%", f"10/24 現在（{pn}/{jn}）"),
             (f"{pm / jn * 100:.1f}%", f"補充判定（誘導偵測擋下算拒絕）"), (f"{statistics.mean(ln) / 1000:.1f}s", f"平均延遲（{len(now)} 筆）")]
    for i, (num, lbl) in enumerate(cards):
        xx = 0.12 + i * 0.25
        ax3.text(xx, 0.6, num, ha="center", fontsize=24, fontweight="bold", color=ACCENT if i else OLD_C, transform=ax3.transAxes)
        ax3.text(xx, 0.25, lbl, ha="center", fontsize=11, color="#444", transform=ax3.transAxes)
    plt.tight_layout()
    fig3.savefig(os.path.join(OUT_DIR, "summary_stats.png"))
    plt.close(fig3)
    print("saved:", OUT_DIR)


if __name__ == "__main__":
    main()
