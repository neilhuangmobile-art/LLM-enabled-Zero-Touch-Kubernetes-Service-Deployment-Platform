"""第二次實驗：盲測（2026-11-07）

題目由 Gemini（使用者在網頁版用 argon 模型出題）寫好，放在 docs/bench_blind/blind_tasks.jsonl。
開發端（Claude Code）沒有讀過題目內容，所以這支程式：
- 只讀檔、照順序送給兩個系統，**畫面上只印題號與結果種類，不印題目文字**。
- 不判斷對錯，只把「實際發生了什麼」記錄下來（blind_raw.jsonl）；對錯由 grade_blind.py 交給
  另一個模型（gemini-pro-latest）依題目自帶的 pass_if 判斷。
- 「部署完之後的操作」這類題目需要先有服務存在。要先建什麼服務，也交給評分模型讀題目決定
  （blind_setup.json），兩個系統用同一份前置設定。

ZeroTouch 走網頁實際的流程：先 /api/intent 判斷意圖，要部署就走 解析 → 審查 → 確認部署；
要擴縮、換版本、回滾、刪除就呼叫對應的 API；其他（問答、反問）就用 /api/chat 取得回覆。
Claude Code 跟第一次實驗一樣用 `claude -p` 無人值守模式。

用法：BENCH_OUT=docs/bench_20261107_r2 python scripts/bench/bench_blind.py [輪數=2]
"""
import json, os, subprocess, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (ADMIN, CL_NS, OUT, ROOT, SYSTEMS, ZT, ZT_NS, QuotaExhausted, ZeroTouch, append,  # noqa: E402
                    clean_both, gen_delta, gen_stats, order_for, rounds_to_run, run_claude, snapshot, wait_ready)
from gemini_judge import ask_json  # noqa: E402

TASKS_FILE = Path(os.environ.get("BLIND_TASKS") or (ROOT / "docs" / "bench_blind" / "blind_tasks.jsonl"))
SETUP_FILE = OUT / "blind_setup.json"

SETUP_PROMPT = """你是實驗的前置設定助手。下面是一位使用者對部署平台說的話。
請判斷：這句話是不是在要求「修改一個已經存在的服務」（例如加大、縮小、換版本、換回上一版、刪掉、查狀態）？
如果是，請設計測試開始前應該先存在的那個服務，讓這句話有意義。

使用者說的話：{text}

只輸出 JSON：
{{"needs_setup": true 或 false,
  "app_name": "服務名稱（小寫英文、數字、連字號，字母開頭；使用者有講名稱就用使用者的）",
  "image": "目前正在跑的映像檔（含版本）",
  "previous_image": "如果題目跟「上一版」有關，填上一版的映像檔（含版本），否則填 null",
  "replicas": 目前的副本數（整數）}}
不需要前置服務時，其他欄位填 null。"""


def load_tasks():
    tasks = []
    for ln in TASKS_FILE.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if ln and not ln.startswith("```"):
            tasks.append(json.loads(ln))
    return tasks


def load_setups(tasks):
    """每題的前置設定只產生一次並存檔，兩個系統、每一輪都用同一份。"""
    setups = json.loads(SETUP_FILE.read_text(encoding="utf-8")) if SETUP_FILE.exists() else {}
    for t in tasks:
        if t["id"] in setups:
            continue
        r = ask_json(SETUP_PROMPT.format(text=t["text"]))
        setups[t["id"]] = r if isinstance(r, dict) else {"needs_setup": False}
        SETUP_FILE.write_text(json.dumps(setups, ensure_ascii=False, indent=1), encoding="utf-8")
    return setups


DEP_YAML = """apiVersion: apps/v1
kind: Deployment
metadata: {{name: {n}, namespace: {ns}}}
spec:
  replicas: {r}
  selector: {{matchLabels: {{app: {n}}}}}
  template:
    metadata: {{labels: {{app: {n}}}}}
    spec:
      containers:
      - name: {n}
        image: {img}
        resources: {{requests: {{memory: 64Mi, cpu: 50m}}, limits: {{memory: 256Mi, cpu: 500m}}}}
"""


def apply_setup(system, setup, zt):
    """建好前置服務。有 previous_image 時先部署上一版再換成目前的版本，兩邊都留下版本紀錄。"""
    if not setup or not setup.get("needs_setup") or not setup.get("app_name") or not setup.get("image"):
        return None
    n, img, prev = setup["app_name"], setup["image"], setup.get("previous_image")
    try:
        r = max(1, min(int(setup.get("replicas") or 1), 5))
    except (TypeError, ValueError):
        r = 1
    first = prev or img
    if system == "zerotouch":   # 透過平台自己的流程建，平台才有 GitOps 與事件紀錄（回滾要用）
        zt.deploy(f"部署 {first}，名稱 {n}，要 {r} 個")
        if prev:
            zt.s.post(ZT + "/api/update", json={"name": n, "image": img}, timeout=300)
    else:
        subprocess.run(ADMIN + ["apply", "-f", "-"], input=DEP_YAML.format(n=n, ns=CL_NS, r=r, img=first),
                       text=True, capture_output=True)
        if prev:
            subprocess.run(ADMIN + ["-n", CL_NS, "set", "image", f"deploy/{n}", f"{n}={img}"], capture_output=True)
    wait_ready(ZT_NS if system == "zerotouch" else CL_NS, timeout=120)
    return {"app_name": n, "image": img, "previous_image": prev, "replicas": r}


