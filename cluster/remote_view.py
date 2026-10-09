"""
cluster/remote_view.py
把客戶叢集回報器送來的報告（cluster.remote_state.clean_report 清洗過的格式），轉成跟
web_demo.py 本機 k8s_* 查詢函式**完全相同的回傳格式**。

2026-10-09 新增（客戶自帶叢集第 5 階段）。web_demo 的查詢函式在 namespace 是客戶端
（zt- 開頭）時改呼叫這裡，所以前端、Chat 接地問答、事實核對、衝突檢查、Dashboard 等
所有呼叫端都不用改。健康判定規則跟 web_demo._pod_detail 一致（同一組異常狀態集合）。

純函式、不碰網路、不 import Flask，CI 能測。
"""
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

# 跟 web_demo._BAD_WAITING / _BAD_TERMINATED 一致
BAD_WAITING = {"CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull",
               "CreateContainerConfigError", "CreateContainerError", "InvalidImageName"}
BAD_TERMINATED = {"OOMKilled", "Error", "ContainerCannotRun", "DeadlineExceeded"}
_TW = timezone(timedelta(hours=8))


def parse_ts(iso: Optional[str]) -> Optional[datetime]:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None


def fmt_ts(iso: Optional[str]) -> str:
    """跟 web_demo._fmt_k8s_time 同格式：台灣時間 YYYY-MM-DD HH:MM。"""
    ts = parse_ts(iso)
    if not ts:
        return ""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(_TW).strftime("%Y-%m-%d %H:%M")


def _sort_key(iso: Optional[str]) -> float:
    ts = parse_ts(iso)
    return ts.timestamp() if ts else 0.0


# ── 列表 ──────────────────────────────────────────────────────────

def list_pods(rep: Dict[str, Any], app: Optional[str] = None) -> List[dict]:
    """同 web_demo.k8s_get_pods。"""
    out = []
    for p in sorted(rep.get("pods") or [], key=lambda p: _sort_key(p.get("created")), reverse=True):
        if app and p.get("app") != app:
            continue
        specs = {c["name"]: c for c in p.get("spec_containers") or []}
        containers = []
        for c in p.get("containers") or []:
            sp = specs.get(c["name"], {})
            containers.append({"name": c["name"], "image": sp.get("image") or c.get("image", ""),
                               "ports": sp.get("ports", []),
                               "resources": {"requests": sp.get("requests", {}), "limits": sp.get("limits", {})}})
        ready_all = bool(p.get("containers")) and all(c.get("ready") for c in p["containers"])
        out.append({
            "name": p["name"], "app": p.get("app") or "", "phase": p.get("phase") or "Unknown",
            "ip": p.get("ip") or "", "node": p.get("node") or "", "age": fmt_ts(p.get("created")),
            "containers": containers,
            "conditions": [{"type": "Ready", "status": "True" if ready_all else "False"}],
            "restarts": sum(c.get("restart_count", 0) for c in p.get("containers") or []),
        })
    return out


def list_deployments(rep: Dict[str, Any]) -> List[dict]:
    """同 web_demo.k8s_get_deployments。回報器自己的 zt-reporter 不列出（那是平台元件，不是客戶的服務）。"""
    out = []
    for d in sorted(rep.get("deployments") or [], key=lambda d: _sort_key(d.get("created")), reverse=True):
        if d["name"] == "zt-reporter":
            continue
        out.append({"name": d["name"], "replicas": d.get("replicas", 0), "ready": d.get("ready", 0),
                    "image": d.get("image") or "", "age": fmt_ts(d.get("created")),
                    "updated": fmt_ts(d.get("updated") or d.get("created"))})
    return out


def list_services(rep: Dict[str, Any], namespace: str) -> List[dict]:
    """同 web_demo.k8s_get_services：只回 LoadBalancer（會綁客戶電腦 host port 的那種）。"""
    out = []
    for s in rep.get("services") or []:
        if s.get("type") != "LoadBalancer":
            continue
        for port in s.get("ports") or []:
            out.append({"name": s["name"], "app": s.get("app") or "", "port": port, "namespace": namespace})
    return out


def node_capacity(rep: Dict[str, Any], parse_memory: Callable[[str], Optional[int]]) -> Optional[dict]:
    """同 web_demo.k8s_get_node_capacity：可配置記憶體最大的那個節點。"""
    nodes = rep.get("nodes") or []
    if not nodes:
        return None
    best = max(nodes, key=lambda n: parse_memory(n.get("memory") or "0") or 0)
    return {"cpu": best.get("cpu") or "0", "memory": best.get("memory") or "0Ki"}


# ── 詳情 ──────────────────────────────────────────────────────────

