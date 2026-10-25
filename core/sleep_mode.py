"""
休眠模式（2026-10-11）：服務太久沒人用就把副本數降到 0，有人連進來時自動叫醒。

判斷「沒人用」：看 Prometheus 的 container_network_receive_packets_total（每個 Pod 收到的封包數）。
健康檢查本身也會產生流量，所以不能用「0 個封包」當標準。2026-10-11 在學校那台實測閒置的服務：
nginx（HTTP 檢查每 5 秒、每 10 秒各一次）每分鐘 110 到 117 個封包，平均一次 HTTP 檢查約 6.3 個；
redis（連線檢查每 10 秒一次）每分鐘 17 到 20 個，一次約 3 個；從叢集內打 200 次請求約 1,550 個封包。
判斷方式：
- 基準用每個 Pod 自己在期間內「每小時封包數的中位數」，不用估算值（估算值差 5% 在 nginx 就是每小時
  350 個封包、約 45 次請求，太粗）。健康檢查的頻率固定，每小時的數字非常穩定。
- 期間內任何一小時超過「中位數 × 1.02 + 60 個封包（約 8 次請求）」就算有人用。
- 中位數本身明顯高於健康檢查的估算值（× 1.5 + 60），代表一直有穩定的流量（例如有監控程式定期連），也算有人用。
估算值偏低時結果是「不休眠」，不會把有人在用的服務關掉；就算誤判休眠，有人連線也會自動喚醒。

自動喚醒（不另外安裝 KEDA）：休眠時把服務的 Service 改接到一個很小的「喚醒器」容器，
有人連線時它回一頁「正在喚醒」並在 log 印一行 ZT-WAKE；平台的背景迴圈看到這行就把副本數
恢復、等就緒後把 Service 接回去、刪掉喚醒器。喚醒器不需要連回平台，也沒有叢集權限。
"""

import json
import re
from typing import Dict, List, Optional, Tuple

# Deployment／Service 上的標記
ANN_SLEEP_REPLICAS = "zerotouch.k8s/sleep-replicas"     # 休眠前開幾個（有這個就代表正在休眠）
ANN_SLEEPING_SINCE = "zerotouch.k8s/sleeping-since"
ANN_ACTIVE_SINCE = "zerotouch.k8s/active-since"         # 最後一次被喚醒的時間，閒置從這之後才開始算
ANN_WAKING = "zerotouch.k8s/waking-since"
ANN_ORIG_SELECTOR = "zerotouch.k8s/original-selector"   # Service 原本的 selector（JSON）
LABEL_WAKER = "zerotouch/waker"

WAKER_IMAGE = "python:3.12-alpine"
WAKE_MARK = "ZT-WAKE"

# 實測值（見檔頭），估背景流量用
PKTS_PER_HTTP_PROBE = 6.5
PKTS_PER_TCP_PROBE = 3.5
MARGIN_PER_HOUR = 60


def waker_name(app: str) -> str:
    return ("zt-waker-" + app)[:63].rstrip("-")


def pod_belongs_to(app: str, pod_name: str) -> bool:
    """Deployment 建的 Pod 叫 <app>-<ReplicaSet 雜湊>-<5 碼>；web 跟 web-api 不會互相算到。"""
    if not pod_name.startswith(app + "-"):
        return False
    return bool(re.fullmatch(r"[a-z0-9]{5,10}-[a-z0-9]{5}", pod_name[len(app) + 1:]))


def expected_probe_packets_per_hour(probes: List[Dict]) -> float:
    """probes：[{"kind": "http"|"tcp"|"exec"|"grpc", "period": 秒}]，每個 Pod 每小時的背景封包數。"""
    total = 0.0
    for p in probes or []:
        period = max(1, int(p.get("period") or 10))
        per = {"http": PKTS_PER_HTTP_PROBE, "grpc": PKTS_PER_HTTP_PROBE, "tcp": PKTS_PER_TCP_PROBE}.get(p.get("kind"), 0)
        total += 3600 / period * per
    return total


