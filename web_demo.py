"""
web_demo.py  —  ZeroTouch K8s Platform v2
Clean white UI + Login/Register + Pod Details + AI Chat + Real K8s
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core.config import ensure_utf8_output
ensure_utf8_output()

import threading, json, urllib.request, hashlib, secrets, re, uuid
from datetime import datetime, timezone, timedelta
from flask import Flask, request, jsonify, render_template_string, session, redirect, url_for

from core.config import ROOT, YAML_DIR, MODEL_SERVER_URL
from llama_client import ask_llama, save_gold_sample, chat_llama
from core.claude_client import claude_chat, is_available as claude_available
from rag import kb_manager
from rag.kb_manager import KBError


# ══════════════════════════════════════════════════════════════════
# 資料集欄位 enrichment (is_k8s / language / complexity / namespace / output)
#   - 不修改 ask_llama() 本體,避免影響推論流程
#   - 在 /api/deploy 收到解析結果後,補上資料集所需的所有欄位
#   - 同時也是「線上版的標註器」:未來收集 gold sample 時可直接寫成
#     k8s_prompt_dataset 標準格式
# ══════════════════════════════════════════════════════════════════

# K8s / Trash talk 關鍵字 (用於 is_k8s 分類)
_K8S_KEYWORDS = (
    # 英文
    "pod", "pods", "deploy", "deployment", "service", "svc", "ingress",
    "namespace", "configmap", "secret", "helm", "kubectl", "replica", "replicas",
    "container", "image", "yaml", "manifest", "kubernetes", "k8s", "cluster",
    "node", "autoscal", "hpa", "pvc", "volume", "rbac", "networkpolicy",
    "nginx", "redis", "mysql", "postgres", "python", "node:",
    # 繁體中文
    "部署", "服務", "命名空間", "容器", "映像", "副本", "節點", "叢集",
    "資源", "記憶體", "埠號", "監聽",
)

_TRASH_TALK_KEYWORDS = (
    "roast", "taunt", "banter", "trash talk", "opponent", "hype my team",
    "嘴砲", "酸對手", "羞辱",
)

# namespace 抓取 (中英雙語 + 混合句)
_NS_PATTERNS = [
    re.compile(r'\bnamespace\s+(?:設為|設定為|為|是|to)?\s*([a-z0-9][-a-z0-9]*)', re.IGNORECASE),
    re.compile(r'\bns\s*[:=]\s*([a-z0-9][-a-z0-9]*)', re.IGNORECASE),
    re.compile(r'\bin\s+namespace\s+([a-z0-9][-a-z0-9]*)', re.IGNORECASE),
    re.compile(r'命名空間\s*(?:為|是|設為|設定為)?\s*([a-z0-9][-a-z0-9]*)'),
]

# complexity 評分用的「進階特徵」關鍵字
_COMPLEX_FEATURES = (
    "ingress", "configmap", "secret", "helm", "autoscal", "hpa",
    "rbac", "networkpolicy", "pvc", "persistentvolume", "rolling",
    "blue-green", "canary", "istio", "service mesh", "kustomize",
    "tls", "probe", "readiness", "liveness",
    # 中文
    "藍綠", "金絲雀", "滾動", "探針", "自動擴", "持久化",
)

_MULTI_APP_PATTERNS = [
    re.compile(r'\b(\d+)\s+(?:applications|apps|services|微服務|個應用)', re.IGNORECASE),
    re.compile(r'\bmulti[-\s]?app', re.IGNORECASE),
]


def _detect_language(text: str) -> str:
    """偵測語言。中文字元 >= 10% 視為 zh-tw,否則 en。"""
    if not text:
        return "en"
    cjk_count = sum(1 for ch in text if '\u4e00' <= ch <= '\u9fff')
    return "zh-tw" if cjk_count / max(len(text), 1) > 0.1 else "en"


def _detect_is_k8s(text: str, parsed: dict) -> bool:
    """
    判斷 prompt 是否為 K8s 請求。
    規則:
      1. 命中 trash talk 關鍵字 → False (強訊號)
      2. 命中 K8s 關鍵字 或 ask_llama 解析成功 → True
      3. 其他 → False
    """
    low = text.lower()
    if any(kw in low for kw in _TRASH_TALK_KEYWORDS):
        return False
    if any(kw in low for kw in _K8S_KEYWORDS):
        return True
    if parsed and isinstance(parsed, dict) and "pods" in parsed:
        return True
    return False


def _detect_namespace(text: str, parsed: dict) -> str:
    """從 prompt 抓 namespace;抓不到回傳 default。"""
    if parsed and parsed.get("namespace"):
        return str(parsed["namespace"])
    for pat in _NS_PATTERNS:
        m = pat.search(text)
        if m:
            return m.group(1)
    return "default"


def _detect_complexity(text: str) -> str:
    """
    啟發式 complexity 評分:
      - 長度分數: < 80 字 → 0 ; < 200 字 → 1 ; >= 200 字 → 2
      - 進階特徵: 每命中 1 個關鍵字 +1
      - 多應用: multi-app 模式 +2
    分數區間: 0-1 simple ; 2-3 medium ; >=4 complex
    """
    score = 0
    low = text.lower()
    length = len(text)
    if length >= 200:
        score += 2
    elif length >= 80:
        score += 1
    score += sum(1 for kw in _COMPLEX_FEATURES if kw in low)
    for pat in _MULTI_APP_PATTERNS:
        m = pat.search(text)
        if m:
            try:
                if int(m.group(1)) >= 2:
                    score += 2
            except (IndexError, ValueError):
                score += 2
            break
    if score >= 4:
        return "complex"
    if score >= 2:
        return "medium"
    return "simple"


def enrich_parsed_result(user_input: str, parsed: dict) -> dict:
    """
    把 ask_llama() 的原始解析結果包裝成資料集標準格式:
        {
          "id":         "k8s-xxxxxxxx",
          "prompt":     <原始輸入>,
          "output":     <ask_llama 解析的結構>,
          "is_k8s":     true/false,
          "complexity": "simple"/"medium"/"complex",
          "namespace":  "default" / 抓到的 namespace,
          "language":   "en" / "zh-tw"
        }
    回傳的 dict 同時保留原本扁平欄位 (app_name/image/pods/port/memory) ,
    讓既有的部署邏輯與前端 stats 不需改動。
    """
    if not isinstance(parsed, dict):
        parsed = {}

    language   = _detect_language(user_input)
    is_k8s     = _detect_is_k8s(user_input, parsed)
    namespace  = _detect_namespace(user_input, parsed)
    complexity = _detect_complexity(user_input)

    # 標準 output 結構 (資料集 ground truth)
    output_block = {
        "app_name": parsed.get("app_name"),
        "image":    parsed.get("image"),
        "pods":     parsed.get("pods"),
        "port":     parsed.get("port"),
        "memory":   parsed.get("memory"),
    }
    output_block = {k: v for k, v in output_block.items() if v is not None}

    enriched = dict(parsed)  # 保留所有原欄位給後續部署用
    enriched.update({
        "id":         f"k8s-{uuid.uuid4().hex[:8]}",
        "prompt":     user_input,
        "output":     output_block,
        "is_k8s":     is_k8s,
        "complexity": complexity,
        "namespace":  namespace,
        "language":   language,
    })
    return enriched

# ── Kubernetes ──────────────────────────────────────────────
K8S_ENABLED = False
try:
    from kubernetes import client as k8s_client, config as k8s_config
    import yaml as yaml_lib
    k8s_config.load_kube_config()
    K8S_ENABLED = True
    print("[K8s] 已連線")
except Exception as e:
    print(f"[K8s] 未連線（模擬模式）：{e}")

NS  = "default"
app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)

# ── Simple in-memory user store (replace with DB for production) ──
USERS = {}
try:
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "users.json"), encoding="utf-8") as _uf:
        USERS = json.load(_uf)
except Exception:
    USERS = {}

def hash_password(pw):
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 120000).hex()
    return f"pbkdf2_sha256${salt}${digest}"

def verify_password(pw, stored):
    if not stored:
        return False
    if stored.startswith("pbkdf2_sha256$"):
        try:
            _, salt, expected = stored.split("$", 2)
            digest = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 120000).hex()
            return secrets.compare_digest(digest, expected)
        except ValueError:
            return False
    return secrets.compare_digest(hashlib.sha256(pw.encode()).hexdigest(), stored)

# ── Model status ────────────────────────────────────────────
def _model_status():
    try:
        with urllib.request.urlopen(f"{MODEL_SERVER_URL}/health", timeout=2) as resp:
            data = json.loads(resp.read())
            return data.get("model_loaded", False), False
    except Exception:
        return False, False

# ── K8s helpers ─────────────────────────────────────────────
_TW_TZ = timezone(timedelta(hours=8))


def _fmt_k8s_time(ts):
    """Format Kubernetes UTC timestamps in Taiwan local time."""
    if not ts:
        return ""
    try:
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts.astimezone(_TW_TZ).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(ts)[:16]


def k8s_deploy(app_name, image, replicas, port=80, memory=None, cpu=None):
    if not K8S_ENABLED:
        return False, "K8s 未連線（模擬模式）"
    try:
        # k8s python client 預設會重試連線失敗（configuration.retries 預設 None，
        # fallback 到 urllib3 預設的 3 次重試），實測封包被丟棄、不主動拒絕連線的情況下，
        # 就算每次呼叫都帶 _request_timeout，重試 3 次疊加起來還是要等 80 秒才失敗。
        # 這裡建一份獨立的 Configuration，把 retries 設成 0，只影響這個函式用的 client，
        # 不動全域設定（其他路徑如 k8s_get_pods／healer 維持原本行為）。
        _cfg = k8s_client.Configuration.get_default_copy()
        _cfg.retries = 0
        _api_client = k8s_client.ApiClient(configuration=_cfg)
        api  = k8s_client.AppsV1Api(_api_client)
        core = k8s_client.CoreV1Api(_api_client)
        resources = None
        if memory or cpu:
            cpu_req = cpu or "100m"
            resources = k8s_client.V1ResourceRequirements(
                requests={"memory": memory, "cpu": cpu_req} if memory else {"cpu": cpu_req},
                limits  ={"memory": memory, "cpu": cpu_req} if memory else {"cpu": cpu_req},
            )
        container = k8s_client.V1Container(
            name=app_name, image=image,
            ports=[k8s_client.V1ContainerPort(container_port=port)],
            resources=resources,
        )
        deploy = k8s_client.V1Deployment(
            api_version="apps/v1", kind="Deployment",
            metadata=k8s_client.V1ObjectMeta(name=app_name),
            spec=k8s_client.V1DeploymentSpec(
                replicas=replicas,
                selector=k8s_client.V1LabelSelector(match_labels={"app": app_name}),
                template=k8s_client.V1PodTemplateSpec(
                    metadata=k8s_client.V1ObjectMeta(labels={"app": app_name}),
                    spec=k8s_client.V1PodSpec(containers=[container]),
                ),
            ),
        )
        svc = k8s_client.V1Service(
            api_version="v1", kind="Service",
            metadata=k8s_client.V1ObjectMeta(name=f"{app_name}-svc"),
            spec=k8s_client.V1ServiceSpec(
                selector={"app": app_name},
                ports=[k8s_client.V1ServicePort(port=port, target_port=port)],
                type="LoadBalancer",
            ),
        )
        try:
            api.replace_namespaced_deployment(app_name, NS, deploy, _request_timeout=(5, 10))
        except Exception:
            api.create_namespaced_deployment(NS, deploy, _request_timeout=(5, 10))
        try:
            core.replace_namespaced_service(f"{app_name}-svc", NS, svc, _request_timeout=(5, 10))
        except Exception:
            core.create_namespaced_service(NS, svc, _request_timeout=(5, 10))
        os.makedirs(YAML_DIR, exist_ok=True)
        path = os.path.join(YAML_DIR, f"{app_name}.yaml")
        with open(path, "w", encoding="utf-8") as f:
            f.write(yaml_lib.dump(deploy.to_dict()))
            f.write("---\n")
            f.write(yaml_lib.dump(svc.to_dict()))
        return True, f"已部署 {app_name}"
    except Exception as e:
        return False, str(e)

def k8s_get_pods(app_name=None):
    if not K8S_ENABLED:
        return []
    try:
        core = k8s_client.CoreV1Api()
        selector = f"app={app_name}" if app_name else None
        pods = core.list_namespaced_pod(NS, label_selector=selector)
        result = []
        for p in pods.items:
            containers = []
            if p.spec and p.spec.containers:
                for c in p.spec.containers:
                    containers.append({
                        "name": c.name,
                        "image": c.image,
                        "ports": [cp.container_port for cp in (c.ports or [])],
                        "resources": {
                            "requests": dict(c.resources.requests) if c.resources and c.resources.requests else {},
                            "limits": dict(c.resources.limits) if c.resources and c.resources.limits else {},
                        }
                    })
            cond = []
            if p.status and p.status.conditions:
                for co in p.status.conditions:
                    cond.append({"type": co.type, "status": co.status})
            result.append({
                "name"      : p.metadata.name,
                "app"       : p.metadata.labels.get("app", "") if p.metadata.labels else "",
                "phase"     : p.status.phase or "Unknown",
                "ip"        : p.status.pod_ip or "",
                "node"      : p.spec.node_name or "",
                "age"       : _fmt_k8s_time(p.metadata.creation_timestamp),
                "containers": containers,
                "conditions": cond,
                "restarts"  : sum(cs.restart_count for cs in (p.status.container_statuses or [])) if p.status and p.status.container_statuses else 0,
            })
        return result
    except Exception:
        return []

def k8s_get_deployments():
    if not K8S_ENABLED:
        return []
    try:
        api = k8s_client.AppsV1Api()
        core = k8s_client.CoreV1Api()
        deps = api.list_namespaced_deployment(NS)
        result = []
        for d in deps.items:
            updated_ts = d.metadata.creation_timestamp
            if d.status and d.status.conditions:
                for cond in d.status.conditions:
                    for attr in ("last_update_time", "last_transition_time"):
                        ts = getattr(cond, attr, None)
                        if ts and (updated_ts is None or ts > updated_ts):
                            updated_ts = ts
            try:
                labels = d.spec.selector.match_labels or {}
                selector = ",".join(f"{k}={v}" for k, v in labels.items()) or None
                if selector:
                    pods = core.list_namespaced_pod(NS, label_selector=selector)
                    for pod in pods.items:
                        ts = pod.metadata.creation_timestamp
                        if ts and (updated_ts is None or ts > updated_ts):
                            updated_ts = ts
            except Exception:
                pass
            result.append({
                "name"    : d.metadata.name,
                "replicas": d.spec.replicas or 0,
                "ready"   : d.status.ready_replicas or 0,
                "image"   : d.spec.template.spec.containers[0].image if d.spec.template.spec.containers else "",
                "age"     : _fmt_k8s_time(d.metadata.creation_timestamp),
                "updated" : _fmt_k8s_time(updated_ts),
            })
        return result
    except Exception:
        return []

def k8s_delete_deployment(app_name):
    if not K8S_ENABLED:
        return False, "K8s 未連線"
    try:
        api  = k8s_client.AppsV1Api()
        core = k8s_client.CoreV1Api()
        api.delete_namespaced_deployment(app_name, NS)
        try:
            core.delete_namespaced_service(f"{app_name}-svc", NS)
        except Exception:
            pass
        return True, f"已刪除 {app_name}"
    except Exception as e:
        return False, str(e)


def _build_agent_manifest(parsed):
    app_name = parsed.get("app_name", "auto-app")
    image = parsed.get("image", "nginx:latest")
    pods = int(parsed.get("pods", parsed.get("replicas", 1)))
    port = int(parsed.get("port", 80))
    memory = parsed.get("memory")
    cpu = parsed.get("cpu")
    container = {
        "name": app_name,
        "image": image,
        "ports": [{"containerPort": port}],
    }
    if memory or cpu:
        cpu_req = cpu or "100m"
        container["resources"] = {
            "requests": {"memory": memory, "cpu": cpu_req} if memory else {"cpu": cpu_req},
            "limits": {"memory": memory, "cpu": cpu_req} if memory else {"cpu": cpu_req},
        }
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": app_name, "namespace": NS},
        "spec": {
            "replicas": pods,
            "selector": {"matchLabels": {"app": app_name}},
            "template": {
                "metadata": {"labels": {"app": app_name}},
                "spec": {"containers": [container]},
            },
        },
    }


def _review_deployment(parsed):
    review = {
        "decision": "approve",
        "reason": "checks passed",
        "blockers": [],
        "warnings": [],
        "agents": None,
        "guardian": None,
        "dry_run": None,
    }
    manifest = _build_agent_manifest(parsed)

    try:
        from guardian.yaml_validator import validate_yaml
        guardian = validate_yaml(manifest)
        review["guardian"] = guardian
        if not guardian.get("ok"):
            review["decision"] = "block"
            review["reason"] = "Guardian validation failed"
            review["blockers"].extend(guardian.get("errors", []))
        review["warnings"].extend(guardian.get("warnings", []))
    except Exception as e:
        review["warnings"].append(f"Guardian validation unavailable: {e}")

    try:
        from agents.orchestrator import orchestrate
        agents_result = orchestrate(manifest, save_report=False)
        review["agents"] = agents_result
        if agents_result.get("decision") == "block":
            review["decision"] = "block"
            review["reason"] = agents_result.get("reason", "Agent review blocked deployment")
            review["blockers"].extend(agents_result.get("blockers", []))
        else:
            review["warnings"].extend(agents_result.get("warnings", []))
    except Exception as e:
        review["warnings"].append(f"Agent review unavailable: {e}")

    try:
        from guardian.dry_run import validate_from_llm_result
        dry = validate_from_llm_result(parsed, mode="client")
        review["dry_run"] = dry
        if dry.get("ok") is False:
            review["decision"] = "block"
            review["reason"] = "kubectl dry-run failed"
            review["blockers"].extend(dry.get("errors", []))
        elif dry.get("ok") is None:
            review["warnings"].extend(dry.get("warnings", []) or dry.get("errors", []))
        else:
            review["warnings"].extend(dry.get("warnings", []))
    except Exception as e:
        review["warnings"].append(f"dry-run unavailable: {e}")

    if review["decision"] != "block" and review["warnings"]:
        review["decision"] = "warn"
        review["reason"] = f"{len(review['warnings'])} warning(s), deployment allowed"
    return review

# ── HTML ────────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Zero-Touch Kubernetes Service Deployment Platform</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@300;400;500;600&family=DM+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{
  --bg:#FAFAFA;--surface:#FFFFFF;--border:#E5E7EB;--border2:#D1D5DB;
  --text:#111827;--text2:#6B7280;--text3:#9CA3AF;
  --green:#16A34A;--green-light:#DCFCE7;--green-mid:#BBF7D0;
  --red:#DC2626;--red-light:#FEE2E2;
  --blue:#2563EB;--blue-light:#DBEAFE;
  --yellow:#D97706;--yellow-light:#FEF3C7;
  --radius:10px;--radius-sm:6px;--shadow:0 1px 3px rgba(0,0,0,.08),0 1px 2px rgba(0,0,0,.04);
  --shadow-md:0 4px 6px rgba(0,0,0,.07),0 2px 4px rgba(0,0,0,.04);
}
body{font-family:'DM Sans',sans-serif;background:var(--bg);color:var(--text);min-height:100vh}

/* ── Auth Pages ── */
.auth-wrap{min-height:100vh;display:flex;align-items:center;justify-content:center;background:var(--bg)}
.auth-card{background:var(--surface);border:1px solid var(--border);border-radius:16px;padding:40px;width:100%;max-width:420px;box-shadow:var(--shadow-md)}
.auth-logo{display:flex;align-items:center;gap:10px;margin-bottom:28px}
.auth-logo svg{width:32px;height:32px}
.auth-logo span{font-size:18px;font-weight:600;color:var(--text)}
.auth-title{font-size:22px;font-weight:600;margin-bottom:6px}
.auth-sub{font-size:14px;color:var(--text2);margin-bottom:28px}
.form-group{margin-bottom:16px}
.form-group label{display:block;font-size:13px;font-weight:500;margin-bottom:6px;color:var(--text)}
.form-group input{width:100%;padding:10px 14px;border:1px solid var(--border2);border-radius:var(--radius-sm);font-size:14px;font-family:inherit;outline:none;transition:border .15s}
.form-group input:focus{border-color:var(--green);box-shadow:0 0 0 3px rgba(22,163,74,.1)}
.btn-primary{width:100%;padding:11px;background:var(--green);color:#fff;border:none;border-radius:var(--radius-sm);font-size:14px;font-weight:500;cursor:pointer;font-family:inherit;transition:background .15s}
.btn-primary:hover{background:#15803D}
.auth-link{text-align:center;margin-top:20px;font-size:13px;color:var(--text2)}
.auth-link a{color:var(--green);text-decoration:none;font-weight:500}
.auth-error{background:var(--red-light);color:var(--red);padding:10px 14px;border-radius:var(--radius-sm);font-size:13px;margin-bottom:16px}

/* ── Layout ── */
.layout{display:flex;height:100vh;overflow:hidden}
.sidebar{width:240px;background:var(--surface);border-right:1px solid var(--border);display:flex;flex-direction:column;flex-shrink:0}
.sidebar-logo{padding:20px 18px 16px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:10px}
.sidebar-logo svg{width:28px;height:28px;flex-shrink:0}
.sidebar-logo span{font-size:13px;line-height:1.3;font-weight:600}
.sidebar-nav{padding:12px 10px;flex:1;overflow-y:auto}
.nav-item{display:flex;align-items:center;gap:10px;padding:9px 10px;border-radius:var(--radius-sm);cursor:pointer;font-size:13.5px;font-weight:500;color:var(--text2);transition:all .15s;border:none;background:none;width:100%;text-align:left}
.nav-item:hover{background:var(--bg);color:var(--text)}
.nav-item.active{background:var(--green-light);color:var(--green)}
.nav-item svg{width:16px;height:16px;flex-shrink:0}
.nav-section{font-size:11px;font-weight:600;color:var(--text3);padding:12px 10px 4px;text-transform:uppercase;letter-spacing:.6px}
.sidebar-footer{padding:14px 18px;border-top:1px solid var(--border)}
.user-info{display:flex;align-items:center;gap:10px}
.user-avatar{width:32px;height:32px;background:var(--green);border-radius:50%;display:flex;align-items:center;justify-content:center;color:#fff;font-size:13px;font-weight:600;flex-shrink:0}
.user-name{font-size:13px;font-weight:500;flex:1}
.logout-btn{font-size:12px;color:var(--text3);cursor:pointer;border:none;background:none;font-family:inherit;padding:2px 6px;border-radius:4px}
.logout-btn:hover{color:var(--red);background:var(--red-light)}

/* ── Main ── */
.main{flex:1;overflow-y:auto;display:flex;flex-direction:column}
.page{display:none;padding:28px 32px;flex:1}
.page.active{display:block}
.page-title{font-size:20px;font-weight:600;margin-bottom:4px}
.page-sub{font-size:13px;color:var(--text2);margin-bottom:24px}

/* ── Status bar ── */
.status-bar{padding:10px 32px;background:var(--surface);border-bottom:1px solid var(--border);display:flex;align-items:center;gap:20px}
.status-pill{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--text2)}
.dot{width:7px;height:7px;border-radius:50%;background:var(--text3)}
.dot.green{background:var(--green)}
.dot.red{background:var(--red)}
.dot.yellow{background:var(--yellow);animation:pulse 1.5s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}

/* ── Cards ── */
.card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:20px;box-shadow:var(--shadow)}
.card-title{font-size:13px;font-weight:600;color:var(--text2);margin-bottom:12px;text-transform:uppercase;letter-spacing:.4px}
.grid-2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.grid-3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:16px}
.stat-num{font-size:28px;font-weight:600;line-height:1.1}
.stat-label{font-size:12px;color:var(--text2);margin-top:4px}

/* ── Deploy form ── */
.deploy-input-wrap{display:flex;gap:10px;margin-bottom:20px}
.deploy-input{flex:1;padding:12px 16px;border:1.5px solid var(--border2);border-radius:var(--radius-sm);font-size:14px;font-family:inherit;outline:none;transition:border .15s;background:var(--surface)}
.deploy-input:focus{border-color:var(--green);box-shadow:0 0 0 3px rgba(22,163,74,.1)}
.deploy-btn{padding:12px 22px;background:var(--green);color:#fff;border:none;border-radius:var(--radius-sm);font-size:14px;font-weight:500;cursor:pointer;font-family:inherit;white-space:nowrap;transition:background .15s}
.deploy-btn:hover{background:#15803D}
.deploy-btn:disabled{background:var(--text3);cursor:not-allowed}
.quick-tags{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:20px}
.tag{padding:5px 12px;border:1px solid var(--border2);border-radius:20px;font-size:12px;cursor:pointer;font-family:'DM Mono',monospace;color:var(--text2);transition:all .15s;background:var(--surface)}
.tag:hover{border-color:var(--green);color:var(--green);background:var(--green-light)}

/* ── Deploy result ── */
.result-box{padding:16px;border-radius:var(--radius-sm);font-size:13px;font-family:'DM Mono',monospace;margin-bottom:16px;display:none;white-space:pre-wrap;line-height:1.6}
.result-box.success{background:var(--green-light);color:#166534;border:1px solid var(--green-mid)}
.result-box.error{background:var(--red-light);color:#991B1B;border:1px solid #FECACA}

/* ── Deploy result v2 (dataset enrichment) ── */
.enrich-card{margin-bottom:16px;border:1px solid var(--border);border-radius:var(--radius);background:var(--surface);overflow:hidden;display:none}
.enrich-card.show{display:block}
.enrich-card.rejected{border-color:#FECACA}
.enrich-head{padding:14px 18px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.enrich-head .status-tag{font-size:12px;font-weight:600;padding:3px 10px;border-radius:12px}
.status-tag.ok{background:var(--green-light);color:#166534}
.status-tag.reject{background:var(--red-light);color:#991B1B}
.enrich-id{font-family:'DM Mono',monospace;font-size:12px;color:var(--text3);margin-left:auto}
.enrich-headline{font-size:13px;color:var(--text2)}
.enrich-body{padding:14px 18px}
.enrich-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:14px}
.enrich-field{padding:10px 12px;background:var(--bg);border-radius:var(--radius-sm);border:1px solid var(--border)}
.enrich-field .lbl{font-size:11px;color:var(--text3);text-transform:uppercase;letter-spacing:.5px;margin-bottom:4px;font-weight:600}
.enrich-field .val{font-size:13px;font-weight:600;color:var(--text);font-family:'DM Mono',monospace}
.pill{display:inline-block;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600;font-family:inherit}
.pill.simple,.pill.true{background:#DCFCE7;color:#166534}
.pill.medium{background:#FEF3C7;color:#92400E}
.pill.complex,.pill.false{background:#FEE2E2;color:#991B1B}
.pill.en{background:#DBEAFE;color:#1E40AF}
.pill.zhtw{background:#FCE7F3;color:#9D174D}
.enrich-output-title{font-size:11px;color:var(--text3);text-transform:uppercase;letter-spacing:.5px;margin-bottom:6px;font-weight:600}
.enrich-output{background:#0F172A;color:#E2E8F0;padding:14px 16px;border-radius:var(--radius-sm);font-family:'DM Mono',monospace;font-size:12.5px;line-height:1.6;white-space:pre;overflow-x:auto;margin:0}
.enrich-output .k{color:#7DD3FC}
.enrich-output .s{color:#86EFAC}
.enrich-output .n{color:#FCD34D}
.enrich-output .b{color:#F472B6}
.enrich-reject-msg{padding:10px 12px;background:var(--red-light);color:#991B1B;border-radius:var(--radius-sm);font-size:13px;margin-bottom:12px}

/* ── Table ── */
.table-wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;padding:10px 14px;font-size:11.5px;font-weight:600;color:var(--text2);border-bottom:1px solid var(--border);text-transform:uppercase;letter-spacing:.4px}
td{padding:12px 14px;border-bottom:1px solid var(--border);vertical-align:middle}
tr:last-child td{border-bottom:none}
tr:hover td{background:var(--bg)}
.badge{display:inline-flex;align-items:center;gap:5px;padding:3px 9px;border-radius:20px;font-size:11.5px;font-weight:500}
.badge.running{background:var(--green-light);color:var(--green)}
.badge.pending{background:var(--yellow-light);color:var(--yellow)}
.badge.failed{background:var(--red-light);color:var(--red)}
.badge.unknown{background:var(--bg);color:var(--text2);border:1px solid var(--border)}
.mono{font-family:'DM Mono',monospace;font-size:12px}
.btn-sm{padding:5px 12px;border-radius:var(--radius-sm);font-size:12px;font-weight:500;cursor:pointer;border:1px solid var(--border2);background:var(--surface);font-family:inherit;transition:all .15s}
.btn-sm:hover{border-color:var(--blue);color:var(--blue)}
.btn-danger{border-color:#FECACA;color:var(--red)}
.btn-danger:hover{background:var(--red-light);border-color:var(--red)}
.action-btns{display:flex;gap:6px}

/* ── Pod detail modal ── */
.modal-bg{position:fixed;inset:0;background:rgba(0,0,0,.3);z-index:1000;display:none;align-items:center;justify-content:center}
.modal-bg.open{display:flex}
.modal{background:var(--surface);border-radius:14px;width:90%;max-width:600px;max-height:85vh;overflow-y:auto;box-shadow:0 20px 40px rgba(0,0,0,.15)}
.modal-header{padding:20px 24px 16px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;background:var(--surface)}
.modal-title{font-size:15px;font-weight:600}
.modal-close{width:30px;height:30px;border-radius:50%;border:none;background:var(--bg);cursor:pointer;font-size:16px;display:flex;align-items:center;justify-content:center;color:var(--text2)}
.modal-body{padding:20px 24px}
.detail-section{margin-bottom:20px}
.detail-section-title{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.5px;color:var(--text3);margin-bottom:10px}
.detail-row{display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid var(--border);font-size:13px}
.detail-row:last-child{border-bottom:none}
.detail-key{color:var(--text2)}
.detail-val{font-family:'DM Mono',monospace;font-size:12px;text-align:right;max-width:300px;word-break:break-all}
.cond-badge{padding:2px 8px;border-radius:4px;font-size:11px;font-weight:500}
.cond-true{background:var(--green-light);color:var(--green)}
.cond-false{background:var(--red-light);color:var(--red)}

/* ── Deploy confirmation card ── */
.deploy-confirm{background:#fff;border:1px solid var(--border);border-radius:14px;padding:16px;box-shadow:var(--shadow);min-width:min(560px,100%)}
.deploy-confirm-head{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:12px}
.deploy-confirm-title{font-size:14px;font-weight:750;color:var(--text)}
.deploy-confirm-sub{font-size:12px;color:var(--text2);margin-top:2px}
.deploy-confirm-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:12px 0}
.deploy-confirm-field label{display:block;font-size:11px;font-weight:700;color:var(--text3);text-transform:uppercase;letter-spacing:.04em;margin-bottom:5px}
.deploy-confirm-field input{width:100%;border:1px solid var(--border2);border-radius:9px;padding:9px 10px;font-family:'DM Mono',monospace;font-size:12.5px;background:#fff;outline:none}
.deploy-confirm-field input:focus{border-color:var(--green);box-shadow:0 0 0 3px rgba(16,163,127,.12)}
.deploy-confirm-actions{display:flex;gap:8px;justify-content:flex-end;margin-top:12px;flex-wrap:wrap}
.deploy-confirm-note{font-size:12px;color:var(--text2);background:var(--surface2);border:1px solid var(--border);border-radius:9px;padding:9px 10px;line-height:1.45}
.deploy-confirm-error{display:none;font-size:12px;color:var(--red);background:var(--red-light);border:1px solid #fecaca;border-radius:9px;padding:8px 10px;margin-top:10px}
.deploy-confirm-btn{border:1px solid var(--border2);background:#fff;color:var(--text);border-radius:9px;padding:9px 12px;font-weight:700;cursor:pointer;font-family:inherit}
.deploy-confirm-btn.primary{background:var(--green);border-color:var(--green);color:#fff}
.deploy-confirm-btn:disabled{opacity:.55;cursor:not-allowed}
@media(max-width:760px){.deploy-confirm-grid{grid-template-columns:1fr}.deploy-confirm{min-width:0}}

/* ── Chat ── */
.chat-wrap{display:flex;flex-direction:column;height:calc(100vh - 110px)}
.chat-messages{flex:1;overflow-y:auto;padding:0 0 16px}
.msg{display:flex;gap:10px;margin-bottom:16px}
.msg.user{flex-direction:row-reverse}
.msg-bubble{max-width:70%;padding:12px 16px;border-radius:12px;font-size:13.5px;line-height:1.6}
.msg.ai .msg-bubble{background:var(--surface);border:1px solid var(--border)}
.msg.user .msg-bubble{background:var(--green);color:#fff}
.msg-avatar{width:32px;height:32px;border-radius:50%;flex-shrink:0;display:flex;align-items:center;justify-content:center;font-size:13px;font-weight:600}
.msg.ai .msg-avatar{background:var(--green-light);color:var(--green)}
.msg.user .msg-avatar{background:var(--green);color:#fff}
.chat-input-wrap{display:flex;gap:10px;padding-top:16px;border-top:1px solid var(--border)}
.chat-input{flex:1;padding:11px 14px;border:1.5px solid var(--border2);border-radius:var(--radius-sm);font-size:14px;font-family:inherit;outline:none;resize:none;transition:border .15s}
.chat-input:focus{border-color:var(--green);box-shadow:0 0 0 3px rgba(22,163,74,.1)}
.chat-send{padding:11px 20px;background:var(--green);color:#fff;border:none;border-radius:var(--radius-sm);font-size:14px;font-weight:500;cursor:pointer;font-family:inherit;transition:background .15s}
.chat-send:hover{background:#15803D}
.typing{display:flex;gap:4px;padding:12px 16px;background:var(--surface);border:1px solid var(--border);border-radius:12px;width:fit-content}
.typing span{width:6px;height:6px;background:var(--text3);border-radius:50%;animation:bounce .9s infinite}
.typing span:nth-child(2){animation-delay:.15s}
.typing span:nth-child(3){animation-delay:.3s}
@keyframes bounce{0%,100%{transform:translateY(0)}50%{transform:translateY(-5px)}}

/* ── Deploy decision court ── */
.court-panel{margin-top:16px}
.court-agents{display:grid;grid-template-columns:1fr 1fr 1fr;gap:16px;margin-bottom:16px}
.court-agent-card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:16px;min-height:120px}
.court-agent-card .court-agent-title{font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.05em;color:var(--text3);margin-bottom:10px;display:flex;align-items:center;justify-content:space-between}
.court-agent-card .court-agent-score{font-size:13px;font-weight:700;color:var(--text)}
.court-agent-card .court-agent-summary{font-size:12.5px;color:var(--text2);line-height:1.5;margin-top:8px}
.court-agent-card .court-agent-issues{margin-top:8px;display:flex;flex-direction:column;gap:6px}
.court-agent-card.thinking .court-agent-summary::after{content:'評估中';color:var(--text3)}
.court-agent-card.thinking .typing{margin-top:4px}
.court-verdict{display:none}
.result-box.warn{background:var(--yellow-light);color:#92400E;border:1px solid var(--yellow)}
.court-actions{display:flex;gap:8px;justify-content:flex-end;margin-top:12px;flex-wrap:wrap}
@media(max-width:760px){.court-agents{grid-template-columns:1fr}}

/* ── Loading ── */
.loading-overlay{position:fixed;inset:0;background:rgba(255,255,255,.95);display:flex;flex-direction:column;align-items:center;justify-content:center;z-index:9999;gap:16px}
.spinner{width:36px;height:36px;border:3px solid var(--border);border-top-color:var(--green);border-radius:50%;animation:spin 1s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.loading-text{font-size:14px;color:var(--text2)}

/* ── Empty state ── */
.empty{text-align:center;padding:48px 20px;color:var(--text2)}
.empty svg{width:40px;height:40px;margin:0 auto 12px;opacity:.3}
.empty p{font-size:14px}


/* ── Product Shell Refresh ─────────────────────────────────── */
:root{
  --bg:#f7f7f5;--surface:#ffffff;--surface2:#f4f4f2;--border:#e4e4df;--border2:#d5d5ce;
  --text:#171717;--text2:#5f6368;--text3:#9aa0a6;--green:#10a37f;--green-light:#e8f7f2;--green-mid:#b8eadb;
  --radius:12px;--radius-sm:10px;--shadow:0 1px 2px rgba(0,0,0,.04),0 8px 24px rgba(0,0,0,.04);
  --shadow-md:0 12px 32px rgba(0,0,0,.10);
}
body{background:var(--bg);font-family:'DM Sans',system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;letter-spacing:0}
.layout{height:100vh;background:var(--bg)}
.sidebar{width:300px;background:#171717;color:#f5f5f0;border-right:0;box-shadow:inset -1px 0 rgba(255,255,255,.06)}
.sidebar-logo{min-height:64px;padding:16px 18px;border-bottom:1px solid rgba(255,255,255,.08)}
.sidebar-logo span{font-size:13px;line-height:1.3;color:#fff;font-weight:700}
.sidebar-logo svg rect{fill:var(--green)}
.sidebar-nav{padding:12px 12px 16px!important;gap:2px}
.sidebar-nav button[onclick="newChat()"],.new-chat-btn{height:44px;background:#fff!important;color:#111!important;border:1px solid rgba(255,255,255,.16)!important;border-radius:12px!important;font-weight:700!important;box-shadow:0 6px 18px rgba(0,0,0,.18)}
.nav-section{color:#8f8f8a;padding:18px 10px 8px;font-size:11px;letter-spacing:.08em}
.nav-item{color:#d8d8d2;border-radius:10px;padding:10px 12px;font-size:14px;background:transparent}
.nav-item:hover{background:#242424;color:#fff}
.nav-item.active{background:#2f2f2f;color:#fff}
.nav-item svg{opacity:.85}
#chat-room-list{max-height:none!important;padding:0!important;min-height:150px!important}
.chat-room-item{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:10px 12px;border-radius:10px;cursor:pointer;margin-bottom:2px;font-size:13px;color:#d8d8d2;background:transparent;transition:background .15s,color .15s}
.chat-room-item:hover,.chat-room-item.active{background:#2f2f2f;color:#fff}
.chat-room-title{display:flex;align-items:center;gap:8px;overflow:hidden;flex:1;min-width:0}
.chat-room-title span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.chat-room-delete{opacity:0;color:#a6a6a0;font-size:14px;padding-left:6px;flex-shrink:0;transition:opacity .15s}
.chat-room-item:hover .chat-room-delete{opacity:1}
.sidebar-footer{border-top:1px solid rgba(255,255,255,.08);padding:16px 18px}
.user-avatar{background:var(--green);color:white}
.user-name{color:#fff}.logout-btn{color:#aaa}.logout-btn:hover{background:#2f2f2f;color:#fff}
.main{background:var(--bg);min-width:0;overflow:hidden}
.status-bar{height:48px;padding:0 28px;background:rgba(247,247,245,.92);backdrop-filter:blur(10px);border-bottom:1px solid var(--border);flex-shrink:0}
.page{padding:32px 40px;overflow-y:auto;height:calc(100vh - 48px)}
.page.active{display:block}.page-title{font-size:26px;font-weight:750;letter-spacing:0;margin-bottom:6px}.page-sub{font-size:15px;margin-bottom:28px;color:var(--text2)}
.card{border:1px solid var(--border);border-radius:14px;box-shadow:var(--shadow);padding:24px;background:#fff}.card-title{font-size:12px;letter-spacing:.08em;color:#747775}
.grid-3{grid-template-columns:repeat(3,minmax(0,1fr));gap:18px}.grid-2{gap:18px}.stat-num{font-size:34px;font-weight:750;color:#111}
.deploy-input-wrap{background:#fff;border:1px solid var(--border);border-radius:16px;padding:8px;gap:8px;box-shadow:0 8px 24px rgba(0,0,0,.04)}
.deploy-input{border:0;font-size:16px;padding:12px 14px}.deploy-input:focus{box-shadow:none}.deploy-btn,.btn-primary{background:var(--green);border-radius:12px;font-weight:700}.deploy-btn{padding:12px 24px}.tag{border-radius:999px;padding:7px 13px;background:#fafafa}
#page-chat{height:calc(100vh - 48px);padding:0!important;overflow:hidden;background:#fff}.chat-shell{height:100%;display:flex;flex-direction:column;background:#fff}.chat-topbar{height:54px;display:flex;align-items:center;justify-content:space-between;padding:0 28px;border-bottom:1px solid var(--border);flex-shrink:0}.chat-title{font-size:15px;font-weight:750;color:#222}.chat-subtitle{font-size:12px;color:var(--text3)}
.chat-messages{flex:1;overflow-y:auto;padding:28px max(32px,calc((100vw - 1040px)/2)) 22px!important;background:#fff}.chat-empty{min-height:100%;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;padding:48px 24px}.chat-empty-logo{width:54px;height:54px;border-radius:16px;background:#171717;color:#fff;display:flex;align-items:center;justify-content:center;font-size:24px;font-weight:800;margin-bottom:20px}.chat-empty h1{font-size:32px;font-weight:760;margin-bottom:10px}.chat-empty p{font-size:15px;color:var(--text2);max-width:560px;line-height:1.6;margin-bottom:28px}.prompt-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;width:100%;max-width:720px}.prompt-card{background:#fff;border:1px solid var(--border);border-radius:14px;padding:16px;text-align:left;cursor:pointer;transition:all .15s}.prompt-card:hover{border-color:#c9c9c2;box-shadow:0 8px 24px rgba(0,0,0,.06);transform:translateY(-1px)}.prompt-card strong{font-size:14px;color:#222}.prompt-card span{display:block;font-size:12px;color:var(--text3);margin-top:5px}
.msg{max-width:900px;margin:0 auto 22px!important;display:flex;gap:14px}.msg.user{flex-direction:row-reverse}.msg-avatar{width:32px;height:32px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-weight:750;font-size:13px;flex-shrink:0;background:#171717;color:#fff}.msg.user .msg-avatar{background:var(--green)}.msg-bubble{max-width:76%;border-radius:18px;padding:13px 16px;font-size:14.5px;line-height:1.62;box-shadow:none}.msg.user .msg-bubble{background:var(--green);color:#fff}.msg.ai .msg-bubble{background:#f4f4f2;border:1px solid var(--border);color:#222}
.chat-composer{padding:14px max(24px,calc((100vw - 960px)/2)) 20px;background:#fff;border-top:1px solid var(--border);flex-shrink:0}.composer-box{display:flex;align-items:flex-end;gap:10px;border:1px solid var(--border2);border-radius:18px;background:#fff;padding:10px 10px 10px 16px;box-shadow:0 8px 24px rgba(0,0,0,.06)}.chat-input{min-height:42px;max-height:180px;resize:none;border:0;outline:none;flex:1;font:inherit;font-size:15px;line-height:1.5;padding:9px 4px;background:transparent}.chat-send{width:42px;height:42px;border-radius:12px;border:0;background:var(--green);color:#fff;font-size:0;cursor:pointer;flex-shrink:0}.chat-send:before{content:'↑';font-size:22px;line-height:1}.composer-hint{text-align:center;font-size:11px;color:var(--text3);margin-top:8px}
.table-wrap{border:1px solid var(--border);border-radius:14px;overflow:hidden}table{background:#fff}th{background:#fafaf8}td,th{padding:13px 16px}.enrich-card{border-radius:14px}.enrich-output{border-radius:12px}
@media (max-width:900px){.sidebar{width:260px}.page{padding:24px}.grid-3,.prompt-grid{grid-template-columns:1fr}.msg-bubble{max-width:86%}}



/* ── Polish Fixes ─────────────────────────────────────────── */
html,body{height:100%;overflow:hidden}
.sidebar{height:100vh;overflow:hidden}
.sidebar-nav{overflow-y:auto!important;overflow-x:hidden!important;min-height:0;scrollbar-width:thin;scrollbar-color:#4b4b4b transparent;padding-bottom:22px!important}
.sidebar-nav::-webkit-scrollbar{width:8px}.sidebar-nav::-webkit-scrollbar-track{background:transparent}.sidebar-nav::-webkit-scrollbar-thumb{background:#4b4b4b;border-radius:999px;border:2px solid #171717}
#chat-room-list{flex:0 0 auto!important;max-height:210px!important;overflow-y:auto!important;overflow-x:hidden!important;scrollbar-width:thin;scrollbar-color:#4b4b4b transparent}
#chat-room-list::-webkit-scrollbar{width:8px}#chat-room-list::-webkit-scrollbar-track{background:transparent}#chat-room-list::-webkit-scrollbar-thumb{background:#4b4b4b;border-radius:999px;border:2px solid #171717}
.main{height:100vh;overflow:hidden}.page{height:calc(100vh - 48px);overflow-y:auto}.page#page-chat{overflow:hidden!important}
.chat-shell{min-height:0}.chat-messages{min-height:0;overscroll-behavior:contain;scrollbar-width:thin;scrollbar-color:#c8c8c1 transparent}.chat-messages::-webkit-scrollbar{width:10px}.chat-messages::-webkit-scrollbar-track{background:transparent}.chat-messages::-webkit-scrollbar-thumb{background:#c8c8c1;border-radius:999px;border:3px solid #fff}
.chat-empty{min-height:100%;justify-content:center}.chat-send{display:flex!important;align-items:center!important;justify-content:center!important;padding:0!important}.chat-send:before{display:block;line-height:1;transform:translateY(-1px)}
.chat-composer{position:relative;z-index:2}.composer-box:focus-within{border-color:var(--green);box-shadow:0 0 0 4px rgba(16,163,127,.12),0 8px 24px rgba(0,0,0,.06)}

/* ── Compact deploy confirmation ─────────────────────────── */
.msg.ai .msg-bubble:has(.deploy-confirm){background:transparent!important;border:0!important;padding:0!important;box-shadow:none!important;max-width:min(560px,92%)!important;width:min(560px,92%)!important}
.deploy-confirm{width:100%!important;min-width:0!important;border-radius:12px!important;padding:14px!important;box-shadow:0 6px 18px rgba(0,0,0,.06)!important;background:#fff!important}
.deploy-confirm-head{margin-bottom:10px!important;align-items:flex-start!important}.deploy-confirm-title{font-size:14px!important}.deploy-confirm-sub{font-size:12px!important;margin-top:1px!important}.deploy-confirm .badge{font-size:11px!important;padding:3px 8px!important;white-space:nowrap}
.deploy-confirm-grid{grid-template-columns:1.1fr 1.1fr .55fr .55fr!important;gap:8px!important;margin:10px 0!important}.deploy-confirm-field label{font-size:10px!important;margin-bottom:4px!important}.deploy-confirm-field input{height:34px!important;padding:7px 9px!important;font-size:12px!important;border-radius:8px!important}.deploy-confirm-field.memory{grid-column:1 / -1}.deploy-confirm-note{font-size:11.5px!important;padding:8px 9px!important;border-radius:8px!important}.deploy-confirm-actions{margin-top:10px!important}.deploy-confirm-btn{padding:7px 10px!important;border-radius:8px!important;font-size:12px!important}.deploy-confirm-error{font-size:11.5px!important;padding:7px 9px!important;margin-top:8px!important}
@media(max-width:760px){.deploy-confirm-grid{grid-template-columns:1fr 1fr!important}.deploy-confirm-field.memory{grid-column:1 / -1}.msg.ai .msg-bubble:has(.deploy-confirm){max-width:96%!important;width:96%!important}}

</style>
</head>
<body>

{% if not logged_in and page != 'register' %}
<!-- ── Login Page ── -->
<div class="auth-wrap">
  <div class="auth-card">
    <div class="auth-logo">
      <svg viewBox="0 0 28 28" fill="none">
        <rect width="28" height="28" rx="7" fill="#16A34A"/>
        <path d="M8 14h12M14 8v12" stroke="#fff" stroke-width="2" stroke-linecap="round"/>
      </svg>
      <span>ZeroTouch K8s</span>
    </div>
    <div class="auth-title">Welcome back</div>
    <div class="auth-sub">Sign in to your account to continue</div>
    {% if error %}<div class="auth-error">{{ error }}</div>{% endif %}
    <form method="POST" action="/auth/login">
      <div class="form-group">
        <label>Username</label>
        <input type="text" name="username" placeholder="Enter your username" required autofocus>
      </div>
      <div class="form-group">
        <label>Password</label>
        <input type="password" name="password" placeholder="Enter your password" required>
      </div>
      <button class="btn-primary" type="submit">Sign In</button>
    </form>
    <div class="auth-link">Don't have an account? <a href="/auth/register">Register</a></div>
  </div>
</div>

{% elif page == 'register' %}
<!-- ── Register Page ── -->
<div class="auth-wrap">
  <div class="auth-card">
    <div class="auth-logo">
      <svg viewBox="0 0 28 28" fill="none">
        <rect width="28" height="28" rx="7" fill="#16A34A"/>
        <path d="M8 14h12M14 8v12" stroke="#fff" stroke-width="2" stroke-linecap="round"/>
      </svg>
      <span>ZeroTouch K8s</span>
    </div>
    <div class="auth-title">Create account</div>
    <div class="auth-sub">Get started with ZeroTouch K8s</div>
    {% if error %}<div class="auth-error">{{ error }}</div>{% endif %}
    <form method="POST" action="/auth/register">
      <div class="form-group">
        <label>Username</label>
        <input type="text" name="username" placeholder="Choose a username" required autofocus>
      </div>
      <div class="form-group">
        <label>Password</label>
        <input type="password" name="password" placeholder="Create a password" required>
      </div>
      <div class="form-group">
        <label>Confirm Password</label>
        <input type="password" name="confirm" placeholder="Confirm your password" required>
      </div>
      <button class="btn-primary" type="submit">Create Account</button>
    </form>
    <div class="auth-link">Already have an account? <a href="/">Sign In</a></div>
  </div>
</div>

{% else %}
<!-- ── Main App ── -->
<div class="layout">
  <!-- Sidebar -->
  <aside class="sidebar">
    <div class="sidebar-logo">
      <svg viewBox="0 0 28 28" fill="none">
        <rect width="28" height="28" rx="7" fill="#16A34A"/>
        <path d="M8 14h12M14 8v12" stroke="#fff" stroke-width="2" stroke-linecap="round"/>
      </svg>
      <span>Zero-Touch Kubernetes Service Deployment Platform</span>
    </div>
    <nav class="sidebar-nav" style="display:flex;flex-direction:column;overflow-y:auto;overflow-x:hidden">
      <div style="padding:10px 10px 6px">
        <button class="new-chat-btn" onclick="newChat()" style="width:100%;padding:9px 12px;background:var(--green);color:#fff;border:none;border-radius:8px;font-size:13px;font-weight:600;cursor:pointer;display:flex;align-items:center;justify-content:center;gap:6px">
          <span style="font-size:18px;line-height:1">+</span> New Chat
        </button>
      </div>
      <div class="nav-section">Chats</div>
      <div id="chat-room-list" style="flex:1;overflow-y:auto;padding:0 6px;min-height:60px;max-height:200px"></div>
      <div class="nav-section">Main</div>
      <button class="nav-item active" data-page="chat" onclick="showPage('chat')">
        <svg viewBox="0 0 16 16" fill="none"><path d="M2.5 3.5a2 2 0 012-2h7a2 2 0 012 2v5a2 2 0 01-2 2H8l-3.5 3v-3a2 2 0 01-2-2v-5z" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/></svg>
        Chat
      </button>
      <button class="nav-item" data-page="deploy" onclick="showPage('deploy')">
        <svg viewBox="0 0 16 16" fill="none"><rect x="2" y="2" width="5" height="5" rx="1" fill="currentColor"/><rect x="9" y="2" width="5" height="5" rx="1" fill="currentColor" opacity=".5"/><rect x="2" y="9" width="5" height="5" rx="1" fill="currentColor" opacity=".5"/><rect x="9" y="9" width="5" height="5" rx="1" fill="currentColor"/></svg>
        Deploy Console
      </button>
      <button class="nav-item" data-page="pods" onclick="showPage('pods')">
        <svg viewBox="0 0 16 16" fill="none"><circle cx="8" cy="8" r="5" stroke="currentColor" stroke-width="1.5"/><circle cx="8" cy="8" r="2" fill="currentColor"/></svg>
        Pods
      </button>
      <button class="nav-item" data-page="deployments" onclick="showPage('deployments')">
        <svg viewBox="0 0 16 16" fill="none"><path d="M2 4h12M2 8h12M2 12h8" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>
        Deployments
      </button>
      <div class="nav-section">Tools</div>
      <button class="nav-item" data-page="gitops" onclick="showPage('gitops')">
        <svg viewBox="0 0 16 16" fill="none"><circle cx="5" cy="4" r="2" stroke="currentColor" stroke-width="1.5"/><circle cx="11" cy="12" r="2" stroke="currentColor" stroke-width="1.5"/><circle cx="11" cy="4" r="2" stroke="currentColor" stroke-width="1.5"/><path d="M5 6v1a3 3 0 003 3h1M11 6v2" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>
        GitOps Log
      </button>
      <button class="nav-item" data-page="healer" onclick="showPage('healer')">
        <svg viewBox="0 0 16 16" fill="none"><path d="M8 2v12M2 8h12" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>
        Healer
      </button>
      <button class="nav-item" data-page="metrics" onclick="showPage('metrics')">
        <svg viewBox="0 0 16 16" fill="none"><path d="M2 12L5 8l3 2 3-4 3 2" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>
        Metrics
      </button>
      <button class="nav-item" data-page="dataset" onclick="showPage('dataset')">
        <svg viewBox="0 0 16 16" fill="none"><rect x="1" y="3" width="14" height="2" rx="1" fill="currentColor"/><rect x="1" y="7" width="14" height="2" rx="1" fill="currentColor" opacity=".6"/><rect x="1" y="11" width="9" height="2" rx="1" fill="currentColor" opacity=".3"/></svg>
        Dataset
      </button>
      <button class="nav-item" data-page="kb" onclick="showPage('kb')">
        <svg viewBox="0 0 16 16" fill="none"><path d="M3 3h10v3H3z" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/><path d="M3 6v7a1 1 0 001 1h8a1 1 0 001-1V6" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/><path d="M6.5 9.5h3" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>
        Knowledge Base
      </button>
    </nav>
    <div class="sidebar-footer">
      <div class="user-info">
        <div class="user-avatar">{{ username[0].upper() }}</div>
        <div class="user-name">{{ username }}</div>
        <form method="POST" action="/auth/logout" style="margin:0">
          <button class="logout-btn" type="submit">Out</button>
        </form>
      </div>
    </div>
  </aside>

  <!-- Main Content -->
  <div class="main">
    <!-- Status Bar -->
    <div class="status-bar">
      <div class="status-pill">
        <div class="dot" id="model-dot"></div>
        <span id="model-status">Model loading...</span>
      </div>
      <div class="status-pill">
        <div class="dot {% if k8s %}green{% else %}red{% endif %}"></div>
        <span>K8s {% if k8s %}Connected{% else %}Simulation{% endif %}</span>
      </div>
      <div class="status-pill" style="margin-left:auto;font-size:12px;color:var(--text3)" id="clock"></div>
    </div>

    <!-- Dashboard Page -->
    <div class="page" id="page-deploy">
      <div class="page-title">Deploy</div>
      <div class="page-sub">Deploy services using natural language</div>

      <div class="card" style="margin-bottom:16px">
        <div class="card-title">Natural Language Deploy</div>
        <div class="deploy-input-wrap">
          <input class="deploy-input" id="deploy-input" placeholder='e.g. "deploy 3 nginx:latest pods for web-frontend, port 80"' onkeydown="if(event.key==='Enter')doDeploy()">
          <button class="deploy-btn" id="deploy-btn" onclick="doDeploy()">Deploy →</button>
        </div>
        <div class="quick-tags">
          <span class="tag" onclick="setInput(this)">nginx ×1</span>
          <span class="tag" onclick="setInput(this)">redis ×2</span>
          <span class="tag" onclick="setInput(this)">postgres db</span>
          <span class="tag" onclick="setInput(this)">node api ×4</span>
          <span class="tag" onclick="setInput(this)">golang svc ×3</span>
          <span class="tag" onclick="setInput(this)">python ×5</span>
        </div>

        <!-- ── Deploy decision court (三代理評審動畫) ── -->
        <div class="court-panel" id="court-panel" style="display:none">
          <div class="court-agents">
            <div class="court-agent-card pending" id="court-security">
              <div class="court-agent-title"><span>Security</span><span class="court-agent-score" id="court-security-score"></span></div>
              <div class="court-agent-issues" id="court-security-issues"></div>
              <div class="court-agent-summary" id="court-security-summary"></div>
            </div>
            <div class="court-agent-card pending" id="court-cost">
              <div class="court-agent-title"><span>Cost</span><span class="court-agent-score" id="court-cost-score"></span></div>
              <div class="court-agent-issues" id="court-cost-issues"></div>
              <div class="court-agent-summary" id="court-cost-summary"></div>
            </div>
            <div class="court-agent-card pending" id="court-perf">
              <div class="court-agent-title"><span>Performance</span><span class="court-agent-score" id="court-perf-score"></span></div>
              <div class="court-agent-issues" id="court-perf-issues"></div>
              <div class="court-agent-summary" id="court-perf-summary"></div>
            </div>
          </div>
          <div class="result-box court-verdict" id="court-verdict"></div>
          <div class="court-actions" id="court-actions"></div>
        </div>

        <!-- ── Dataset enrichment card (新增欄位顯示) ── -->
        <div class="enrich-card" id="enrich-card">
          <div class="enrich-head">
            <span class="status-tag" id="enrich-status">—</span>
            <span class="enrich-headline" id="enrich-headline">Parsed</span>
            <span class="enrich-id" id="enrich-id"></span>
          </div>
          <div class="enrich-body">
            <div class="enrich-reject-msg" id="enrich-reject-msg" style="display:none"></div>
            <div class="enrich-grid">
              <div class="enrich-field">
                <div class="lbl">is_k8s</div>
                <div class="val" id="enrich-isk8s">—</div>
              </div>
              <div class="enrich-field">
                <div class="lbl">complexity</div>
                <div class="val" id="enrich-complexity">—</div>
              </div>
              <div class="enrich-field">
                <div class="lbl">language</div>
                <div class="val" id="enrich-language">—</div>
              </div>
              <div class="enrich-field">
                <div class="lbl">namespace</div>
                <div class="val" id="enrich-namespace">—</div>
              </div>
            </div>
            <div class="enrich-output-title">output (dataset ground truth)</div>
            <pre class="enrich-output" id="enrich-output">{}</pre>
          </div>
        </div>

        <div class="result-box" id="result-box"></div>
      </div>

      <div class="grid-3">
        <div class="card">
          <div class="card-title">Pods</div>
          <div class="stat-num" id="stat-pods">—</div>
          <div class="stat-label">Total running</div>
        </div>
        <div class="card">
          <div class="card-title">Deployments</div>
          <div class="stat-num" id="stat-deps">—</div>
          <div class="stat-label">Active deployments</div>
        </div>
        <div class="card">
          <div class="card-title">Model</div>
          <div class="stat-num" style="font-size:18px" id="stat-model">—</div>
          <div class="stat-label">LLaMA-3.1 + LoRA</div>
        </div>
      </div>
    </div>

    <!-- Pods Page -->
    <div class="page" id="page-pods">
      <div class="page-title">Pods</div>
      <div class="page-sub">All running pods in the cluster</div>
      <div class="card">
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Name</th>
                <th>App</th>
                <th>Status</th>
                <th>IP</th>
                <th>Node</th>
                <th>Restarts</th>
                <th>Created</th>
                <th>Updated</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody id="pods-tbody">
              <tr><td colspan="8" class="empty"><p>Loading...</p></td></tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- Deployments Page -->
    <div class="page" id="page-deployments">
      <div class="page-title">Deployments</div>
      <div class="page-sub">Manage your Kubernetes deployments</div>
      <div class="card">
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Name</th>
                <th>Image</th>
                <th>Replicas</th>
                <th>Ready</th>
                <th>Created</th>
                <th>Updated</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody id="deps-tbody">
              <tr><td colspan="7" class="empty"><p>Loading...</p></td></tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- Chat Page -->
    <div class="page active" id="page-chat" style="padding:0;overflow:hidden">
      <div class="chat-shell">
        <div class="chat-topbar">
          <div>
            <div class="chat-title">ZeroTouch K8s Assistant</div>
            <div class="chat-subtitle">Chat, deploy, inspect, and recover Kubernetes services</div>
          </div>
          <div class="status-pill"><div class="dot green"></div><span>Workspace ready</span></div>
        </div>
        <div class="chat-messages" id="chat-messages"></div>
        <div class="chat-composer">
          <div class="composer-box">
            <textarea class="chat-input" id="chat-input" placeholder="Message ZeroTouch K8s..." rows="1" oninput="autoGrowChatInput(this)" onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();sendChat()}"></textarea>
            <button class="chat-send" onclick="sendChat()" aria-label="Send message">Send</button>
          </div>
          <div class="composer-hint">Ask about Kubernetes, deploy services, list pods, scale workloads, or troubleshoot failures.</div>
        </div>
      </div>
    </div>

    <div class="page" id="page-dataset">
      <div class="page-title">Dataset Manager</div>
      <div class="page-sub">Enrich &amp; inspect the K8s training dataset</div>
      <div class="grid-3" style="margin-bottom:16px">
        <div class="card"><div class="card-title">Total Records</div><div class="stat-num" id="ds-total">--</div><div class="stat-label">across all files</div></div>
        <div class="card"><div class="card-title">Output Filled</div><div class="stat-num" id="ds-output-pct">--</div><div class="stat-label">ground truth coverage</div></div>
        <div class="card"><div class="card-title">K8s / Non-K8s</div><div class="stat-num" id="ds-k8s-ratio">--</div><div class="stat-label">is_k8s ratio</div></div>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px">
        <div class="card"><div class="card-title">Files</div><div id="ds-files" style="margin-top:8px;font-size:13px;color:var(--text2)">Loading...</div></div>
        <div class="card"><div class="card-title">Top Categories</div><div id="ds-categories" style="margin-top:8px;font-size:13px;color:var(--text2)">Loading...</div></div>
      </div>
      <div class="card">
        <div class="card-title">Run Enrichment</div>
        <div style="display:flex;gap:10px;margin:12px 0 8px;flex-wrap:wrap">
          <button class="btn-primary" onclick="runEnrich('--skip-output')">Quick Fill (rules only)</button>
          <button class="btn-primary" onclick="runEnrich('')">Full Enrich (LLaMA output)</button>
          <button class="btn-primary" onclick="runEnrich('--dry-run')" style="background:var(--surface);color:var(--text);border:1px solid var(--border)">Dry Run</button>
          <button class="btn-primary" onclick="loadDatasetStats()" style="background:var(--surface);color:var(--text);border:1px solid var(--border)">Refresh Stats</button>
        </div>
        <div id="ds-run-status" style="font-size:12px;color:var(--text3);margin-bottom:6px"></div>
        <pre id="ds-log" style="background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:12px;font-size:12px;max-height:320px;overflow-y:auto;white-space:pre-wrap;color:var(--text2)">Log will appear here...</pre>
      </div>
    </div>

    <div class="page" id="page-kb">
      <div class="page-title">Knowledge Base (RAG)</div>
      <div class="page-sub">Manage the documents that power retrieval-augmented answers &amp; measure retrieval quality</div>

      <div class="grid-3" style="margin-bottom:16px">
        <div class="card"><div class="card-title">Retrieval Method</div><div class="stat-num" id="kb-method" style="font-size:20px">--</div><div class="stat-label" id="kb-method-sub">embedding model</div></div>
        <div class="card"><div class="card-title">Indexed Chunks</div><div class="stat-num" id="kb-chunks">--</div><div class="stat-label">across all documents</div></div>
        <div class="card"><div class="card-title">Last Built</div><div class="stat-num" style="font-size:14px" id="kb-built-at">--</div><div class="stat-label" id="kb-built-sub">build time</div></div>
      </div>

      <div style="display:grid;grid-template-columns:1.2fr 1fr;gap:16px;margin-bottom:16px;align-items:start">
        <div class="card">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
            <div class="card-title" style="margin:0">Documents</div>
            <button class="btn-primary" onclick="rebuildKB()" id="kb-rebuild-btn">Rebuild Index</button>
          </div>
          <div id="kb-doc-list" style="font-size:13px;color:var(--text2)">Loading...</div>
          <div id="kb-rebuild-status" style="font-size:12px;color:var(--text3);margin-top:8px"></div>
        </div>
        <div class="card">
          <div class="card-title">Add Document</div>
          <div style="margin-top:10px;display:flex;flex-direction:column;gap:8px">
            <input id="kb-new-filename" class="deploy-input" placeholder="filename.md (only .md / .txt)">
            <textarea id="kb-new-content" placeholder="Markdown or plain text content..." style="width:100%;min-height:120px;padding:10px;border:1px solid var(--border);border-radius:8px;font-size:13px;font-family:inherit;background:var(--surface);color:var(--text);resize:vertical;box-sizing:border-box"></textarea>
            <button class="btn-primary" onclick="addKBDoc()">Upload &amp; Rebuild</button>
            <div id="kb-add-status" style="font-size:12px;color:var(--text3)"></div>
          </div>
        </div>
      </div>

      <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px">
        <div class="card">
          <div class="card-title">Test a Query</div>
          <div style="margin-top:10px;display:flex;gap:8px">
            <input id="kb-query-input" class="deploy-input" placeholder="e.g. CrashLoopBackOff 怎麼解決" onkeydown="if(event.key==='Enter')runKBQuery()">
            <button class="btn-primary" onclick="runKBQuery()">Search</button>
          </div>
          <div id="kb-query-results" style="margin-top:12px;font-size:13px;color:var(--text2)"></div>
        </div>
        <div class="card">
          <div style="display:flex;justify-content:space-between;align-items:center">
            <div class="card-title" style="margin:0">Retrieval Quality Evaluation</div>
            <button class="btn-primary" onclick="runKBEval()" id="kb-eval-btn">Run Evaluation</button>
          </div>
          <div id="kb-eval-results" style="margin-top:12px;font-size:13px;color:var(--text2)">Run an evaluation to compare ChromaDB vs the legacy TF-IDF method on a curated test set.</div>
        </div>
      </div>
    </div>

    <div class="page" id="page-gitops">
      <div class="page-title">GitOps Log</div>
      <div class="page-sub">Deployment history and rollback</div>
      <div class="card">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
          <span style="font-size:13px;color:var(--text2)">All git commits for deployments</span>
          <button class="btn-primary" onclick="loadGitops()">Refresh</button>
        </div>
        <div id="gitops-list"><div style="color:var(--text3);font-size:13px">Loading...</div></div>
      </div>
    </div>

    <div class="page" id="page-healer">
      <div class="page-title">Healer</div>
      <div class="page-sub">Pod auto self-healing</div>
      <div class="grid-3" style="margin-bottom:16px">
        <div class="card"><div class="card-title">Issues Found</div><div class="stat-num" id="healer-count">--</div><div class="stat-label">Abnormal pods</div></div>
        <div class="card"><div class="card-title">Last Scan</div><div class="stat-num" style="font-size:14px" id="healer-time">--</div><div class="stat-label">Scan time</div></div>
        <div class="card"><div class="card-title">Status</div><div class="stat-num" id="healer-fixed">OK</div><div class="stat-label">Healer state</div></div>
      </div>
      <div class="card">
        <div style="display:flex;gap:10px;margin-bottom:12px">
          <button class="btn-primary" onclick="loadHealer()">Scan Now</button>
          <button class="btn-primary" onclick="healerAutoFix()" style="background:#DC2626">Auto Fix All</button>
        </div>
        <div id="healer-list"><div style="color:var(--text3);font-size:13px">Loading...</div></div>
      </div>
    </div>

    <div class="page" id="page-metrics">
      <div class="page-title">Metrics</div>
      <div class="page-sub">Prometheus observability</div>
      <div class="grid-3" style="margin-bottom:16px">
        <div class="card"><div class="card-title">Prometheus</div><div class="stat-num" id="prom-status">--</div><div class="stat-label">Connection</div></div>
        <div class="card"><div class="card-title">Running Pods</div><div class="stat-num" id="prom-pods">--</div><div class="stat-label">default namespace</div></div>
        <div class="card"><div class="card-title">Endpoint</div><div class="stat-num" style="font-size:13px" id="prom-url">--</div><div class="stat-label">Prometheus URL</div></div>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px">
        <div class="card">
          <div class="card-title">Live Metrics</div>
          <div id="metrics-rows" style="margin-top:8px;font-size:13px;color:var(--text2)">Loading...</div>
        </div>
        <div class="card">
          <div class="card-title">PromQL Quick Reference</div>
          <div style="margin-top:8px">
            <div style="display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid var(--border)"><span style="font-size:12px;color:var(--text2)">All pods</span><code style="font-size:11px;background:var(--bg);padding:2px 6px;border-radius:4px">count(kube_pod_info)</code></div>
            <div style="display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid var(--border)"><span style="font-size:12px;color:var(--text2)">Running pods</span><code style="font-size:11px;background:var(--bg);padding:2px 6px;border-radius:4px">kube_pod_status_phase</code></div>
            <div style="display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid var(--border)"><span style="font-size:12px;color:var(--text2)">Deployments</span><code style="font-size:11px;background:var(--bg);padding:2px 6px;border-radius:4px">kube_deployment_spec_replicas</code></div>
            <div style="display:flex;justify-content:space-between;padding:6px 0"><span style="font-size:12px;color:var(--text2)">Prometheus up</span><code style="font-size:11px;background:var(--bg);padding:2px 6px;border-radius:4px">up</code></div>
            <div style="margin-top:10px;font-size:11px;color:var(--text3)">Full UI: <a href="http://192.168.50.219:30922" target="_blank" style="color:var(--green)">Prometheus (port 30922)</a></div>
          </div>
        </div>
      </div>
    </div>

  </div>
</div>

<!-- Pod Detail Modal -->
<div class="modal-bg" id="pod-modal">
  <div class="modal">
    <div class="modal-header">
      <div class="modal-title" id="modal-pod-name">Pod Details</div>
      <button class="modal-close" onclick="closeModal()">✕</button>
    </div>
    <div class="modal-body" id="modal-pod-body"></div>
  </div>
</div>

{% endif %}

<script>
// ── Auth guard ──
const loggedIn = {{ 'true' if logged_in else 'false' }};

// ── Clock ──
function updateClock(){
  const el = document.getElementById('clock');
  if(el) el.textContent = new Date().toLocaleTimeString('en-GB');
}
setInterval(updateClock, 1000);
updateClock();

function autoGrowChatInput(el){
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 180) + 'px';
}

// ── Page nav ──
function showPage(name){
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  const page = document.getElementById('page-' + name);
  if(page) page.classList.add('active');
  const nav = document.querySelector(`.nav-item[data-page="${name}"]`);
  if(nav) nav.classList.add('active');

  if(name === 'chat') { initChats(); setTimeout(()=>document.getElementById('chat-input')?.focus(), 0); }
  if(name === 'pods') loadPods();
  if(name === 'deployments') loadDeployments();
  if(name === 'dataset') loadDatasetStats();
  if(name === 'gitops') loadGitops();
  if(name === 'healer') loadHealer();
  if(name === 'metrics') loadMetrics();
  if(name === 'kb') loadKB();
}

// ── Status polling ──
async function pollStatus(){
  try {
    const r = await fetch('/api/status');
    const d = await r.json();
    const dot = document.getElementById('model-dot');
    const txt = document.getElementById('stat-model');
    const ms  = document.getElementById('model-status');
    if(d.model_ready){
      dot.className = 'dot green';
      if(ms) ms.textContent = 'Model Ready';
      if(txt) txt.textContent = 'Ready';
    } else {
      dot.className = 'dot yellow';
      if(ms) ms.textContent = 'Model Loading...';
      if(txt) txt.textContent = 'Loading';
    }
  } catch(e){}
}
if(loggedIn){ pollStatus(); setInterval(pollStatus, 4000); }

// ── Stats ──
async function loadStats(){
  try {
    const [pr, dr] = await Promise.all([fetch('/api/pods'), fetch('/api/deployments')]);
    const pd = await pr.json(); const dd = await dr.json();
    const sp = document.getElementById('stat-pods');
    const sd = document.getElementById('stat-deps');
    if(sp) sp.textContent = pd.pods.length;
    if(sd) sd.textContent = dd.deployments.length;
  } catch(e){}
}
if(loggedIn){ loadStats(); setInterval(loadStats, 8000); }

// ── Quick tags ──
const TAG_MAP = {
  'nginx ×1': 'deploy 1 nginx:latest pod for web-frontend, port 80',
  'redis ×2': 'start 2 redis:7-alpine pods for cache-server, port 6379',
  'postgres db': 'launch 3 postgres:15 pods named db-primary, port 5432',
  'node api ×4': 'spin up 4 node:20-alpine pods for api-gateway, port 3000',
  'golang svc ×3': 'create 3 golang:1.21-alpine pods for scheduler',
  'python ×5': 'run 5 python:3.11-slim pods for data-processor',
};
function setInput(el){
  const inp = document.getElementById('deploy-input');
  if(inp) inp.value = TAG_MAP[el.textContent.trim()] || el.textContent;
}

// ── Deploy ──
function _escapeHtml(s){ return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function _prettyJson(obj){
  // 帶語法高亮的 JSON pretty printer
  const json = JSON.stringify(obj, null, 2);
  return _escapeHtml(json)
    .replace(/(&quot;[^&]*?&quot;)(\s*:)/g, '<span class="k">$1</span>$2')
    .replace(/:\s*(&quot;[^&]*?&quot;)/g, ': <span class="s">$1</span>')
    .replace(/:\s*(-?\d+(?:\.\d+)?)/g, ': <span class="n">$1</span>')
    .replace(/:\s*(true|false|null)\b/g, ': <span class="b">$1</span>');
}
function renderEnrichCard(p, info){
  // info = { ok: bool, headline: str, rejectMsg: str|null }
  const card = document.getElementById('enrich-card');
  card.classList.add('show');
  card.classList.toggle('rejected', !info.ok);

  const statusTag = document.getElementById('enrich-status');
  statusTag.textContent = info.ok ? '✓ Accepted' : '✗ Rejected';
  statusTag.className = 'status-tag ' + (info.ok ? 'ok' : 'reject');

  document.getElementById('enrich-headline').textContent = info.headline;
  document.getElementById('enrich-id').textContent = p.id ? '#' + p.id : '';

  const rejBox = document.getElementById('enrich-reject-msg');
  if(info.rejectMsg){
    rejBox.style.display = 'block';
    rejBox.textContent = info.rejectMsg;
  } else {
    rejBox.style.display = 'none';
  }

  // 4 個 pill 欄位
  const isk8s = String(p.is_k8s);
  document.getElementById('enrich-isk8s').innerHTML =
    `<span class="pill ${isk8s}">${isk8s}</span>`;
  document.getElementById('enrich-complexity').innerHTML =
    `<span class="pill ${p.complexity}">${p.complexity}</span>`;
  const langCls = (p.language === 'zh-tw') ? 'zhtw' : 'en';
  document.getElementById('enrich-language').innerHTML =
    `<span class="pill ${langCls}">${p.language}</span>`;
  document.getElementById('enrich-namespace').textContent = p.namespace || 'default';

  // 結構化 output JSON (dataset ground truth)
  document.getElementById('enrich-output').innerHTML = _prettyJson(p.output || {});
}

let courtRequestId = 0;

async function doDeploy(){
  const inp = document.getElementById('deploy-input');
  const btn = document.getElementById('deploy-btn');
  const box = document.getElementById('result-box');
  const card = document.getElementById('enrich-card');
  const panel = document.getElementById('court-panel');
  const text = inp.value.trim();
  if(!text) return;
  const myId = ++courtRequestId;
  btn.disabled = true;
  inp.disabled = true;
  btn.textContent = 'Reviewing...';
  box.style.display = 'none';
  card.classList.remove('show');
  panel.style.display = 'none';
  try {
    const r = await fetch('/api/deploy/parse', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({input: text})
    });
    if(myId !== courtRequestId) return;
    const d = await r.json();
    if(myId !== courtRequestId) return;
    if(!r.ok || d.error){
      box.style.display = 'block';
      box.className = 'result-box error';
      box.textContent = '✗ Error: ' + (d.error || 'request failed');
      btn.disabled = false;
      inp.disabled = false;
      btn.textContent = 'Deploy →';
    } else if(d.rejected){
      // Guardian 拒絕:只顯示 enrichment 卡片,不送 K8s
      renderEnrichCard(d.parsed, {
        ok: false,
        headline: 'Guardian blocked this request',
        rejectMsg: d.reason || 'Not a K8s request'
      });
      btn.disabled = false;
      inp.disabled = false;
      btn.textContent = 'Deploy →';
    } else {
      renderEnrichCard(d.parsed, {
        ok: true,
        headline: 'Reviewed by agents — confirm to deploy',
        rejectMsg: null
      });
      renderDecisionCourt(myId, text, d.raw || d.parsed, d.review);
    }
  } catch(e){
    if(myId !== courtRequestId) return;
    box.style.display = 'block';
    box.className = 'result-box error';
    box.textContent = '✗ Network error';
    btn.disabled = false;
    inp.disabled = false;
    btn.textContent = 'Deploy →';
  }
}

function _courtSeverityCls(sev){
  return ['critical','high'].includes(sev) ? 'complex' : sev === 'medium' ? 'medium' : 'simple';
}

function _renderCourtIssues(elId, issues){
  const el = document.getElementById(elId);
  if(!el) return;
  if(!issues || !issues.length){ el.innerHTML = ''; return; }
  el.innerHTML = issues.map(i =>
    `<span class="pill ${_courtSeverityCls(i.severity)}">${escHtml(i.severity || 'info')}: ${escHtml(i.message || '')}</span>`
  ).join('');
}

function renderDecisionCourt(myId, originalText, parsed, review){
  const btn = document.getElementById('deploy-btn');
  const inp = document.getElementById('deploy-input');
  const panel = document.getElementById('court-panel');
  const verdict = document.getElementById('court-verdict');
  const actions = document.getElementById('court-actions');
  panel.style.display = 'block';
  verdict.style.display = 'none';
  actions.innerHTML = '';

  const agents = review && review.agents;
  const security = agents && agents.security;
  const cost = agents && agents.cost;
  const perf = agents && agents.perf;

  const finish = () => {
    if(myId !== courtRequestId) return;
    btn.disabled = false;
    inp.disabled = false;
    btn.textContent = 'Deploy →';
  };

  if(!review || !security || !cost || !perf){
    // 代理資料不完整,優雅降級:不演動畫,直接顯示最終判決
    ['security','cost','perf'].forEach(a => {
      const card = document.getElementById('court-' + a);
      if(card) card.className = 'court-agent-card done';
    });
    showCourtVerdict(myId, originalText, parsed, review);
    finish();
    return;
  }

  const cards = [
    {key: 'security', data: security, render: renderSecurityCard},
    {key: 'cost', data: cost, render: renderCostCard},
    {key: 'perf', data: perf, render: renderPerfCard},
  ];
  cards.forEach(c => {
    const card = document.getElementById('court-' + c.key);
    if(card) card.className = 'court-agent-card thinking';
    document.getElementById(`court-${c.key}-issues`).innerHTML = '';
    document.getElementById(`court-${c.key}-score`).textContent = '';
    document.getElementById(`court-${c.key}-summary`).innerHTML = '<span class="typing"><span></span><span></span><span></span></span>';
  });

  cards.forEach((c, idx) => {
    setTimeout(() => {
      if(myId !== courtRequestId) return;
      const card = document.getElementById('court-' + c.key);
      if(card) card.className = 'court-agent-card done';
      c.render(c.data);
    }, idx * 500);
  });

  setTimeout(() => {
    if(myId !== courtRequestId) return;
    showCourtVerdict(myId, originalText, parsed, review);
    finish();
  }, cards.length * 500);
}

function renderSecurityCard(security){
  document.getElementById('court-security-score').textContent =
    typeof security.score === 'number' ? `${security.score}/100` : '';
  _renderCourtIssues('court-security-issues', security.issues);
  document.getElementById('court-security-summary').textContent = security.summary || '';
}

function renderCostCard(cost){
  const est = cost.cost_estimate && cost.cost_estimate.estimated_usd;
  document.getElementById('court-cost-score').textContent =
    (est !== undefined && est !== null) ? `$${est}/mo` : '不明';
  _renderCourtIssues('court-cost-issues', cost.issues);
  document.getElementById('court-cost-summary').textContent = cost.summary || '';
}

function renderPerfCard(perf){
  document.getElementById('court-perf-score').textContent = perf.hpa_yaml ? 'HPA suggested' : '';
  _renderCourtIssues('court-perf-issues', perf.issues);
  document.getElementById('court-perf-summary').textContent = perf.summary || '';
}

function showCourtVerdict(myId, originalText, parsed, review){
  const verdict = document.getElementById('court-verdict');
  const actions = document.getElementById('court-actions');
  const decision = (review && review.decision) || 'block';
  verdict.style.display = 'block';
  if(decision === 'approve'){
    verdict.className = 'result-box court-verdict success';
    verdict.textContent = '✓ ' + (review.reason || 'All agent checks passed');
    actions.innerHTML = `<button class="deploy-confirm-btn primary" id="court-deploy-btn">Deploy</button>`;
    document.getElementById('court-deploy-btn').onclick = () => courtProceedDeploy(myId, originalText, parsed);
  } else if(decision === 'warn'){
    verdict.className = 'result-box court-verdict warn';
    verdict.textContent = '⚠ ' + (review.reason || 'Warnings found') +
      (review.warnings && review.warnings.length ? '\n' + review.warnings.map(w => '· ' + w).join('\n') : '');
    actions.innerHTML = `<button class="deploy-confirm-btn" id="court-cancel-btn">Cancel</button>
      <button class="deploy-confirm-btn primary" id="court-deploy-btn">Deploy anyway</button>`;
    document.getElementById('court-deploy-btn').onclick = () => courtProceedDeploy(myId, originalText, parsed);
    document.getElementById('court-cancel-btn').onclick = () => closeCourtPanel(myId);
  } else {
    verdict.className = 'result-box court-verdict error';
    verdict.textContent = '✗ ' + (review.reason || 'Blocked') +
      (review.blockers && review.blockers.length ? '\n' + review.blockers.map(b => '· ' + b).join('\n') : '');
    actions.innerHTML = `<button class="deploy-confirm-btn" id="court-close-btn">Close</button>`;
    document.getElementById('court-close-btn').onclick = () => closeCourtPanel(myId);
  }
}

function closeCourtPanel(myId){
  if(myId !== courtRequestId) return;
  document.getElementById('court-panel').style.display = 'none';
}

async function courtProceedDeploy(myId, originalText, parsed){
  if(myId !== courtRequestId) return;
  const actions = document.getElementById('court-actions');
  const verdict = document.getElementById('court-verdict');
  actions.querySelectorAll('button').forEach(b => b.disabled = true);
  try {
    const r = await fetch('/api/deploy', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({input: originalText, parsed})
    });
    if(myId !== courtRequestId) return;
    const d = await r.json();
    if(myId !== courtRequestId) return;
    if(d.error || d.rejected){
      verdict.className = 'result-box court-verdict error';
      verdict.textContent = '✗ ' + (d.error || d.reason || 'Deployment blocked.');
      actions.querySelectorAll('button').forEach(b => b.disabled = false);
      return;
    }
    const p = d.parsed || parsed;
    if(d.k8s_deploy && d.k8s_deploy.ok === false){
      verdict.className = 'result-box court-verdict warn';
      verdict.textContent = `⚠ GitOps 已提交，但 K8s 實際部署失敗：${d.k8s_deploy.message}`;
      actions.innerHTML = `<button class="deploy-confirm-btn" id="court-close-btn">Close</button>`;
      document.getElementById('court-close-btn').onclick = () => closeCourtPanel(myId);
      return;
    }
    verdict.className = 'result-box court-verdict success';
    verdict.textContent = `✓ App: ${p.app_name}  ·  Image: ${p.image}  ·  Pods: ${p.pods}${p.port ? '  ·  Port: ' + p.port : ''}`;
    actions.innerHTML = `<button class="deploy-confirm-btn" id="court-close-btn">Close</button>`;
    document.getElementById('court-close-btn').onclick = () => closeCourtPanel(myId);
    loadStats();
    if(document.getElementById('page-pods')?.classList.contains('active')) loadPods();
    if(document.getElementById('page-deployments')?.classList.contains('active')) loadDeployments();
  } catch(e){
    if(myId !== courtRequestId) return;
    verdict.className = 'result-box court-verdict error';
    verdict.textContent = '✗ Network error: ' + e;
    actions.querySelectorAll('button').forEach(b => b.disabled = false);
  }
}

// ── Pods ──
async function loadPods(){
  const tbody = document.getElementById('pods-tbody');
  if(!tbody) return;
  tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;padding:32px;color:var(--text3)">Loading...</td></tr>';
  try {
    const r = await fetch('/api/pods');
    const d = await r.json();
    if(!d.pods.length){
      tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;padding:32px;color:var(--text3)">No pods found</td></tr>';
      return;
    }
    tbody.innerHTML = d.pods.map(p => `
      <tr>
        <td class="mono">${p.name}</td>
        <td>${p.app || '—'}</td>
        <td><span class="badge ${p.phase.toLowerCase()}">${p.phase}</span></td>
        <td class="mono">${p.ip || '—'}</td>
        <td class="mono">${p.node || '—'}</td>
        <td>${p.restarts}</td>
        <td class="mono">${p.age}</td>
        <td>
          <div class="action-btns">
            <button class="btn-sm" onclick='showPodDetail(${JSON.stringify(JSON.stringify(p))})'>Details</button>
          </div>
        </td>
      </tr>
    `).join('');
  } catch(e){
    tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;color:var(--red)">Failed to load pods</td></tr>';
  }
}

// ── Pod Detail Modal ──
function showPodDetail(jsonStr){
  const p = JSON.parse(jsonStr);
  document.getElementById('modal-pod-name').textContent = p.name;
  const body = document.getElementById('modal-pod-body');

  const statusColor = p.phase === 'Running' ? 'var(--green)' : p.phase === 'Pending' ? 'var(--yellow)' : 'var(--red)';

  let containersHtml = '';
  if(p.containers && p.containers.length){
    p.containers.forEach(c => {
      const ports = c.ports.length ? c.ports.join(', ') : '—';
      const req = Object.entries(c.resources.requests || {}).map(([k,v]) => `${k}: ${v}`).join(', ') || '—';
      const lim = Object.entries(c.resources.limits || {}).map(([k,v]) => `${k}: ${v}`).join(', ') || '—';
      containersHtml += `
        <div style="background:var(--bg);border-radius:8px;padding:12px 14px;margin-bottom:10px">
          <div style="font-weight:600;font-size:13px;margin-bottom:8px">${c.name}</div>
          <div class="detail-row"><span class="detail-key">Image</span><span class="detail-val">${c.image}</span></div>
          <div class="detail-row"><span class="detail-key">Ports</span><span class="detail-val">${ports}</span></div>
          <div class="detail-row"><span class="detail-key">Requests</span><span class="detail-val">${req}</span></div>
          <div class="detail-row"><span class="detail-key">Limits</span><span class="detail-val">${lim}</span></div>
        </div>`;
    });
  } else {
    containersHtml = '<p style="color:var(--text3);font-size:13px">No container info</p>';
  }

  let condHtml = '';
  if(p.conditions && p.conditions.length){
    condHtml = p.conditions.map(c =>
      `<span class="cond-badge ${c.status==='True'?'cond-true':'cond-false'}" style="margin:2px">${c.type}: ${c.status}</span>`
    ).join('');
  } else {
    condHtml = '<span style="color:var(--text3);font-size:13px">—</span>';
  }

  body.innerHTML = `
    <div class="detail-section">
      <div class="detail-section-title">General</div>
      <div class="detail-row"><span class="detail-key">Name</span><span class="detail-val">${p.name}</span></div>
      <div class="detail-row"><span class="detail-key">App</span><span class="detail-val">${p.app || '—'}</span></div>
      <div class="detail-row"><span class="detail-key">Status</span><span class="detail-val" style="color:${statusColor};font-weight:600">${p.phase}</span></div>
      <div class="detail-row"><span class="detail-key">Pod IP</span><span class="detail-val">${p.ip || '—'}</span></div>
      <div class="detail-row"><span class="detail-key">Node</span><span class="detail-val">${p.node || '—'}</span></div>
      <div class="detail-row"><span class="detail-key">Restarts</span><span class="detail-val">${p.restarts}</span></div>
      <div class="detail-row"><span class="detail-key">Created</span><span class="detail-val">${p.age}</span></div>
    </div>
    <div class="detail-section">
      <div class="detail-section-title">Containers</div>
      ${containersHtml}
    </div>
    <div class="detail-section">
      <div class="detail-section-title">Conditions</div>
      <div style="display:flex;flex-wrap:wrap;gap:6px">${condHtml}</div>
    </div>
  `;
  document.getElementById('pod-modal').classList.add('open');
}
function closeModal(){ document.getElementById('pod-modal').classList.remove('open'); }
document.getElementById('pod-modal')?.addEventListener('click', function(e){ if(e.target===this) closeModal(); });

// ── Deployments ──
async function loadDeployments(){
  const tbody = document.getElementById('deps-tbody');
  if(!tbody) return;
  tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;padding:32px;color:var(--text3)">Loading...</td></tr>';
  try {
    const r = await fetch('/api/deployments');
    const d = await r.json();
    if(!d.deployments.length){
      tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;padding:32px;color:var(--text3)">No deployments found</td></tr>';
      return;
    }
    tbody.innerHTML = d.deployments.map(dep => `
      <tr>
        <td style="font-weight:500">${dep.name}</td>
        <td class="mono">${dep.image}</td>
        <td>${dep.replicas}</td>
        <td>
          <span class="badge ${dep.ready >= dep.replicas ? 'running' : 'pending'}">
            ${dep.ready}/${dep.replicas}
          </span>
        </td>
        <td class="mono">${dep.age}</td>
        <td class="mono">${dep.updated || dep.age}</td>
        <td>
          <div class="action-btns">
            <button class="btn-sm btn-danger" onclick="deleteDeployment('${dep.name}')">Delete</button>
          </div>
        </td>
      </tr>
    `).join('');
  } catch(e){
    tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;color:var(--red)">Failed to load</td></tr>';
  }
}

async function deleteDeployment(name){
  if(!confirm(`Delete deployment "${name}"?`)) return;
  try {
    const r = await fetch('/api/delete', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({name})
    });
    const d = await r.json();
    if(d.success) loadDeployments();
    else alert('Error: ' + d.error);
  } catch(e){ alert('Network error'); }
}

// ── Chat ──
let chatHistory = [];


async function loadGitops(){
  document.getElementById('gitops-list').innerHTML='<div style="color:var(--text3);font-size:13px">Loading...</div>';
  try{
    const r=await fetch('/api/gitops'); const d=await r.json();
    const commits=d.commits||[];
    if(!commits.length){document.getElementById('gitops-list').innerHTML='<div style="color:var(--text3);font-size:13px">No commits yet.</div>';return;}
    document.getElementById('gitops-list').innerHTML=commits.map(cm=>`
      <div style="border:1px solid var(--border);border-radius:8px;padding:12px;margin-bottom:8px">
        <div style="display:flex;justify-content:space-between;align-items:center">
          <div>
            <span style="font-family:monospace;font-size:11px;background:var(--bg);padding:2px 6px;border-radius:4px;color:var(--green)">${cm.hash}</span>
            <span style="font-size:13px;margin-left:8px;font-weight:500">${cm.app||'--'}</span>
          </div>
          <div style="display:flex;align-items:center;gap:8px">
            <span style="font-size:11px;color:var(--text3)">${cm.time||''}</span>
            <button onclick="doRollback('${cm.app||''}')" style="font-size:11px;padding:3px 10px;border-radius:4px;border:1px solid var(--border);background:var(--surface);cursor:pointer">Rollback</button>
          </div>
        </div>
        <div style="font-size:12px;color:var(--text2);margin-top:4px">${cm.message||''}</div>
      </div>`).join('');
  }catch(e){document.getElementById('gitops-list').innerHTML='<div style="color:var(--text3)">Error: '+e+'</div>';}
}

async function doRollback(app){
  if(!app){alert('No app name');return;}
  if(!confirm('Rollback '+app+'?'))return;
  const r=await fetch('/api/rollback',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({app_name:app})});
  const d=await r.json();
  alert(d.message||d.error||'Done');
  loadGitops();
}

async function loadHealer(){
  document.getElementById('healer-list').innerHTML='<div style="color:var(--text3);font-size:13px">Scanning...</div>';
  document.getElementById('healer-time').textContent=new Date().toLocaleTimeString();
  try{
    const r=await fetch('/api/healer/scan'); const d=await r.json();
    const issues=d.issues||[];
    document.getElementById('healer-count').textContent=issues.length;
    if(!issues.length){document.getElementById('healer-list').innerHTML='<div style="color:var(--green);font-size:13px">All pods healthy</div>';return;}
    document.getElementById('healer-list').innerHTML=issues.map(i=>`
      <div style="border:1px solid #FCA5A5;border-radius:8px;padding:12px;margin-bottom:8px;background:#FFF5F5">
        <div style="display:flex;justify-content:space-between;align-items:center">
          <div><span style="font-weight:600;font-size:13px">${i.pod}</span>
          <span style="margin-left:8px;font-size:11px;background:#FEE2E2;color:#DC2626;padding:2px 8px;border-radius:10px">${i.status}</span></div>
          <button onclick="fixPod('${i.pod}')" style="font-size:11px;padding:3px 10px;border-radius:4px;border:none;background:#DC2626;color:#fff;cursor:pointer">Fix</button>
        </div>
        <div style="font-size:12px;color:#6B7280;margin-top:4px">${i.action||''}</div>
      </div>`).join('');
  }catch(e){document.getElementById('healer-list').innerHTML='<div style="color:var(--text3)">Error: '+e+'</div>';}
}

async function fixPod(pod){
  const r=await fetch('/api/healer/fix',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({pod_name:pod})});
  const d=await r.json();
  alert(d.message||d.error||'Done');
  loadHealer();
}

async function healerAutoFix(){
  if(!confirm('Auto fix all issues?'))return;
  const r=await fetch('/api/healer/auto_fix',{method:'POST'});
  const d=await r.json();
  alert('Fixed: '+d.fixed+', Failed: '+d.failed);
  loadHealer();
}

async function loadMetrics(){
  document.getElementById('prom-status').textContent='...';
  try{
    const r=await fetch('/api/metrics'); const d=await r.json();
    if(!d.connected){
      document.getElementById('prom-status').textContent='Offline';
      document.getElementById('prom-pods').textContent='--';
      document.getElementById('prom-url').textContent='Not connected';
      document.getElementById('metrics-rows').innerHTML='<div style="color:var(--text3)">Run: kubectl port-forward -n monitoring svc/prometheus 9090:9090</div>';
      return;
    }
    const m=d.metrics||{};
    document.getElementById('prom-status').textContent='Online';
    document.getElementById('prom-pods').textContent=m.running_pods!=null?m.running_pods:'N/A';
    document.getElementById('prom-url').textContent=d.url||'localhost:9090';
    document.getElementById('metrics-rows').innerHTML=[
      ['Prometheus','UP'],
      ['Pod Count',m.pod_count!=null?m.pod_count:'N/A'],
      ['Running Pods',m.running_pods!=null?m.running_pods:'N/A'],
      ['Kube Pods',m.kube_pods!=null?m.kube_pods:'N/A'],
    ].map(([k,v])=>'<div style="display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid var(--border)"><span style="color:var(--text2);font-size:12px">'+k+'</span><span style="font-size:12px;font-weight:500">'+v+'</span></div>').join('');
  }catch(e){document.getElementById('prom-status').textContent='ERR';}
}

async function loadDatasetStats(){
  document.getElementById('ds-total').textContent = '...';
  try {
    const r = await fetch('/api/dataset/stats');
    const d = await r.json();
    document.getElementById('ds-total').textContent = d.total_records.toLocaleString();
    document.getElementById('ds-output-pct').textContent = d.output_pct + '%';
    document.getElementById('ds-k8s-ratio').textContent = d.k8s_count + ' / ' + d.non_k8s_count;
    let fhtml = '';
    for(const [name, cnt] of Object.entries(d.files)){
      fhtml += '<div style="display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid var(--border)"><span>'+name+'</span><span style="color:var(--green);font-weight:600">'+cnt.toLocaleString()+'</span></div>';
    }
    document.getElementById('ds-files').innerHTML = fhtml || 'No files found';
    let chtml = '';
    for(const [cat, cnt] of d.top_categories){
      chtml += '<div style="display:flex;justify-content:space-between;padding:3px 0;border-bottom:1px solid var(--border)"><span style="font-size:11px">'+cat+'</span><span style="color:var(--text);font-weight:600">'+cnt+'</span></div>';
    }
    document.getElementById('ds-categories').innerHTML = chtml || 'No data';
  } catch(e){ document.getElementById('ds-total').textContent = 'ERR'; }
}

// ── Knowledge Base (RAG) ──
function kbConfColor(conf){
  return conf === 'high' ? 'var(--green)' : (conf === 'medium' ? '#D97706' : 'var(--text3)');
}
function kbConfBadge(conf, score){
  return '<span style="font-size:11px;font-weight:600;color:'+kbConfColor(conf)+'">'+conf.toUpperCase()+' · '+(score*100).toFixed(0)+'%</span>';
}

async function loadKB(){
  document.getElementById('kb-doc-list').textContent = 'Loading...';
  try{
    const r = await fetch('/api/rag/status'); const d = await r.json();
    document.getElementById('kb-method').textContent = (d.active_method||'--').toUpperCase();
    document.getElementById('kb-method-sub').textContent = (d.chroma && d.chroma.embedding_model) ? d.chroma.embedding_model + ' (' + d.chroma.device + ')' : 'legacy TF-IDF';
    document.getElementById('kb-chunks').textContent = d.chunk_count!=null ? d.chunk_count : '--';
    document.getElementById('kb-built-at').textContent = d.built_at ? new Date(d.built_at*1000).toLocaleString() : 'never built';
    document.getElementById('kb-built-sub').textContent = d.elapsed_sec!=null ? ('built in ' + d.elapsed_sec + 's') : 'build time';
    renderKBDocs(d.documents || []);
  }catch(e){
    document.getElementById('kb-doc-list').textContent = 'Failed to load: ' + e;
  }
}

function renderKBDocs(docs){
  const el = document.getElementById('kb-doc-list');
  if(!docs.length){ el.innerHTML = 'No documents yet — add one on the right.'; return; }
  el.innerHTML = docs.map(doc => `
    <div style="display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:1px solid var(--border)">
      <div>
        <div style="font-weight:600">${escHtml(doc.filename)}</div>
        <div style="font-size:11px;color:var(--text3)">${(doc.size_bytes/1024).toFixed(1)}KB · ${doc.chunk_count} chunks · ${new Date(doc.modified_at*1000).toLocaleDateString()}</div>
      </div>
      <button onclick="deleteKBDoc('${escHtml(doc.filename)}')" style="background:none;border:1px solid var(--border);color:var(--red);border-radius:6px;padding:5px 10px;font-size:12px;cursor:pointer">Delete</button>
    </div>`).join('');
}

async function rebuildKB(){
  const btn = document.getElementById('kb-rebuild-btn');
  const status = document.getElementById('kb-rebuild-status');
  btn.disabled = true;
  status.textContent = 'Rebuilding index (embedding + writing to ChromaDB)...';
  try{
    const r = await fetch('/api/rag/rebuild', {method:'POST'});
    const d = await r.json();
    if(d.error){ status.textContent = 'Error: ' + d.error; }
    else { status.textContent = `Rebuilt: ${d.chunk_count} chunks from ${d.doc_count} documents in ${d.elapsed_sec}s (${d.active_method}).`; }
    await loadKB();
  }catch(e){
    status.textContent = 'Network error: ' + e;
  }finally{
    btn.disabled = false;
  }
}

async function addKBDoc(){
  const filename = document.getElementById('kb-new-filename').value.trim();
  const content = document.getElementById('kb-new-content').value;
  const status = document.getElementById('kb-add-status');
  if(!filename || !content.trim()){ status.textContent = 'Filename and content are both required.'; return; }
  status.textContent = 'Uploading...';
  try{
    const r = await fetch('/api/rag/docs', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({filename, content})});
    const d = await r.json();
    if(d.error){ status.textContent = 'Error: ' + d.error; return; }
    document.getElementById('kb-new-filename').value = '';
    document.getElementById('kb-new-content').value = '';
    status.textContent = 'Uploaded. Rebuilding index...';
    await rebuildKB();
    status.textContent = 'Document added and index rebuilt.';
  }catch(e){
    status.textContent = 'Network error: ' + e;
  }
}

async function deleteKBDoc(filename){
  if(!confirm('Delete "' + filename + '" and rebuild the index?')) return;
  const status = document.getElementById('kb-rebuild-status');
  try{
    const r = await fetch('/api/rag/docs/' + encodeURIComponent(filename), {method:'DELETE'});
    const d = await r.json();
    if(d.error){ status.textContent = 'Error: ' + d.error; return; }
    status.textContent = 'Deleted. Rebuilding index...';
    await rebuildKB();
  }catch(e){
    status.textContent = 'Network error: ' + e;
  }
}

async function runKBQuery(){
  const query = document.getElementById('kb-query-input').value.trim();
  const el = document.getElementById('kb-query-results');
  if(!query){ return; }
  el.textContent = 'Searching...';
  try{
    const r = await fetch('/api/rag/query', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({query, top_k:5})});
    const d = await r.json();
    if(d.error){ el.textContent = 'Error: ' + d.error; return; }
    if(!d.results.length){ el.innerHTML = '<span style="color:var(--text3)">No matching documents found.</span>'; return; }
    el.innerHTML = `<div style="font-size:11px;color:var(--text3);margin-bottom:8px">method: ${d.method}</div>` + d.results.map(r => `
      <div style="padding:8px 0;border-bottom:1px solid var(--border)">
        <div style="display:flex;justify-content:space-between"><strong>${escHtml(r.source)}</strong>${kbConfBadge(r.confidence, r.score)}</div>
        <div style="font-size:12px;color:var(--text2);margin-top:4px;white-space:pre-wrap">${escHtml(r.text.slice(0,220))}${r.text.length>220?'...':''}</div>
      </div>`).join('');
  }catch(e){
    el.textContent = 'Network error: ' + e;
  }
}

async function runKBEval(){
  const btn = document.getElementById('kb-eval-btn');
  const el = document.getElementById('kb-eval-results');
  btn.disabled = true;
  el.textContent = 'Running evaluation on curated test set...';
  try{
    const r = await fetch('/api/rag/eval', {method:'POST'});
    const d = await r.json();
    if(d.error){ el.textContent = 'Error: ' + d.error; return; }
    const rows = ['chroma','tfidf'].map(key => {
      const m = d.results[key];
      if(!m) return '';
      if(m.error) return `<div style="padding:6px 0;color:var(--text3)">${key}: ${escHtml(m.error)}</div>`;
      return `<div style="display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid var(--border)">
        <span style="font-weight:600">${escHtml(m.method)}</span>
        <span style="font-size:12px;color:var(--text2)">Recall@${d.top_k}: ${(m.recall_at_k*100).toFixed(0)}% · MRR: ${m.mrr.toFixed(2)} · ${m.avg_latency_ms.toFixed(0)}ms</span>
      </div>`;
    }).join('');
    el.innerHTML = `<div style="font-size:11px;color:var(--text3);margin-bottom:6px">${d.num_queries} test queries</div>` + rows;
  }catch(e){
    el.textContent = 'Network error: ' + e;
  }finally{
    btn.disabled = false;
  }
}

async function runEnrich(flags){
  const log = document.getElementById('ds-log');
  const status = document.getElementById('ds-run-status');
  log.textContent = 'Starting...\n';
  status.textContent = 'Running...';
  try {
    const r = await fetch('/api/dataset/run', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({flags: flags})
    });
    const d = await r.json();
    log.textContent = d.log || d.error || 'Done';
    status.textContent = d.success ? 'Completed' : 'Error';
    if(d.success) loadDatasetStats();
  } catch(e){ log.textContent = 'Connection error: ' + e; status.textContent = 'Failed'; }
}


// ── Multi-chat rooms ────────────────────────────────────────
let chats = [];
let currentChatId = null;

function initChats(){
  try { chats = JSON.parse(localStorage.getItem('k8s_chats')||'[]'); } catch(e){ chats=[]; }
  currentChatId = localStorage.getItem('k8s_current_chat') || null;
  if(!chats.length){
    const id = 'chat_' + Date.now();
    chats.push({id, title:'New Chat', messages:[]});
    currentChatId = id;
    saveChats();
  }
  if(!currentChatId || !chats.find(ch=>ch.id===currentChatId)){
    currentChatId = chats[chats.length-1].id;
  }
  renderChatList();
  renderMessages();
}

function saveChats(){
  localStorage.setItem('k8s_chats', JSON.stringify(chats));
  localStorage.setItem('k8s_current_chat', currentChatId||'');
}

function newChat(){
  if(!chats.length){
    try { chats = JSON.parse(localStorage.getItem('k8s_chats')||'[]'); } catch(e){ chats=[]; }
  }
  const id = 'chat_' + Date.now();
  chats.push({id, title:'New Chat', messages:[]});
  currentChatId = id;
  saveChats();
  showPage('chat');
  renderChatList();
  renderMessages();
  setTimeout(()=>document.getElementById('chat-input')?.focus(), 0);
}

function deleteChat(id, e){
  e.stopPropagation();
  chats = chats.filter(ch=>ch.id!==id);
  if(currentChatId===id) currentChatId = chats.length ? chats[chats.length-1].id : null;
  if(!chats.length){ newChat(); return; }
  saveChats();
  renderChatList();
  renderMessages();
}

function switchChat(id){
  currentChatId = id;
  saveChats();
  showPage('chat');
  renderChatList();
  renderMessages();
}

function currentChat(){
  return chats.find(ch=>ch.id===currentChatId);
}

function renderChatList(){
  const el = document.getElementById('chat-room-list');
  if(!el) return;
  el.innerHTML = chats.slice().reverse().map(ch=>`
    <div class="chat-room-item ${ch.id===currentChatId?'active':''}" onclick="switchChat('${ch.id}')">
      <span class="chat-room-title"><svg viewBox="0 0 16 16" fill="none" width="13" height="13" style="flex-shrink:0"><path d="M2 3a1 1 0 011-1h10a1 1 0 011 1v7a1 1 0 01-1 1H9l-3 2v-2H3a1 1 0 01-1-1V3z" stroke="currentColor" stroke-width="1.5"/></svg><span>${escHtml(ch.title)}</span></span>
      <span class="chat-room-delete" onclick="deleteChat('${ch.id}',event)">&#x2715;</span>
    </div>`).join('');
}

function renderMessages(){
  const msgs = document.getElementById('chat-messages');
  if(!msgs) return;
  const ch = currentChat();
  if(!ch || !ch.messages.length){
    msgs.innerHTML = `<div class="chat-empty">
      <div class="chat-empty-logo">K</div>
      <h1>How can I help with your cluster?</h1>
      <p>Use natural language to deploy services, inspect workloads, troubleshoot failures, or ask Kubernetes questions.</p>
      <div class="prompt-grid">
        <div class="prompt-card" onclick="document.getElementById('chat-input').value='deploy 3 nginx:latest pods for web-frontend';sendChat()">
          <strong>Deploy a service</strong><span>deploy 3 nginx:latest pods for web-frontend</span>
        </div>
        <div class="prompt-card" onclick="document.getElementById('chat-input').value='list pods';sendChat()">
          <strong>Check cluster status</strong><span>list pods and show deployments</span>
        </div>
        <div class="prompt-card" onclick="document.getElementById('chat-input').value='Explain Kubernetes Deployment vs Service';sendChat()">
          <strong>Learn Kubernetes</strong><span>Explain Deployment vs Service</span>
        </div>
        <div class="prompt-card" onclick="document.getElementById('chat-input').value='How do I debug CrashLoopBackOff?';sendChat()">
          <strong>Troubleshoot</strong><span>How do I debug CrashLoopBackOff?</span>
        </div>
      </div>
    </div>`;
    return;
  }
  msgs.innerHTML = ch.messages.map(m=>renderMsgHTML(m.role, m.content, m.sources)).join('');
  msgs.scrollTop = msgs.scrollHeight;
}

function renderSourcesHTML(sources){
  if(!sources || !sources.length) return '';
  const items = sources.map(s => `
    <div style="padding:6px 0;border-bottom:1px solid var(--border)">
      <div style="display:flex;justify-content:space-between;gap:8px;font-size:12px">
        <strong>${escHtml(s.source||'')}</strong>${kbConfBadge(s.confidence||'low', s.score||0)}
      </div>
      <div style="font-size:11.5px;color:var(--text3);margin-top:3px;white-space:pre-wrap">${escHtml(s.text||'')}</div>
    </div>`).join('');
  return `<details style="margin-top:10px;font-size:12px;border-top:1px solid var(--border);padding-top:8px">
    <summary style="cursor:pointer;color:var(--text3)">📎 ${sources.length} 個引用來源（RAG 知識庫）</summary>
    <div style="margin-top:6px">${items}</div>
  </details>`;
}

function renderMsgHTML(role, content, sources){
  if(role==='user'){
    return `<div class="msg user" style="margin-bottom:16px"><div class="msg-avatar">U</div><div class="msg-bubble">${escHtml(content)}</div></div>`;
  }
  return `<div class="msg ai" style="margin-bottom:16px"><div class="msg-avatar">K</div><div class="msg-bubble" style="white-space:pre-wrap">${content}${renderSourcesHTML(sources)}</div></div>`;
}

function escHtml(s){ return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

function appendMsg(role, content, sources){
  const ch = currentChat();
  if(!ch) return;
  ch.messages.push({role, content, sources});
  if(ch.messages.length===1 && role==='user'){
    ch.title = content.slice(0,30) + (content.length>30?'...':'');
  }
  saveChats();
  renderChatList();
  const msgs = document.getElementById('chat-messages');
  msgs.innerHTML += renderMsgHTML(role, content, sources);
  msgs.scrollTop = msgs.scrollHeight;
}

function appendTyping(){
  const msgs = document.getElementById('chat-messages');
  const div = document.createElement('div');
  div.className = 'msg ai'; div.id = 'typing-indicator'; div.style.marginBottom='16px';
  div.innerHTML = '<div class="msg-avatar">K</div><div class="msg-bubble"><span style="opacity:.5">Thinking...</span></div>';
  msgs.appendChild(div);
  msgs.scrollTop = msgs.scrollHeight;
}

function deployConfirmHTML(id, parsed, originalText){
  const output = parsed.output || parsed;
  const app = escHtml(String(output.app_name || parsed.app_name || 'auto-app'));
  const image = escHtml(String(output.image || parsed.image || 'nginx:latest'));
  const pods = escHtml(String(output.pods || parsed.pods || 1));
  const port = escHtml(String(output.port || parsed.port || 80));
  const memory = escHtml(String(output.memory || parsed.memory || ''));
  return `<div class="deploy-confirm" id="${id}" data-original="${escHtml(originalText)}">
    <div class="deploy-confirm-head">
      <div>
        <div class="deploy-confirm-title">Confirm deployment details</div>
        <div class="deploy-confirm-sub">Edit anything that looks wrong, then deploy.</div>
      </div>
      <span class="badge pending">Needs confirmation</span>
    </div>
    <div class="deploy-confirm-grid">
      <div class="deploy-confirm-field"><label>App name</label><input data-field="app_name" value="${app}" placeholder="my-app"></div>
      <div class="deploy-confirm-field"><label>Image</label><input data-field="image" value="${image}" placeholder="nginx:latest"></div>
      <div class="deploy-confirm-field"><label>Pods</label><input data-field="pods" type="number" min="1" max="100" value="${pods}"></div>
      <div class="deploy-confirm-field"><label>Port</label><input data-field="port" type="number" min="1" max="65535" value="${port}"></div>
      <div class="deploy-confirm-field memory"><label>Memory limit</label><input data-field="memory" value="${memory}" placeholder="optional, e.g. 128Mi"></div>
    </div>
    <div class="deploy-confirm-note">This will create or update a Kubernetes Deployment and Service after confirmation. If the app name already exists, Kubernetes updates that existing Deployment.</div>
    <div class="deploy-confirm-error" data-role="error"></div>
    <div class="deploy-confirm-actions">
      <button class="deploy-confirm-btn" onclick="cancelDeployConfirm('${id}')">Cancel</button>
      <button class="deploy-confirm-btn primary" onclick="confirmDeploy('${id}')">Confirm & Deploy</button>
    </div>
  </div>`;
}

function readDeployConfirm(id){
  const root = document.getElementById(id);
  if(!root) return null;
  const get = f => root.querySelector(`[data-field="${f}"]`)?.value.trim() || '';
  const parsed = {
    app_name: get('app_name'),
    image: get('image'),
    pods: parseInt(get('pods'), 10),
    port: parseInt(get('port'), 10)
  };
  const memory = get('memory');
  if(memory) parsed.memory = memory;
  return {root, parsed, input: root.dataset.original || ''};
}

function setDeployConfirmError(root, msg){
  const el = root.querySelector('[data-role="error"]');
  if(!el) return;
  el.style.display = msg ? 'block' : 'none';
  el.textContent = msg || '';
}

function cancelDeployConfirm(id){
  const root = document.getElementById(id);
  if(root){
    root.querySelectorAll('input,button').forEach(el=>el.disabled=true);
    const badge = root.querySelector('.badge');
    if(badge){ badge.textContent='Cancelled'; badge.className='badge failed'; }
  }
}

async function confirmDeploy(id){
  const data = readDeployConfirm(id);
  if(!data) return;
  const {root, parsed, input} = data;
  setDeployConfirmError(root, '');
  if(!parsed.app_name || !parsed.image){ setDeployConfirmError(root, 'App name and image are required.'); return; }
  if(!Number.isInteger(parsed.pods) || parsed.pods < 1 || parsed.pods > 100){ setDeployConfirmError(root, 'Pods must be between 1 and 100.'); return; }
  if(!Number.isInteger(parsed.port) || parsed.port < 1 || parsed.port > 65535){ setDeployConfirmError(root, 'Port must be between 1 and 65535.'); return; }
  if(parsed.memory && !/^\d+(Mi|Gi|Ki|M|G)$/.test(parsed.memory)){ setDeployConfirmError(root, 'Memory must look like 128Mi or 1Gi.'); return; }

  root.querySelectorAll('input,button').forEach(el=>el.disabled=true);
  const badge = root.querySelector('.badge');
  if(badge){ badge.textContent='Deploying'; badge.className='badge pending'; }
  try{
    const r = await fetch('/api/deploy',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({input, parsed})});
    const d = await r.json();
    if(d.error || d.rejected){
      setDeployConfirmError(root, d.error || d.reason || 'Deployment blocked.');
      root.querySelectorAll('input,button').forEach(el=>el.disabled=false);
      if(badge){ badge.textContent='Needs changes'; badge.className='badge failed'; }
      return;
    }
    if(d.k8s_deploy && d.k8s_deploy.ok === false){
      setDeployConfirmError(root, `GitOps 已提交，但 K8s 實際部署失敗：${d.k8s_deploy.message}`);
      root.querySelectorAll('input,button').forEach(el=>el.disabled=false);
      if(badge){ badge.textContent='K8s failed'; badge.className='badge failed'; }
      return;
    }
    const p = d.parsed || parsed;
    const appName = p.app_name || parsed.app_name;
    const pods = p.pods || parsed.pods;
    const image = p.image || parsed.image;
    const port = p.port || parsed.port;
    const successText = `部署成功：${appName} 已送出，Pods: ${pods}，Image: ${image}${port ? '，Port: ' + port : ''}。你可以到 Pods / Deployments 頁查看狀態。`;
    const ch = currentChat();
    if(ch){
      ch.messages = ch.messages.filter(m => !(m.role === 'assistant' && String(m.content || '').includes(`id="${id}"`)));
      saveChats();
    }
    root.closest('.msg')?.remove();
    appendMsg('assistant', successText);
    loadStats();
    if(document.getElementById('page-pods')?.classList.contains('active')) loadPods();
    if(document.getElementById('page-deployments')?.classList.contains('active')) loadDeployments();
  }catch(e){
    setDeployConfirmError(root, 'Network error: ' + e);
    root.querySelectorAll('input,button').forEach(el=>el.disabled=false);
    if(badge){ badge.textContent='Failed'; badge.className='badge failed'; }
  }
}

function removeTyping(){ const el=document.getElementById('typing-indicator'); if(el) el.remove(); }

async function sendChat(){
  const inp = document.getElementById('chat-input');
  const text = inp.value.trim();
  if(!text) return;
  inp.value = '';
  inp.style.height = '';
  if(!currentChatId) newChat();
  appendMsg('user', text);
  appendTyping();

  let replied = false;

  if(/^(list|show|\u67e5\u770b|\u986f\u793a)\s*(all\s*)?(pods?|pod|\u5bb9\u5668)/i.test(text)){
    replied = true;
    try{
      const r = await fetch('/api/pods'); const d = await r.json();
      const pods = d.pods||[];
      let reply = pods.length ? pods.map(p=>`- ${p.name} [${p.phase}] - ${(p.containers&&p.containers[0]&&p.containers[0].image)||''}`).join('\n') : 'No pods running.';
      removeTyping(); appendMsg('assistant', 'Running Pods:\n'+reply);
    }catch(e){ removeTyping(); appendMsg('assistant','Error: '+e); }
  }
  else if(/^(list|show|\u67e5\u770b|\u986f\u793a)\s*(all\s*)?(deploy|deployment|\u90e8\u7f72)/i.test(text)){
    replied = true;
    try{
      const r = await fetch('/api/deployments'); const d = await r.json();
      const deps = d.deployments||[];
      let reply = deps.length ? deps.map(d=>`- ${d.name} - ${d.ready}/${d.replicas} ready`).join('\n') : 'No deployments.';
      removeTyping(); appendMsg('assistant', 'Deployments:\n'+reply);
    }catch(e){ removeTyping(); appendMsg('assistant','Error: '+e); }
  }
  else if(/^(delete|remove|\u522a\u9664|del)\s+(\S+)/i.test(text)){
    replied = true;
    const name = text.match(/^(?:delete|remove|\u522a\u9664|del)\s+(\S+)/i)[1];
    try{
      const r = await fetch('/api/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name})});
      const d = await r.json();
      removeTyping(); appendMsg('assistant', d.success ? 'Deleted: '+name : 'Error: '+(d.error||d.message));
    }catch(e){ removeTyping(); appendMsg('assistant','Error: '+e); }
  }
  else if(/scale\s+(\S+)\s+to\s+(\d+)/i.test(text)){
    replied = true;
    const m = text.match(/scale\s+(\S+)\s+to\s+(\d+)/i);
    const app = m[1], n = parseInt(m[2]);
    try{
      const r = await fetch('/api/scale',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:app,replicas:n})});
      const d = await r.json();
      removeTyping(); appendMsg('assistant', d.success ? 'Scaled '+app+' to '+n+' replicas' : 'Error: '+(d.error||d.message));
    }catch(e){ removeTyping(); appendMsg('assistant','Error: '+e); }
  }
  else if(/update\s+(\S+)\s+to\s+(\S+)/i.test(text)){
    replied = true;
    const m = text.match(/update\s+(\S+)\s+to\s+(\S+)/i);
    const app = m[1], image = m[2];
    try{
      const r = await fetch('/api/update',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:app,image})});
      const d = await r.json();
      removeTyping(); appendMsg('assistant', d.success ? 'Updated '+app+' to '+image : 'Error: '+(d.error||d.message));
    }catch(e){ removeTyping(); appendMsg('assistant','Error: '+e); }
  }
  else if(/rollback\s+(\S+)/i.test(text)){
    replied = true;
    const app = text.match(/rollback\s+(\S+)/i)[1];
    try{
      const r = await fetch('/api/rollback',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({app_name:app})});
      const d = await r.json();
      removeTyping(); appendMsg('assistant', d.success!==false ? (d.message||'Rolled back '+app) : 'Error: '+(d.error||d.message));
    }catch(e){ removeTyping(); appendMsg('assistant','Error: '+e); }
  }
  else if(/^(deploy|start|launch|run|spin\s+up|\u90e8\u7f72|\u4f48\u7f72|\u90e8\u5c6c|\u8d77\s)/i.test(text) || ((/\u5e6b\u6211|\u8acb/i.test(text)) && /(deploy|\u90e8\u7f72|\u4f48\u7f72|\u90e8\u5c6c|pods?|pod|\u8d77)/i.test(text) && (/\d+\s*(pods?|replicas?|\u500b|\u526f\u672c)/i.test(text) || /(nginx|redis|postgres|node|python|golang|image|\u6620\u50cf|\u93e1\u50cf)/i.test(text)))){
    replied = true;
    try{
      const r = await fetch('/api/deploy/parse',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({input:text})});
      const d = await r.json();
      removeTyping();
      if(d.error || d.rejected){
        appendMsg('assistant', d.error || d.reason || 'Deployment request was blocked.');
      } else {
        const confirmId = 'deploy_confirm_' + Date.now();
        appendMsg('assistant', deployConfirmHTML(confirmId, d.parsed || {}, text));
      }
    }catch(e){
      removeTyping();
      appendMsg('assistant','Connection error while parsing deployment: '+e);
    }
  }

  if(!replied){
    try{
      const hist = (currentChat()?.messages||[]).slice(-10).map(m=>({role:m.role==='assistant'?'assistant':'user',content:m.content}));
      const r = await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:text, history:hist})});
      const d = await r.json();
      removeTyping();
      appendMsg('assistant', d.reply||d.error||'No response', d.sources);
    }catch(e){ removeTyping(); appendMsg('assistant','Connection error: '+e); }
  }
}


window.addEventListener('DOMContentLoaded', function(){
  if(loggedIn){
    initChats();
    showPage('chat');
  }
});
</script>
</body>
</html>
"""

