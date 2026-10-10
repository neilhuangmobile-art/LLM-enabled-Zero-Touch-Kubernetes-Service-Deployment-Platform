"""2026-10-10 組員極限測試（user-aaa nginx）發現的問題的回歸測試。"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pytest
import yaml

from agents.cost_agent import recommended_env
from healer.pod_watcher import _check_pod


def _pod(last_reason=None, finished_ago_min=1, running=True):
    last = None
    if last_reason:
        last = NS(terminated=NS(reason=last_reason, message="", exit_code=137,
                                finished_at=datetime.now(timezone.utc) - timedelta(minutes=finished_ago_min)))
    cs = NS(name="nginx", restart_count=1, last_state=last,
            state=NS(waiting=None, terminated=None, running=NS() if running else None))
    return NS(metadata=NS(name="nginx-abc", namespace="user-aaa"),
              status=NS(phase="Running", container_statuses=[cs]))


def test_healer_sees_oom_that_already_recovered():
    """記憶體超過上限被砍、3 秒後自動重啟：掃描時容器已經是 Running，也要抓到這次 OOM。"""
    issue = _check_pod(_pod("OOMKilled", finished_ago_min=1))
    assert issue and issue["reason"] == "OOMKilled"


def test_old_oom_is_not_reported_again():
    assert _check_pod(_pod("OOMKilled", finished_ago_min=120)) is None


def test_healthy_pod_without_history():
    assert _check_pod(_pod(None)) is None
    assert _check_pod(_pod("Completed", finished_ago_min=1)) is None


@pytest.mark.parametrize("image,on", [
    ("nginx", True), ("nginx:1.27", True), ("docker.io/library/nginx:1.27-alpine", True),
    ("nginxinc/nginx-unprivileged:1.27", True), ("localhost:5000/nginx:1.27", True),
    ("redis:7", False), ("my-nginx-app:1.0", False), ("python:3.12", False),
])
def test_nginx_worker_autotune(image, on):
    assert bool(recommended_env(image)) is on


def test_git_manifest_gets_nginx_autotune_and_user_env_wins():
    from gitops.manifest_writer import _build_deployment_yaml
    doc = yaml.safe_load(_build_deployment_yaml({"app_name": "web", "image": "nginx:1.27", "pods": 2}, "user-aaa"))
    env = {e["name"]: e["value"] for e in doc["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["NGINX_ENTRYPOINT_WORKER_PROCESSES_AUTOTUNE"] == "1"
    doc = yaml.safe_load(_build_deployment_yaml(
        {"app_name": "web", "image": "nginx:1.27", "env": {"NGINX_ENTRYPOINT_WORKER_PROCESSES_AUTOTUNE": ""}}, "u"))
    env = {e["name"]: e["value"] for e in doc["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["NGINX_ENTRYPOINT_WORKER_PROCESSES_AUTOTUNE"] == ""


def test_review_manifest_matches_git_env():
    web_demo = pytest.importorskip("web_demo")
    m = web_demo._build_agent_manifest({"app_name": "web", "image": "nginx:1.27", "pods": 1, "port": 80})
    c = m["spec"]["template"]["spec"]["containers"][0]
    assert {e["name"] for e in c.get("env", [])} == set(recommended_env("nginx:1.27"))


def test_scale_blocked_when_pod_slots_run_out(monkeypatch):
    """要求 100 個副本只排得下 95 個：Pod 位置不夠時要擋下並說明，不能讓多的卡在 Pending。"""
    web_demo = pytest.importorskip("web_demo")
    monkeypatch.setattr(web_demo, "K8S_ENABLED", True, raising=False)
    monkeypatch.setattr(web_demo, "_local_k8s_get_node_capacity", lambda: {"cpu": "32", "memory": "64Gi"}, raising=False)
    monkeypatch.setattr(web_demo, "k8s_get_node_capacity", lambda *a, **k: {"cpu": "32", "memory": "64Gi"})
    monkeypatch.setattr(web_demo, "_sum_cluster_resource_requests", lambda **k: (0, 0))
    monkeypatch.setattr(web_demo, "_pod_slots", lambda: (110, 17))

    class _Apps:
        def read_namespaced_deployment(self, name, ns):
            return NS(spec=NS(replicas=2, template=NS(spec=NS(containers=[]))))
    monkeypatch.setattr(web_demo.k8s_client, "AppsV1Api", lambda *a: _Apps())
    blocked, msg = web_demo._check_scale_risk("nginx", 100, "user-aaa")
    assert blocked and "Pod 位置不夠" in msg and "110" in msg
    blocked, _ = web_demo._check_scale_risk("nginx", 50, "user-aaa")
    assert not blocked
