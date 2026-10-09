"""
ZeroTouch 狀態回報器（跑在客戶的 K8s 叢集裡，namespace = 客戶專屬的 zt-<帳號>-<雜湊>）

做的事只有一件：每 15 秒讀取自己 namespace 的狀態，POST 給 ZeroTouch 平台。
- 只讀：ServiceAccount 只有 get/list 權限（見安裝包裡的 Role），不能建立、修改、刪除任何東西
- 只送出：平台的回應內容完全不讀、不執行。平台要改變任何東西，只能透過 Git commit 讓 Flux 套用
- 只用 Python 標準函式庫，沒有第三方套件；這份原始碼就是實際執行的程式，可以直接檢查

環境變數：ZT_NAMESPACE、ZT_REPORT_URL、ZT_INTERVAL（秒，預設 15）
token：/etc/zt/token（Secret 掛載）
"""
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

AGENT_VERSION = "2"  # 2：補 Pod IP、容器規格、Deployment 時間、Service（平台第 5 階段要用）
API = "https://kubernetes.default.svc"
SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
NS = os.environ.get("ZT_NAMESPACE", "")
REPORT_URL = os.environ.get("ZT_REPORT_URL", "")
INTERVAL = max(5, int(os.environ.get("ZT_INTERVAL", "15")))
BAD_REASONS = {"CrashLoopBackOff", "OOMKilled", "ImagePullBackOff", "ErrImagePull", "Error",
               "CreateContainerConfigError", "RunContainerError"}


def _ctx():
    return ssl.create_default_context(cafile=os.path.join(SA_DIR, "ca.crt"))


def kget(path, errors, raw=False):
    """讀 K8s API。沒權限或不存在回 None 並記下原因，不中斷整份回報。"""
    try:
        with open(os.path.join(SA_DIR, "token")) as f:  # token 會自動輪換，每次重讀
            sa_token = f.read().strip()
        req = urllib.request.Request(API + path, headers={"Authorization": "Bearer " + sa_token})
        with urllib.request.urlopen(req, context=_ctx(), timeout=10) as r:
            body = r.read()
        return body.decode("utf-8", "replace") if raw else json.loads(body)
    except urllib.error.HTTPError as e:
        if e.code != 404:
            errors.append(f"GET {path.split('?')[0]} -> HTTP {e.code}")
    except Exception as e:  # 網路、逾時
        errors.append(f"GET {path.split('?')[0]} -> {type(e).__name__}")
    return None


def _ready(obj):
    for c in ((obj or {}).get("status") or {}).get("conditions") or []:
        if c.get("type") == "Ready":
            return {"ready": c.get("status") == "True", "reason": c.get("reason"),
                    "message": c.get("message"),
                    "revision": ((obj or {}).get("status") or {}).get("lastAppliedRevision")
                    or (((obj or {}).get("status") or {}).get("artifact") or {}).get("revision")}
    return {"ready": False, "reason": "Unknown", "message": "no Ready condition yet"}