# ── Routes ──────────────────────────────────────────────────
@app.route("/")
def index():
    if "username" not in session:
        return render_template_string(HTML, logged_in=False, page='login', error=None, k8s=K8S_ENABLED, username='')
    return render_template_string(HTML, logged_in=True, page='app', error=None, k8s=K8S_ENABLED, username=session["username"])

@app.route("/auth/register", methods=["GET","POST"])
def register():
    if request.method == "GET":
        return render_template_string(HTML, logged_in=False, page='register', error=None, k8s=K8S_ENABLED, username='')
    username = request.form.get("username","").strip()
    password = request.form.get("password","")
    confirm  = request.form.get("confirm","")
    if not username or not password:
        return render_template_string(HTML, logged_in=False, page='register', error="Please fill in all fields", k8s=K8S_ENABLED, username='')
    if password != confirm:
        return render_template_string(HTML, logged_in=False, page='register', error="Passwords do not match", k8s=K8S_ENABLED, username='')
    if username in USERS:
        return render_template_string(HTML, logged_in=False, page='register', error="Username already taken", k8s=K8S_ENABLED, username='')
    if len(password) < 6:
        return render_template_string(HTML, logged_in=False, page='register', error="Password must be at least 6 characters", k8s=K8S_ENABLED, username='')
    USERS[username] = {"password_hash": hash_password(password), "created_at": datetime.now().isoformat()}
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "users.json"), "w", encoding="utf-8") as _uf:
        json.dump(USERS, _uf)
    session["username"] = username
    return redirect("/")