def probes_of_container(container) -> List[Dict]:
    """從 kubernetes client 的 V1Container 讀出健康檢查的種類與頻率（readiness、liveness、startup 都算）。"""
    out = []
    for attr in ("readiness_probe", "liveness_probe", "startup_probe"):
        pr = getattr(container, attr, None)
        if not pr:
            continue
        kind = "http" if pr.http_get else "tcp" if pr.tcp_socket else "grpc" if getattr(pr, "grpc", None) else "exec"
        out.append({"kind": kind, "period": pr.period_seconds or 10})
    return out


def _margin(bucket_seconds: int) -> float:
    return max(8.0, MARGIN_PER_HOUR * bucket_seconds / 3600)


def busy_limit(typical: float, bucket_seconds: int) -> float:
    """某一格超過這個數字就算有人用。一小時一格有 120 個取樣點，數字很穩（誤差約 1%）；
    測試用的一分鐘一格只有 2 個取樣點，2026-10-11 實測閒置的 nginx 每分鐘在 106 到 121 之間跳動，要放寬。"""
    return typical * (1.02 if bucket_seconds >= 3600 else 1.25) + _margin(bucket_seconds)


def steady_limit(expected_per_hour: float, bucket_seconds: int) -> float:
    """平常（中位數）就超過這個數字，代表一直有健康檢查以外的流量。"""
    return expected_per_hour * bucket_seconds / 3600 * 1.5 + _margin(bucket_seconds)


def traffic_queries(namespace: str, window_seconds: int, bucket_seconds: int) -> Dict[str, str]:
    """每個 Pod 在這段期間裡「最忙的那一格」與「平常（中位數）那一格」收到幾個封包。"""
    ns = re.sub(r"[^a-z0-9-]", "", namespace)
    inner = (f'increase(container_network_receive_packets_total{{namespace="{ns}"}}[{bucket_seconds}s])'
             f'[{window_seconds}s:{bucket_seconds}s]')
    return {"busiest": f"max by (pod) (max_over_time({inner}))",
            "typical": f"max by (pod) (quantile_over_time(0.5, {inner}))"}


def bucket_for(window_seconds: int) -> int:
    """5 天這種長期間用 1 小時一格；測試用的幾分鐘期間用 1 分鐘一格。"""
    return 3600 if window_seconds >= 6 * 3600 else 60


def decide_idle(now: float, active_since: float, window_seconds: int, busiest_by_pod: Dict[str, float],
                typical_by_pod: Dict[str, float], expected_per_hour: float, bucket_seconds: int,
                history_since: Optional[float]) -> Tuple[bool, str]:
    """回傳 (要不要休眠, 給使用者看的原因)。任何資料不足的情況都回 False（寧可不休眠）。"""
    if now - active_since < window_seconds:
        return False, "最近有部署、喚醒或更新，還沒滿閒置期間"
    if history_since is None or history_since > now - window_seconds:
        return False, "監控資料還沒累積到閒置期間那麼久"
    pods = [p for p in busiest_by_pod if p in typical_by_pod]
    if not pods:
        return False, "查不到這個服務的流量資料"
    unit = "小時" if bucket_seconds >= 3600 else "分鐘"
    steady = steady_limit(expected_per_hour, bucket_seconds)
    for p in pods:
        busiest, typical = busiest_by_pod[p], typical_by_pod[p]
        if typical > steady:
            return False, f"平常每{unit}就有 {typical:.0f} 個封包，比健康檢查多，表示一直有人在用"
        if busiest > busy_limit(typical, bucket_seconds):
            return False, f"有人在用：最忙的那{unit}收到 {busiest:.0f} 個封包，平常只有 {typical:.0f} 個"
    return True, "期間內只有健康檢查的流量，沒有人使用"


