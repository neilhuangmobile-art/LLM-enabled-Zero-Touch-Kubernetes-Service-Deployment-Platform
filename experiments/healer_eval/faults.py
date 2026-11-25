"""
experiments/healer_eval/faults.py
Healer 評估的故障題組（開發題組，2026-11-25，Healer 方案第 6 節）。

每題一個 Deployment（必要時先部署正常版再改壞），放在專用 namespace。欄位：
- id、category、variant、split（dev／blind）
- expected_action：期望的診斷動作代碼（比對代碼，不比文字）
- expected_hands_off：正確答案是「不動手」（程式本身的錯、缺設定、權限、log 假 OOM、對照組…）
- fixable：Healer 動手後應該要修好（判定穩定 120 秒）
- build()：回傳 {"deploy": Deployment dict, "pre_image": 先部署的正常映像檔或 None}

注意：這份是開發題組（調整 Healer 的人出的題）。盲測題要由不調整 Healer 的人另外出，版本凍結後只跑一次。
"""
from __future__ import annotations

from typing import Dict, List, Optional

BUSYBOX = "busybox:1.36"
PY = "python:3.12-alpine"
NGINX = "nginx:1.27-alpine"


def deployment(name: str, image: str, command: Optional[List[str]] = None, replicas: int = 1,
               mem_limit: Optional[str] = "128Mi", mem_request: Optional[str] = "64Mi", env=None,
               liveness=None, readiness=None, port: Optional[int] = None, security=None, cpu_request="20m") -> Dict:
    res = {"requests": {"cpu": cpu_request}}
    if mem_request:
        res["requests"]["memory"] = mem_request
    if mem_limit:
        res["limits"] = {"memory": mem_limit}
    c = {"name": "app", "image": image, "resources": res}
    if command:
        c["command"] = command
    if env:
        c["env"] = env
    if port:
        c["ports"] = [{"containerPort": port}]
    if liveness:
        c["livenessProbe"] = liveness
    if readiness:
        c["readinessProbe"] = readiness
    if security:
        c["securityContext"] = security
    return {"apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": name, "labels": {"app": name}},
            "spec": {"replicas": replicas, "selector": {"matchLabels": {"app": name}},
                     "template": {"metadata": {"labels": {"app": name}},
                                  "spec": {"containers": [c], "terminationGracePeriodSeconds": 2}}}}


def _sh(script: str) -> List[str]:
    return ["sh", "-c", script]


def _http_probe(port: int, delay: int = 3, period: int = 5, failures: int = 2) -> Dict:
    return {"httpGet": {"path": "/", "port": port}, "initialDelaySeconds": delay, "periodSeconds": period,
            "failureThreshold": failures, "timeoutSeconds": 2}


def _case(cid, category, variant, action, hands_off, fixable, deploy, pre_image=None, max_actions=0,
          pre_deploy=None, extra=None, expected_detect=None, expected_remediation=None):
    """max_actions：停手題允許的自動動作次數（方案權限表：多副本的 Deployment 允許重建 Pod 一次）。
    2026-11-25（階段 2～3 擴充）：
    - pre_deploy：先部署這個正常版本、等可用，再把容器設定改成 deploy 的（測「退回最近的更新」）
    - extra：同時建立的其他物件（Service、PVC、ResourceQuota）
    - expected_detect：擴大偵測（healer/detect.py）要報出的 code；有這個欄位時，通過條件是有報出來而且沒有動手
    - expected_remediation：可修題要求實際用的處理方式（例如 revert_recent_change）"""
    return {"id": cid, "category": category, "variant": variant, "split": "dev", "expected_action": action,
            "expected_hands_off": hands_off, "fixable": fixable, "max_actions": max_actions,
            "expected_detect": expected_detect, "expected_remediation": expected_remediation,
            "build": (lambda d=deploy, p=pre_image, pd=pre_deploy, ex=extra:
                      {"deploy": d, "pre_image": p, "pre_deploy": pd, "extra": ex or []})}


def _with_marker_volume(d: Dict) -> Dict:
    spec = d["spec"]["template"]["spec"]
    spec["volumes"] = [{"name": "mark", "emptyDir": {}}]
    spec["containers"][0]["volumeMounts"] = [{"name": "mark", "mountPath": "/mark"}]
    return d


