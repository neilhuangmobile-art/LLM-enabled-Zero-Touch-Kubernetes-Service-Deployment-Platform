"""Healer 補強方案階段 1（healer/verify.py：快照、dry-run、驗證、退回）與 GitOps 紀錄（2026-11-25）。"""
import os
import sys
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from unittest.mock import MagicMock

import pytest
from kubernetes import client as k8s

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from healer import guard, verify

remediate_module = sys.modules["healer.remediate"]


def _deploy(limit="512Mi", request="256Mi", image="web:1", ann=None, replicas=1):
    c = k8s.V1Container(name="web", image=image,
                        resources=k8s.V1ResourceRequirements(limits={"memory": limit} if limit else None,
                                                             requests={"memory": request} if request else None))
    return k8s.V1Deployment(
        metadata=k8s.V1ObjectMeta(name="web", namespace="user-a", generation=3, annotations=dict(ann or {})),
        spec=k8s.V1DeploymentSpec(replicas=replicas, selector=k8s.V1LabelSelector(match_labels={"app": "web"}),
                                  template=k8s.V1PodTemplateSpec(spec=k8s.V1PodSpec(containers=[c]))))


class FakeApps:
    def __init__(self, deploy, dry_run_error=None):
        self.deploy, self.dry_run_error, self.patches = deploy, dry_run_error, []

    def read_namespaced_deployment(self, name, ns):
        import copy
        return copy.deepcopy(self.deploy)

    def patch_namespaced_deployment(self, name, ns, body, dry_run=None):
        if dry_run:
            if self.dry_run_error:
                raise Exception(self.dry_run_error)
            return body
        self.patches.append(body)
        self.deploy = body
        return body

    # 2026-11-25：退回改成整個物件取代（合併式 patch 拿不掉新增的欄位）
    replace_namespaced_deployment = patch_namespaced_deployment


# ── apply_patch ──────────────────────────────────────────────────
def test_dry_run_failure_means_no_change():
    apps = FakeApps(_deploy(), dry_run_error="admission webhook denied")
    r = verify.apply_patch(apps, "user-a", "web", lambda d: setattr(d.spec.template.spec.containers[0], "image", "x") or {})
    assert not r["ok"] and "dry-run" in r["message"] and apps.patches == []


def test_skip_patch_sends_nothing():
    apps = FakeApps(_deploy())

    def mutate(d):
        raise verify.SkipPatch("不用改")
    r = verify.apply_patch(apps, "user-a", "web", mutate)
    assert r["skipped"] and apps.patches == []


def test_snapshot_and_restore_roundtrip():
    d = _deploy(limit="512Mi", image="web:1")
    snap = verify.snapshot(d)
    d.spec.template.spec.containers[0].image = "web:2"
    d.spec.template.spec.containers[0].resources.limits = {"memory": "1Gi"}
    assert verify.restore(d, snap) == ["web"]
    c = d.spec.template.spec.containers[0]
    assert c.image == "web:1" and c.resources.limits == {"memory": "512Mi"}


# ── evaluate ─────────────────────────────────────────────────────
def _status_deploy(avail=1, gen=3, obs=3):
    d = _deploy()
    d.metadata.generation = gen
    d.status = k8s.V1DeploymentStatus(available_replicas=avail, updated_replicas=avail, observed_generation=obs,
                                      replicas=1, unavailable_replicas=None if avail else 1)
    return d


def _pod(ready_since, created, restarts=0, ready=True):
    t = lambda s: datetime.fromtimestamp(s, tz=timezone.utc)  # noqa: E731
    return NS(metadata=NS(name="web-abc-1", creation_timestamp=t(created)),
              status=NS(conditions=[NS(type="Ready", status="True" if ready else "False", last_transition_time=t(ready_since))],
                        container_statuses=[NS(restart_count=restarts)]))


def _core(pods, events=()):
    core = MagicMock()
    core.list_namespaced_pod.return_value = NS(items=list(pods))
    core.list_namespaced_event.return_value = NS(items=list(events))
    return core


def _item(fixed=1000.0):
    return {"key": "user-a/web/OOMKilled", "namespace": "user-a", "deployment": "web", "action": "increase_memory",
            "snapshot": None, "rollback_allowed": True, "fixed_at": fixed, "due": fixed + 90, "deadline": fixed + 300}


def test_verify_ok_after_ready_hold():
    apps = MagicMock()
    apps.read_namespaced_deployment.return_value = _status_deploy()
    r = verify.evaluate(apps, _core([_pod(ready_since=1010, created=1005)]), _item(), now=1200)
    assert r["state"] == "ok"


def test_verify_waits_until_due_and_ready_long_enough():
    apps = MagicMock()
    apps.read_namespaced_deployment.return_value = _status_deploy()
    assert verify.evaluate(apps, _core([_pod(ready_since=1150, created=1140)]), _item(), now=1200)["state"] == "wait"