# ── 喚醒器 ──
# 只用標準函式庫。127.0.0.1 的連線（平台自己的檢查）不算；其他任何連線都印一行 ZT-WAKE。
# 看起來是 HTTP 請求就回一頁說明並自動重新整理；其他協定（redis、資料庫）直接關掉連線，
# 用戶端會重試，服務醒來後就連得上。
WAKER_SCRIPT = r'''
import os, socket, threading, sys
PAGE = """<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8"><meta http-equiv="refresh" content="8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>服務正在喚醒</title>
<style>body{font-family:system-ui,"Noto Sans TC",sans-serif;background:#0b0f14;color:#e6edf3;display:grid;place-items:center;
min-height:100vh;margin:0}main{max-width:30rem;padding:2rem;text-align:center}h1{font-size:1.4rem}p{color:#9aa7b4;line-height:1.7}
.dot{width:12px;height:12px;border-radius:50%;background:#3fb950;display:inline-block;animation:p 1.2s infinite}
@keyframes p{50%{opacity:.25}}</style></head><body><main><div class="dot"></div><h1>服務正在喚醒</h1>
<p>這個服務一段時間沒有人使用，平台先讓它休眠以節省資源。你的連線已經把它叫醒，通常 30 秒到 1 分鐘就會恢復，這個頁面會自動重新整理。</p>
</main></body></html>""".encode("utf-8")
RESP = (b"HTTP/1.1 503 Service Unavailable\r\nContent-Type: text/html; charset=utf-8\r\nRetry-After: 10\r\n"
        b"Cache-Control: no-store\r\nConnection: close\r\nContent-Length: " + str(len(PAGE)).encode() + b"\r\n\r\n" + PAGE)
METHODS = (b"GET ", b"POST", b"HEAD", b"PUT ", b"DELE", b"PATC", b"OPTI")
def handle(conn, addr):
    try:
        if addr[0] in ("127.0.0.1", "::1"):
            return
        print("ZT-WAKE", addr[0], flush=True)
        conn.settimeout(3)
        try:
            data = conn.recv(8)
        except Exception:
            data = b""
        if data.startswith(METHODS):
            conn.sendall(RESP)
    finally:
        try: conn.close()
        except Exception: pass
def serve(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", port)); s.listen(64)
    while True:
        c, a = s.accept()
        threading.Thread(target=handle, args=(c, a), daemon=True).start()
ports = [int(p) for p in os.environ.get("PORTS", "80").split(",") if p.strip()]
print("waker listening on", ports, flush=True)
for p in ports[1:]:
    threading.Thread(target=serve, args=(p,), daemon=True).start()
serve(ports[0])
'''


def waker_manifest(app: str, namespace: str, ports: List[int]) -> Dict:
    """喚醒器 Deployment。最嚴格的安全設定：非 root、沒有任何額外權限、不掛帳號憑證、唯讀檔案系統。
    ip_unprivileged_port_start 是 Kubernetes 認定安全的 sysctl，讓非 root 也能聽 80 這種小號 port。"""
    name = waker_name(app)
    ports = sorted({int(p) for p in ports if 0 < int(p) < 65536})[:8] or [80]
    labels = {LABEL_WAKER: app}
    return {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": name, "namespace": namespace,
                     "labels": {**labels, "app.kubernetes.io/managed-by": "zerotouch"}},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": labels},
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "automountServiceAccountToken": False,
                    "securityContext": {
                        "runAsNonRoot": True, "runAsUser": 65534, "runAsGroup": 65534,
                        "seccompProfile": {"type": "RuntimeDefault"},
                        "sysctls": [{"name": "net.ipv4.ip_unprivileged_port_start", "value": "0"}],
                    },
                    "containers": [{
                        "name": "waker", "image": WAKER_IMAGE,
                        "command": ["python", "-u", "-c", WAKER_SCRIPT],
                        "env": [{"name": "PORTS", "value": ",".join(str(p) for p in ports)}],
                        "ports": [{"containerPort": p} for p in ports],
                        "resources": {"requests": {"cpu": "10m", "memory": "32Mi"},
                                      "limits": {"cpu": "100m", "memory": "64Mi"}},
                        "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                            "capabilities": {"drop": ["ALL"]}},
                    }],
                },
            },
        },
    }


def selector_matches(selector: Optional[Dict], pod_labels: Dict) -> bool:
    return bool(selector) and all(pod_labels.get(k) == v for k, v in selector.items())


def dump_selector(selector: Dict) -> str:
    return json.dumps(selector or {}, sort_keys=True)
