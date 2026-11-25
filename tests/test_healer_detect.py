"""Healer 補強方案階段 2：擴大偵測（healer/detect.py，2026-11-25）。只通知，不改設定。"""
import os
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from healer import detect

NOW = datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc)
OLD = NOW - timedelta(hours=1)


def _meta(name, ns="user-a", labels=None, owners=None, ann=None, created=OLD):
    return NS(name=name, namespace=ns, labels=labels or {}, owner_references=owners or [], annotations=ann or {},
              creation_timestamp=created)


def _cond(typ, status, reason=None, message=None, since=OLD):
    return NS(type=typ, status=status, reason=reason, message=message, last_transition_time=since)


def _cs(name="app", running=False, waiting=None, terminated=None, restarts=0):
    return NS(name=name, restart_count=restarts,
              state=NS(running=NS() if running else None, waiting=waiting, terminated=terminated))


def _pod(name="web-abc-12345", phase="Running", conds=None, statuses=None, init=None, reason=None, labels=None,
         ports=(80,), rs="web-abc", created=OLD):
    return NS(metadata=_meta(name, labels=labels or {"app": "web"}, owners=[NS(kind="ReplicaSet", name=rs)], created=created),
              spec=NS(containers=[NS(ports=[NS(container_port=p, name=None) for p in ports])]),
              status=NS(phase=phase, conditions=conds or [], container_statuses=statuses or [],
                        init_container_statuses=init or [], reason=reason, message=None))


RS = [NS(metadata=NS(name="web-abc", namespace="user-a", owner_references=[NS(kind="Deployment", name="web")]))]


def _run(**kw):
    args = dict(pods=[], events=[], deployments=[], services=[], slices=[], pvcs=[], storage_classes=[], replica_sets=RS)
    args.update(kw)
    return detect.analyze(now=NOW, **args)


def _codes(fs):
    return {f["code"] for f in fs}


def test_unschedulable():
    p = _pod(phase="Pending", conds=[_cond("PodScheduled", "False", "Unschedulable", "0/1 nodes are available: 1 Insufficient cpu.")])
    fs = _run(pods=[p])
    assert _codes(fs) == {"unschedulable"} and fs[0]["app"] == "web"


def test_pending_just_scheduled_is_fine():
    assert _run(pods=[_pod(phase="Pending", conds=[_cond("PodScheduled", "True")])]) == []


def test_pvc_unbound_missing_class_but_not_wait_for_first_consumer():
    sc = [NS(metadata=NS(name="hostpath", annotations={"storageclass.kubernetes.io/is-default-class": "true"}),
             volume_binding_mode="WaitForFirstConsumer")]
    bad = NS(metadata=_meta("data", created=OLD), spec=NS(storage_class_name="fast-ssd"), status=NS(phase="Pending"))
    wffc = NS(metadata=_meta("cache", created=OLD), spec=NS(storage_class_name=None), status=NS(phase="Pending"))
    fs = _run(pvcs=[bad, wffc], storage_classes=sc)
    assert [f["app"] for f in fs] == ["data"] and "fast-ssd" in fs[0]["root_cause"]


def test_not_ready_after_threshold_only():
    p = _pod(conds=[_cond("Ready", "False", since=NOW - timedelta(minutes=20))], statuses=[_cs(running=True)])
    assert _codes(_run(pods=[p])) == {"not_ready"}
    young = _pod(conds=[_cond("Ready", "False", since=NOW - timedelta(minutes=5))], statuses=[_cs(running=True)])
    assert _run(pods=[young]) == []


def _svc(sel, port=80, target=80, ann=None):
    return NS(metadata=_meta("web-svc", ann=ann), spec=NS(selector=sel, type="ClusterIP",
                                                          ports=[NS(port=port, target_port=target)]))


def test_service_selector_mismatch_names_the_labels():
    fs = _run(pods=[_pod()], services=[_svc({"app": "wbe"})])
    assert _codes(fs) == {"no_endpoints"} and "app=web" in fs[0]["suggestion"]


def test_service_target_port_mismatch():
    fs = _run(pods=[_pod(ports=(80,))], services=[_svc({"app": "web"}, target=8080)])
    assert _codes(fs) == {"no_endpoints"} and "8080" in fs[0]["evidence"]


def test_service_ok_or_sleeping_or_scaled_to_zero():
    assert _run(pods=[_pod()], services=[_svc({"app": "web"})]) == []
    assert _run(services=[_svc({"app": "web"}, ann={detect.ANN_ORIG_SELECTOR: "{}"})]) == []
    dep0 = NS(metadata=_meta("web"), spec=NS(replicas=0, template=NS(metadata=NS(labels={"app": "web"}))),
              status=NS(conditions=[]))
    assert _run(services=[_svc({"app": "web"})], deployments=[dep0]) == []


def test_rollout_stuck():
    d = NS(metadata=_meta("web"), spec=NS(replicas=1, template=NS(metadata=NS(labels={}))),
           status=NS(conditions=[_cond("Progressing", "False", "ProgressDeadlineExceeded", "ReplicaSet has timed out progressing.")]))
    assert _codes(_run(deployments=[d])) == {"rollout_stuck"}


def test_mount_failed_needs_age_and_event():
    p = _pod(phase="Pending", statuses=[_cs(waiting=NS(reason="ContainerCreating", message=None))])
    ev = NS(type="Warning", reason="FailedMount", message='MountVolume.SetUp failed for volume "cfg" : configmap "cfg" not found',
            involved_object=NS(namespace="user-a", kind="Pod", name=p.metadata.name))
    assert _codes(_run(pods=[p], events=[ev])) == {"mount_failed"}
    assert _run(pods=[p]) == []
    young = _pod(phase="Pending", statuses=[_cs(waiting=NS(reason="ContainerCreating", message=None))],
                 created=NOW - timedelta(minutes=1))
    assert _run(pods=[young], events=[ev]) == []


def test_init_failed_and_evicted():
    p = _pod(phase="Pending", init=[_cs("wait-db", waiting=NS(reason="CrashLoopBackOff", message="back-off"), restarts=3)])
    assert _codes(_run(pods=[p])) == {"init_failed"}
    assert _codes(_run(pods=[_pod(phase="Failed", reason="Evicted")])) == {"evicted"}


def test_quota_exceeded_maps_replicaset_to_deployment():
    ev = NS(type="Warning", reason="FailedCreate", message='pods "web-abc-x" is forbidden: exceeded quota: q, requested: cpu=2',
            involved_object=NS(namespace="user-a", kind="ReplicaSet", name="web-abc"))
    fs = _run(events=[ev])
    assert _codes(fs) == {"quota_exceeded"} and fs[0]["app"] == "web"


def test_namespace_scope_and_waker_skipped():
    p = _pod(phase="Failed", reason="Evicted")
    assert _run(pods=[p], owns_ns=lambda ns: False) == []
    w = _pod(phase="Failed", reason="Evicted", labels={"zerotouch/waker": "web"})
    assert _run(pods=[w]) == []