def collect():
    errors = []
    ns = urllib.parse.quote(NS)
    rep = {"agent_version": AGENT_VERSION, "namespace": NS, "errors": errors,
           "deployments": [], "pods": [], "events": [], "nodes": [], "metrics": None, "services": []}

    src = kget(f"/apis/source.toolkit.fluxcd.io/v1/namespaces/{ns}/gitrepositories/zt-apps", errors)
    ks = kget(f"/apis/kustomize.toolkit.fluxcd.io/v1/namespaces/{ns}/kustomizations/zt-apps", errors)
    rep["flux"] = {"source": _ready(src), "kustomization": _ready(ks)}
    rep["platform_configmap"] = kget(f"/api/v1/namespaces/{ns}/configmaps/zerotouch-info", errors) is not None

    for d in (kget(f"/apis/apps/v1/namespaces/{ns}/deployments", errors) or {}).get("items", []):
        spec, st = d.get("spec", {}), d.get("status", {})
        c0 = ((spec.get("template") or {}).get("spec") or {}).get("containers") or [{}]
        times = [d["metadata"].get("creationTimestamp")] + [
            c.get("lastUpdateTime") or c.get("lastTransitionTime") for c in st.get("conditions") or []]
        rep["deployments"].append({
            "name": d["metadata"]["name"], "replicas": spec.get("replicas") or 0,
            "created": d["metadata"].get("creationTimestamp"),
            "updated": max([t for t in times if t] or [None]),
            "ready": st.get("readyReplicas") or 0, "available": st.get("availableReplicas") or 0,
            "image": c0[0].get("image"),
            "memory_limit": ((c0[0].get("resources") or {}).get("limits") or {}).get("memory"),
            "conditions": [f"{c.get('type')}={c.get('status')}: {c.get('reason') or ''}"
                           for c in st.get("conditions") or []]})

    for p in (kget(f"/api/v1/namespaces/{ns}/pods", errors) or {}).get("items", []):
        meta, st = p["metadata"], p.get("status", {})
        containers, unhealthy = [], None
        for cs in st.get("containerStatuses") or []:
            state = cs.get("state") or {}
            kind = next(iter(state), "")
            cur = state.get(kind) or {}
            last = ((cs.get("lastState") or {}).get("terminated") or {})
            item = {"name": cs.get("name"), "ready": cs.get("ready"),
                    "restart_count": cs.get("restartCount", 0), "state": kind,
                    "reason": cur.get("reason"), "last_reason": last.get("reason"),
                    "message": cur.get("message") or last.get("message"), "image": cs.get("image")}
            containers.append(item)
            if item["reason"] in BAD_REASONS or item["last_reason"] in BAD_REASONS:
                unhealthy = item
        logs = ""
        if unhealthy:  # 只抓不健康 Pod 的最後 50 行，給平台的 Healer 診斷用
            q = urllib.parse.urlencode({"container": unhealthy["name"], "tailLines": 50,
                                        "previous": "true" if unhealthy["restart_count"] else "false"})
            logs = kget(f"/api/v1/namespaces/{ns}/pods/{urllib.parse.quote(meta['name'])}/log?{q}",
                        errors, raw=True) or ""
        spec_containers = [{
            "name": c.get("name"), "image": c.get("image"),
            "ports": [pt.get("containerPort") for pt in c.get("ports") or [] if pt.get("containerPort")],
            "requests": (c.get("resources") or {}).get("requests") or {},
            "limits": (c.get("resources") or {}).get("limits") or {}}
            for c in (p.get("spec") or {}).get("containers") or []]
        rep["pods"].append({"name": meta["name"], "app": (meta.get("labels") or {}).get("app"),
                            "ip": st.get("podIP"), "spec_containers": spec_containers,
                            "phase": st.get("phase"), "node": (p.get("spec") or {}).get("nodeName"),
                            "created": meta.get("creationTimestamp"), "containers": containers,
                            "logs": logs[-8000:]})

    for s in (kget(f"/api/v1/namespaces/{ns}/services", errors) or {}).get("items", []):
        spec = s.get("spec") or {}
        rep["services"].append({"name": s["metadata"]["name"], "type": spec.get("type"),
                                "app": (spec.get("selector") or {}).get("app"),
                                "ports": [pt.get("port") for pt in spec.get("ports") or [] if pt.get("port")]})

    evs = (kget(f"/api/v1/namespaces/{ns}/events?limit=200", errors) or {}).get("items", [])
    for e in evs[-200:]:
        rep["events"].append({"object": (e.get("involvedObject") or {}).get("name"), "type": e.get("type"),
                              "reason": e.get("reason"), "message": e.get("message"),
                              "count": e.get("count") or 1,
                              "last": e.get("lastTimestamp") or e.get("eventTime")})

    for n in (kget("/api/v1/nodes", errors) or {}).get("items", []):
        alloc = (n.get("status") or {}).get("allocatable") or {}
        rep["nodes"].append({"name": n["metadata"]["name"], "cpu": alloc.get("cpu"),
                             "memory": alloc.get("memory")})

    m = kget(f"/apis/metrics.k8s.io/v1beta1/namespaces/{ns}/pods", [])  # 沒裝 metrics-server 是正常的
    if m:
        rep["metrics"] = {}
        for item in m.get("items", []):
            cs = item.get("containers") or [{}]
            rep["metrics"][item["metadata"]["name"]] = (cs[0].get("usage") or {})
    return rep


def send(rep, token):
    body = json.dumps(rep).encode("utf-8")
    req = urllib.request.Request(REPORT_URL, data=body, method="POST", headers={
        "Content-Type": "application/json", "Authorization": "Bearer " + token,
        "User-Agent": f"zerotouch-reporter/{AGENT_VERSION}"})
    with urllib.request.urlopen(req, timeout=15) as r:
        r.read(1024)  # 讀掉回應但完全不使用（平台不能透過回報器下指令）
        return r.status


def main():
    if not NS or not REPORT_URL:
        print("ZT_NAMESPACE / ZT_REPORT_URL not set", file=sys.stderr)
        sys.exit(2)
    with open("/etc/zt/token") as f:
        token = f.read().strip()
    backoff = INTERVAL
    while True:
        try:
            status = send(collect(), token)
            print(f"report sent: HTTP {status}", flush=True)
            backoff = INTERVAL
        except urllib.error.HTTPError as e:
            # 401 = token 被撤銷（例如帳號重新安裝或被停用），放慢頻率，不要一直打
            print(f"report rejected: HTTP {e.code}", flush=True)
            backoff = 300 if e.code in (401, 403) else min(backoff * 2, 120)
        except Exception as e:
            print(f"report failed: {type(e).__name__}: {e}", flush=True)
            backoff = min(backoff * 2, 120)
        time.sleep(backoff)


if __name__ == "__main__":
    main()