@app.route("/auth/login", methods=["POST"])
def login():
    username = request.form.get("username","").strip()
    password = request.form.get("password","")
    stored_user = USERS.get(username)
    stored_hash = stored_user.get("password_hash", "") if isinstance(stored_user, dict) else stored_user
    if verify_password(password, stored_hash):
        if not isinstance(stored_user, dict):
            USERS[username] = {"password_hash": hash_password(password), "created_at": datetime.now().isoformat()}
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "users.json"), "w", encoding="utf-8") as _uf:
                json.dump(USERS, _uf)
        session["username"] = username
        return redirect("/")
    return render_template_string(HTML, logged_in=False, page='login', error="Invalid username or password", k8s=K8S_ENABLED, username='')

@app.route("/auth/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect("/")


def _rag_fallback_context(message: str, history: list = None) -> tuple:
    """回傳 (顯示用文字, 引用來源 list)，供本地模型未啟動時的快速回覆使用。"""
    try:
        from rag.retriever import retrieve_with_history
        docs = retrieve_with_history(message, history=history, top_k=2, min_score=0.02)
    except Exception:
        docs = []
    if not docs:
        return "", []
    parts = []
    for doc in docs:
        text = doc.get("text", "").strip()
        if text:
            parts.append(f"來源 {doc.get('source','RAG')}：\n{text[:900]}")
    return "\n\n".join(parts), docs


def _looks_like_system_help(message: str) -> bool:
    low = (message or '').lower()
    product_terms = (
        'zerotouch', 'this system', 'this app', 'use this', 'how to use', 'how to deploy',
        'healer', 'gitops', 'dataset', 'metrics', 'deploy console',
        '這套', '系統', '怎麼用', '如何使用', '教學', '功能', '使用方式',
        '怎麼部署', '如何部署', '怎麼部屬', '如何部屬', '怎麼佈署', '如何佈署'
    )
    return any(t in low for t in product_terms)


def _system_help_reply(message: str) -> str:
    low = (message or '').lower()

    if 'healer' in low or '自動修復' in low or '修復' in low:
        return (
            "Healer 是用來掃描並修復異常 Pod 的工具。\n\n"
            "使用方式：\n"
            "1. 左側點 `Healer`。\n"
            "2. 按 `Scan Now` 掃描異常 Pod。\n"
            "3. 如果看到 CrashLoopBackOff、OOMKilled、ImagePullBackOff、ErrImagePull 或 Error，先看原因。\n"
            "4. 只想修單一 Pod 就按該 Pod 的修復；想全部處理就按 `Auto Fix All`。\n"
            "5. 回到 `Pods` 或 `Deployments` 確認新的 Pod 是否變成 Running。\n\n"
            "注意：Healer 的主要動作是刪掉壞掉的 Pod，讓 Deployment/ReplicaSet 重建。"
            "如果 image tag、Secret、環境變數或程式本身錯了，Pod 可能還會再壞，需要修根因。"
        )

    if 'gitops' in low or 'rollback' in low or '回滾' in low or '版本' in low:
        return (
            "GitOps Log 用來看部署歷史與協助 rollback。\n\n"
            "使用方式：\n"
            "1. 左側點 `GitOps Log`。\n"
            "2. 按 `Refresh` 讀取最近部署紀錄。\n"
            "3. 找到你要確認的 app 或 commit。\n"
            "4. 若要回滾，可以在 Chat 輸入 `rollback <app-name>`。\n\n"
            "小技巧：先輸入 `show deployments` 找到正確 app name，再 rollback。"
        )

    if 'dataset' in low or '資料集' in low or 'training' in low or '訓練' in low:
        return (
            "Dataset Manager 是用來檢查與補齊 Kubernetes 訓練資料。\n\n"
            "使用方式：\n"
            "1. 左側點 `Dataset`。\n"
            "2. 看 Total Records、Output Filled、K8s / Non-K8s 比例。\n"
            "3. `Quick Fill (rules only)` 會快速用規則補欄位。\n"
            "4. `Full Enrich (LLaMA output)` 會用模型補答案，速度較慢。\n"
            "5. `Dry Run` 可以先預覽，不直接寫入。"
        )

    if 'metrics' in low or 'prometheus' in low or '監控' in low or '指標' in low:
        return (
            "Metrics 頁面用來看 Prometheus 與叢集基本指標。\n\n"
            "使用方式：\n"
            "1. 左側點 `Metrics`。\n"
            "2. 查看 Prometheus 是否 Online。\n"
            "3. 看 Running Pods 與 endpoint。\n"
            "4. 右側有 PromQL quick reference，可以用來查 Pod 數、Running 狀態與 Deployment replicas。\n\n"
            "如果 Prometheus offline，先確認 Prometheus service 或 port-forward。"
        )

    if 'deploy' in low or '部署' in low or 'pod' in low or 'pods' in low:
        return (
            "你可以用 Chat 或 Deploy Console 部署 Pod。建議格式：\n\n"
            "`deploy <數量> <image> pods for <app-name>, port <port>`\n\n"
            "範例：\n"
            "- `deploy 3 nginx:latest pods for web-frontend, port 80`\n"
            "- `spin up 4 node:20-alpine pods for api-gateway, port 3000`\n"
            "- `幫我部署 5 個 redis pods 給 cache-service port 6379`\n\n"
            "送出後系統會解析 replicas/image/app/port，跑 Guardian、Agent review、dry-run，K8s 連線正常時才建立 Deployment。"
        )

    return (
        "這套 ZeroTouch K8s 主要有幾個區塊：\n\n"
        "- `Chat`：問問題、列 Pods、部署服務、scale/update/rollback。\n"
        "- `Deploy Console`：用自然語言建立 Deployment/Service。\n"
        "- `Pods`：查看 Pod 狀態、IP、node、restart。\n"
        "- `Deployments`：查看 app image、replicas、ready 數與刪除部署。\n"
        "- `Healer`：掃描 CrashLoopBackOff/OOMKilled/ImagePullBackOff 等異常 Pod，並刪除重建。\n"
        "- `GitOps Log`：看部署歷史與 rollback 線索。\n"
        "- `Metrics`：看 Prometheus 與叢集基本指標。\n"
        "- `Dataset`：檢查與補齊訓練資料。\n\n"
        "如果你要部署，直接輸入例如：`deploy 3 nginx:latest pods for web-frontend, port 80`。"
    )


def _fallback_chat_reply(message: str, history: list = None) -> tuple:
    """回傳 (reply, sources)。只有走到 RAG 知識庫的分支才會有非空的 sources。"""
    text = message.strip()
    low = text.lower()
    greetings = ("hi", "hello", "hey", "嗨", "你好", "哈囉", "早安", "午安", "晚安")
    if any(g in low for g in greetings):
        return "嗨，我是 ZeroTouch K8s Assistant。你可以跟我閒聊，也可以問 Kubernetes、查 Pods/Deployments、排查錯誤，或用自然語言部署服務。", []
    if "pod" in low or "pods" in low or "容器" in low:
        pods = k8s_get_pods()
        if pods:
            lines = [f"- {p['name']} [{p['phase']}] app={p.get('app') or '-'} restarts={p.get('restarts', 0)}" for p in pods[:12]]
            return "目前 Pods：\n" + "\n".join(lines), []
    if "deployment" in low or "deployments" in low or "部署" in low:
        deps = k8s_get_deployments()
        if deps:
            lines = [f"- {d['name']} ready={d['ready']}/{d['replicas']} image={d['image']}" for d in deps[:12]]
            return "目前 Deployments：\n" + "\n".join(lines), []
    if "crashloop" in low or "crashloopbackoff" in low:
        return "CrashLoopBackOff 常見排查順序：\n1. `kubectl logs <pod> --previous` 看崩潰前日誌\n2. `kubectl describe pod <pod>` 看 Events、Exit Code、OOMKilled\n3. 檢查 image、command、env、Secret/ConfigMap、port、volume mount\n4. 檢查 liveness/readiness probe 是否太早或路徑錯\n5. 若是 OOMKilled，調高 memory limit 或降低啟動負載。", []
    if "service" in low and "deployment" in low:
        return "Deployment 負責維持 Pod 副本數、滾動更新與自動重建；Service 提供固定 DNS/IP，透過 selector 把流量導到符合 label 的 Pods。簡單說：Deployment 管應用怎麼跑，Service 管別人怎麼連到它。", []

    rag_text, rag_docs = _rag_fallback_context(message, history)
    if rag_text:
        return "我先用本地 RAG 知識庫回答：\n\n" + rag_text, rag_docs

    return "我可以回答一般問題與 Kubernetes 問題；目前本地模型 server 沒有啟動，所以先用快速規則/RAG 回覆。若要完整自然語言能力，請另外開一個終端執行 `python3 core/model_server.py`，再開網站。", []

@app.route("/api/status")
def api_status():
    ready, loading = _model_status()
    return jsonify({"model_ready": ready, "model_loading": loading, "k8s": K8S_ENABLED, "claude_api": claude_available()})

@app.route("/api/chat", methods=["POST"])
def api_chat():
    if "username" not in session:
        return jsonify({"error": "Not authenticated"}), 401
    data    = request.get_json() or {}
    message = data.get("message","").strip()
    history = data.get("history",[])
    if not message:
        return jsonify({"error": "Empty message"}), 400
    if len(history) > 40:
        history = history[-40:]
    if _looks_like_system_help(message):
        return jsonify({"reply": _system_help_reply(message), "source": "system_help"})
    reply, sources = chat_llama(message, history)
    if reply.startswith("[Local Model unavailable]"):
        reply, sources = _fallback_chat_reply(message, history)
    resp = {"reply": reply}
    if sources:
        from rag.retriever import confidence_label
        resp["sources"] = [
            {"source": s.get("source"), "score": s.get("score"),
             "confidence": confidence_label(s.get("score", 0)),
             "text": s.get("text", "")[:300]}
            for s in sources
        ]
    return jsonify(resp)


@app.route("/api/rag/status")
def api_rag_status():
    if "username" not in session:
        return jsonify({"error": "Not authenticated"}), 401
    return jsonify(kb_manager.get_status())


@app.route("/api/rag/docs", methods=["GET", "POST"])
def api_rag_docs():
    if "username" not in session:
        return jsonify({"error": "Not authenticated"}), 401
    if request.method == "GET":
        return jsonify({"documents": kb_manager.list_documents()})

    data = request.get_json(silent=True) or {}
    filename = str(data.get("filename", "")).strip()
    content  = data.get("content", "")
    try:
        result = kb_manager.add_document(filename, content)
        return jsonify({"success": True, **result})
    except KBError as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/rag/docs/<path:filename>", methods=["DELETE"])
def api_rag_delete_doc(filename):
    if "username" not in session:
        return jsonify({"error": "Not authenticated"}), 401
    try:
        result = kb_manager.delete_document(filename)
        return jsonify({"success": True, **result})
    except KBError as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/rag/rebuild", methods=["POST"])
def api_rag_rebuild():
    if "username" not in session:
        return jsonify({"error": "Not authenticated"}), 401
    try:
        meta = kb_manager.rebuild_index()
        return jsonify({"success": True, **meta})
    except KBError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"重建索引失敗：{e}"}), 500


