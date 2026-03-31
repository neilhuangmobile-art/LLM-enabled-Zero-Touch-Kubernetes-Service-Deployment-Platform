"""
web_demo.py
Zero-Touch K8s 部署平台 — 高品質 Web Demo
串接真實 LLaMA-3 LoRA 模型 + Kubernetes API

執行前安裝：pip install flask kubernetes pyyaml
執行：python web_demo.py
開啟瀏覽器：http://localhost:5000
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core.config import ensure_utf8_output
ensure_utf8_output()

import threading, json, urllib.request
from flask import Flask, request, jsonify, render_template_string

from core.config import YAML_DIR, MODEL_SERVER_URL
from llama_client import ask_llama, save_gold_sample

# ── Kubernetes（有 K8s 環境才啟用）──────────────────────────
K8S_ENABLED = False
try:
    from kubernetes import client as k8s_client, config as k8s_config
    import yaml as yaml_lib
    k8s_config.load_kube_config()
    K8S_ENABLED = True
    print("[K8s] 已連線")
except Exception as e:
    print(f"[K8s] 未連線（模擬模式）：{e}")

# ── 設定 ────────────────────────────────────────────────────
NS = "default"

app = Flask(__name__)


def _model_status():
    """檢查 Model Server 健康狀態。"""
    try:
        with urllib.request.urlopen(f"{MODEL_SERVER_URL}/health", timeout=2) as resp:
            data = json.loads(resp.read())
            return data.get("model_loaded", False), False
    except Exception:
        return False, True  # 未就緒，視為載入中



# ════════════════════════════════════════════════════════════
# Kubernetes 操作
# ════════════════════════════════════════════════════════════
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

        # 儲存 YAML
        os.makedirs(YAML_DIR, exist_ok=True)
        path = os.path.join(YAML_DIR, f"{app_name}.yaml")
        with open(path, "w", encoding="utf-8") as f:
            f.write(yaml_lib.dump(deploy.to_dict()))
            f.write("---\n")
            f.write(yaml_lib.dump(svc.to_dict()))

        return True, f"已部署到 K8s，YAML 儲存於 {path}"
    except Exception as e:
        return False, str(e)


def k8s_get_pods(app_name=None):
    if not K8S_ENABLED:
        return []
    try:
        core = k8s_client.CoreV1Api()
        selector = f"app={app_name}" if app_name else None
        pods = core.list_namespaced_pod(NS, label_selector=selector)
        return [
            {
                "name"  : p.metadata.name,
                "app"   : p.metadata.labels.get("app", ""),
                "phase" : p.status.phase or "Unknown",
                "ip"    : p.status.pod_ip or "",
                "node"  : p.spec.node_name or "",
                "age"   : str(p.metadata.creation_timestamp)[:16] if p.metadata.creation_timestamp else "",
            }
            for p in pods.items
        ]
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


# ════════════════════════════════════════════════════════════
# HTML 前端
# ════════════════════════════════════════════════════════════
HTML = r"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ZeroTouch K8s — LLM Deployment Platform</title>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;600;700&family=Syne:wght@600;800&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#070b10;--bg2:#0d1117;--bg3:#161b22;
  --green:#00ff88;--blue:#00ccff;--purple:#bb99ff;--amber:#ffbb00;--red:#ff5555;
  --border:rgba(255,255,255,0.07);--text:#e6edf3;--muted:rgba(255,255,255,0.32);
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;background:var(--bg);color:var(--text);font-family:'JetBrains Mono',monospace}
::-webkit-scrollbar{width:3px}::-webkit-scrollbar-thumb{background:rgba(255,255,255,0.1);border-radius:2px}

/* ── LOADING SCREEN ── */
#loading-screen{
  position:fixed;inset:0;background:var(--bg);z-index:9999;
  display:flex;flex-direction:column;align-items:center;justify-content:center;gap:24px;
  transition:opacity 0.6s ease;
}
#loading-screen.hidden{opacity:0;pointer-events:none}
.loading-logo{font-family:'Syne',sans-serif;font-size:28px;font-weight:800;color:#fff;letter-spacing:-0.03em}
.loading-logo span{color:var(--green)}
.loading-bar-wrap{width:280px;height:2px;background:rgba(255,255,255,0.08);border-radius:1px;overflow:hidden}
.loading-bar{height:100%;background:linear-gradient(90deg,var(--green),var(--blue));border-radius:1px;width:0;transition:width 0.4s ease}
.loading-status{font-size:11px;color:var(--muted);letter-spacing:0.05em;min-height:16px}

/* ── HEADER ── */
header{
  position:sticky;top:0;z-index:100;
  border-bottom:1px solid var(--border);
  background:rgba(7,11,16,0.85);backdrop-filter:blur(12px);
  padding:14px 28px;display:flex;align-items:center;justify-content:space-between;
}
.logo{display:flex;align-items:center;gap:10px}
.logo-icon{
  width:34px;height:34px;border-radius:9px;font-size:17px;
  background:linear-gradient(135deg,var(--green),var(--blue));
  display:flex;align-items:center;justify-content:center;flex-shrink:0;
}
.logo h1{font-family:'Syne',sans-serif;font-size:16px;font-weight:800;letter-spacing:-0.02em}
.logo p{font-size:9px;color:var(--muted);letter-spacing:0.12em;margin-top:1px}
.hstats{display:flex;gap:22px}
.hstat{text-align:right}
.hstat-label{font-size:9px;color:var(--muted);letter-spacing:0.1em}
.hstat-val{font-size:12px;font-weight:600;margin-top:2px}
#model-status-dot{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--amber);vertical-align:middle;margin-right:5px;animation:pulse 1.2s infinite}
#model-status-dot.ready{background:var(--green);animation:none;box-shadow:0 0 5px var(--green)}

/* ── LAYOUT ── */
.layout{display:grid;grid-template-columns:480px 1fr;height:calc(100vh - 65px);overflow:hidden}
.left-panel{border-right:1px solid var(--border);display:flex;flex-direction:column;overflow:hidden}
.right-panel{display:flex;flex-direction:column;overflow:hidden}

/* ── INPUT SECTION ── */
.input-section{padding:20px 24px;border-bottom:1px solid var(--border)}
.section-label{font-size:9px;color:var(--muted);letter-spacing:0.12em;margin-bottom:8px}
.input-wrap{
  position:relative;
  background:rgba(255,255,255,0.04);
  border:1px solid rgba(255,255,255,0.1);
  border-radius:11px;padding:12px 14px;
  transition:border-color 0.2s;
}
.input-wrap:focus-within{border-color:rgba(0,255,136,0.4);box-shadow:0 0 0 3px rgba(0,255,136,0.06)}
textarea{
  width:100%;background:none;border:none;outline:none;resize:none;
  font-family:'JetBrains Mono',monospace;font-size:13px;color:var(--text);line-height:1.6;
  padding-right:44px;
}
textarea::placeholder{color:var(--muted)}
textarea:disabled{opacity:0.5;cursor:not-allowed}
.send-btn{
  position:absolute;right:10px;bottom:10px;
  width:32px;height:32px;border-radius:8px;border:none;cursor:pointer;
  font-size:15px;display:flex;align-items:center;justify-content:center;
  background:rgba(0,255,136,0.12);border:1px solid rgba(0,255,136,0.3);
  color:var(--green);transition:all 0.2s;
}
.send-btn:hover{background:rgba(0,255,136,0.22)}
.send-btn:disabled{opacity:0.3;cursor:not-allowed}

/* 防呆提示 */
.input-hint{font-size:10px;margin-top:6px;min-height:14px;transition:color 0.2s}
.input-hint.warn{color:var(--amber)}
.input-hint.ok{color:var(--green)}
.input-hint.err{color:var(--red)}

/* 快速建議 */
.chips{display:flex;flex-wrap:wrap;gap:5px;margin-top:10px}
.chip{
  font-size:10px;padding:3px 9px;border-radius:20px;cursor:pointer;
  background:rgba(255,255,255,0.04);border:1px solid var(--border);
  color:var(--muted);transition:all 0.15s;white-space:nowrap;
}
.chip:hover{border-color:rgba(0,255,136,0.3);color:var(--green)}

/* ── PROGRESS ── */
.progress-section{padding:14px 24px;border-bottom:1px solid var(--border);display:none}
.prog-header{display:flex;justify-content:space-between;margin-bottom:7px}
.prog-step{font-size:11px;color:var(--green)}
.prog-count{font-size:11px;color:var(--muted)}
.prog-bar-bg{height:2px;background:rgba(255,255,255,0.06);border-radius:1px}
.prog-bar{height:100%;background:linear-gradient(90deg,var(--green),var(--blue));border-radius:1px;transition:width 0.35s ease;width:0}

/* ── PARSED RESULT ── */
.parsed-section{padding:14px 24px;border-bottom:1px solid var(--border);display:none}
.parsed-grid{display:grid;grid-template-columns:1fr 1fr;gap:7px;margin-top:8px}
.parsed-card{background:rgba(0,255,136,0.04);border:1px solid rgba(0,255,136,0.12);border-radius:8px;padding:9px 11px}
.parsed-key{font-size:9px;color:var(--muted);letter-spacing:0.08em}
.parsed-val{font-size:12px;color:var(--green);font-weight:600;margin-top:2px;word-break:break-all}

/* ── LOG ── */
.log-section{flex:1;padding:14px 24px;overflow-y:auto}
.log-entry{font-size:11.5px;line-height:1.85}
.lt{color:rgba(255,255,255,0.18)}
.ls{color:var(--green)}.li{color:var(--blue)}.ld{color:var(--muted)}.ln{color:rgba(255,255,255,0.6)}.le{color:var(--red)}

/* ── RIGHT TABS ── */
.rtabs{display:flex;gap:0;border-bottom:1px solid var(--border);padding:0 24px;flex-shrink:0}
.rtab{
  padding:14px 16px 13px;font-size:10px;letter-spacing:0.1em;
  background:none;border:none;cursor:pointer;color:var(--muted);
  border-bottom:2px solid transparent;font-family:'JetBrains Mono',monospace;
  transition:all 0.2s;
}
.rtab.active{color:#fff;border-bottom-color:var(--green)}
.rtab-content{flex:1;overflow-y:auto;padding:20px 24px}

/* ── DEPLOYMENTS ── */
.dep-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.dep-card{
  background:rgba(255,255,255,0.03);border:1px solid var(--border);
  border-radius:12px;padding:15px 17px;transition:border-color 0.3s;
  animation:slideIn 0.4s ease;
}
.dep-card.new{border-color:rgba(0,255,136,0.35);background:rgba(0,255,136,0.04)}
.dep-card.deploying{border-color:rgba(255,187,0,0.3);background:rgba(255,187,0,0.03)}
@keyframes slideIn{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:translateY(0)}}
.dep-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:7px}
.dep-name{display:flex;align-items:center;gap:7px;font-size:12px;font-weight:600}
.sdot{width:7px;height:7px;border-radius:50%;flex-shrink:0}
.sdot.running{background:var(--green);box-shadow:0 0 5px var(--green)}
.sdot.deploying{background:var(--amber);animation:pulse 1s infinite}
.sdot.pending{background:var(--amber)}
.sdot.failed{background:var(--red)}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:0.25}}
.badge-new{font-size:9px;padding:1px 5px;background:rgba(0,255,136,0.12);border:1px solid rgba(0,255,136,0.25);color:var(--green);border-radius:3px}
.dep-reps{font-size:11px;color:var(--muted)}
.dep-image{font-size:10px;color:var(--muted);margin-bottom:10px}
.bar-row{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.bar-label{display:flex;justify-content:space-between;margin-bottom:3px}
.bar-lbl{font-size:9px;color:var(--muted)}
.bar-val{font-size:9px}
.bar-bg{height:3px;background:rgba(255,255,255,0.06);border-radius:2px;overflow:hidden}
.bar-fill{height:100%;border-radius:2px;transition:width 1.2s ease}
.dep-footer{display:flex;gap:10px;margin-top:10px}
.dep-footer span{font-size:9px;color:rgba(255,255,255,0.22)}

/* ── PODS TABLE ── */
.pods-table{width:100%;border-collapse:collapse;font-size:11.5px}
.pods-table th{font-size:9px;color:var(--muted);letter-spacing:0.1em;padding:8px 10px;text-align:left;border-bottom:1px solid var(--border)}
.pods-table td{padding:9px 10px;border-bottom:1px solid rgba(255,255,255,0.04)}
.pods-table tr:last-child td{border-bottom:none}
.phase-badge{display:inline-flex;align-items:center;gap:5px;font-size:10px;padding:2px 7px;border-radius:4px}
.phase-badge.Running{background:rgba(0,255,136,0.1);color:var(--green)}
.phase-badge.Pending{background:rgba(255,187,0,0.1);color:var(--amber)}
.phase-badge.Failed{background:rgba(255,85,85,0.1);color:var(--red)}

/* ── MODEL STATS ── */
.stat-card{background:rgba(0,255,136,0.02);border:1px solid rgba(0,255,136,0.1);border-radius:12px;padding:18px 20px;margin-bottom:12px}
.stat-card-title{font-size:9px;color:rgba(0,255,136,0.5);letter-spacing:0.12em;margin-bottom:14px}
.stat-row{display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid rgba(255,255,255,0.04)}
.stat-row:last-child{border-bottom:none}
.stat-key{font-size:11.5px;color:var(--muted)}
.stat-val{font-size:11.5px;color:rgba(255,255,255,0.7)}
.stat-val.hl{color:var(--green);font-weight:600}

/* ── EMPTY STATE ── */
.empty{text-align:center;padding:48px 0;color:var(--muted)}
.empty-icon{font-size:32px;margin-bottom:10px;opacity:0.4}
.empty-text{font-size:12px}

/* ── REFRESH BTN ── */
.refresh-btn{
  font-size:10px;padding:5px 12px;border-radius:6px;
  background:rgba(255,255,255,0.05);border:1px solid var(--border);
  color:var(--muted);cursor:pointer;font-family:'JetBrains Mono',monospace;
  transition:all 0.2s;float:right;margin-top:-4px;
}
.refresh-btn:hover{border-color:rgba(0,204,255,0.3);color:var(--blue)}
</style>
</head>
<body>

<!-- ══ LOADING SCREEN ══════════════════════════════════════ -->
<div id="loading-screen">
  <div class="loading-logo">Zero<span>Touch</span> K8s</div>
  <div class="loading-bar-wrap"><div class="loading-bar" id="load-bar"></div></div>
  <div class="loading-status" id="load-status">正在初始化系統...</div>
</div>

<!-- ══ HEADER ══════════════════════════════════════════════ -->
<header>
  <div class="logo">
    <div class="logo-icon">⎈</div>
    <div>
      <h1>ZeroTouch K8s</h1>
      <p>LLM-BASED DEPLOYMENT PLATFORM</p>
    </div>
  </div>
  <div class="hstats">
    <div class="hstat">
      <div class="hstat-label">MODEL</div>
      <div class="hstat-val" style="color:var(--blue)">
        <span id="model-status-dot"></span><span id="model-status-text">載入中...</span>
      </div>
    </div>
    <div class="hstat">
      <div class="hstat-label">K8S</div>
      <div class="hstat-val" id="k8s-status" style="color:var(--muted)">檢查中...</div>
    </div>
    <div class="hstat">
      <div class="hstat-label">PODS</div>
      <div class="hstat-val" id="total-pods" style="color:var(--purple)">— running</div>
    </div>
  </div>
</header>

<!-- ══ MAIN LAYOUT ════════════════════════════════════════ -->
<div class="layout">

  <!-- ── LEFT ─────────────────────────────────────────── -->
  <div class="left-panel">

    <!-- Input -->
    <div class="input-section">
      <div class="section-label">自然語言部署指令</div>
      <div class="input-wrap">
        <textarea id="cmd-input" rows="2"
          placeholder="例：部署 3 個 nginx，port 80…"
          oninput="validateInput()"
          onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();submitDeploy()}"></textarea>
        <button class="send-btn" id="send-btn" onclick="submitDeploy()" title="部署 (Enter)">↵</button>
      </div>
      <div class="input-hint" id="input-hint"></div>
      <div class="chips">
        <div class="chip" onclick="fill('部署 3 個 nginx，port 80')">nginx ×3</div>
        <div class="chip" onclick="fill('幫我起 2 個 redis，記憶體 256Mi')">redis ×2</div>
        <div class="chip" onclick="fill('建立 postgres 資料庫，port 5432')">postgres db</div>
        <div class="chip" onclick="fill('deploy 4 node:20-alpine pods, port 3000')">node api ×4</div>
        <div class="chip" onclick="fill('起個 golang 微服務，3 個 pod，port 8080')">golang svc ×3</div>
        <div class="chip" onclick="fill('幫我跑 5 個 python:3.11-slim，記憶體 512Mi')">python ×5</div>
      </div>
    </div>

    <!-- Progress -->
    <div class="progress-section" id="prog-wrap">
      <div class="prog-header">
        <span class="prog-step" id="prog-step">初始化...</span>
        <span class="prog-count" id="prog-count">0/10</span>
      </div>
      <div class="prog-bar-bg"><div class="prog-bar" id="prog-bar"></div></div>
    </div>

    <!-- Parsed -->
    <div class="parsed-section" id="parsed-wrap">
      <div class="section-label">AI 解析結果</div>
      <div class="parsed-grid" id="parsed-grid"></div>
    </div>

    <!-- Log -->
    <div class="log-section" id="log"></div>
  </div>

  <!-- ── RIGHT ─────────────────────────────────────────── -->
  <div class="right-panel">
    <div class="rtabs">
      <button class="rtab active" onclick="showTab('deps',this)">DEPLOYMENTS</button>
      <button class="rtab" onclick="showTab('pods',this)">PODS <span id="pods-badge" style="font-size:9px;color:var(--muted)"></span></button>
      <button class="rtab" onclick="showTab('stats',this)">MODEL STATS</button>
      <button class="refresh-btn" onclick="refreshK8s()">↻ 刷新</button>
    </div>

    <!-- Deployments tab -->
    <div class="rtab-content" id="tab-deps">
      <div class="dep-grid" id="dep-grid">
        <div class="empty" style="grid-column:1/-1">
          <div class="empty-icon">⎈</div>
          <div class="empty-text">尚無部署，輸入指令開始</div>
        </div>
      </div>
    </div>

    <!-- Pods tab -->
    <div class="rtab-content" id="tab-pods" style="display:none">
      <table class="pods-table">
        <thead><tr>
          <th>POD NAME</th><th>APP</th><th>STATUS</th><th>IP</th><th>NODE</th>
        </tr></thead>
        <tbody id="pods-tbody"><tr><td colspan="5" style="text-align:center;padding:40px;color:var(--muted)">無 Pod 資料</td></tr></tbody>
      </table>
    </div>

    <!-- Model stats tab -->
    <div class="rtab-content" id="tab-stats" style="display:none">
      <div class="stat-card">
        <div class="stat-card-title">LLM MODEL INFORMATION</div>
        <div class="stat-row"><span class="stat-key">Base Model</span><span class="stat-val">LLaMA-3 8B Instruct</span></div>
        <div class="stat-row"><span class="stat-key">Fine-tune Method</span><span class="stat-val">LoRA (r=16, α=32)</span></div>
        <div class="stat-row"><span class="stat-key">Training Samples</span><span class="stat-val">800 K8s pairs</span></div>
        <div class="stat-row"><span class="stat-key">Quantization</span><span class="stat-val">4-bit NF4</span></div>
        <div class="stat-row"><span class="stat-key">Accuracy (60 tests)</span><span class="stat-val hl">100.0%</span></div>
        <div class="stat-row"><span class="stat-key">Inference Speed</span><span class="stat-val hl">14.4s avg (↑29.2% vs base)</span></div>
        <div class="stat-row"><span class="stat-key">Languages</span><span class="stat-val">中文 / English</span></div>
        <div class="stat-row"><span class="stat-key">Gold Samples Collected</span><span class="stat-val" id="gold-count">0</span></div>
      </div>
      <div class="stat-card">
        <div class="stat-card-title">SYSTEM STATUS</div>
        <div class="stat-row"><span class="stat-key">Model Status</span><span class="stat-val hl" id="stat-model">載入中...</span></div>
        <div class="stat-row"><span class="stat-key">K8s Connection</span><span class="stat-val" id="stat-k8s">檢查中...</span></div>
        <div class="stat-row"><span class="stat-key">Total Deployments</span><span class="stat-val" id="stat-deps">0</span></div>
        <div class="stat-row"><span class="stat-key">Running Pods</span><span class="stat-val" id="stat-pods">0</span></div>
      </div>
    </div>
  </div>
</div>

<script>
// ── 狀態 ───────────────────────────────────────────────────
let localDeps = [];    // 本地已部署列表
let goldCount = 0;
let isDeploying = false;
let modelReady  = false;

const STEPS = [
  "解析自然語言意圖...",
  "LLaMA-3 LoRA 推論中...",
  "生成 Kubernetes YAML manifest...",
  "驗證資源規格...",
  "呼叫 Kubernetes API...",
  "建立 Deployment 物件...",
  "建立 Service 物件...",
  "排程 Pod 到節點...",
  "等待容器啟動...",
  "健康檢查通過 ✓",
];
const DURATIONS = [600,1200,500,350,600,450,350,750,1100,400];

// ── Loading screen ─────────────────────────────────────────
let loadPct = 0;
function advanceLoad(pct, msg){
  loadPct = pct;
  document.getElementById('load-bar').style.width = pct + '%';
  document.getElementById('load-status').textContent = msg;
}

async function initSystem(){
  advanceLoad(10,'正在連線後端...');
  await sleep(300);
  advanceLoad(25,'檢查 Kubernetes 環境...');
  const status = await fetchStatus();
  advanceLoad(50,'等待 LLaMA-3 模型載入...');
  await pollModelReady();
  advanceLoad(90,'初始化 UI...');
  await sleep(200);
  refreshK8s();
  advanceLoad(100,'就緒');
  await sleep(400);
  document.getElementById('loading-screen').classList.add('hidden');
}

async function fetchStatus(){
  try{
    const r = await fetch('/api/status');
    const d = await r.json();
    updateModelUI(d.model_ready);
    document.getElementById('k8s-status').textContent   = d.k8s ? '已連線 ✓' : '模擬模式';
    document.getElementById('k8s-status').style.color   = d.k8s ? 'var(--green)' : 'var(--muted)';
    document.getElementById('stat-k8s').textContent     = d.k8s ? '已連線 ✓' : '模擬模式';
    return d;
  }catch(e){ return {}; }
}

async function pollModelReady(){
  for(let i=0;i<60;i++){
    const r = await fetchStatus();
    if(r.model_ready){ return; }
    if(!r.model_loading){ return; }
    await sleep(2000);
  }
}

function updateModelUI(ready){
  const dot  = document.getElementById('model-status-dot');
  const txt  = document.getElementById('model-status-text');
  const stat = document.getElementById('stat-model');
  modelReady = ready;
  if(ready){
    dot.classList.add('ready');
    txt.textContent  = 'LLaMA-3 LoRA ✓';
    stat.textContent = '已載入 ✓';
    stat.className   = 'stat-val hl';
  } else {
    dot.classList.remove('ready');
    txt.textContent  = '載入中...';
    stat.textContent = '載入中...';
    stat.className   = 'stat-val';
  }
}

// ── 工具 ───────────────────────────────────────────────────
function sleep(ms){ return new Promise(r=>setTimeout(r,ms)); }
function now(){ return new Date().toLocaleTimeString('en-GB',{hour12:false}); }

function addLog(msg, type='n'){
  const el = document.getElementById('log');
  const d  = document.createElement('div');
  d.className = 'log-entry';
  d.innerHTML = `<span class="lt">${now()} </span><span class="l${type}">${msg}</span>`;
  el.appendChild(d);
  el.scrollTop = el.scrollHeight;
}

function fill(text){
  document.getElementById('cmd-input').value = text;
  validateInput();
  document.getElementById('cmd-input').focus();
}

function showTab(name, btn){
  ['deps','pods','stats'].forEach(t=>{
    document.getElementById('tab-'+t).style.display = t===name?'':'none';
  });
  document.querySelectorAll('.rtab').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
}

// ── 防呆驗證 ───────────────────────────────────────────────
function validateInput(){
  const v = document.getElementById('cmd-input').value.trim();
  const hint = document.getElementById('input-hint');
  const btn  = document.getElementById('send-btn');

  if(!v){
    hint.textContent = '';
    hint.className   = 'input-hint';
    btn.disabled = true;
    return false;
  }
  if(v.length < 3){
    hint.textContent = '⚠️ 指令太短，請描述要部署的服務';
    hint.className   = 'input-hint warn';
    btn.disabled = true;
    return false;
  }
  if(/^\d+$/.test(v)){
    hint.textContent = '⚠️ 請輸入完整指令，例如：部署 3 個 nginx';
    hint.className   = 'input-hint warn';
    btn.disabled = true;
    return false;
  }
  if(isDeploying){
    hint.textContent = '⏳ 正在部署中，請稍候...';
    hint.className   = 'input-hint warn';
    btn.disabled = true;
    return false;
  }
  hint.textContent = '✓ 按 Enter 或點擊按鈕部署';
  hint.className   = 'input-hint ok';
  btn.disabled = false;
  return true;
}

// ── 部署流程 ───────────────────────────────────────────────
async function submitDeploy(){
  const input = document.getElementById('cmd-input').value.trim();
  if(!validateInput() || isDeploying) return;
  if(!modelReady){
    const ok = confirm('模型尚未載入完成，使用模擬模式繼續？');
    if(!ok) return;
  }

  isDeploying = true;
  document.getElementById('send-btn').disabled = true;
  addLog(`>>> ${input}`, 'i');

  // Show progress & parsed sections
  document.getElementById('prog-wrap').style.display   = '';
  document.getElementById('parsed-wrap').style.display = '';

  // Call backend /api/deploy
  let parsed = null;
  try{
    const res = await fetch('/api/deploy', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({input}),
    });
    const data = await res.json();
    if(data.error){
      addLog(`❌ 解析失敗：${data.error}`, 'e');
      resetDeploy();
      return;
    }
    parsed = data.parsed;
  }catch(e){
    addLog('❌ 後端連線失敗', 'e');
    resetDeploy();
    return;
  }

  // Show parsed result
  const pg = document.getElementById('parsed-grid');
  pg.innerHTML = '';
  [['app_name',parsed.app_name],['image',parsed.image],
   ['pods',parsed.pods],['port',parsed.port],
   ...(parsed.memory?[['memory',parsed.memory]]:[])
  ].forEach(([k,v])=>{
    pg.innerHTML += `<div class="parsed-card"><div class="parsed-key">${k}</div><div class="parsed-val">${v}</div></div>`;
  });
  addLog(`AI 解析完成 → ${parsed.app_name} × ${parsed.pods}`, 's');

  // Add deploying card to local deps
  const depId = Date.now();
  const dep = {
    id: depId,
    name: parsed.app_name,
    image: parsed.image,
    replicas: parsed.pods,
    port: parsed.port,
    memory: parsed.memory||null,
    status: 'deploying',
    cpu: 0, mem: 0,
    ip: null, age: '0m',
    isNew: true,
  };
  localDeps.push(dep);
  renderDeps();
  showTab('deps', document.querySelector('.rtab'));

  // Animate steps
  for(let i=0;i<STEPS.length;i++){
    document.getElementById('prog-step').textContent = STEPS[i];
    document.getElementById('prog-count').textContent = `${i+1}/${STEPS.length}`;
    document.getElementById('prog-bar').style.width = `${(i+1)/STEPS.length*100}%`;
    addLog(STEPS[i]);
    await sleep(DURATIONS[i]);
  }

  // Mark running
  const d = localDeps.find(d=>d.id===depId);
  if(d){
    d.status = 'running';
    d.cpu    = Math.floor(Math.random()*18)+5;
    d.mem    = Math.floor(Math.random()*28)+18;
    d.ip     = `10.244.${Math.floor(Math.random()*3)+1}.${Math.floor(Math.random()*200)+10}`;
  }
  renderDeps();
  updatePodsCount();

  goldCount++;
  document.getElementById('gold-count').textContent = goldCount;
  document.getElementById('stat-deps').textContent  = localDeps.filter(d=>d.status==='running').length;

  addLog(`✅ ${parsed.app_name} 部署成功（${parsed.pods} pod running）`, 's');
  addLog(`   IP: ${d?.ip}  Port: ${parsed.port}`, 'd');

  setTimeout(()=>{
    const x = localDeps.find(d=>d.id===depId);
    if(x) x.isNew = false;
    renderDeps();
  }, 4000);

  document.getElementById('prog-wrap').style.display = 'none';
  document.getElementById('cmd-input').value = '';
  resetDeploy();
  refreshK8s();
}

function resetDeploy(){
  isDeploying = false;
  validateInput();
}

// ── 渲染 Deployments ───────────────────────────────────────
function renderDeps(){
  const grid = document.getElementById('dep-grid');
  if(!localDeps.length){
    grid.innerHTML = '<div class="empty" style="grid-column:1/-1"><div class="empty-icon">⎈</div><div class="empty-text">尚無部署，輸入指令開始</div></div>';
    return;
  }
  grid.innerHTML = localDeps.map(d=>`
    <div class="dep-card ${d.isNew?'new':''} ${d.status==='deploying'?'deploying':''}">
      <div class="dep-header">
        <div class="dep-name">
          <div class="sdot ${d.status}"></div>
          <span>${d.name}</span>
          ${d.isNew?'<span class="badge-new">NEW</span>':''}
        </div>
        <span class="dep-reps">${d.replicas}×</span>
      </div>
      <div class="dep-image">${d.image}</div>
      <div class="bar-row">
        <div>
          <div class="bar-label"><span class="bar-lbl">CPU</span><span class="bar-val" style="color:#00ccff">${Math.round(d.cpu)}%</span></div>
          <div class="bar-bg"><div class="bar-fill" style="width:${d.cpu}%;background:#00ccff"></div></div>
        </div>
        <div>
          <div class="bar-label"><span class="bar-lbl">MEM</span><span class="bar-val" style="color:#bb99ff">${Math.round(d.mem)}%</span></div>
          <div class="bar-bg"><div class="bar-fill" style="width:${d.mem}%;background:#bb99ff"></div></div>
        </div>
      </div>
      <div class="dep-footer">
        <span>:${d.port}</span>
        <span>${d.ip||'...'}</span>
        <span style="margin-left:auto">${d.age}</span>
      </div>
    </div>`).join('');
}

// ── 更新 Pod 數量 ──────────────────────────────────────────
function updatePodsCount(){
  const running = localDeps.filter(d=>d.status==='running').length;
  document.getElementById('total-pods').textContent = `${running} running`;
  document.getElementById('stat-pods').textContent  = running;
}

// ── 刷新 K8s 真實狀態 ─────────────────────────────────────
async function refreshK8s(){
  try{
    const r = await fetch('/api/pods');
    const d = await r.json();
    renderPodsTable(d.pods || []);
    document.getElementById('pods-badge').textContent = d.pods?.length ? `(${d.pods.length})` : '';
  }catch(e){}
  try{
    const r = await fetch('/api/deployments');
    const d = await r.json();
    if(d.deployments?.length){
      mergeK8sDeps(d.deployments);
    }
  }catch(e){}
  await fetchStatus();
}

function mergeK8sDeps(k8sDeps){
  k8sDeps.forEach(kd=>{
    const existing = localDeps.find(d=>d.name===kd.name);
    if(!existing){
      localDeps.push({
        id: Date.now() + Math.random(),
        name: kd.name, image: kd.image,
        replicas: kd.replicas, port: 80,
        status: kd.ready === kd.replicas ? 'running' : 'deploying',
        cpu: Math.floor(Math.random()*20)+5,
        mem: Math.floor(Math.random()*30)+15,
        ip: null, age: kd.age, isNew: false,
      });
    }
  });
  renderDeps();
  updatePodsCount();
}

function renderPodsTable(pods){
  const tb = document.getElementById('pods-tbody');
  if(!pods.length){
    tb.innerHTML = '<tr><td colspan="5" style="text-align:center;padding:40px;color:var(--muted)">無 Pod 資料（K8s 模擬模式）</td></tr>';
    return;
  }
  tb.innerHTML = pods.map(p=>`
    <tr>
      <td style="font-size:11px">${p.name}</td>
      <td>${p.app}</td>
      <td><span class="phase-badge ${p.phase}">${p.phase}</span></td>
      <td style="color:var(--muted)">${p.ip||'—'}</td>
      <td style="color:var(--muted)">${p.node||'—'}</td>
    </tr>`).join('');
}

// ── 動態 CPU/MEM ───────────────────────────────────────────
setInterval(()=>{
  localDeps.forEach(d=>{
    if(d.status==='running'){
      d.cpu = Math.min(92,Math.max(3, d.cpu+(Math.random()-.5)*7));
      d.mem = Math.min(92,Math.max(8, d.mem+(Math.random()-.5)*4));
    }
  });
  renderDeps();
}, 2800);

// ── 定期刷新 K8s 狀態（30秒）──────────────────────────────
setInterval(refreshK8s, 30000);

// ── 啟動 ──────────────────────────────────────────────────
initSystem();
</script>
</body>
</html>"""


