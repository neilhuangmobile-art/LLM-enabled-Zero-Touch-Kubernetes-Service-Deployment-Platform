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
    assert "本機資料夾" in texts and "保存空間" in texts and "POSTGRES_PASSWORD" in texts
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
    assert {"privileged", "network_mode", "cap_add"}.isdisjoint(s)
    assert s["args"] == ["sh", "-c", "rm -rf /"]   # 啟動指令照轉：它只在使用者自己的容器裡執行，跟映像檔本身一樣
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


def test_oversized_requests_rejected_before_parsing():
    """請求大小上限：先擋下過大的請求，不能讀完 1 GB 才發現超過 compose 的 100 KB 上限。"""
    web_demo = pytest.importorskip("web_demo")
    c = web_demo.app.test_client()
    with c.session_transaction() as s:
        s["username"] = "size-check"
    assert web_demo.app.config["MAX_CONTENT_LENGTH"] == 1024 * 1024
    big = '{"text": "' + "x" * (2 * 1024 * 1024) + '"}'
    assert c.post("/api/compose/parse", data=big, content_type="application/json").status_code == 413
    mid = '{"text": "' + "x" * 300_000 + '"}'   # 低於全站上限、高於 compose 上限
    r = c.post("/api/compose/parse", data=mid, content_type="application/json")
    assert r.status_code == 413 and "太大" in r.get_json()["error"]
    ok = c.post("/api/compose/parse", json={"text": "services:\n  web: {image: nginx:1.27}\n"})
    assert ok.status_code == 200 and ok.get_json()["services"][0]["app_name"] == "web"


def test_compose_route_is_rate_limited():
    from core.web_security import match_rule
    assert match_rule("/api/compose/parse", "POST")[0] == "compose"


def test_command_and_volumes_are_converted():
    r = parse_compose("""
services:
  cache:
    image: redis:7.2
    command: redis-server --appendonly yes
    volumes: ["cachedata:/data", "/scratch", "./conf:/etc/redis:ro"]
  tool:
    image: alpine:3.20
    entrypoint: ["sh", "-c"]
    command: ["echo hi && sleep 3600"]
    volumes:
      - {type: tmpfs, target: /tmp/work}
      - {type: volume, source: shared, target: /shared, read_only: true}
volumes: {cachedata: {}, shared: {}}
""")
    s = _by_name(r)
    assert s["cache"]["args"] == ["redis-server", "--appendonly", "yes"] and s["cache"]["command"] is None
    assert s["tool"]["command"] == ["sh", "-c"] and s["tool"]["args"] == ["echo hi && sleep 3600"]
    vols = {v["mount"]: v for v in s["cache"]["volumes"]}
    assert vols["/data"]["kind"] == "pvc" and vols["/data"]["size"] == "1Gi" and vols["/scratch"]["kind"] == "empty"
    assert "/etc/redis" not in vols   # 本機資料夾不轉
    t = {v["mount"]: v for v in s["tool"]["volumes"]}
    assert t["/tmp/work"]["kind"] == "empty" and t["/shared"]["read_only"] is True
    assert any("刪除這個服務時資料會一起刪除" in p["text"] for p in s["cache"]["problems"])


def test_volumes_flow_into_git_manifest_and_pvc():
    from gitops.manifest_writer import _build_deployment_yaml, build_pvc_yaml
    spec = {"app_name": "db", "image": "postgres:16", "port": 5432, "args": ["postgres", "-c", "max_connections=50"],
            "volumes": [{"name": "pgdata", "mount": "/var/lib/postgresql/data", "kind": "pvc", "size": "1Gi"},
                        {"name": "tmp0", "mount": "/tmp/x", "kind": "empty"}]}
    pod = yaml.safe_load(_build_deployment_yaml(spec, "u"))["spec"]["template"]["spec"]
    c = pod["containers"][0]
    assert c["args"] == ["postgres", "-c", "max_connections=50"]
    assert {"name": "pgdata", "mountPath": "/var/lib/postgresql/data"} in c["volumeMounts"]
    assert {"name": "pgdata", "persistentVolumeClaim": {"claimName": "db-pgdata"}} in pod["volumes"]
    assert {"name": "tmp0", "emptyDir": {}} in pod["volumes"]
    pvc = yaml.safe_load(build_pvc_yaml(spec, "u"))
    assert pvc["kind"] == "PersistentVolumeClaim" and pvc["metadata"]["labels"]["zerotouch/app"] == "db"
    assert pvc["spec"]["resources"]["requests"]["storage"] == "1Gi"