@app.route("/api/rag/query", methods=["POST"])
def api_rag_query():
    if "username" not in session:
        return jsonify({"error": "Not authenticated"}), 401
    data = request.get_json(silent=True) or {}
    query_text = str(data.get("query", "")).strip()
    top_k = int(data.get("top_k", 5) or 5)
    if not query_text:
        return jsonify({"error": "查詢字串不能是空的"}), 400
    from rag.retriever import retrieve, active_method, confidence_label
    results = retrieve(query_text, top_k=top_k, min_score=0.0)
    return jsonify({
        "method": active_method(),
        "results": [
            {"source": r["source"], "score": r["score"],
             "confidence": confidence_label(r["score"]), "text": r["text"]}
            for r in results
        ],
    })


@app.route("/api/rag/eval", methods=["POST"])
def api_rag_eval():
    if "username" not in session:
        return jsonify({"error": "Not authenticated"}), 401
    try:
        from rag.eval_retrieval import run as run_eval
        report = run_eval(top_k=3)
        return jsonify(report)
    except Exception as e:
        return jsonify({"error": f"評估失敗：{e}"}), 500

def _sanitize_deploy_payload(raw: dict) -> dict:
    parsed = dict(raw or {})
    clean = {
        "app_name": str(parsed.get("app_name", "")).strip(),
        "image": str(parsed.get("image", "")).strip(),
    }
    try:
        clean["pods"] = int(parsed.get("pods", parsed.get("replicas", 1)))
    except (TypeError, ValueError):
        clean["pods"] = 0
    try:
        clean["port"] = int(parsed.get("port", 80))
    except (TypeError, ValueError):
        clean["port"] = 80
    memory = str(parsed.get("memory", "")).strip()
    if memory:
        clean["memory"] = memory
    cpu = str(parsed.get("cpu", "")).strip()
    if cpu:
        clean["cpu"] = cpu
    if parsed.get("node_count") is not None:
        try:
            node_count = int(parsed["node_count"])
            if node_count >= 1:
                clean["node_count"] = node_count
        except (TypeError, ValueError):
            pass
    if parsed.get("_parser"):
        clean["_parser"] = parsed.get("_parser")
    return clean


