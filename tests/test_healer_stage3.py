"""Healer 補強方案階段 3（2026-11-25）：最近的更新造成崩潰 → 退回；啟動太慢改用 startupProbe。"""
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kubernetes import client as k

from healer import guard, verify

remediate_module = __import__("healer.remediate", fromlist=["x"])

NOW = time.time()


def _rs(hash_, rev, minutes_ago, image="app:1", owner="web"):
    created = datetime.fromtimestamp(NOW - minutes_ago * 60, tz=timezone.utc)
    return NS(metadata=NS(name=f"{owner}-{hash_}", labels={"pod-template-hash": hash_},
                          annotations={guard.ANN_REVISION: str(rev)}, creation_timestamp=created,
                          owner_references=[NS(kind="Deployment", name=owner)]),
              spec=NS(template=k.V1PodTemplateSpec(
                  metadata=k.V1ObjectMeta(labels={"app": owner, "pod-template-hash": hash_}),
                  spec=k.V1PodSpec(containers=[k.V1Container(name="app", image=image)]))))


def _dep(rev, good=None):
    ann = {guard.ANN_REVISION: str(rev)}
    if good:
        ann[guard.ANN_LAST_GOOD_TEMPLATE] = good
    return NS(metadata=NS(name="web", annotations=ann))


def test_recent_update_reverts_to_last_good_template():
    rss = [_rs("good1", 1, 300, "app:1"), _rs("bad22", 2, 5, "app:2")]
    target, why = guard.recent_change_plan(_dep(2, good="good1"), rss, now=NOW)
    assert why is None and target.metadata.labels["pod-template-hash"] == "good1"


def test_no_revert_when_update_is_old_or_no_record_or_current_is_good():
    rss = [_rs("good1", 1, 300), _rs("bad22", 2, 45)]
    assert guard.recent_change_plan(_dep(2, good="good1"), rss, now=NOW)[1] == "最近 30 分鐘沒有更新過"
    rss = [_rs("good1", 1, 300), _rs("bad22", 2, 5)]
    assert "沒有" in guard.recent_change_plan(_dep(2), rss, now=NOW)[1]
    assert "不是剛才的更新" in guard.recent_change_plan(_dep(2, good="bad22"), rss, now=NOW)[1]
    assert "清掉" in guard.recent_change_plan(_dep(2, good="gone9"), rss, now=NOW)[1]


def test_other_deployment_replicasets_are_ignored():
    rss = [_rs("good1", 1, 300, owner="api"), _rs("bad22", 2, 5)]
    assert guard.recent_change_plan(_dep(2, good="good1"), rss, now=NOW)[0] is None


def test_last_good_records_template_hash():
    d = NS(metadata=NS(annotations={}, generation=1),
           spec=NS(replicas=1, template=NS(spec=NS(containers=[NS(image="app:1")]))),
           status=NS(available_replicas=1, updated_replicas=1, observed_generation=1, replicas=1, unavailable_replicas=None))
    assert guard.last_good_update(d, "good1") == {guard.ANN_LAST_GOOD_IMAGE: "app:1", guard.ANN_LAST_GOOD_TEMPLATE: "good1"}
    d.metadata.annotations = {guard.ANN_LAST_GOOD_IMAGE: "app:1", guard.ANN_LAST_GOOD_TEMPLATE: "good1"}
    assert guard.last_good_update(d, "good1") is None


def _deployment(image="app:2", env=None):
    return k.V1Deployment(
        metadata=k.V1ObjectMeta(name="web", namespace="user-a", generation=2,
                                annotations={guard.ANN_REVISION: "2", guard.ANN_LAST_GOOD_TEMPLATE: "good1"}),
        spec=k.V1DeploymentSpec(replicas=1, selector=k.V1LabelSelector(match_labels={"app": "web"}),
                                template=k.V1PodTemplateSpec(
                                    metadata=k.V1ObjectMeta(labels={"app": "web"}),
                                    spec=k.V1PodSpec(containers=[k.V1Container(name="app", image=image, env=env)]))))


