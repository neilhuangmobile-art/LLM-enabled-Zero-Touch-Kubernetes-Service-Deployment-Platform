"""Healer 補強方案階段 0（healer/guard.py、diagnose／remediate 的保護，2026-11-25）。"""
import os
import sys
from types import SimpleNamespace as NS
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from healer import guard
from healer.diagnose import diagnose_issue

remediate_module = sys.modules["healer.remediate"]


def _ref(kind, name, controller=True):
    return NS(kind=kind, name=name, controller=controller)


def _pod(refs, ns="user-a"):
    return NS(metadata=NS(name="p", namespace=ns, owner_references=refs))


# ── 0.1 上層控制器 ────────────────────────────────────────────────
def test_owner_replicaset_to_deployment():
    apps = MagicMock()
    apps.read_namespaced_replica_set.return_value = NS(metadata=NS(owner_references=[_ref("Deployment", "my-app-v2")]))
    assert guard.resolve_owner(_pod([_ref("ReplicaSet", "my-app-v2-7d6b8c9f4")]), apps) == {"kind": "Deployment", "name": "my-app-v2"}


@pytest.mark.parametrize("refs,kind", [([_ref("StatefulSet", "db")], "StatefulSet"), ([_ref("Job", "backup-29")], "Job"),
                                       ([], None), ([_ref("DaemonSet", "agent")], "DaemonSet")])
def test_owner_not_deployment(refs, kind):
    assert guard.resolve_owner(_pod(refs), MagicMock())["kind"] == kind


def test_owner_replicaset_lookup_failure_does_not_guess():
    apps = MagicMock()
    apps.read_namespaced_replica_set.side_effect = Exception("boom")
    assert guard.resolve_owner(_pod([_ref("ReplicaSet", "web-abc")]), apps) == {"kind": "ReplicaSet", "name": "web-abc"}


def test_non_deployment_is_notify_only(monkeypatch):
    monkeypatch.setattr(remediate_module, "_resolve_owner", lambda issue: {"kind": "StatefulSet", "name": "db"})
    r = remediate_module.remediate({"pod_name": "db-0", "namespace": "user-a", "container": "db", "reason": "OOMKilled"},
                                   {"action": "increase_memory", "suggestion": "x"})
    assert r["action"] == "manual_inspect" and r["blocked_action"] == "increase_memory"


# ── 0.2 冷卻、上限、連續失敗 ──────────────────────────────────────
def test_limiter_cooldown_and_hourly_budget():
    lim = guard.Limiter(cooldown_s=600, per_hour=3)
    k = lim.key("ns", "web", "OOMKilled")
    assert lim.acquire(k, "ns", now=1000) is None
    assert "剛處理過" in lim.acquire(k, "ns", now=1100)
    assert lim.acquire(k, "ns", now=1700) is None                  # 冷卻過了
    assert lim.acquire(lim.key("ns", "api", "x"), "ns", now=1710) is None
    assert "一小時內" in lim.acquire(lim.key("ns", "db", "x"), "ns", now=1720)
    assert lim.acquire(lim.key("other", "db", "x"), "other", now=1720) is None   # 另一個帳號不受影響


def test_limiter_stops_after_two_failures():
    lim = guard.Limiter()
    k = lim.key("ns", "web", "CrashLoopBackOff")
    lim.failed(k)
    assert not lim.stopped(k)
    lim.failed(k)
    assert lim.stopped(k) and "連續" in lim.check(k, "ns")
    lim.succeeded(k)
    assert lim.check(k, "ns") is None


def test_limiter_release_returns_budget():
    lim = guard.Limiter(per_hour=1)
    k = lim.key("ns", "web", "x")
    assert lim.acquire(k, "ns", now=10) is None
    lim.release(k, "ns")
    assert lim.acquire(lim.key("ns", "api", "x"), "ns", now=11) is None


# ── 0.3 記憶體 ────────────────────────────────────────────────────
def test_memory_doubles_with_cap_and_keeps_guaranteed():
    plan, _ = guard.plan_memory_bump("1Gi", "1Gi", 0)
    assert plan["limit"] == "2048Mi" and plan["request"] == "2048Mi"        # Guaranteed 調完仍相等
    plan, _ = guard.plan_memory_bump("3Gi", "1536Mi", 1)
    assert plan["limit"] == "4096Mi" and plan["request"] == "2048Mi"        # 依比例、不超過 4Gi


