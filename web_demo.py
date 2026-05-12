"""
web_demo.py  v4  —  ZeroTouch K8s Platform
Multi-chat + Conversational AI (LLaMA) + Real K8s + Edit form
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core.config import ensure_utf8_output
ensure_utf8_output()

import threading, json, urllib.request, hashlib, secrets, time
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
    print(f"[K8s] Not connected: {e}")

NS = "default"
app = Flask(__name__)
app.secret_key = secrets.token_hex(32)

# ── User store ───────────────────────────────────────────────
import json as _jmod
_USERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "users.json")
def _load_users():
    try:
        if os.path.exists(_USERS_FILE):
            return _jmod.load(open(_USERS_FILE))
    except: pass
    return {}
def _save_users(u):
    try: _jmod.dump(u, open(_USERS_FILE, "w"))
    except: pass
USERS = _load_users()
def hash_pw(pw): return hashlib.sha256(pw.encode()).hexdigest()

# ── Pending deploy confirmations ─────────────────────────────
PENDING = {}  # "username:chat_id" -> parsed dict

# ── K8s helpers ──────────────────────────────────────────────
def k8s_deploy(app_name, image, replicas, port=80, memory=None):
    if not K8S_ENABLED:
        return False, "K8s not connected (simulation)"
    try:
        api  = k8s_client.AppsV1Api()
        core = k8s_client.CoreV1Api()
        resources = None
        if memory:
            resources = k8s_client.V1ResourceRequirements(
                requests={"memory": memory, "cpu": "100m"},
                limits={"memory": memory, "cpu": "500m"},
            )
        container = k8s_client.V1Container(
            name=app_name, image=image,
            ports=[k8s_client.V1ContainerPort(container_port=int(port))],
            resources=resources,
        )
        deploy = k8s_client.V1Deployment(
            api_version="apps/v1", kind="Deployment",
            metadata=k8s_client.V1ObjectMeta(name=app_name),
            spec=k8s_client.V1DeploymentSpec(
                replicas=int(replicas),
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
                ports=[k8s_client.V1ServicePort(port=int(port), target_port=int(port))],
                type="LoadBalancer",
            ),
        )
        try: api.create_namespaced_deployment(NS, deploy)
        except:
            import random
            app_name2 = app_name + "-" + str(random.randint(100, 999))
            deploy.metadata.name = app_name2
            deploy.spec.selector.match_labels["app"] = app_name2
            deploy.spec.template.metadata.labels["app"] = app_name2
            deploy.spec.template.spec.containers[0].name = app_name2
            svc.metadata.name = app_name2 + "-svc"
            svc.spec.selector["app"] = app_name2
            api.create_namespaced_deployment(NS, deploy)
            app_name = app_name2
        try: core.create_namespaced_service(NS, svc)
        except: pass
        os.makedirs(YAML_DIR, exist_ok=True)
        with open(os.path.join(YAML_DIR, app_name + ".yaml"), "w") as f:
            f.write(yaml_lib.dump(deploy.to_dict()))
        return True, app_name
    except Exception as e:
        return False, str(e)

def _age(ct):
    if not ct: return ""
    diff = (datetime.now().replace(tzinfo=None) - ct.replace(tzinfo=None)).total_seconds()
    if diff < 3600: return str(int(diff // 60)) + "m ago"
    if diff < 86400: return str(int(diff // 3600)) + "h ago"
    return str(int(diff // 86400)) + "d ago"

def k8s_get_pods():
    if not K8S_ENABLED: return []
    try:
        core = k8s_client.CoreV1Api()
        result = []
        for p in core.list_namespaced_pod(NS).items:
            restarts = sum(cs.restart_count for cs in (p.status.container_statuses or [])) if p.status and p.status.container_statuses else 0
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
            conds = [{"type": co.type, "status": co.status} for co in (p.status.conditions or [])] if p.status else []
            result.append({
                "name": p.metadata.name,
                "app": p.metadata.labels.get("app", "") if p.metadata.labels else "",
                "phase": p.status.phase or "Unknown",
                "ip": p.status.pod_ip or "",
                "node": p.spec.node_name or "",
                "age": _age(p.metadata.creation_timestamp),
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
        result = []
        for d in api.list_namespaced_deployment(NS).items:
            result.append({
                "name": d.metadata.name,
                "replicas": d.spec.replicas or 0,
                "ready": d.status.ready_replicas or 0,
                "image": d.spec.template.spec.containers[0].image if d.spec.template.spec.containers else "",
                "age": _age(d.metadata.creation_timestamp),
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
            return json.loads(resp.read()).get("model_loaded", False), False
    except: return False, True

def llama_chat(question):
    """Call LLaMA /chat endpoint for general Q&A"""
    try:
        data = json.dumps({"question": question, "max_new_tokens": 400}).encode()
        req = urllib.request.Request(
            MODEL_SERVER_URL + "/chat", data=data,
            headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=25) as resp:
            ans = json.loads(resp.read()).get("answer", "").strip()
        for stop in ["### User", "### System", "### Assistant", "User:", "Assistant:"]:
            if stop in ans:
                ans = ans[:ans.index(stop)].strip()
        return ans if len(ans) > 10 else None
    except: return None

# ── Main chat agent ──────────────────────────────────────────
def process_message(username, chat_id, message):
    msg_lower = message.lower().strip()
    pending_key = username + ":" + chat_id

    # ── 處理確認部署 ──
    if pending_key in PENDING:
        parsed = PENDING[pending_key]
        confirm_words = ["yes", "ok", "sure", "go", "confirm", "deploy", "yep", "好", "確認", "部署", "執行"]
        cancel_words  = ["no", "cancel", "stop", "不", "取消", "算了", "cancel"]
        if any(w in msg_lower for w in cancel_words):
            del PENDING[pending_key]
            return "Deployment cancelled.", "cancelled"
        if any(w in msg_lower for w in confirm_words):
            del PENDING[pending_key]
            suffix = str(int(time.time()))[-4:]
            app_name = parsed["app_name"] + "-" + suffix
            if K8S_ENABLED:
                ok, result = k8s_deploy(app_name, parsed["image"], parsed["pods"],
                                        parsed.get("port", 80), parsed.get("memory"))
                threading.Thread(target=save_gold_sample, args=(message, parsed), daemon=True).start()
                if ok:
                    actual_name = result
                    return ("Deployment started!\n\n"
                            "Name: " + actual_name + "\n"
                            "Image: " + str(parsed["image"]) + "\n"
                            "Pods: " + str(parsed["pods"]) + "\n"
                            + ("Port: " + str(parsed["port"]) + "\n" if parsed.get("port") else "")
                            + "\nCheck the Pods tab in a few seconds."), "deployed"
                else:
                    return "Deployment failed: " + result, "error"
            else:
                return "Simulation mode — K8s not connected.", "simulated"
        return ("Still waiting for confirmation:\n\n"
                "App: " + str(parsed.get("app_name", "")) + "\n"
                "Image: " + str(parsed.get("image", "")) + "\n"
                "Pods: " + str(parsed.get("pods", "")) + "\n\n"
                "Reply **yes** to deploy or **no** to cancel."), "waiting"

    deploy_kw = ["deploy","start","launch","run","create","spin","build","add","新增","部署","建立","起","跑","幫我"]
    delete_kw = ["delete","remove","kill","destroy","刪除","移除","刪掉"]
    list_kw   = ["list","show","get pods","get deploy","check pods","查","顯示","看","列出","有哪些","現在有","how many pods","how many deploy"]
    scale_kw  = ["scale","resize","replicas","adjust","調整","擴展","縮小","改成"]

    is_delete = any(k in msg_lower for k in delete_kw)
    is_list   = any(k in msg_lower for k in list_kw) and not any(k in msg_lower for k in deploy_kw + delete_kw)
    is_scale  = any(k in msg_lower for k in scale_kw) and not is_delete
    is_deploy = any(k in msg_lower for k in deploy_kw) and not is_delete

    # ── 部署 ──
    if is_deploy:
        parsed = ask_llama(message)
        if "error" in parsed or not parsed.get("image") or not parsed.get("pods"):
            return ("I couldn't parse that deployment. Please be more specific, for example:\n\n"
                    "deploy 3 nginx:latest pods for web-frontend\n"
                    "start 2 redis:7-alpine pods, port 6379"), "parse_failed"
        PENDING[pending_key] = parsed
        lines = ["Here's what I parsed:\n"]
        lines.append("App: **" + str(parsed.get("app_name", "")) + "**")
        lines.append("Image: **" + str(parsed.get("image", "")) + "**")
        lines.append("Pods: **" + str(parsed.get("pods", "")) + "**")
        if parsed.get("port"): lines.append("Port: **" + str(parsed["port"]) + "**")
        if parsed.get("memory"): lines.append("Memory: **" + str(parsed["memory"]) + "**")
        lines.append("\nDoes this look right?")
        return "\n".join(lines), "confirm_pending"

    # ── 刪除 ──
    elif is_delete:
        deps = k8s_get_deployments()
        dep_names = [d["name"] for d in deps]
        target = None
        for name in dep_names:
            if name.lower() in msg_lower:
                target = name
                break
        if not target:
            words = [w.strip(".,!?-") for w in message.split()]
            for name in dep_names:
                for part in name.replace("-", " ").split():
                    if len(part) > 3 and part.lower() in [w.lower() for w in words]:
                        target = name
                        break
        if target:
            ok, msg_r = k8s_delete_deployment(target)
            return ("Deleted **" + target + "** successfully!" if ok else "Failed: " + msg_r), "deleted"
        elif dep_names:
            return ("Which deployment to delete?\n\n" + "\n".join("- " + n for n in dep_names)), "list_for_delete"
        else:
            return "No deployments to delete.", "no_deps"

    # ── 列出狀態 ──
    elif is_list:
        pods = k8s_get_pods()
        deps = k8s_get_deployments()
        if "pod" in msg_lower:
            if not pods: return "No pods running.", "listed"
            lines = ["**" + str(len(pods)) + " pods** running:\n"]
            for p in pods:
                icon = "🟢" if p["phase"] == "Running" else "🟡" if p["phase"] == "Pending" else "🔴"
                lines.append(icon + " " + p["name"] + " — " + p["phase"] + " — " + p["ip"])
            return "\n".join(lines), "listed"
        else:
            if not deps: return "No deployments found.", "listed"
            lines = ["**" + str(len(deps)) + " deployments**:\n"]
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
            try:
                k8s_client.AppsV1Api().patch_namespaced_deployment(
                    target, NS, {"spec": {"replicas": int(nums[-1])}})
                return "Scaled **" + target + "** to **" + nums[-1] + "** replicas!", "scaled"
            except Exception as e:
                return "Scale failed: " + str(e), "error"
        elif not target:
            return "Which deployment? Available: " + (", ".join(dep_names) if dep_names else "none"), "need_target"
        else:
            return "How many replicas? e.g. scale web-frontend to 5", "need_count"

    # ── 一般問題：直接用 LLaMA ──
    else:
        ans = llama_chat(message)
        if ans:
            return ans, "llm_answered"
        # LLaMA 失敗才用簡單回覆
        pods = k8s_get_pods()
        deps = k8s_get_deployments()
        return ("I'm your K8s assistant! System: **" + str(len(pods)) + " pods**, **" + str(len(deps)) + " deployments**.\n\n"
                "I can help you:\n"
                "- deploy 3 nginx:latest pods for web-frontend\n"
                "- delete <deployment-name>\n"
                "- list pods / show deployments\n"
                "- scale <name> to 5\n"
                "- Ask any Kubernetes question!"), "answered"


# ── HTML ─────────────────────────────────────────────────────
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
  --bg:#F9FAFB;--surf:#FFFFFF;--b1:#E5E7EB;--b2:#D1D5DB;
  --t1:#111827;--t2:#6B7280;--t3:#9CA3AF;
  --gr:#16A34A;--grl:#DCFCE7;--grm:#BBF7D0;
  --rd:#DC2626;--rdl:#FEE2E2;
  --bl:#2563EB;--bll:#DBEAFE;
  --yw:#D97706;--ywl:#FEF3C7;
  --sw:220px;--r:10px;--rsm:6px;
}
body{font-family:'Geist',sans-serif;background:var(--bg);color:var(--t1);height:100vh;overflow:hidden}

/* Auth */
.auth{min-height:100vh;display:flex;align-items:center;justify-content:center}
.acard{background:var(--surf);border:1px solid var(--b1);border-radius:16px;padding:40px;width:420px;box-shadow:0 4px 24px rgba(0,0,0,.08)}
.alogo{display:flex;align-items:center;gap:10px;margin-bottom:28px}
.aicon{width:34px;height:34px;background:var(--gr);border-radius:8px;display:flex;align-items:center;justify-content:center;color:#fff;font-size:16px;font-weight:700}
.atitle{font-size:22px;font-weight:600;margin-bottom:6px}
.asub{font-size:14px;color:var(--t2);margin-bottom:24px}
.fg{margin-bottom:16px}
.fg label{display:block;font-size:13px;font-weight:500;margin-bottom:6px}
.fg input{width:100%;padding:10px 14px;border:1.5px solid var(--b2);border-radius:var(--rsm);font-size:14px;font-family:inherit;outline:none;transition:border .15s}
.fg input:focus{border-color:var(--gr);box-shadow:0 0 0 3px rgba(22,163,74,.1)}
.btnp{width:100%;padding:11px;background:var(--gr);color:#fff;border:none;border-radius:var(--rsm);font-size:14px;font-weight:500;cursor:pointer;font-family:inherit}
.btnp:hover{background:#15803D}
.alink{text-align:center;margin-top:20px;font-size:13px;color:var(--t2)}
.alink a{color:var(--gr);text-decoration:none;font-weight:500}
.aerr{background:var(--rdl);color:var(--rd);padding:10px 14px;border-radius:var(--rsm);font-size:13px;margin-bottom:16px}

/* Layout */
.layout{display:flex;height:100vh}

/* Sidebar */
.sb{width:var(--sw);background:var(--surf);border-right:1px solid var(--b1);display:flex;flex-direction:column;flex-shrink:0}
.sbhead{padding:14px 14px 10px;border-bottom:1px solid var(--b1)}
.sbbrand{display:flex;align-items:center;gap:9px;margin-bottom:12px}
.sbicon{width:28px;height:28px;background:var(--gr);border-radius:7px;display:flex;align-items:center;justify-content:center;color:#fff;font-size:14px;font-weight:700;flex-shrink:0}
.sbtext{font-size:14px;font-weight:600}
.newbtn{width:100%;padding:8px;background:var(--gr);color:#fff;border:none;border-radius:var(--rsm);font-size:13px;font-weight:500;cursor:pointer;font-family:inherit;display:flex;align-items:center;gap:6px;justify-content:center}
.newbtn:hover{background:#15803D}
.sbnav{padding:8px 8px 0;flex:1;overflow-y:auto}
.nsec{font-size:11px;color:var(--t3);font-weight:600;letter-spacing:.5px;text-transform:uppercase;padding:8px 6px 4px}
.ni{display:flex;align-items:center;gap:8px;padding:7px 8px;border-radius:var(--rsm);cursor:pointer;font-size:13px;color:var(--t2);transition:all .12s;border:none;background:none;width:100%;text-align:left}
.ni:hover{background:var(--bg);color:var(--t1)}
.ni.active{background:var(--grl);color:var(--gr)}
.ni svg{width:14px;height:14px;flex-shrink:0}
.nitext{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:13px}
.nidel{opacity:0;padding:2px 5px;border-radius:4px;font-size:11px;color:var(--t3);cursor:pointer;border:none;background:none;flex-shrink:0;line-height:1}
.ni:hover .nidel{opacity:1}
.nidel:hover{color:var(--rd);background:var(--rdl)}
.sbdiv{height:1px;background:var(--b1);margin:6px 8px}
.sbfoot{padding:12px 14px;border-top:1px solid var(--b1)}
.urow{display:flex;align-items:center;gap:9px}
.uav{width:30px;height:30px;background:var(--gr);border-radius:50%;display:flex;align-items:center;justify-content:center;color:#fff;font-size:12px;font-weight:600;flex-shrink:0}
.uname{font-size:13px;font-weight:500;flex:1}
.obtn{font-size:12px;color:var(--t3);cursor:pointer;border:none;background:none;font-family:inherit;padding:3px 8px;border-radius:4px}
.obtn:hover{color:var(--rd);background:var(--rdl)}

/* Status bar */
.stbar{padding:8px 24px;background:var(--surf);border-bottom:1px solid var(--b1);display:flex;align-items:center;gap:16px;flex-shrink:0}
.spill{display:flex;align-items:center;gap:5px;font-size:12px;color:var(--t2)}
.dot{width:7px;height:7px;border-radius:50%;background:var(--t3)}
.dot.on{background:var(--gr)}
.dot.off{background:var(--rd)}
.dot.pu{background:var(--yw);animation:pulse 1.5s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
.clk{margin-left:auto;font-size:12px;color:var(--t3);font-family:'Geist Mono',monospace}

/* Main */
.main{flex:1;display:flex;flex-direction:column;overflow:hidden}
.chatarea{flex:1;display:flex;overflow:hidden}

/* Chat messages */
.msglist{flex:1;overflow-y:auto;padding:20px 24px;display:flex;flex-direction:column;gap:14px}
.msg{display:flex;gap:10px}
.msg.user{flex-direction:row-reverse;align-self:flex-end;max-width:75%}
.msg.ai{align-self:flex-start;max-width:80%}
.mav{width:30px;height:30px;border-radius:50%;flex-shrink:0;display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:600}
.msg.ai .mav{background:var(--grl);color:var(--gr)}
.msg.user .mav{background:var(--gr);color:#fff}
.mcont{max-width:100%}
.mbub{padding:11px 15px;border-radius:14px;font-size:14px;line-height:1.65}
.msg.ai .mbub{background:var(--surf);border:1px solid var(--b1);border-radius:4px 14px 14px 14px}
.msg.user .mbub{background:var(--gr);color:#fff;border-radius:14px 4px 14px 14px}
.mtime{font-size:11px;color:var(--t3);margin-top:3px;padding:0 3px}
.msg.user .mtime{text-align:right}
.mbub b,.mbub strong{font-weight:600}
.mbub code{font-family:'Geist Mono',monospace;font-size:12px;background:rgba(0,0,0,.06);padding:1px 5px;border-radius:4px}
.msg.user .mbub code{background:rgba(255,255,255,.2)}

/* Confirm form */
.cform{margin-top:10px;border:1px solid var(--b1);border-radius:8px;padding:14px;background:var(--bg)}
.cform-title{font-size:11px;font-weight:600;color:var(--t2);margin-bottom:10px;text-transform:uppercase;letter-spacing:.4px}
.cform-fields{display:grid;gap:8px;margin-bottom:12px}
.cfield{display:flex;align-items:center;gap:8px}
.cfield label{font-size:12px;color:var(--t2);width:62px;flex-shrink:0}
.cfield input{flex:1;padding:6px 10px;border:1px solid var(--b2);border-radius:6px;font-size:13px;font-family:inherit;outline:none}
.cfield input:focus{border-color:var(--gr)}
.cbtns{display:flex;gap:8px;flex-wrap:wrap}
.cdeploy{padding:7px 16px;background:var(--gr);color:#fff;border:none;border-radius:var(--rsm);font-size:13px;font-weight:500;cursor:pointer;font-family:inherit}
.cdeploy:hover{background:#15803D}
.ccancel{padding:7px 16px;background:var(--surf);color:var(--t2);border:1px solid var(--b2);border-radius:var(--rsm);font-size:13px;cursor:pointer;font-family:inherit}
.ccancel:hover{background:var(--bg)}

/* Typing */
.typing{display:flex;gap:4px;padding:11px 15px;background:var(--surf);border:1px solid var(--b1);border-radius:4px 14px 14px 14px;width:fit-content}
.typing span{width:6px;height:6px;background:var(--t3);border-radius:50%;animation:bounce .9s infinite}
.typing span:nth-child(2){animation-delay:.15s}
.typing span:nth-child(3){animation-delay:.3s}
@keyframes bounce{0%,100%{transform:translateY(0)}50%{transform:translateY(-5px)}}

/* Welcome */
.welcome{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:40px;text-align:center}
.wicon{width:52px;height:52px;background:var(--gr);border-radius:14px;display:flex;align-items:center;justify-content:center;color:#fff;font-size:24px;font-weight:700;margin:0 auto 14px}
.wtitle{font-size:20px;font-weight:600;margin-bottom:8px}
.wsub{font-size:14px;color:var(--t2);margin-bottom:28px;max-width:400px;line-height:1.6}
.wgrid{display:grid;grid-template-columns:1fr 1fr;gap:10px;max-width:460px}
.wcard{padding:13px 15px;background:var(--surf);border:1px solid var(--b1);border-radius:var(--r);cursor:pointer;text-align:left;transition:all .12s;border:none;font-family:inherit;width:100%}
.wcard:hover{border-color:var(--gr)!important;background:var(--grl)}
.wcard-t{font-size:13px;font-weight:500;margin-bottom:3px;text-align:left}
.wcard-s{font-size:12px;color:var(--t2);text-align:left}

/* Input */
.inputarea{padding:14px 24px 18px;border-top:1px solid var(--b1);background:var(--surf);flex-shrink:0}
.inputwrap{display:flex;gap:10px;align-items:flex-end;background:var(--bg);border:1.5px solid var(--b2);border-radius:12px;padding:10px 14px;transition:border .15s}
.inputwrap:focus-within{border-color:var(--gr);box-shadow:0 0 0 3px rgba(22,163,74,.1)}
.chatinput{flex:1;border:none;background:none;resize:none;font-size:14px;font-family:inherit;outline:none;max-height:140px;line-height:1.5;color:var(--t1)}
.chatinput::placeholder{color:var(--t3)}
.sendbtn{padding:8px 16px;background:var(--gr);color:#fff;border:none;border-radius:8px;font-size:13px;font-weight:500;cursor:pointer;font-family:inherit;flex-shrink:0}
.sendbtn:hover{background:#15803D}
.hint{font-size:11px;color:var(--t3);margin-top:5px;text-align:center}

/* Pods panel */
.ppanel{display:none;width:340px;border-left:1px solid var(--b1);background:var(--surf);flex-direction:column;flex-shrink:0}
.ppanel.open{display:flex}
.phead{padding:14px 16px;border-bottom:1px solid var(--b1);display:flex;align-items:center;justify-content:space-between}
.ptitle{font-size:14px;font-weight:600}
.pclose{width:26px;height:26px;border-radius:6px;border:none;background:var(--bg);cursor:pointer;color:var(--t2);font-size:13px}
.ptabs{display:flex;border-bottom:1px solid var(--b1);padding:0 16px}
.ptab{padding:9px 12px;font-size:13px;font-weight:500;cursor:pointer;border-bottom:2px solid transparent;color:var(--t2)}
.ptab.active{color:var(--gr);border-bottom-color:var(--gr)}
.pcont{flex:1;overflow-y:auto;padding:10px}
.podcard{padding:10px 12px;border:1px solid var(--b1);border-radius:var(--rsm);margin-bottom:7px;cursor:pointer;transition:all .12s}
.podcard:hover{border-color:var(--gr);background:var(--grl)}
.podname{font-size:12px;font-family:'Geist Mono',monospace;font-weight:500;margin-bottom:4px}
.podmeta{display:flex;gap:7px;align-items:center;flex-wrap:wrap}
.podbadge{padding:2px 8px;border-radius:20px;font-size:11px;font-weight:500}
.podbadge.Running{background:var(--grl);color:var(--gr)}
.podbadge.Pending{background:var(--ywl);color:var(--yw)}
.podbadge.Failed,.podbadge.Error{background:var(--rdl);color:var(--rd)}
.podip{font-size:11px;color:var(--t3);font-family:'Geist Mono',monospace}
.depcard{padding:10px 12px;border:1px solid var(--b1);border-radius:var(--rsm);margin-bottom:7px}
.depname{font-size:13px;font-weight:500;margin-bottom:3px}
.depmeta{font-size:12px;color:var(--t2)}
.depdel{margin-top:7px;padding:4px 10px;font-size:11px;border:1px solid #FECACA;color:var(--rd);background:none;border-radius:4px;cursor:pointer;font-family:inherit}
.depdel:hover{background:var(--rdl)}
.empty{text-align:center;padding:28px;color:var(--t3);font-size:13px}

/* Modal */
.mbg{position:fixed;inset:0;background:rgba(0,0,0,.25);z-index:1000;display:none;align-items:center;justify-content:center}
.mbg.open{display:flex}
.modal{background:var(--surf);border-radius:14px;width:540px;max-height:80vh;overflow-y:auto;box-shadow:0 20px 40px rgba(0,0,0,.15)}
.mhead{padding:16px 20px 12px;border-bottom:1px solid var(--b1);display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;background:var(--surf)}
.mtitle{font-size:14px;font-weight:600}
.mclose{width:26px;height:26px;border-radius:6px;border:none;background:var(--bg);cursor:pointer;font-size:13px;color:var(--t2)}
.mbody{padding:16px 20px}
.dsec{margin-bottom:16px}
.dsectitle{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.5px;color:var(--t3);margin-bottom:8px}
.drow{display:flex;justify-content:space-between;padding:7px 0;border-bottom:1px solid var(--b1);font-size:13px}
.drow:last-child{border-bottom:none}
.dkey{color:var(--t2)}
.dval{font-family:'Geist Mono',monospace;font-size:12px;max-width:260px;word-break:break-all;text-align:right}
.chip{padding:2px 8px;border-radius:4px;font-size:11px;font-weight:500;margin:2px}
.ct{background:var(--grl);color:var(--gr)}
.cf{background:var(--rdl);color:var(--rd)}
</style>
</head>
<body>

{% if not logged_in and page != 'register' %}
<div class="auth">
  <div class="acard">
    <div class="alogo"><div class="aicon">K</div><span style="font-size:15px;font-weight:600">ZeroTouch K8s</span></div>
    <div class="atitle">Welcome back</div>
    <div class="asub">Sign in to your account</div>
    {% if error %}<div class="aerr">{{ error }}</div>{% endif %}
    <form method="POST" action="/auth/login">
      <div class="fg"><label>Username</label><input type="text" name="username" placeholder="Enter your username" required autofocus></div>
      <div class="fg"><label>Password</label><input type="password" name="password" placeholder="Enter your password" required></div>
      <button class="btnp" type="submit">Sign In</button>
    </form>
    <div class="alink">Don't have an account? <a href="/auth/register">Register</a></div>
  </div>
</div>

{% elif not logged_in and page == 'register' %}
<div class="auth">
  <div class="acard">
    <div class="alogo"><div class="aicon">K</div><span style="font-size:15px;font-weight:600">ZeroTouch K8s</span></div>
    <div class="atitle">Create account</div>
    <div class="asub">Get started with ZeroTouch K8s</div>
    {% if error %}<div class="aerr">{{ error }}</div>{% endif %}
    <form method="POST" action="/auth/register">
      <div class="fg"><label>Username</label><input type="text" name="username" placeholder="Choose a username" required autofocus></div>
      <div class="fg"><label>Password</label><input type="password" name="password" placeholder="At least 6 characters" required></div>
      <div class="fg"><label>Confirm</label><input type="password" name="confirm" placeholder="Confirm password" required></div>
      <button class="btnp" type="submit">Create Account</button>
    </form>
    <div class="alink">Already have an account? <a href="/">Sign In</a></div>
  </div>
</div>

{% else %}
<div class="layout">
  <aside class="sb">
    <div class="sbhead">
      <div class="sbbrand">
        <div class="sbicon">K</div>
        <span class="sbtext">ZeroTouch K8s</span>
      </div>
      <button class="newbtn" onclick="newChat()">
        <svg width="13" height="13" viewBox="0 0 13 13"><path d="M6.5 1v11M1 6.5h11" stroke="#fff" stroke-width="1.8" stroke-linecap="round"/></svg>
        New Chat
      </button>
    </div>
    <div class="sbnav">
      <div class="nsec">Chats</div>
      <div id="chat-list"></div>
      <div class="sbdiv"></div>
      <div class="nsec">System</div>
      <div class="ni" onclick="togglePanel('pods')">
        <svg viewBox="0 0 16 16" fill="none"><circle cx="8" cy="8" r="5" stroke="currentColor" stroke-width="1.5"/><circle cx="8" cy="8" r="2" fill="currentColor"/></svg>
        <span class="nitext">Pods</span>
      </div>
      <div class="ni" onclick="togglePanel('deployments')">
        <svg viewBox="0 0 16 16" fill="none"><path d="M2 4h12M2 8h12M2 12h8" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>
        <span class="nitext">Deployments</span>
      </div>
    </div>
    <div class="sbfoot">
      <div class="urow">
        <div class="uav">{{ username[0].upper() }}</div>
        <div class="uname">{{ username }}</div>
        <form method="POST" action="/auth/logout" style="margin:0">
          <button class="obtn" type="submit">Out</button>
        </form>
      </div>
    </div>
  </aside>

  <div class="main">
    <div class="stbar">
      <div class="spill"><div class="dot" id="mdot"></div><span id="mst">Checking...</span></div>
      <div class="spill"><div class="dot {% if k8s %}on{% else %}off{% endif %}"></div><span>K8s {% if k8s %}Connected{% else %}Simulation{% endif %}</span></div>
      <div class="clk" id="clk"></div>
    </div>
    <div class="chatarea">
      <div style="flex:1;display:flex;flex-direction:column;overflow:hidden" id="chat-area"></div>
      <div class="ppanel" id="ppanel">
        <div class="phead">
          <div class="ptitle" id="ptitle">Pods</div>
          <button class="pclose" onclick="togglePanel(null)">✕</button>
        </div>
        <div class="ptabs">
          <div class="ptab active" id="tab-pods" onclick="switchTab('pods')">Pods</div>
          <div class="ptab" id="tab-deps" onclick="switchTab('deployments')">Deployments</div>
        </div>
        <div class="pcont" id="pcont">Loading...</div>
      </div>
    </div>
  </div>
</div>

<div class="mbg" id="pod-modal">
  <div class="modal">
    <div class="mhead">
      <div class="mtitle" id="mtitle">Pod Details</div>
      <button class="mclose" onclick="closeModal()">✕</button>
    </div>
    <div class="mbody" id="mbody"></div>
  </div>
</div>

<script>
const USERNAME = "{{ username }}";
let currentChatId = null;
let panelMode = null;
let chats = JSON.parse(localStorage.getItem('k8s_chats_' + USERNAME) || '{}');

// Clock
setInterval(()=>{ const el=document.getElementById('clk'); if(el) el.textContent=new Date().toLocaleTimeString('en-GB'); },1000);

// Model status
async function pollStatus(){
  try{
    const r=await fetch('/api/status'); const d=await r.json();
    const dot=document.getElementById('mdot'); const st=document.getElementById('mst');
    if(d.model_ready){dot.className='dot on';st.textContent='Model Ready';}
    else{dot.className='dot pu';st.textContent='Model Loading...';}
  }catch(e){}
}
pollStatus(); setInterval(pollStatus,5000);

function saveChats(){ localStorage.setItem('k8s_chats_'+USERNAME, JSON.stringify(chats)); }

function renderChatList(){
  const el=document.getElementById('chat-list');
  const sorted=Object.entries(chats).sort((a,b)=>(b[1].ts||0)-(a[1].ts||0));
  if(!sorted.length){
    el.innerHTML='<div style="font-size:12px;color:var(--t3);padding:6px 8px">No chats yet</div>';
    return;
  }
  el.innerHTML=sorted.map(([id,chat])=>`
    <div class="ni ${id===currentChatId?'active':''}" onclick="loadChat('${id}')">
      <svg viewBox="0 0 16 16" fill="none"><path d="M2 3a1 1 0 011-1h10a1 1 0 011 1v7a1 1 0 01-1 1H9l-3 2v-2H3a1 1 0 01-1-1V3z" stroke="currentColor" stroke-width="1.5"/></svg>
      <span class="nitext">${chat.title||'New Chat'}</span>
      <span class="nidel" onclick="event.stopPropagation();deleteChat('${id}')">✕</span>
    </div>`).join('');
}

function newChat(){
  const id='c'+Date.now();
  chats[id]={title:'New Chat',messages:[],ts:Date.now()};
  saveChats(); loadChat(id);
}

function loadChat(id){
  currentChatId=id; renderChatList(); renderChatArea();
}

function deleteChat(id){
  if(!confirm('Delete this chat?')) return;
  delete chats[id]; saveChats();
  if(currentChatId===id){ currentChatId=null; showWelcome(); }
  renderChatList();
}

function showWelcome(){
  document.getElementById('chat-area').innerHTML=`
    <div class="welcome">
      <div class="wicon">K</div>
      <div class="wtitle">ZeroTouch K8s Assistant</div>
      <div class="wsub">Deploy and manage Kubernetes services using natural language. Ask me anything about K8s or start with a quick action.</div>
      <div class="wgrid">
        <button class="wcard" style="border:1px solid var(--b1)" onclick="quickStart('deploy 3 nginx:latest pods for web-frontend')">
          <div class="wcard-t">Deploy a service</div>
          <div class="wcard-s">deploy 3 nginx:latest pods...</div>
        </button>
        <button class="wcard" style="border:1px solid var(--b1)" onclick="quickStart('list pods')">
          <div class="wcard-t">Check status</div>
          <div class="wcard-s">list pods / show deployments</div>
        </button>
        <button class="wcard" style="border:1px solid var(--b1)" onclick="quickStart('What is a Pod?')">
          <div class="wcard-t">Learn K8s</div>
          <div class="wcard-s">What is a Pod, Deployment...</div>
        </button>
        <button class="wcard" style="border:1px solid var(--b1)" onclick="quickStart('delete ')">
          <div class="wcard-t">Delete deployment</div>
          <div class="wcard-s">delete &lt;deployment-name&gt;</div>
        </button>
      </div>
    </div>
    ${inputHTML()}`;
}

function inputHTML(){
  return `<div class="inputarea">
    <div class="inputwrap">
      <textarea class="chatinput" id="chat-input" placeholder="Message ZeroTouch K8s..." rows="1"
        onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();sendMsg()}"
        oninput="this.style.height='auto';this.style.height=Math.min(this.scrollHeight,140)+'px'"></textarea>
      <button class="sendbtn" onclick="sendMsg()">Send</button>
    </div>
    <div class="hint">Enter to send · Shift+Enter for new line</div>
  </div>`;
}

function quickStart(text){
  if(!currentChatId) newChat();
  const inp=document.getElementById('chat-input');
  if(inp){ inp.value=text; inp.focus(); }
}

function renderChatArea(){
  if(!currentChatId){ showWelcome(); return; }
  const chat=chats[currentChatId];
  const msgs=chat.messages||[];
  if(msgs.length===0){ showWelcome(); return; }
  document.getElementById('chat-area').innerHTML=`
    <div class="msglist" id="msglist">${msgs.map(renderMsg).join('')}</div>
    ${inputHTML()}`;
  scrollBottom();
}

function renderMsg(m){
  const cls=m.role==='user'?'user':'ai';
  const av=m.role==='user'?USERNAME[0].toUpperCase():'K';
  const txt=fmtText(m.content);
  let extra='';
  if(m.action==='confirm_pending'){
    // Extract fields from message
    const appM=m.content.match(/App:\s*\*\*([^*]+)\*\*/);
    const imgM=m.content.match(/Image:\s*\*\*([^*]+)\*\*/);
    const podM=m.content.match(/Pods:\s*\*\*(\d+)\*\*/);
    const portM=m.content.match(/Port:\s*\*\*(\d+)\*\*/);
    const app=appM?appM[1]:'';
    const img=imgM?imgM[1]:'nginx:latest';
    const pods=podM?podM[1]:'1';
    const port=portM?portM[1]:'80';
    extra=`<div class="cform">
      <div class="cform-title">Confirm or Edit</div>
      <div class="cform-fields">
        <div class="cfield"><label>App</label><input id="cf-app" value="${app}" placeholder="app-name"></div>
        <div class="cfield"><label>Image</label><input id="cf-img" value="${img}" placeholder="nginx:latest" style="font-family:'Geist Mono',monospace"></div>
        <div class="cfield"><label>Pods</label><input id="cf-pods" type="number" value="${pods}" min="1" max="20" style="width:80px"></div>
        <div class="cfield"><label>Port</label><input id="cf-port" type="number" value="${port}" style="width:100px"></div>
      </div>
      <div class="cbtns">
        <button class="cdeploy" onclick="deployFromForm()">Deploy</button>
        <button class="ccancel" onclick="sendQuick('no')">Cancel</button>
      </div>
    </div>`;
  }
  return `<div class="msg ${cls}">
    <div class="mav">${av}</div>
    <div class="mcont"><div class="mbub">${txt}${extra}</div><div class="mtime">${m.time||''}</div></div>
  </div>`;
}

function fmtText(t){
  return t.replace(/\*\*(.+?)\*\*/g,'<strong>$1</strong>')
           .replace(/`(.+?)`/g,'<code>$1</code>')
           .replace(/\n/g,'<br>');
}

function scrollBottom(){ const el=document.getElementById('msglist'); if(el) el.scrollTop=el.scrollHeight; }

function deployFromForm(){
  const app=document.getElementById('cf-app')?.value.trim();
  const img=document.getElementById('cf-img')?.value.trim();
  const pods=document.getElementById('cf-pods')?.value.trim();
  const port=document.getElementById('cf-port')?.value.trim();
  if(!app||!img||!pods){ alert('Please fill in all fields'); return; }
  const msg='deploy-form:app='+app+' image='+img+' pods='+pods+' port='+(port||'80');
  sendMsgText(msg);
}

function sendQuick(text){ sendMsgText(text); }

async function sendMsg(){
  const inp=document.getElementById('chat-input');
  if(!inp) return;
  const text=inp.value.trim();
  if(!text) return;
  inp.value=''; inp.style.height='auto';
  sendMsgText(text);
}

async function sendMsgText(text){
  if(!currentChatId) newChat();
  const chat=chats[currentChatId];
  const time=new Date().toLocaleTimeString('en-GB',{hour:'2-digit',minute:'2-digit'});
  const displayText=text.startsWith('deploy-form:')?'[Deploying with edited parameters...]':text;
  chat.messages.push({role:'user',content:displayText,time});
  if(chat.title==='New Chat') chat.title=text.slice(0,30);
  chat.ts=Date.now();
  saveChats(); renderChatArea();

  const msglist=document.getElementById('msglist');
  const typing=document.createElement('div');
  typing.className='msg ai'; typing.id='typing';
  typing.innerHTML='<div class="mav">K</div><div class="mcont"><div class="typing"><span></span><span></span><span></span></div></div>';
  if(msglist){ msglist.appendChild(typing); scrollBottom(); }

  try{
    const r=await fetch('/api/chat',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({message:text,chat_id:currentChatId})
    });
    const d=await r.json();
    document.getElementById('typing')?.remove();
    const reply=d.reply||'No response';
    const action=d.action||'';
    chat.messages.push({role:'ai',content:reply,time:new Date().toLocaleTimeString('en-GB',{hour:'2-digit',minute:'2-digit'}),action});
    saveChats(); renderChatArea();
  }catch(e){
    document.getElementById('typing')?.remove();
    chat.messages.push({role:'ai',content:'Connection error. Please try again.',time:''});
    saveChats(); renderChatArea();
  }
}

// Panel
function togglePanel(mode){
  const p=document.getElementById('ppanel');
  if(panelMode===mode||!mode){ p.classList.remove('open'); panelMode=null; return; }
  panelMode=mode; p.classList.add('open'); switchTab(mode);
}
function switchTab(tab){
  panelMode=tab;
  document.getElementById('tab-pods').classList.toggle('active',tab==='pods');
  document.getElementById('tab-deps').classList.toggle('active',tab==='deployments');
  document.getElementById('ptitle').textContent=tab==='pods'?'Pods':'Deployments';
  tab==='pods'?loadPods():loadDeps();
}
async function loadPods(){
  const el=document.getElementById('pcont');
  el.innerHTML='<div class="empty">Loading...</div>';
  try{
    const r=await fetch('/api/pods'); const d=await r.json();
    if(!d.pods.length){ el.innerHTML='<div class="empty">No pods found</div>'; return; }
    el.innerHTML=d.pods.map(p=>`
      <div class="podcard" onclick='showPodDetail(${JSON.stringify(JSON.stringify(p))})'>
        <div class="podname">${p.name}</div>
        <div class="podmeta">
          <span class="podbadge ${p.phase}">${p.phase}</span>
          <span class="podip">${p.ip}</span>
          <span class="podip">${p.age}</span>
        </div>
      </div>`).join('');
  }catch(e){ el.innerHTML='<div class="empty">Error loading</div>'; }
}
async function loadDeps(){
  const el=document.getElementById('pcont');
  el.innerHTML='<div class="empty">Loading...</div>';
  try{
    const r=await fetch('/api/deployments'); const d=await r.json();
    if(!d.deployments.length){ el.innerHTML='<div class="empty">No deployments</div>'; return; }
    el.innerHTML=d.deployments.map(dep=>`
      <div class="depcard">
        <div class="depname">${dep.name}</div>
        <div class="depmeta">${dep.image} · ${dep.ready}/${dep.replicas} ready · ${dep.age}</div>
        <button class="depdel" onclick="deleteDep('${dep.name}')">Delete</button>
      </div>`).join('');
  }catch(e){ el.innerHTML='<div class="empty">Error loading</div>'; }
}
async function deleteDep(name){
  if(!confirm('Delete '+name+'?')) return;
  const r=await fetch('/api/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name})});
  const d=await r.json();
  if(d.success) loadDeps(); else alert('Error: '+d.error);
}
function showPodDetail(jsonStr){
  const p=JSON.parse(jsonStr);
  document.getElementById('mtitle').textContent=p.name;
  const sc=p.phase==='Running'?'var(--gr)':p.phase==='Pending'?'var(--yw)':'var(--rd)';
  let cHtml='';
  (p.containers||[]).forEach(c=>{
    const req=Object.entries(c.resources.requests||{}).map(([k,v])=>k+': '+v).join(', ')||'—';
    const lim=Object.entries(c.resources.limits||{}).map(([k,v])=>k+': '+v).join(', ')||'—';
    cHtml+=`<div style="background:var(--bg);border-radius:8px;padding:10px 12px;margin-bottom:8px">
      <div style="font-weight:600;font-size:13px;margin-bottom:6px">${c.name}</div>
      <div class="drow"><span class="dkey">Image</span><span class="dval">${c.image}</span></div>
      <div class="drow"><span class="dkey">Ports</span><span class="dval">${c.ports.join(', ')||'—'}</span></div>
      <div class="drow"><span class="dkey">Requests</span><span class="dval">${req}</span></div>
      <div class="drow"><span class="dkey">Limits</span><span class="dval">${lim}</span></div>
    </div>`;
  });
  const condHtml=(p.conditions||[]).map(c=>`<span class="chip ${c.status==='True'?'ct':'cf'}">${c.type}: ${c.status}</span>`).join('')||'—';
  document.getElementById('mbody').innerHTML=`
    <div class="dsec">
      <div class="dsectitle">General</div>
      <div class="drow"><span class="dkey">Name</span><span class="dval">${p.name}</span></div>
      <div class="drow"><span class="dkey">App</span><span class="dval">${p.app||'—'}</span></div>
      <div class="drow"><span class="dkey">Status</span><span class="dval" style="color:${sc};font-weight:600">${p.phase}</span></div>
      <div class="drow"><span class="dkey">IP</span><span class="dval">${p.ip||'—'}</span></div>
      <div class="drow"><span class="dkey">Node</span><span class="dval">${p.node||'—'}</span></div>
      <div class="drow"><span class="dkey">Restarts</span><span class="dval">${p.restarts}</span></div>
      <div class="drow"><span class="dkey">Age</span><span class="dval">${p.age}</span></div>
    </div>
    <div class="dsec"><div class="dsectitle">Containers</div>${cHtml||'<p style="color:var(--t3);font-size:13px">No info</p>'}</div>
    <div class="dsec"><div class="dsectitle">Conditions</div><div style="display:flex;flex-wrap:wrap">${condHtml}</div></div>`;
  document.getElementById('pod-modal').classList.add('open');
}
function closeModal(){ document.getElementById('pod-modal').classList.remove('open'); }
document.getElementById('pod-modal')?.addEventListener('click',e=>{ if(e.target.id==='pod-modal') closeModal(); });

// Init
renderChatList();
const sorted=Object.entries(chats).sort((a,b)=>(b[1].ts||0)-(a[1].ts||0));
if(sorted.length){ loadChat(sorted[0][0]); } else { showWelcome(); }
</script>
{% endif %}
</body>
</html>
"""