def _prepare_deploy(user_input: str, parsed_override: dict = None):
    parsed = _sanitize_deploy_payload(parsed_override) if parsed_override else ask_llama(user_input)
    if "error" in parsed:
        return None, None, None, ({"error": parsed["error"]}, 422)

    enriched = enrich_parsed_result(user_input, parsed)
    if not enriched["is_k8s"]:
        return parsed, enriched, None, ({
            "parsed": enriched,
            "k8s": False,
            "rejected": True,
            "reason": "Guardian: 此請求不像 K8s 任務 (is_k8s=false)",
        }, 200)

    missing = [k for k in ("app_name", "image", "pods") if not parsed.get(k)]
    if missing:
        return parsed, enriched, None, ({"error": f"Missing required field(s): {', '.join(missing)}", "parsed": enriched}, 422)
    try:
        parsed["pods"] = int(parsed["pods"])
        parsed["port"] = int(parsed.get("port", 80))
    except (TypeError, ValueError):
        return parsed, enriched, None, ({"error": "Invalid pods or port", "parsed": enriched}, 422)
    if not 1 <= parsed["pods"] <= 100:
        return parsed, enriched, None, ({"error": "pods must be between 1 and 100", "parsed": enriched}, 422)
    if not 1 <= parsed["port"] <= 65535:
        return parsed, enriched, None, ({"error": "port must be between 1 and 65535", "parsed": enriched}, 422)
    if parsed.get("memory") and not re.match(r"^\d+(Mi|Gi|Ki|M|G)$", str(parsed["memory"])):
        return parsed, enriched, None, ({"error": "memory must look like 128Mi or 1Gi", "parsed": enriched}, 422)

    review = _review_deployment(parsed)
    try:
        if parsed.get("node_count") is not None:
            # 模型自己判斷出來的 node_count（訓練資料裡有明講 node 容量時才會出現）
            review["node_estimate"] = {"node_count": parsed["node_count"], "source": "llm"}
        else:
            from agents.cost_agent import estimate_node_count
            result = estimate_node_count(parsed.get("cpu"), parsed.get("memory"), parsed.get("pods", 1))
            result["source"] = "calculated"
            review["node_estimate"] = result
    except Exception as e:
        review["node_estimate"] = None
        review.setdefault("warnings", []).append(f"node_estimate failed: {e}")
    if review["decision"] == "block":
        return parsed, enriched, review, ({
            "parsed": enriched,
            "k8s": False,
            "rejected": True,
            "reason": review["reason"],
            "review": review,
        }, 200)
    return parsed, enriched, review, None


