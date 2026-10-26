"""
qa_full_20261026_summarize.py — 全系統測試（2026-10-26）摘要與圖表

讀 docs/qa_full_20261026_results.jsonl（同一個 id 以最後一筆為準），輸出：
    docs/qa_full_20261026_summary.json   各類別通過數、失敗項目、延遲統計
    docs/qa_full_20261026_by_group.svg   各類別通過率長條圖（深淺色都可讀，滑過顯示數字）
用法：python qa_full_20261026_summarize.py
"""
import json
import os
import re
from html import escape

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(ROOT, "docs", "qa_full_20261026_results.jsonl")
SUMMARY = os.path.join(ROOT, "docs", "qa_full_20261026_summary.json")
SVG = os.path.join(ROOT, "docs", "qa_full_20261026_by_group.svg")


def load():
    latest, total_rows = {}, 0
    with open(SRC, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                row = json.loads(line)
                latest[row["id"]] = row
                total_rows += 1
    return latest, total_rows


def _order(group):
    """「B4/D27 頁面」排在 B5 前、B10 在 B9 後：先比字母，再比數字。"""
    m = re.match(r"([A-Z])(\d*)", group)
    return (m.group(1), int(m.group(2) or 0)) if m else ("~", 0)


def summarize(latest, total_rows):
    groups = {}
    for r in latest.values():
        g = groups.setdefault(r["group"], {"passed": 0, "total": 0})
        g["total"] += 1
        g["passed"] += int(r["passed"])
    lat = sorted(r["latency_ms"] for r in latest.values() if r.get("latency_ms"))
    pct = lambda p: lat[min(len(lat) - 1, int(p * len(lat)))] if lat else None
    passed = sum(g["passed"] for g in groups.values())
    return {"run": "2026-10-26", "total_records": total_rows, "tests": len(latest), "passed": passed,
            "pass_rate": round(passed / len(latest), 3),
            "latency_ms": {"n": len(lat), "p50": pct(0.5), "p90": pct(0.9), "max": lat[-1] if lat else None},
            "by_group": dict(sorted(groups.items(), key=lambda kv: _order(kv[0]))),
            "failed": [{"id": r["id"], "group": r["group"], "name": r["name"], "actual": str(r["actual"])[:200]}
                       for r in sorted(latest.values(), key=lambda r: r["id"]) if not r["passed"]]}


def svg_chart(s):
    rows = list(s["by_group"].items())
    row_h, label_w, bar_w, top = 26, 300, 360, 56
    w, h = label_w + bar_w + 80, top + row_h * len(rows) + 30
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}" '
           f'font-family="Noto Sans TC, Microsoft JhengHei, sans-serif" role="img" '
           f'aria-label="各類別通過率：{s["passed"]}/{s["tests"]}">',
           "<style>.bg{fill:#fcfcfb}.t1{fill:#0b0b0b}.t2{fill:#52514e}.track{fill:#e1e0d9}.bar{fill:#2a78d6}"
           ".bar:hover{opacity:.8}@media (prefers-color-scheme:dark){.bg{fill:#1a1a19}.t1{fill:#f0efec}"
           ".t2{fill:#c3c2b7}.track{fill:#2c2c2a}.bar{fill:#3987e5}}</style>",
           f'<rect class="bg" width="{w}" height="{h}"/>',
           f'<text class="t1" x="16" y="26" font-size="16" font-weight="600">全系統測試各類別通過率（2026-10-26）</text>',
           f'<text class="t2" x="16" y="44" font-size="12">共 {s["tests"]} 項，通過 {s["passed"]} 項'
           f'（{s["pass_rate"] * 100:.1f}%）；同一項重測以最後一次為準</text>']
    for i, (name, g) in enumerate(rows):
        y = top + i * row_h
        frac = g["passed"] / g["total"]
        tip = escape(f"{name}：{g['passed']}/{g['total']}（{frac * 100:.0f}%）")
        out.append(f'<g><title>{tip}</title>'
                   f'<text class="t2" x="{label_w - 8}" y="{y + 15}" font-size="12" text-anchor="end">{escape(name)}</text>'
                   f'<rect class="track" x="{label_w}" y="{y + 5}" width="{bar_w}" height="12" rx="4"/>'
                   f'<rect class="bar" x="{label_w}" y="{y + 5}" width="{max(bar_w * frac, 4):.1f}" height="12" rx="4"/>'
                   f'<text class="t1" x="{label_w + bar_w + 8}" y="{y + 15}" font-size="12">{g["passed"]}/{g["total"]}</text></g>')
    out.append("</svg>")
    return "\n".join(out)


def main():
    latest, total_rows = load()
    s = summarize(latest, total_rows)
    with open(SUMMARY, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=1)
    with open(SVG, "w", encoding="utf-8") as f:
        f.write(svg_chart(s))
    print(json.dumps({k: s[k] for k in ("tests", "passed", "pass_rate", "latency_ms")}, ensure_ascii=False))
    for x in s["failed"]:
        print("FAIL", x["id"], x["name"], x["actual"][:100])


if __name__ == "__main__":
    main()