def _alloc_py(mb: int) -> List[str]:
    return ["python", "-c", f"import time; x = bytearray({mb} * 1024 * 1024); print('allocated', flush=True); time.sleep(10**9)"]


CASES: List[Dict] = [
    # 1. 記憶體不足：真的需要比上限多一點，調高一次就會好
    _case("oom-py", "oom", "python 需要 100MB、上限 64Mi", "increase_memory", False, True,
          deployment("oom-py", PY, _alloc_py(100), mem_limit="64Mi", mem_request="64Mi")),
    _case("oom-busybox", "oom", "busybox 吃 90MB、上限 80Mi", "increase_memory", False, True,
          deployment("oom-busybox", BUSYBOX, _sh("head -c 90m /dev/zero | tail; sleep 999999"), mem_limit="80Mi", mem_request="40Mi")),
    # 2. 程式本身的錯：正確答案是停手
    _case("apperr-sh", "app_error", "啟動就 exit 1", "analyze_logs", True, False,
          deployment("apperr-sh", BUSYBOX, _sh("echo 'Traceback: KeyError settings'; exit 1"))),
    _case("apperr-py", "app_error", "python 例外", "analyze_logs", True, False,
          deployment("apperr-py", PY, ["python", "-c", "raise SystemExit('ValueError: bad config value')"])),
    # 3. 映像檔 tag 錯，之前有正常版本：應該退回
    _case("badtag-rollback", "bad_image", "nginx 從正常版改成不存在的 tag", "fix_image", False, True,
          deployment("badtag-rollback", "nginx:9.99.99-doesnotexist", port=80), pre_image=NGINX),
    # 3b. 映像檔 tag 錯，從來沒正常過：只能通知
    _case("badtag-new", "bad_image", "一開始就是不存在的 tag", "fix_image", True, False,
          deployment("badtag-new", "redis:0.0.0-nope")),
    # 4. 私有映像檔要登入：只通知
    _case("private-image", "private_image", "私有倉庫", "fix_image", True, False,
          deployment("private-image", "ghcr.io/zt-eval-nonexistent-org/private-app:1.0")),
    # 5. 探針 port 錯：改成正確 port
    _case("probe-port", "probe_port", "nginx 聽 80、探針打 8081", "fix_probe", False, True,
          deployment("probe-port", NGINX, port=80, liveness=_http_probe(8081), readiness=_http_probe(8081))),
    # 6. 啟動太慢：放寬等待時間
    _case("slow-start", "slow_start", "啟動要 25 秒、探針 3 秒後就檢查", "fix_probe", False, True,
          deployment("slow-start", PY, _sh("sleep 25; python -m http.server 8000"), port=8000,
                     liveness=_http_probe(8000, delay=3, period=5, failures=2))),
    # 7. 缺 ConfigMap：停手
    _case("missing-cm", "missing_config", "引用不存在的 ConfigMap", "create_config", True, False,
          deployment("missing-cm", BUSYBOX, _sh("sleep 999999"),
                     env=[{"name": "MODE", "valueFrom": {"configMapKeyRef": {"name": "app-settings", "key": "mode"}}}])),
    # 8. 缺 Secret：停手
    _case("missing-secret", "missing_secret", "引用不存在的 Secret", "create_config", True, False,
          deployment("missing-secret", BUSYBOX, _sh("sleep 999999"),
                     env=[{"name": "DB_PASSWORD", "valueFrom": {"secretKeyRef": {"name": "db-cred", "key": "password"}}}])),
    # 9. 相依服務連不上：只通知
    _case("dep-down", "dependency", "連不上資料庫就結束", "check_dependencies", True, False,
          deployment("dep-down", BUSYBOX, _sh("echo 'FATAL: could not connect to database at db:5432: connection refused'; exit 1"))),
    # 10. 權限不足：停手
    _case("perm-denied", "permission", "非 root 寫 /root", "fix_permissions", True, False,
          deployment("perm-denied", BUSYBOX, _sh("touch /root/x || { echo 'touch: /root/x: Permission denied'; exit 1; }"),
                     security={"runAsUser": 1000, "runAsNonRoot": True})),
    # 11. 指令找不到：停手
    # 2026-11-25：診斷多了 fix_command（指令找不到／exit 0，只通知），期望動作跟著改
    _case("cmd-notfound", "command_not_found", "指令打錯", "fix_command", True, False,
          deployment("cmd-notfound", BUSYBOX, ["no-such-binary-here"])),
    # 12. 一啟動就正常結束：停手（不是常駐服務）
    _case("exit-zero", "completes", "跑完就 exit 0", "fix_command", True, False,
          deployment("exit-zero", BUSYBOX, _sh("echo 'job finished'; exit 0"))),
    # 13. log 印出 OOMKilled 但沒有真的 OOM：不能調記憶體（測 P3）
    _case("fake-oom-log", "fake_oom_log", "log 寫 OOMKilled 然後 exit 1", "analyze_logs", True, False,
          deployment("fake-oom-log", BUSYBOX, _sh("echo 'OOMKilled: out of memory (simulated by app)'; exit 1"))),
    # 14. 資源不夠 Pending：目前偵測不到（階段 2），只通知
    _case("pending-cpu", "pending", "要求 64 顆 CPU", "manual_inspect", True, False,
          deployment("pending-cpu", BUSYBOX, _sh("sleep 999999"), cpu_request="64")),
    # 多副本的程式錯誤：允許重建一次，但修不好（測不會無限重建）
    _case("apperr-2rep", "app_error", "兩個副本都 exit 1", "analyze_logs", True, False,
          deployment("apperr-2rep", BUSYBOX, _sh("echo 'panic: nil map'; exit 1"), replicas=2), max_actions=1),
    # 對照組：正常服務，Healer 不能動手
    _case("ok-nginx", "normal", "正常 nginx", "none", True, True, deployment("ok-nginx", NGINX, port=80)),
    _case("ok-redis", "normal", "正常 redis", "none", True, True, deployment("ok-redis", "redis:7-alpine", port=6379)),
    _case("ok-http-403", "normal", "會印 HTTP 403 存取紀錄的正常服務", "none", True, True,
          deployment("ok-http-403", BUSYBOX, _sh("while true; do echo '10.0.0.1 - - \"GET /admin HTTP/1.1\" 403 153'; sleep 5; done"))),
    # 暫時性故障：第一次啟動失敗、之後正常（標記檔放在 emptyDir，容器重啟後還在）
    _case("transient", "transient", "第一次 exit 1，之後正常", "none", True, True,
          _with_marker_volume(deployment("transient", BUSYBOX,
                                         _sh("if [ -f /mark/ok ]; then sleep 999999; else touch /mark/ok; exit 1; fi")))),
]


