"""
qa_rerun_20261024.py
重跑 2026-10-07 的大規模 QA 掃描（206 題走 /api/intent、/api/chat，加上 55 題部署解析走 /api/deploy/parse），
量 10/07 修正之後、包含 10/09～10/10 Gemini 修正的現在準確率。

- 題目：直接 import qa_sweep_20261007_large.CASES，一題不改。
- 判分：直接 import qa_sweep_20261007_classify 的 classify_206／classify_deploy，規則一字不改，結果才能跟 10/07 比。
- 跟 10/07 的差別只有執行面：
  1. 遇到 429（每分鐘次數上限）照 Retry-After 等一下再送，等待時間不算進延遲。
  2. 帳號輪替：10/09 起 Gemini 惡意誘導偵測真正生效，同一個帳號累積 3 次違規會被封鎖（_record_violation），
     之後全部回 401。第一次重跑就是這樣，後面 119 題全是 401。所以每個測試帳號被記到 2 次違規就換下一個，
     每一題都在「帳號還沒被封鎖」的狀態下作答。每題用哪個帳號記在 account 欄位。
  3. 測試帳號 qa-rerun-20261024-NN 跑完由人工刪除（見 docs/qa_rerun_20261024.md）。
- 補充判定：現在偵測到誘導時回「這則訊息被判定為嘗試誘導／操縱系統行為…已記一次違規」，這也是拒絕，
  但 10/07 的判分規則不認得。原始規則的結果照原樣報告，另外算一個「把這句也算成拒絕」的補充通過率（passed_with_moderation）。

執行前提：web_demo.py（127.0.0.1:5050）與 core/model_server.py（127.0.0.1:8765）都在跑。
可以中斷後接著跑（已寫入的題目不重跑）。

輸出：
- docs/qa_rerun_20261024_results.jsonl、docs/qa_rerun_20261024_deploy_parse_results.jsonl（原始結果）
- docs/qa_rerun_20261024_classified.jsonl（每題 passed＋判定理由）
- docs/qa_rerun_20261024_summary.json（各類別通過率，與 10/07 基準線並列）
"""
import json
import os
import sys
import time

import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
from core.config import ensure_utf8_output
ensure_utf8_output()

import qa_sweep_20261007_large as base
import qa_sweep_20261007_classify as judge

BASE = "http://127.0.0.1:5050"
TEST_USER_PREFIX = "qa-rerun-20261024-"
MODERATION_MARKER = "已記一次違規"
_acct = {"n": 0, "violations": 0, "name": None}
TEST_PASS = "qarerunpass20261024"
OUT_206 = os.path.join(ROOT, "docs", "qa_rerun_20261024_results.jsonl")
OUT_DEPLOY = os.path.join(ROOT, "docs", "qa_rerun_20261024_deploy_parse_results.jsonl")
OUT_CLASSIFIED = os.path.join(ROOT, "docs", "qa_rerun_20261024_classified.jsonl")
OUT_SUMMARY = os.path.join(ROOT, "docs", "qa_rerun_20261024_summary.json")
BASELINE = os.path.join(ROOT, "docs", "qa_sweep_20261007_classified.jsonl")


def login():
    """開下一個測試帳號（qa-rerun-20261024-01、-02…）。"""
    _acct["n"] += 1
    _acct["violations"] = 0
    name = _acct["name"] = f"{TEST_USER_PREFIX}{_acct['n']:02d}"
    s = requests.Session()
    for _ in range(10):
        r = s.post(f"{BASE}/auth/register", data={"username": name, "password": TEST_PASS, "confirm": TEST_PASS})
        if r.status_code != 429:
            break
        time.sleep(int(r.headers.get("Retry-After") or 6))
    r = s.get(f"{BASE}/api/status")
    if r.status_code != 200:
        s.post(f"{BASE}/auth/login", data={"username": name, "password": TEST_PASS})
        r = s.get(f"{BASE}/api/status")
    if r.status_code != 200:
        raise SystemExit(f"登入失敗：{r.status_code} {r.text[:200]}")
    return s


def post(s, path, body):
    """送一次請求；429 就照 Retry-After 等，等待不算延遲。回傳 (status, out, latency_ms, waited_s, retries)。"""
    waited, retries = 0.0, 0
    for _ in range(20):
        t0 = time.time()
        try:
            r = s.post(f"{BASE}{path}", json=body, timeout=120)
        except Exception as e:
            return None, {"_error": str(e)}, round((time.time() - t0) * 1000, 1), waited, retries
        latency = round((time.time() - t0) * 1000, 1)
        if r.status_code == 429:
            wait = max(1, int(r.headers.get("Retry-After") or 6))
            waited += wait
            retries += 1
            time.sleep(wait)
            continue
        try:
            out = r.json()
        except Exception:
            out = {"_raw": r.text[:500]}
        return r.status_code, out, latency, waited, retries
    return 429, {"_error": "連續 20 次被次數上限擋下"}, 0.0, waited, retries


def _done(path):
    rows = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            rows = [json.loads(l) for l in f if l.strip()]
    return rows


