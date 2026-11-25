"""定時執行（CronJob，2026-11-17）：YAML、審查、dry-run、格式檢查、部署、API。不碰真的叢集。"""
import datetime as dt
import os
import sys
from types import SimpleNamespace as NS

import pytest
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.pipeline import cron  # noqa: E402
from gitops import manifest_writer as mw  # noqa: E402

SPEC = {"app_name": "backup", "image": "alpine:3.20", "pods": 1, "port": 80, "workload": "cronjob",
        "schedule": "0 3 * * *", "command": ["sh", "-c", "echo hi"], "memory": "64Mi", "cpu": "50m",
        "env": {"DB_PASSWORD": "s3cret"}, "secret_keys": ["DB_PASSWORD"]}


# ── 排程小工具 ───────────────────────────────────────────────────────
def test_cron_interval_next_run_and_monthly_runs():
    assert cron.min_interval_minutes("*/5 * * * *") == 5 and cron.too_frequent("*/2 * * * *")
    assert not cron.too_frequent("0 3 * * *") and cron.too_frequent("0,2 * * * *")
    now = dt.datetime(2026, 11, 17, 18, 7, tzinfo=cron._TZ)          # 週二
    assert cron.next_run("0 3 * * *", now).isoformat() == "2026-11-18T03:00:00+08:00"
    assert cron.next_run("0 0 * * 1", now).isoformat() == "2026-11-23T00:00:00+08:00"
    assert cron.runs_per_month("0 3 * * *") == 30 and round(cron.runs_per_month("0 0 * * 1")) == 4
    assert cron.describe_zh("0 3 * * *") == "每天 03:00（台灣時間）"


# ── YAML ─────────────────────────────────────────────────────────────
def test_cronjob_doc_limits_and_no_service_or_probes():
    doc = mw.build_cronjob_doc(SPEC, "user-x")
    sp = doc["spec"]
    assert doc["kind"] == "CronJob" and sp["schedule"] == "0 3 * * *" and sp["timeZone"] == "Asia/Taipei"
    assert sp["concurrencyPolicy"] == "Forbid"
    js = sp["jobTemplate"]["spec"]
    assert js["activeDeadlineSeconds"] == 3600 and js["backoffLimit"] == 2
    pod = js["template"]["spec"]
    c = pod["containers"][0]
    # 2026-11-25（Healer 方案階段 5）：改成 Never，podFailurePolicy 才能用（見 tests/test_cronjob_policy.py）
    assert pod["restartPolicy"] == "Never" and js["template"]["metadata"]["labels"]["app"] == "backup"
    assert "readinessProbe" not in c and "ports" not in c
    assert c["securityContext"]["allowPrivilegeEscalation"] is False
    pw = [e for e in c["env"] if e["name"] == "DB_PASSWORD"][0]
    assert "valueFrom" in pw and "s3cret" not in yaml.dump(doc)      # 密碼不寫進設定檔


def test_write_manifest_writes_cronjob_without_service(tmp_path):
    res = mw.write_manifest(SPEC, repo_path=str(tmp_path), namespace="user-x", commit=False)
    names = sorted(os.path.basename(f) for f in res["files"])
    assert names == ["cronjob.yaml"]


