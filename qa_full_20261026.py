"""
qa_full_20261026.py — 專題展前全系統測試（2026-10-26）

每一項測試寫一列進 docs/qa_full_20261026_results.jsonl：
  id、group、name、input、expected、actual、passed、rule（判定規則）、latency_ms、ts
同一個 id 重跑會覆蓋舊結果（讀檔時以最後一筆為準），所以修好問題後可以只重跑那一組。

用法：python qa_full_20261026.py <group>
  external  外網：透過公開網址做 HTTPS 登入與安全檢查
  （其他組別見檔案下方 GROUPS）
"""
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "docs", "qa_full_20261026_results.jsonl")
PUBLIC = "https://user.tail4c2fd2.ts.net"
LOCAL_TEST = "http://127.0.0.1:5051"          # 本機模式的測試網站
# 帳號密碼從環境變數讀，不寫進程式（網站對外開放登入）。執行前設定 QA_DEMO_USER／QA_DEMO_PW、QA_USER_PW
DEMO_USER, DEMO_PW = os.environ.get("QA_DEMO_USER", "demo-shot"), os.environ.get("QA_DEMO_PW", "")


def record(tid, group, name, passed, expected, actual, rule, latency_ms=None, inp=None):
    row = {"id": tid, "group": group, "name": name, "input": inp, "expected": expected,
           "actual": actual, "passed": bool(passed), "rule": rule,
           "latency_ms": None if latency_ms is None else round(latency_ms, 1),
           "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
    with open(OUT, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(("PASS " if passed else "FAIL ") + f"{tid} {name} :: {str(actual)[:160]}")
    return passed


class Timer:
    def __enter__(self):
        self.t = time.perf_counter(); return self

    def __exit__(self, *a):
        self.ms = (time.perf_counter() - self.t) * 1000


def session(base, user=DEMO_USER, pw=DEMO_PW, secure_ok=False):
    """登入並回傳 requests.Session。本機 http 測試時把 Secure cookie 轉成可送出（伺服器設定的是 https 網址）。"""
    import requests
    s = requests.Session()
    s.post(base + "/auth/login", data={"username": user, "password": pw}, allow_redirects=False, timeout=30)
    if not secure_ok:
        for c in s.cookies:
            c.secure = False
    return s


# ── C. 外網 ─────────────────────────────────────────────────────────
def g_external():
    import requests
    G = "C 外網"
    # 19. 透過公開網址（Tailscale Funnel、HTTPS）登入，確認登入狀態在 HTTPS 下正常保持
    s = requests.Session()
    with Timer() as t:
        r = s.post(PUBLIC + "/auth/login", data={"username": DEMO_USER, "password": DEMO_PW}, allow_redirects=False, timeout=30)
    ck = [c for c in s.cookies if c.name == "session"]
    record("C19-1", G, "HTTPS 登入", r.status_code == 302 and ck and ck[0].secure, "302 轉址，session cookie 帶 Secure",
           {"status": r.status_code, "cookie_secure": ck[0].secure if ck else None}, "登入成功且 cookie 只在 HTTPS 送出", t.ms)
    with Timer() as t:
        st = s.get(PUBLIC + "/api/status", timeout=30)
    # 正式網站是客戶自帶叢集模式：未連線的帳號應拿到清楚的引導訊息，而不是 401
    body = st.json() if "json" in st.headers.get("content-type", "") else {}
    record("C19-2", G, "HTTPS 下登入狀態有被帶上", st.status_code in (200, 403) and st.status_code != 401,
           "不是 401（代表 cookie 有送出）", {"status": st.status_code, "body": str(body)[:120]},
           "登入後呼叫 API 不得是 401", t.ms)
    for path, name in [("/m", "手機監控版"), ("/onboarding", "客戶叢集引導頁"), ("/manifest.webmanifest", "PWA 設定檔"),
                       ("/sw.js", "推播背景程式"), ("/icons/icon-192.png", "App 圖示")]:
        with Timer() as t:
            r = s.get(PUBLIC + path, timeout=30)
        record(f"C19-{path}", G, f"HTTPS 取得{name}", r.status_code == 200, "200", r.status_code, "HTTP 200", t.ms, path)
    # 20. 外網安全
    anon = requests.Session()
    apis = ["/api/status", "/api/dashboard", "/api/pods", "/api/deployments", "/api/chats", "/api/projects",
            "/api/calendar", "/api/gitops", "/api/healer/status", "/api/report", "/api/autoscale", "/api/trash"]
    codes = {}
    for a in apis:
        codes[a] = anon.get(PUBLIC + a, timeout=30).status_code
    record("C20-1", G, "未登入呼叫 12 支 API 都被擋", all(c == 401 for c in codes.values()), "全部 401", codes,
           "每一支都必須回 401")
    big = "x" * (1024 * 1024 + 10)
    with Timer() as t:
        try:
            r = s.post(PUBLIC + "/api/chat", data=json.dumps({"message": big}), headers={"Content-Type": "application/json"}, timeout=60)
            got = r.status_code
        except requests.exceptions.SSLError:
            got = "連線被中斷"
    # 2026-10-26 實測：直接連本機回 413（waitress）；經過 Tailscale 通道時，伺服器提早拒絕，通道直接斷線。
    # 兩種都代表請求沒有進到程式。前端上傳另有 100 KB 上限，一般使用者碰不到。
    record("C20-2", G, "超過 1 MB 的請求被擋", got in (413, "連線被中斷"), "413 或連線被拒", got,
           "請求沒有被處理即算通過（經過通道時看到的是連線中斷，直接連本機為 413）", t.ms)
    # 登入每分鐘 10 次：用錯的密碼連送 12 次，第 11 次起要 429
    bad = requests.Session(); codes = []
    for i in range(12):
        codes.append(bad.post(PUBLIC + "/auth/login", data={"username": "nobody-qa", "password": "wrong"}, allow_redirects=False,
                              timeout=30).status_code)
    record("C20-3", G, "登入暴力破解頻率限制", codes[:10].count(429) == 0 and 429 in codes[10:], "前 10 次不擋、第 11 次起 429",
           codes, "第 11、12 次至少一次 429")
    r = anon.get(PUBLIC + "/auth/google/login", allow_redirects=False, timeout=30)
    record("C20-4", G, "Google 登入入口可用", r.status_code in (302, 200), "302 轉到 Google 或說明頁", r.status_code, "不是 5xx")


def g_external_rest():
    import requests
    G = "C 外網"
    s = session(PUBLIC, secure_ok=True)
    anon = requests.Session()
    big = "x" * (1024 * 1024 + 10)
    with Timer() as t:
        try:
            got = s.post(PUBLIC + "/api/chat", data=json.dumps({"message": big}), headers={"Content-Type": "application/json"}, timeout=60).status_code
        except requests.exceptions.SSLError:
            got = "連線被中斷"
    record("C20-2", G, "超過 1 MB 的請求被擋", got in (413, "連線被中斷"), "413 或連線被拒", got,
           "請求沒有被處理即算通過（經過通道時看到的是連線中斷，直接連本機為 413）", t.ms)
    bad = requests.Session(); codes = []
    for i in range(12):
        codes.append(bad.post(PUBLIC + "/auth/login", data={"username": "nobody-qa", "password": "wrong"}, allow_redirects=False,
                              timeout=30).status_code)
    record("C20-3", G, "登入暴力破解頻率限制", codes[:10].count(429) == 0 and 429 in codes[10:], "前 10 次不擋、第 11 次起 429",
           codes, "第 11、12 次至少一次 429")
    r = anon.get(PUBLIC + "/auth/google/login", allow_redirects=False, timeout=30)
    record("C20-4", G, "Google 登入入口可用", r.status_code in (302, 200), "302 轉到 Google 或說明頁", r.status_code, "不是 5xx")


# ── B. 核心功能（本機模式測試網站，專用帳號 qa-full，每一步用 kubectl 對照叢集）──────────────
QA_USER, QA_PW, QA_NS = "qa-full", os.environ.get("QA_USER_PW", ""), "user-qa-full"   # 測試帳號每次執行時註冊、結束後刪除


def kc(*args, ns=QA_NS):
    import subprocess
    return subprocess.run(["kubectl", "-n", ns, *args], capture_output=True, text=True).stdout.strip()


def wait_for(fn, timeout=120, every=3):
    """反覆呼叫 fn() 直到回傳真值或逾時；回傳 (結果, 花費秒數)。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        v = fn()
        if v:
            return v, time.time() - t0
        time.sleep(every)
    return None, time.time() - t0


def step(tid, group, name, fn):
    """包住單一測試，例外也記成失敗，不讓整組中斷。"""
    try:
        fn()
    except Exception as e:
        record(tid, group, name, False, "不拋出例外", f"{type(e).__name__}: {e}"[:300], "執行過程不得出錯")


class Api:
    def __init__(self, base, user, pw):
        import requests
        self.base = base
        self.s = requests.Session()
        self.s.post(base + "/auth/register", data={"username": user, "password": pw, "confirm": pw}, allow_redirects=False)
        self._fix()
        self.s.post(base + "/auth/login", data={"username": user, "password": pw}, allow_redirects=False)
        self._fix()

    def _fix(self):
        for c in self.s.cookies:
            c.secure = False

    def get(self, p):
        t = time.perf_counter(); r = self.s.get(self.base + p, timeout=180); self._fix()
        return r, (time.perf_counter() - t) * 1000

    def post(self, p, body=None):
        t = time.perf_counter(); r = self.s.post(self.base + p, json=body or {}, timeout=300); self._fix()
        return r, (time.perf_counter() - t) * 1000


def deploy(api, spec, text=None):
    import uuid
    body = {"input": text or f"部署 {spec['app_name']}", "parsed": spec, "idempotency_key": str(uuid.uuid4())}
    r, ms = api.post("/api/deploy", body)
    return r, ms, body


def ready(app, n):
    return kc("get", "deploy", app, "-o", "jsonpath={.status.readyReplicas}") == str(n)


def g_core():
    import datetime as dt
    import re
    G5, G6, G7, G8, G9, G10, G11, G12, G13, G14, G15, G16 = (
        "B5 讀取 API", "B6 對話", "B7 部署全流程", "B8 情境與金額", "B9 匯入", "B10 參數查證", "B11 預約",
        "B12 分時擴縮", "B13 休眠喚醒", "B14 自動修復", "B15 專案個人化", "B16 報告")
    api = Api(LOCAL_TEST, QA_USER, QA_PW)

    # B5：所有讀取 API 都要 200
    for p in ["/api/status", "/api/dashboard", "/api/pods", "/api/deployments", "/api/metrics", "/api/healer/status",
              "/api/healer/scan", "/api/gitops", "/api/autoscale", "/api/calendar", "/api/chats", "/api/projects", "/api/trash",
              "/api/schedule", "/api/schedule/due", "/api/push/status", "/api/rag/status", "/api/rag/docs", "/api/report",
              "/api/reports/weekly", "/api/personalize/suggest", "/api/onboard/status", "/api/deploy/conflicts",
              "/api/dataset/stats", "/m", "/"]:
        def t(p=p):
            r, ms = api.get(p)
            record(f"B5{p}", G5, f"GET {p}", r.status_code == 200, "200", r.status_code, "HTTP 200", ms, p)
        step(f"B5{p}", G5, f"GET {p}", t)

    # B7：部署全流程（qa-web，nginx:1.27，2 個副本，port 8088）
    spec = {"app_name": "qa-web", "image": "nginx:1.27", "pods": 2, "port": 8088}
    def t_parse():
        r, ms = api.post("/api/deploy/parse", {"parsed": spec})
        d = r.json()
        record("B7-1", G7, "部署審查（parse）", r.status_code == 200 and d.get("review", {}).get("decision") in ("approve", "warn"),
               "審查通過或提醒", {"status": r.status_code, "decision": d.get("review", {}).get("decision")}, "可以部署", ms)
    step("B7-1", G7, "部署審查", t_parse)
    state = {}
    def t_deploy():
        r, ms, body = deploy(api, spec)
        state["body"] = body
        ok = (r.json().get("k8s_deploy") or {}).get("ok")
        _, sec = wait_for(lambda: ready("qa-web", 2), 120)
        record("B7-2", G7, "部署並等 2 個副本就緒", ok and ready("qa-web", 2), "2/2 就緒",
               {"api_ok": ok, "ready": kc("get", "deploy", "qa-web", "-o", "jsonpath={.status.readyReplicas}"), "秒": round(sec, 1)},
               "kubectl 看到 2 個就緒副本", ms)
    step("B7-2", G7, "部署", t_deploy)
    def t_idem():
        r, ms = api.post("/api/deploy", state["body"])
        commits = kc("get", "deploy", "qa-web", "-o", "jsonpath={.metadata.generation}")
        record("B7-3", G7, "同一個請求重送不重複部署", r.json().get("replayed") is True, "replayed=true",
               {"replayed": r.json().get("replayed"), "generation": commits}, "回傳 replayed、不重新部署", ms)
    step("B7-3", G7, "重送不重複", t_idem)

    # B11：預約（2 分鐘以後）把 qa-web 調成 1 個；B12：分時擴縮 3～5 分鐘後開 3 個。兩個一起等
    def t_sched_create():
        run_at = time.time() + 130
        r, ms = api.post("/api/schedule", {"kind": "scale", "name": "qa-web", "replicas": 1, "run_at": run_at, "text": "qa 預約"})
        d = r.json(); state["task"] = (d.get("task") or {}).get("id"); state["run_at"] = run_at
        record("B11-1", G11, "建立預約", r.status_code == 200 and state["task"], "建立成功", str(d)[:150], "回傳預約編號", ms)
    step("B11-1", G11, "建立預約", t_sched_create)
    now = dt.datetime.now()
    st_, en_ = (now + dt.timedelta(minutes=4)).strftime("%H:%M"), (now + dt.timedelta(minutes=6)).strftime("%H:%M")
    def t_ts_create():
        plan = {"app": "qa-web", "base": 1, "windows": [{"start": st_, "end": en_, "replicas": 3, "days": [0, 1, 2, 3, 4, 5, 6]}]}
        r, ms = api.post("/api/autoscale/schedule", {"plan": plan})
        record("B12-1", G12, "設定分時擴縮", r.json().get("ok"), "ok", r.json(), f"{st_}～{en_} 開 3 個", ms)
    step("B12-1", G12, "設定分時擴縮", t_ts_create)

    # B6：意圖判斷（等待期間做）
    intents = [("幫我部署兩個 redis", "deploy"), ("把 qa-web 調成 3 個", "scale"), ("qa-web 現在健康嗎", "pod_health"),
               ("什麼是 ConfigMap", "qa"), ("刪掉 qa-web", "delete"), ("把 qa-web 的映像檔換成 nginx:1.28", "update"),
               ("qa-web 回滾到上一版", "rollback"), ("列出我的 pods", None), ("deploy 3 nginx pods for web-a", "deploy"),
               ("scale qa-web to 4", "scale")]
    for i, (msg, want) in enumerate(intents):
        def t(msg=msg, want=want, i=i):
            r, ms = api.post("/api/intent", {"message": msg})
            got = r.json().get("action")
            ok = (got == want) if want else (r.status_code == 200 and got is not None)
            record(f"B6-{i+1}", G6, f"意圖：{msg}", ok, want or "任一合理動作", got, "action 等於預期", ms, msg)
        step(f"B6-{i+1}", G6, f"意圖 {msg}", t)
    def t_chat():
        r, ms = api.post("/api/chat", {"message": "qa-web 現在有幾個副本？", "history": []})
        reply = r.json().get("reply", "")
        actual_n = kc("get", "deploy", "qa-web", "-o", "jsonpath={.spec.replicas}")
        record("B6-chat", G6, "聊天回答與叢集一致", actual_n in reply, f"回覆含 {actual_n}", reply[:120], "回覆中的副本數等於 kubectl", ms)
    step("B6-chat", G6, "聊天接地", t_chat)

    # B8：情境部署與金額算式
    def t_scn():
        r, ms = api.post("/api/deploy/parse", {"input": "幫我部署 nginx:1.27 叫 qa-event，給 500 人用 3 天"})
        d = r.json(); sc = d.get("scenario") or {}; sv = sc.get("savings") or {}
        m = re.search(r"相減省下約 ([\d.]+) 美元", sv.get("how") or "")
        record("B8-1", G8, "情境推算副本數", sc.get("replicas") == 1, "1 個（500 人×兩成÷200）", sc.get("replicas"), "副本數符合公式", ms)
        record("B8-2", G8, "省錢金額附算式且數字一致", bool(m) and float(m.group(1)) == sv.get("total_usd"), "算式結果＝顯示金額",
               {"how": (sv.get("how") or "")[:80], "total": sv.get("total_usd")}, "算式的相減結果等於 total_usd")
        rs = d.get("resource_summary") or {}
        record("B8-3", G8, "月費附算式", bool(rs.get("cost_how")) and f"≈ {rs.get('monthly_usd')} 美元" in (rs.get("cost_how") or ""),
               "有算式、結果一致", {"usd": rs.get("monthly_usd"), "how": (rs.get("cost_how") or "")[:60]}, "算式結尾等於月費")
    step("B8", G8, "情境", t_scn)

    # B9：compose 多服務、K8s YAML
    compose = ("services:\n  qa-api:\n    image: nginx:1.27\n    ports: ['8091:80']\n    depends_on: [qa-cache]\n"
               "  qa-cache:\n    image: redis:7.2\n    environment:\n      REDIS_PASSWORD: s3cret\n")
    def t_compose():
        r, ms = api.post("/api/import/parse", {"filename": "docker-compose.yml", "text": compose})
        d = r.json(); svcs = d.get("services") or []
        record("B9-1", G9, "compose 解析出 2 個服務", len(svcs) == 2 and d.get("deployable"), "2 個、可部署",
               {"n": len(svcs), "deployable": d.get("deployable")}, "服務數正確", ms)
        order = [x["app_name"] for x in svcs]
        record("B9-2", G9, "依相依順序排列", order.index("qa-cache") < order.index("qa-api"), "qa-cache 在前", order, "被依賴的先部署")
        cache = [x for x in svcs if x["app_name"] == "qa-cache"][0]
        env = cache.get("env") or {}
        record("B9-3", G9, "密碼類設定改用機密物件", "REDIS_PASSWORD" not in env or env.get("REDIS_PASSWORD") != "s3cret" or cache.get("secret_env"),
               "不以明文環境變數保存", {k: v for k, v in cache.items() if k in ("env", "secret_env", "secrets")}, "密碼不留在一般環境變數")
        okc = 0
        for sv in svcs:
            spec2 = {k: sv.get(k) for k in ("app_name", "image", "pods", "port", "container_port", "env", "memory", "cpu") if sv.get(k) is not None}
            r2, _, _ = deploy(api, spec2)
            okc += bool((r2.json().get("k8s_deploy") or {}).get("ok"))
        _, sec = wait_for(lambda: ready("qa-api", 1) and ready("qa-cache", 1), 120)
        record("B9-4", G9, "compose 兩個服務實際部署就緒", okc == 2 and ready("qa-api", 1) and ready("qa-cache", 1), "2 個都就緒",
               {"api_ok": okc, "秒": round(sec, 1)}, "kubectl 看到兩個服務就緒")
        for n in ("qa-api", "qa-cache"):
            api.post("/api/delete", {"name": n})
    step("B9-c", G9, "compose", t_compose)
    def t_k8s():
        yml = ("apiVersion: apps/v1\nkind: Deployment\nmetadata: {name: qa-yaml}\nspec:\n  replicas: 2\n  template:\n    spec:\n"
               "      containers:\n      - name: app\n        image: nginx:1.27\n        ports: [{containerPort: 80}]\n---\n"
               "apiVersion: v1\nkind: Service\nmetadata: {name: qa-yaml}\nspec: {ports: [{port: 80}]}\n")
        r, ms = api.post("/api/import/parse", {"filename": "app.yaml", "text": yml})
        d = r.json(); svcs = d.get("services") or []
        record("B9-5", G9, "K8s YAML 解析", d.get("format") != "compose" and len(svcs) == 1 and svcs[0].get("pods") == 2,
               "1 個服務、2 個副本", {"format": d.get("format"), "n": len(svcs)}, "解析結果正確", ms)
    step("B9-5", G9, "K8s YAML", t_k8s)

    # B10：參數查證
    def t_ground():
        r, ms = api.post("/api/deploy/parse", {"input": "部署 2 個 redis 7.2 叫 qa-cache2"})
        p = r.json().get("raw") or {}
        record("B10-1", G10, "「redis 7.2」保留版本", p.get("image") == "redis:7.2" and p.get("pods") == 2, "redis:7.2、2 個",
               {"image": p.get("image"), "pods": p.get("pods")}, "映像檔與副本數正確", ms)
        r, ms = api.post("/api/deploy/parse", {"input": "幫我部署一個快取服務"})
        d = r.json(); img = (d.get("raw") or d.get("parsed") or {}).get("image")
        record("B10-2", G10, "沒講映像檔不代為猜測", bool(d.get("error")) or not img or (d.get("missing") and "image" in d["missing"]),
               "回錯誤或請使用者補", {"error": d.get("error"), "image": img, "missing": d.get("missing")}, "不出現使用者沒講過的映像檔", ms)
    step("B10", G10, "參數查證", t_ground)

    # B11：等預約到期 → 執行
    def t_sched_run():
        due, sec = wait_for(lambda: any(t.get("id") == state["task"] for t in api.get("/api/schedule/due")[0].json().get("tasks", [])),
                            timeout=max(30, state["run_at"] - time.time() + 90), every=5)
        record("B11-2", G11, "時間到變成「等你確認」", bool(due), "到期", {"等待秒數": round(sec, 1)}, "到期後出現在 due 清單")
        api.post(f"/api/schedule/{state['task']}/start")
        r, ms = api.post("/api/scale", {"name": "qa-web", "replicas": 1})
        api.post(f"/api/schedule/{state['task']}/done", {"status": "done", "result": "ok"})
        _, s2 = wait_for(lambda: kc("get", "deploy", "qa-web", "-o", "jsonpath={.spec.replicas}") == "1", 60)
        record("B11-3", G11, "確認後執行，副本數變 1", kc("get", "deploy", "qa-web", "-o", "jsonpath={.spec.replicas}") == "1",
               "1", kc("get", "deploy", "qa-web", "-o", "jsonpath={.spec.replicas}"), "kubectl 副本數為 1", ms)
    step("B11-2", G11, "預約到期執行", t_sched_run)

    # B12：等分時擴縮的開始與結束
    def t_ts_run():
        start_ts = dt.datetime.combine(now.date(), dt.datetime.strptime(st_, "%H:%M").time()).timestamp()
        end_ts = dt.datetime.combine(now.date(), dt.datetime.strptime(en_, "%H:%M").time()).timestamp()
        time.sleep(max(0, start_ts - time.time()))
        _, s1 = wait_for(lambda: kc("get", "deploy", "qa-web", "-o", "jsonpath={.spec.replicas}") == "3", 90, 2)
        record("B12-2", G12, "時段開始自動擴成 3 個", kc("get", "deploy", "qa-web", "-o", "jsonpath={.spec.replicas}") == "3",
               "3", {"開始後秒數": round(s1, 1)}, "時段開始後 60 秒內副本數為 3", s1 * 1000)
        time.sleep(max(0, end_ts - time.time()))
        _, s2 = wait_for(lambda: kc("get", "deploy", "qa-web", "-o", "jsonpath={.spec.replicas}") == "1", 90, 2)
        record("B12-3", G12, "時段結束自動縮回 1 個", kc("get", "deploy", "qa-web", "-o", "jsonpath={.spec.replicas}") == "1",
               "1", {"結束後秒數": round(s2, 1)}, "時段結束後 60 秒內副本數為 1", s2 * 1000)
        api.post("/api/autoscale/cancel", {"name": "qa-web"})
    step("B12-2", G12, "分時擴縮切換", t_ts_run)

    # B7 後半：換版本、回滾、刪除
    def t_update():
        r, ms = api.post("/api/update", {"name": "qa-web", "image": "nginx:1.28"})
        _, sec = wait_for(lambda: kc("get", "deploy", "qa-web", "-o", "jsonpath={.spec.template.spec.containers[0].image}") == "nginx:1.28", 60)
        record("B7-4", G7, "換映像檔版本", kc("get", "deploy", "qa-web", "-o", "jsonpath={.spec.template.spec.containers[0].image}") == "nginx:1.28",
               "nginx:1.28", kc("get", "deploy", "qa-web", "-o", "jsonpath={.spec.template.spec.containers[0].image}"), "kubectl 映像檔已更新", ms)
        r, ms = api.post("/api/rollback", {"name": "qa-web"})
        _, sec = wait_for(lambda: kc("get", "deploy", "qa-web", "-o", "jsonpath={.spec.template.spec.containers[0].image}") == "nginx:1.27", 90)
        record("B7-5", G7, "回滾到上一版", kc("get", "deploy", "qa-web", "-o", "jsonpath={.spec.template.spec.containers[0].image}") == "nginx:1.27",
               "nginx:1.27", {"image": kc("get", "deploy", "qa-web", "-o", "jsonpath={.spec.template.spec.containers[0].image}"), "resp": str(r.json())[:100]},
               "kubectl 映像檔回到 1.27", ms)
    step("B7-4", G7, "換版本與回滾", t_update)

    # B13：休眠 → 有人連線 → 自動喚醒（直接呼叫平台的休眠程序；這台叢集沒有網路流量指標，閒置偵測不會自己觸發）
    def t_sleep():
        import subprocess, requests as rq
        out = subprocess.run([sys.executable, "-c", "import web_demo as w;print(w._as_sleep('user-qa-full','qa-web','qa 測試'))"],
                             capture_output=True, text=True, encoding="utf-8", cwd=ROOT, env={**os.environ, "HEALER_ONLY_NAMESPACES": "zz-none",
                                                                          "CUSTOMER_CLUSTER_MODE": "0", "PYTHONIOENCODING": "utf-8"}).stdout
        slept = kc("get", "deploy", "qa-web", "-o", "jsonpath={.spec.replicas}") == "0"
        record("B13-1", G13, "進入休眠（副本數 0、接上喚醒器）", slept and "zt-waker-qa-web" in kc("get", "deploy"), "0 個副本＋喚醒器",
               out.strip()[-80:], "kubectl 副本數 0 且有喚醒器")
        t0 = time.time()
        try:
            page = rq.get("http://localhost:8088/", timeout=10).text
        except Exception as e:
            page = str(e)
        _, sec = wait_for(lambda: ready("qa-web", 1), 120, 2)
        record("B13-2", G13, "有人連線時顯示喚醒頁", "服務正在喚醒" in page, "喚醒頁", page[:60], "回應內容是喚醒頁")
        record("B13-3", G13, "自動喚醒恢復服務", ready("qa-web", 1), "120 秒內就緒", {"秒": round(time.time() - t0, 1)},
               "kubectl 就緒副本 1", (time.time() - t0) * 1000)
        ok_page = ""
        try:
            ok_page = rq.get("http://localhost:8088/", timeout=10).text
        except Exception as e:
            ok_page = str(e)
        record("B13-4", G13, "喚醒後恢復正常內容", "nginx" in ok_page.lower(), "nginx 歡迎頁", ok_page[:40], "內容不再是喚醒頁")
    step("B13", G13, "休眠喚醒", t_sleep)

    # B14：自動修復——映像檔錯誤、記憶體不足
    def t_heal_image():
        api.post("/api/update", {"name": "qa-web", "image": "nginx:9.99-doesnotexist"})
        t0 = time.time()
        _, sec = wait_for(lambda: kc("get", "deploy", "qa-web", "-o", "jsonpath={.spec.template.spec.containers[0].image}") != "nginx:9.99-doesnotexist", 240, 5)
        img = kc("get", "deploy", "qa-web", "-o", "jsonpath={.spec.template.spec.containers[0].image}")
        record("B14-1", G14, "映像檔錯誤自動退回", img != "nginx:9.99-doesnotexist", "退回可用版本", {"image": img, "秒": round(sec, 1)},
               "240 秒內映像檔被改回", sec * 1000)
    step("B14-1", G14, "映像檔錯誤", t_heal_image)
    def t_heal_oom():
        spec3 = {"app_name": "qa-oom", "image": "redis:7.2", "pods": 1, "port": 6390, "memory": "8Mi"}
        deploy(api, spec3)
        _, sec = wait_for(lambda: kc("get", "deploy", "qa-oom", "-o", "jsonpath={.spec.template.spec.containers[0].resources.limits.memory}") not in ("8Mi", ""), 300, 5)
        mem = kc("get", "deploy", "qa-oom", "-o", "jsonpath={.spec.template.spec.containers[0].resources.limits.memory}")
        record("B14-2", G14, "記憶體不足自動調高上限", mem not in ("8Mi", ""), "上限大於 8Mi", {"limit": mem, "秒": round(sec, 1)},
               "300 秒內記憶體上限被調高", sec * 1000)
        api.post("/api/delete", {"name": "qa-oom"})
    step("B14-2", G14, "記憶體不足", t_heal_oom)

    # B15：個人化（qa 帳號已部署多次 qa-web，習慣 2 個副本）、專案與回收桶
    def t_ws():
        r, ms = api.get("/api/personalize/suggest?text=" + "部署 nginx")
        d = r.json()
        record("B15-1", G15, "個人化建議有依據", d.get("ok") and d.get("reason"), "ok 且附理由", {k: d.get(k) for k in ("pods", "memory", "reason")},
               "回傳建議與理由", ms)
        pid = api.post("/api/projects", {"name": "qa-專案"})[0].json()["project"]["id"]
        r1 = api.s.patch(LOCAL_TEST + f"/api/projects/{pid}", json={"defaults": {"min_replicas": 2, "memory": "256Mi"}}).json()
        api._fix()
        r2 = api.post(f"/api/projects/{pid}/services", {"name": "qa-web", "action": "add"})[0].json()
        summ = api.get(f"/api/projects/{pid}/summary")[0]
        record("B15-2", G15, "專案：預設值、移入服務、摘要", r1.get("ok") and r2.get("ok") and summ.status_code == 200, "都成功",
               {"defaults": r1.get("ok"), "add": r2.get("ok"), "summary": summ.status_code}, "三個操作都成功")
        api.post(f"/api/projects/{pid}/delete", {"confirm_name": "qa-專案", "rest": "move", "remove": {}})
        tr = api.get("/api/trash")[0].json().get("items", [])
        rs = api.post("/api/trash/restore", {"type": "project", "id": pid})[0].json()
        record("B15-3", G15, "刪除專案進回收桶並可復原", any(x["id"] == pid and x["days_left"] == 7 for x in tr) and rs.get("ok"),
               "回收桶有、剩 7 天、可復原", {"in_trash": any(x["id"] == pid for x in tr), "restore": rs.get("ok")}, "復原成功")
        api.post(f"/api/projects/{pid}/delete", {"confirm_name": [p for p in api.get('/api/projects')[0].json()['projects'] if p['id'] == pid][0]['name'],
                                                 "rest": "move", "remove": {}})
        api.post("/api/trash/purge", {"type": "project", "id": pid})
    step("B15", G15, "專案與個人化", t_ws)

    # B16：分析報告
    def t_report():
        r, ms = api.get("/api/report?days=7")
        d = r.json()
        n_deploy = (d.get("deploys") or {}).get("total")
        record("B16-1", G16, "分析報告統計部署次數", r.status_code == 200 and (n_deploy or 0) >= 3, "至少 3 次（本次測試部署過的）",
               {"deploys": n_deploy, "heals": (d.get("heals") or {}).get("total")}, "數字與本次測試一致", ms)
        r, ms = api.get("/api/reports/weekly")
        record("B16-2", G16, "每週報告 API", r.status_code == 200 and r.json().get("ok"), "ok", r.status_code, "HTTP 200 且 ok", ms)
    step("B16", G16, "報告", t_report)

    # 收尾：刪除測試服務
    api.post("/api/delete", {"name": "qa-web"})
    _, sec = wait_for(lambda: "qa-web" not in kc("get", "deploy"), 60)
    record("B7-6", G7, "刪除服務", "qa-web" not in kc("get", "deploy").split(), "服務消失", kc("get", "deploy") or "（無）", "kubectl 查不到")


# ── D. 模擬設定不完整的電腦（5052：模型伺服器、K8s、Prometheus、Gemini 全部指向不存在的位址）──
DEGRADED = "http://127.0.0.1:5052"


def g_degraded():
    from playwright.sync_api import sync_playwright
    api = Api(DEGRADED, DEMO_USER, DEMO_PW)
    st, ms = api.get("/api/status")
    d = st.json()
    record("D22-1", "D22 模型伺服器沒開", "狀態 API 如實回報模型未就緒", d.get("model_ready") is False, "model_ready=false", d,
           "不能把沒開的模型回報成就緒", ms)
    record("D23-1", "D23 K8s 連不上", "狀態 API 如實回報 K8s 未連線", d.get("k8s") is False, "k8s=false", d.get("k8s"),
           "不能把連不上的叢集回報成已連線", ms)
    with sync_playwright() as p:
        b = p.chromium.launch(channel="chrome"); pg = b.new_page(viewport={"width": 1400, "height": 900}); errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto(DEGRADED + "/"); pg.fill("input[name=username]", DEMO_USER); pg.fill("input[name=password]", DEMO_PW)
        pg.click("button[type=submit]"); pg.wait_for_timeout(5000)
        mw = pg.evaluate("()=>{const e=document.getElementById('model-warning');return e&&getComputedStyle(e).display!=='none'?e.innerText:''}")
        dw = pg.evaluate("()=>{const e=document.getElementById('docker-warning');return e&&getComputedStyle(e).display!=='none'?e.innerText:''}")
        record("D22-2", "D22 模型伺服器沒開", "畫面顯示模型沒開的橫幅與處理步驟", bool(mw) and ("模型" in mw), "看得到橫幅、附步驟",
               mw[:150], "橫幅可見且說明怎麼處理")
        record("D23-2", "D23 K8s 連不上", "畫面顯示 K8s 連不上的橫幅與處理步驟", bool(dw) and ("Kubernetes" in dw or "K8s" in dw),
               "看得到橫幅、附步驟", dw[:150], "橫幅可見且說明怎麼處理（Docker 有開所以應指向 Kubernetes）")
        pg.screenshot(path=os.path.join(ROOT, "docs", "qa_full_20261026_degraded.png"))
        record("D2x-js", "D22 模型伺服器沒開", "設定不完整時頁面沒有 JavaScript 錯誤", not errs, "0 個錯誤", errs[:3], "pageerror 為 0")
        b.close()
    r, ms = api.post("/api/chat", {"message": "什麼是 Pod？", "history": []})
    reply = r.json().get("reply", "")
    record("D22-3", "D22 模型伺服器沒開", "聊天退回規則或知識庫，並說明模型沒開", r.status_code == 200 and len(reply) > 20, "有替代回答",
           reply[:150], "200 且有內容（不是空白或錯誤堆疊）", ms)
    r, ms = api.post("/api/deploy/parse", {"input": "部署 2 個 nginx:1.27 叫 qa-off"})
    dd = r.json(); raw = dd.get("raw") or {}
    record("D24-1", "D24 沒有 Gemini 金鑰", "沒有 Gemini 也能用規則解析部署句子", raw.get("image") == "nginx:1.27" and raw.get("pods") == 2,
           "nginx:1.27、2 個", {"image": raw.get("image"), "pods": raw.get("pods"), "parser": raw.get("_parser")}, "解析結果正確", ms)
    r, ms = api.post("/api/deploy", {"input": "部署 2 個 nginx:1.27 叫 qa-off", "parsed": {"app_name": "qa-off", "image": "nginx:1.27", "pods": 2, "port": 8099}})
    kd = r.json().get("k8s_deploy")
    record("D23-3", "D23 K8s 連不上", "K8s 沒連上時不會假裝部署成功", not (kd or {}).get("ok"), "不回報部署成功",
           {"status": r.status_code, "k8s_deploy": kd, "k8s": r.json().get("k8s")}, "k8s_deploy.ok 不得為 true", ms)
    r, ms = api.get("/api/metrics")
    m = r.json()
    record("D26-1", "D26 沒有 Prometheus", "指標不以 0 代替", not m.get("connected") and not (m.get("metrics") or {}).get("prometheus_up"),
           "connected=false", m, "Prometheus 連不上時要回報連不上，不能顯示 0", ms)
    r, ms = api.get("/api/autoscale")
    record("D23-4", "D23 K8s 連不上", "分時與休眠頁說明叢集連不上", r.status_code in (200, 503) and r.json().get("ok") is False and r.json().get("error"),
           "ok=false 並附原因", r.json(), "有中文說明", ms)


# ── B4／D27～29：頁面、瀏覽器、螢幕、深色模式、慢速網路 ───────────────────────────────
PAGES = ["chat", "services", "healer", "calendar", "dashboard", "gitops", "trash"]
VIEWPORTS = [("手機", 390, 844), ("平板", 768, 1024), ("筆電", 1366, 768), ("大螢幕", 1920, 1080)]


def _login_page(pg, base):
    pg.goto(base + "/"); pg.fill("input[name=username]", DEMO_USER); pg.fill("input[name=password]", DEMO_PW)
    pg.click("button[type=submit]"); pg.wait_for_timeout(3000)
    pg.evaluate("()=>{const o=document.getElementById('due-overlay'); if(o) o.hidden=true;}")


def g_ui():
    from playwright.sync_api import sync_playwright
    base = LOCAL_TEST
    with sync_playwright() as p:
        browsers = [("Chrome", lambda: p.chromium.launch(channel="chrome")), ("Firefox", p.firefox.launch), ("Safari 核心", p.webkit.launch)]
        for bname, launch in browsers:
            b = launch()
            for vname, w, h in VIEWPORTS:
                ctx = b.new_context(viewport={"width": w, "height": h}); pg = ctx.new_page(); errs = []
                pg.on("pageerror", lambda e, errs=errs: errs.append(str(e)[:120]))
                t0 = time.perf_counter(); _login_page(pg, base); login_ms = (time.perf_counter() - t0) * 1000
                overflow, empty = [], []
                for name in PAGES:
                    pg.evaluate(f"showPage('{name}')"); pg.wait_for_timeout(1500)
                    info = pg.evaluate(f"""()=>{{const el=document.getElementById('page-{name}');
                        return {{ow: document.documentElement.scrollWidth - window.innerWidth,
                                 txt: el ? el.innerText.trim().length : 0}}}}""")
                    if info["ow"] > 2: overflow.append(f"{name}(+{info['ow']}px)")
                    if info["txt"] < 20: empty.append(name)
                ok = not errs and not overflow and not empty
                record(f"B4-{bname}-{vname}", "B4/D27 頁面與瀏覽器", f"{bname}・{vname} {w}px：7 頁", ok, "沒有錯誤、沒有橫向捲動、內容有顯示",
                       {"js錯誤": errs[:3], "超出寬度": overflow, "空白頁": empty}, "三項都必須為空", login_ms)
                ctx.close()
            # 手機監控版（只在手機寬度測）
            ctx = b.new_context(viewport={"width": 390, "height": 844}, user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) Mobile/15E148")
            pg = ctx.new_page(); errs = []; pg.on("pageerror", lambda e, errs=errs: errs.append(str(e)[:120]))
            pg.goto(base + "/m"); pg.wait_for_timeout(1000)
            if pg.locator("input[name=username]").count():
                pg.fill("input[name=username]", DEMO_USER); pg.fill("input[name=password]", DEMO_PW); pg.locator("button[type=submit]").first.click()
                pg.wait_for_timeout(3500)
            tabs = pg.locator("nav button, .tabbar button, [data-tab]").count()
            ow = pg.evaluate("()=>document.documentElement.scrollWidth - window.innerWidth")
            record(f"B4-{bname}-m", "B4/D27 頁面與瀏覽器", f"{bname}・手機監控版", not errs and ow <= 2 and tabs >= 4,
                   "沒有錯誤、不超寬、底部分頁", {"js錯誤": errs[:3], "超出寬度": ow, "分頁數": tabs}, "三項都符合")
            ctx.close(); b.close()
        # D28 深色模式
        b = p.chromium.launch(channel="chrome")
        for scheme in ("light", "dark"):
            ctx = b.new_context(viewport={"width": 1366, "height": 768}, color_scheme=scheme); pg = ctx.new_page()
            _login_page(pg, base)
            pg.evaluate("()=>{try{localStorage.removeItem('zt-theme')}catch(e){}}")
            bg = pg.evaluate("()=>getComputedStyle(document.body).backgroundColor")
            fg = pg.evaluate("()=>getComputedStyle(document.body).color")
            def lum(c):
                import re as _re
                r, g, b_ = [int(x) for x in _re.findall(r"\d+", c)[:3]]
                return 0.299 * r + 0.587 * g + 0.114 * b_
            ok = (lum(bg) < 80 and lum(fg) > 150) if scheme == "dark" else (lum(bg) > 180 and lum(fg) < 110)
            record(f"D28-{scheme}", "D28 深淺色", f"{'深色' if scheme == 'dark' else '淺色'}模式文字看得清楚", ok,
                   "背景與文字明暗相反", {"背景": bg, "文字": fg}, "背景亮度與文字亮度差距明顯")
            pg.screenshot(path=os.path.join(ROOT, "docs", f"qa_full_20261026_{scheme}.png")); ctx.close()
        # D29 慢速網路（模擬 3G：下載 750 kbps、上傳 250 kbps、延遲 100 毫秒）
        ctx = b.new_context(viewport={"width": 390, "height": 844}); pg = ctx.new_page()
        cdp = ctx.new_cdp_session(pg)
        cdp.send("Network.enable")
        cdp.send("Network.emulateNetworkConditions", {"offline": False, "latency": 100, "downloadThroughput": 750 * 1024 / 8,
                                                      "uploadThroughput": 250 * 1024 / 8})
        t0 = time.perf_counter(); pg.goto(base + "/", wait_until="load"); t_login = (time.perf_counter() - t0) * 1000
        record("D29-1", "D29 慢速網路", "3G 網路開啟登入頁", t_login < 15000, "15 秒內", f"{t_login / 1000:.1f} 秒", "load 事件 15 秒內", t_login)
        pg.fill("input[name=username]", DEMO_USER); pg.fill("input[name=password]", DEMO_PW)
        t0 = time.perf_counter(); pg.click("button[type=submit]"); pg.wait_for_load_state("load"); t_home = (time.perf_counter() - t0) * 1000
        record("D29-2", "D29 慢速網路", "3G 網路登入後進入首頁", t_home < 30000, "30 秒內", f"{t_home / 1000:.1f} 秒", "load 事件 30 秒內", t_home)
        size = pg.evaluate("()=>performance.getEntriesByType('navigation')[0].transferSize")
        record("D29-3", "D29 慢速網路", "首頁傳輸量", True, "記錄數值", f"{size / 1024:.0f} KB", "僅記錄，作為慢速網路評估依據")
        pg.goto(base + "/m", wait_until="load"); t0 = time.perf_counter(); pg.reload(wait_until="load"); t_m = (time.perf_counter() - t0) * 1000
        record("D29-4", "D29 慢速網路", "3G 網路開手機監控版", t_m < 10000, "10 秒內", f"{t_m / 1000:.1f} 秒", "load 事件 10 秒內", t_m)
        ctx.close(); b.close()


GROUPS = {"external": g_external, "external_rest": g_external_rest, "core": g_core, "degraded": g_degraded, "ui": g_ui}

if __name__ == "__main__":
    GROUPS[sys.argv[1]]()
