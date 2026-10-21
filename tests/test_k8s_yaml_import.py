"""Kubernetes YAML 匯入（agents/k8s_yaml_import.py）與「強制當密碼」欄位的測試。"""

import pytest
import yaml

from agents.compose_import import ComposeError
from agents.cost_agent import default_port
from agents.k8s_yaml_import import looks_like_k8s, parse_k8s_yaml

SAMPLE = """
apiVersion: v1
kind: Namespace
metadata: {name: shop}
---
apiVersion: v1
kind: ConfigMap
metadata: {name: web-config, namespace: shop}
data: {API_URL: "http://api:8080", MODE: prod}
---
apiVersion: v1
kind: Secret
metadata: {name: db-secret}
type: Opaque
data: {DATABASE_URL: cG9zdGdyZXM6Ly91c2VyOnB3QGRiOjU0MzIvYXBw}
stringData: {POSTGRES_PASSWORD: example}
---
apiVersion: apps/v1
kind: Deployment
metadata: {name: frontend}
spec:
  replicas: 2
  selector: {matchLabels: {app: web}}
  template:
    metadata: {labels: {app: web}}
    spec:
      containers:
      - name: nginx
        image: nginx:1.27
        ports: [{containerPort: 80, name: http}]
        envFrom: [{configMapRef: {name: web-config}}]
        resources: {limits: {cpu: "0.5", memory: 256Mi}}
      - {name: sidecar, image: busybox}
---
apiVersion: v1
kind: Service
metadata: {name: web}
spec:
  type: LoadBalancer
  selector: {app: web}
  ports: [{port: 8088, targetPort: http}]
---
apiVersion: apps/v1
kind: StatefulSet
metadata: {name: db}
spec:
  selector: {matchLabels: {app: db}}
  template:
    metadata: {labels: {app: db}}
    spec:
      hostNetwork: true
      containers:
      - name: postgres
        image: postgres:16
        env:
        - {name: POSTGRES_PASSWORD, valueFrom: {secretKeyRef: {name: db-secret, key: POSTGRES_PASSWORD}}}
        - {name: DATABASE_URL, valueFrom: {secretKeyRef: {name: db-secret, key: DATABASE_URL}}}
        - {name: POD_IP, valueFrom: {fieldRef: {fieldPath: status.podIP}}}
        securityContext: {privileged: true}
        volumeMounts: [{name: data, mountPath: /var/lib/postgresql/data}, {name: host, mountPath: /host}]
      volumes:
      - {name: data, persistentVolumeClaim: {claimName: pgdata}}
      - {name: host, hostPath: {path: /etc}}
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata: {name: pgdata}
spec: {accessModes: [ReadWriteOnce], resources: {requests: {storage: 20Gi}}}
---
apiVersion: v1
kind: Service
metadata: {name: db}
spec: {selector: {app: db}, ports: [{port: 5432}]}
---
apiVersion: batch/v1
kind: CronJob
metadata: {name: backup}
spec: {schedule: "0 3 * * *"}
---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata: {name: web-ing}
"""


def _by_name(r):
    return {s["app_name"]: s for s in r["services"]}


def test_detects_k8s_yaml():
    assert looks_like_k8s(SAMPLE)
    assert not looks_like_k8s("services:\n  web: {image: nginx}\n")


def test_deployment_and_service_are_matched():
    s = _by_name(parse_k8s_yaml(SAMPLE, default_port))
    web = s["web"]   # 部署名稱改用 Service 的名字，其他服務才連得到
    assert (web["image"], web["pods"], web["port"], web["container_port"], web["expose"]) == ("nginx:1.27", 2, 8088, 80, True)
    assert (web["memory"], web["cpu"]) == ("256Mi", "500m")
    assert web["env"] == {"API_URL": "http://api:8080", "MODE": "prod"}
    assert any("只部署第一個" in p["text"] for p in web["problems"])
    db = s["db"]
    assert (db["port"], db["container_port"], db["expose"]) == (5432, 5432, False)