def _resolve(rep: Dict[str, Any], name: str) -> List[dict]:
    """同 web_demo._resolve_pods：完整名稱 → app label → 名稱前綴。"""
    pods = rep.get("pods") or []
    exact = [p for p in pods if p["name"] == name]
    if exact:
        return exact
    by_app = [p for p in pods if p.get("app") == name]
    if by_app:
        return by_app
    return [p for p in pods if name and p["name"].startswith(name)]


def _pod_detail(rep: Dict[str, Any], p: dict) -> dict:
    """同 web_demo._pod_detail 的輸出與健康判定。"""
    specs = {c["name"]: c for c in p.get("spec_containers") or []}
    containers, unhealthy_reason = [], None
    for c in p.get("containers") or []:
        state = c.get("state") or "unknown"
        reason, last_reason = c.get("reason") or "", c.get("last_reason") or ""
        ready, restarts = bool(c.get("ready")), int(c.get("restart_count") or 0)
        ok_now = state == "running" and ready
        note = ""
        if not ok_now and (reason in BAD_WAITING or reason in BAD_TERMINATED):
            unhealthy_reason = f"{c['name']}: {reason}"
        elif not ok_now and last_reason in BAD_TERMINATED:
            unhealthy_reason = f"{c['name']}: 上次因 {last_reason} 終止，尚未恢復"
        elif ok_now and restarts >= 5:
            note = f"{c['name']} 目前正常但曾重啟 {restarts} 次" + (f"（上次 {last_reason}）" if last_reason else "")
        sp = specs.get(c["name"], {})
        containers.append({"name": c["name"], "image": sp.get("image") or c.get("image", ""), "ready": ready,
                           "restart_count": restarts, "note": note, "state": state, "reason": reason,
                           "message": (c.get("message") or "")[:200], "last_reason": last_reason,
                           "requests": sp.get("requests", {}), "limits": sp.get("limits", {})})
    phase = p.get("phase") or "Unknown"
    all_ready = bool(containers) and all(x["ready"] for x in containers)
    healthy = phase in ("Running", "Succeeded") and all_ready and unhealthy_reason is None
    notes = [x["note"] for x in containers if x["note"]]
    if healthy:
        summary = f"Running，{len(containers)} 個容器都 ready" + (("（" + "；".join(notes) + "）") if notes else "")
    elif unhealthy_reason:
        summary = unhealthy_reason
    else:
        summary = f"phase={phase}" + ("" if all_ready else "，容器尚未全部 ready")
    evs = [e for e in rep.get("events") or [] if e.get("object") == p["name"]]
    evs.sort(key=lambda e: _sort_key(e.get("last")), reverse=True)
    return {
        "name": p["name"], "app": p.get("app") or "", "phase": phase, "node": p.get("node") or "",
        "ip": p.get("ip") or "", "age": fmt_ts(p.get("created")),
        "restarts": sum(x["restart_count"] for x in containers), "containers": containers,
        "events": [{"type": e.get("type"), "reason": e.get("reason"), "message": (e.get("message") or "")[:200],
                    "count": e.get("count", 1)} for e in evs[:5]],
        "healthy": healthy, "health_summary": summary,
        "logs": p.get("logs") or "",  # 客戶叢集只有回報器抓的最後 50 行（不健康 Pod 才有）
    }


def describe_pods(rep: Dict[str, Any], name: str) -> List[dict]:
    """同 web_demo.k8s_describe_pod。"""
    return [_pod_detail(rep, p) for p in _resolve(rep, name)]


def describe_deployment(rep: Dict[str, Any], name: str) -> Optional[dict]:
    """同 web_demo.k8s_describe_deployment。"""
    if not name:
        return None
    deps = [d for d in rep.get("deployments") or [] if d["name"] != "zt-reporter"]
    d = next((x for x in deps if x["name"] == name), None) or \
        next((x for x in deps if x["name"].startswith(name)), None)
    if not d:
        return None
    replicas, ready, available = d.get("replicas", 0), d.get("ready", 0), d.get("available", 0)
    conds = []
    for c in d.get("conditions") or []:  # 回報器格式："Type=Status: reason"
        head, _, reason = c.partition(": ")
        typ, _, status = head.partition("=")
        conds.append({"type": typ, "status": status, "reason": reason, "message": ""})
    healthy = ready == replicas and replicas > 0
    return {"name": d["name"], "image": d.get("image") or "", "replicas": replicas, "ready": ready,
            "available": available, "updated": available, "unavailable": max(0, replicas - available),
            "age": fmt_ts(d.get("created")), "conditions": conds, "healthy": healthy,
            "health_summary": (f"{ready}/{replicas} ready" if healthy
                               else f"只有 {ready}/{replicas} ready，{max(0, replicas - available)} 個不可用")}


# ── Healer ────────────────────────────────────────────────────────