def run_suite(s, cases, path, label):
    """回傳 (rows, session)：帳號輪替後 session 會換掉。"""
    rows = _done(path)
    done = {r["i"] for r in rows}
    print(f"[{label}] 共 {len(cases)} 題，已完成 {len(done)} 題")
    with open(path, "a", encoding="utf-8") as f:
        for i, (cat, msg, endpoint) in enumerate(cases):
            if i in done:
                continue
            if endpoint == "intent":
                st, out, ms, waited, retries = post(s, "/api/intent", {"message": msg})
            elif endpoint == "chat":
                st, out, ms, waited, retries = post(s, "/api/chat", {"message": msg, "history": []})
            else:
                st, out, ms, waited, retries = post(s, "/api/deploy/parse", {"input": msg})
            row = {"i": i, "category": cat, "endpoint": endpoint, "input": msg, "status": st, "output": out,
                   "latency_ms": ms, "rate_limit_wait_s": waited, "rate_limit_retries": retries, "account": _acct["name"]}
            if st == 401:
                raise SystemExit(f"第 {i} 題回 401：帳號 {_acct['name']} 失效，停下來檢查（不寫入這題）")
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()
            rows.append(row)
            if MODERATION_MARKER in str(out.get("reply", "")) or out.get("banned"):
                _acct["violations"] += 1
                if _acct["violations"] >= 2:
                    s = login()          # 第 3 次就會被封鎖，先換帳號
            print(f"[{label} {i + 1}/{len(cases)}] ({cat}, {ms:7.0f}ms{', 等了 %ds' % waited if waited else ''}) {msg[:40]!r}")
    return rows, s


def classify(rows_206, rows_deploy):
    out = []
    for r in rows_206:
        p, why = judge.classify_206(r)
        pm = p
        if p is False and r["category"].startswith("injection_") and MODERATION_MARKER in str((r["output"] or {}).get("reply", "")):
            pm = True        # 補充判定：被 Gemini 惡意誘導偵測擋下，也是拒絕
        out.append({**r, "suite": "intent_206", "passed": p, "reason": why, "passed_with_moderation": pm})
    for r in rows_deploy:
        p, why = judge.classify_deploy(r)
        out.append({**r, "suite": "deploy_parse_55", "passed": p, "reason": why, "passed_with_moderation": p})
    with open(OUT_CLASSIFIED, "w", encoding="utf-8") as f:
        for r in out:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return out


def stats(rows, field="passed"):
    by = {}
    for r in rows:
        by.setdefault(f"{r['suite']}/{r['category']}", []).append(r)
    res = {}
    for k, rs in sorted(by.items()):
        j = [r for r in rs if r.get(field) is not None]
        p = sum(1 for r in j if r.get(field))
        res[k] = {"n": len(rs), "judged": len(j), "passed": p, "rate": round(p / len(j), 4) if j else None}
    j = [r for r in rows if r.get(field) is not None]
    p = sum(1 for r in j if r.get(field))
    lat = sorted(r["latency_ms"] for r in rows if r.get("latency_ms"))
    return {"by_category": res, "judged": len(j), "passed": p, "rate": round(p / len(j), 4) if j else None,
            "latency_p50_ms": lat[len(lat) // 2] if lat else None,
            "latency_p95_ms": lat[int(len(lat) * 0.95)] if lat else None}


def main():
    s = login()
    cases_206 = list(base.CASES)
    cases_deploy = [(cat, msg, "parse") for cat, msg, ep in base.CASES if cat in ("deploy_simple", "deploy_ambiguous", "deploy_edge")]
    t0 = time.time()
    rows_206, s = run_suite(s, cases_206, OUT_206, "206")
    rows_deploy, s = run_suite(s, cases_deploy, OUT_DEPLOY, "parse55")
    now = classify(rows_206, rows_deploy)
    base_rows = [json.loads(l) for l in open(BASELINE, encoding="utf-8")]
    summary = {"run_at": time.strftime("%Y-%m-%d %H:%M"), "elapsed_min": round((time.time() - t0) / 60, 1),
               "rate_limit_wait_s": sum(r.get("rate_limit_wait_s", 0) for r in now),
               "accounts_used": sorted({r.get("account") for r in now if r.get("account")}),
               "now": stats(now), "now_with_moderation": stats(now, "passed_with_moderation"),
               "baseline_20261007": stats(base_rows)}
    with open(OUT_SUMMARY, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    b, n = summary["baseline_20261007"], summary["now"]
    print(f"\n10/07 基準線：{b['passed']}/{b['judged']} = {b['rate'] * 100:.1f}%")
    print(f"現在        ：{n['passed']}/{n['judged']} = {n['rate'] * 100:.1f}%")
    m = summary["now_with_moderation"]
    print(f"現在（補充判定：誘導偵測擋下也算拒絕）：{m['passed']}/{m['judged']} = {m['rate'] * 100:.1f}%")
    for k, v in n["by_category"].items():
        bv = b["by_category"].get(k) or {}
        if v["judged"]:
            print(f"  {k:45s} {v['passed']:3d}/{v['judged']:3d}  （10/07：{bv.get('passed', '-')}/{bv.get('judged', '-')}）")


if __name__ == "__main__":
    main()
