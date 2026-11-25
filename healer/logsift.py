"""
healer/logsift.py
Healer 補強方案階段 4.1／4.2（docs/healer_plan_20261120.md，2026-11-25）：給監控小模型的證據整理。

- sift(logs, query)：log 先去重再挑重點。去掉時間戳、UUID、IP、十六進位、數字後，把重複的行收斂成
  「模板 ×次數」，再用 BM25 以錯誤關鍵字與 K8s reason 當查詢挑最相關的行，總長控制在 limit 字以內。
  1.5B 模型只看得到 800 字，同樣的長度去重後能涵蓋更多不同的錯誤（依據：TriFleetRCA 2026）。
  規則層仍然看原始 log，這裡只影響送給模型的內容。
- pick_runbook(query)：從 healer/runbooks/*.md 用同一套 BM25 挑一篇最相關的處理手冊摘要給模型參考
  （依據：HolmesGPT 案例，有 runbook 4.6/5、沒有 3.6/5，單一案例僅供參考）。

不加套件，BM25 自己寫（幾十行、資料量很小）。
"""
from __future__ import annotations

import math
import os
import re
from collections import Counter, OrderedDict
from typing import Dict, List, Optional, Tuple

_MASKS = [
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b"), "<time>"),
    (re.compile(r"\b\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\b"), "<time>"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<uuid>"),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b"), "<ip>"),
    (re.compile(r"\b0x[0-9a-f]+\b|\b[0-9a-f]{12,}\b", re.I), "<hex>"),
    (re.compile(r"\d+"), "<n>"),          # 包含 12ms、attempt3 這種黏在字上的數字
]

# 查詢一定帶上的錯誤字眼（再加上 K8s reason 與事件 reason）
BASE_QUERY = ("error fatal panic exception failed failure refused denied unauthorized forbidden "
              "killed oom memory heap timeout timed not found missing cannot unable traceback exit")

_TOKEN = re.compile(r"[a-z][a-z0-9_]+|[一-鿿]")


def template(line: str) -> str:
    t = line.strip()
    for rx, rep in _MASKS:
        t = rx.sub(rep, t)
    return re.sub(r"\s+", " ", t)


def tokens(text: str) -> List[str]:
    return _TOKEN.findall((text or "").lower())


def bm25_scores(query: str, docs: List[str], k1: float = 1.5, b: float = 0.75) -> List[float]:
    toks = [tokens(d) for d in docs]
    n = len(docs)
    if not n:
        return []
    avg = sum(len(t) for t in toks) / n or 1.0
    df = Counter(w for t in toks for w in set(t))
    q = set(tokens(query))
    out = []
    for t in toks:
        tf = Counter(t)
        s = 0.0
        for w in q:
            if w not in tf:
                continue
            idf = math.log(1 + (n - df[w] + 0.5) / (df[w] + 0.5))
            s += idf * tf[w] * (k1 + 1) / (tf[w] + k1 * (1 - b + b * len(t) / avg))
        out.append(s)
    return out


def dedupe(logs: str) -> List[Tuple[str, int, int]]:
    """回傳 [(代表行, 次數, 最後出現的位置)]，依第一次出現的順序。代表行用最後一次出現的原文（最新）。"""
    groups: "OrderedDict[str, list]" = OrderedDict()
    for i, line in enumerate((logs or "").splitlines()):
        if not line.strip():
            continue
        key = template(line)
        g = groups.setdefault(key, [line.strip(), 0, i])
        g[0], g[1], g[2] = line.strip(), g[1] + 1, i
    return [tuple(v) for v in groups.values()]


def sift(logs: str, query: str = "", limit: int = 800, keep_tail: int = 3) -> str:
    """去重＋挑重點。結果依原本的時間順序排列；最後 keep_tail 行一定保留（崩潰前最後印的通常最關鍵）。"""
    groups = dedupe(logs)
    if not groups:
        return ""
    lines = [f"{line} (x{cnt})" if cnt > 1 else line for line, cnt, _ in groups]
    if sum(len(x) + 1 for x in lines) <= limit:
        return "\n".join(lines)
    scores = bm25_scores(BASE_QUERY + " " + (query or ""), [g[0] for g in groups])
    tail = set(sorted(range(len(groups)), key=lambda i: groups[i][2])[-keep_tail:])
    order = sorted(range(len(groups)), key=lambda i: (i not in tail, -scores[i], -groups[i][2]))
    chosen, used = [], 0
    for i in order:
        piece = lines[i][:300]
        if used + len(piece) + 1 > limit:
            continue
        chosen.append(i)
        used += len(piece) + 1
    chosen.sort(key=lambda i: groups[i][2])
    return "\n".join(lines[i][:300] for i in chosen)


# ── 處理手冊 ───────────────────────────────────────────────────────
_RUNBOOK_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "runbooks")
_runbooks: Optional[List[Dict]] = None


def _load_runbooks() -> List[Dict]:
    global _runbooks
    if _runbooks is None:
        out = []
        for fn in sorted(os.listdir(_RUNBOOK_DIR)) if os.path.isdir(_RUNBOOK_DIR) else []:
            if not fn.endswith(".md"):
                continue
            with open(os.path.join(_RUNBOOK_DIR, fn), encoding="utf-8") as f:
                text = f.read()
            m = re.search(r"^keywords:\s*(.+)$", text, re.M)
            summary = re.search(r"^## 給模型的摘要\s*\n(.+?)(?:\n## |\Z)", text, re.S | re.M)
            out.append({"name": fn[:-3], "keywords": (m.group(1) if m else ""),
                        "summary": " ".join((summary.group(1) if summary else "").split()), "text": text})
        _runbooks = out
    return _runbooks


def pick_runbook(query: str, limit: int = 500) -> Optional[Dict]:
    """回傳 {"name", "summary"}；沒有相關的（分數 0）回傳 None。比對用 keywords＋全文，關鍵字加權。"""
    books = _load_runbooks()
    if not books or not query:
        return None
    docs = [(b["keywords"] + " ") * 3 + b["text"] for b in books]
    scores = bm25_scores(query, docs)
    best = max(range(len(books)), key=lambda i: scores[i])
    if scores[best] <= 0:
        return None
    return {"name": books[best]["name"], "summary": books[best]["summary"][:limit]}