def _service(name: str, selector: Dict, port: int, target) -> Dict:
    return {"apiVersion": "v1", "kind": "Service", "metadata": {"name": name},
            "spec": {"selector": selector, "ports": [{"port": port, "targetPort": target}]}}


def _with_init(d: Dict, script: str) -> Dict:
    d["spec"]["template"]["spec"]["initContainers"] = [
        {"name": "wait-db", "image": BUSYBOX, "command": _sh(script), "resources": {"requests": {"cpu": "10m"}}}]
    return d


def _with_volume(d: Dict, volume: Dict, path: str) -> Dict:
    spec = d["spec"]["template"]["spec"]
    spec["volumes"] = [volume]
    spec["containers"][0]["volumeMounts"] = [{"name": volume["name"], "mountPath": path}]
    return d


def _progress_deadline(d: Dict, seconds: int) -> Dict:
    d["spec"]["progressDeadlineSeconds"] = seconds
    return d


def _readiness_path(path: str) -> Dict:
    return {"httpGet": {"path": path, "port": 80}, "periodSeconds": 5, "failureThreshold": 1, "timeoutSeconds": 2}


# ── 2026-11-25 擴充：階段 2（擴大偵測，只通知）、階段 3（退回最近的更新、startupProbe）、診斷 ──────────────
CASES += [
    _case("pending-mem", "pending", "要求 512Gi 記憶體", "none", True, False,
          deployment("pending-mem", BUSYBOX, _sh("sleep 999999"), mem_limit=None, mem_request="512Gi"),
          expected_detect="unschedulable"),
    _case("pvc-badclass", "pvc_unbound", "PVC 指定不存在的 StorageClass", "none", True, False,
          _with_volume(deployment("pvc-badclass", BUSYBOX, _sh("sleep 999999")),
                       {"name": "data", "persistentVolumeClaim": {"claimName": "data"}}, "/data"),
          extra=[{"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": {"name": "data"},
                  "spec": {"accessModes": ["ReadWriteOnce"], "storageClassName": "no-such-class",
                           "resources": {"requests": {"storage": "1Gi"}}}}],
          expected_detect="pvc_unbound"),
    _case("notready-path", "not_ready", "readiness 打不存在的路徑（404）", "none", True, False,
          deployment("notready-path", NGINX, port=80, readiness=_readiness_path("/healthz-nope")),
          expected_detect="not_ready"),
    _case("svc-selector", "no_endpoints", "Service selector 打錯", "none", True, True,
          deployment("svc-selector", NGINX, port=80),
          extra=[_service("svc-selector", {"app": "svc-selectr"}, 80, 80)], expected_detect="no_endpoints"),
    _case("svc-targetport", "no_endpoints", "Service targetPort 8080、容器 80", "none", True, True,
          deployment("svc-targetport", NGINX, port=80),
          extra=[_service("svc-targetport", {"app": "svc-targetport"}, 80, 8080)], expected_detect="no_endpoints"),
    _case("rollout-stuck", "rollout_stuck", "新版本一直沒就緒、progressDeadline 30 秒", "none", True, False,
          _progress_deadline(deployment("rollout-stuck", NGINX, port=80, readiness=_readiness_path("/never")), 30),
          expected_detect="rollout_stuck"),
    _case("mount-secret", "mount_failed", "掛載不存在的 Secret", "none", True, False,
          _with_volume(deployment("mount-secret", BUSYBOX, _sh("sleep 999999")),
                       {"name": "cred", "secret": {"secretName": "no-such-secret"}}, "/cred"),
          expected_detect="mount_failed"),
    _case("init-fail", "init_failed", "initContainer exit 1", "none", True, False,
          _with_init(deployment("init-fail", BUSYBOX, _sh("sleep 999999")), "echo 'waiting for db... giving up'; exit 1"),
          expected_detect="init_failed"),
    _case("quota-pods", "quota_exceeded", "配額只允許 1 個 Pod、要 2 個副本", "none", True, False,
          deployment("quota-pods", BUSYBOX, _sh("sleep 999999"), replicas=2),
          extra=[{"apiVersion": "v1", "kind": "ResourceQuota", "metadata": {"name": "q"}, "spec": {"hard": {"pods": "1"}}}],
          expected_detect="quota_exceeded"),
    # 階段 3.1：剛才的更新造成崩潰 → 退回（不換映像檔，只改指令／加了缺的設定）
    _case("recent-crash", "recent_change", "正常服務改了啟動指令後 exit 1", "analyze_logs", False, True,
          deployment("recent-crash", BUSYBOX, _sh("echo 'panic: new release broke startup'; exit 1")),
          pre_deploy=deployment("recent-crash", BUSYBOX, _sh("sleep 999999")),
          expected_remediation="revert_recent_change"),
    _case("recent-config", "recent_change", "正常服務新增引用不存在 ConfigMap 的環境變數", "create_config", False, True,
          deployment("recent-config", BUSYBOX, _sh("sleep 999999"),
                     env=[{"name": "FEATURE", "valueFrom": {"configMapKeyRef": {"name": "new-flags", "key": "f"}}}]),
          pre_deploy=deployment("recent-config", BUSYBOX, _sh("sleep 999999")),
          expected_remediation="revert_recent_change"),
    # 階段 3.5：程式自己的 heap 用完（不是容器 OOMKilled），加容器記憶體沒用 → 只通知
    _case("heap-oom", "heap_oom", "log 印 java.lang.OutOfMemoryError 後 exit 1", "tune_heap", True, False,
          deployment("heap-oom", BUSYBOX, _sh("echo 'Exception in thread \"main\" java.lang.OutOfMemoryError: Java heap space'; exit 1"))),
    # 對照組：正常的服務＋正確的 Service，不能報 no_endpoints
    _case("ok-svc", "normal", "正常 nginx＋正確 Service", "none", True, True,
          deployment("ok-svc", NGINX, port=80), extra=[_service("ok-svc", {"app": "ok-svc"}, 80, 80)]),
]


def by_id() -> Dict[str, Dict]:
    return {c["id"]: c for c in CASES}