def test_verify_fails_on_restart_after_fix():
    apps = MagicMock()
    apps.read_namespaced_deployment.return_value = _status_deploy()
    r = verify.evaluate(apps, _core([_pod(ready_since=1010, created=1005, restarts=2)]), _item(), now=1100)
    assert r["state"] == "fail" and "重啟" in r["why"]


def test_verify_fails_at_deadline_when_not_available():
    apps = MagicMock()
    apps.read_namespaced_deployment.return_value = _status_deploy(avail=0)
    r = verify.evaluate(apps, _core([_pod(ready_since=1010, created=1005, ready=False)]), _item(), now=1400)
    assert r["state"] == "fail"


def test_verify_fails_on_new_backoff_event():
    apps = MagicMock()
    apps.read_namespaced_deployment.return_value = _status_deploy()
    ev = NS(reason="BackOff", involved_object=NS(name="web-abc-1"),
            last_timestamp=datetime.fromtimestamp(1100, tz=timezone.utc), event_time=None)
    r = verify.evaluate(apps, _core([_pod(ready_since=1010, created=1005)], [ev]), _item(), now=1200)
    assert r["state"] == "fail" and "BackOff" in r["why"]


def test_old_events_before_fix_are_ignored():
    apps = MagicMock()
    apps.read_namespaced_deployment.return_value = _status_deploy()
    ev = NS(reason="BackOff", involved_object=NS(name="web"),
            last_timestamp=datetime.fromtimestamp(900, tz=timezone.utc), event_time=None)
    assert verify.evaluate(apps, _core([_pod(ready_since=1010, created=1005)], [ev]), _item(), now=1200)["state"] == "ok"


def test_rollback_restores_snapshot_and_needs_permission():
    apps = FakeApps(_deploy(limit="1Gi", image="web:2"))
    snap = verify.snapshot(_deploy(limit="512Mi", image="web:1"))
    assert not verify.rollback(apps, {**_item(), "snapshot": snap, "rollback_allowed": False})["ok"]
    r = verify.rollback(apps, {**_item(), "snapshot": snap})
    c = apps.deploy.spec.template.spec.containers[0]
    assert r["ok"] and c.image == "web:1" and c.resources.limits == {"memory": "512Mi"}


def test_pending_disabled_by_switch(monkeypatch):
    p = verify.Pending()
    monkeypatch.setenv("HEALER_VERIFY", "0")
    p.register("k", "ns", "web", "fix_probe", None, True)
    assert not p.has("k")


# ── remediate：調高記憶體的完整流程 ──────────────────────────────────
@pytest.fixture
def wired(monkeypatch):
    apps = FakeApps(_deploy(limit="512Mi", request="256Mi"))
    monkeypatch.setattr(remediate_module, "_load_k8s_config", lambda: None)
    monkeypatch.setattr(remediate_module.k8s_client, "AppsV1Api", lambda: apps)
    gitops = []
    monkeypatch.setattr(remediate_module, "_record_gitops", lambda issue, action: gitops.append(action) or {"ok": True})
    verify.PENDING._items.clear()
    return apps, gitops


def _issue():
    return {"pod_name": "web-abc-1", "namespace": "user-a", "container": "web", "reason": "OOMKilled",
            "owner_kind": "Deployment", "deployment": "web"}


def test_increase_memory_patches_registers_and_records(wired):
    apps, gitops = wired
    r = remediate_module.remediate(_issue(), {"action": "increase_memory", "severity": "high"})
    c = apps.deploy.spec.template.spec.containers[0]
    assert r["ok"] and r["details"]["old"] == "512Mi" and r["details"]["new"] == "1024Mi"
    assert c.resources.limits["memory"] == "1024Mi" and c.resources.requests["memory"] == "512Mi"
    assert apps.deploy.metadata.annotations[guard.ANN_MEMORY_BUMPS] == "1"
    assert verify.PENDING.has("user-a/web/oom") and gitops == ["increase_memory"]   # key 用問題類別


def test_increase_memory_without_limit_only_notifies(wired, monkeypatch):
    apps, gitops = wired
    apps.deploy = _deploy(limit=None, request=None)
    r = remediate_module.remediate(_issue(), {"action": "increase_memory", "severity": "high"})
    assert r["action"] == "manual_inspect" and apps.patches == [] and gitops == []


def test_fix_image_needs_last_good_image(wired):
    apps, gitops = wired
    issue = {**_issue(), "reason": "ImagePullBackOff"}
    r = remediate_module.remediate(issue, {"action": "fix_image"})
    assert r["action"] == "manual_inspect" and apps.patches == []
    apps.deploy = _deploy(image="web:bad", ann={guard.ANN_LAST_GOOD_IMAGE: "web:1"})
    r = remediate_module.remediate(issue, {"action": "fix_image"})
    assert r["ok"] and apps.deploy.spec.template.spec.containers[0].image == "web:1" and gitops == ["fix_image"]


