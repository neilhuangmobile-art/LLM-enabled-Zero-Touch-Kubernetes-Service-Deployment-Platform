"""
qa_concurrent_local_20261026.py — 三人同時使用（本機模式），全系統測試 E30

沿用 qa_concurrent_users.py 的情境（問答 → 意圖 → 解析 → 部署 → 等就緒 → 隔離 → 擴縮 → 接地問答 → 刪除），
改成本機模式的測試站、3 個當場註冊的新帳號，並先量一個人單獨問答的延遲當基準。
密碼每次隨機產生，不寫進程式碼。測試完要自己刪掉 qa-c1～qa-c3 帳號與 namespace。

用法：python qa_concurrent_local_20261026.py http://127.0.0.1:5051
輸出：docs/qa_full_20261026_results.jsonl（E30-*）、docs/qa_concurrent_local_20261026.jsonl（逐步明細）
"""
import json
import os
import secrets
import sys
import threading
import time

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import qa_concurrent_users as cu  # noqa: E402
import qa_full_20261026 as qa  # noqa: E402

DETAIL = os.path.join(ROOT, "docs", "qa_concurrent_local_20261026.jsonl")
G30 = "E30 多人同時使用"
N = 3


def register(base, name, pw):
    r = requests.post(base + "/auth/register", data={"username": name, "password": pw, "confirm": pw}, timeout=60)
    return r.status_code


def main():
    base = sys.argv[1].rstrip("/")
    names = [f"qa-c{i + 1}" for i in range(N)]
    pw = secrets.token_urlsafe(12)
    for n in names:
        register(base, n, pw)
    users = [cu.User(base, n, pw, n.split("-")[1]) for n in names]
    for i, u in enumerate(users):
        u.port = 8191 + i

    for q in cu.CHAT_QS:
        cu.chat(users[0], q, "baseline")

    barrier = threading.Barrier(N)
    ts = [threading.Thread(target=cu.scenario, args=(u, users[(i + 1) % N].app, barrier)) for i, u in enumerate(users)]
    t0 = time.time()
    [t.start() for t in ts]
    [t.join() for t in ts]
    wall = time.time() - t0

    # 每個人都看不到其他所有人的服務（情境本身只檢查下一個人）
    rows = cu.results
    with open(DETAIL, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")

    by_step = {}
    for r in rows:
        if r["phase"] == "concurrent":
            by_step.setdefault(r["step"], []).append(r)
    for step, rs in by_step.items():
        ok = all(r["passed"] for r in rs)
        lat = [r["latency_ms"] for r in rs]
        qa.record(f"E30-{step}", G30, f"{N} 人同時：{step}", ok, f"{N} 人都通過", {r["user"]: r["passed"] for r in rs},
                  rs[0]["passed_rule"], sum(lat) / len(lat))
    base_chat = [r["latency_ms"] for r in rows if r["phase"] == "baseline"]
    conc_chat = [r["latency_ms"] for r in rows if r["phase"] == "concurrent" and r["step"] == "chat_qa"]
    qa.record("E30-latency", G30, f"問答延遲：1 人 vs {N} 人同時", True, "記錄排隊延遲",
              {"單人平均ms": round(sum(base_chat) / len(base_chat)), f"{N}人平均ms": round(sum(conc_chat) / len(conc_chat)),
               "最慢ms": round(max(conc_chat)), "整段秒數": round(wall, 1)},
              "量測值，不判定通過與否", sum(conc_chat) / len(conc_chat))


if __name__ == "__main__":
    main()
