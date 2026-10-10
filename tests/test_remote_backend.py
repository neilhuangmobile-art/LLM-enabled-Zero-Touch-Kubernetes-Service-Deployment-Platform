"""
客戶自帶叢集第 5 階段：所有功能接上客戶叢集。
- cluster/remote_view.py：回報 → 跟本機 k8s_* 函式相同格式（純邏輯）
- cluster/remote_ops.py：Healer 診斷 → Git 操作（純邏輯，假 repo）
- web_demo 路由：客戶模式下查詢讀回報、寫入變成 commit、本機路徑不受影響（假 repo + 假報告）
"""
import pytest

from cluster import remote_view as rv
from cluster import remote_ops as rops
from cluster.remote_state import clean_report

NS = "zt-carol-0123456789abcdef"


def _parse_mem(v):
    import re
    m = re.match(r"^(\d+)(Ki|Mi|Gi)?$", str(v or ""))
    if not m:
        return None
    return int(m.group(1)) * {"Ki": 1024, "Mi": 1024 ** 2, "Gi": 1024 ** 3, None: 1}[m.group(2)]


def _parse_cpu(v):
    s = str(v or "")
    if s.endswith("m"):
        return int(s[:-1])
    return int(float(s) * 1000) if s else None


def REPORT(**over):
    raw = {
        "agent_version": "2", "namespace": NS, "platform_configmap": True,
        "flux": {"source": {"ready": True}, "kustomization": {"ready": True}},
        "nodes": [{"name": "docker-desktop", "cpu": "8", "memory": "8Gi"}],
        "deployments": [
            {"name": "web", "replicas": 2, "ready": 2, "available": 2, "image": "nginx:1.27",
             "memory_limit": "256Mi", "created": "2026-10-09T01:00:00Z", "updated": "2026-10-09T02:00:00Z",
             "conditions": ["Available=True: MinimumReplicasAvailable"]},
            {"name": "api", "replicas": 1, "ready": 0, "available": 0, "image": "myapi:2",
             "memory_limit": "128Mi", "created": "2026-10-09T03:00:00Z", "conditions": []},
            {"name": "zt-reporter", "replicas": 1, "ready": 1, "image": "python:3.12-slim"},
        ],
        "pods": [
            {"name": "web-abc-1", "app": "web", "phase": "Running", "ip": "10.1.0.5", "created": "2026-10-09T01:00:00Z",
             "containers": [{"name": "web", "ready": True, "restart_count": 0, "state": "running"}],
             "spec_containers": [{"name": "web", "image": "nginx:1.27", "ports": [80],
                                  "requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"memory": "256Mi"}}]},
            {"name": "web-abc-2", "app": "web", "phase": "Running", "created": "2026-10-09T01:00:01Z",
             "containers": [{"name": "web", "ready": True, "restart_count": 0, "state": "running"}],
             "spec_containers": [{"name": "web", "requests": {"cpu": "100m", "memory": "128Mi"}}]},
            {"name": "api-xyz-1", "app": "api", "phase": "Running", "created": "2026-10-09T03:00:00Z",
             "containers": [{"name": "api", "ready": False, "restart_count": 6, "state": "waiting",
                             "reason": "CrashLoopBackOff", "last_reason": "OOMKilled"}],
             "spec_containers": [{"name": "api", "requests": {"cpu": "200m", "memory": "128Mi"}}],
             "logs": "java.lang.OutOfMemoryError"},
            {"name": "zt-reporter-1", "app": "zt-reporter", "phase": "Running", "containers": [
                {"name": "reporter", "ready": True, "restart_count": 0, "state": "running"}],
             "spec_containers": [{"name": "reporter", "requests": {"cpu": "10m", "memory": "32Mi"}}]},
        ],
        "events": [{"object": "api-xyz-1", "type": "Warning", "reason": "BackOff",
                    "message": "Back-off restarting", "last": "2026-10-09T03:05:00Z"}],
        "services": [{"name": "web", "type": "LoadBalancer", "app": "web", "ports": [8080]},
                     {"name": "api", "type": "ClusterIP", "app": "api", "ports": [9000]}],
        "metrics": {"web-abc-1": {"cpu": "5m", "memory": "20Mi"}},
    }
    raw.update(over)
    return clean_report(raw)


# ── remote_view：格式與本機一致 ──
def test_list_deployments_hides_reporter_and_sorts_newest_first():
    deps = rv.list_deployments(REPORT())
    assert [d["name"] for d in deps] == ["api", "web"]
    assert set(deps[0]) == {"name", "replicas", "ready", "image", "age", "updated"}
    assert deps[1]["age"] == "2026-10-09 09:00", "台灣時間格式同本機 _fmt_k8s_time"


def test_list_pods_matches_local_shape():
    pods = rv.list_pods(REPORT(), app="web")
    assert len(pods) == 2
    assert set(pods[0]) == {"name", "app", "phase", "ip", "node", "age", "containers", "conditions", "restarts"}
    assert pods[-1]["containers"][0]["resources"]["requests"]["memory"] == "128Mi"


def test_describe_pods_health_rules_match_local():
    api = rv.describe_pods(REPORT(), "api")
    assert len(api) == 1 and api[0]["healthy"] is False
    assert "CrashLoopBackOff" in api[0]["health_summary"]
    assert api[0]["events"][0]["reason"] == "BackOff"
    web = rv.describe_pods(REPORT(), "web-abc")
    assert len(web) == 2 and all(p["healthy"] for p in web)


def test_describe_deployment_parses_conditions():
    d = rv.describe_deployment(REPORT(), "web")
    assert d["healthy"] and d["conditions"][0] == {"type": "Available", "status": "True",
                                                   "reason": "MinimumReplicasAvailable", "message": ""}
    assert rv.describe_deployment(REPORT(), "zt-reporter") is None
    assert rv.describe_deployment(REPORT(), "nope") is None


def test_services_only_loadbalancer_and_capacity():
    assert rv.list_services(REPORT(), NS) == [{"name": "web", "app": "web", "port": 8080, "namespace": NS}]
    assert rv.node_capacity(REPORT(), _parse_mem) == {"cpu": "8", "memory": "8Gi"}
    assert rv.node_capacity(REPORT(nodes=[]), _parse_mem) is None


def test_requests_sum_excludes_platform_reporter():
    cpu, mem = rv.requests_sum(REPORT(), _parse_cpu, _parse_mem)
    assert cpu == 400 and mem == 3 * 128 * 1024 ** 2


def test_unhealthy_issues_feed_healer():
    issues = rv.unhealthy_issues(REPORT(), NS)
    assert len(issues) == 1
    i = issues[0]
    assert i["pod_name"] == "api-xyz-1" and i["app"] == "api" and i["reason"] == "CrashLoopBackOff"
    assert "OutOfMemoryError" in i["logs"] and i["events"][0]["reason"] == "BackOff"


def test_pod_usage_and_snapshots():
    assert rv.pod_usage(REPORT(), "web-abc-1", _parse_cpu, _parse_mem) == \
        {"available": True, "cpu_cores": 0.005, "memory_mi": 20.0}
    assert rv.pod_usage(REPORT(), "api-xyz-1", _parse_cpu, _parse_mem)["available"] is False
    snaps = rv.light_snapshots(REPORT(), "t")
    assert snaps["api"]["restarts"] == 6 and snaps["web"]["ready"] == 1


def test_empty_report_never_crashes():
    for fn in (rv.list_pods, rv.list_deployments):
        assert fn({}) == []
    assert rv.describe_pods({}, "x") == [] and rv.unhealthy_issues({}, NS) == []


# ── remote_ops：Healer 動作 → Git 操作 ──
class FakeRepo:
    def __init__(self):
        self.calls = []

    def restart_app(self, app):
        self.calls.append(("restart", app)); return {"commit": "c1"}

    def revert_app(self, app):
        self.calls.append(("revert", app)); return {"commit": "c2", "rolled_back_to": "abc1234"}

    def restore_previous_image(self, app):
        self.calls.append(("restore_image", app))
        return {"commit": "c4", "restored_image": "nginx:1.27", "previous_image": "nginx:bad"}

    def set_memory_limit(self, app, mem):
        self.calls.append(("memory", app, mem)); return {"commit": "c3"}


ISSUE = {"pod_name": "api-xyz-1", "app": "api", "reason": "OOMKilled"}


@pytest.mark.parametrize("action,expected", [
    ("analyze_logs", ("restart", "api")),
    ("fix_image", ("restore_image", "api")),  # 不是 revert_app：見 restore_previous_image 說明
    ("increase_memory", ("memory", "api", "256Mi")),
])
def test_heal_maps_actions_to_git(action, expected):
    repo = FakeRepo()
    r = rops.heal(repo, ISSUE, {"action": action}, current_memory="128Mi")
    assert r["ok"] and r["pending_sync"] and repo.calls == [expected]


def test_heal_memory_has_ceiling():
    repo = FakeRepo()
    assert rops.heal(repo, ISSUE, {"action": "increase_memory"}, current_memory="3Gi")["ok"]
    assert repo.calls[-1] == ("memory", "api", "4096Mi")
    r = rops.heal(repo, ISSUE, {"action": "increase_memory"}, current_memory="4Gi")
    assert r["ok"] is False and "不再自動加大" in r["message"]


def test_heal_manual_actions_do_not_touch_repo():
    repo = FakeRepo()
    r = rops.heal(repo, ISSUE, {"action": "check_dependencies", "suggestion": "檢查資料庫"})
    assert r["ok"] is False and r.get("manual") and "檢查資料庫" in r["message"] and repo.calls == []


def test_heal_without_app_is_explained():
    r = rops.heal(FakeRepo(), {"pod_name": "x"}, {"action": "analyze_logs"})
    assert r["ok"] is False and "手動" in r["message"]


# ── web_demo 路由：客戶模式 ──
web_demo = pytest.importorskip("web_demo")


class RouteRepo:
    def __init__(self):
        self.calls = []

    def write_app(self, parsed):
        self.calls.append(("write", parsed)); return {"app": parsed["app_name"], "commit": "deadbeef1"}

    def set_replicas(self, app, n):
        self.calls.append(("scale", app, n)); return {"commit": "c", "previous_replicas": 2}

    def set_image(self, app, image):
        self.calls.append(("image", app, image)); return {"commit": "c", "previous_image": "nginx:1.27"}

    def revert_app(self, app):
        self.calls.append(("revert", app)); return {"commit": "c", "rolled_back_to": "abc1234"}

    def delete_app(self, app):
        self.calls.append(("delete", app)); return {"commit": "c"}

    def restart_app(self, app):
        self.calls.append(("restart", app)); return {"commit": "c"}

    def set_memory_limit(self, app, mem):
        self.calls.append(("memory", app, mem)); return {"commit": "c"}

    def history(self, app=None, limit=20):
        return [{"commit": "abc1234", "time": "2026-10-09T10:00:00+00:00",
                 "message": "scale to 3: web (by carol via ZeroTouch, x)"}]


@pytest.fixture
def remote(tmp_path, monkeypatch):
    from cluster.remote_state import ClusterRegistry
    from core.web_security import RateLimiter
    from gitops.tenant_repo import tenant_slug
    monkeypatch.setattr(web_demo, "K8S_ENABLED", False)  # 平台自己的 K8s 關掉也要能用客戶叢集
    monkeypatch.setattr(web_demo, "USERS", {})
    monkeypatch.setattr(web_demo, "__file__", str(tmp_path / "web_demo.py"))
    monkeypatch.setattr(web_demo, "_rate_limiter", RateLimiter())
    monkeypatch.setattr(web_demo, "CLUSTERS", ClusterRegistry(str(tmp_path / "c.json")))
    monkeypatch.setattr(web_demo, "CUSTOMER_CLUSTER_MODE", True)
    repo = RouteRepo()
    monkeypatch.setattr(web_demo, "_tenant_repo", lambda u: repo)
    monkeypatch.setattr(web_demo, "_remote_heal_recent", {})  # 修復冷卻紀錄每個測試獨立
    web_demo.app.config["TESTING"] = True
    with web_demo.app.test_client() as c:
        c.post("/auth/register", data={"username": "carol", "password": "hunter22", "confirm": "hunter22"})
        rep = REPORT(namespace=tenant_slug("carol"))
        web_demo.CLUSTERS.store_report("carol", rep)
        c.repo = repo
        yield c


def test_reads_come_from_customer_report(remote):
    assert [d["name"] for d in remote.get("/api/deployments").get_json()["deployments"]] == ["api", "web"]
    assert len(remote.get("/api/pods").get_json()["pods"]) == 4
    d = remote.get("/api/pods/api").get_json()
    assert d["found"] and d["pods"][0]["healthy"] is False
    assert remote.get("/api/deployments/web").get_json()["deployment"]["healthy"] is True
    issues = remote.get("/api/healer/scan").get_json()["issues"]
    assert [i["pod_name"] for i in issues] == ["api-xyz-1"]
    m = remote.get("/api/metrics").get_json()["metrics"]
    assert m["pod_count"] == 3 and "customer-cluster" in m["source"]
    dash = remote.get("/api/dashboard").get_json()
    assert dash["customer_cluster"] and dash["cluster_capacity"]["cpu_cores"] == 8.0
    st = remote.get("/api/status").get_json()
    assert st["k8s"] is True and st["customer_cluster"]["code"] == "connected"


def test_writes_become_git_commits(remote):
    r = remote.post("/api/scale", json={"name": "web", "replicas": 3}).get_json()
    assert r["success"] and r["pending_sync"] and ("scale", "web", 3) in remote.repo.calls
    r = remote.post("/api/update", json={"name": "web", "image": "nginx:1.28"}).get_json()
    assert r["success"] and ("image", "web", "nginx:1.28") in remote.repo.calls
    r = remote.post("/api/rollback", json={"app_name": "web"}).get_json()
    assert r["success"] and ("revert", "web") in remote.repo.calls
    r = remote.post("/api/delete", json={"name": "web"}).get_json()
    assert r["success"] and ("delete", "web") in remote.repo.calls
    g = remote.get("/api/gitops").get_json()
    assert g["commits"][0]["app"] == "web" and g["customer_cluster"]


def test_update_to_miner_image_is_blocked_before_git(remote):
    """換映像檔不經過完整 guardian 驗證，挖礦映像要在 /api/update 就擋下，不能寫進客戶 repo。"""
    resp = remote.post("/api/update", json={"name": "web", "image": "metal3d/xmrig:6.21"})
    assert resp.status_code == 400
    assert "挖礦" in resp.get_json()["error"]
    assert not any(c[0] == "image" for c in remote.repo.calls)


def test_scale_over_customer_capacity_is_blocked(remote):
    # 副本數上限是 50（policy_rules.yaml），要在上限內超出容量：節點縮成 2Gi，20 × 128Mi 放不下
    _, rep = web_demo.CLUSTERS.latest_report("carol")
    rep["nodes"][0]["memory"] = "2Gi"
    r = remote.post("/api/scale", json={"name": "web", "replicas": 20})
    assert r.status_code == 409 and "容量" in r.get_json()["error"]
    assert not any(c[0] == "scale" for c in remote.repo.calls)


def test_writes_blocked_when_cluster_offline(remote):
    at, rep = web_demo.CLUSTERS.latest_report("carol")
    web_demo.CLUSTERS._reports["carol"] = (at - 600, rep)
    r = remote.post("/api/scale", json={"name": "web", "replicas": 3})
    assert r.status_code == 409 and "離線" in r.get_json()["error"]
    assert remote.repo.calls == []


def test_heal_pod_goes_through_git(remote, monkeypatch):
    import healer.diagnose as hd
    monkeypatch.setattr(hd, "diagnose_issue", lambda ctx: {"action": "analyze_logs", "root_cause": "crash"})
    r = remote.post("/api/healer/fix", json={"pod_name": "api-xyz-1"}).get_json()
    assert r["success"], r
    assert r["root_cause"] == "crash"
    assert remote.repo.calls == [("restart", "api")]


def test_admin_still_uses_local_cluster(remote, monkeypatch):
    monkeypatch.setattr(web_demo, "_is_admin_user", lambda u: True)
    assert remote.get("/api/deployments").get_json()["deployments"] == []  # 本機 K8s 關閉 → 空清單
    assert remote.repo.calls == []


def test_app_from_commit_message():
    f = web_demo._app_from_commit
    assert f("deploy web (2x nginx) (by u via ZeroTouch, t)") == "web"
    assert f("update image -> nginx:2: web (by u via ZeroTouch, t)") == "web"
    assert f("set memory 512Mi: api (by u, t)") == "api"
    assert f("rollback web to abc1234 (by u, t)") == "web"
    assert f("restore image of web -> nginx:1.27 (by u, t)") == "web"


def test_heal_cooldown_prevents_stacked_fixes(remote, monkeypatch):
    """修復 commit 後到叢集同步前，回報裡還看得到同一個問題：第二次（手動或背景迴圈）不能再動手。"""
    import healer.diagnose as hd
    monkeypatch.setattr(hd, "diagnose_issue", lambda ctx: {"action": "analyze_logs", "root_cause": "crash"})
    first = remote.post("/api/healer/fix", json={"pod_name": "api-xyz-1"}).get_json()
    second = remote.post("/api/healer/fix", json={"pod_name": "api-xyz-1"}).get_json()
    assert first["success"] and first["action"] == "analyze_logs"
    assert second["action"] == "in_progress" and "剛修復過" in second["message"]
    assert remote.repo.calls == [("restart", "api")], "冷卻時間內只能有一次修復"
    from gitops.tenant_repo import tenant_slug
    r = web_demo._real_heal_pod("api-xyz-1", tenant_slug("carol"))  # 背景迴圈走同一個入口
    assert r["action"] == "in_progress" and remote.repo.calls == [("restart", "api")]


def test_remote_heal_budget_caps_per_hour(remote, monkeypatch):
    """背景安全審查：客戶可自建大量壞 Pod，自動修復每位客戶每小時有上限（防 GPU/GitHub 被吃光）。"""
    monkeypatch.setattr(web_demo, "_remote_heal_budget", {})
    allowed = [web_demo._remote_heal_allowed("carol") for _ in range(15)]
    assert allowed.count(True) == web_demo._REMOTE_HEAL_PER_HOUR and allowed[-1] is False
    assert web_demo._remote_heal_allowed("dave") is True, "上限是每位客戶各自計算"


def test_auto_fix_is_capped(remote, monkeypatch):
    import healer.diagnose as hd
    monkeypatch.setattr(hd, "diagnose_issue", lambda ctx: {"action": "manual_inspect"})
    many = [{"name": f"bad-{i}-x-y", "app": f"bad{i}", "phase": "Running",
             "containers": [{"name": "c", "state": "waiting", "reason": "CrashLoopBackOff"}]} for i in range(12)]
    from gitops.tenant_repo import tenant_slug
    web_demo.CLUSTERS.store_report("carol", REPORT(namespace=tenant_slug("carol"), pods=many))
    d = remote.post("/api/healer/auto_fix").get_json()
    assert len(d["details"]) == web_demo._REMOTE_AUTOFIX_MAX


def test_clean_exit_crash_loop_is_detected():
    """一直 exit 0 又被重啟的服務（busybox）也要當成故障（2026-10-09 實測 Healer 沒反應）。"""
    pods = [{"name": "crash-x-1", "app": "crash", "phase": "Running", "containers": [
        {"name": "c", "ready": False, "restart_count": 4, "state": "terminated",
         "reason": "Completed", "last_reason": "Completed"}]},
            {"name": "ok-x-1", "app": "ok", "phase": "Running", "containers": [
        {"name": "c", "ready": True, "restart_count": 7, "state": "running"}]}]
    issues = rv.unhealthy_issues(REPORT(pods=pods), NS)
    assert [(i["app"], i["reason"]) for i in issues] == [("crash", "CrashLoopBackOff")], \
        "目前正常執行的服務就算重啟過很多次也不算"
