"""2026-11-11：部署紀錄寫到獨立的 git repo，不再混進原始碼 repo（docs/security_review.md、CHANGELOG 同日）。"""
import importlib
import os
import shutil
import subprocess

import pytest

pytest.importorskip("yaml")
if shutil.which("git") is None:
    pytest.skip("需要 git 指令", allow_module_level=True)

from gitops.manifest_writer import ensure_gitops_repo, write_manifest


def _git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, encoding="utf-8").stdout.strip()


def test_new_repo_is_initialised_and_imports_old_manifests(tmp_path):
    old = tmp_path / "src" / "manifests" / "user-a" / "web"
    old.mkdir(parents=True)
    (old / "deployment.yaml").write_text("kind: Deployment\n", encoding="utf-8")
    repo = tmp_path / "gitops"

    assert ensure_gitops_repo(str(repo), import_from=str(tmp_path / "src" / "manifests")) is True
    assert (repo / ".git").exists()
    assert (repo / "manifests" / "user-a" / "web" / "deployment.yaml").exists()
    assert _git(repo, "status", "--porcelain") == ""          # 匯入的內容已經 commit
    assert _git(repo, "rev-list", "--count", "HEAD") == "1"


def test_existing_repo_is_left_alone(tmp_path):
    repo = tmp_path / "gitops"
    ensure_gitops_repo(str(repo))
    assert ensure_gitops_repo(str(repo)) is False
    assert _git(repo, "rev-list", "--count", "HEAD") == "1"


def test_deploy_commit_goes_to_gitops_repo_not_parent(tmp_path):
    parent = tmp_path / "code"
    parent.mkdir()
    _git(parent, "init", "-q")
    repo = parent / "data" / "gitops"                         # 跟正式環境一樣放在原始碼 repo 裡面
    ensure_gitops_repo(str(repo))

    result = write_manifest({"app_name": "web", "image": "nginx:1.27", "pods": 1, "port": 80},
                            repo_path=str(repo), namespace="user-a", commit=True)
    assert result["ok"] and result["commit_sha"]
    assert "gitops: deploy web to user-a" in _git(repo, "log", "-1", "--format=%s")
    assert _git(parent, "rev-list", "--all", "--count") == "0"   # 原始碼 repo 沒有任何 commit


@pytest.mark.parametrize("value", ["", "."])
def test_config_never_points_at_source_repo(monkeypatch, value):
    monkeypatch.setenv("GITOPS_REPO_PATH", value)
    import core.config as cfg
    cfg = importlib.reload(cfg)
    try:
        assert os.path.normcase(cfg.GITOPS_REPO_PATH) == os.path.normcase(os.path.join(cfg.ROOT, "data", "gitops"))
    finally:
        monkeypatch.undo()
        importlib.reload(cfg)
