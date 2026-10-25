"""部署請求重送不重複（2026-10-23，/api/deploy 冪等）：同一個請求編號、同一份規格成功過，就直接回傳上次的結果，
不再寫 Git、不再部署、不再記事件；失敗不記，重試會重新部署。"""
import json
import time
import uuid

import pytest

wd = pytest.importorskip("web_demo")

SPEC = {"app_name": "idem-web", "image": "nginx:1.27", "pods": 1, "port": 80}


@pytest.fixture
def env(monkeypatch):
    ns = "user-idem-" + uuid.uuid4().hex[:8]
    calls = {"k8s": 0, "git": 0, "sample": 0, "k8s_ok": True}

    def fake_k8s(*a, **kw):
        calls["k8s"] += 1
        return (True, "ok") if calls["k8s_ok"] else (False, "叢集沒有回應")

    def fake_git(*a, **kw):
        calls["git"] += 1
        return {"ok": True, "files": [], "commit_sha": "abc1234"}

    def fake_sample(*a, **kw):
        calls["sample"] += 1

    import gitops.manifest_writer as mw
    monkeypatch.setattr(mw, "write_manifest", fake_git)
    monkeypatch.setattr(wd, "K8S_ENABLED", True)
    monkeypatch.setattr(wd, "k8s_deploy", fake_k8s)
    monkeypatch.setattr(wd, "save_gold_sample", fake_sample)
    monkeypatch.setattr(wd, "_session_ns", lambda: ns)
    monkeypatch.setattr(wd, "_ensure_user_namespace", lambda ns: None)
    monkeypatch.setattr(wd, "_is_remote_ns", lambda ns: False)
    monkeypatch.setattr(wd, "_ws_assign_after_deploy", lambda *a, **kw: None)
    monkeypatch.setattr(wd, "_resource_summary", lambda *a, **kw: {})
    monkeypatch.setattr(wd, "_prepare_deploy", lambda text, spec: (dict(spec or SPEC), dict(spec or SPEC), {"decision": "approve"}, None))
    monkeypatch.setattr(wd, "_deploy_idem", {})
    from core.web_security import RateLimiter
    monkeypatch.setattr(wd, "_rate_limiter", RateLimiter())     # 每項測試重新計算部署次數上限
    calls["ns"] = ns
    return calls


def _client(user="alice"):
    c = wd.app.test_client()
    with c.session_transaction() as s:
        s["username"] = user
    return c


def _deploy(c, key=None, spec=SPEC):
    body = {"input": "部署 idem-web", "parsed": spec}
    if key:
        body["idempotency_key"] = key
    return c.post("/api/deploy", data=json.dumps(body), content_type="application/json")


def _deploy_events(ns):
    return [e for e in wd._events.query(ns) if e.get("kind") == "deploy"]


def test_same_key_deploys_once_and_replays(env):
    c = _client()
    r1, r2 = _deploy(c, "k1"), _deploy(c, "k1")
    assert r1.status_code == r2.status_code == 200
    assert not r1.get_json().get("replayed") and r2.get_json()["replayed"] is True
    assert r2.get_json()["k8s_deploy"]["ok"] is True
    assert env["k8s"] == env["git"] == 1
    time.sleep(0.2)                       # 樣本在背景執行緒存
    assert env["sample"] == 1
    assert len(_deploy_events(env["ns"])) == 1


def test_failure_not_cached_retry_deploys_again(env):
    c = _client()
    env["k8s_ok"] = False
    assert _deploy(c, "k2").get_json()["k8s_deploy"]["ok"] is False
    env["k8s_ok"] = True
    r = _deploy(c, "k2")
    assert not r.get_json().get("replayed") and r.get_json()["k8s_deploy"]["ok"] is True
    assert env["k8s"] == 2
    assert _deploy(c, "k2").get_json()["replayed"] is True and env["k8s"] == 2


def test_changed_spec_is_a_new_deploy(env):
    c = _client()
    _deploy(c, "k3")
    r = _deploy(c, "k3", dict(SPEC, pods=2))
    assert not r.get_json().get("replayed") and env["k8s"] == 2


def test_keys_are_per_user(env):
    _deploy(_client("alice"), "k4")
    r = _deploy(_client("bob"), "k4")
    assert not r.get_json().get("replayed") and env["k8s"] == 2


def test_without_key_behaves_as_before(env):
    c = _client()
    _deploy(c), _deploy(c)
    assert env["k8s"] == 2 and not wd._deploy_idem


def test_in_progress_duplicate_gets_409(env):
    c = _client()
    body = {"input": "部署 idem-web", "parsed": SPEC, "idempotency_key": "k5"}
    wd._deploy_idem[("alice", "k5")] = {"fp": wd._deploy_fingerprint(body), "state": "running", "at": time.time()}
    r = _deploy(c, "k5")
    assert r.status_code == 409 and r.get_json()["in_progress"] is True
    assert env["k8s"] == 0 and not _deploy_events(env["ns"])


def test_exception_clears_running_mark(env, monkeypatch):
    c = _client()
    def boom(*a, **kw):
        raise RuntimeError("壞掉")
    monkeypatch.setattr(wd, "_prepare_deploy", boom)
    try:
        assert _deploy(c, "k6").status_code == 500
    except RuntimeError:
        pass          # 其他測試把 Flask 設成直接拋出例外時，會走到這裡；兩種情況都要清掉執行中標記
    assert ("alice", "k6") not in wd._deploy_idem


def test_not_connected_is_a_failure_not_cached_and_logged_as_fail(env, monkeypatch):
    """2026-10-26 全系統測試：沒連上 K8s 時原本回 k8s_deploy=None，前端顯示部署完成、重送快取記成成功、
    事件記成部署成功；連上叢集後按「再試一次」會拿到舊結果，服務永遠不會啟動。"""
    monkeypatch.setattr(wd, "K8S_ENABLED", False)
    c = _client()
    r1 = _deploy(c, "nc1")
    kd = r1.get_json()["k8s_deploy"]
    assert kd["ok"] is False and kd["not_connected"] and "Kubernetes" in kd["message"]
    assert [e["outcome"] for e in _deploy_events(env["ns"])] == ["fail"]
    # 連上叢集後用同一個請求編號重送：要真的部署，不能回放上一次沒部署成功的結果
    monkeypatch.setattr(wd, "K8S_ENABLED", True)
    r2 = _deploy(c, "nc1")
    assert not r2.get_json().get("replayed") and r2.get_json()["k8s_deploy"]["ok"] is True
