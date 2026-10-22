"""部署／回滾的 Git 提交（gitops/manifest_writer.commit_paths）。

2026-10-23 全系統檢查：原本用 GitPython 的 index.commit()，它在 Windows 上執行 pre-commit hook 會呼叫到
WSL 的 bash 而失敗，10/9 之後本機模式的部署與回滾都沒有留下 Git 紀錄。改用 git 指令後的行為鎖在這裡。
"""
import os
import shutil
import subprocess

import pytest

from gitops.manifest_writer import commit_paths

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="需要 git 指令")


def _git(d, *args):
    return subprocess.run(["git", "-C", str(d), *args], capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "seed.txt").write_text("seed")
    _git(tmp_path, "add", "seed.txt")
    _git(tmp_path, "commit", "-q", "-m", "seed")
    return tmp_path


def _hook(repo, body):
    h = repo / ".git" / "hooks" / "pre-commit"
    # CI 是 Python 3.9：Path.write_text 沒有 newline 參數（3.10 才有）
    with open(h, "w", newline="\n") as f:
        f.write("#!/bin/sh\n" + body + "\n")
    os.chmod(h, 0o755)


def test_commits_only_given_paths(repo):
    """使用者暫存區裡原本有別的東西：不能被部署的 commit 一起帶走。"""
    (repo / "other.txt").write_text("not mine")
    _git(repo, "add", "other.txt")
    d = repo / "manifests" / "ns" / "web"
    d.mkdir(parents=True)
    (d / "deployment.yaml").write_text("kind: Deployment\n")
    sha, err = commit_paths(repo, [d / "deployment.yaml"], "gitops: deploy web")
    assert sha and err is None
    files = _git(repo, "show", "--name-only", "--format=", "HEAD").stdout.split()
    assert files == ["manifests/ns/web/deployment.yaml"]
    assert "A  other.txt" in _git(repo, "status", "--short").stdout


def test_hook_runs_and_passing_hook_commits(repo):
    _hook(repo, "echo ran > hook_ran.txt; exit 0")
    (repo / "a.yaml").write_text("x: 1\n")
    sha, _ = commit_paths(repo, [repo / "a.yaml"], "gitops: deploy a")
    assert sha and (repo / "hook_ran.txt").exists()


def test_failing_hook_leaves_nothing_staged(repo):
    """例如 gitleaks 擋下：不提交，而且檔案要移出暫存區，不能留下半套狀態。"""
    _hook(repo, "echo 'leak found' >&2; exit 1")
    (repo / "b.yaml").write_text("password: hunter2\n")
    sha, err = commit_paths(repo, [repo / "b.yaml"], "gitops: deploy b")
    assert sha is None and "leak found" in err
    assert _git(repo, "status", "--short").stdout.strip() == "?? b.yaml"


def test_directory_path_for_rollback(repo):
    d = repo / "manifests" / "ns" / "app"
    d.mkdir(parents=True)
    (d / "deployment.yaml").write_text("v: 1\n")
    (d / "service.yaml").write_text("v: 1\n")
    sha, _ = commit_paths(repo, [os.path.relpath(d, repo)], "gitops: revert app")
    assert sha
    assert sorted(_git(repo, "show", "--name-only", "--format=", "HEAD").stdout.split()) == [
        "manifests/ns/app/deployment.yaml", "manifests/ns/app/service.yaml"]
