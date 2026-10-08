"""
客戶自帶叢集：安裝腳本 / 安裝包 / 進度 / 回報器回報的路由，以及 cluster/onboarding.py 產生的內容。
web_demo 需要 flask 等重依賴，缺的時候整檔跳過（照 CLAUDE.md 的 importorskip 寫法）。
"""
import json

import pytest
import yaml

web_demo = pytest.importorskip("web_demo")
from cluster.onboarding import build_bundle, build_install_script  # noqa: E402
from cluster.remote_state import ClusterRegistry  # noqa: E402
from core.web_security import RateLimiter  # noqa: E402

NS = "zt-alice-0123456789abcdef"
KEY = {"private_key": "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----\n",
       "known_hosts": "github.com ssh-ed25519 AAAA\n"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(web_demo, "K8S_ENABLED", False)
    monkeypatch.setattr(web_demo, "USERS", {"alice": {"password_hash": "x"}})
    monkeypatch.setattr(web_demo, "__file__", str(tmp_path / "web_demo.py"))
    monkeypatch.setattr(web_demo, "_rate_limiter", RateLimiter())
    monkeypatch.setattr(web_demo, "CLUSTERS", ClusterRegistry(str(tmp_path / "clusters.json")))
    web_demo.app.config["TESTING"] = True
    with web_demo.app.test_client() as c:
        yield c


# ── 產生的內容 ──
def test_bundle_is_namespace_scoped_and_readonly_reporter():
    docs = list(yaml.safe_load_all(build_bundle(NS, "alice", "https://x.ts.net",
                                                "git@github.com:org/r.git", KEY, "tok")))
    kinds = {(d["kind"], d["metadata"]["name"]) for d in docs}
    assert ("Namespace", NS) in kinds
    ns_doc = next(d for d in docs if d["kind"] == "Namespace")
    assert ns_doc["metadata"]["labels"]["pod-security.kubernetes.io/enforce"] == "baseline"
    for d in docs:
        if d["kind"] not in ("Namespace", "ClusterRole", "ClusterRoleBinding"):
            assert d["metadata"]["namespace"] == NS, d["kind"]
    ks = next(d for d in docs if d["kind"] == "Kustomization")
    assert ks["spec"]["serviceAccountName"] == "zt-flux-applier" and ks["spec"]["targetNamespace"] == NS
    applier = next(d for d in docs if d["kind"] == "RoleBinding" and d["metadata"]["name"] == "zt-flux-applier")
    assert applier["roleRef"]["kind"] == "ClusterRole" and applier["roleRef"]["name"] == "admin", \
        "admin 綁在 RoleBinding（只限這個 namespace），不是 ClusterRoleBinding"
    role = next(d for d in docs if d["kind"] == "Role")
    verbs = {v for r in role["rules"] for v in r["verbs"]}
    assert verbs <= {"get", "list"}, "回報器只能讀"
    cr = next(d for d in docs if d["kind"] == "ClusterRole")
    assert cr["rules"] == [{"apiGroups": [""], "resources": ["nodes"], "verbs": ["get", "list"]}]
    src = next(d for d in docs if d["kind"] == "GitRepository")
    assert src["spec"]["url"] == "ssh://git@github.com/org/r.git"
    dep = next(d for d in docs if d["kind"] == "Deployment")
    c = dep["spec"]["template"]["spec"]["containers"][0]
    assert c["securityContext"]["readOnlyRootFilesystem"] is True
    assert dep["spec"]["template"]["spec"]["securityContext"]["runAsNonRoot"] is True


def test_bundle_rejects_bad_namespace_or_url():
    with pytest.raises(ValueError):
        build_bundle("../evil", "a", "https://x", "git@github.com:o/r.git", KEY, "t")
    with pytest.raises(ValueError):
        build_bundle(NS, "a", "https://x'; rm -rf /", "git@github.com:o/r.git", KEY, "t")


def test_script_rejects_injection_in_token():
    with pytest.raises(ValueError):
        build_install_script("https://x.ts.net", "abc'; Remove-Item C:\\ -Recurse #" + "a" * 20, NS)


def test_script_never_exits_powershell_and_reports_each_step():
    s = build_install_script("https://x.ts.net", "A" * 32, NS)
    assert "exit" not in s.lower().replace("exitcode", "")
    for step in ("start", "kubectl", "cluster", "flux", "crds", "bundle", "done"):
        assert f"'{step}'" in s


# ── 路由 ──
def test_expired_script_is_friendly_powershell_not_html(client):
    r = client.get("/onboard/" + "x" * 32 + ".ps1")
    assert r.status_code == 200 and "過期" in r.get_data(as_text=True)
    assert "<html" not in r.get_data(as_text=True).lower()


def test_valid_script_contains_namespace_and_token(client):
    tok = web_demo.CLUSTERS.issue_onboard_token("alice")
    body = client.get(f"/onboard/{tok}.ps1").get_data(as_text=True)
    from gitops.tenant_repo import tenant_slug
    assert tok in body and tenant_slug("alice") in body


def test_bundle_without_github_config_is_friendly_503(client, monkeypatch):
    monkeypatch.setattr(web_demo, "TENANT_GITHUB_TOKEN", "")
    tok = web_demo.CLUSTERS.issue_onboard_token("alice")
    r = client.get(f"/onboard/{tok}/bundle.yaml")
    assert r.status_code == 503 and "請稍後再試" in r.get_data(as_text=True)


def test_progress_is_recorded_and_drives_status(client):
    tok = web_demo.CLUSTERS.issue_onboard_token("alice")
    r = client.post(f"/onboard/{tok}/progress", json={"step": "cluster", "ok": False, "detail": "refused"})
    assert r.status_code == 204
    assert web_demo.CLUSTERS.status("alice")["code"] == "cluster_unreachable"
    assert client.post("/onboard/bad/progress", json={}).status_code == 410


def _report():
    return {"agent_version": "1", "namespace": NS, "platform_configmap": True,
            "flux": {"source": {"ready": True}, "kustomization": {"ready": True}},
            "pods": [], "deployments": [], "events": []}


def test_agent_report_auth_size_and_validation(client):
    assert client.post("/agent/report", json=_report()).status_code == 401
    rt = web_demo.CLUSTERS.issue_report_token("alice")
    h = {"Authorization": "Bearer " + rt}
    assert client.post("/agent/report", data="not json", headers=h).status_code == 400
    big = json.dumps({**_report(), "pad": "x" * (300 * 1024)})
    assert client.post("/agent/report", data=big, headers=h,
                       content_type="application/json").status_code == 413
    assert client.post("/agent/report", json=_report(), headers=h).status_code == 204
    assert web_demo.CLUSTERS.status("alice")["code"] == "connected"


def test_banned_user_report_token_revoked(client):
    rt = web_demo.CLUSTERS.issue_report_token("alice")
    web_demo._ban_user("alice")
    r = client.post("/agent/report", json=_report(), headers={"Authorization": "Bearer " + rt})
    assert r.status_code in (401, 403)


def test_cluster_base_url_rewrites_localhost_for_pods(monkeypatch):
    monkeypatch.setattr(web_demo, "PUBLIC_BASE_URL", "")
    with web_demo.app.test_request_context("/", base_url="http://localhost:5050"):
        assert web_demo._cluster_base_url() == "http://host.docker.internal:5050"
        assert web_demo._script_base_url() == "http://localhost:5050"
