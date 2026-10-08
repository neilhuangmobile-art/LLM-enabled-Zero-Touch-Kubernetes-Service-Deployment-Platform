"""Voice-of-customer mining: exact verbatims from HN (Algolia API) and Stack Overflow (StackExchange API).
Prints short exact excerpts around matched keywords with URLs. Output saved as JSONL for traceability."""
import html, json, re, sys, time, urllib.parse, urllib.request

OUT = sys.argv[1]
SINCE = 1704067200  # 2024-01-01

HN_QUERIES = {
    "sizing":      ["kubernetes requests limits", "kubernetes memory limit guess", "kubernetes resource requests"],
    "complexity":  ["kubernetes overkill", "kubernetes complexity small team", "kubernetes yaml"],
    "paas":        ["heroku kubernetes", "coolify kubernetes", "kubernetes paas"],
    "ai_ops":      ["k8sgpt", "llm kubectl", "ai kubernetes agent", "chatgpt kubernetes yaml"],
    "debugging":   ["crashloopbackoff", "kubernetes debugging"],
    "cost":        ["kubernetes bill", "kubernetes cost"],
}
SO_QUERIES = {
    "sizing":    "how to determine resource requests limits",
    "debugging": "CrashLoopBackOff",
    "beginner":  "deploy application kubernetes beginner",
    "oom":       "OOMKilled",
}

def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "voc-research/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            import gzip; data = gzip.decompress(data)
        return json.loads(data)

def clean(t):
    t = re.sub(r"<p>", " ", t or ""); t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", html.unescape(t)).strip()

def excerpt(text, kw, width=260):
    words = [w for w in kw.lower().split() if len(w) > 3]
    low = text.lower()
    pos = min([low.find(w) for w in words if low.find(w) >= 0] or [0])
    s = max(0, pos - width // 2)
    return ("…" if s else "") + text[s:s + width] + ("…" if s + width < len(text) else "")

rows = []
for theme, qs in HN_QUERIES.items():
    for q in qs:
        u = ("https://hn.algolia.com/api/v1/search?" + urllib.parse.urlencode(
            {"query": q, "tags": "comment", "hitsPerPage": 12, "numericFilters": f"created_at_i>{SINCE}"}))
        for h in get(u)["hits"]:
            t = clean(h.get("comment_text"))
            if len(t) < 80: continue
            rows.append({"src": "HN", "theme": theme, "query": q, "date": h["created_at"][:10],
                         "points": h.get("points"), "url": f"https://news.ycombinator.com/item?id={h['objectID']}",
                         "text": t})
        time.sleep(0.3)

for theme, q in SO_QUERIES.items():
    u = ("https://api.stackexchange.com/2.3/search/advanced?" + urllib.parse.urlencode(
        {"q": q, "tagged": "kubernetes", "site": "stackoverflow", "sort": "votes", "order": "desc",
         "pagesize": 8, "filter": "withbody"}))
    for it in get(u)["items"]:
        rows.append({"src": "StackOverflow", "theme": theme, "query": q,
                     "date": time.strftime("%Y-%m-%d", time.gmtime(it["creation_date"])),
                     "points": it.get("score"), "views": it.get("view_count"), "url": it["link"],
                     "title": html.unescape(it["title"]), "text": clean(it.get("body"))})
    time.sleep(0.5)

with open(OUT, "w", encoding="utf-8") as f:
    for r in rows: f.write(json.dumps(r, ensure_ascii=False) + "\n")
print("rows:", len(rows), {k: sum(1 for r in rows if r["theme"] == k) for k in set(r["theme"] for r in rows)})