# ── 審查 ─────────────────────────────────────────────────────────────
def test_validator_now_checks_cronjob_containers():
    from guardian.yaml_validator import validate_yaml
    doc = mw.build_cronjob_doc(SPEC, "user-x")
    doc["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["securityContext"]["privileged"] = True
    r = validate_yaml(doc)
    assert not r["ok"] and any("privileged" in e for e in r["errors"])


def test_agents_see_cronjob_containers_and_cost_is_bounded():
    from agents.cost_agent import _estimate_monthly_cost
    from agents.perf_agent import analyze as perf
    from agents.security_agent import _get_containers
    doc = mw.build_cronjob_doc(SPEC, "user-x")
    assert len(_get_containers(doc)) == 1
    est = _estimate_monthly_cost(doc)
    dep = {"kind": "Deployment", "spec": {"replicas": 1, "template": doc["spec"]["jobTemplate"]["spec"]["template"]}}
    assert est["estimated_usd"] < _estimate_monthly_cost(dep)["estimated_usd"] and "上限" in est["note"]
    p = perf(doc)
    assert p["ok"] and not p["issues"] and p["hpa_yaml"] is None


def test_dry_run_builds_cronjob():
    from guardian.dry_run import _build_k8s_manifests
    docs = _build_k8s_manifests(SPEC)
    assert [d["kind"] for d in docs] == ["CronJob"]


# ── 網站 ─────────────────────────────────────────────────────────────
web_demo = pytest.importorskip("web_demo")


def test_sanitize_accepts_valid_and_rejects_too_frequent():
    ok = web_demo._sanitize_deploy_payload(SPEC)
    assert ok["workload"] == "cronjob" and ok["schedule"] == "0 3 * * *" and ok["pods"] == 1 and ok["expose"] is False
    bad = web_demo._sanitize_deploy_payload({**SPEC, "schedule": "* * * * *"})
    assert "error" in bad and "5 分鐘" in bad["error"]
    assert "error" in web_demo._sanitize_deploy_payload({**SPEC, "schedule": "每天三點"})
    # 一般服務的行為不變
    assert "workload" not in web_demo._sanitize_deploy_payload({"app_name": "web", "image": "nginx", "pods": 2})


class _FakeBatch:
    def __init__(self, exists=False):
        self.calls, self.exists = [], exists

    def __getattr__(self, name):
        def f(*a, **k):
            self.calls.append(name)
            if name.startswith("replace") and not self.exists:
                raise RuntimeError("not found")
            if name == "read_namespaced_cron_job":
                return NS(metadata=NS(uid="u1"), spec=NS(job_template={"spec": {"template": {}}}))
            return NS(items=[])
        return f


def test_k8s_deploy_cronjob_uses_batch_api_and_no_service(monkeypatch, tmp_path):
    batch, core = _FakeBatch(), _FakeBatch()
    monkeypatch.setattr(web_demo, "K8S_ENABLED", True)
    monkeypatch.setattr(web_demo, "_k8s_batch_clients", lambda: (batch, core))
    monkeypatch.setattr(web_demo, "YAML_DIR", str(tmp_path))
    ok, msg = web_demo.k8s_deploy_cronjob(SPEC, "user-x")
    assert ok and "每天 03:00" in msg
    assert "create_namespaced_cron_job" in batch.calls
    assert not any("service" in c for c in core.calls) and "create_namespaced_secret" in core.calls


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(web_demo, "K8S_ENABLED", False)
    monkeypatch.setattr(web_demo, "USERS", {})
    monkeypatch.setattr(web_demo, "__file__", str(tmp_path / "web_demo.py"))
    from core.web_security import RateLimiter
    monkeypatch.setattr(web_demo, "_rate_limiter", RateLimiter())
    web_demo.app.config["TESTING"] = True
    with web_demo.app.test_client() as c:
        yield c


def _login(c):
    c.post("/auth/register", data={"username": "cronuser", "password": "hunter22", "confirm": "hunter22"})


def test_cron_apis_require_login(client):
    assert client.get("/api/cronjobs").status_code == 401
    assert client.post("/api/cronjobs/backup/run").status_code == 401
    assert client.post("/api/cronjobs/backup/suspend").status_code == 401


def test_cron_run_suspend_and_not_found(client, monkeypatch):
    _login(client)
    monkeypatch.setattr(web_demo, "K8S_ENABLED", True)
    monkeypatch.setattr(web_demo, "_is_remote_ns", lambda ns: False)
    assert client.post("/api/cronjobs/Bad_Name/run").status_code == 400
    monkeypatch.setattr(web_demo, "k8s_cronjob_exists", lambda n, ns: False)
    assert client.post("/api/cronjobs/backup/run").status_code == 404      # 不是自己 namespace 的也是找不到
    batch = _FakeBatch(exists=True)
    monkeypatch.setattr(web_demo, "k8s_cronjob_exists", lambda n, ns: True)
    monkeypatch.setattr(web_demo, "_k8s_batch_clients", lambda: (batch, batch))
    monkeypatch.setattr(web_demo._events, "record", lambda *a, **k: None)
    r = client.post("/api/cronjobs/backup/run")
    assert r.status_code == 200 and "create_namespaced_job" in batch.calls
    r = client.post("/api/cronjobs/backup/suspend", json={"suspend": True})
    assert r.status_code == 200 and "patch_namespaced_cron_job" in batch.calls and "暫停" in r.get_json()["message"]


def test_scale_and_rollback_explain_for_cronjob(client, monkeypatch):
    _login(client)
    monkeypatch.setattr(web_demo, "K8S_ENABLED", True)
    monkeypatch.setattr(web_demo, "_is_cronjob_only", lambda n, ns: True)
    r = client.post("/api/scale", json={"name": "backup", "replicas": 3})
    assert r.status_code == 400 and "定時執行" in r.get_json()["error"]
    r = client.post("/api/rollback", json={"name": "backup"})
    assert r.status_code == 400 and "定時執行" in r.get_json()["error"]


def test_delete_cronjob(client, monkeypatch):
    _login(client)
    monkeypatch.setattr(web_demo, "_is_cronjob_only", lambda n, ns: True)
    seen = []
    monkeypatch.setattr(web_demo, "k8s_delete_cronjob", lambda n, ns: seen.append((n, ns)))
    r = client.post("/api/delete", json={"name": "backup"})
    assert r.get_json()["success"] and seen and seen[0][0] == "backup"
