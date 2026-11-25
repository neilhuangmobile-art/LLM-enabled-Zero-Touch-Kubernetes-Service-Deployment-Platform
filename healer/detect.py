"""
healer/detect.py
Healer 補強方案階段 2（docs/healer_plan_20261120.md，2026-11-25）：擴大偵測。

原本 Healer 只看容器的 waiting／terminated 狀態（healer/pod_watcher._check_pod），下面這些新手常見的
問題完全偵測不到。這裡只「通知＋給建議」，不改任何設定；全部用固定規則判斷，不呼叫模型。

| code           | 偵測方式 |
|----------------|----------|
| unschedulable  | Pod Pending，PodScheduled 條件的訊息含 Insufficient（資源不夠） |
| pvc_unbound    | PVC Pending 超過 2 分鐘，StorageClass 不存在或是 Immediate（WaitForFirstConsumer 等 Pod 排上去才綁，不是錯誤） |
| quota_exceeded | ReplicaSet 建不出 Pod：FailedCreate 事件含 exceeded quota |
| not_ready      | 容器在跑但 Ready=false 超過 15 分鐘（kubernetes-mixin KubePodNotReady 的標準） |
| no_endpoints   | Service 有 selector 但對不到任何 Pod，或 targetPort 對不上容器的 port |
| rollout_stuck  | Deployment 條件 Progressing=False、reason ProgressDeadlineExceeded |
| mount_failed   | ContainerCreating 超過 5 分鐘＋FailedMount 事件 |
| init_failed    | initContainer 失敗（Error、CrashLoopBackOff） |
| evicted        | Pod 被驅逐（status.reason == Evicted） |

開關：HEALER_DETECT=0 關閉。not_ready 的門檻可用 HEALER_NOT_READY_MIN 調（評估用）。
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Callable, Dict, Iterable, List, Optional

LABELS = {
    "unschedulable": "資源不夠、排不上去",
    "pvc_unbound": "保存空間沒有綁上",
    "quota_exceeded": "超過資源配額",
    "not_ready": "一直沒有就緒",
    "no_endpoints": "Service 連不到 Pod",
    "rollout_stuck": "更新卡住",
    "mount_failed": "掛載失敗",
    "init_failed": "初始化容器失敗",
    "evicted": "被系統驅逐",
}

PVC_GRACE_S = 120
MOUNT_GRACE_S = 300
SERVICE_GRACE_S = 120


def enabled() -> bool:
    return os.environ.get("HEALER_DETECT", "1").strip().lower() not in ("0", "false", "off", "no")


def not_ready_seconds() -> int:
    try:
        return int(float(os.environ.get("HEALER_NOT_READY_MIN", "15")) * 60)
    except ValueError:
        return 900


# ── 小工具 ─────────────────────────────────────────────────────────
def _age(ts, now: datetime) -> float:
    if not ts:
        return 0.0
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (now - ts).total_seconds()


def _cut(text, n=300) -> str:
    return " ".join(str(text or "").split())[:n]


def _meta(o):
    return getattr(o, "metadata", None)


def _owner_app(pod, rs_owner: Dict[tuple, str]) -> str:
    """Pod → ReplicaSet → Deployment（用 ReplicaSet 清單查，不從名稱猜）。查不到用 app label 或 Pod 名稱。"""
    m = _meta(pod)
    for ref in getattr(m, "owner_references", None) or []:
        if ref.kind == "ReplicaSet":
            dep = rs_owner.get((m.namespace, ref.name))
            if dep:
                return dep
        elif ref.kind in ("StatefulSet", "DaemonSet", "Job", "CronJob"):
            return ref.name
    return ((getattr(m, "labels", None) or {}).get("app")) or m.name


def _finding(code, ns, app, obj, root_cause, suggestion, evidence) -> Dict:
    return {"code": code, "namespace": ns, "app": app, "object": obj, "label": LABELS[code],
            "root_cause": root_cause, "suggestion": suggestion, "evidence": _cut(evidence)}


def _cond(obj, typ):
    for c in getattr(getattr(obj, "status", None), "conditions", None) or []:
        if c.type == typ:
            return c
    return None


# ── 規則 ───────────────────────────────────────────────────────────
def _pod_findings(pod, app: str, warn_events: Dict[tuple, List], now: datetime) -> List[Dict]:
    m, st = pod.metadata, pod.status
    ns, out = m.namespace, []
    if getattr(st, "reason", None) == "Evicted":
        out.append(_finding("evicted", ns, app, m.name, "Pod 被系統驅逐（通常是節點記憶體或磁碟不夠）",
                            "被驅逐的 Pod 不會自己消失，可以刪掉；如果常發生，請降低服務的記憶體用量或資源設定。",
                            getattr(st, "message", "") or "Evicted"))
        return out
    phase = getattr(st, "phase", None)
    sched = _cond(pod, "PodScheduled")
    if phase == "Pending" and sched is not None and sched.status == "False":
        msg = sched.message or ""
        if "Insufficient" in msg:
            out.append(_finding("unschedulable", ns, app, m.name, "叢集剩下的 CPU 或記憶體不夠，Pod 排不上去",
                                "把這個服務的 CPU／記憶體需求（requests）調小，或減少副本數、刪掉不用的服務。單一節點的叢集加機器通常不實際。",
                                msg))
        elif "unbound" in msg and "PersistentVolumeClaim" in msg:
            out.append(_finding("pvc_unbound", ns, app, m.name, "Pod 要用的保存空間（PVC）還沒綁上，所以排不上去",
                                "確認 PVC 的 StorageClass 存在；這個叢集沒有對應的儲存類別時，請改用預設的 StorageClass。",
                                msg))
    for cs in getattr(st, "init_container_statuses", None) or []:
        w = getattr(cs.state, "waiting", None)
        t = getattr(cs.state, "terminated", None)
        bad = (w and w.reason in ("CrashLoopBackOff", "Error", "RunContainerError")) or \
              (t and t.exit_code not in (0, None) and (cs.restart_count or 0) > 0)
        if bad:
            out.append(_finding("init_failed", ns, app, m.name, f"初始化容器「{cs.name}」失敗，主程式還沒開始跑",
                                f"看初始化容器「{cs.name}」的 log；常見原因是它在等的服務還沒好、或指令寫錯。",
                                (w and (w.message or w.reason)) or (t and f"exit {t.exit_code} {t.reason or ''}") or ""))
            break
    statuses = getattr(st, "container_statuses", None) or []
    creating = [cs for cs in statuses if getattr(cs.state, "waiting", None) and cs.state.waiting.reason == "ContainerCreating"]
    if phase == "Pending" and creating and _age(m.creation_timestamp, now) >= MOUNT_GRACE_S:
        mounts = [e for e in warn_events.get((ns, "Pod", m.name), []) if e.reason in ("FailedMount", "FailedAttachVolume")]
        if mounts:
            out.append(_finding("mount_failed", ns, app, m.name, "要掛進容器的東西（ConfigMap、Secret 或保存空間）找不到或掛不上",
                                "確認部署時有一起建立這些 ConfigMap、Secret 或 PVC，名稱有沒有打錯。",
                                mounts[-1].message))
    ready = _cond(pod, "Ready")
    if phase == "Running" and ready is not None and ready.status == "False" and statuses \
            and all(getattr(cs.state, "running", None) for cs in statuses) \
            and _age(ready.last_transition_time, now) >= not_ready_seconds():
        out.append(_finding("not_ready", ns, app, m.name, "容器在跑，但就緒檢查（readiness）一直沒通過，所以不會收到流量",
                            "readiness 探針的 port 或路徑可能設錯；請確認程式真的在那個 port、那個路徑回應。",
                            ready.message or ready.reason or "Ready=False"))
    return out


def _deployment_findings(d) -> List[Dict]:
    c = _cond(d, "Progressing")
    if c is not None and c.status == "False" and c.reason == "ProgressDeadlineExceeded":
        m = d.metadata
        return [_finding("rollout_stuck", m.namespace, m.name, m.name, "新版本一直起不來，更新卡住了（舊版本可能還在跑）",
                         "看新 Pod 的狀態和事件找原因；如果是剛才的更新造成的，可以在「我的服務」退回上一版。",
                         c.message or c.reason)]
    return []


ANN_ORIG_SELECTOR = "zerotouch.k8s/original-selector"   # 休眠中（core/sleep_mode.py），Service 暫時接到喚醒器


def _service_findings(svc, pods_by_ns: Dict[str, list], slices_by_svc: Dict[tuple, list], now: datetime,
                      zero_selectors: Dict[str, list] = None) -> List[Dict]:
    m, spec = svc.metadata, svc.spec
    sel = getattr(spec, "selector", None) or {}
    if not sel or getattr(spec, "type", None) == "ExternalName" or _age(m.creation_timestamp, now) < SERVICE_GRACE_S:
        return []
    if ANN_ORIG_SELECTOR in (m.annotations or {}):
        return []   # 休眠中：Service 接的是喚醒器，由休眠模組管理
    ns = m.namespace
    matched = [p for p in pods_by_ns.get(ns, [])
               if all(((p.metadata.labels or {}).get(k) == v) for k, v in sel.items())
               and getattr(p.status, "phase", None) not in ("Succeeded", "Failed")]
    if not matched and any(all(lbl.get(k) == v for k, v in sel.items()) for lbl in (zero_selectors or {}).get(ns, [])):
        return []   # 對應的服務副本數是 0（使用者自己關掉或休眠），沒有 Pod 是正常的
    if not matched:
        labels = sorted({f"{k}={v}" for p in pods_by_ns.get(ns, []) for k, v in (p.metadata.labels or {}).items()
                         if k in sel})
        hint = ("；同一個 namespace 裡的 Pod 有的是 " + "、".join(labels[:5])) if labels else "；同一個 namespace 裡沒有任何 Pod 有這些 label"
        want = "、".join(f"{k}={v}" for k, v in sel.items())
        return [_finding("no_endpoints", ns, m.name, m.name, f"Service「{m.name}」找不到對應的 Pod（selector 對不上）",
                         f"Service 要找 label {want} 的 Pod{hint}。請把 Service 的 selector 改成跟 Pod 的 label 一樣。",
                         f"selector {want}，符合的 Pod 0 個")]
    # targetPort 對不上：只在 Pod 有宣告 containerPort 時判斷（沒宣告的話，程式還是可能在那個 port 上）
    declared = {pt.container_port for p in matched for c in (p.spec.containers or []) for pt in (c.ports or [])}
    named = {pt.name for p in matched for c in (p.spec.containers or []) for pt in (c.ports or []) if pt.name}
    bad = []
    for sp in getattr(spec, "ports", None) or []:
        tp = sp.target_port if sp.target_port is not None else sp.port
        if isinstance(tp, str) and not tp.isdigit():
            if tp not in named:
                bad.append(f"targetPort「{tp}」在容器裡找不到同名的 port")
        elif declared and int(tp) not in declared:
            bad.append(f"targetPort {tp} 不是容器的 port（容器是 {'、'.join(str(x) for x in sorted(declared))}）")
    if bad:
        ready_eps = sum(1 for s in slices_by_svc.get((ns, m.name), []) for e in (s.endpoints or [])
                        if getattr(getattr(e, "conditions", None), "ready", False))
        return [_finding("no_endpoints", ns, m.name, m.name, f"Service「{m.name}」的 port 對不上容器，連進來會失敗",
                         "把 Service 的 targetPort 改成容器實際監聽的 port。",
                         "；".join(bad) + f"（就緒端點 {ready_eps} 個）")]
    return []


def _quota_findings(warn_events: Dict[tuple, List], rs_owner: Dict[tuple, str]) -> List[Dict]:
    out, seen = [], set()
    for (ns, kind, name), evs in warn_events.items():
        if kind != "ReplicaSet":
            continue
        hit = [e for e in evs if e.reason == "FailedCreate" and "exceeded quota" in (e.message or "")]
        if hit:
            app = rs_owner.get((ns, name)) or name
            if (ns, app) in seen:
                continue
            seen.add((ns, app))
            out.append(_finding("quota_exceeded", ns, app, name, "這個帳號的資源配額用完了，新的 Pod 建不出來",
                                "刪掉不用的服務，或把服務的 CPU／記憶體調小。", hit[-1].message))
    return out


def _pvc_findings(pvcs: Iterable, classes: Dict[str, str], default_class: Optional[str], now: datetime) -> List[Dict]:
    out = []
    for pvc in pvcs:
        m = pvc.metadata
        if getattr(pvc.status, "phase", None) != "Pending" or _age(m.creation_timestamp, now) < PVC_GRACE_S:
            continue
        sc = getattr(pvc.spec, "storage_class_name", None) or default_class
        if sc and sc in classes and classes[sc] == "WaitForFirstConsumer":
            continue   # 等第一個使用它的 Pod 排上去才綁，不是錯誤
        why = f"StorageClass「{sc}」不存在" if sc and sc not in classes else \
            ("沒有指定 StorageClass，叢集也沒有預設的" if not sc else f"StorageClass「{sc}」一直沒有建出空間")
        out.append(_finding("pvc_unbound", m.namespace, m.name, m.name, f"保存空間「{m.name}」一直沒有綁上：{why}",
                            "改用這個叢集有的 StorageClass（通常不指定就會用預設的）。", why))
    return out


# ── 對外 ───────────────────────────────────────────────────────────
def analyze(pods, events, deployments, services, slices, pvcs, storage_classes, replica_sets,
            owns_ns: Callable[[str], bool] = lambda ns: True, now: Optional[datetime] = None) -> List[Dict]:
    """純函式（測試用假物件即可）。回傳 finding 清單，同一個 (namespace, app, code) 只留一筆。"""
    now = now or datetime.now(timezone.utc)
    rs_owner = {}
    for rs in replica_sets or []:
        for ref in rs.metadata.owner_references or []:
            if ref.kind == "Deployment":
                rs_owner[(rs.metadata.namespace, rs.metadata.name)] = ref.name
    warn = {}
    for e in events or []:
        if getattr(e, "type", None) != "Warning":
            continue
        io = e.involved_object
        warn.setdefault((io.namespace, io.kind, io.name), []).append(e)
    all_pods = [p for p in (pods or []) if owns_ns(p.metadata.namespace)]
    pods = [p for p in all_pods if "zerotouch/waker" not in (p.metadata.labels or {})]
    pods_by_ns = {}
    for p in all_pods:   # Service 比對包含喚醒器（休眠中的 Service 接的是它）
        pods_by_ns.setdefault(p.metadata.namespace, []).append(p)
    zero_selectors = {}
    for d in deployments or []:
        if (getattr(d.spec, "replicas", None) or 0) == 0:
            tmpl = getattr(getattr(d.spec, "template", None), "metadata", None)
            zero_selectors.setdefault(d.metadata.namespace, []).append((getattr(tmpl, "labels", None) or {}))
    slices_by_svc = {}
    for s in slices or []:
        svc = (s.metadata.labels or {}).get("kubernetes.io/service-name")
        if svc:
            slices_by_svc.setdefault((s.metadata.namespace, svc), []).append(s)
    classes = {sc.metadata.name: (getattr(sc, "volume_binding_mode", None) or "Immediate") for sc in storage_classes or []}
    default_class = next((sc.metadata.name for sc in storage_classes or []
                          if (sc.metadata.annotations or {}).get("storageclass.kubernetes.io/is-default-class") == "true"), None)

    out: List[Dict] = []
    for p in pods:
        out += _pod_findings(p, _owner_app(p, rs_owner), warn, now)
    for d in deployments or []:
        if owns_ns(d.metadata.namespace) and "zerotouch/waker" not in (d.metadata.labels or {}):
            out += _deployment_findings(d)
    for s in services or []:
        if owns_ns(s.metadata.namespace):
            out += _service_findings(s, pods_by_ns, slices_by_svc, now, zero_selectors)
    out += [f for f in _quota_findings(warn, rs_owner) if owns_ns(f["namespace"])]
    # 同一個 namespace 已經從 Pod 那邊報過「保存空間沒綁上」（有服務名稱），PVC 本身就不再重複報
    pvc_ns = {f["namespace"] for f in out if f["code"] == "pvc_unbound"}
    out += [f for f in _pvc_findings(pvcs or [], classes, default_class, now)
            if owns_ns(f["namespace"]) and f["namespace"] not in pvc_ns]
    seen, uniq = set(), []
    for f in out:
        k = (f["namespace"], f["app"], f["code"])
        if k not in seen:
            seen.add(k)
            uniq.append(f)
    return uniq


def scan_extra(owns_ns: Callable[[str], bool] = lambda ns: True, namespace: str = "") -> List[Dict]:
    """查叢集並回傳 finding。namespace 給了就只查那個（評估程式用）。"""
    from kubernetes import client
    core, apps = client.CoreV1Api(), client.AppsV1Api()

    def _all(fn_all, fn_ns, **kw):
        try:
            return (fn_ns(namespace, **kw) if namespace else fn_all(**kw)).items
        except Exception:  # noqa: BLE001 — 某一種資源查不到不影響其他偵測
            return []

    try:
        slices_api = client.DiscoveryV1Api()
        slices = _all(slices_api.list_endpoint_slice_for_all_namespaces, slices_api.list_namespaced_endpoint_slice)
    except Exception:  # noqa: BLE001
        slices = []
    try:
        storage_classes = client.StorageV1Api().list_storage_class().items
    except Exception:  # noqa: BLE001
        storage_classes = []
    return analyze(
        pods=_all(core.list_pod_for_all_namespaces, core.list_namespaced_pod),
        events=_all(core.list_event_for_all_namespaces, core.list_namespaced_event, field_selector="type=Warning"),
        deployments=_all(apps.list_deployment_for_all_namespaces, apps.list_namespaced_deployment),
        services=_all(core.list_service_for_all_namespaces, core.list_namespaced_service),
        slices=slices,
        pvcs=_all(core.list_persistent_volume_claim_for_all_namespaces, core.list_namespaced_persistent_volume_claim),
        storage_classes=storage_classes,
        replica_sets=_all(apps.list_replica_set_for_all_namespaces, apps.list_namespaced_replica_set),
        owns_ns=owns_ns,
    )
