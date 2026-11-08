"""實驗 C：故障修復（同一個故障同時植入兩邊）

- ZeroTouch：植入後不做任何事，看背景 Healer（每 30 秒巡檢）能不能自己發現並修好。計時從植入開始。
- Claude Code：植入後等故障真的發生（使用者「發現」），再用新手的話請它修。計時從開口開始；
  它不會自己發現故障，這件事在報告裡另外寫，不混進修復時間。
判定：限時內工作負載全部就緒，而且修法符合題目要求（例如映像檔要換回能用的版本）才算通過。
用法：python scripts/bench/bench_faults.py [輪數=2]
"""
import json, subprocess, sys, time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (ADMIN, CL_NS, OUT, SYSTEMS, ZT, ZT_NS, ZeroTouch, append, clean, kubectl_json,  # noqa: E402
                    rounds_to_run, run_claude, snapshot, wait_ready)

FAULTS = {
    # 記憶體上限太小：程式要 100MB，上限只給 64Mi → OOMKilled
    "F1_oom": {
        "desc": "記憶體不足（OOMKilled）",
        "good": None,
        "manifest": lambda ns: f"""
apiVersion: apps/v1
kind: Deployment
metadata: {{name: report-api, namespace: {ns}, labels: {{app: report-api}}}}
spec:
  replicas: 1
  selector: {{matchLabels: {{app: report-api}}}}
  template:
    metadata: {{labels: {{app: report-api}}}}
    spec:
      containers:
      - name: report-api
        image: python:3.12-slim
        command: ["python", "-c", "a=bytearray(100*1024*1024); import time; time.sleep(36000)"]
        resources: {{requests: {{memory: 32Mi, cpu: 50m}}, limits: {{memory: 64Mi, cpu: 200m}}}}
""",
        "ask": "我的服務 report-api 一直重開，好像壞掉了，幫我修好",
        "verify": lambda s: (True, "就緒即可"),
    },
    # 換到不存在的映像檔版本 → ImagePullBackOff；正確修法是換回能用的版本
    "F2_badimage": {
        "desc": "映像檔版本不存在（ImagePullBackOff）",
        "manifest": lambda ns: f"""
apiVersion: apps/v1
kind: Deployment
metadata: {{name: shop-web, namespace: {ns}, labels: {{app: shop-web}}}}
spec:
  replicas: 2
  selector: {{matchLabels: {{app: shop-web}}}}
  template:
    metadata: {{labels: {{app: shop-web}}}}
    spec:
      containers:
      - name: shop-web
        image: nginx:1.27
        ports: [{{containerPort: 80}}]
        resources: {{requests: {{memory: 64Mi, cpu: 50m}}, limits: {{memory: 128Mi, cpu: 200m}}}}
""",
        "break": lambda ns: ADMIN + ["-n", ns, "set", "image", "deploy/shop-web", "shop-web=nginx:1.27-doesnotexist"],
        "ask": "我剛更新完網站 shop-web 之後就打不開了，幫我修好",
        "verify": lambda s: (all("doesnotexist" not in x["image"] for x in s["spec"]), "映像檔已換回可用版本"),
    },
    # 存活檢查打錯 port（8081，實際是 80）→ 一直被重啟
    "F3_probe": {
        "desc": "健康檢查設錯（一直被重啟）",
        "manifest": lambda ns: f"""
apiVersion: apps/v1
kind: Deployment
metadata: {{name: club-page, namespace: {ns}, labels: {{app: club-page}}}}
spec:
  replicas: 1
  selector: {{matchLabels: {{app: club-page}}}}
  template:
    metadata: {{labels: {{app: club-page}}}}
    spec:
      containers:
      - name: club-page
        image: nginx:1.27
        ports: [{{containerPort: 80}}]
        livenessProbe: {{httpGet: {{path: /, port: 8081}}, initialDelaySeconds: 3, periodSeconds: 3, failureThreshold: 2}}
        readinessProbe: {{httpGet: {{path: /, port: 8081}}, periodSeconds: 3}}
        resources: {{requests: {{memory: 64Mi, cpu: 50m}}, limits: {{memory: 128Mi, cpu: 200m}}}}
""",
        "ask": "我的社團網頁 club-page 一直重開、打不開，幫我修好",
        "verify": lambda s: (True, "就緒即可"),
    },
}


def apply(text):
    subprocess.run(ADMIN + ["apply", "-f", "-"], input=text, text=True, capture_output=True)


def inject(fid, ns):
    f = FAULTS[fid]
    apply(f["manifest"](ns))
    if "break" in f:
        wait_ready(ns, timeout=90)  # 先讓好的版本跑起來，留下可以回滾的紀錄
        subprocess.run(f["break"](ns), capture_output=True)