@pytest.mark.parametrize("limit,bumps,word", [("4Gi", 0, "平台上限"), ("512Mi", 2, "2 次"), (None, 0, "沒有設定")])
def test_memory_refuses(limit, bumps, word):
    plan, why = guard.plan_memory_bump(limit, None, bumps)
    assert plan is None and word in why


def test_memory_cap_shared_with_customer_cluster():
    from cluster import remote_ops
    assert guard.MAX_MEMORY_MI == remote_ops.MAX_MEMORY_MI == 4096


# ── 0.4／0.6 結構化證據與規則 ──────────────────────────────────────
def _ctx(reason, logs="", message="", events=None, exit_code=None):
    return {"pod_name": "web-abc-xyz", "namespace": "user-a", "container": "web", "reason": reason,
            "message": message, "logs": logs, "events": events or [], "exit_code": exit_code}


def test_log_saying_oomkilled_does_not_trigger_memory():
    r = diagnose_issue(_ctx("CrashLoopBackOff", logs="FATAL OOMKilled out of memory, exiting"), use_llm=False)
    assert r["action"] != "increase_memory"


def test_real_oom_still_triggers_memory():
    r = diagnose_issue(_ctx("OOMKilled", exit_code=137), use_llm=False)
    assert r["action"] == "increase_memory"


def test_log_only_image_error_is_blocked():
    # 容器狀態正常重啟，只有 log 印出 ImagePullBackOff：不能觸發換映像檔
    r = diagnose_issue(_ctx("Error", logs="ImagePullBackOff manifest unknown"), use_llm=False)
    assert r["action"] not in guard.AUTO_ACTIONS


def test_http_403_access_log_is_not_permission_problem():
    r = diagnose_issue(_ctx("Error", logs='10.0.0.1 - - "GET /admin HTTP/1.1" 403 153'), use_llm=False)
    assert r["action"] != "fix_permissions"
    r = diagnose_issue(_ctx("Error", logs="Error: 403 Forbidden: serviceaccount cannot list pods"), use_llm=False)
    assert r["action"] == "fix_permissions"


def test_memory_limit_text_in_logs_is_not_oom():
    r = diagnose_issue(_ctx("Error", logs="config: memory limit set to 512MB"), use_llm=False)
    assert r["action"] != "increase_memory"


def test_guard_off_restores_old_behavior(monkeypatch):
    monkeypatch.setenv("HEALER_GUARD", "0")
    r = diagnose_issue(_ctx("CrashLoopBackOff", logs="OOMKilled"), use_llm=False)
    assert r["action"] == "increase_memory"


def test_rate_limited_pull_detected():
    assert guard.rate_limited_pull("failed to pull: 429 too many requests toomanyrequests")
    assert not guard.rate_limited_pull("manifest unknown")


# ── 0.7 namespace ────────────────────────────────────────────────
def test_namespace_scope(monkeypatch):
    from core import config
    monkeypatch.setattr(config, "HEALER_ONLY_NAMESPACES", set())
    monkeypatch.setattr(config, "HEALER_SKIP_NAMESPACES", set())
    assert not config.healer_owns_namespace("kube-system")
    assert not config.healer_owns_namespace("monitoring")
    assert not config.healer_owns_namespace("local-path-storage")         # Docker Desktop 的儲存元件
    assert not config.healer_owns_namespace("healer-eval-1-oom")          # 評估用的只有明確指定才處理
    assert config.healer_owns_namespace("user-a")
    monkeypatch.setattr(config, "HEALER_ONLY_NAMESPACES", {"healer-eval-1-oom"})
    assert config.healer_owns_namespace("healer-eval-1-oom")
    assert guard.notify_only_namespace("default")


# ── P7 曾經正常的映像檔 ─────────────────────────────────────────────
def _dep(image, avail=1, replicas=1, ann=None, gen=1, obs=1):
    return NS(metadata=NS(annotations=ann or {}, generation=gen),
              spec=NS(replicas=replicas, template=NS(spec=NS(containers=[NS(image=image)]))),
              status=NS(available_replicas=avail, updated_replicas=avail, observed_generation=obs, replicas=replicas,
                        unavailable_replicas=None if avail >= replicas else replicas - avail))


def test_last_good_image_only_when_healthy_and_changed():
    assert guard.last_good_update(_dep("nginx:1.27")) == {guard.ANN_LAST_GOOD_IMAGE: "nginx:1.27"}
    assert guard.last_good_update(_dep("nginx:1.27", ann={guard.ANN_LAST_GOOD_IMAGE: "nginx:1.27"})) is None
    assert guard.last_good_update(_dep("nginx:bad", avail=0)) is None
    assert guard.last_good_update(_dep("nginx:new", gen=2, obs=1)) is None   # 更新還沒完成
    # 2026-11-25 評估發現：映像檔改壞時舊 Pod 還在跑，available／updated 都滿，但多了一個卡住的 Pod，不能記成正常
    stuck = _dep("nginx:bad")
    stuck.status.replicas = 2
    assert guard.last_good_update(stuck) is None


