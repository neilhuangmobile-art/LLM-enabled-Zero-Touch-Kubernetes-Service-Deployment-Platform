"""
cluster/remote_state.py：一次性 token、安裝進度、回報驗證、連線檢查頁的六種狀況。
純邏輯，用假時鐘，CI 能跑。
"""
import json

import pytest

from cluster.remote_state import (ClusterRegistry, ReportError, clean_report, connection_status,
                                  status_text, MAX_REPORT_BYTES)


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


def _good_report(**over):
    rep = {"agent_version": "1", "namespace": "zt-a",
           "flux": {"source": {"ready": True, "reason": "Succeeded", "message": "ok"},
                    "kustomization": {"ready": True, "reason": "ReconciliationSucceeded", "message": "ok"}},
           "platform_configmap": True, "deployments": [], "pods": [], "events": []}
    rep.update(over)
    return clean_report(rep)


# ── token ──
def test_onboard_token_hashed_single_active_and_expires(tmp_path):
    clk = Clock()
    reg = ClusterRegistry(str(tmp_path / "c.json"), clock=clk)
    t1 = reg.issue_onboard_token("alice")
    assert reg.user_for_onboard_token(t1) == "alice"
    assert t1 not in (tmp_path / "c.json").read_text(), "token 原文不能落地"
    t2 = reg.issue_onboard_token("alice")
    assert reg.user_for_onboard_token(t1) is None, "重新產生後舊 token 作廢"
    assert reg.user_for_onboard_token(t2) == "alice"
    clk.t += 31 * 60
    assert reg.user_for_onboard_token(t2) is None, "30 分鐘後過期"


def test_revoke_and_wrong_tokens(tmp_path):
    reg = ClusterRegistry(str(tmp_path / "c.json"), clock=Clock())
    t = reg.issue_onboard_token("alice")
    reg.revoke_onboard_tokens("alice")
    assert reg.user_for_onboard_token(t) is None
    assert reg.user_for_onboard_token("") is None and reg.user_for_report_token("x") is None


def test_report_token_maps_to_user_and_persists(tmp_path):
    p = str(tmp_path / "c.json")
    reg = ClusterRegistry(p, clock=Clock())
    rt = reg.issue_report_token("bob")
    assert ClusterRegistry(p).user_for_report_token(rt) == "bob", "重啟後還認得"


def test_tokens_of_two_users_are_independent(tmp_path):
    reg = ClusterRegistry(str(tmp_path / "c.json"), clock=Clock())
    ta, tb = reg.issue_onboard_token("a"), reg.issue_onboard_token("b")
    assert reg.user_for_onboard_token(ta) == "a" and reg.user_for_onboard_token(tb) == "b"


def test_consent_versioned(tmp_path):
    reg = ClusterRegistry(str(tmp_path / "c.json"), clock=Clock())
    assert not reg.has_consent("a")
    reg.record_consent("a")
    assert reg.has_consent("a") and not reg.has_consent("a", version="future")


# ── 回報驗證 ──
def test_clean_report_keeps_whitelist_and_truncates():
    rep = clean_report({
        "flux": {"source": {"ready": True}}, "evil": "x" * 10,
        "pods": [{"name": "p" * 1000, "phase": "Running", "logs": "\n".join(f"l{i}" for i in range(500)),
                  "containers": [{"name": "c", "restart_count": "3", "reason": "OOMKilled"}]}] * 400,
        "events": [{"reason": "BackOff", "message": "m" * 5000}],
    })
    assert "evil" not in rep
    assert len(rep["pods"]) == 300
    p = rep["pods"][0]
    assert len(p["name"]) <= 254 and p["logs"].count("\n") <= 49
    assert p["containers"][0]["restart_count"] == 3
    assert len(rep["events"][0]["message"]) <= 301
    assert rep["flux"]["kustomization"]["ready"] is False, "缺欄位視為未就緒"


def test_clean_report_rejects_non_object():
    with pytest.raises(ReportError):
        clean_report(["not", "a", "dict"])


def test_max_report_bytes_is_sane():
    assert 64 * 1024 <= MAX_REPORT_BYTES <= 1024 * 1024


