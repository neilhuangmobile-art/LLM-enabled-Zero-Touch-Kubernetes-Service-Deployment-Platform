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


def test_bundle_second_download_is_rejected(client, monkeypatch):
    monkeypatch.setattr(web_demo, "TENANT_GITHUB_TOKEN", "")
    tok = web_demo.CLUSTERS.issue_onboard_token("alice")
    client.get(f"/onboard/{tok}/bundle.yaml")
    r = client.get(f"/onboard/{tok}/bundle.yaml")
    assert r.status_code == 410 and "已經用過" in r.get_data(as_text=True)


def test_report_size_limit_holds_without_content_length(client):
    """chunked 傳輸沒有 Content-Length，不能因此繞過大小上限（背景安全審查：resource exhaustion）。"""
    import io
    rt = web_demo.CLUSTERS.issue_report_token("alice")
    big = json.dumps({**_report(), "pad": "x" * (400 * 1024)}).encode()
    r = client.post("/agent/report", input_stream=io.BytesIO(big),
                    headers={"Authorization": "Bearer " + rt, "Content-Type": "application/json",
                             "Transfer-Encoding": "chunked"})
    assert r.status_code in (400, 413)
    assert web_demo.CLUSTERS.latest_report("alice") == (None, None)


# ── 第 4 階段：引導流程與擋關 ──
@pytest.fixture
def customer_mode(client, monkeypatch):
    monkeypatch.setattr(web_demo, "CUSTOMER_CLUSTER_MODE", True)
    client.post("/auth/register", data={"username": "carol", "password": "hunter22", "confirm": "hunter22"})
    return client


def test_local_mode_has_no_gate(client):
    client.post("/auth/register", data={"username": "dave", "password": "hunter22", "confirm": "hunter22"})
    assert client.get("/").status_code == 200
    assert client.get("/api/pods").status_code == 200


def test_unconnected_user_is_sent_to_onboarding(customer_mode):
    c = customer_mode
    r = c.get("/")
    assert r.status_code == 302 and r.headers["Location"].endswith("/onboarding")
    r = c.get("/api/pods")
    assert r.status_code == 403 and "叢集連線" in r.get_json()["error"]
    assert c.get("/api/onboard/status").status_code == 200, "引導流程自己的 API 不能被擋"


def test_consent_page_then_decline_logs_out(customer_mode):
    c = customer_mode
    body = c.get("/onboarding").get_data(as_text=True)
    assert "授權同意書" in body and "不同意並離開" in body and "同意並繼續" in body
    from gitops.tenant_repo import tenant_slug
    assert tenant_slug("carol") in body, "同意書要寫出客戶專屬 namespace"
    c.post("/onboard/decline")
    assert c.get("/api/onboard/status").status_code == 401


def test_agree_then_install_page_shows_command_and_back_revokes(customer_mode, monkeypatch):
    monkeypatch.setattr(web_demo, "TENANT_GITHUB_TOKEN", "tok")
    monkeypatch.setattr(web_demo, "TENANT_GITHUB_ORG", "org")
    c = customer_mode
    r = c.post("/onboard/consent")
    assert r.headers["Location"].endswith("step=install")
    import html
    # Jinja 自動把引號轉成 &#34;（XSS 防護）；瀏覽器顯示與「複製」拿到的都是還原後的文字
    body = html.unescape(c.get("/onboarding?step=install").get_data(as_text=True))
    assert 'irm "' in body and "| iex" in body and "返回" in body and "繼續" in body
    with c.session_transaction() as s:
        tok = s["onboard_token"]
    assert web_demo.CLUSTERS.user_for_onboard_token(tok) == "carol"
    again = c.get("/onboarding?step=install").get_data(as_text=True)
    assert tok in again, "重新整理頁面沿用同一個指令，不能清掉安裝進度"
    c.post("/onboard/back")
    assert web_demo.CLUSTERS.user_for_onboard_token(tok) is None, "返回要作廢指令"


def test_install_page_without_github_explains_instead_of_command(customer_mode, monkeypatch):
    monkeypatch.setattr(web_demo, "TENANT_GITHUB_TOKEN", "")
    c = customer_mode
    c.post("/onboard/consent")
    body = c.get("/onboarding?step=install").get_data(as_text=True)
    assert "還沒設定完成" in body and "| iex" not in body


def test_check_status_json_and_unlock_after_connected(customer_mode):
    c = customer_mode
    c.post("/onboard/consent")
    d = c.get("/api/onboard/status").get_json()
    assert d["code"] == "not_started" and d["title"] and d["howto"] and d["ok"] is False
    web_demo.CLUSTERS.store_report("carol", __import__("cluster.remote_state", fromlist=["x"]).clean_report(_report()))
    d = c.get("/api/onboard/status").get_json()
    assert d["ok"] is True and d["code"] == "connected"
    assert c.get("/").status_code == 200, "連線成功後可以進網站"
    assert c.get("/api/pods").status_code == 200


def test_admin_is_never_gated(customer_mode, monkeypatch):
    monkeypatch.setattr(web_demo, "_is_admin_user", lambda u: True)
    assert customer_mode.get("/").status_code == 200