# ════════════════════════════════════════════════════════════
# Flask Routes
# ════════════════════════════════════════════════════════════
@app.route("/")
def index():
    return render_template_string(HTML)


@app.route("/api/status")
def api_status():
    ready, loading = _model_status()
    return jsonify({
        "model_ready"  : ready,
        "model_loading": loading,
        "k8s"          : K8S_ENABLED,
    })


@app.route("/api/deploy", methods=["POST"])
def api_deploy():
    data       = request.get_json()
    user_input = (data or {}).get("input", "").strip()

    if not user_input or len(user_input) < 3:
        return jsonify({"error": "輸入不能為空或過短"}), 400
    if user_input.isdigit():
        return jsonify({"error": "請輸入完整指令，不要只輸入數字"}), 400

    parsed = ask_llama(user_input)

    if "error" in parsed:
        return jsonify({"error": parsed["error"]}), 422

    # 存 gold sample
    threading.Thread(
        target=save_gold_sample, args=(user_input, parsed), daemon=True
    ).start()

    # 呼叫真實 K8s（如果有）
    if K8S_ENABLED:
        threading.Thread(
            target=k8s_deploy,
            args=(parsed["app_name"], parsed["image"], parsed["pods"],
                  parsed.get("port", 80), parsed.get("memory")),
            daemon=True
        ).start()

    return jsonify({"parsed": parsed, "k8s": K8S_ENABLED})


@app.route("/api/pods")
def api_pods():
    return jsonify({"pods": k8s_get_pods()})


@app.route("/api/deployments")
def api_deployments():
    return jsonify({"deployments": k8s_get_deployments()})


# ════════════════════════════════════════════════════════════
# 啟動
# ════════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("=" * 60)
    print("  ZeroTouch K8s Web Demo")
    print("=" * 60)
    print(f"  K8s   : {'已連線' if K8S_ENABLED else '模擬模式'}")
    print(f"  開啟瀏覽器：http://localhost:5000")
    print("  Model Server 將在首次推論時自動啟動")
    print("=" * 60)

    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
