"""
web_demo.py  —  ZeroTouch K8s Platform v2
Clean white UI + Login/Register + Pod Details + AI Chat + Real K8s
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core.config import ensure_utf8_output
ensure_utf8_output()

import threading, json, urllib.request, hashlib, secrets, re, uuid
from datetime import datetime
from flask import Flask, request, jsonify, render_template_string, session, redirect, url_for

from core.config import YAML_DIR, MODEL_SERVER_URL
from llama_client import ask_llama, save_gold_sample
from core.claude_client import claude_chat, is_available as claude_available


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
app.secret_key = secrets.token_hex(32)

# ── Simple in-memory user store (replace with DB for production) ──
USERS = {}  # username -> {password_hash, created_at}

def hash_password(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

# ── Model status ────────────────────────────────────────────
def _model_status():
    try:
        with urllib.request.urlopen(f"{MODEL_SERVER_URL}/health", timeout=2) as resp:
            data = json.loads(resp.read())
            return data.get("model_loaded", False), False
    except Exception:
        return False, False

# ── K8s helpers ─────────────────────────────────────────────
def k8s_deploy(app_name, image, replicas, port=80, memory=None):
    if not K8S_ENABLED:
        return False, "K8s 未連線（模擬模式）"
    try:
        api  = k8s_client.AppsV1Api()
        core = k8s_client.CoreV1Api()
        resources = None
        if memory:
            resources = k8s_client.V1ResourceRequirements(
                requests={"memory": memory, "cpu": "100m"},
                limits  ={"memory": memory, "cpu": "500m"},
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
            api.replace_namespaced_deployment(app_name, NS, deploy)
        except Exception:
            api.create_namespaced_deployment(NS, deploy)
        try:
            core.replace_namespaced_service(f"{app_name}-svc", NS, svc)
        except Exception:
            core.create_namespaced_service(NS, svc)
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
                "age"       : str(p.metadata.creation_timestamp)[:16] if p.metadata.creation_timestamp else "",
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
        deps = api.list_namespaced_deployment(NS)
        return [
            {
                "name"    : d.metadata.name,
                "replicas": d.spec.replicas or 0,
                "ready"   : d.status.ready_replicas or 0,
                "image"   : d.spec.template.spec.containers[0].image if d.spec.template.spec.containers else "",
                "age"     : str(d.metadata.creation_timestamp)[:16] if d.metadata.creation_timestamp else "",
            }
            for d in deps.items
        ]
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

# ── HTML ────────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ZeroTouch K8s</title>
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
.sidebar-logo svg{width:28px;height:28px}
.sidebar-logo span{font-size:15px;font-weight:600}
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

/* ── Loading ── */
.loading-overlay{position:fixed;inset:0;background:rgba(255,255,255,.95);display:flex;flex-direction:column;align-items:center;justify-content:center;z-index:9999;gap:16px}
.spinner{width:36px;height:36px;border:3px solid var(--border);border-top-color:var(--green);border-radius:50%;animation:spin 1s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.loading-text{font-size:14px;color:var(--text2)}

/* ── Empty state ── */
.empty{text-align:center;padding:48px 20px;color:var(--text2)}
.empty svg{width:40px;height:40px;margin:0 auto 12px;opacity:.3}
.empty p{font-size:14px}
</style>
</head>
<body>

{% if not logged_in %}
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
      <span>ZeroTouch K8s</span>
    </div>
    <nav class="sidebar-nav">
      <div class="nav-section">Main</div>
      <button class="nav-item active" onclick="showPage('deploy')">
        <svg viewBox="0 0 16 16" fill="none"><rect x="2" y="2" width="5" height="5" rx="1" fill="currentColor"/><rect x="9" y="2" width="5" height="5" rx="1" fill="currentColor" opacity=".5"/><rect x="2" y="9" width="5" height="5" rx="1" fill="currentColor" opacity=".5"/><rect x="9" y="9" width="5" height="5" rx="1" fill="currentColor"/></svg>
        Dashboard
      </button>
      <button class="nav-item" onclick="showPage('pods')">
        <svg viewBox="0 0 16 16" fill="none"><circle cx="8" cy="8" r="5" stroke="currentColor" stroke-width="1.5"/><circle cx="8" cy="8" r="2" fill="currentColor"/></svg>
        Pods
      </button>
      <button class="nav-item" onclick="showPage('deployments')">
        <svg viewBox="0 0 16 16" fill="none"><path d="M2 4h12M2 8h12M2 12h8" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>
        Deployments
      </button>
      <div class="nav-section">Tools</div>
      <button class="nav-item" onclick="showPage('chat')">
        <svg viewBox="0 0 16 16" fill="none"><path d="M2 3a1 1 0 011-1h10a1 1 0 011 1v7a1 1 0 01-1 1H9l-3 2v-2H3a1 1 0 01-1-1V3z" stroke="currentColor" stroke-width="1.5"/></svg>
        AI Chat
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
    <div class="page active" id="page-deploy">
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
                <th>Age</th>
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
                <th>Age</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody id="deps-tbody">
              <tr><td colspan="6" class="empty"><p>Loading...</p></td></tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- Chat Page -->
    <div class="page" id="page-chat">
      <div class="page-title">AI Chat</div>
      <div class="page-sub">Ask anything about Kubernetes or deployments</div>
      <div class="card" style="height:calc(100vh - 190px);display:flex;flex-direction:column">
        <div class="chat-messages" id="chat-messages">
          <div class="msg ai">
            <div class="msg-avatar">K</div>
            <div class="msg-bubble">Hi! I'm your K8s assistant. Ask me anything about Kubernetes, deployments, or how to use this system.</div>
          </div>
        </div>
        <div class="chat-input-wrap">
          <textarea class="chat-input" id="chat-input" placeholder="Ask anything..." rows="1" onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();sendChat()}"></textarea>
          <button class="chat-send" onclick="sendChat()">Send</button>
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

// ── Page nav ──
function showPage(name){
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  document.getElementById('page-' + name).classList.add('active');
  event.currentTarget.classList.add('active');
  if(name === 'pods') loadPods();
  if(name === 'deployments') loadDeployments();
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

async function doDeploy(){
  const inp = document.getElementById('deploy-input');
  const btn = document.getElementById('deploy-btn');
  const box = document.getElementById('result-box');
  const card = document.getElementById('enrich-card');
  const text = inp.value.trim();
  if(!text) return;
  btn.disabled = true;
  btn.textContent = 'Deploying...';
  box.style.display = 'none';
  card.classList.remove('show');
  try {
    const r = await fetch('/api/deploy', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({input: text})
    });
    const d = await r.json();
    if(d.error){
      box.style.display = 'block';
      box.className = 'result-box error';
      box.textContent = '✗ Error: ' + d.error;
    } else if(d.rejected){
      // Guardian 拒絕:只顯示 enrichment 卡片,不送 K8s
      renderEnrichCard(d.parsed, {
        ok: false,
        headline: 'Guardian blocked this request',
        rejectMsg: d.reason || 'Not a K8s request'
      });
    } else {
      // 正常部署:顯示 enrichment 卡片 + 簡短成功訊息
      renderEnrichCard(d.parsed, {
        ok: true,
        headline: d.k8s ? 'Real K8s deployment dispatched' : 'Simulation mode',
        rejectMsg: null
      });
      const p = d.parsed;
      box.style.display = 'block';
      box.className = 'result-box success';
      box.textContent = `✓ App: ${p.app_name}  ·  Image: ${p.image}  ·  Pods: ${p.pods}${p.port ? '  ·  Port: ' + p.port : ''}${p.memory ? '  ·  Memory: ' + p.memory : ''}`;
      loadStats();
    }
  } catch(e){
    box.style.display = 'block';
    box.className = 'result-box error';
    box.textContent = '✗ Network error';
  }
  btn.disabled = false;
  btn.textContent = 'Deploy →';
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
  tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;padding:32px;color:var(--text3)">Loading...</td></tr>';
  try {
    const r = await fetch('/api/deployments');
    const d = await r.json();
    if(!d.deployments.length){
      tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;padding:32px;color:var(--text3)">No deployments found</td></tr>';
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
        <td>
          <div class="action-btns">
            <button class="btn-sm btn-danger" onclick="deleteDeployment('${dep.name}')">Delete</button>
          </div>
        </td>
      </tr>
    `).join('');
  } catch(e){
    tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;color:var(--red)">Failed to load</td></tr>';
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
async function sendChat(){
  const inp = document.getElementById('chat-input');
  const msgs = document.getElementById('chat-messages');
  const text = inp.value.trim();
  if(!text) return;
  inp.value = '';

  msgs.innerHTML += `<div class="msg user"><div class="msg-avatar">U</div><div class="msg-bubble">${text}</div></div>`;
  const typing = document.createElement('div');
  typing.className = 'msg ai';
  typing.innerHTML = '<div class="msg-avatar">K</div><div class="typing"><span></span><span></span><span></span></div>';
  msgs.appendChild(typing);
  msgs.scrollTop = msgs.scrollHeight;

  chatHistory.push({role:'user', content: text});
  try {
    const r = await fetch('/api/chat', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({message: text, history: chatHistory})
    });
    const d = await r.json();
    typing.remove();
    const reply = d.reply || d.error || 'No response';
    chatHistory.push({role:'assistant', content: reply});
    msgs.innerHTML += `<div class="msg ai"><div class="msg-avatar">K</div><div class="msg-bubble">${reply.replace(/\n/g,'<br>')}</div></div>`;
    msgs.scrollTop = msgs.scrollHeight;
  } catch(e){
    typing.remove();
    msgs.innerHTML += `<div class="msg ai"><div class="msg-avatar">K</div><div class="msg-bubble" style="color:var(--red)">Connection error</div></div>`;
  }
}
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
    session["username"] = username
    return redirect("/")

@app.route("/auth/login", methods=["POST"])
def login():
    username = request.form.get("username","").strip()
    password = request.form.get("password","")
    if username in USERS and USERS[username]["password_hash"] == hash_password(password):
        session["username"] = username
        return redirect("/")
    return render_template_string(HTML, logged_in=False, page='login', error="Invalid username or password", k8s=K8S_ENABLED, username='')

@app.route("/auth/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect("/")

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
    reply = claude_chat(message, history)
    return jsonify({"reply": reply})

@app.route("/api/deploy", methods=["POST"])
def api_deploy():
    if "username" not in session:
        return jsonify({"error": "Not authenticated"}), 401
    data       = request.get_json()
    user_input = (data or {}).get("input","").strip()
    if not user_input or len(user_input) < 3:
        return jsonify({"error": "Input too short"}), 400
    parsed = ask_llama(user_input)
    if "error" in parsed:
        return jsonify({"error": parsed["error"]}), 422

    # ── 套用資料集欄位 enrichment ──────────────────────────────
    enriched = enrich_parsed_result(user_input, parsed)

    # Guardian 雛形:若判斷不是 K8s 請求,拒絕部署但仍回傳分類結果讓使用者看到
    if not enriched["is_k8s"]:
        return jsonify({
            "parsed":   enriched,
            "k8s":      False,
            "rejected": True,
            "reason":   "Guardian: 此請求不像 K8s 任務 (is_k8s=false)"
        }), 200

    threading.Thread(target=save_gold_sample, args=(user_input, parsed), daemon=True).start()
    if K8S_ENABLED:
        threading.Thread(
            target=k8s_deploy,
            args=(parsed["app_name"], parsed["image"], parsed["pods"], parsed.get("port", 80), parsed.get("memory")),
            daemon=True
        ).start()
    return jsonify({"parsed": enriched, "k8s": K8S_ENABLED})

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

if __name__ == "__main__":
    print("=" * 60)
    print("  ZeroTouch K8s Web Demo v2")
    print("=" * 60)
    print(f"  K8s   : {'Connected' if K8S_ENABLED else 'Simulation'}")
    print(f"  Open  : http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=False)