@app.route("/api/deploy/parse", methods=["POST"])
def api_deploy_parse():
    if "username" not in session:
        return jsonify({"error": "Not authenticated"}), 401
    data = request.get_json() or {}
    user_input = data.get("input", "").strip()
    if not user_input or len(user_input) < 3:
        return jsonify({"error": "Input too short"}), 400
    parsed, enriched, review, error = _prepare_deploy(user_input)
    if error:
        payload, status = error
        return jsonify(payload), status
    return jsonify({"parsed": enriched, "raw": parsed, "review": review, "k8s": K8S_ENABLED})


@app.route("/api/deploy", methods=["POST"])
def api_deploy():
    if "username" not in session:
        return jsonify({"error": "Not authenticated"}), 401
    data = request.get_json() or {}
    user_input = data.get("input", "").strip()
    parsed_override = data.get("parsed")
    if not user_input and parsed_override:
        user_input = f"deploy {parsed_override.get('pods', 1)} {parsed_override.get('image', 'nginx:latest')} pods for {parsed_override.get('app_name', 'auto-app')}, port {parsed_override.get('port', 80)}"
    if not user_input or len(user_input) < 3:
        return jsonify({"error": "Input too short"}), 400

    parsed, enriched, review, error = _prepare_deploy(user_input, parsed_override)
    if error:
        payload, status = error
        return jsonify(payload), status

    gitops_result = None
    try:
        from gitops.manifest_writer import write_manifest
        gitops_result = write_manifest(parsed, repo_path=ROOT, namespace=NS, dry_run=False, commit=True)
    except Exception as e:
        gitops_result = {"ok": False, "message": str(e), "files": [], "commit_sha": None}
        review.setdefault("warnings", []).append(f"GitOps manifest write failed: {e}")

    threading.Thread(target=save_gold_sample, args=(user_input, parsed), daemon=True).start()

    # 同步呼叫（不是背景執行緒）：讓回應反映 K8s 是不是真的部署成功，而不是「沒報錯就當作成功」。
    # k8s_deploy() 內部四個 API 呼叫都已加上 _request_timeout=(5,10) 且 retries=0，不會無限期卡住這個 request。
    k8s_deploy_result = None
    if K8S_ENABLED:
        ok, message = k8s_deploy(parsed["app_name"], parsed["image"], parsed["pods"], parsed.get("port", 80), parsed.get("memory"), parsed.get("cpu"))
        k8s_deploy_result = {"ok": ok, "message": message}
        if not ok:
            review.setdefault("warnings", []).append(f"K8s 部署失敗：{message}")

    return jsonify({"parsed": enriched, "k8s": K8S_ENABLED, "review": review, "gitops": gitops_result, "k8s_deploy": k8s_deploy_result})

