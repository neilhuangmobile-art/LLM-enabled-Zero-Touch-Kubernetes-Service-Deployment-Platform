"""
experiments/healer_eval/run_eval.py
Healer 評估（Healer 方案第 6 節，2026-11-25）：在真的叢集注入故障，跑 Healer，依 K8s 狀態判定。

每題一個 namespace `healer-eval-<run>-<id>`，所有題目同時跑，結束整個刪掉。
用 --code-root 指定要評估的 Healer 程式碼（例如舊版的 git worktree），同一批題目新舊版各跑一次做配對比較。

用法（專案根目錄）：
  python experiments/healer_eval/run_eval.py --tag new
  python experiments/healer_eval/run_eval.py --tag old --code-root ../zt-healer-old
  python experiments/healer_eval/run_eval.py --compare old new
  python experiments/healer_eval/run_eval.py --tag blind1 --faults <組員出的題組.json>   # 盲測（見 BLIND_GUIDE.md）
注意：
- 網站的背景 Healer 也會掃描整個叢集。新版網站會跳過 healer-eval-*；舊版網站不會，評估前要確認網站跑的是新版，
  或暫時停掉網站，否則網站會搶先修評估用的服務。
- 預設不呼叫診斷模型（--llm 才呼叫），動作由規則決定；兩版用同一個設定才能比較。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, "..", ".."))
RESULTS = os.path.join(HERE, "results")
AUTO = {"increase_memory", "fix_image", "fix_probe", "analyze_logs", "revert_recent_change"}


def _load_code(code_root: str):
    """被評估的 Healer 程式碼放最前面（舊版 worktree 或目前的程式）。"""
    sys.path.insert(0, os.path.abspath(code_root))
    os.environ.setdefault("HEALER_GITOPS", "0")      # 評估用的服務沒有部署紀錄，也不要寫進去
    os.environ.setdefault("HEALER_NOT_READY_MIN", "1")   # 正式是 15 分鐘；評估最多等 7 分鐘，縮短門檻
    import healer.pod_watcher as pw  # noqa: F401
    from healer.diagnose import diagnose_issue
    remediate = sys.modules["healer.remediate"].remediate if "healer.remediate" in sys.modules else None
    if remediate is None:
        import healer.remediate  # noqa: F401
        remediate = sys.modules["healer.remediate"].remediate
    try:
        from healer import guard, verify
    except ImportError:
        guard = verify = None
    try:
        from healer import detect   # 2026-11-25 階段 2；舊版沒有
    except ImportError:
        detect = None
    return pw, diagnose_issue, remediate, guard, verify, detect


def _k8s():
    from kubernetes import client, config
    config.load_kube_config()
    return client, client.CoreV1Api(), client.AppsV1Api()


def _ns_for(run: str, cid: str) -> str:
    return f"healer-eval-{run}-{cid}"[:63].rstrip("-")


def _healthy(apps, ns, name) -> bool:
    try:
        d = apps.read_namespaced_deployment(name, ns)
    except Exception:  # noqa: BLE001
        return False
    want = d.spec.replicas or 0
    st = d.status
    # 沒有卡住的舊 Pod（status.replicas＝副本數）、沒有不可用的副本：映像檔改壞時舊 Pod 還在跑，不能算健康
    return want > 0 and (st.available_replicas or 0) >= want and (st.updated_replicas or 0) >= want \
        and (st.replicas or 0) == want and not st.unavailable_replicas \
        and (st.observed_generation or 0) >= (d.metadata.generation or 0)


def _create_extra(core, ns, obj):
    kind = obj["kind"]
    fn = {"Service": core.create_namespaced_service,
          "PersistentVolumeClaim": core.create_namespaced_persistent_volume_claim,
          "ResourceQuota": core.create_namespaced_resource_quota,
          "ConfigMap": core.create_namespaced_config_map, "Secret": core.create_namespaced_secret}.get(kind)
    if fn is None:
        raise ValueError(f"評估題組不支援 {kind}")
    fn(ns, obj)


def setup(client, core, apps, case, ns):
    core.create_namespace(client.V1Namespace(metadata=client.V1ObjectMeta(name=ns, labels={"healer-eval": "1"})))
    b = case["build"]()
    for obj in b.get("extra") or []:
        _create_extra(core, ns, json.loads(json.dumps(obj)))
    dep = json.loads(json.dumps(b["deploy"]))
    if b.get("pre_deploy"):   # 先部署正常版本，等它可用，再把容器設定整個換成壞的（測「退回最近的更新」）
        apps.create_namespaced_deployment(ns, json.loads(json.dumps(b["pre_deploy"])))
        t0 = time.time()
        while time.time() - t0 < 180 and not _healthy(apps, ns, dep["metadata"]["name"]):
            time.sleep(3)
        return {"pending_patch": dep, "patch_container": True}
    if b["pre_image"]:   # 先部署正常版本，等它可用，再改成壞的（測「退回到曾經正常的版本」）
        good = json.loads(json.dumps(dep))
        good["spec"]["template"]["spec"]["containers"][0]["image"] = b["pre_image"]
        apps.create_namespaced_deployment(ns, good)
        t0 = time.time()
        while time.time() - t0 < 180 and not _healthy(apps, ns, dep["metadata"]["name"]):
            time.sleep(3)
        return {"pending_patch": dep}
    apps.create_namespaced_deployment(ns, dep)
    return {}


def tick_new_features(apps, core, guard, verify, ns, rec, detect=None):
    """新版才有的：記曾經正常的映像檔、檢查待驗證、擴大偵測（照網站背景迴圈 _healer_last_good_tick／
    _healer_verify_tick／_healer_detect_tick）。"""
    if detect is not None and detect.enabled():
        for f in detect.scan_extra(namespace=ns):
            if f["code"] not in rec["detect_codes"]:
                rec["detect_codes"].append(f["code"])
                rec["detect_s"] = rec["detect_s"] if rec["detect_s"] is not None else round(time.time() - (rec["t0"] or time.time()), 1)
    if guard is None or not guard.enabled():
        return
    rss = apps.list_namespaced_replica_set(ns).items if hasattr(guard, "current_replica_set") else []
    for d in apps.list_namespaced_deployment(ns).items:
        if hasattr(guard, "current_replica_set"):
            cur = guard.current_replica_set(d, rss)
            upd = guard.last_good_update(d, (cur.metadata.labels or {}).get("pod-template-hash") if cur else None)
        else:
            upd = guard.last_good_update(d)
        if upd:
            apps.patch_namespaced_deployment(d.metadata.name, ns, {"metadata": {"annotations": upd}})
    if verify is None or not verify.enabled():
        return
    for item in verify.PENDING.items():
        if item["namespace"] != ns:
            continue
        r = verify.evaluate(apps, core, item)
        if r["state"] == "wait":
            continue
        verify.PENDING.pop(item["key"])
        rec["verify"].append(r["state"])
        if r["state"] == "fail":
            back = verify.rollback(apps, item)
            rec["rolled_back"] = rec["rolled_back"] or bool(back.get("ok"))
            for _ in range(guard.MAX_FAILURES):
                guard.LIMITER.failed(item["key"])
        else:
            guard.LIMITER.succeeded(item["key"])


def heal_tick(pw, diagnose_issue, remediate, guard, verify, core, apps, ns, rec, seen, use_llm):
    """照網站背景迴圈處理一個 namespace（新版有冷卻、上限、上層控制器；舊版用 Pod 名稱去重）。"""
    issues = pw.scan_once(namespace=ns) or []
    now_keys = set()
    for issue in issues:
        if rec["detect_s"] is None:
            rec["detect_s"] = round(time.time() - rec["t0"], 1)
        if issue.get("job_pod"):
            continue
        if guard is not None and guard.enabled():
            pc = guard.problem_class(issue["reason"]) if hasattr(guard, "problem_class") else issue["reason"]
            key = guard.Limiter.key(ns, issue.get("deployment") or issue["pod_name"], pc)
            now_keys.add(key)
            if verify is not None and verify.PENDING.has(key):
                continue
            last = seen.get(key)
            if last is not None and time.time() - last < guard.COOLDOWN_S:
                continue
            if last is not None and guard.LIMITER.failed(key) >= guard.MAX_FAILURES:
                continue
            if guard.LIMITER.acquire(key, ns):
                continue
        else:
            key = f"{ns}/{issue['pod_name']}/{issue['reason']}"
            now_keys.add(key)
            if key in seen:
                continue
        seen[key] = time.time()
        logs = pw._get_pod_logs(issue["pod_name"], ns, issue["container"])
        events = pw._get_pod_events(issue["pod_name"], ns)
        t = time.time()
        diag = diagnose_issue({**issue, "logs": logs, "events": events}, use_llm=use_llm)
        rec["diag_ms"] = rec["diag_ms"] or int((time.time() - t) * 1000)
        rec["diag_actions"].append(diag.get("action"))
        rec.setdefault("diag_root", []).append(str(diag.get("root_cause") or "")[:120])
        t = time.time()
        result = remediate(issue, diag)
        act = result.get("action") or diag.get("action")
        rec["remediations"].append({"action": act, "ok": result.get("ok"), "message": str(result.get("message"))[:200],
                                    "details": result.get("details")})
        if act in AUTO and result.get("ok"):
            rec["acted"] += 1
            rec["fix_s"] = rec["fix_s"] or round(time.time() - rec["t0"], 1)
    for k in list(seen):
        if k not in now_keys and (guard is None or not guard.enabled() or time.time() - seen[k] > guard.COOLDOWN_S):
            seen.pop(k)


def judge(apps, case, ns, rec):
    name = case["build"]()["deploy"]["metadata"]["name"]
    healthy = _healthy(apps, ns, name)
    try:
        d = apps.read_namespaced_deployment(name, ns)
        lim = ((d.spec.template.spec.containers[0].resources.limits or {}).get("memory"))
    except Exception:  # noqa: BLE001
        lim = None
    first = rec["diag_actions"][0] if rec["diag_actions"] else ("none" if rec["detect_s"] is None else None)
    hands_off_ok = rec["acted"] <= case.get("max_actions", 0)
    if case["expected_hands_off"]:
        passed = hands_off_ok and (healthy or not case["fixable"])
    else:
        passed = healthy and rec["stable_s"] >= 120
    # 2026-11-25：擴大偵測題要報出對的 code；指定處理方式的可修題要真的用那個方式修好
    detect_ok = None
    if case.get("expected_detect"):
        detect_ok = case["expected_detect"] in rec["detect_codes"]
        passed = passed and detect_ok
    if case["category"] == "normal" and rec["detect_codes"]:
        passed = False   # 對照組被擴大偵測誤報
    if case.get("expected_remediation"):
        passed = passed and any(x["action"] == case["expected_remediation"] and x["ok"] for x in rec["remediations"])
    try:
        sp = d.spec.template.spec.containers[0].startup_probe
        startup = {"failure_threshold": sp.failure_threshold, "period_seconds": sp.period_seconds} if sp else None
    except Exception:  # noqa: BLE001
        startup = None
    made_worse = bool(case["expected_hands_off"] and rec["acted"] and case["fixable"] and not healthy)
    return {"detected": rec["detect_s"] is not None, "diag_action": first,
            "diag_correct": first == case["expected_action"], "acted": rec["acted"], "passed": bool(passed),
            "healthy_at_end": healthy, "made_worse": made_worse, "final_memory_limit": lim,
            "detect_codes": rec["detect_codes"], "detect_ok": detect_ok, "final_startup_probe": startup,
            "judge_rule": "停手題：沒有自動動作（可修好的對照組還要結束時健康）；可修題：結束時健康且穩定 120 秒；"
                          "有 expected_detect 的題還要報出那個 code；有 expected_remediation 的題要用那個方式修好"}


def load_external(path: str):
    """盲測題組（組員出的 JSON 檔，格式見 BLIND_GUIDE.md）。轉成跟 faults.CASES 一樣的結構。"""
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    out = []
    for c in raw:
        for k in ("id", "category", "expected_hands_off", "fixable", "deploy"):
            if k not in c:
                raise ValueError(f"題目 {c.get('id')} 缺少欄位 {k}")
        out.append({"id": c["id"], "category": c["category"], "variant": c.get("variant", ""), "split": "blind",
                    "expected_action": c.get("expected_action", "none"), "expected_hands_off": bool(c["expected_hands_off"]),
                    "fixable": bool(c["fixable"]), "max_actions": int(c.get("max_actions", 0)),
                    "expected_detect": c.get("expected_detect"), "expected_remediation": c.get("expected_remediation"),
                    "build": (lambda c=c: {"deploy": c["deploy"], "pre_image": c.get("pre_image"),
                                           "pre_deploy": c.get("pre_deploy"), "extra": c.get("extra") or []})})
    return out


def run(args):
    pw, diagnose_issue, remediate, guard, verify, detect = _load_code(args.code_root)
    if args.faults:
        CASES = load_external(args.faults)
    else:
        from faults import CASES
    client, core, apps = _k8s()
    cases = [c for c in CASES if not args.only or c["id"] in args.only.split(",")]
    run_id = args.run or datetime.now().strftime("%m%d%H%M")
    state = {}
    for c in cases:
        ns = _ns_for(run_id, c["id"])
        if not os.environ.get("HEALER_ONLY_NAMESPACES", "").count(ns):
            os.environ["HEALER_ONLY_NAMESPACES"] = ",".join(filter(None, [os.environ.get("HEALER_ONLY_NAMESPACES"), ns]))
        extra = setup(client, core, apps, c, ns)
        state[c["id"]] = {"ns": ns, "extra": extra, "rec": {"t0": None, "detect_s": None, "diag_ms": None, "fix_s": None,
                                                           "diag_actions": [], "remediations": [], "acted": 0,
                                                           "verify": [], "rolled_back": False, "stable_s": 0,
                                                           "stable_since": None, "detect_codes": []},
                          "seen": {}}
    # 正常版已經可用的題目，現在改成壞版本（所有題目同一個時間點開始計時）
    for c in cases:
        st = state[c["id"]]
        if st["extra"].get("pending_patch"):
            dep = st["extra"]["pending_patch"]
            if guard is not None and guard.enabled():
                tick_new_features(apps, core, guard, verify, st["ns"], st["rec"])
            c0 = dep["spec"]["template"]["spec"]["containers"][0]
            # pre_image：只換映像檔；pre_deploy：整個容器設定換成壞的（策略合併，依容器名稱）
            body = c0 if st["extra"].get("patch_container") else {"name": "app", "image": c0["image"]}
            apps.patch_namespaced_deployment(dep["metadata"]["name"], st["ns"],
                                             {"spec": {"template": {"spec": {"containers": [body]}}}})
        st["rec"]["t0"] = time.time()
    t_end = time.time() + args.max_wait
    while time.time() < t_end:
        for c in cases:
            st = state[c["id"]]
            try:
                tick_new_features(apps, core, guard, verify, st["ns"], st["rec"], detect)
                heal_tick(pw, diagnose_issue, remediate, guard, verify, core, apps, st["ns"], st["rec"], st["seen"], args.llm)
            except Exception as e:  # noqa: BLE001
                st["rec"].setdefault("errors", []).append(str(e)[:200])
            name = c["build"]()["deploy"]["metadata"]["name"]
            if _healthy(apps, st["ns"], name):
                st["rec"]["stable_since"] = st["rec"]["stable_since"] or time.time()
                st["rec"]["stable_s"] = int(time.time() - st["rec"]["stable_since"])
            else:
                st["rec"]["stable_since"], st["rec"]["stable_s"] = None, 0
        time.sleep(args.interval)
    os.makedirs(RESULTS, exist_ok=True)
    # 盲測結果另存，不覆蓋開發題的結果
    out = os.path.join(RESULTS, f"{'blind' if args.faults else 'healer'}_{args.tag}.jsonl")
    with open(out, "w", encoding="utf-8") as f:
        for c in cases:
            st = state[c["id"]]
            rec = st["rec"]
            row = {"id": c["id"], "category": c["category"], "variant": c["variant"], "split": c["split"],
                   "diag_root": rec.get("diag_root", [])[:2],
                   "expected_action": c["expected_action"], "expected_hands_off": c["expected_hands_off"],
                   **judge(apps, c, st["ns"], rec), "detect_s": rec["detect_s"], "diag_ms": rec["diag_ms"],
                   "fix_s": rec["fix_s"], "stable_s": rec["stable_s"], "verify": rec["verify"],
                   "rolled_back": rec["rolled_back"], "remediations": rec["remediations"][:6],
                   "errors": rec.get("errors", [])[:3], "code_root": args.code_root, "llm": args.llm}
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            if not args.keep:
                try:
                    core.delete_namespace(st["ns"])
                except Exception:  # noqa: BLE001
                    pass
    summary(out)


def summary(path):
    rows = [json.loads(x) for x in open(path, encoding="utf-8")]
    n = len(rows)
    print(f"{os.path.basename(path)}：{n} 題")
    print(f"  通過 {sum(r['passed'] for r in rows)}/{n}；偵測到 {sum(r['detected'] for r in rows)}；"
          f"診斷動作正確 {sum(r['diag_correct'] for r in rows)}；自動動手的題 {sum(1 for r in rows if r['acted'])}；"
          f"修壞 {sum(r['made_worse'] for r in rows)}")
    ho = [r for r in rows if r["expected_hands_off"]]
    print(f"  該停手的 {len(ho)} 題：有停手 {sum(1 for r in ho if not r['acted'])}")
    fx = [r for r in rows if not r["expected_hands_off"]]
    print(f"  該修好的 {len(fx)} 題：修好 {sum(r['passed'] for r in fx)}")
    dt = [r for r in rows if r.get("detect_ok") is not None]
    if dt:
        print(f"  擴大偵測 {len(dt)} 題：報對 {sum(r['detect_ok'] for r in dt)}")
    norm = [r for r in rows if r["category"] == "normal"]
    if norm:
        print(f"  對照組 {len(norm)} 題：被擴大偵測誤報 {sum(1 for r in norm if r.get('detect_codes'))}")
    for r in rows:
        print(f"   {'✓' if r['passed'] else '✗'} {r['id']:16s} 診斷={r['diag_action']} 動手={r['acted']} 健康={r['healthy_at_end']} "
              f"記憶體={r['final_memory_limit']} 驗證={r['verify']} 退回={r['rolled_back']} 偵測={r.get('detect_codes')}")


def compare(a, b):
    from scipy.stats import binomtest
    A = {json.loads(x)["id"]: json.loads(x) for x in open(os.path.join(RESULTS, f"healer_{a}.jsonl"), encoding="utf-8")}
    B = {json.loads(x)["id"]: json.loads(x) for x in open(os.path.join(RESULTS, f"healer_{b}.jsonl"), encoding="utf-8")}
    ids = [i for i in A if i in B]
    x = sum(B[i]["passed"] and not A[i]["passed"] for i in ids)
    y = sum(A[i]["passed"] and not B[i]["passed"] for i in ids)
    p = binomtest(min(x, y), x + y, 0.5).pvalue if x + y else 1.0
    print(f"{a} {sum(A[i]['passed'] for i in ids)}/{len(ids)} → {b} {sum(B[i]['passed'] for i in ids)}/{len(ids)}；"
          f"只有 {b} 對 {x}、只有 {a} 對 {y}，McNemar p={p:.3f}")
    for i in ids:
        if A[i]["passed"] != B[i]["passed"]:
            print(f"   {i}: {a}={'✓' if A[i]['passed'] else '✗'} {b}={'✓' if B[i]['passed'] else '✗'}")


if __name__ == "__main__":
    sys.path.insert(0, HERE)
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="new")
    ap.add_argument("--code-root", default=ROOT)
    ap.add_argument("--only", help="只跑這些題（逗號分隔 id）")
    ap.add_argument("--run", help="namespace 用的編號（預設月日時分）")
    ap.add_argument("--max-wait", type=int, default=420)
    ap.add_argument("--interval", type=int, default=15)
    ap.add_argument("--llm", action="store_true", help="診斷時呼叫模型（預設只用規則）")
    ap.add_argument("--keep", action="store_true", help="跑完不刪 namespace（除錯用）")
    ap.add_argument("--compare", nargs=2)
    ap.add_argument("--faults", help="外部題組 JSON（盲測用，見 BLIND_GUIDE.md）；結果存成 results/blind_<tag>.jsonl")
    a = ap.parse_args()
    if a.compare:
        compare(*a.compare)
    else:
        run(a)
