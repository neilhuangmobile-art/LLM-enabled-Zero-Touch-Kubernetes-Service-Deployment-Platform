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
app.secret_key = "zerotouch_k8s_2025_fixed_key"

# ── Simple in-memory user store (replace with DB for production) ──
USERS = {}
try:
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "users.json"), encoding="utf-8") as _uf:
        USERS = json.load(_uf)
except Exception:
    USERS = {}

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
    <nav class="sidebar-nav" style="display:flex;flex-direction:column;overflow:hidden">
      <div style="padding:10px 10px 6px">
        <button onclick="newChat()" style="width:100%;padding:9px 12px;background:var(--green);color:#fff;border:none;border-radius:8px;font-size:13px;font-weight:600;cursor:pointer;display:flex;align-items:center;justify-content:center;gap:6px">
          <span style="font-size:18px;line-height:1">+</span> New Chat
        </button>
      </div>
      <div class="nav-section">Chats</div>
      <div id="chat-room-list" style="flex:1;overflow-y:auto;padding:0 6px;min-height:60px;max-height:200px"></div>
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
      <button class="nav-item" onclick="showPage('gitops')">
        <svg viewBox="0 0 16 16" fill="none"><circle cx="5" cy="4" r="2" stroke="currentColor" stroke-width="1.5"/><circle cx="11" cy="12" r="2" stroke="currentColor" stroke-width="1.5"/><circle cx="11" cy="4" r="2" stroke="currentColor" stroke-width="1.5"/><path d="M5 6v1a3 3 0 003 3h1M11 6v2" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>
        GitOps Log
      </button>
      <button class="nav-item" onclick="showPage('healer')">
        <svg viewBox="0 0 16 16" fill="none"><path d="M8 2v12M2 8h12" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>
        Healer
      </button>
      <button class="nav-item" onclick="showPage('metrics')">
        <svg viewBox="0 0 16 16" fill="none"><path d="M2 12L5 8l3 2 3-4 3 2" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>
        Metrics
      </button>
      <button class="nav-item" onclick="showPage('dataset')">
        <svg viewBox="0 0 16 16" fill="none"><rect x="1" y="3" width="14" height="2" rx="1" fill="currentColor"/><rect x="1" y="7" width="14" height="2" rx="1" fill="currentColor" opacity=".6"/><rect x="1" y="11" width="9" height="2" rx="1" fill="currentColor" opacity=".3"/></svg>
        Dataset
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
    <div class="page" id="page-chat" style="padding:0;overflow:hidden">
      <div style="display:flex;flex-direction:column;height:calc(100vh - 56px);background:var(--bg)">
        <div class="chat-messages" id="chat-messages" style="flex:1;overflow-y:auto;padding:20px 28px"></div>
          <div style="border-top:1px solid var(--border);padding:12px 20px;background:var(--surface)">
            <div style="display:flex;gap:8px;align-items:flex-end">
              <textarea class="chat-input" id="chat-input" placeholder="Message ZeroTouch K8s..." rows="1" onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();sendChat()}"></textarea>
              <button class="chat-send" onclick="sendChat()">Send</button>
            </div>
            <div style="font-size:11px;color:var(--text3);margin-top:5px">Enter to send &middot; Shift+Enter for new line</div>
          </div>
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