@app.route("/api/pods")
def api_pods():
    if "username" not in session:
        return jsonify({"error": "Not authenticated"}), 401
    return jsonify({"pods": k8s_get_pods()})

@app.route("/api/deployments")
def api_deployments():
    if "username" not in session:
        return jsonify({"error": "Not authenticated"}), 401
    return jsonify({"deployments": k8s_get_deployments()})

@app.route("/api/delete", methods=["POST"])
def api_delete():
    if "username" not in session:
        return jsonify({"error": "Not authenticated"}), 401
    name = (request.get_json() or {}).get("name","").strip()
    if not name:
        return jsonify({"error": "Name required"}), 400
    ok, msg = k8s_delete_deployment(name)
    return jsonify({"success": ok, "message": msg, "error": None if ok else msg})



@app.route("/api/scale", methods=["POST"])
def api_scale():
    if "username" not in session:
        return jsonify({"error": "Not authenticated"}), 401
    if not K8S_ENABLED:
        return jsonify({"success": False, "error": "K8s 未連線"}), 503
    data = request.get_json() or {}
    name = data.get("name", "").strip()
    try:
        replicas = int(data.get("replicas", 1))
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "replicas must be an integer"}), 400
    if not name:
        return jsonify({"success": False, "error": "Name required"}), 400
    if not 1 <= replicas <= 100:
        return jsonify({"success": False, "error": "replicas must be between 1 and 100"}), 400
    try:
        api = k8s_client.AppsV1Api()
        body = {"spec": {"replicas": replicas}}
        api.patch_namespaced_deployment_scale(name=name, namespace=NS, body=body)
        return jsonify({"success": True, "message": f"已調整 {name} replicas={replicas}"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/update", methods=["POST"])
def api_update():
    if "username" not in session:
        return jsonify({"error": "Not authenticated"}), 401
    if not K8S_ENABLED:
        return jsonify({"success": False, "error": "K8s 未連線"}), 503
    data = request.get_json() or {}
    name = data.get("name", "").strip()
    image = data.get("image", "").strip()
    if not name or not image:
        return jsonify({"success": False, "error": "name and image are required"}), 400
    try:
        api = k8s_client.AppsV1Api()
        dep = api.read_namespaced_deployment(name, NS)
        if not dep.spec.template.spec.containers:
            return jsonify({"success": False, "error": "deployment has no containers"}), 400
        old_image = dep.spec.template.spec.containers[0].image
        annotations = dep.spec.template.metadata.annotations or {}
        annotations["zerotouch.k8s/previous-image"] = old_image
        annotations["zerotouch.k8s/updated-at"] = datetime.utcnow().isoformat()
        dep.spec.template.metadata.annotations = annotations
        dep.spec.template.spec.containers[0].image = image
        api.patch_namespaced_deployment(name, NS, dep)
        return jsonify({"success": True, "message": f"已更新 {name} image={image}", "previous_image": old_image})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/rollback", methods=["POST"])
def api_rollback():
    if "username" not in session:
        return jsonify({"error": "Not authenticated"}), 401
    data = request.get_json() or {}
    app_name = (data.get("app_name") or data.get("name") or "").strip()
    if not app_name:
        return jsonify({"success": False, "error": "app_name required"}), 400
    try:
        from gitops.rollback import rollback
        result = rollback(app_name, namespace=NS, repo_path=ROOT, strategy="auto", dry_run=False)
        if result.get("ok"):
            return jsonify({
                "success": True,
                "message": result.get("message", ""),
                "details": result.get("details", {}),
                "strategy": result.get("strategy"),
                "error": None,
            })
    except Exception as e:
        result = {"message": str(e), "details": {}}

    # Fallback without kubectl: revert to the previous image annotation stored by /api/update.
    try:
        if not K8S_ENABLED:
            return jsonify({"success": False, "error": "K8s 未連線"}), 503
        api = k8s_client.AppsV1Api()
        dep = api.read_namespaced_deployment(app_name, NS)
        annotations = dep.spec.template.metadata.annotations or {}
        previous = annotations.get("zerotouch.k8s/previous-image")
        if not previous:
            return jsonify({
                "success": False,
                "error": result.get("message", "rollback failed; no previous image annotation"),
                "details": result.get("details", {}),
            }), 500
        current = dep.spec.template.spec.containers[0].image
        annotations["zerotouch.k8s/previous-image"] = current
        annotations["zerotouch.k8s/rolled-back-at"] = datetime.utcnow().isoformat()
        dep.spec.template.metadata.annotations = annotations
        dep.spec.template.spec.containers[0].image = previous
        api.patch_namespaced_deployment(app_name, NS, dep)
        return jsonify({
            "success": True,
            "message": f"已回滾 {app_name} image {current} → {previous}",
            "strategy": "python-client-image",
            "details": {"previous_image": previous, "current_image": current},
            "error": None,
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e), "details": result.get("details", {})}), 500

@app.route("/api/dataset/stats")
def api_dataset_stats():
    import glob
    dataset_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dataset")
    files_info = {}
    total = 0
    output_filled = 0
    k8s_count = 0
    non_k8s_count = 0
    categories = {}
    scan_paths = (
        glob.glob(os.path.join(os.path.dirname(os.path.abspath(__file__)), "*.jsonl")) +
        glob.glob(os.path.join(dataset_dir, "*.jsonl"))
    )
    for path in scan_paths:
        name = os.path.basename(path)
        cnt = 0
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    cnt += 1
                    total += 1
                    if rec.get("output") is not None:
                        output_filled += 1
                    if "is_k8s" in rec:
                        if rec["is_k8s"]:
                            k8s_count += 1
                        else:
                            non_k8s_count += 1
                    cat = rec.get("category", "")
                    if cat:
                        categories[cat] = categories.get(cat, 0) + 1
        except Exception:
            pass
        if cnt:
            files_info[name] = cnt
    output_pct = round(output_filled / total * 100, 1) if total else 0
    top_cats = sorted(categories.items(), key=lambda x: -x[1])[:10]
    return jsonify({"total_records": total, "output_filled": output_filled,
        "output_pct": output_pct, "k8s_count": k8s_count, "non_k8s_count": non_k8s_count,
        "files": files_info, "top_categories": top_cats})


@app.route("/api/dataset/run", methods=["POST"])
def api_dataset_run():
    import subprocess
    data = request.get_json() or {}
    flags = data.get("flags", "--skip-output")
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "enrich_dataset.py")
    if not os.path.exists(script):
        return jsonify({"success": False, "error": "enrich_dataset.py not found", "log": ""})
    cmd = ["python3", script] + (flags.split() if flags else [])
    try:
        result = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=600)
        log = result.stdout + ("\nSTDERR:\n" + result.stderr if result.stderr else "")
        return jsonify({"success": result.returncode == 0, "log": log})
    except subprocess.TimeoutExpired:
        return jsonify({"success": False, "log": "Timeout after 600s", "error": "timeout"})
    except Exception as e:
        return jsonify({"success": False, "log": str(e), "error": str(e)})