def wait_fault(ns, timeout=90):
    """等故障真的出現（有容器處於 waiting/重啟）。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        for p in kubectl_json(["get", "pods", "-n", ns])["items"]:
            for c in p.get("status", {}).get("containerStatuses", []) or []:
                w = (c.get("state") or {}).get("waiting") or {}
                if w.get("reason") in ("CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull") or c.get("restartCount", 0) >= 2:
                    return round(time.time() - t0, 1), w.get("reason") or "restarting"
        time.sleep(3)
    return None, "no-fault-seen"


def healer_actions(s, since):
    try:
        d = s.get(ZT + "/api/healer/status", timeout=30).json()
    except Exception:  # noqa: BLE001
        return []
    acts = d.get("recent_actions") or (d.get("background") or {}).get("recent_actions") or []
    return [a for a in acts if a.get("namespace") == ZT_NS and (a.get("time") or "") >= since]


def stable_ready(ns, timeout):
    """就緒而且 20 秒內沒有再重啟，才算真的修好（避免剛好在兩次崩潰之間量到）。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        ok, _, _ = wait_ready(ns, timeout=max(5, timeout - (time.time() - t0)))
        if not ok:
            break
        r0 = sum(c.get("restartCount", 0) for p in kubectl_json(["get", "pods", "-n", ns])["items"]
                 for c in p.get("status", {}).get("containerStatuses", []) or [])
        time.sleep(20)
        pods = kubectl_json(["get", "pods", "-n", ns])["items"]
        r1 = sum(c.get("restartCount", 0) for p in pods for c in p.get("status", {}).get("containerStatuses", []) or [])
        ok2, _, _ = wait_ready(ns, timeout=3)
        if ok2 and r1 == r0:
            return True, round(time.time() - t0, 1)
    return False, round(time.time() - t0, 1)


def run_zt(fid, rnd, zt):
    clean(ZT_NS)
    since = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    t0 = time.time()
    inject(fid, ZT_NS)
    seen_s, reason = wait_fault(ZT_NS)
    ok, _ = stable_ready(ZT_NS, timeout=420)
    total = round(time.time() - t0, 1)
    snap = snapshot(ZT_NS, f"{fid}_zerotouch_r{rnd}")
    vok, vwhy = FAULTS[fid]["verify"](snap)
    acts = healer_actions(zt.s, since)
    passed = ok and vok
    rec = {"suite": "C_fault", "task_id": fid, "category": FAULTS[fid]["desc"], "input": "(植入故障後不做任何事)",
           "system": "zerotouch", "round": rnd, "passed": passed,
           "pass_rule": ("420 秒內自動恢復且穩定 20 秒；" + vwhy) if passed else f"沒有自動恢復（{'就緒' if ok else '未就緒'}；{vwhy if ok else ''}）",
           "fault_seen_s": seen_s, "fault_reason": reason, "recover_s_from_inject": total if ok else None,
           "human_needed": not ok, "cost_usd": 0.0, "healer_actions": acts, "snapshot": snap,
           "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
    append(OUT / "C_fault.jsonl", rec)
    print(f"[{rec['ts']}] {fid} r{rnd} zerotouch pass={passed} recover={rec['recover_s_from_inject']} actions={[a.get('action') for a in acts]}", flush=True)


def run_cl(fid, rnd):
    clean(CL_NS)
    inject(fid, CL_NS)
    seen_s, reason = wait_fault(CL_NS)
    time.sleep(15)  # 讓故障持續一下，模擬使用者發現後才開口
    t0 = time.time()
    r = run_claude(FAULTS[fid]["ask"], f"D:/bench/claude_ws/{fid}_r{rnd}", timeout=600)
    agent_s = round(time.time() - t0, 1)
    ok, _ = stable_ready(CL_NS, timeout=120)
    snap = snapshot(CL_NS, f"{fid}_claude_r{rnd}")
    vok, vwhy = FAULTS[fid]["verify"](snap)
    passed = ok and vok
    rec = {"suite": "C_fault", "task_id": fid, "category": FAULTS[fid]["desc"], "input": FAULTS[fid]["ask"],
           "system": "claude", "round": rnd, "passed": passed,
           "pass_rule": ("使用者開口後修好且穩定 20 秒；" + vwhy) if passed else f"沒有修好（{'就緒' if ok else '未就緒'}；{vwhy if ok else ''}）",
           "fault_seen_s": seen_s, "fault_reason": reason, "agent_s": agent_s, "recover_s_after_ask": round(time.time() - t0, 1) if ok else None,
           "human_needed": True, "cost_usd": r.get("cost_usd"), "turns": r.get("turns"), "result": r.get("result"),
           "commands": r.get("commands"), "snapshot": snap, "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
    append(OUT / "C_fault.jsonl", rec)
    print(f"[{rec['ts']}] {fid} r{rnd} claude    pass={passed} agent={agent_s}s ${r.get('cost_usd') or 0:.3f}", flush=True)


if __name__ == "__main__":
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    zt = ZeroTouch() if "zerotouch" in SYSTEMS else None
    for rnd in rounds_to_run(rounds):
        for fid in FAULTS:
            if "zerotouch" in SYSTEMS:
                run_zt(fid, rnd, zt)
            if "claude" in SYSTEMS:
                run_cl(fid, rnd)
    for s in SYSTEMS:
        clean(ZT_NS if s == "zerotouch" else CL_NS)
