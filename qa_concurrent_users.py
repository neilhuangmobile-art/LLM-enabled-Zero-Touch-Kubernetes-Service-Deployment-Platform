"""
qa_concurrent_users.py — 兩人同時使用的端到端 QA（客戶自帶叢集模式，全程走對外網址）

2026-10-09 新增（客戶自帶叢集第 6 階段）。兩個測試帳號各自連上自己的叢集 namespace，
同一時間開始各自跑完整流程：Chat 問答 → 意圖辨識 → 自然語言部署解析 → 部署 → 等同步 →
隔離檢查 → 擴縮 → 接地問答（問自己的服務） → 刪除。另外先跑「只有一個人」的 Chat 基準，
量化 Model Server 一次只處理一個請求（有鎖）在兩人同時使用時造成的排隊延遲。

每一題存成結構化欄位（AGENT_RULES：做系統測試要留量化數據）：
    user, step, category, input, output, passed, passed_rule, latency_ms, phase(baseline|concurrent)

用法：
    python qa_concurrent_users.py https://your.ts.net userA:passA userB:passB
輸出：docs/qa_concurrent_20261009.jsonl、docs/qa_concurrent_20261009_summary.json
"""
import json
import os
import sys
import threading
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core.config import ensure_utf8_output  # noqa: E402

ensure_utf8_output()

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "qa_concurrent_20261009.jsonl")
SUMMARY = OUT.replace(".jsonl", "_summary.json")
CHAT_QS = ["Deployment 跟 Pod 差在哪？請簡短回答", "什麼是 Kubernetes Service？請簡短回答",
           "要怎麼讓服務有更多副本？請簡短回答"]
_lock = threading.Lock()
results = []


def record(**row):
    row["t"] = time.strftime("%H:%M:%S")
    with _lock:
        results.append(row)
        print(f"[{row['phase']:10}] {row['user']:10} {row['step']:22} "
              f"{'PASS' if row['passed'] else 'FAIL'} {row['latency_ms']:>7.0f}ms  {str(row['output'])[:70]}")


def timed(fn):
    t = time.time()
    try:
        out = fn()
    except Exception as e:
        out = {"_error": f"{type(e).__name__}: {e}"}
    return out, (time.time() - t) * 1000


class User:
    def __init__(self, base, name, pw, tag):
        self.base, self.name, self.tag = base, name, tag
        self.s = requests.Session()
        r = self.s.post(base + "/auth/login", data={"username": name, "password": pw}, timeout=30)
        assert r.ok, f"login failed for {name}"
        self.app = f"qa-{tag}-web"

    def get(self, path):
        r = self.s.get(self.base + path, timeout=300)
        return r.json() if r.headers.get("content-type", "").startswith("application/json") else {"_status": r.status_code}

    def post(self, path, body):
        r = self.s.post(self.base + path, json=body, timeout=300)
        return r.json() if r.headers.get("content-type", "").startswith("application/json") else {"_status": r.status_code}

    def deployments(self):
        return {d["name"]: d for d in self.get("/api/deployments").get("deployments", [])}


def chat(u, q, phase):
    out, ms = timed(lambda: u.post("/api/chat", {"message": q, "history": []}))
    reply = str(out.get("reply") or out.get("response") or "")
    ok = bool(reply.strip()) and "_error" not in out
    record(user=u.name, step="chat_qa", category="chat", input=q, output=reply[:300], passed=ok,
           passed_rule="HTTP 成功且回覆非空", latency_ms=ms, phase=phase)


def wait_for(u, cond, step, rule, phase, limit=180):
    t = time.time()
    while time.time() - t < limit:
        d = u.deployments()
        if cond(d):
            record(user=u.name, step=step, category="sync", input=u.app, output=json.dumps(d.get(u.app), ensure_ascii=False),
                   passed=True, passed_rule=rule, latency_ms=(time.time() - t) * 1000, phase=phase)
            return True
        time.sleep(5)
    record(user=u.name, step=step, category="sync", input=u.app, output=json.dumps(u.deployments(), ensure_ascii=False)[:300],
           passed=False, passed_rule=rule + f"（{limit} 秒內）", latency_ms=limit * 1000, phase=phase)
    return False


