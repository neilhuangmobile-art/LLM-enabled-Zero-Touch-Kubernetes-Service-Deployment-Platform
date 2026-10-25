"""一個服務開多個 port（2026-10-24，compose／K8s YAML 匯入的 extra_ports）。前半是純邏輯（CI 會跑），
最後兩項需要 flask（web_demo 的規格檢查與 port 衝突判斷）。"""
import pytest
import yaml

from agents.compose_import import extra_ports, parse_compose, MAX_EXTRA_PORTS
from agents.cost_agent import default_port
from agents.k8s_yaml_import import parse_k8s_yaml
from gitops.manifest_writer import _build_deployment_yaml, _build_service_yaml


def test_compose_second_port_becomes_extra():
    svc = parse_compose("""
services:
  web:
    image: nginx:1.27
    ports: ["8095:80", "8096:81", "8095:82"]
""")["services"][0]
    assert svc["port"] == 8095 and svc["container_port"] == 80
    assert svc["extra_ports"] == [{"port": 8096, "container": 81}]          # 跟主 port 撞的丟掉
    assert not any("只開第一個" in p["text"] for p in svc["problems"])


def test_single_port_has_no_extra_field():
    svc = parse_compose("services:\n  web:\n    image: nginx:1.27\n    ports: ['8095:80']\n")["services"][0]
    assert "extra_ports" not in svc


def test_extra_ports_limit_and_validation():
    pairs = [{"port": 9000 + i, "container": 80 + i} for i in range(10)] + [{"port": "x", "container": 1}, {"port": 70000, "container": 80}]
    out = extra_ports(pairs, 8080)
    assert len(out) == MAX_EXTRA_PORTS and out[0] == {"port": 9000, "container": 80}


def test_k8s_yaml_service_ports_with_named_target():
    doc = """
apiVersion: apps/v1
kind: Deployment
metadata: {name: api}
spec:
  selector: {matchLabels: {app: api}}
  template:
    metadata: {labels: {app: api}}
    spec:
      containers:
      - name: api
        image: nginx:1.27
        ports: [{containerPort: 80, name: http}, {containerPort: 9090, name: metrics}]
---
apiVersion: v1
kind: Service
metadata: {name: api}
spec:
  type: LoadBalancer
  selector: {app: api}
  ports: [{port: 8097, targetPort: http}, {port: 8098, targetPort: metrics}]
"""
    s = parse_k8s_yaml(doc, default_port)["services"][0]
    assert s["port"] == 8097 and s["container_port"] == 80
    assert s["extra_ports"] == [{"port": 8098, "container": 9090}]


def test_manifests_open_every_port():
    spec = {"app_name": "web", "image": "nginx:1.27", "pods": 1, "port": 8095, "container_port": 80,
            "extra_ports": [{"port": 8096, "container": 81}]}
    dep = yaml.safe_load(_build_deployment_yaml(spec, "user-a"))
    assert [p["containerPort"] for p in dep["spec"]["template"]["spec"]["containers"][0]["ports"]] == [80, 81]
    svc = yaml.safe_load(_build_service_yaml(spec, "user-a"))
    ports = {p["name"]: (p["port"], p["targetPort"]) for p in svc["spec"]["ports"]}
    assert ports == {"app": (80, 80), "external": (8095, 80), "c81": (81, 81), "e8096": (8096, 81)}
    # 沒有額外 port 時跟原本一樣
    plain = yaml.safe_load(_build_service_yaml({k: v for k, v in spec.items() if k != "extra_ports"}, "user-a"))
    assert [p["name"] for p in plain["spec"]["ports"]] == ["app", "external"]


def test_sanitize_keeps_valid_extra_ports():
    wd = pytest.importorskip("web_demo")
    clean = wd._sanitize_deploy_payload({"app_name": "web", "image": "nginx", "pods": 1, "port": 8095,
                                         "extra_ports": [{"port": 8096, "container": 81}, {"port": 8095, "container": 80},
                                                         {"port": "bad"}, "x"]})
    assert clean["extra_ports"] == [{"port": 8096, "container": 81}]
    assert "extra_ports" not in wd._sanitize_deploy_payload({"app_name": "web", "image": "nginx", "port": 80})


def test_port_conflict_owner_hides_other_accounts():
    wd = pytest.importorskip("web_demo")
    svcs = [{"name": "a-svc", "app": "a", "port": 8096, "namespace": "user-me"},
            {"name": "b-svc", "app": "secret-app", "port": 8097, "namespace": "user-other"}]
    assert wd._port_conflict_owner(8096, "user-me", "web", svcs) == ("a", False)
    assert wd._port_conflict_owner(8096, "user-me", "a", svcs) == (None, False)      # 更新同一個服務不算
    assert wd._port_conflict_owner(8097, "user-me", "web", svcs) == ("another account", True)
    assert wd._port_conflict_owner(8099, "user-me", "web", svcs) == (None, False)
