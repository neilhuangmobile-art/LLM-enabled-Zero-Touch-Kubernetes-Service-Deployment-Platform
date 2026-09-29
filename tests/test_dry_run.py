"""
tests/test_dry_run.py
測 guardian/dry_run.py 的 _kubectl_dry_run()——2026-09-29 發現的真實 bug：
`--dry-run=client`（docstring 宣稱「只做語法檢查、不需連線叢集」）實際上新版
kubectl 仍會嘗試連叢集下載 OpenAPI schema，K8s 斷線時這個請求會失敗，導致
「無法判斷 manifest 對不對」被誤判成「manifest 確定有問題」而擋下部署，且錯誤
訊息是一整段對新手無意義的原始網路堆疊錯誤。修法：偵測這種連線失敗的錯誤訊息，
回傳 ok=None（呼叫端 web_demo.py._prepare_deploy 本來就已經設計成 ok is None
時只加警告、不擋部署），不是 ok=False。
"""
import subprocess

import pytest

from guardian.dry_run import _kubectl_dry_run


class _FakeCompleted:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_connectivity_failure_returns_ok_none_not_false(monkeypatch, tmp_path):
    """K8s 斷線時 kubectl 連不到叢集下載 openapi schema，應該回傳 ok=None
    （無法判斷）而不是 ok=False（判定失敗/擋下部署）。"""
    stderr = (
        'error: error validating "test.yaml": error validating data: '
        'failed to download openapi: Get "https://kubernetes.docker.internal:6443/'
        'openapi/v2?timeout=32s": dial tcp 127.0.0.1:6443: connectex: No connection '
        "could be made because the target machine actively refused it.; if you "
        "choose to ignore these errors, turn validation off with --validate=false"
    )

    def fake_run(cmd, capture_output, text, timeout):
        return _FakeCompleted(returncode=1, stdout="", stderr=stderr)

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = _kubectl_dry_run("apiVersion: v1\nkind: Pod\n", "client", "default")

    assert result["ok"] is None
    assert result["errors"] == []
    assert any("連不上" in w or "unreachable" in w for w in result["warnings"])


def test_genuine_validation_error_still_blocks(monkeypatch):
    """跟連線無關的真正 YAML/schema 錯誤，要維持原本 ok=False 的擋下行為，
    不能被這次的修法連帶放行。"""
    stderr = 'error: error validating "test.yaml": unknown field "spec.totallyBogusField"'

    def fake_run(cmd, capture_output, text, timeout):
        return _FakeCompleted(returncode=1, stdout="", stderr=stderr)

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = _kubectl_dry_run("apiVersion: v1\nkind: Pod\n", "client", "default")

    assert result["ok"] is False
    assert result["errors"]


def test_success_still_ok_true(monkeypatch):
    """對照組：kubectl 真的驗證成功時行為不變。"""
    def fake_run(cmd, capture_output, text, timeout):
        return _FakeCompleted(returncode=0, stdout="pod/test created (dry run)", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = _kubectl_dry_run("apiVersion: v1\nkind: Pod\n", "client", "default")

    assert result["ok"] is True
    assert result["errors"] == []


def test_kubectl_not_installed_still_ok_none(monkeypatch):
    """既有行為不受影響：kubectl 本身找不到時仍回傳 ok=None。"""
    def fake_run(cmd, capture_output, text, timeout):
        raise FileNotFoundError("kubectl not found")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = _kubectl_dry_run("apiVersion: v1\nkind: Pod\n", "client", "default")

    assert result["ok"] is None
