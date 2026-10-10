"""docker-compose 匯入（agents/compose_import.py）與部署欄位傳遞的測試。"""
import pytest
import yaml

from agents.compose_import import ComposeError, parse_compose, _memory, _cpu, _parse_port
from agents.cost_agent import default_port

SAMPLE = """
services:
  web_front:
    image: nginx:1.27
    ports: ["8080:80"]
    depends_on: [api]
  api:
    image: ghcr.io/acme/orders-api:1.4
    ports: ["3000"]
    environment:
      DB_HOST: db
      DB_PASSWORD: s3cret
    depends_on:
      db:
        condition: service_healthy
    deploy:
      replicas: 2
      resources: {limits: {cpus: "0.5", memory: 512M}}
  db:
    image: postgres:16
    environment: [POSTGRES_PASSWORD=example, POSTGRES_DB=orders, FROM_HOST]
    volumes: ["pgdata:/var/lib/postgresql/data", "./init.sql:/docker-entrypoint-initdb.d/init.sql"]
  worker:
    build: ./worker
volumes: {pgdata: {}}
"""


def _by_name(r):
    return {s["app_name"]: s for s in r["services"]}


def test_order_follows_dependencies():
    r = parse_compose(SAMPLE, default_port)
    order = [s["app_name"] for s in r["services"]]
    assert order.index("db") < order.index("api") < order.index("web-front")


def test_ports_expose_and_internal_services():
    s = _by_name(parse_compose(SAMPLE, default_port))
    assert (s["web-front"]["port"], s["web-front"]["container_port"], s["web-front"]["expose"]) == (8080, 80, True)
    assert (s["api"]["port"], s["api"]["container_port"]) == (3000, 3000)
    # 沒寫 ports：只給內部連，用映像檔預設 port
    assert (s["db"]["port"], s["db"]["container_port"], s["db"]["expose"]) == (5432, 5432, False)
    assert all(x["dns_alias"] for x in s.values())


def test_env_resources_replicas():
    s = _by_name(parse_compose(SAMPLE, default_port))
    assert s["api"]["env"] == {"DB_HOST": "db", "DB_PASSWORD": "s3cret"}
    assert s["db"]["env"] == {"POSTGRES_PASSWORD": "example", "POSTGRES_DB": "orders"}   # 沒有值的略過
    assert (s["api"]["pods"], s["api"]["memory"], s["api"]["cpu"]) == (2, "512Mi", "500m")


def test_problems_are_reported():
    r = parse_compose(SAMPLE, default_port)
    s = _by_name(r)
    assert not r["deployable"]
    assert any(p["level"] == "block" and "build" in p["text"] for p in s["worker"]["problems"])
    texts = " ".join(p["text"] for p in s["db"]["problems"])
    assert "本機資料夾" in texts and "資料不會保留" in texts and "POSTGRES_PASSWORD" in texts
    assert any("web-front" in p["text"] for p in s["web-front"]["problems"])   # 改名要告知


def test_unsafe_settings_are_never_passed_through():
    r = parse_compose("""
services:
  x:
    image: alpine:3.20
    privileged: true
    network_mode: host
    cap_add: [SYS_ADMIN]
    command: ["sh", "-c", "rm -rf /"]
""")
    s = r["services"][0]
    assert {"privileged", "network_mode", "cap_add", "command"}.isdisjoint(s)
    assert sum(1 for p in s["problems"] if p["level"] == "warn") >= 3


def test_dependency_cycle_is_rejected():
    with pytest.raises(ComposeError, match="一圈"):
        parse_compose("services:\n  a: {image: x, depends_on: [b]}\n  b: {image: y, depends_on: [a]}\n")


@pytest.mark.parametrize("text,msg", [
    ("", "空的"), ("hello: world", "services"), ("services: [", "YAML"),
    ("services:\n" + "".join(f"  s{i}: {{image: nginx}}\n" for i in range(11)), "最多"),
    ("x" * 100_001, "太大"),
], ids=["empty", "no-services", "bad-yaml", "too-many", "too-big"])
def test_bad_files(text, msg):
    with pytest.raises(ComposeError, match=msg):
        parse_compose(text)


def test_value_conversions():
    assert [_memory(x) for x in ("512M", "1g", "256mb", "268435456", "2G")] == ["512Mi", "1Gi", "256Mi", "256Mi", "2Gi"]
    assert [_cpu(x) for x in ("0.5", "1.5", "2", "0", "abc")] == ["500m", "1500m", "2000m", None, None]
    assert _parse_port("127.0.0.1:8080:80/tcp") == {"external": 8080, "container": 80}
    assert _parse_port({"target": 80, "published": 8081}) == {"external": 8081, "container": 80}


