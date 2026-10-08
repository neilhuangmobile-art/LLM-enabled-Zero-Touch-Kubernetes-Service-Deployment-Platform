"""
gitops/tenant_repo.py：客戶 repo 的每個操作都要變成正確的 commit。
遠端用本機 bare repo（不連 GitHub），GitHub API 用假的 session——CI 能跑，只需要 git。
"""
import shutil
import subprocess
import threading

import pytest
import yaml

from gitops.tenant_repo import (GitHubTenantHost, TenantRepo, TenantRepoError, tenant_slug)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="需要 git")


@pytest.fixture
def repo(tmp_path):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(remote)], check=True)
    r = TenantRepo("Alice_Test", str(remote), str(tmp_path / "work"))
    r.ensure_initialized()
    return r


def _remote_file(repo, path):
    """直接從遠端讀檔，確認真的 push 上去了（不是只改了本機副本）。"""
    out = subprocess.run(["git", "--git-dir", repo.remote_url, "show", f"main:{path}"],
                         capture_output=True, text=True, encoding="utf-8")
    return out.stdout if out.returncode == 0 else None


PARSED = {"app_name": "web", "image": "nginx:1.27", "pods": 2, "port": 80,
          "cpu": "100m", "memory": "128Mi"}


def test_slug_is_valid_namespace():
    assert tenant_slug("Alice_Test") == "zt-alice-test"
    assert tenant_slug("") == "zt-unknown"
    assert len(tenant_slug("x" * 200)) <= 53


def test_init_creates_platform_configmap_in_tenant_namespace(repo):
    info = yaml.safe_load(_remote_file(repo, "apps/_platform/zerotouch-info.yaml"))
    assert info["kind"] == "ConfigMap" and info["metadata"]["namespace"] == "zt-alice-test"
    assert repo.ensure_initialized() is None, "重複初始化不應產生新 commit"


def test_write_app_pushes_hardened_deployment_and_service(repo):
    res = repo.write_app(PARSED)
    assert res["commit"] and res["namespace"] == "zt-alice-test"
    dep = yaml.safe_load(_remote_file(repo, "apps/web/deployment.yaml"))
    assert dep["metadata"]["namespace"] == "zt-alice-test"
    assert dep["spec"]["replicas"] == 2
    c = dep["spec"]["template"]["spec"]["containers"][0]
    assert c["image"] == "nginx:1.27"
    assert c["securityContext"]["allowPrivilegeEscalation"] is False
    assert yaml.safe_load(_remote_file(repo, "apps/web/service.yaml"))["kind"] == "Service"


def test_scale_update_memory_restart(repo):
    repo.write_app(PARSED)
    assert repo.set_replicas("web", 5)["previous_replicas"] == 2
    assert repo.set_image("web", "nginx:1.28")["previous_image"] == "nginx:1.27"
    repo.set_memory_limit("web", "512Mi")
    repo.restart_app("web")
    dep = yaml.safe_load(_remote_file(repo, "apps/web/deployment.yaml"))
    c = dep["spec"]["template"]["spec"]["containers"][0]
    ann = dep["spec"]["template"]["metadata"]["annotations"]
    assert dep["spec"]["replicas"] == 5 and c["image"] == "nginx:1.28"
    assert c["resources"]["limits"]["memory"] == "512Mi"
    assert ann["zerotouch.k8s/previous-image"] == "nginx:1.27"
    assert "kubectl.kubernetes.io/restartedAt" in ann
    msgs = [h["message"] for h in repo.history("web")]
    assert any("scale to 5" in m for m in msgs) and all("Alice_Test" in m for m in msgs)


def test_revert_restores_previous_version_without_rewriting_history(repo):
    repo.write_app(PARSED)
    repo.set_image("web", "nginx:broken")
    before = len(repo.history())
    res = repo.revert_app("web")
    dep = yaml.safe_load(_remote_file(repo, "apps/web/deployment.yaml"))
    assert dep["spec"]["template"]["spec"]["containers"][0]["image"] == "nginx:1.27"
    assert len(repo.history()) == before + 1 and res["rolled_back_to"]