def test_revert_replaces_whole_template_and_registers_full_rollback(monkeypatch):
    apps = MagicMock()
    apps.read_namespaced_deployment.return_value = _deployment(env=[k.V1EnvVar(name="NEW", value="1")])
    apps.list_namespaced_replica_set.return_value.items = [_rs("good1", 1, 300, "app:1"), _rs("bad22", 2, 5, "app:2")]
    monkeypatch.setattr(remediate_module, "_load_k8s_config", lambda: None)
    monkeypatch.setattr(remediate_module.k8s_client, "AppsV1Api", lambda: apps)
    monkeypatch.setattr(remediate_module, "_record_gitops", lambda issue, action: {"ok": True})
    verify.PENDING._items.clear()
    r = remediate_module._guarded_revert_recent({"namespace": "user-a", "deployment": "web", "reason": "CrashLoopBackOff"})
    assert r["ok"] and r["action"] == "revert_recent_change" and "已退回你剛才的更新" in r["message"]
    # dry-run 先、再真的取代（取代才拿得掉新版多出來的環境變數）
    calls = apps.replace_namespaced_deployment.call_args_list
    assert len(calls) == 2 and calls[0].kwargs.get("dry_run") == "All"
    sent = calls[1].args[2]
    c = sent.spec.template.spec.containers[0]
    assert c.image == "app:1" and not c.env and "pod-template-hash" not in sent.spec.template.metadata.labels
    item = verify.PENDING.items()[0]
    assert item["full_template"] and item["snapshot"]["template"].spec.containers[0].image == "app:2"
    verify.PENDING._items.clear()


def test_rollback_of_full_template_uses_replace():
    apps = MagicMock()
    d = _deployment(image="app:1")
    apps.read_namespaced_deployment.return_value = d
    snap = verify.snapshot(_deployment(image="app:2"))
    r = verify.rollback(apps, {"namespace": "user-a", "deployment": "web", "snapshot": snap,
                               "rollback_allowed": True, "full_template": True})
    assert r["ok"] and apps.replace_namespaced_deployment.call_count == 2
    assert apps.replace_namespaced_deployment.call_args.args[2].spec.template.spec.containers[0].image == "app:2"


def test_revert_not_attempted_in_notify_only_namespace(monkeypatch):
    called = []
    monkeypatch.setattr(remediate_module, "_guarded_revert_recent", lambda issue: called.append(1))
    monkeypatch.setattr(remediate_module, "_resolve_owner", lambda issue: {"kind": "Deployment", "name": "web"})
    remediate_module.remediate({"pod_name": "web-x-y", "namespace": "default", "reason": "CrashLoopBackOff"},
                               {"action": "manual_inspect"})
    assert called == []


def test_revert_off_switch(monkeypatch):
    monkeypatch.setenv("HEALER_REVERT_RECENT", "0")
    assert not guard.revert_recent_enabled()


# ── 3.2 startupProbe ─────────────────────────────────────────────
def _probe(period=10, failure=3, delay=5):
    return k.V1Probe(http_get=k.V1HTTPGetAction(path="/", port=80), period_seconds=period,
                     failure_threshold=failure, initial_delay_seconds=delay)


def test_startup_probe_added_from_liveness():
    c = k.V1Container(name="app", liveness_probe=_probe())
    msg = remediate_module.startup_probe_plan(c)
    sp = c.startup_probe
    assert msg and sp.failure_threshold * sp.period_seconds >= 300 and sp.http_get.port == 80
    assert c.liveness_probe.initial_delay_seconds == 5      # liveness 本身不改


def test_startup_probe_threshold_raised_once():
    c = k.V1Container(name="app", startup_probe=_probe(period=5, failure=6))
    assert remediate_module.startup_probe_plan(c) and c.startup_probe.failure_threshold == 60
    assert remediate_module.startup_probe_plan(c) is None   # 已經夠長
    assert remediate_module.startup_probe_plan(k.V1Container(name="x")) is None