def test_fix_image_not_on_rate_limit(wired):
    apps, _ = wired
    apps.deploy = _deploy(image="web:2", ann={guard.ANN_LAST_GOOD_IMAGE: "web:1"})
    r = remediate_module.remediate({**_issue(), "reason": "ErrImagePull", "message": "429 Too Many Requests"},
                                   {"action": "fix_image"})
    assert r["action"] == "manual_inspect" and apps.patches == []


def test_crashloop_single_replica_does_not_delete_pod(wired, monkeypatch):
    core = MagicMock()
    monkeypatch.setattr(remediate_module.k8s_client, "CoreV1Api", lambda: core)
    r = remediate_module.remediate({**_issue(), "reason": "CrashLoopBackOff"}, {"action": "analyze_logs"})
    assert r["action"] == "manual_inspect"
    core.delete_namespaced_pod.assert_not_called()


# ── GitOps 紀錄 ─────────────────────────────────────────────────
MANIFEST = """---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: web
  namespace: user-a
spec:
  template:
    spec:
      containers:
      - name: web
        image: web:1
        resources:
          limits:
            memory: 512Mi
"""


def test_gitops_manifest_updated_and_committed(tmp_path, monkeypatch):
    import subprocess
    from gitops import heal_record
    repo = tmp_path / "gitops"
    d = repo / "manifests" / "user-a" / "web"
    d.mkdir(parents=True)
    (d / "deployment.yaml").write_text(MANIFEST, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    for k, v in (("user.name", "t"), ("user.email", "t@t")):
        subprocess.run(["git", "-C", str(repo), "config", k, v], check=True)
    live = _deploy(limit="1024Mi", request="512Mi", image="web:1")
    r = heal_record.record_heal("user-a", "web", "increase_memory", repo_path=str(repo), deploy=live)
    assert r["ok"] and r.get("sha")
    text = (d / "deployment.yaml").read_text(encoding="utf-8")
    assert "1024Mi" in text and "512Mi" in text
    msg = subprocess.run(["git", "-C", str(repo), "log", "-1", "--format=%s"], capture_output=True, text=True).stdout.strip()
    assert msg == "healer: web increase_memory"
    again = heal_record.record_heal("user-a", "web", "increase_memory", repo_path=str(repo), deploy=live)
    assert again["ok"] and again.get("skipped")


def test_gitops_skips_missing_file_and_bad_names(tmp_path):
    from gitops import heal_record
    (tmp_path / ".git").mkdir()
    assert heal_record.record_heal("user-a", "nope", "fix_probe", repo_path=str(tmp_path), deploy=_deploy())["skipped"]
    assert heal_record.record_heal("user-a", "../x", "fix_probe", repo_path=str(tmp_path))["skipped"] == "名稱不合法"


def test_startup_probe_failures_during_start_are_not_a_relapse():
    """2026-11-25 評估發現：加了 startupProbe 之後，啟動期間的 Startup probe failed 事件被當成修壞，誤退回。"""
    apps = MagicMock()
    apps.read_namespaced_deployment.return_value = _status_deploy()
    ev = NS(reason="Unhealthy", message="Startup probe failed: Get \"http://10.1.0.5:8000/\": connection refused",
            involved_object=NS(name="web-abc-1"), last_timestamp=datetime.fromtimestamp(1100, tz=timezone.utc), event_time=None)
    assert verify.evaluate(apps, _core([_pod(ready_since=1010, created=1005)], [ev]), _item(), now=1200)["state"] == "ok"
    ev.message = "Liveness probe failed: connection refused"
    assert verify.evaluate(apps, _core([_pod(ready_since=1010, created=1005)], [ev]), _item(), now=1200)["state"] == "fail"


def test_rollback_removes_fields_added_by_fix():
    """2026-11-25 評估發現：退回原本用合併式 patch，修復時新增的 startupProbe 拿不掉。"""
    from kubernetes import client as k
    snap = verify.snapshot(_deploy(limit="512Mi", image="web:1"))
    fixed = _deploy(limit="512Mi", image="web:1")
    fixed.spec.template.spec.containers[0].startup_probe = k.V1Probe(period_seconds=10, failure_threshold=30)
    apps = MagicMock()
    apps.read_namespaced_deployment.return_value = fixed
    r = verify.rollback(apps, {**_item(), "snapshot": snap})
    sent = apps.replace_namespaced_deployment.call_args.args[2]
    assert r["ok"] and sent.spec.template.spec.containers[0].startup_probe is None
    assert apps.patch_namespaced_deployment.call_count == 0