def scenario(u, other_app, barrier):
    phase = "concurrent"
    barrier.wait()  # 兩人同時開始
    for q in CHAT_QS:
        chat(u, q, phase)

    q = f"幫我部署 2 個 nginx，名稱 {u.app}"
    out, ms = timed(lambda: u.post("/api/intent", {"message": q}))
    act = (out.get("action") or (out.get("result") or {}).get("action") or "")
    record(user=u.name, step="intent", category="intent", input=q, output=act, passed=act == "deploy",
           passed_rule="action == deploy", latency_ms=ms, phase=phase)

    q = f"部署 2 個 nginx:1.27，名稱 {u.app}，port 80"
    out, ms = timed(lambda: u.post("/api/deploy/parse", {"input": q}))
    p = out.get("parsed") or {}
    # 2026-10-09 第一次跑時規則只檢查「有沒有 nginx」，把 nginx:1.27 被解析成 nginx:latest 的錯誤
    # 算成通過——映像檔要完全一致，使用者指定的版本號不能被換掉
    ok = p.get("app_name") == u.app and int(p.get("pods") or 0) == 2 and p.get("image") == "nginx:1.27"
    record(user=u.name, step="deploy_parse", category="deploy", input=q,
           output={k: p.get(k) for k in ("app_name", "pods", "image", "port")}, passed=ok,
           passed_rule="app_name、pods 一致，且 image 完全等於 nginx:1.27", latency_ms=ms, phase=phase)

    spec = {"app_name": u.app, "image": "nginx:1.27", "pods": 2, "port": 80}
    out, ms = timed(lambda: u.post("/api/deploy", {"parsed": spec}))
    ok = (out.get("k8s_deploy") or {}).get("ok") is True
    record(user=u.name, step="deploy_commit", category="deploy", input=spec,
           output=(out.get("gitops") or {}).get("message") or out.get("error"), passed=ok,
           passed_rule="k8s_deploy.ok == True（已 commit 到客戶 repo）", latency_ms=ms, phase=phase)
    wait_for(u, lambda d: d.get(u.app, {}).get("ready") == 2, "deploy_synced", "叢集上 2/2 ready", phase)

    d = u.deployments()
    ok = other_app not in d
    record(user=u.name, step="isolation", category="isolation", input=f"不應看到 {other_app}",
           output=sorted(d), passed=ok, passed_rule="Deployments 清單不含對方的服務", latency_ms=0, phase=phase)

    out, ms = timed(lambda: u.post("/api/scale", {"name": u.app, "replicas": 3}))
    record(user=u.name, step="scale_commit", category="ops", input="replicas=3", output=out.get("message") or out.get("error"),
           passed=bool(out.get("success")), passed_rule="success == True", latency_ms=ms, phase=phase)
    wait_for(u, lambda d: d.get(u.app, {}).get("ready") == 3, "scale_synced", "叢集上 3/3 ready", phase)

    q = f"{u.app} 現在狀態如何？"
    out, ms = timed(lambda: u.post("/api/chat", {"message": q, "history": []}))
    reply = str(out.get("reply") or "")
    ok = bool(reply) and other_app not in reply
    record(user=u.name, step="chat_grounded", category="isolation", input=q, output=reply[:300], passed=ok,
           passed_rule="有回覆且沒有提到對方的服務", latency_ms=ms, phase=phase)

    out, ms = timed(lambda: u.post("/api/delete", {"name": u.app}))
    record(user=u.name, step="delete_commit", category="ops", input=u.app, output=out.get("message") or out.get("error"),
           passed=bool(out.get("success")), passed_rule="success == True", latency_ms=ms, phase=phase)
    wait_for(u, lambda d: u.app not in d, "delete_synced", "服務已從叢集移除", phase)


def main():
    base = sys.argv[1].rstrip("/")
    (a_name, a_pw), (b_name, b_pw) = [x.split(":", 1) for x in sys.argv[2:4]]
    a, b = User(base, a_name, a_pw, "a"), User(base, b_name, b_pw, "b")

    print("== 基準：只有一個人使用 ==")
    for q in CHAT_QS:
        chat(a, q, "baseline")

    print("== 兩人同時使用 ==")
    barrier = threading.Barrier(2)
    ts = [threading.Thread(target=scenario, args=(a, b.app, barrier)),
          threading.Thread(target=scenario, args=(b, a.app, barrier))]
    t0 = time.time()
    [t.start() for t in ts]
    [t.join() for t in ts]
    wall = time.time() - t0

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
    passed = sum(r["passed"] for r in results)
    base_chat = [r["latency_ms"] for r in results if r["phase"] == "baseline"]
    conc_chat = [r["latency_ms"] for r in results if r["phase"] == "concurrent" and r["step"] == "chat_qa"]
    summary = {"total": len(results), "passed": passed, "pass_rate": round(passed / len(results), 3),
               "concurrent_wall_seconds": round(wall, 1),
               "chat_latency_ms": {"baseline_avg": round(sum(base_chat) / len(base_chat)),
                                   "concurrent_avg": round(sum(conc_chat) / len(conc_chat))},
               "by_category": {}}
    for r in results:
        c = summary["by_category"].setdefault(r["category"], {"total": 0, "passed": 0})
        c["total"] += 1
        c["passed"] += int(r["passed"])
    with open(SUMMARY, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)
    print(json.dumps(summary, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