def test_internal_services_deploy_first():
    order = [x["app_name"] for x in parse_k8s_yaml(SAMPLE, default_port)["services"]]
    assert order.index("db") < order.index("web")


def test_secret_values_are_forced_secret_even_with_plain_names():
    db = _by_name(parse_k8s_yaml(SAMPLE, default_port))["db"]
    assert db["env"]["DATABASE_URL"] == "postgres://user:pw@db:5432/app"   # base64 解開
    assert set(db["secret_keys"]) == {"DATABASE_URL", "POSTGRES_PASSWORD"}
    assert "POD_IP" not in db["env"]


def test_unsafe_and_unsupported_items_reported_never_passed():
    r = parse_k8s_yaml(SAMPLE, default_port)
    db = _by_name(r)["db"]
    texts = " ".join(p["text"] for p in db["problems"])
    assert "hostNetwork" in texts and "hostPath" in texts and "特權" in texts and "StatefulSet" in texts
    assert [v["mount"] for v in db["volumes"]] == ["/var/lib/postgresql/data"]   # hostPath 不轉
    assert db["volumes"][0]["size"] == "5Gi" and "20Gi" in texts                 # 超過上限要說明
    top = " ".join(p["text"] for p in r["problems"])
    assert "CronJob" in top and "Ingress" in top and top.count("namespace") == 1
    assert {"hostNetwork", "privileged", "hostPath"}.isdisjoint(str(r["services"]))


@pytest.mark.parametrize("text,msg", [
    ("apiVersion: batch/v1\nkind: Job\nmetadata: {name: x}\n", "沒有 Deployment"),
    ("hello: world\n", "apiVersion"),
    ("apiVersion: v1\nkind: [\n", "YAML"),
])
def test_bad_files(text, msg):
    with pytest.raises(ComposeError, match=msg):
        parse_k8s_yaml(text)


def test_alias_bomb_rejected():
    lines = ['apiVersion: v1', 'kind: ConfigMap', 'metadata: {name: x}', 'a0: &a0 ["x","x","x","x","x","x","x","x","x","x"]']
    for i in range(1, 10):
        lines.append(f"a{i}: &a{i} [" + ",".join([f"*a{i-1}"] * 10) + "]")
    with pytest.raises(ComposeError, match="展開後內容過大"):
        parse_k8s_yaml("\n".join(lines))


def test_forced_secret_keys_stay_out_of_git():
    """名稱不像密碼的值（DATABASE_URL）只要來自 Secret，也不能寫進部署設定檔。"""
    from gitops.manifest_writer import _build_deployment_yaml, build_secret_yaml
    spec = {"app_name": "db", "image": "postgres:16", "port": 5432,
            "env": {"DATABASE_URL": "postgres://user:pw@db/app", "TZ": "Asia/Taipei"}, "secret_keys": ["DATABASE_URL"]}
    text = _build_deployment_yaml(spec, "u")
    assert "user:pw" not in text and "Asia/Taipei" in text
    assert yaml.safe_load(build_secret_yaml(spec, "u"))["stringData"] == {"DATABASE_URL": "postgres://user:pw@db/app"}


def test_import_route_accepts_both_formats_and_sanitizes_secret_keys():
    web_demo = pytest.importorskip("web_demo")
    c = web_demo.app.test_client()
    with c.session_transaction() as s:
        s["username"] = "import-check"
    r = c.post("/api/import/parse", json={"text": SAMPLE}).get_json()
    assert r["format"] == "k8s" and {x["app_name"] for x in r["services"]} == {"web", "db"}
    r = c.post("/api/import/parse", json={"text": "services:\n  web: {image: nginx:1.27}\n"}).get_json()
    assert r["format"] == "compose"
    clean = web_demo._sanitize_deploy_payload({"app_name": "db", "image": "x:1", "env": {"A": "1"},
                                               "secret_keys": ["A", "NOT_IN_ENV"]})
    assert clean["secret_keys"] == ["A"]
    m = web_demo._build_agent_manifest(clean)
    assert "valueFrom" in m["spec"]["template"]["spec"]["containers"][0]["env"][0]