def test_compose_fields_flow_into_git_manifest():
    from gitops.manifest_writer import _build_deployment_yaml, _build_service_yaml
    spec = {"app_name": "api", "image": "ghcr.io/acme/orders-api:1.4", "pods": 2, "port": 8080,
            "container_port": 3000, "env": {"DB_HOST": "db"}}
    c = yaml.safe_load(_build_deployment_yaml(spec, "u"))["spec"]["template"]["spec"]["containers"][0]
    assert c["ports"][0]["containerPort"] == 3000 and {"name": "DB_HOST", "value": "db"} in c["env"]
    ports = yaml.safe_load(_build_service_yaml(spec, "u"))["spec"]["ports"]
    assert {p["port"] for p in ports} == {3000, 8080} and all(p["targetPort"] == 3000 for p in ports)


def test_sanitize_keeps_compose_fields_and_drops_bad_ones():
    web_demo = pytest.importorskip("web_demo")
    clean = web_demo._sanitize_deploy_payload({
        "app_name": "api", "image": "x:1", "pods": 1, "port": 8080, "container_port": 3000,
        "env": {"OK_1": "a", "bad key": "b", "X": None, "Y": 5}, "expose": False, "dns_alias": True,
        "privileged": True,
    })
    assert clean["env"] == {"OK_1": "a", "Y": "5"} and clean["container_port"] == 3000
    assert clean["expose"] is False and clean["dns_alias"] is True and "privileged" not in clean
    m = web_demo._build_agent_manifest(clean)
    c = m["spec"]["template"]["spec"]["containers"][0]
    assert c["ports"][0]["containerPort"] == 3000 and {"name": "OK_1", "value": "a"} in c["env"]


def test_yaml_alias_bomb_is_rejected_quickly():
    """「十億笑聲」：十層、每層參照上一層十次，原始檔不到 1 KB，展開後是 10^10 個節點。"""
    import time
    lines = ['a0: &a0 ["x","x","x","x","x","x","x","x","x","x"]']
    for i in range(1, 10):
        lines.append(f"a{i}: &a{i} [" + ",".join([f"*a{i-1}"] * 10) + "]")
    bomb = "\n".join(lines) + "\nservices:\n  web:\n    image: nginx\n    environment: *a9\n"
    t = time.perf_counter()
    with pytest.raises(ComposeError, match="展開後內容過大"):
        parse_compose(bomb)
    assert time.perf_counter() - t < 1


def test_normal_yaml_anchors_still_work():
    r = parse_compose("""
x-common: &common
  environment: {TZ: Asia/Taipei}
  deploy: {replicas: 2}
services:
  web: {<<: *common, image: nginx:1.27, ports: ["8080:80"]}
  api: {<<: *common, image: traefik/whoami:v1.10}
""")
    s = _by_name(r)
    assert s["web"]["env"] == {"TZ": "Asia/Taipei"} and s["api"]["pods"] == 2


def test_secret_env_values_never_reach_git_manifest():
    """密碼類環境變數的值不能寫進部署設定檔（會 commit 進 Git），只能寫「去哪個 Secret 拿」。"""
    from gitops.manifest_writer import _build_deployment_yaml, build_secret_yaml
    spec = {"app_name": "api", "image": "x:1", "port": 80,
            "env": {"DB_HOST": "db", "DB_PASSWORD": "s3cret-value", "API_TOKEN": "tok-123"}}
    text = _build_deployment_yaml(spec, "u")
    assert "s3cret-value" not in text and "tok-123" not in text
    env = {e["name"]: e for e in yaml.safe_load(text)["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["DB_HOST"]["value"] == "db"
    assert env["DB_PASSWORD"]["valueFrom"]["secretKeyRef"] == {"name": "api-secrets", "key": "DB_PASSWORD"}
    sec = yaml.safe_load(build_secret_yaml(spec, "zt-carol"))
    assert sec["kind"] == "Secret" and sec["stringData"] == {"DB_PASSWORD": "s3cret-value", "API_TOKEN": "tok-123"}
    assert build_secret_yaml({"app_name": "a", "env": {"X": "1"}}, "u") is None


def test_review_manifest_has_no_secret_values():
    web_demo = pytest.importorskip("web_demo")
    m = web_demo._build_agent_manifest({"app_name": "api", "image": "x:1", "pods": 1, "port": 80,
                                        "env": {"DB_PASSWORD": "s3cret-value"}})
    assert "s3cret-value" not in str(m)
