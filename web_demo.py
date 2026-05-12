"""
web_demo.py  v3  —  ZeroTouch K8s Platform
Multi-chat rooms + Conversational AI + Real K8s Deploy
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core.config import ensure_utf8_output
ensure_utf8_output()

import threading, json, urllib.request, hashlib, secrets, uuid
from datetime import datetime
from flask import Flask, request, jsonify, render_template_string, session, redirect

from core.config import YAML_DIR, MODEL_SERVER_URL
from llama_client import ask_llama, save_gold_sample

K8S_ENABLED = False
try:
    from kubernetes import client as k8s_client, config as k8s_config
    import yaml as yaml_lib
    k8s_config.load_kube_config()
    K8S_ENABLED = True
    print("[K8s] Connected")
except Exception as e:
    print(f"[K8s] Not connected (simulation): {e}")

NS  = "default"
app = Flask(__name__)
app.secret_key = secrets.token_hex(32)

# ── User store ──────────────────────────────────────────────
import json as _j
_USERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "users.json")
def _load_users():
    try:
        if os.path.exists(_USERS_FILE):
            return _j.load(open(_USERS_FILE))
    except: pass
    return {}
def _save_users(u):
    try: _j.dump(u, open(_USERS_FILE, "w"))
    except: pass
USERS = _load_users()

def hash_pw(pw): return hashlib.sha256(pw.encode()).hexdigest()

# ── Chat store (in-memory, per user) ────────────────────────
# CHATS[username] = {chat_id: {"title": str, "messages": [...], "created": str}}
CHATS = {}

def get_user_chats(username):
    if username not in CHATS:
        CHATS[username] = {}
    return CHATS[username]

# ── K8s helpers ─────────────────────────────────────────────
def k8s_deploy(app_name, image, replicas, port=80, memory=None):
    if not K8S_ENABLED:
        return False, "K8s not connected (simulation mode)"
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
            metadata=k8s_client.V1ObjectMeta(name=app_name + "-svc"),
            spec=k8s_client.V1ServiceSpec(
                selector={"app": app_name},
                ports=[k8s_client.V1ServicePort(port=port, target_port=port)],
                type="LoadBalancer",
            ),
        )
        try: api.create_namespaced_deployment(NS, deploy)
        except: api.replace_namespaced_deployment(app_name, NS, deploy)
        try: core.create_namespaced_service(NS, svc)
        except: core.replace_namespaced_service(app_name + "-svc", NS, svc)
        os.makedirs(YAML_DIR, exist_ok=True)
        with open(os.path.join(YAML_DIR, app_name + ".yaml"), "w") as f:
            f.write(yaml_lib.dump(deploy.to_dict()))
        return True, "Deployed " + app_name
    except Exception as e:
        return False, str(e)

def k8s_get_pods():
    if not K8S_ENABLED: return []
    try:
        core = k8s_client.CoreV1Api()
        pods = core.list_namespaced_pod(NS)
        result = []
        for p in pods.items:
            ct = p.metadata.creation_timestamp
            if ct:
                diff = (datetime.now().replace(tzinfo=None) - ct.replace(tzinfo=None)).total_seconds()
                if diff < 3600: age = str(int(diff // 60)) + "m ago"
                elif diff < 86400: age = str(int(diff // 3600)) + "h ago"
                else: age = str(int(diff // 86400)) + "d ago"
            else: age = ""
            restarts = 0
            if p.status and p.status.container_statuses:
                restarts = sum(cs.restart_count for cs in p.status.container_statuses)
            containers = []
            if p.spec and p.spec.containers:
                for c in p.spec.containers:
                    containers.append({
                        "name": c.name, "image": c.image,
                        "ports": [cp.container_port for cp in (c.ports or [])],
                        "resources": {
                            "requests": dict(c.resources.requests) if c.resources and c.resources.requests else {},
                            "limits": dict(c.resources.limits) if c.resources and c.resources.limits else {},
                        }
                    })
            conds = []
            if p.status and p.status.conditions:
                for co in p.status.conditions:
                    conds.append({"type": co.type, "status": co.status})
            result.append({
                "name": p.metadata.name,
                "app": p.metadata.labels.get("app", "") if p.metadata.labels else "",
                "phase": p.status.phase or "Unknown",
                "ip": p.status.pod_ip or "",
                "node": p.spec.node_name or "",
                "age": age,
                "restarts": restarts,
                "containers": containers,
                "conditions": conds,
            })
        return result
    except: return []

def k8s_get_deployments():
    if not K8S_ENABLED: return []
    try:
        api = k8s_client.AppsV1Api()
        deps = api.list_namespaced_deployment(NS)
        result = []
        for d in deps.items:
            ct = d.metadata.creation_timestamp
            if ct:
                diff = (datetime.now().replace(tzinfo=None) - ct.replace(tzinfo=None)).total_seconds()
                if diff < 3600: age = str(int(diff // 60)) + "m ago"
                elif diff < 86400: age = str(int(diff // 3600)) + "h ago"
                else: age = str(int(diff // 86400)) + "d ago"
            else: age = ""
            result.append({
                "name": d.metadata.name,
                "replicas": d.spec.replicas or 0,
                "ready": d.status.ready_replicas or 0,
                "image": d.spec.template.spec.containers[0].image if d.spec.template.spec.containers else "",
                "age": age,
            })
        return result
    except: return []

def k8s_delete_deployment(name):
    if not K8S_ENABLED: return False, "K8s not connected"
    try:
        k8s_client.AppsV1Api().delete_namespaced_deployment(name, NS)
        try: k8s_client.CoreV1Api().delete_namespaced_service(name + "-svc", NS)
        except: pass
        return True, "Deleted " + name
    except Exception as e:
        return False, str(e)

def _model_status():
    try:
        with urllib.request.urlopen(MODEL_SERVER_URL + "/health", timeout=2) as resp:
            data = json.loads(resp.read())
            return data.get("model_loaded", False), False
    except: return False, True

# ── AI Chat Logic ────────────────────────────────────────────
PENDING_DEPLOYS = {}  # session_key -> parsed

def process_message(username, chat_id, message, history):
    """
    Conversational K8s agent.
    Returns (reply_text, action_taken)
    """
    msg_lower = message.lower().strip()

    # ── 確認部署 ──
    pending_key = username + ":" + chat_id
    if pending_key in PENDING_DEPLOYS:
        parsed = PENDING_DEPLOYS[pending_key]
        if any(w in msg_lower for w in ["yes","ok","sure","go","confirm","deploy","yes!","yep","好","確認","部署","執行"]):
            del PENDING_DEPLOYS[pending_key]
            import time as _t
            suffix = str(int(_t.time()))[-4:]
            app_name = parsed["app_name"] + "-" + suffix
            if K8S_ENABLED:
                ok, msg_r = k8s_deploy(app_name, parsed["image"], parsed["pods"],
                                       parsed.get("port", 80), parsed.get("memory"))
                threading.Thread(target=save_gold_sample, args=(message, parsed), daemon=True).start()
                if ok:
                    return ("Deployment started!\n\n"
                            "Name: " + app_name + "\n"
                            "Image: " + str(parsed["image"]) + "\n"
                            "Pods: " + str(parsed["pods"]) + "\n"
                            + ("Port: " + str(parsed["port"]) + "\n" if parsed.get("port") else "")
                            + "\nCheck the Pods tab in a few seconds."), "deployed"
                else:
                    return "Deployment failed: " + msg_r, "error"
            else:
                del PENDING_DEPLOYS[pending_key]
                return ("Simulation mode — K8s not connected.\n\nWould deploy:\n"
                        "Name: " + app_name + "\nImage: " + str(parsed["image"]) + "\nPods: " + str(parsed["pods"])), "simulated"
        elif any(w in msg_lower for w in ["no","cancel","stop","不","取消","算了"]):
            del PENDING_DEPLOYS[pending_key]
            return "Deployment cancelled. Let me know if you want to try something else!", "cancelled"
        else:
            # 提醒使用者
            app_name = parsed.get("app_name", "")
            return ("I'm waiting for your confirmation to deploy:\n\n"
                    "App: " + str(app_name) + "\n"
                    "Image: " + str(parsed.get("image", "")) + "\n"
                    "Pods: " + str(parsed.get("pods", "")) + "\n\n"
                    "Reply **yes** to deploy or **no** to cancel."), "waiting"

    deploy_kw = ["deploy","start","launch","run","create","spin up","build","新增","部署","建立","起","跑"]
    delete_kw = ["delete","remove","kill","destroy","刪除","移除","刪掉"]
    list_kw   = ["list","show","get","check","status","查","顯示","看","列出","有哪些","現在有","how many"]
    scale_kw  = ["scale","resize","replicas","adjust","調整","擴展","縮小","改成"]

    is_delete = any(k in msg_lower for k in delete_kw)
    is_list   = any(k in msg_lower for k in list_kw) and not any(k in msg_lower for k in deploy_kw + delete_kw)
    is_scale  = any(k in msg_lower for k in scale_kw) and not is_delete
    is_deploy = any(k in msg_lower for k in deploy_kw) and not is_delete

    # ── Deploy: parse first, ask for confirmation ──
    if is_deploy:
        parsed = ask_llama(message)
        if "error" in parsed or not parsed.get("image") or not parsed.get("pods"):
            return ("I couldn't quite parse that deployment request. Could you be more specific?\n\n"
                    "For example:\n"
                    "- deploy 3 nginx:latest pods for web-frontend\n"
                    "- start 2 redis:7-alpine pods, port 6379\n"
                    "- launch 1 python:3.11-slim pod named api-server"), "parse_failed"
        PENDING_DEPLOYS[pending_key] = parsed
        lines = ["Here's what I parsed from your request:\n"]
        lines.append("App name: **" + str(parsed.get("app_name", "")) + "**")
        lines.append("Image: **" + str(parsed.get("image", "")) + "**")
        lines.append("Pods: **" + str(parsed.get("pods", "")) + "**")
        if parsed.get("port"): lines.append("Port: **" + str(parsed["port"]) + "**")
        if parsed.get("memory"): lines.append("Memory: **" + str(parsed["memory"]) + "**")
        lines.append("\nDoes this look right? Reply **yes** to deploy or **no** to cancel.")
        return "\n".join(lines), "confirm_pending"

    # ── Delete ──
    elif is_delete:
        deps = k8s_get_deployments()
        dep_names = [d["name"] for d in deps]
        target = None
        for name in dep_names:
            if name.lower() in msg_lower:
                target = name
                break
        if not target:
            words = [w.strip(".,!?") for w in message.split()]
            for name in dep_names:
                for part in name.replace("-", " ").split():
                    if len(part) > 3 and part in [w.lower() for w in words]:
                        target = name
                        break
        if target:
            ok, msg_r = k8s_delete_deployment(target)
            return ("Deleted **" + target + "** successfully!" if ok else "Failed to delete: " + msg_r), "deleted"
        elif dep_names:
            return ("Which deployment would you like to delete?\n\nAvailable:\n"
                    + "\n".join("- " + n for n in dep_names)), "list_for_delete"
        else:
            return "There are no deployments to delete.", "no_deps"

    # ── List / Status ──
    elif is_list:
        pods = k8s_get_pods()
        deps = k8s_get_deployments()
        if "pod" in msg_lower:
            if not pods:
                return "No pods are currently running.", "listed"
            lines = ["Currently running **" + str(len(pods)) + " pods**:\n"]
            for p in pods:
                icon = "🟢" if p["phase"] == "Running" else "🟡" if p["phase"] == "Pending" else "🔴"
                lines.append(icon + " " + p["name"] + " (" + p["app"] + ") — " + p["phase"] + " — " + p["ip"])
            return "\n".join(lines), "listed"
        else:
            if not deps:
                return "No deployments found.", "listed"
            lines = ["Currently **" + str(len(deps)) + " active deployments**:\n"]
            for d in deps:
                lines.append("📦 " + d["name"] + " — " + d["image"] + " — " + str(d["ready"]) + "/" + str(d["replicas"]) + " ready")
            return "\n".join(lines), "listed"

    # ── Scale ──
    elif is_scale:
        import re as _re
        nums = _re.findall(r"\d+", message)
        deps = k8s_get_deployments()
        dep_names = [d["name"] for d in deps]
        target = None
        for name in dep_names:
            if name.lower() in msg_lower:
                target = name
                break
        if target and nums:
            new_replicas = int(nums[-1])
            try:
                k8s_client.AppsV1Api().patch_namespaced_deployment(
                    target, NS, {"spec": {"replicas": new_replicas}})
                return "Scaled **" + target + "** to **" + str(new_replicas) + "** replicas!", "scaled"
            except Exception as e:
                return "Scale failed: " + str(e), "error"
        elif not target:
            return ("Which deployment to scale? Available:\n"
                    + "\n".join("- " + n for n in dep_names if dep_names)
                    + ("\nNo deployments found." if not dep_names else "")), "list_for_scale"
        else:
            return "How many replicas? e.g. scale web-frontend to 5", "need_count"

    # ── Knowledge base ──
    else:
        kb = {
            ("kubernetes","what is k8s","k8s是什麼","什麼是kubernetes"):
                "**Kubernetes (K8s)** is an open-source container orchestration platform that automates deployment, scaling, and management of containerized applications.\n\nIn this system, you can deploy services by just typing natural language — I'll handle the rest!",
            ("what is a pod","pod是什麼","什麼是pod","pod"):
                "**A Pod** is the smallest deployable unit in Kubernetes. It wraps one or more containers that share the same network and storage.\n\nEach pod gets its own IP address and can communicate with other pods in the cluster.",
            ("what is deployment","deployment是什麼","什麼是deployment"):
                "**A Deployment** manages a set of identical pods and ensures the desired number are always running. If a pod crashes, the Deployment automatically creates a new one to replace it.",
            ("what is service","service是什麼","什麼是service"):
                "**A Service** provides a stable network endpoint for pods. It handles load balancing, distributing traffic evenly across all pod replicas.",
            ("what is lora","lora是什麼","什麼是lora"):
                "**LoRA (Low-Rank Adaptation)** is an efficient fine-tuning technique. Instead of retraining the entire model, it trains a small set of additional parameters.\n\nThis system uses LLaMA-3.1-8B + LoRA, fine-tuned on 800 K8s deployment examples to understand your natural language commands.",
            ("how","help","usage","使用","怎麼用","如何"):
                "**How to use ZeroTouch K8s:**\n\n📦 **Deploy a service:**\ndeploy 3 nginx:latest pods for web-frontend\n\n🗑️ **Delete a deployment:**\ndelete web-frontend-1234\n\n📋 **Check status:**\nlist pods / show deployments\n\n🔧 **Scale:**\nscale web-frontend to 5\n\n💬 **Ask anything about K8s** — I'm here to help!",
            ("yaml","manifest"):
                "YAML manifests define Kubernetes resources. This system auto-generates them for you — just describe what you want in natural language and I'll create the correct Deployment + Service YAML.",
            ("node","worker","control plane"):
                "This cluster has 2 nodes:\n- **desktop-control-plane** — manages the cluster\n- **desktop-worker** — runs the actual workloads (pods)",
        }
        reply = None
        for keys, answer in kb.items():
            if isinstance(keys, str): keys = (keys,)
            if any(k in msg_lower for k in keys):
                reply = answer
                break
        if not reply:
            pods = k8s_get_pods()
            deps = k8s_get_deployments()
            reply = ("I'm your **ZeroTouch K8s assistant**! "
                     "System status: **" + str(len(pods)) + " pods** running, **" + str(len(deps)) + " deployments** active.\n\n"
                     "I can help you:\n"
                     "- 📦 Deploy services (just describe what you want)\n"
                     "- 🗑️ Delete deployments\n"
                     "- 📋 Check pod/deployment status\n"
                     "- 🔧 Scale deployments\n"
                     "- 💬 Answer K8s questions\n\n"
                     "What would you like to do?")
        return reply, "answered"


# ── HTML Template ────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ZeroTouch K8s</title>
<link href="https://fonts.googleapis.com/css2?family=Geist:wght@300;400;500;600&family=Geist+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{
  --bg:#F9FAFB;--surface:#FFFFFF;--border:#E5E7EB;--border2:#D1D5DB;
  --text:#111827;--text2:#6B7280;--text3:#9CA3AF;
  --green:#16A34A;--green-l:#DCFCE7;--green-m:#BBF7D0;
  --red:#DC2626;--red-l:#FEE2E2;
  --blue:#2563EB;--blue-l:#DBEAFE;
  --yellow:#D97706;--yellow-l:#FEF3C7;
  --sidebar:220px;
  --radius:10px;--radius-sm:6px;
  --shadow:0 1px 3px rgba(0,0,0,.08);
  --shadow-md:0 4px 16px rgba(0,0,0,.08);
}
body{font-family:'Geist',sans-serif;background:var(--bg);color:var(--text);height:100vh;overflow:hidden}

/* ── Auth ── */
.auth{min-height:100vh;display:flex;align-items:center;justify-content:center;background:var(--bg)}
.auth-card{background:var(--surface);border:1px solid var(--border);border-radius:16px;padding:40px;width:420px;box-shadow:var(--shadow-md)}
.auth-logo{display:flex;align-items:center;gap:10px;margin-bottom:28px}
.auth-logo-icon{width:34px;height:34px;background:var(--green);border-radius:8px;display:flex;align-items:center;justify-content:center;color:#fff;font-size:18px;font-weight:700}
.auth-logo-text{font-size:16px;font-weight:600}
.auth-title{font-size:22px;font-weight:600;margin-bottom:6px}
.auth-sub{font-size:14px;color:var(--text2);margin-bottom:24px}
.form-group{margin-bottom:16px}
.form-group label{display:block;font-size:13px;font-weight:500;margin-bottom:6px}
.form-group input{width:100%;padding:10px 14px;border:1.5px solid var(--border2);border-radius:var(--radius-sm);font-size:14px;font-family:inherit;outline:none;transition:border .15s;background:var(--surface)}
.form-group input:focus{border-color:var(--green);box-shadow:0 0 0 3px rgba(22,163,74,.1)}
.btn-primary{width:100%;padding:11px;background:var(--green);color:#fff;border:none;border-radius:var(--radius-sm);font-size:14px;font-weight:500;cursor:pointer;font-family:inherit}
.btn-primary:hover{background:#15803D}
.auth-link{text-align:center;margin-top:20px;font-size:13px;color:var(--text2)}
.auth-link a{color:var(--green);text-decoration:none;font-weight:500}
.auth-error{background:var(--red-l);color:var(--red);padding:10px 14px;border-radius:var(--radius-sm);font-size:13px;margin-bottom:16px}

/* ── Layout ── */
.layout{display:flex;height:100vh}

/* ── Sidebar ── */
.sidebar{width:var(--sidebar);background:var(--surface);border-right:1px solid var(--border);display:flex;flex-direction:column;flex-shrink:0}
.sidebar-head{padding:16px 14px 12px;border-bottom:1px solid var(--border)}
.sidebar-brand{display:flex;align-items:center;gap:9px;margin-bottom:14px}
.brand-icon{width:28px;height:28px;background:var(--green);border-radius:7px;display:flex;align-items:center;justify-content:center;color:#fff;font-size:15px;font-weight:700;flex-shrink:0}
.brand-text{font-size:14px;font-weight:600}
.new-chat-btn{width:100%;padding:8px 12px;background:var(--green);color:#fff;border:none;border-radius:var(--radius-sm);font-size:13px;font-weight:500;cursor:pointer;font-family:inherit;display:flex;align-items:center;gap:7px;justify-content:center}
.new-chat-btn:hover{background:#15803D}
.sidebar-nav{padding:10px 10px 0;flex:1;overflow-y:auto}
.nav-section-title{font-size:11px;color:var(--text3);font-weight:600;letter-spacing:.5px;text-transform:uppercase;padding:8px 6px 4px}
.nav-item{display:flex;align-items:center;gap:8px;padding:8px 8px;border-radius:var(--radius-sm);cursor:pointer;font-size:13px;color:var(--text2);transition:all .15s;border:none;background:none;width:100%;text-align:left;position:relative}
.nav-item:hover{background:var(--bg);color:var(--text)}
.nav-item.active{background:var(--green-l);color:var(--green)}
.nav-item svg{width:15px;height:15px;flex-shrink:0}
.nav-item-text{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.nav-item-del{opacity:0;padding:2px 4px;border-radius:4px;font-size:11px;color:var(--text3);cursor:pointer;border:none;background:none;flex-shrink:0}
.nav-item:hover .nav-item-del{opacity:1}
.nav-item-del:hover{color:var(--red);background:var(--red-l)}
.sidebar-divider{height:1px;background:var(--border);margin:8px 10px}
.sidebar-footer{padding:12px 14px;border-top:1px solid var(--border)}
.user-row{display:flex;align-items:center;gap:9px}
.user-avatar{width:30px;height:30px;background:var(--green);border-radius:50%;display:flex;align-items:center;justify-content:center;color:#fff;font-size:12px;font-weight:600;flex-shrink:0}
.user-name{font-size:13px;font-weight:500;flex:1}
.logout-btn{font-size:12px;color:var(--text3);cursor:pointer;border:none;background:none;font-family:inherit;padding:3px 8px;border-radius:4px}
.logout-btn:hover{color:var(--red);background:var(--red-l)}

/* ── Status bar ── */
.status-bar{padding:8px 24px;background:var(--surface);border-bottom:1px solid var(--border);display:flex;align-items:center;gap:18px;flex-shrink:0}
.status-pill{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--text2)}
.dot{width:7px;height:7px;border-radius:50%;background:var(--text3)}
.dot.on{background:var(--green)}
.dot.off{background:var(--red)}
.dot.pulse{background:var(--yellow);animation:pulse 1.5s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
.clock{margin-left:auto;font-size:12px;color:var(--text3);font-family:'Geist Mono',monospace}

/* ── Main ── */
.main{flex:1;display:flex;flex-direction:column;overflow:hidden}

/* ── Chat ── */
.chat-area{flex:1;display:flex;flex-direction:column;overflow:hidden}
.chat-messages{flex:1;overflow-y:auto;padding:24px;display:flex;flex-direction:column;gap:16px}
.msg{display:flex;gap:10px;max-width:760px}
.msg.user{flex-direction:row-reverse;align-self:flex-end}
.msg.ai{align-self:flex-start}
.msg-avatar{width:32px;height:32px;border-radius:50%;flex-shrink:0;display:flex;align-items:center;justify-content:center;font-size:13px;font-weight:600}
.msg.ai .msg-avatar{background:var(--green-l);color:var(--green)}
.msg.user .msg-avatar{background:var(--green);color:#fff}
.msg-content{max-width:640px}
.msg-bubble{padding:12px 16px;border-radius:14px;font-size:14px;line-height:1.65}
.msg.ai .msg-bubble{background:var(--surface);border:1px solid var(--border);border-radius:4px 14px 14px 14px}
.msg.user .msg-bubble{background:var(--green);color:#fff;border-radius:14px 4px 14px 14px}
.msg-time{font-size:11px;color:var(--text3);margin-top:4px;padding:0 4px}
.msg.user .msg-time{text-align:right}

/* Markdown-ish */
.msg-bubble b,.msg-bubble strong{font-weight:600}
.msg-bubble code{font-family:'Geist Mono',monospace;font-size:12px;background:rgba(0,0,0,.06);padding:1px 5px;border-radius:4px}
.msg.user .msg-bubble code{background:rgba(255,255,255,.2)}

/* Confirm button */
.confirm-btns{display:flex;gap:8px;margin-top:10px}
.confirm-yes{padding:7px 18px;background:var(--green);color:#fff;border:none;border-radius:var(--radius-sm);font-size:13px;font-weight:500;cursor:pointer;font-family:inherit}
.confirm-yes:hover{background:#15803D}
.confirm-no{padding:7px 18px;background:var(--surface);color:var(--text2);border:1px solid var(--border2);border-radius:var(--radius-sm);font-size:13px;cursor:pointer;font-family:inherit}
.confirm-no:hover{background:var(--bg)}

/* Typing indicator */
.typing{display:flex;gap:4px;padding:12px 16px;background:var(--surface);border:1px solid var(--border);border-radius:4px 14px 14px 14px;width:fit-content}
.typing span{width:6px;height:6px;background:var(--text3);border-radius:50%;animation:bounce .9s infinite}
.typing span:nth-child(2){animation-delay:.15s}
.typing span:nth-child(3){animation-delay:.3s}
@keyframes bounce{0%,100%{transform:translateY(0)}50%{transform:translateY(-5px)}}

/* Welcome screen */
.welcome{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:40px;text-align:center}
.welcome-icon{width:56px;height:56px;background:var(--green);border-radius:14px;display:flex;align-items:center;justify-content:center;color:#fff;font-size:28px;font-weight:700;margin:0 auto 16px}
.welcome-title{font-size:22px;font-weight:600;margin-bottom:8px}
.welcome-sub{font-size:14px;color:var(--text2);margin-bottom:32px;max-width:440px}
.quick-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;max-width:480px}
.quick-card{padding:14px 16px;background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);cursor:pointer;text-align:left;transition:all .15s;font-family:inherit}
.quick-card:hover{border-color:var(--green);background:var(--green-l)}
.quick-card-title{font-size:13px;font-weight:500;margin-bottom:3px}
.quick-card-sub{font-size:12px;color:var(--text2)}

/* Input area */
.input-area{padding:16px 24px 20px;border-top:1px solid var(--border);background:var(--surface);flex-shrink:0}
.input-wrap{display:flex;gap:10px;align-items:flex-end;background:var(--bg);border:1.5px solid var(--border2);border-radius:12px;padding:10px 14px;transition:border .15s}
.input-wrap:focus-within{border-color:var(--green);box-shadow:0 0 0 3px rgba(22,163,74,.1)}
.chat-textarea{flex:1;border:none;background:none;resize:none;font-size:14px;font-family:inherit;outline:none;max-height:160px;line-height:1.5;color:var(--text)}
.chat-textarea::placeholder{color:var(--text3)}
.send-btn{padding:8px 16px;background:var(--green);color:#fff;border:none;border-radius:8px;font-size:13px;font-weight:500;cursor:pointer;font-family:inherit;align-self:flex-end;flex-shrink:0}
.send-btn:hover{background:#15803D}
.send-btn:disabled{background:var(--text3);cursor:not-allowed}
.input-hint{font-size:11px;color:var(--text3);margin-top:6px;text-align:center}

/* Pods sidebar panel */
.pods-panel{display:none;width:360px;border-left:1px solid var(--border);background:var(--surface);flex-direction:column;flex-shrink:0}
.pods-panel.open{display:flex}
.panel-head{padding:16px 18px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between}
.panel-title{font-size:14px;font-weight:600}
.panel-close{width:28px;height:28px;border-radius:6px;border:none;background:var(--bg);cursor:pointer;display:flex;align-items:center;justify-content:center;color:var(--text2);font-size:14px}
.panel-tabs{display:flex;border-bottom:1px solid var(--border);padding:0 18px}
.panel-tab{padding:10px 14px;font-size:13px;font-weight:500;cursor:pointer;border-bottom:2px solid transparent;color:var(--text2);transition:all .15s}
.panel-tab.active{color:var(--green);border-bottom-color:var(--green)}
.panel-content{flex:1;overflow-y:auto;padding:12px}
.pod-card{padding:12px 14px;border:1px solid var(--border);border-radius:var(--radius-sm);margin-bottom:8px;cursor:pointer;transition:all .15s}
.pod-card:hover{border-color:var(--green);background:var(--green-l)}
.pod-name{font-size:12px;font-family:'Geist Mono',monospace;font-weight:500;margin-bottom:4px}
.pod-meta{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.pod-badge{padding:2px 8px;border-radius:20px;font-size:11px;font-weight:500}
.pod-badge.Running{background:var(--green-l);color:var(--green)}
.pod-badge.Pending{background:var(--yellow-l);color:var(--yellow)}
.pod-badge.Failed,.pod-badge.Error{background:var(--red-l);color:var(--red)}
.pod-badge.Unknown{background:var(--bg);color:var(--text2);border:1px solid var(--border)}
.pod-ip{font-size:11px;color:var(--text3);font-family:'Geist Mono',monospace}
.dep-card{padding:12px 14px;border:1px solid var(--border);border-radius:var(--radius-sm);margin-bottom:8px}
.dep-name{font-size:13px;font-weight:500;margin-bottom:4px}
.dep-meta{font-size:12px;color:var(--text2)}
.dep-del{margin-top:8px;padding:4px 10px;font-size:11px;border:1px solid #FECACA;color:var(--red);background:none;border-radius:4px;cursor:pointer;font-family:inherit}
.dep-del:hover{background:var(--red-l)}
.empty-state{text-align:center;padding:32px;color:var(--text3);font-size:13px}

/* Pod detail modal */
.modal-bg{position:fixed;inset:0;background:rgba(0,0,0,.25);z-index:1000;display:none;align-items:center;justify-content:center}
.modal-bg.open{display:flex}
.modal{background:var(--surface);border-radius:14px;width:560px;max-height:80vh;overflow-y:auto;box-shadow:0 20px 40px rgba(0,0,0,.15)}
.modal-head{padding:18px 22px 14px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;background:var(--surface)}
.modal-title{font-size:15px;font-weight:600}
.modal-close{width:28px;height:28px;border-radius:6px;border:none;background:var(--bg);cursor:pointer;font-size:14px;color:var(--text2)}
.modal-body{padding:18px 22px}
.detail-section{margin-bottom:18px}
.detail-section-title{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.5px;color:var(--text3);margin-bottom:8px}
.detail-row{display:flex;justify-content:space-between;padding:7px 0;border-bottom:1px solid var(--border);font-size:13px}
.detail-row:last-child{border-bottom:none}
.detail-key{color:var(--text2)}
.detail-val{font-family:'Geist Mono',monospace;font-size:12px;max-width:280px;word-break:break-all;text-align:right}
.cond-chip{padding:2px 8px;border-radius:4px;font-size:11px;font-weight:500;margin:2px}
.cond-true{background:var(--green-l);color:var(--green)}
.cond-false{background:var(--red-l);color:var(--red)}
</style>
</head>
<body>

{% if not logged_in and page != 'register' %}
<div class="auth">
  <div class="auth-card">
    <div class="auth-logo">
      <div class="auth-logo-icon">K</div>
      <span class="auth-logo-text">ZeroTouch K8s</span>
    </div>
    <div class="auth-title">Welcome back</div>
    <div class="auth-sub">Sign in to your account to continue</div>
    {% if error %}<div class="auth-error">{{ error }}</div>{% endif %}
    <form method="POST" action="/auth/login">
      <div class="form-group"><label>Username</label><input type="text" name="username" placeholder="Enter your username" required autofocus></div>
      <div class="form-group"><label>Password</label><input type="password" name="password" placeholder="Enter your password" required></div>
      <button class="btn-primary" type="submit">Sign In</button>
    </form>
    <div class="auth-link">Don't have an account? <a href="/auth/register">Register</a></div>
  </div>
</div>

{% elif not logged_in and page == 'register' %}
<div class="auth">
  <div class="auth-card">
    <div class="auth-logo">
      <div class="auth-logo-icon">K</div>
      <span class="auth-logo-text">ZeroTouch K8s</span>
    </div>
    <div class="auth-title">Create account</div>
    <div class="auth-sub">Get started with ZeroTouch K8s</div>
    {% if error %}<div class="auth-error">{{ error }}</div>{% endif %}
    <form method="POST" action="/auth/register">
      <div class="form-group"><label>Username</label><input type="text" name="username" placeholder="Choose a username" required autofocus></div>
      <div class="form-group"><label>Password</label><input type="password" name="password" placeholder="Create a password" required></div>
      <div class="form-group"><label>Confirm Password</label><input type="password" name="confirm" placeholder="Confirm your password" required></div>
      <button class="btn-primary" type="submit">Create Account</button>
    </form>
    <div class="auth-link">Already have an account? <a href="/">Sign In</a></div>
  </div>
</div>

{% else %}
<div class="layout">
  <!-- Sidebar -->
  <aside class="sidebar">
    <div class="sidebar-head">
      <div class="sidebar-brand">
        <div class="brand-icon">K</div>
        <span class="brand-text">ZeroTouch K8s</span>
      </div>
      <button class="new-chat-btn" onclick="newChat()">
        <svg width="13" height="13" viewBox="0 0 13 13" fill="none"><path d="M6.5 1v11M1 6.5h11" stroke="#fff" stroke-width="1.8" stroke-linecap="round"/></svg>
        New Chat
      </button>
    </div>
    <div class="sidebar-nav">
      <div class="nav-section-title">Chats</div>
      <div id="chat-list"></div>
      <div class="sidebar-divider"></div>
      <div class="nav-section-title">System</div>
      <button class="nav-item" onclick="togglePanel('pods')">
        <svg viewBox="0 0 16 16" fill="none"><circle cx="8" cy="8" r="5" stroke="currentColor" stroke-width="1.5"/><circle cx="8" cy="8" r="2" fill="currentColor"/></svg>
        <span class="nav-item-text">Pods</span>
      </button>
      <button class="nav-item" onclick="togglePanel('deployments')">
        <svg viewBox="0 0 16 16" fill="none"><path d="M2 4h12M2 8h12M2 12h8" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>
        <span class="nav-item-text">Deployments</span>
      </button>
    </div>
    <div class="sidebar-footer">
      <div class="user-row">
        <div class="user-avatar">{{ username[0].upper() }}</div>
        <div class="user-name">{{ username }}</div>
        <form method="POST" action="/auth/logout" style="margin:0">
          <button class="logout-btn" type="submit">Out</button>
        </form>
      </div>
    </div>
  </aside>

  <!-- Main -->
  <div class="main">
    <div class="status-bar">
      <div class="status-pill"><div class="dot" id="mdot"></div><span id="mstatus">Checking model...</span></div>
      <div class="status-pill"><div class="dot {% if k8s %}on{% else %}off{% endif %}"></div><span>K8s {% if k8s %}Connected{% else %}Simulation{% endif %}</span></div>
      <div class="clock" id="clock"></div>
    </div>
    <div style="display:flex;flex:1;overflow:hidden">
      <div class="chat-area" id="chat-area">
        <!-- Welcome screen or chat messages will be injected here -->
      </div>
      <!-- Pods/Deployments panel -->
      <div class="pods-panel" id="pods-panel">
        <div class="panel-head">
          <div class="panel-title" id="panel-title">Pods</div>
          <button class="panel-close" onclick="togglePanel(null)">✕</button>
        </div>
        <div class="panel-tabs">
          <div class="panel-tab active" id="tab-pods" onclick="switchTab('pods')">Pods</div>
          <div class="panel-tab" id="tab-deps" onclick="switchTab('deployments')">Deployments</div>
        </div>
        <div class="panel-content" id="panel-content">Loading...</div>
      </div>
    </div>
  </div>
</div>

<!-- Pod detail modal -->
<div class="modal-bg" id="pod-modal">
  <div class="modal">
    <div class="modal-head">
      <div class="modal-title" id="modal-title">Pod Details</div>
      <button class="modal-close" onclick="closeModal()">✕</button>
    </div>
    <div class="modal-body" id="modal-body"></div>
  </div>
</div>

<script>
const USERNAME = "{{ username }}";
let currentChatId = null;
let panelMode = null;

// ── Clock ──
setInterval(() => {
  const el = document.getElementById('clock');
  if(el) el.textContent = new Date().toLocaleTimeString('en-GB');
}, 1000);

// ── Model status ──
async function pollStatus(){
  try {
    const r = await fetch('/api/status');
    const d = await r.json();
    const dot = document.getElementById('mdot');
    const txt = document.getElementById('mstatus');
    if(d.model_ready){ dot.className='dot on'; txt.textContent='Model Ready'; }
    else { dot.className='dot pulse'; txt.textContent='Model Loading...'; }
  } catch(e){}
}
pollStatus(); setInterval(pollStatus, 5000);

// ── Chat management ──
let chats = JSON.parse(localStorage.getItem('chats_' + USERNAME) || '{}');

function saveChats(){ localStorage.setItem('chats_' + USERNAME, JSON.stringify(chats)); }

function renderChatList(){
  const el = document.getElementById('chat-list');
  const sorted = Object.entries(chats).sort((a,b) => (b[1].created||'') > (a[1].created||'') ? 1 : -1);
  if(!sorted.length){
    el.innerHTML = '<div style="font-size:12px;color:var(--text3);padding:6px 8px">No chats yet</div>';
    return;
  }
  el.innerHTML = sorted.map(([id, chat]) =>
    `<button class="nav-item ${id===currentChatId?'active':''}" onclick="loadChat('${id}')">
      <svg viewBox="0 0 16 16" fill="none"><path d="M2 3a1 1 0 011-1h10a1 1 0 011 1v7a1 1 0 01-1 1H9l-3 2v-2H3a1 1 0 01-1-1V3z" stroke="currentColor" stroke-width="1.5"/></svg>
      <span class="nav-item-text">${chat.title || 'New Chat'}</span>
      <button class="nav-item-del" onclick="event.stopPropagation();deleteChat('${id}')">✕</button>
    </button>`
  ).join('');
}

function newChat(){
  const id = 'chat_' + Date.now();
  chats[id] = {title: 'New Chat', messages: [], created: new Date().toISOString()};
  saveChats();
  loadChat(id);
}

function loadChat(id){
  currentChatId = id;
  renderChatList();
  renderChatArea();
}

function deleteChat(id){
  if(!confirm('Delete this chat?')) return;
  delete chats[id];
  saveChats();
  if(currentChatId === id){
    currentChatId = null;
    showWelcome();
  }
  renderChatList();
}

function showWelcome(){
  document.getElementById('chat-area').innerHTML = `
    <div class="welcome">
      <div class="welcome-icon">K</div>
      <div class="welcome-title">ZeroTouch K8s Assistant</div>
      <div class="welcome-sub">Deploy and manage Kubernetes services using natural language. Ask me anything or start with a quick action below.</div>
      <div class="quick-grid">
        <div class="quick-card" onclick="startWithPrompt('deploy 3 nginx:latest pods for web-frontend')">
          <div class="quick-card-title">Deploy a service</div>
          <div class="quick-card-sub">deploy 3 nginx:latest pods...</div>
        </div>
        <div class="quick-card" onclick="startWithPrompt('list pods')">
          <div class="quick-card-title">Check pod status</div>
          <div class="quick-card-sub">list pods / show deployments</div>
        </div>
        <div class="quick-card" onclick="startWithPrompt('what is Kubernetes?')">
          <div class="quick-card-title">Learn K8s concepts</div>
          <div class="quick-card-sub">what is a pod, deployment...</div>
        </div>
        <div class="quick-card" onclick="startWithPrompt('delete ')">
          <div class="quick-card-title">Delete a deployment</div>
          <div class="quick-card-sub">delete &lt;deployment-name&gt;</div>
        </div>
      </div>
    </div>
    <div style="flex-shrink:0">
      <div class="input-area">
        <div class="input-wrap">
          <textarea class="chat-textarea" id="chat-input" placeholder="Message ZeroTouch K8s..." rows="1"
            onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();sendMsg()}"
            oninput="autoResize(this)"></textarea>
          <button class="send-btn" onclick="sendMsg()">Send</button>
        </div>
        <div class="input-hint">Press Enter to send · Shift+Enter for new line</div>
      </div>
    </div>`;
}

function startWithPrompt(text){
  if(!currentChatId) newChat();
  const inp = document.getElementById('chat-input');
  if(inp){ inp.value = text; inp.focus(); }
}

function renderChatArea(){
  if(!currentChatId){ showWelcome(); return; }
  const chat = chats[currentChatId];
  const msgs = chat.messages || [];
  document.getElementById('chat-area').innerHTML = `
    <div class="chat-messages" id="msg-list">
      ${msgs.map(m => renderMsg(m)).join('')}
    </div>
    <div style="flex-shrink:0">
      <div class="input-area">
        <div class="input-wrap">
          <textarea class="chat-textarea" id="chat-input" placeholder="Message ZeroTouch K8s..." rows="1"
            onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();sendMsg()}"
            oninput="autoResize(this)"></textarea>
          <button class="send-btn" onclick="sendMsg()">Send</button>
        </div>
        <div class="input-hint">Press Enter to send · Shift+Enter for new line</div>
      </div>
    </div>`;
  scrollBottom();
}

function renderMsg(m){
  const cls = m.role === 'user' ? 'user' : 'ai';
  const av  = m.role === 'user' ? USERNAME[0].toUpperCase() : 'K';
  const text = formatText(m.content);
  const confirmBtns = m.action === 'confirm_pending' ? `
    <div class="confirm-btns">
      <button class="confirm-yes" onclick="sendQuick('yes')">Deploy</button>
      <button class="confirm-no"  onclick="sendQuick('no')">Cancel</button>
    </div>` : '';
  return `<div class="msg ${cls}">
    <div class="msg-avatar">${av}</div>
    <div class="msg-content">
      <div class="msg-bubble">${text}${confirmBtns}</div>
      <div class="msg-time">${m.time || ''}</div>
    </div>
  </div>`;
}

function formatText(text){
  return text
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/`(.+?)`/g, '<code>$1</code>')
    .replace(/\n/g, '<br>');
}

function autoResize(el){
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 160) + 'px';
}

function scrollBottom(){
  const el = document.getElementById('msg-list');
  if(el) el.scrollTop = el.scrollHeight;
}

async function sendMsg(){
  const inp = document.getElementById('chat-input');
  if(!inp) return;
  const text = inp.value.trim();
  if(!text) return;
  inp.value = ''; inp.style.height = 'auto';

  if(!currentChatId) newChat();
  const chat = chats[currentChatId];
  const time = new Date().toLocaleTimeString('en-GB', {hour:'2-digit',minute:'2-digit'});

  chat.messages.push({role:'user', content:text, time});
  if(chat.title === 'New Chat') chat.title = text.slice(0,28);
  saveChats();
  renderChatArea();

  // typing indicator
  const msgList = document.getElementById('msg-list');
  const typing = document.createElement('div');
  typing.className = 'msg ai'; typing.id = 'typing-indicator';
  typing.innerHTML = '<div class="msg-avatar">K</div><div class="msg-content"><div class="typing"><span></span><span></span><span></span></div></div>';
  if(msgList){ msgList.appendChild(typing); scrollBottom(); }

  try {
    const r = await fetch('/api/chat', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({message: text, chat_id: currentChatId})
    });
    const d = await r.json();
    document.getElementById('typing-indicator')?.remove();
    const reply = d.reply || 'No response';
    const action = d.action || '';
    chat.messages.push({role:'assistant', content:reply, time: new Date().toLocaleTimeString('en-GB',{hour:'2-digit',minute:'2-digit'}), action});
    saveChats();
    renderChatArea();
  } catch(e){
    document.getElementById('typing-indicator')?.remove();
    const chat2 = chats[currentChatId];
    chat2.messages.push({role:'assistant', content:'Connection error. Please try again.', time:''});
    saveChats();
    renderChatArea();
  }
}

function sendQuick(text){
  const inp = document.getElementById('chat-input');
  if(inp){ inp.value = text; sendMsg(); }
}

// ── Pods panel ──
function togglePanel(mode){
  const panel = document.getElementById('pods-panel');
  if(panelMode === mode || !mode){ panel.classList.remove('open'); panelMode=null; return; }
  panelMode = mode;
  panel.classList.add('open');
  switchTab(mode);
}

function switchTab(tab){
  panelMode = tab;
  document.getElementById('tab-pods').classList.toggle('active', tab==='pods');
  document.getElementById('tab-deps').classList.toggle('active', tab==='deployments');
  document.getElementById('panel-title').textContent = tab==='pods' ? 'Pods' : 'Deployments';
  if(tab==='pods') loadPanelPods();
  else loadPanelDeps();
}

async function loadPanelPods(){
  const el = document.getElementById('panel-content');
  el.innerHTML = '<div class="empty-state">Loading...</div>';
  try {
    const r = await fetch('/api/pods');
    const d = await r.json();
    if(!d.pods.length){ el.innerHTML='<div class="empty-state">No pods found</div>'; return; }
    el.innerHTML = d.pods.map(p => `
      <div class="pod-card" onclick='showPodDetail(${JSON.stringify(JSON.stringify(p))})'>
        <div class="pod-name">${p.name}</div>
        <div class="pod-meta">
          <span class="pod-badge ${p.phase}">${p.phase}</span>
          <span class="pod-ip">${p.ip}</span>
          <span class="pod-ip">${p.age}</span>
        </div>
      </div>`).join('');
  } catch(e){ el.innerHTML='<div class="empty-state">Error loading pods</div>'; }
}

async function loadPanelDeps(){
  const el = document.getElementById('panel-content');
  el.innerHTML = '<div class="empty-state">Loading...</div>';
  try {
    const r = await fetch('/api/deployments');
    const d = await r.json();
    if(!d.deployments.length){ el.innerHTML='<div class="empty-state">No deployments found</div>'; return; }
    el.innerHTML = d.deployments.map(dep => `
      <div class="dep-card">
        <div class="dep-name">${dep.name}</div>
        <div class="dep-meta">${dep.image} · ${dep.ready}/${dep.replicas} ready · ${dep.age}</div>
        <button class="dep-del" onclick="deleteDep('${dep.name}')">Delete</button>
      </div>`).join('');
  } catch(e){ el.innerHTML='<div class="empty-state">Error loading deployments</div>'; }
}

async function deleteDep(name){
  if(!confirm('Delete ' + name + '?')) return;
  const r = await fetch('/api/delete', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name})});
  const d = await r.json();
  if(d.success) loadPanelDeps();
  else alert('Error: ' + d.error);
}

function showPodDetail(jsonStr){
  const p = JSON.parse(jsonStr);
  document.getElementById('modal-title').textContent = p.name;
  const sc = p.phase==='Running'?'var(--green)':p.phase==='Pending'?'var(--yellow)':'var(--red)';
  let cHtml = '';
  (p.containers||[]).forEach(c => {
    const req = Object.entries(c.resources.requests||{}).map(([k,v])=>k+': '+v).join(', ')||'—';
    const lim = Object.entries(c.resources.limits||{}).map(([k,v])=>k+': '+v).join(', ')||'—';
    cHtml += `<div style="background:var(--bg);border-radius:8px;padding:10px 12px;margin-bottom:8px">
      <div style="font-weight:600;font-size:13px;margin-bottom:6px">${c.name}</div>
      <div class="detail-row"><span class="detail-key">Image</span><span class="detail-val">${c.image}</span></div>
      <div class="detail-row"><span class="detail-key">Ports</span><span class="detail-val">${c.ports.join(', ')||'—'}</span></div>
      <div class="detail-row"><span class="detail-key">Requests</span><span class="detail-val">${req}</span></div>
      <div class="detail-row"><span class="detail-key">Limits</span><span class="detail-val">${lim}</span></div>
    </div>`;
  });
  let condHtml = (p.conditions||[]).map(c =>
    `<span class="cond-chip ${c.status==='True'?'cond-true':'cond-false'}">${c.type}: ${c.status}</span>`).join('')||'—';
  document.getElementById('modal-body').innerHTML = `
    <div class="detail-section">
      <div class="detail-section-title">General</div>
      <div class="detail-row"><span class="detail-key">Name</span><span class="detail-val">${p.name}</span></div>
      <div class="detail-row"><span class="detail-key">App</span><span class="detail-val">${p.app||'—'}</span></div>
      <div class="detail-row"><span class="detail-key">Status</span><span class="detail-val" style="color:${sc};font-weight:600">${p.phase}</span></div>
      <div class="detail-row"><span class="detail-key">IP</span><span class="detail-val">${p.ip||'—'}</span></div>
      <div class="detail-row"><span class="detail-key">Node</span><span class="detail-val">${p.node||'—'}</span></div>
      <div class="detail-row"><span class="detail-key">Restarts</span><span class="detail-val">${p.restarts}</span></div>
      <div class="detail-row"><span class="detail-key">Age</span><span class="detail-val">${p.age}</span></div>
    </div>
    <div class="detail-section"><div class="detail-section-title">Containers</div>${cHtml}</div>
    <div class="detail-section"><div class="detail-section-title">Conditions</div><div style="display:flex;flex-wrap:wrap">${condHtml}</div></div>`;
  document.getElementById('pod-modal').classList.add('open');
}
function closeModal(){ document.getElementById('pod-modal').classList.remove('open'); }
document.getElementById('pod-modal')?.addEventListener('click', e=>{ if(e.target.id==='pod-modal') closeModal(); });

// Init
renderChatList();
if(Object.keys(chats).length > 0){
  const lastId = Object.entries(chats).sort((a,b)=>(b[1].created||'')>(a[1].created||'')?1:-1)[0][0];
  loadChat(lastId);
} else {
  showWelcome();
}
</script>
{% endif %}
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
    USERS[username] = {"password_hash": hash_pw(password), "created_at": datetime.now().isoformat()}
    _save_users(USERS)
    session["username"] = username
    return redirect("/")

@app.route("/auth/login", methods=["POST"])
def login():
    username = request.form.get("username","").strip()
    password = request.form.get("password","")
    if username in USERS and USERS[username]["password_hash"] == hash_pw(password):
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
    return jsonify({"model_ready": ready, "k8s": K8S_ENABLED})

@app.route("/api/chat", methods=["POST"])
def api_chat():
    if "username" not in session:
        return jsonify({"error": "Not authenticated"}), 401
    data     = request.get_json() or {}
    message  = data.get("message","").strip()
    chat_id  = data.get("chat_id","default")
    if not message:
        return jsonify({"error": "Empty message"}), 400
    username = session["username"]
    reply, action = process_message(username, chat_id, message, [])
    return jsonify({"reply": reply, "action": action})

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
    return jsonify({"success": ok, "error": None if ok else msg})

if __name__ == "__main__":
    print("=" * 55)
    print("  ZeroTouch K8s  v3")
    print(f"  K8s   : {'Connected' if K8S_ENABLED else 'Simulation'}")
    print("  Open  : http://localhost:5000")
    print("=" * 55)
    app.run(host="0.0.0.0", port=5000, debug=False)
