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


def test_scale_limit_matches_deploy_policy():
    """擴展的副本數上限跟部署審查用同一個設定值（policy_rules.yaml），組員原本能直接擴到 100 個。"""
    from guardian.yaml_validator import max_replicas
    web_demo = pytest.importorskip("web_demo")
    c = web_demo.app.test_client()
    with c.session_transaction() as s:
        s["username"] = "limit-check"
    r = c.post("/api/scale", json={"name": "web", "replicas": max_replicas() + 1})
    assert r.status_code in (400, 503)
    if r.status_code == 400:
        assert str(max_replicas()) in r.get_json()["error"]


def test_probes_by_image_type():
    """網頁伺服器用 HTTP 檢查（偵測得到卡住）；其他程式只用連線檢查、不設重啟條件。"""
    from agents.cost_agent import recommended_probes
    p = recommended_probes("nginx:1.27", 80)
    assert p["readinessProbe"]["httpGet"]["port"] == 80 and "livenessProbe" in p
    # 卡住超過 30 秒才重啟，短暫卡頓（組員測試的 25 秒）只會先停止分流量
    lv = p["livenessProbe"]
    assert lv["periodSeconds"] * lv["failureThreshold"] >= 30
    r = recommended_probes("redis:7", 6379)
    assert r["readinessProbe"]["tcpSocket"]["port"] == 6379 and "livenessProbe" not in r
    assert recommended_probes("python:3.12", 0) == {}


def test_probes_same_in_review_and_git():
    from gitops.manifest_writer import _build_deployment_yaml
    from agents.cost_agent import recommended_probes
    web_demo = pytest.importorskip("web_demo")
    spec = {"app_name": "web", "image": "nginx:1.27", "pods": 2, "port": 8080}
    review_c = web_demo._build_agent_manifest(spec)["spec"]["template"]["spec"]["containers"][0]
    git_c = yaml.safe_load(_build_deployment_yaml(spec, "u"))["spec"]["template"]["spec"]["containers"][0]
    want = recommended_probes("nginx:1.27", 80)   # 對外 8080，但 nginx 在容器裡聽 80，檢查要打 80
    for c in (review_c, git_c):
        assert c["readinessProbe"] == want["readinessProbe"] and c["livenessProbe"] == want["livenessProbe"]


@pytest.mark.parametrize("text,image,parsed_port,expected", [
    ("幫我部署 redis", "redis:latest", 80, 6379),
    ("部署 postgres:16", "postgres:16", 80, 5432),
    ("部署 redis，開 8080 port", "redis:latest", 8080, 8080),
    ("部署 redis port 80", "redis:latest", 80, 80),
    ("部署 nginx", "nginx:latest", 80, 80),
])
def test_known_service_default_port(monkeypatch, text, image, parsed_port, expected):
    web_demo = pytest.importorskip("web_demo")
    monkeypatch.setattr(web_demo, "ask_llama", lambda t: {"app_name": "svc", "image": image, "pods": 1, "port": parsed_port})
    monkeypatch.setattr(web_demo, "_apply_scenario", lambda t, p: None)
    monkeypatch.setattr(web_demo, "_review_deployment", lambda p: {"decision": "approve"})
    monkeypatch.setattr(web_demo, "K8S_ENABLED", False, raising=False)
    parsed, _, _, err = web_demo._prepare_deploy(text)
    assert err is None and parsed["port"] == expected


def test_external_port_maps_to_real_container_port():
    """「nginx 開 8085 port」：對外是 8085，容器裡 nginx 聽 80。原本兩邊都設 8085，服務一直連不上。"""
    from gitops.manifest_writer import _build_deployment_yaml, _build_service_yaml
    from agents.cost_agent import container_port
    spec = {"app_name": "web", "image": "nginx:1.27", "pods": 1, "port": 8085}
    dep = yaml.safe_load(_build_deployment_yaml(spec, "u"))
    svc = yaml.safe_load(_build_service_yaml(spec, "u"))
    assert dep["spec"]["template"]["spec"]["containers"][0]["ports"][0]["containerPort"] == 80
    assert svc["spec"]["ports"][0] == {"protocol": "TCP", "port": 8085, "targetPort": 80}
    assert container_port("redis:7", 7000) == 6379
    assert container_port("ghcr.io/me/my-api:1.0", 3000) == 3000   # 不認識的映像檔照使用者講的