def zt_handle(zt, text):
    """照網頁的流程：先判斷意圖，再做對應的事。回傳實際發生的事（不判斷對錯）。"""
    t0 = time.time()
    rec: dict = {"route": None}
    try:
        intent = zt.s.post(ZT + "/api/intent", json={"message": text}, timeout=300).json()
    except Exception as e:  # noqa: BLE001
        intent = {"action": "qa", "error": str(e)[:200]}
    act, args = intent.get("action"), intent.get("args") or {}
    rec["intent"] = {"action": act, "args": args}
    name = (args.get("name") or "").strip()
    calls = {"scale": ("/api/scale", {"name": name, "replicas": args.get("replicas")}),
             "update_image": ("/api/update", {"name": name, "image": args.get("image")}),
             "rollback": ("/api/rollback", {"app_name": name}),
             "delete": ("/api/delete", {"name": name})}
    if act == "deploy":
        rec["route"] = "deploy"
        rec.update(zt.deploy(text))
    elif act in calls and name and (act != "update_image" or args.get("image")) and (act != "scale" or args.get("replicas")):
        rec["route"] = act
        path, body = calls[act]
        r = zt.s.post(ZT + path, json=body, timeout=300)
        try:
            res = r.json()
        except Exception:  # noqa: BLE001
            res = {"error": r.text[:300]}
        rec["api_result"] = {k: res.get(k) for k in ("success", "ok", "message", "error") if k in res}
        rec["outcome"] = "executed" if (res.get("success") or res.get("ok")) is True else "api_failed"
    else:
        # 問答、反問、缺參數：網頁會用聊天回覆，這裡也取得回覆內容給評分看
        rec["route"] = "chat"
        try:
            d = zt.s.post(ZT + "/api/chat", json={"message": text, "history": []}, timeout=600).json()
            rec["reply"] = str(d.get("reply") or d.get("response") or d.get("error") or "")[:2000]
        except Exception as e:  # noqa: BLE001
            rec["reply"] = f"(聊天失敗：{e})"
        rec["outcome"] = "replied"
    rec["total_s"] = round(time.time() - t0, 2)
    return rec


def run_one(system, task, setup, rnd, zt):
    ns = ZT_NS if system == "zerotouch" else CL_NS
    account_reset = zt.reset_if_flagged() if system == "zerotouch" else False
    clean_both()
    pre = apply_setup(system, setup, zt)
    g0 = gen_stats()
    if system == "zerotouch":
        rec = zt_handle(zt, task["text"])
        rec["gpu"] = gen_delta(g0, gen_stats())
    else:
        rec = run_claude(task["text"], f"D:/bench/claude_ws_blind/{task['id']}_r{rnd}")
        rec["outcome"] = "agent_done" if not rec["timed_out"] else "timeout"
    ready, ready_s, state = wait_ready(ns, timeout=180)
    snap = snapshot(ns, f"blind_{task['id']}_{system}_r{rnd}")
    out = {"suite": "blind", "task_id": task["id"], "category": task.get("category"), "input": task["text"],
           "system": system, "round": rnd, "setup": pre, "outcome": rec.get("outcome"),
           "ready": ready, "ready_wait_s": ready_s, "ready_state": state,
           "latency_s": rec.get("total_s"), "cost_usd": rec.get("cost_usd"), "gpu": rec.get("gpu"),
           "snapshot": snap, "account_reset_before": account_reset,
           # 系統對使用者說了什麼：ZeroTouch 的提示、錯誤、聊天回覆；Claude 的最後回覆
           "said": {k: rec.get(k) for k in ("message", "reply", "result", "decision", "warnings", "blockers")
                    if rec.get(k)},
           "notices": ((rec.get("parsed") or {}).get("_notices") or []),
           "detail": {k: v for k, v in rec.items() if k not in ("commands", "parsed")},
           "commands": rec.get("commands", []), "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
    append(OUT / "blind_raw.jsonl", out)
    # 只印題號與結果種類，不印題目文字（開發端不看題目）
    print(f"[{out['ts']}] {task['id']} r{rnd} {system:9} {str(out['outcome']):12} ready={ready!s:5} "
          f"{out['latency_s']}s ${out['cost_usd'] or 0:.3f}", flush=True)


if __name__ == "__main__":
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    tasks = load_tasks()
    only = {x for x in os.environ.get("BLIND_ONLY", "").split(",") if x}   # 冒煙測試用：只跑指定題號
    if only:
        tasks = [t for t in tasks if t["id"] in only]
    setups = load_setups(tasks)
    print(f"盲測 {len(tasks)} 題，需要前置服務的 {sum(1 for s in setups.values() if s.get('needs_setup'))} 題，"
          f"輸出到 {OUT}", flush=True)
    zt = ZeroTouch() if "zerotouch" in SYSTEMS else None
    # 接續用：已經有有效紀錄的（不是測試程式錯誤）就跳過。作廢的紀錄要先移到 blind_raw_invalid.jsonl
    raw_path = OUT / "blind_raw.jsonl"
    done = set()
    if raw_path.exists():
        for ln in raw_path.read_text(encoding="utf-8").splitlines():
            if ln.strip():
                r0 = json.loads(ln)
                if r0.get("outcome") != "harness_error":
                    done.add((r0["task_id"], r0["system"], r0["round"]))
    for rnd in rounds_to_run(rounds):
        for task in tasks:
            for system in order_for(rnd):
                if (task["id"], system, rnd) in done:
                    continue
                try:
                    run_one(system, task, setups.get(task["id"]), rnd, zt)
                except QuotaExhausted:
                    print("Claude 額度用完，停止（這一筆不寫入）", flush=True)
                    sys.exit(3)
                except Exception as e:  # noqa: BLE001
                    append(OUT / "blind_raw.jsonl", {"suite": "blind", "task_id": task["id"], "system": system,
                                                     "round": rnd, "outcome": "harness_error", "error": str(e)[:300]})
                    print(f"{task['id']} r{rnd} {system} HARNESS ERROR {type(e).__name__}", flush=True)
    clean_both()