def test_problem_class_groups_crash_reasons():
    assert guard.problem_class("Error") == guard.problem_class("CrashLoopBackOff") == "crash"
    assert guard.problem_class("ErrImagePull") == guard.problem_class("ImagePullBackOff")


# ── 2026-11-25 診斷誤判修正（缺 ConfigMap／指令找不到被判成映像檔問題）────────────────
def test_missing_configmap_is_not_image_problem():
    ev = [{"reason": "Pulled", "message": 'Container image "busybox:1.36" already present on machine'},
          {"reason": "Failed", "message": 'Error: configmap "app-config" not found'}]
    r = diagnose_issue(_ctx("CreateContainerConfigError", events=ev), use_llm=False)
    assert r["action"] == "create_config"
    assert not guard.supports("fix_image", guard.structured_text(_ctx("CreateContainerConfigError", events=ev)))


def test_command_not_found_exit_127():
    r = diagnose_issue(_ctx("CrashLoopBackOff", exit_code=127,
                            events=[{"reason": "BackOff", "message": "Back-off restarting failed container"}]), use_llm=False)
    assert r["action"] == "fix_command"
    r = diagnose_issue(_ctx("RunContainerError", message='exec: "srv": executable file not found in $PATH'), use_llm=False)
    assert r["action"] == "fix_command"


def test_exit_zero_is_not_rebuilt():
    r = diagnose_issue(_ctx("CrashLoopBackOff", exit_code=0), use_llm=False)
    assert r["action"] == "fix_command"


def test_real_image_error_still_fix_image():
    ev = [{"reason": "Failed", "message": 'Failed to pull image "nginx:1.999": manifest for nginx:1.999 not found: manifest unknown'}]
    r = diagnose_issue(_ctx("ImagePullBackOff", events=ev), use_llm=False)
    assert r["action"] == "fix_image"


def test_heap_oom_is_notify_only():
    r = diagnose_issue(_ctx("CrashLoopBackOff", exit_code=1, logs="Exception in thread main java.lang.OutOfMemoryError: Java heap space"),
                       use_llm=False)
    assert r["action"] == "tune_heap"
    # 容器真的被 OOMKilled 仍然調記憶體
    r = diagnose_issue({**_ctx("CrashLoopBackOff", exit_code=137, logs="java.lang.OutOfMemoryError"), "last_reason": "OOMKilled"},
                       use_llm=False)
    assert r["action"] == "increase_memory"


def test_exit_137_without_oom_note():
    r = diagnose_issue(_ctx("CrashLoopBackOff", exit_code=137), use_llm=False)
    assert r["action"] != "increase_memory" and "137" in r["root_cause"]


# ── 2026-11-25 冷卻／停手狀態在網站重啟後從事件紀錄還原 ─────────────────────────────
def test_limiter_restore_from_event_records():
    now = 100000.0
    k1, k2 = "user-a/web/crash", "user-a/api/oom"
    recs = [
        {"ts": now - 7200, "namespace": "user-a", "outcome": "ok", "data": {"action": "analyze_logs", "key": k1}},
        {"ts": now - 300, "namespace": "user-a", "outcome": "ok", "data": {"action": "increase_memory", "key": k2,
                                                                         "verify": "pending"}},
        {"ts": now - 200, "namespace": "user-a", "outcome": "fail", "data": {"action": "fix_image", "key": k1,
                                                                            "verify": "fail"}},
        {"ts": now - 100, "namespace": "user-a", "outcome": "inspect", "data": {"action": "manual_inspect"}},  # 舊紀錄沒有 key
    ]
    lim = guard.Limiter(per_hour=2)
    assert lim.restore(recs, now=now) == 2
    assert lim.stopped(k1)                                           # 驗證失敗 → 維持停手
    assert "剛處理過" in lim.check(k2, "user-a", now=now)             # 5 分鐘前動手 → 冷卻中
    assert lim.check("user-a/db/crash", "user-a", now=now) is None   # 額度只用掉 1 次（7200 秒前的不算）
    lim.restore([{"ts": now, "namespace": "user-a", "outcome": "ok", "data": {"key": k1, "verify": "ok"}}], now=now)
    assert not lim.stopped(k1)                                       # 之後驗證成功 → 解除