# ── Routes ───────────────────────────────────────────────────
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
    ready, _ = _model_status()
    return jsonify({"model_ready": ready, "k8s": K8S_ENABLED})

@app.route("/api/chat", methods=["POST"])
def api_chat():
    if "username" not in session:
        return jsonify({"error": "Not authenticated"}), 401
    data    = request.get_json() or {}
    message = data.get("message","").strip()
    chat_id = data.get("chat_id","default")
    if not message:
        return jsonify({"error": "Empty message"}), 400

    # Handle deploy-form submission
    if message.startswith("deploy-form:"):
        import re as _re
        app_m  = _re.search(r"app=(\S+)", message)
        img_m  = _re.search(r"image=(\S+)", message)
        pods_m = _re.search(r"pods=(\d+)", message)
        port_m = _re.search(r"port=(\d+)", message)
        if app_m and img_m and pods_m:
            parsed = {
                "app_name": app_m.group(1),
                "image":    img_m.group(1),
                "pods":     int(pods_m.group(1)),
                "port":     int(port_m.group(1)) if port_m else 80,
            }
            suffix = str(int(time.time()))[-4:]
            app_name = parsed["app_name"] + "-" + suffix
            if K8S_ENABLED:
                ok, result = k8s_deploy(app_name, parsed["image"], parsed["pods"], parsed["port"])
                threading.Thread(target=save_gold_sample, args=(message, parsed), daemon=True).start()
                if ok:
                    reply = ("Deployment started!\n\nName: " + result + "\nImage: " + parsed["image"] +
                             "\nPods: " + str(parsed["pods"]) + "\n\nCheck the Pods tab!")
                    return jsonify({"reply": reply, "action": "deployed"})
                else:
                    return jsonify({"reply": "Deployment failed: " + result, "action": "error"})
            else:
                return jsonify({"reply": "Simulation mode — K8s not connected.", "action": "simulated"})

    reply, action = process_message(session["username"], chat_id, message)
    return jsonify({"reply": reply, "action": action})

@app.route("/api/pods")
def api_pods():
    if "username" not in session: return jsonify({"error": "Not authenticated"}), 401
    return jsonify({"pods": k8s_get_pods()})

@app.route("/api/deployments")
def api_deployments():
    if "username" not in session: return jsonify({"error": "Not authenticated"}), 401
    return jsonify({"deployments": k8s_get_deployments()})

@app.route("/api/delete", methods=["POST"])
def api_delete():
    if "username" not in session: return jsonify({"error": "Not authenticated"}), 401
    name = (request.get_json() or {}).get("name","").strip()
    if not name: return jsonify({"error": "Name required"}), 400
    ok, msg = k8s_delete_deployment(name)
    return jsonify({"success": ok, "error": None if ok else msg})

if __name__ == "__main__":
    print("=" * 50)
    print("  ZeroTouch K8s  v4")
    print(f"  K8s: {'Connected' if K8S_ENABLED else 'Simulation'}")
    print("  http://localhost:5000")
    print("=" * 50)
    app.run(host="0.0.0.0", port=5000, debug=False)