@app.route("/api/gitops")
def api_gitops():
    if "username" not in session: return jsonify({"error": "Not authenticated"}), 401
    import subprocess
    try:
        result = subprocess.run(
            ["git", "log", "--pretty=format:%H|%s|%ai", "--", "manifests/", "yamls/deployments/"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            capture_output=True, text=True, timeout=10
        )
        commits = []
        for line in result.stdout.strip().split("\n"):
            if not line: continue
            parts = line.split("|", 2)
            if len(parts) < 2: continue
            h = parts[0][:7]
            msg = parts[1] if len(parts) > 1 else ""
            time = parts[2][:16] if len(parts) > 2 else ""
            app_name = ""
            m = re.search(r"gitops:\s*(?:deploy|revert)\s+([^\s]+)", msg, re.IGNORECASE)
            if m:
                app_name = m.group(1)
            elif "deploy" in msg.lower():
                words = msg.split()
                for i, w in enumerate(words):
                    if w.lower() == "deploy" and i+1 < len(words):
                        app_name = words[i+1]
                        break
            commits.append({"hash": h, "message": msg, "time": time, "app": app_name})
        return jsonify({"commits": commits[:20]})
    except Exception as e:
        return jsonify({"commits": [], "error": str(e)})


@app.route("/api/healer/scan")
def api_healer_scan():
    if "username" not in session: return jsonify({"error": "Not authenticated"}), 401
    try:
        from healer.pod_watcher import scan_once
        issues = scan_once()
        return jsonify({"issues": issues or []})
    except Exception as e:
        try:
            from kubernetes import client as k8s_client, config as k8s_config
            k8s_config.load_kube_config()
            v1 = k8s_client.CoreV1Api()
            pods = v1.list_namespaced_pod(namespace="default")
            issues = []
            bad = {"CrashLoopBackOff", "OOMKilled", "ImagePullBackOff", "ErrImagePull", "Error"}
            for pod in pods.items:
                if pod.status and pod.status.container_statuses:
                    for cs in pod.status.container_statuses:
                        reason = ""
                        if cs.state and cs.state.waiting:
                            reason = cs.state.waiting.reason or ""
                        elif cs.state and cs.state.terminated:
                            reason = cs.state.terminated.reason or ""
                        if reason in bad:
                            issues.append({"pod": pod.metadata.name, "status": reason, "action": "Needs fix"})
            return jsonify({"issues": issues})
        except Exception as e2:
            return jsonify({"issues": [], "error": str(e2)})


@app.route("/api/healer/fix", methods=["POST"])
def api_healer_fix():
    if "username" not in session: return jsonify({"error": "Not authenticated"}), 401
    data = request.get_json() or {}
    pod_name = data.get("pod_name", "")
    if not pod_name:
        return jsonify({"success": False, "error": "No pod name"})
    try:
        from kubernetes import client as k8s_client, config as k8s_config
        k8s_config.load_kube_config()
        v1 = k8s_client.CoreV1Api()
        v1.delete_namespaced_pod(name=pod_name, namespace="default")
        return jsonify({"success": True, "message": f"Pod {pod_name} deleted, ReplicaSet will recreate"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route("/api/healer/auto_fix", methods=["POST"])
def api_healer_auto_fix():
    if "username" not in session: return jsonify({"error": "Not authenticated"}), 401
    try:
        from kubernetes import client as k8s_client, config as k8s_config
        k8s_config.load_kube_config()
        v1 = k8s_client.CoreV1Api()
        pods = v1.list_namespaced_pod(namespace="default")
        fixed = 0
        failed = 0
        bad = {"CrashLoopBackOff", "OOMKilled", "ImagePullBackOff", "ErrImagePull", "Error"}
        for pod in pods.items:
            if pod.status and pod.status.container_statuses:
                for cs in pod.status.container_statuses:
                    reason = ""
                    if cs.state and cs.state.waiting:
                        reason = cs.state.waiting.reason or ""
                    if reason in bad:
                        try:
                            v1.delete_namespaced_pod(name=pod.metadata.name, namespace="default")
                            fixed += 1
                        except:
                            failed += 1
        return jsonify({"fixed": fixed, "failed": failed})
    except Exception as e:
        return jsonify({"fixed": 0, "failed": 0, "error": str(e)})


@app.route("/api/metrics")
def api_metrics():
    if "username" not in session: return jsonify({"error": "Not authenticated"}), 401
    prom_url = "http://localhost:9090"
    try:
        import urllib.request as ur
        def prom_query(q):
            url = f"{prom_url}/api/v1/query?query={urllib.parse.quote(q)}"
            with ur.urlopen(url, timeout=3) as r:
                d = json.loads(r.read())
            if d.get("status") == "success" and d["data"]["result"]:
                return float(d["data"]["result"][0]["value"][1])
            return None
        import urllib.parse
        metrics = {}
        metrics["prometheus_up"] = True
        try: metrics["pod_count"] = prom_query('count(kube_pod_info{namespace="default"})')
        except: metrics["pod_count"] = None
        try: metrics["running_pods"] = prom_query('count(kube_pod_status_phase{phase="Running",namespace="default"})')
        except: metrics["running_pods"] = None
        try: metrics["kube_pods"] = prom_query("count(kube_pod_info)")
        except: metrics["kube_pods"] = None
        if K8S_ENABLED and (metrics["pod_count"] is None or metrics["running_pods"] is None):
            pods = k8s_get_pods()
            if metrics["pod_count"] is None:
                metrics["pod_count"] = len(pods)
            if metrics["running_pods"] is None:
                metrics["running_pods"] = len([p for p in pods if p.get("phase") == "Running"])
            if metrics["kube_pods"] is None:
                metrics["kube_pods"] = len(pods)
            metrics["source"] = "prometheus+k8s-fallback"
        return jsonify({"connected": True, "url": prom_url, "metrics": metrics})
    except Exception as e:
        return jsonify({"connected": False, "error": str(e)})

if __name__ == "__main__":
    print("=" * 60)
    print("  ZeroTouch K8s Web Demo v2")
    print("=" * 60)
    print(f"  K8s   : {'Connected' if K8S_ENABLED else 'Simulation'}")
    print(f"  Open  : http://localhost:5050")
    app.run(host="0.0.0.0", port=5050, debug=False)
