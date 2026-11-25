"""客戶叢集的 Healer 修復驗證與退回（cluster/remote_verify.py、cluster/remote_ops.heal 的共用規則，2026-11-25）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cluster import remote_ops as rops
from cluster import remote_verify as rv


def _rep(memory="512Mi", image="api:1", replicas=2, ready=2, available=2, conditions=None):
    return {"deployments": [{"name": "api", "replicas": replicas, "ready": ready, "available": available,
                             "image": image, "memory_limit": memory, "conditions": conditions or []}]}


def _item(expect, action="increase_memory", sent=1000.0):
    p = rv.RemotePending()
    p.register("zt-a/api/oom", "zt-a", "api", action, expect, now=sent)
    return p.items()[0]


def test_waits_for_sync_then_healthy_hold():
    it = _item({"memory_limit": "512Mi"})
    assert rv.evaluate(_rep(memory="256Mi"), it, set(), now=1030)["state"] == "wait"      # 還沒同步
    assert rv.evaluate(_rep(), it, set(), now=1060)["state"] == "wait"                    # 同步了，觀察中
    assert rv.evaluate(_rep(), it, set(), now=1100)["state"] == "wait"
    assert rv.evaluate(_rep(), it, set(), now=1160)["state"] == "ok"                      # 健康超過 90 秒


def test_unhealthy_resets_hold_and_fails_after_deadline():
    it = _item({"memory_limit": "512Mi"})
    rv.evaluate(_rep(), it, set(), now=1060)
    assert rv.evaluate(_rep(), it, {"api"}, now=1120)["state"] == "wait" and it["healthy_since"] is None
    r = rv.evaluate(_rep(), it, {"api"}, now=1000 + rv.DEADLINE_S + 1)
    assert r["state"] == "fail" and "不正常" in r["why"]


def test_never_synced_fails_with_flux_hint():
    it = _item({"memory_limit": "512Mi"})
    r = rv.evaluate(_rep(memory="256Mi"), it, set(), now=1000 + rv.DEADLINE_S + 1)
    assert r["state"] == "fail" and "Flux" in r["why"]


def test_stuck_rollout_is_not_healthy():
    it = _item({"image": "api:1"}, action="fix_image")
    rep = _rep(conditions=["Progressing=False: ProgressDeadlineExceeded"])
    assert rv.evaluate(rep, it, set(), now=1500)["why"] == "更新卡住"


def test_restart_has_no_field_waits_minimum():
    it = _item({}, action="analyze_logs")
    assert rv.evaluate(_rep(), it, set(), now=1030)["state"] == "wait"
    rv.evaluate(_rep(), it, set(), now=1061)
    assert rv.evaluate(_rep(), it, set(), now=1152)["state"] == "ok"


def test_rollback_only_when_config_still_ours():
    it = _item({"memory_limit": "512Mi"})
    assert rv.can_rollback(_rep(), it)
    assert not rv.can_rollback(_rep(memory="1Gi"), it)                      # 使用者後來又改過
    assert not rv.can_rollback(_rep(), _item({}, action="analyze_logs"))    # 重新啟動沒有東西可以退


# ── remote_ops.heal 和本機共用的規則 ──
class Repo:
    def __init__(self):
        self.calls = []

    def restart_app(self, app):
        self.calls.append("restart"); return {"commit": "c"}

    def set_memory_limit(self, app, mem):
        self.calls.append(("memory", mem)); return {"commit": "c"}

    def restore_previous_image(self, app):
        self.calls.append("image"); return {"commit": "c", "restored_image": "api:1", "previous_image": "api:bad"}


ISSUE = {"pod_name": "api-x-y", "app": "api", "reason": "CrashLoopBackOff"}


def test_single_replica_is_not_restarted():
    repo = Repo()
    r = rops.heal(repo, ISSUE, {"action": "analyze_logs"}, replicas=1)
    assert r["action"] == "manual_inspect" and repo.calls == []
    r = rops.heal(repo, ISSUE, {"action": "analyze_logs"}, replicas=3)
    assert r["pending_sync"] and r["expect"] == {}


def test_memory_bump_limit_and_no_limit_set():
    repo = Repo()
    assert rops.heal(repo, ISSUE, {"action": "increase_memory"}, current_memory="256Mi", memory_bumps=2)["ok"] is False
    assert rops.heal(repo, ISSUE, {"action": "increase_memory"}, current_memory=None)["ok"] is False
    assert repo.calls == []
    r = rops.heal(repo, ISSUE, {"action": "increase_memory"}, current_memory="256Mi", memory_bumps=1)
    assert r["expect"] == {"memory_limit": "512Mi"} and repo.calls == [("memory", "512Mi")]


def test_image_restore_expect():
    r = rops.heal(Repo(), ISSUE, {"action": "fix_image"})
    assert r["expect"] == {"image": "api:1"}