# ── 連線檢查頁的狀況 ──
NOW = 2_000_000.0


@pytest.mark.parametrize("progress,code", [
    ([], "not_started"),
    ([{"step": "kubectl", "ok": False, "detail": "not found"}], "no_kubectl"),
    ([{"step": "cluster", "ok": False}], "cluster_unreachable"),
    ([{"step": "flux", "ok": False}], "flux_failed"),
    ([{"step": "crds", "ok": False}], "flux_failed"),
    ([{"step": "bundle", "ok": False}], "bundle_failed"),
    ([{"step": "reporter", "ok": False}], "reporter_not_ready"),
    ([{"step": "done", "ok": True, "at": NOW - 10}], "reporter_not_ready"),
    # 腳本還在跑（例如剛裝完 Flux、在等它啟動）：顯示安裝中，不要說回報程式壞了
    ([{"step": "kubectl", "ok": True}, {"step": "flux", "ok": True}], "installing"),
    ([{"step": "done", "ok": True, "at": NOW - 300}], "no_report"),
])
def test_status_from_install_progress(progress, code):
    assert connection_status(progress, None, None, NOW)["code"] == code


def test_status_from_reports():
    assert connection_status([], _good_report(), NOW - 5, NOW)["code"] == "connected"
    bad_src = _good_report(flux={"source": {"ready": False, "message": "auth failed"},
                                 "kustomization": {"ready": False}})
    st = connection_status([], bad_src, NOW - 5, NOW)
    assert st["code"] == "git_not_ready" and "auth failed" in st["detail"]
    assert connection_status([], _good_report(platform_configmap=False), NOW - 5, NOW)["code"] == "sync_pending"
    assert connection_status([], _good_report(), NOW - 600, NOW)["code"] == "offline"


def test_ever_connected_without_report_is_offline_not_reinstall():
    assert connection_status([], None, None, NOW, ever_connected=True)["code"] == "offline"


def test_just_restarted_platform_waits_instead_of_alarming():
    st = connection_status([], None, None, NOW, ever_connected=True, platform_started=NOW - 10)
    assert st["code"] == "awaiting_report"
    st = connection_status([], None, None, NOW, ever_connected=True, platform_started=NOW - 600)
    assert st["code"] == "offline"


def test_registry_marks_connected_once(tmp_path):
    clk = Clock()
    reg = ClusterRegistry(str(tmp_path / "c.json"), clock=clk)
    reg.store_report("alice", _good_report())
    assert reg.status("alice")["ok"] and reg.ever_connected("alice")
    clk.t += 600
    assert reg.status("alice")["code"] == "offline"
    data = json.loads((tmp_path / "c.json").read_text(encoding="utf-8"))
    assert data["alice"]["connected_at"]


def test_every_status_has_bilingual_text_with_namespace():
    for code in ["awaiting_report", "installing", "not_started", "no_kubectl", "cluster_unreachable", "flux_failed", "bundle_failed",
                 "reporter_not_ready", "no_report", "git_not_ready", "sync_pending", "offline", "connected"]:
        title, howto = status_text(code, "zt-alice-1234")
        assert " / " in title and howto
        assert "{ns}" not in howto


def test_forget_invalidates_everything(tmp_path):
    reg = ClusterRegistry(str(tmp_path / "c.json"), clock=Clock())
    rt = reg.issue_report_token("eve")
    reg.store_report("eve", _good_report())
    reg.forget("eve")
    assert reg.user_for_report_token(rt) is None and reg.latest_report("eve") == (None, None)


def test_bundle_can_be_claimed_only_once(tmp_path):
    """安裝包含 deploy key 與回報 token，只能下載一次（2026-10-09 背景安全審查：token replay）。"""
    reg = ClusterRegistry(str(tmp_path / "c.json"), clock=Clock())
    t = reg.issue_onboard_token("alice")
    assert reg.claim_bundle(t) == "alice"
    assert reg.claim_bundle(t) is None, "第二次領取要被拒絕"
    assert reg.user_for_onboard_token(t) == "alice", "token 仍可用來回報安裝進度"
    t2 = reg.issue_onboard_token("alice")
    assert reg.claim_bundle(t2) == "alice", "新指令可以再領一次"