def unhealthy_issues(rep: Dict[str, Any], namespace: str) -> List[dict]:
    """不健康的 Pod → 給 Healer 掃描/診斷用的 issue（欄位對齊 healer.pod_watcher 的 issue）。"""
    issues = []
    for p in rep.get("pods") or []:
        for c in p.get("containers") or []:
            reason = c.get("reason") or ""
            last = c.get("last_reason") or ""
            bad = reason if (reason in BAD_WAITING or reason in BAD_TERMINATED) else \
                (last if last in BAD_TERMINATED and not (c.get("state") == "running" and c.get("ready")) else "")
            if not bad:
                continue
            issues.append({"pod": p["name"], "pod_name": p["name"], "namespace": namespace,
                           "app": p.get("app") or "", "container": c["name"], "status": bad, "reason": bad,
                           "restart_count": c.get("restart_count", 0), "action": "Needs fix",
                           "logs": p.get("logs") or "",
                           "events": [{"reason": e.get("reason"), "message": e.get("message")}
                                      for e in rep.get("events") or [] if e.get("object") == p["name"]][-5:]})
            break
    return issues


# ── 資源加總 / Dashboard ───────────────────────────────────────────

def _app_requests(rep: Dict[str, Any]) -> Dict[str, dict]:
    """app 名稱 → 第一個容器的 requests（從回報的 Pod 規格取，回報器的 Deployment 摘要沒有 requests）。"""
    out: Dict[str, dict] = {}
    for p in rep.get("pods") or []:
        app = p.get("app")
        specs = p.get("spec_containers") or []
        if app and specs and app not in out:
            out[app] = specs[0].get("requests") or {}
    return out


def requests_sum(rep: Dict[str, Any], parse_cpu, parse_mem) -> tuple:
    """客戶 namespace 內所有 Pod 的 requests 加總（毫核, bytes），平台元件 zt-reporter 不算。
    注意：平台只看得到客戶自己的 namespace，看不到客戶叢集裡其他程式用掉的資源。"""
    cpu = mem = 0
    for p in rep.get("pods") or []:
        if p.get("app") == "zt-reporter" or p.get("phase") in ("Succeeded", "Failed"):
            continue
        for c in p.get("spec_containers") or []:
            cpu += parse_cpu((c.get("requests") or {}).get("cpu")) or 0
            mem += parse_mem((c.get("requests") or {}).get("memory")) or 0
    return cpu, mem


def deployment_breakdown(rep: Dict[str, Any], parse_cpu, parse_mem, estimate_cost) -> dict:
    """同 web_demo._my_deployment_resource_breakdown 的輸出。"""
    reqs = _app_requests(rep)
    deployments, total_cpu, total_mem = [], 0, 0
    for d in list_deployments(rep):
        r = reqs.get(d["name"], {})
        cpu = (parse_cpu(r.get("cpu")) or 0) * d["replicas"]
        mem = (parse_mem(r.get("memory")) or 0) * d["replicas"]
        total_cpu += cpu
        total_mem += mem
        deployments.append({"name": d["name"], "replicas": d["replicas"],
                            "cpu_cores": round(cpu / 1000, 2), "mem_gib": round(mem / (1024 ** 3), 3)})
    if total_cpu == 0 and total_mem == 0:
        monthly = 0.0
    else:
        monthly = estimate_cost({"spec": {"replicas": 1, "template": {"spec": {"containers": [
            {"resources": {"requests": {"cpu": f"{total_cpu}m", "memory": str(total_mem)}}}]}}}}).get("estimated_usd")
    return {"deployments": deployments, "total_cpu_cores": round(total_cpu / 1000, 2),
            "total_mem_gib": round(total_mem / (1024 ** 3), 2), "monthly_usd": monthly}


def pod_usage(rep: Dict[str, Any], pod_name: str, parse_cpu, parse_mem) -> dict:
    """單一 Pod 的實際用量（客戶叢集有裝 metrics-server 才有），格式同 web_demo._pod_real_usage。"""
    m = (rep.get("metrics") or {}).get(pod_name)
    if not m:
        return {"available": False, "cpu_cores": None, "memory_mi": None}
    cpu_mc = parse_cpu(m.get("cpu"))
    mem_b = parse_mem(m.get("memory"))
    return {"available": True,
            "cpu_cores": round(cpu_mc / 1000, 3) if cpu_mc is not None else None,
            "memory_mi": round(mem_b / (1024 ** 2), 1) if mem_b is not None else None}


def light_snapshots(rep: Dict[str, Any], now_iso: str) -> Dict[str, dict]:
    """app 名稱 → Pod 歷史趨勢用的輕量快照（同 web_demo._pod_light_snapshot 欄位）。"""
    out = {}
    for p in rep.get("pods") or []:
        cs = p.get("containers") or []
        out[p.get("app") or p["name"]] = {
            "t": now_iso, "phase": p.get("phase") or "Unknown",
            "restarts": sum(c.get("restart_count", 0) for c in cs),
            "ready": sum(1 for c in cs if c.get("ready")), "total": len(cs)}
    return out