def test_revert_without_previous_version_is_friendly_error(repo):
    repo.write_app(PARSED)
    with pytest.raises(TenantRepoError, match="沒有可以回滾"):
        repo.revert_app("web")


def test_delete_app_removes_files(repo):
    repo.write_app(PARSED)
    repo.delete_app("web")
    assert _remote_file(repo, "apps/web/deployment.yaml") is None
    assert repo.list_apps() == []


@pytest.mark.parametrize("bad", ["../../etc", "_platform", "a/b", "..", "UPPER/../x"])
def test_path_traversal_and_platform_dir_rejected(repo, bad):
    with pytest.raises(TenantRepoError):
        repo.set_replicas(bad, 1)


def test_missing_app_is_friendly_error(repo):
    with pytest.raises(TenantRepoError, match="找不到"):
        repo.set_replicas("ghost", 1)


def test_concurrent_writes_from_two_clones_both_land(tmp_path, repo):
    """兩個人同時操作同一個帳號（兩個分頁）：每位客戶一把鎖，兩個 commit 都要在遠端。"""
    other = TenantRepo("Alice_Test", repo.remote_url, str(tmp_path / "work2"))
    errors = []

    def go(r, name):
        try:
            r.write_app(dict(PARSED, app_name=name))
        except Exception as e:  # pragma: no cover
            errors.append(e)

    ts = [threading.Thread(target=go, args=(repo, "app-a")),
          threading.Thread(target=go, args=(other, "app-b"))]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors
    assert set(repo.list_apps()) == {"app-a", "app-b"}


class FakeResp:
    def __init__(self, status, data=None):
        self.status_code, self._d, self.text = status, data, str(data)

    def json(self):
        return self._d


class FakeSession:
    def __init__(self):
        self.headers, self.calls = {}, []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw.get("json")))
        if url.endswith("/repos/org/zt-a") and method == "GET":
            return FakeResp(404) if len(self.calls) == 1 else FakeResp(200, {
                "full_name": "org/zt-a", "clone_url": "https://github.com/org/zt-a.git",
                "ssh_url": "git@github.com:org/zt-a.git", "default_branch": "main"})
        if url.endswith("/orgs/org/repos"):
            return FakeResp(201, {"full_name": "org/zt-a", "clone_url": "https://github.com/org/zt-a.git",
                                  "ssh_url": "git@github.com:org/zt-a.git", "default_branch": "main"})
        if url.endswith("/keys") and method == "GET":
            return FakeResp(200, [{"id": 7, "title": "zerotouch-flux"}, {"id": 8, "title": "other"}])
        if url.endswith("/keys") and method == "POST":
            return FakeResp(201, {})
        if url.endswith("/meta"):
            return FakeResp(200, {"ssh_keys": ["ssh-ed25519 AAAAfake"]})
        return FakeResp(204)


def test_github_host_creates_private_repo_and_read_only_key():
    s = FakeSession()
    host = GitHubTenantHost("tok", "org", session=s)
    info = host.ensure_repo("zt-a")
    assert info["ssh_url"] == "git@github.com:org/zt-a.git"
    create = next(j for m, u, j in s.calls if u.endswith("/orgs/org/repos"))
    assert create["private"] is True
    key = host.rotate_deploy_key("zt-a")
    post = next(j for m, u, j in s.calls if m == "POST" and u.endswith("/keys"))
    assert post["read_only"] is True, "deploy key 一定要唯讀"
    deleted = [u for m, u, _ in s.calls if m == "DELETE"]
    assert deleted == ["https://api.github.com/repos/org/zt-a/keys/7"], "只刪平台自己的舊 key"
    assert key["private_key"].startswith("-----BEGIN OPENSSH PRIVATE KEY-----")
    assert key["known_hosts"].startswith("github.com ssh-ed25519")


def test_github_host_requires_config():
    with pytest.raises(TenantRepoError, match="TENANT_GITHUB_TOKEN"):
        GitHubTenantHost("", "org")