// ── Page nav ──
function showPage(name){
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  document.getElementById('page-' + name).classList.add('active');
  if(event && event.currentTarget) event.currentTarget.classList.add('active');
}
  if(name === 'pods') loadPods();
  if(name === 'deployments') loadDeployments();
  if(name === 'dataset') loadDatasetStats();
  if(name === 'gitops') loadGitops();
  if(name === 'healer') loadHealer();
  if(name === 'metrics') loadMetrics();
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
  if(!chats.length){ newChat(); return; }
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
  const id = 'chat_' + Date.now();
  chats.push({id, title:'New Chat', messages:[]});
  currentChatId = id;
  saveChats();
  renderChatList();
  renderMessages();
  document.getElementById('chat-input').focus();
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
    <div onclick="switchChat('${ch.id}')" style="display:flex;align-items:center;justify-content:space-between;padding:8px 10px;border-radius:8px;cursor:pointer;margin-bottom:2px;font-size:12px;color:#e5e7eb;background:${ch.id===currentChatId?'rgba(22,163,74,.25)':'transparent'};transition:background .15s" onmouseover="this.querySelector('.del').style.opacity='1'" onmouseout="this.querySelector('.del').style.opacity='0'">
      <span style="display:flex;align-items:center;gap:6px;overflow:hidden;flex:1"><svg viewBox="0 0 16 16" fill="none" width="12" height="12" style="flex-shrink:0"><path d="M2 3a1 1 0 011-1h10a1 1 0 011 1v7a1 1 0 01-1 1H9l-3 2v-2H3a1 1 0 01-1-1V3z" stroke="currentColor" stroke-width="1.5"/></svg><span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${ch.title}</span></span>
      <span class="del" onclick="deleteChat('${ch.id}',event)" style="opacity:0;color:#9ca3af;font-size:14px;padding-left:6px;flex-shrink:0;transition:opacity .15s">&#x2715;</span>
    </div>`).join('');
}

function renderMessages(){
  const msgs = document.getElementById('chat-messages');
  if(!msgs) return;
  const ch = currentChat();
  if(!ch || !ch.messages.length){
    msgs.innerHTML = `<div style="display:flex;flex-direction:column;align-items:center;justify-content:center;height:100%;padding:40px 20px">
      <div style="width:56px;height:56px;border-radius:50%;background:var(--green);display:flex;align-items:center;justify-content:center;font-size:24px;font-weight:700;color:#fff;margin-bottom:16px">K</div>
      <div style="font-size:22px;font-weight:700;color:var(--text);margin-bottom:8px">ZeroTouch K8s Assistant</div>
      <div style="font-size:14px;color:var(--text2);text-align:center;max-width:480px;margin-bottom:32px">Deploy and manage Kubernetes services using natural language. Ask me anything about K8s or start with a quick action.</div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;width:100%;max-width:520px">
        <div onclick="document.getElementById('chat-input').value='deploy 3 nginx:latest pods for web-frontend';sendChat()" style="background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:16px;cursor:pointer;transition:border-color .15s" onmouseover="this.style.borderColor='var(--green)'" onmouseout="this.style.borderColor='var(--border)'">
          <div style="font-weight:600;font-size:13px;color:var(--text);margin-bottom:4px">Deploy a service</div>
          <div style="font-size:12px;color:var(--text3)">deploy 3 nginx:latest pods...</div>
        </div>
        <div onclick="document.getElementById('chat-input').value='list pods';sendChat()" style="background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:16px;cursor:pointer;transition:border-color .15s" onmouseover="this.style.borderColor='var(--green)'" onmouseout="this.style.borderColor='var(--border)'">
          <div style="font-weight:600;font-size:13px;color:var(--text);margin-bottom:4px">Check status</div>
          <div style="font-size:12px;color:var(--text3)">list pods / show deployments</div>
        </div>
        <div onclick="document.getElementById('chat-input').value='What is a Pod?';sendChat()" style="background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:16px;cursor:pointer;transition:border-color .15s" onmouseover="this.style.borderColor='var(--green)'" onmouseout="this.style.borderColor='var(--border)'">
          <div style="font-weight:600;font-size:13px;color:var(--text);margin-bottom:4px">Learn K8s</div>
          <div style="font-size:12px;color:var(--text3)">What is a Pod, Deployment...</div>
        </div>
        <div onclick="document.getElementById('chat-input').value='delete ';document.getElementById('chat-input').focus()" style="background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:16px;cursor:pointer;transition:border-color .15s" onmouseover="this.style.borderColor='var(--green)'" onmouseout="this.style.borderColor='var(--border)'">
          <div style="font-weight:600;font-size:13px;color:var(--text);margin-bottom:4px">Delete deployment</div>
          <div style="font-size:12px;color:var(--text3)">delete &lt;deployment-name&gt;</div>
        </div>
      </div>
    </div>`;
    return;
  }
  msgs.innerHTML = ch.messages.map(m=>renderMsgHTML(m.role, m.content)).join('');
  msgs.scrollTop = msgs.scrollHeight;
}

function renderMsgHTML(role, content){
  if(role==='user'){
    return `<div class="msg user" style="margin-bottom:16px"><div class="msg-avatar">U</div><div class="msg-bubble">${escHtml(content)}</div></div>`;
  }
  return `<div class="msg ai" style="margin-bottom:16px"><div class="msg-avatar">K</div><div class="msg-bubble" style="white-space:pre-wrap">${content}</div></div>`;
}

function escHtml(s){ return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

function appendMsg(role, content){
  const ch = currentChat();
  if(!ch) return;
  ch.messages.push({role, content});
  if(ch.messages.length===1 && role==='user'){
    ch.title = content.slice(0,30) + (content.length>30?'...':'');
  }
  saveChats();
  renderChatList();
  const msgs = document.getElementById('chat-messages');
  msgs.innerHTML += renderMsgHTML(role, content);
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
      let reply = pods.length ? pods.map(p=>`- ${p.name} [${p.status}] - ${p.image}`).join('\n') : 'No pods running.';
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
  else if(/^(deploy|start|launch|run|\u5e6b\u6211|\u8acb|\u90e8\u7f72|\u8d77\s)/i.test(text) || /\d+\s*(pods?|replicas?|\u500b)/i.test(text)){
    replied = true;
    removeTyping();
    const pipeId = 'pipe_'+Date.now();
    const steps = ['LLaMA Inference','Multi-Agent Review','Guardian Validation','Creating K8s Resources','Deployment Complete'];
    let pipeHTML = `<div id="${pipeId}" style="background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:16px">`;
    steps.forEach((s,i)=>{
      pipeHTML += `<div id="${pipeId}_${i}" style="display:flex;align-items:center;gap:10px;padding:5px 0;color:var(--text3);font-size:13px"><div style="width:18px;height:18px;border-radius:50%;border:2px solid var(--border);flex-shrink:0;text-align:center;font-size:10px;line-height:16px"></div>${s}</div>`;
    });
    pipeHTML += `<div id="${pipeId}_result" style="margin-top:8px;font-size:13px"></div></div>`;

    const ch2 = currentChat();
    if(ch2){ ch2.messages.push({role:'assistant', content: pipeHTML}); saveChats(); }
    const msgs2 = document.getElementById('chat-messages');
    const tempDiv = document.createElement('div');
    tempDiv.className = 'msg ai'; tempDiv.style.marginBottom='16px';
    tempDiv.innerHTML = `<div class="msg-avatar">K</div><div class="msg-bubble" style="padding:8px;background:transparent;border:none;max-width:100%">${pipeHTML}</div>`;
    msgs2.appendChild(tempDiv);
    msgs2.scrollTop = msgs2.scrollHeight;

    function setStep(i, done){
      const el = document.getElementById(`${pipeId}_${i}`);
      if(!el) return;
      el.style.color = done ? 'var(--green)' : 'var(--text)';
      const dot = el.querySelector('div');
      dot.style.background = done ? 'var(--green)' : 'var(--green-light)';
      dot.style.borderColor = 'var(--green)';
      dot.innerHTML = done ? '&#x2713;' : '';
    }

    setStep(0, false);
    try{
      const r = await fetch('/api/deploy',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({input:text})});
      setStep(0,true); setStep(1,false);
      await new Promise(res=>setTimeout(res,300));
      setStep(1,true); setStep(2,false);
      await new Promise(res=>setTimeout(res,200));
      setStep(2,true); setStep(3,false);
      const d = await r.json();
      await new Promise(res=>setTimeout(res,300));
      setStep(3,true); setStep(4,false);
      await new Promise(res=>setTimeout(res,200));
      setStep(4,true);
      const res2 = document.getElementById(`${pipeId}_result`);
      if(res2){
        if(d.rejected){ res2.innerHTML=`<div style="color:#ef4444">Blocked: ${d.reason||''}</div>`; }
        else if(d.parsed){
          const p=d.parsed;
          res2.innerHTML<`<div style="color:var(--green)">Deployed: <b>${p.app_name}</b> x${p.pods} (${p.image})${p.port?', port '+p.port:''}</div>`;
        }
        if(d.error){ res2.innerHTML<`<div style="color:#ef4444">${d.error}</div>`; }
      }
    }catch(e){
      const res2=document.getElementById(`${pipeId}_result`);
      if(res2) res2.innerHTML=`<div style="color:#ef4444">Error: ${e}</div>`;
    }
  }

  if(!replied){
    try{
      const hist = (currentChat()?.messages||[]).slice(-10).map(m=>({role:m.role==='assistant'?'assistant':'user',content:m.content}));
      const r = await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:text, history:hist})});
      const d = await r.json();
      removeTyping();
      appendMsg('assistant', d.reply||d.error||'No response');
    }catch(e){ removeTyping(); appendMsg('assistant','Connection error: '+e); }
  }
}


window.addEventListener('DOMContentLoaded', function(){
  if(document.getElementById('page-chat') && document.getElementById('page-chat').classList.contains('active')){
    initChats();
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
            ["git", "log", "--pretty=format:%H|%s|%ai", "--", "manifests/"],
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
            if "deploy" in msg.lower():
                words = msg.split()
                for i, w in enumerate(words):
                    if w.lower() in ("deploy", "gitops:") and i+1 < len(words):
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
        return jsonify({"connected": True, "url": prom_url, "metrics": metrics})
    except Exception as e:
        return jsonify({"connected": False, "error": str(e)})

if __name__ == "__main__":
    print("=" * 60)
    print("  ZeroTouch K8s Web Demo v2")
    print("=" * 60)
    print(f"  K8s   : {'Connected' if K8S_ENABLED else 'Simulation'}")
    print(f"  Open  : http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=False)