@pytest.mark.parametrize("vol,kept", [
    ({"name": "data", "mount": "/data", "kind": "pvc", "size": "1Gi"}, True),
    ({"name": "data", "mount": "/", "kind": "pvc", "size": "1Gi"}, False),          # 掛到根目錄
    ({"name": "data", "mount": "/a/../etc", "kind": "pvc", "size": "1Gi"}, False),  # 路徑跳脫
    ({"name": "data", "mount": "data", "kind": "pvc", "size": "1Gi"}, False),       # 不是絕對路徑
    ({"name": "data", "mount": "/data", "kind": "pvc", "size": "500Gi"}, False),    # 超過大小上限
    ({"name": "data", "mount": "/data", "kind": "hostPath", "size": "1Gi"}, False), # 不認識的種類
    ({"name": "Bad_Name", "mount": "/data", "kind": "pvc", "size": "1Gi"}, False),
])
def test_sanitize_volumes(vol, kept):
    web_demo = pytest.importorskip("web_demo")
    clean = web_demo._sanitize_deploy_payload({"app_name": "a", "image": "x:1", "volumes": [vol]})
    assert ("volumes" in clean) is kept


def test_storage_quota_blocks_before_deploy(monkeypatch):
    """每個帳號的保存空間有上限；超過時在審查就擋下並說明，不要等到部署才失敗。"""
    from types import SimpleNamespace as NS
    web_demo = pytest.importorskip("web_demo")
    existing = [NS(metadata=NS(name=f"old-{i}"), spec=NS(resources=NS(requests={"storage": "1Gi"}))) for i in range(8)]

    class _Core:
        def list_namespaced_persistent_volume_claim(self, ns):
            return NS(items=existing)

    class _Apps:
        def read_namespaced_deployment(self, name, ns):
            raise Exception("not found")

    monkeypatch.setattr(web_demo, "K8S_ENABLED", True, raising=False)
    monkeypatch.setattr(web_demo.k8s_client, "CoreV1Api", lambda *a: _Core())
    monkeypatch.setattr(web_demo.k8s_client, "AppsV1Api", lambda *a: _Apps())
    monkeypatch.setattr(web_demo, "_pod_slots", lambda: (110, 10))
    monkeypatch.setattr(web_demo, "k8s_get_node_capacity", lambda *a, **k: None)
    monkeypatch.setattr(web_demo, "_review_deployment", lambda p: {"decision": "approve", "blockers": []})
    c = web_demo.app.test_client()
    spec = {"app_name": "db", "image": "postgres:16", "pods": 1, "port": 5432,
            "volumes": [{"name": f"v{i}", "mount": f"/d{i}", "kind": "pvc", "size": "1Gi"} for i in range(3)]}
    with web_demo.app.test_request_context():
        web_demo.session["username"] = "quota-check"
        monkeypatch.setattr(web_demo, "_session_ns", lambda: "user-quota-check")
        _, _, review, err = web_demo._prepare_deploy("x", spec)
    assert review["decision"] == "block" and any("保存空間超過上限" in b for b in review["blockers"])
    spec["volumes"] = spec["volumes"][:2]   # 8 + 2 = 10 個、10Gi，剛好在上限內
    with web_demo.app.test_request_context():
        web_demo.session["username"] = "quota-check"
        _, _, review, err = web_demo._prepare_deploy("x", spec)
    assert review["decision"] == "approve"
