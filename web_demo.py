"""
web_demo.py  —  ZeroTouch K8s Platform v2
Clean white UI + Login/Register + Pod Details + AI Chat + Real K8s
"""
import sys, os, shutil
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core.config import ensure_utf8_output
ensure_utf8_output()

import threading, json, urllib.request, hashlib, secrets, re, uuid
from datetime import datetime, timezone, timedelta
from flask import Flask, request, jsonify, render_template_string, session, redirect, url_for

from core.config import ROOT, YAML_DIR, MODEL_SERVER_URL
from llama_client import ask_llama, save_gold_sample, chat_llama, classify_intent
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

def _check_docker():
    """檢查 Docker daemon 是否真的在跑（不是只看 kubeconfig 存不存在）。
    給新手看的訊息，所以區分「Docker 沒開」跟「Docker 開了但 K8s 連不上」兩種情況。"""
    import subprocess
    try:
        r = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"],
                           capture_output=True, text=True, timeout=4)
        if r.returncode == 0 and r.stdout.strip():
            return True, ""
        return False, "Docker 指令跑得動，但 daemon 沒回應，請確認 Docker Desktop 已完全啟動。"
    except FileNotFoundError:
        return False, "找不到 docker 指令，請先安裝 Docker Desktop。"
    except subprocess.TimeoutExpired:
        return False, "Docker 沒有回應（逾時），可能還在啟動中，請稍候再重新整理。"
    except Exception as e:
        return False, f"檢查 Docker 狀態時發生錯誤：{e}"


def _check_k8s_live():
    """即時探測 K8s API 是否連得上（K8S_ENABLED 只在啟動時檢查一次，這裡每次 /api/status 都重測，
    避免 Docker/K8s 中途掛掉卻一直顯示 Connected）。"""
    if not K8S_ENABLED:
        return False
    try:
        k8s_client.CoreV1Api().list_namespace(_request_timeout=(2, 3), limit=1)
        return True
    except Exception:
        return False


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


def k8s_deploy(app_name, image, replicas, port=80, memory=None, cpu=None, namespace=None):
    namespace = namespace or NS
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
        # 使用者沒填 memory/cpu 時，過去完全不設限制（一個 Pod 有機會吃光整台節點資源）。
        # 改成套用 agents/cost_agent 既有的「依 image 類型推薦資源」規則（跟審查卡上顯示
        # 給使用者看的建議是同一份 profile，不會兩邊數字不一致）；使用者有指定的維度就照使用者的，
        # 沒指定的維度才補預設值。
        try:
            from agents.cost_agent import _APP_PROFILES, _detect_app_type
            _profile = _APP_PROFILES.get(_detect_app_type(image, app_name), _APP_PROFILES["default"])
            _req_cpu, _req_mem, _lim_cpu, _lim_mem = _profile
        except Exception:
            _req_cpu, _req_mem, _lim_cpu, _lim_mem = 100, 128, 500, 256
        cpu_req = cpu or f"{_req_cpu}m"
        cpu_lim = cpu or f"{_lim_cpu}m"
        mem_req = memory or f"{_req_mem}Mi"
        mem_lim = memory or f"{_lim_mem}Mi"
        resources = k8s_client.V1ResourceRequirements(
            requests={"memory": mem_req, "cpu": cpu_req},
            limits={"memory": mem_lim, "cpu": cpu_lim},
        )
        container = k8s_client.V1Container(
            name=app_name, image=image,
            ports=[k8s_client.V1ContainerPort(container_port=port)],
            resources=resources,
            # 安全性最低硬化：擋掉容器內程序取得比父程序更高的權限。不強制 runAsNonRoot/
            # 丟棄 capabilities，因為這個平台常部署的官方 image（nginx 綁 80 port 等）預設
            # 用 root 執行，硬上非 root 反而會讓現有 demo 流程直接壞掉。
            security_context=k8s_client.V1SecurityContext(allow_privilege_escalation=False),
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
            api.replace_namespaced_deployment(app_name, namespace, deploy, _request_timeout=(5, 10))
        except Exception:
            api.create_namespaced_deployment(namespace, deploy, _request_timeout=(5, 10))
        try:
            core.replace_namespaced_service(f"{app_name}-svc", namespace, svc, _request_timeout=(5, 10))
        except Exception:
            core.create_namespaced_service(namespace, svc, _request_timeout=(5, 10))
        os.makedirs(YAML_DIR, exist_ok=True)
        path = os.path.join(YAML_DIR, f"{app_name}.yaml")
        with open(path, "w", encoding="utf-8") as f:
            f.write(yaml_lib.dump(deploy.to_dict()))
            f.write("---\n")
            f.write(yaml_lib.dump(svc.to_dict()))
        return True, f"已部署 {app_name}"
    except Exception as e:
        return False, str(e)

def k8s_get_pods(app_name=None, namespace=None):
    namespace = namespace or NS
    if not K8S_ENABLED:
        return []
    try:
        core = k8s_client.CoreV1Api()
        selector = f"app={app_name}" if app_name else None
        pods = core.list_namespaced_pod(namespace, label_selector=selector)
        # 2026-09-15：K8s API 回傳的順序沒有保證，使用者要求「最新部署的排最上面」，
        # 用真正的 creation_timestamp（不是後面才格式化成字串的 age）排序，前端分頁
        # 才不用自己再排一次。
        items = sorted(pods.items,
                        key=lambda p: p.metadata.creation_timestamp.timestamp() if p.metadata.creation_timestamp else 0,
                        reverse=True)
        result = []
        for p in items:
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

def k8s_get_deployments(namespace=None):
    namespace = namespace or NS
    if not K8S_ENABLED:
        return []
    try:
        api = k8s_client.AppsV1Api()
        core = k8s_client.CoreV1Api()
        deps = api.list_namespaced_deployment(namespace)
        # 2026-09-15：跟 k8s_get_pods 一樣，改用真正的 creation_timestamp 排序，
        # 最新部署的排最上面，不用前端再排一次。
        deps_items = sorted(deps.items,
                             key=lambda d: d.metadata.creation_timestamp.timestamp() if d.metadata.creation_timestamp else 0,
                             reverse=True)
        result = []
        for d in deps_items:
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
                    pods = core.list_namespaced_pod(namespace, label_selector=selector)
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


def k8s_get_services(namespace=None, all_namespaces=False):
    """列出目前所有 LoadBalancer Service 的 {name, app, port}，給部署前的 port 衝突檢查用。
    Docker Desktop 的 K8s 一個 port 只能真的綁一個 LoadBalancer Service 到 localhost，
    兩個服務搶同一個 port 時，其中一個會卡在 external-ip pending、瀏覽器連不到——這是
    host 層級的實體限制，不因為每個帳號有自己的 namespace 就消失，所以 port 衝突檢查
    要跨所有 namespace 查（all_namespaces=True），不能只看自己的 namespace。"""
    namespace = namespace or NS
    if not K8S_ENABLED:
        return []
    try:
        core = k8s_client.CoreV1Api()
        result = []
        items = (core.list_service_for_all_namespaces().items if all_namespaces
                 else core.list_namespaced_service(namespace).items)
        for s in items:
            if s.spec.type != "LoadBalancer":
                continue
            app = (s.spec.selector or {}).get("app", "")
            for p in (s.spec.ports or []):
                result.append({"name": s.metadata.name, "app": app, "port": p.port})
        return result
    except Exception:
        return []


def k8s_get_node_capacity():
    """回傳叢集裡「單一節點」最大的可配置 CPU/記憶體（取 allocatable 最大的那個節點）。
    給部署前檢查「這個 pod 的資源需求有沒有大到連一個節點都放不下」用——這種情況跟
    「需要幾個節點」不一樣，是不管幾個節點都不會排程成功，Pod 會卡在 Pending 永遠不會動，
    但 k8s_deploy() 建立 Deployment/Service 物件本身還是會回報成功，使用者看不出來。
    回傳 None 代表沒連上 K8s 或查不到節點，呼叫端應該跳過這項檢查而不是誤判。"""
    if not K8S_ENABLED:
        return None
    try:
        core = k8s_client.CoreV1Api()
        nodes = core.list_node().items
        if not nodes:
            return None
        from agents.cost_agent import _parse_memory_bytes
        best = max(nodes, key=lambda n: _parse_memory_bytes(n.status.allocatable.get("memory", "0")) or 0)
        alloc = best.status.allocatable
        return {"cpu": alloc.get("cpu", "0"), "memory": alloc.get("memory", "0Ki")}
    except Exception:
        return None


def _user_namespace(username: str) -> str:
    """把帳號名稱轉成合法的 K8s namespace 名稱（DNS-1123 label：小寫字母數字+連字號，
    開頭結尾不能是連字號，長度上限 63）。每個帳號固定對應一個 namespace，讓
    Pod/Deployment/Chat 等資源天然互相隔離（K8s 層級的隔離，不是 UI 濾掉而已）。"""
    safe = re.sub(r"[^a-z0-9-]", "-", (username or "").lower()).strip("-")[:50]
    return f"user-{safe or 'unknown'}"


def _ensure_user_namespace(namespace: str):
    """namespace 不存在就建立；已存在直接跳過。刻意設計成「隨時呼叫都安全」而不是
    只在註冊時做一次——即使註冊當下 K8s 剛好斷線，之後第一次真的要部署時還是會
    自動補建，不會讓使用者卡住（符合「不能靜默失敗」但也不能「因為當下環境沒設好
    就整個功能壞掉」的專案原則）。"""
    if not K8S_ENABLED:
        return
    try:
        core = k8s_client.CoreV1Api()
        try:
            core.read_namespace(namespace)
        except Exception:
            core.create_namespace(k8s_client.V1Namespace(
                metadata=k8s_client.V1ObjectMeta(name=namespace)))
    except Exception:
        pass  # 建立失敗（例如剛好重複建立的 race）不阻擋操作，讓後續呼叫自然報錯


def _save_users():
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "users.json"), "w", encoding="utf-8") as _uf:
        json.dump(USERS, _uf)


def _record_violation(username: str) -> bool:
    """記一次惡意誘導/操縱行為違規，回傳這次是否觸發封鎖（累積滿 3 次）。"""
    user = USERS.get(username)
    if not isinstance(user, dict):
        return False
    user["violation_count"] = user.get("violation_count", 0) + 1
    if user["violation_count"] >= 3:
        _ban_user(username)
        _save_users()
        return True
    _save_users()
    return False


def _ban_user(username: str):
    """標記帳號為封鎖，並刪除該帳號的 K8s namespace（連同裡面所有 Pod/Deployment/
    Service 一次清掉）。故意不動 git 裡 manifests/<namespace>/ 底下的歷史檔案——
    那些是 GitOps 的稽核軌跡，只刪除叢集裡活著的資源。"""
    user = USERS.get(username)
    if isinstance(user, dict):
        user["banned"] = True
    if K8S_ENABLED:
        try:
            k8s_client.CoreV1Api().delete_namespace(_user_namespace(username))
        except Exception:
            pass


def _check_scale_risk(name: str, new_replicas: int, namespace: str = None):
    """在真的調整 replicas 前先檢查：整個叢集（所有 namespace 的所有 Deployment 加總）
    在套用這個新 replicas 之後，資源需求會不會超出最大節點的容量。跟部署時的單一 Pod
    檢查是不同層次的風險——這裡是「單個 Pod 都放得下，但疊加起來的總量放不下」，超出的
    那些副本會卡在 Pending 永遠排不進去，操作本身不會報錯，使用者不會馬上發現。

    故意查「全叢集所有 namespace」而不是只查目前使用者自己的 namespace——每個帳號
    有自己的 namespace 是為了隔離「看得到誰的東西」，但節點的實體資源（CPU/記憶體）
    是所有 namespace 共用的，A 使用者部署時如果只看自己的 namespace，會忽略掉
    B 使用者已經用掉的資源，兩個人都以為自己還有空間、疊加起來真的把節點塞爆。

    回傳 (blocked: bool, message: str|None)；查不到容量或沒有設資源限制時直接放行
    （不誤判），因為這只是「操作前的風險提醒」，不是唯一的安全網。"""
    namespace = namespace or NS
    try:
        real_capacity = k8s_get_node_capacity()
        if not real_capacity or not K8S_ENABLED:
            return False, None
        from agents.cost_agent import _parse_cpu_millicores, _parse_memory_bytes
        node_cpu_mc = _parse_cpu_millicores(real_capacity.get("cpu")) or 0
        node_mem_b = _parse_memory_bytes(real_capacity.get("memory")) or 0
        if not node_cpu_mc and not node_mem_b:
            return False, None
        apps_api = k8s_client.AppsV1Api()
        deployments = apps_api.list_deployment_for_all_namespaces().items
        total_cpu_mc = 0
        total_mem_b = 0
        for d in deployments:
            containers = d.spec.template.spec.containers if d.spec.template.spec else []
            if not containers or not containers[0].resources or not containers[0].resources.requests:
                continue
            is_target = d.metadata.name == name and d.metadata.namespace == namespace
            reps = new_replicas if is_target else (d.spec.replicas or 1)
            req = containers[0].resources.requests
            total_cpu_mc += (_parse_cpu_millicores(req.get("cpu")) or 0) * reps
            total_mem_b += (_parse_memory_bytes(req.get("memory")) or 0) * reps
        over_cpu = node_cpu_mc and total_cpu_mc > node_cpu_mc
        over_mem = node_mem_b and total_mem_b > node_mem_b
        if over_cpu or over_mem:
            return True, (
                f"這個操作會讓整個叢集的資源需求超出節點容量：套用後全部 Deployment 合計約需要 "
                f"{total_cpu_mc}m CPU / {total_mem_b // (1024**2)}Mi 記憶體，"
                f"但叢集最大節點只有 {node_cpu_mc}m CPU / {node_mem_b // (1024**2)}Mi 可用。"
                f"多出來排不進去的 Pod 會卡在 Pending 狀態，不會顯示錯誤，容易被忽略。"
                f"已阻止這次操作，請降低副本數或先移除/縮小其他部署。 / "
                f"This would push total cluster resource demand past the largest node's capacity — "
                f"the extra pods would get stuck Pending silently. Blocked; please lower the replica "
                f"count or scale down other deployments first."
            )
        return False, None
    except Exception:
        return False, None


_BAD_WAITING = {"CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull",
                "CreateContainerConfigError", "CreateContainerError", "InvalidImageName"}
_BAD_TERMINATED = {"OOMKilled", "Error", "ContainerCannotRun", "DeadlineExceeded"}


def _resolve_pods(name, namespace=None):
    """把使用者給的名字解析成一組 pod。可能是完整 pod 名、app label、或名稱前綴。"""
    namespace = namespace or NS
    if not K8S_ENABLED or not name:
        return []
    core = k8s_client.CoreV1Api()
    try:
        p = core.read_namespaced_pod(name, namespace)
        return [p]
    except Exception:
        pass
    try:
        pods = core.list_namespaced_pod(namespace, label_selector=f"app={name}")
        if pods.items:
            return pods.items
    except Exception:
        pass
    try:
        allp = core.list_namespaced_pod(namespace)
        pref = [p for p in allp.items if p.metadata.name.startswith(name)]
        return pref
    except Exception:
        return []


def _pod_detail(p):
    """單一 pod 的詳細狀態 + 健康判定。p 是 V1Pod。"""
    core = k8s_client.CoreV1Api()
    cs_by_name = {cs.name: cs for cs in (p.status.container_statuses or [])} if p.status else {}
    containers = []
    unhealthy_reason = None
    for c in (p.spec.containers or []):
        cs = cs_by_name.get(c.name)
        state, reason, message, last_reason = "unknown", "", "", ""
        ready, restarts = False, 0
        if cs:
            ready = bool(cs.ready)
            restarts = cs.restart_count or 0
            st = cs.state
            if st and st.running:
                state = "running"
            elif st and st.waiting:
                state, reason, message = "waiting", st.waiting.reason or "", (st.waiting.message or "")[:200]
            elif st and st.terminated:
                state = "terminated"
                reason, message = st.terminated.reason or "", (st.terminated.message or "")[:200]
            if cs.last_state and cs.last_state.terminated:
                last_reason = cs.last_state.terminated.reason or ""
        currently_ok = (state == "running" and ready)
        note = ""
        if not currently_ok and (reason in _BAD_WAITING or reason in _BAD_TERMINATED):
            unhealthy_reason = f"{c.name}: {reason}"
        elif not currently_ok and last_reason in _BAD_TERMINATED:
            unhealthy_reason = f"{c.name}: 上次因 {last_reason} 終止，尚未恢復"
        elif currently_ok and restarts >= 5:
            note = f"{c.name} 目前正常但曾重啟 {restarts} 次" + (f"（上次 {last_reason}）" if last_reason else "")
        containers.append({
            "name": c.name, "image": c.image, "ready": ready, "restart_count": restarts,
            "note": note,
            "state": state, "reason": reason, "message": message, "last_reason": last_reason,
            "requests": dict(c.resources.requests) if c.resources and c.resources.requests else {},
            "limits": dict(c.resources.limits) if c.resources and c.resources.limits else {},
        })
    phase = (p.status.phase if p.status else "") or "Unknown"
    all_ready = bool(containers) and all(x["ready"] for x in containers)
    healthy = phase in ("Running", "Succeeded") and all_ready and unhealthy_reason is None
    notes = [x["note"] for x in containers if x.get("note")]
    if healthy:
        summary = f"Running，{len(containers)} 個容器都 ready"
        if notes:
            summary += "（" + "；".join(notes) + "）"
    elif unhealthy_reason:
        summary = unhealthy_reason
    else:
        summary = f"phase={phase}" + ("" if all_ready else "，容器尚未全部 ready")
    # 事件
    events = []
    try:
        evs = core.list_namespaced_event(
            p.metadata.namespace, field_selector=f"involvedObject.name={p.metadata.name}")
        ev_sorted = sorted(evs.items, key=lambda e: (e.last_timestamp or e.event_time or p.metadata.creation_timestamp), reverse=True)
        for e in ev_sorted[:5]:
            events.append({"type": e.type, "reason": e.reason,
                           "message": (e.message or "")[:200], "count": e.count or 1})
    except Exception:
        pass
    return {
        "name": p.metadata.name,
        "app": (p.metadata.labels or {}).get("app", ""),
        "phase": phase,
        "node": (p.spec.node_name or "") if p.spec else "",
        "ip": (p.status.pod_ip or "") if p.status else "",
        "age": _fmt_k8s_time(p.metadata.creation_timestamp),
        "restarts": sum(x["restart_count"] for x in containers),
        "containers": containers,
        "events": events,
        "healthy": healthy,
        "health_summary": summary,
    }


def k8s_describe_pod(name, namespace=None):
    """回傳一或多個符合 name 的 pod 詳情（list）。找不到回空 list。"""
    return [_pod_detail(p) for p in _resolve_pods(name, namespace)]


def _pod_real_usage(namespace, pod_name):
    """查詢 Pod 實際 CPU/記憶體用量（來自真實部署的 kube-prometheus-stack，
    2026-09-15 起才真的接上）。Prometheus 不可用時回傳 available=False，
    前端要顯示「無法取得」而不是留空白或誤導成 0（不能靜默失敗）。"""
    try:
        from observability.prometheus_client import PrometheusClient
        prom_url = os.environ.get("PROMETHEUS_URL", "http://127.0.0.1:9090").replace("localhost", "127.0.0.1")
        client = PrometheusClient(prom_url, timeout=2)
        if not client.is_alive():
            return {"available": False, "cpu_cores": None, "memory_mi": None}
        return {
            "available": True,
            "cpu_cores": client.pod_cpu_usage(pod_name, namespace),
            "memory_mi": client.pod_memory_usage_mi(pod_name, namespace),
        }
    except Exception:
        return {"available": False, "cpu_cores": None, "memory_mi": None}


def k8s_describe_deployment(name, namespace=None):
    """單一 deployment 的詳細狀態。找不到回 None。"""
    namespace = namespace or NS
    if not K8S_ENABLED or not name:
        return None
    try:
        api = k8s_client.AppsV1Api()
        d = api.read_namespaced_deployment(name, namespace)
    except Exception:
        # 名字可能是 app label 或前綴，退回列表比對
        for dd in k8s_get_deployments(namespace):
            if dd["name"] == name or dd["name"].startswith(name):
                return dd
        return None
    st = d.status
    spec_replicas = d.spec.replicas or 0
    ready = (st.ready_replicas or 0) if st else 0
    available = (st.available_replicas or 0) if st else 0
    updated = (st.updated_replicas or 0) if st else 0
    conds = []
    if st and st.conditions:
        for c in st.conditions:
            conds.append({"type": c.type, "status": c.status,
                          "reason": c.reason or "", "message": (c.message or "")[:160]})
    healthy = ready == spec_replicas and spec_replicas > 0
    return {
        "name": d.metadata.name,
        "image": d.spec.template.spec.containers[0].image if d.spec.template.spec.containers else "",
        "replicas": spec_replicas, "ready": ready, "available": available,
        "updated": updated, "unavailable": max(0, spec_replicas - available),
        "age": _fmt_k8s_time(d.metadata.creation_timestamp),
        "conditions": conds,
        "healthy": healthy,
        "health_summary": (f"{ready}/{spec_replicas} ready" if healthy
                           else f"只有 {ready}/{spec_replicas} ready，{max(0, spec_replicas - available)} 個不可用"),
    }


def k8s_delete_deployment(app_name, namespace=None):
    namespace = namespace or NS
    if not K8S_ENABLED:
        return False, "K8s 未連線"
    try:
        api  = k8s_client.AppsV1Api()
        core = k8s_client.CoreV1Api()
        api.delete_namespaced_deployment(app_name, namespace)
        try:
            core.delete_namespaced_service(f"{app_name}-svc", namespace)
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

    # 使用者輸入本身可不可行的檢查：名稱撞名、port 撞別的服務。
    # 這兩個是 2026-09-13 真的在這台機器上遇到的問題（web-frontend/auto-app/zt-smoke
    # 三個都要 port 80，Docker Desktop 一個 port 只能真的綁一個 LoadBalancer，
    # 其中一個會卡在 external-ip pending、瀏覽器連不到）——不是假設性風險。
    try:
        app_name = parsed.get("app_name")
        if app_name and app_name in {d["name"] for d in k8s_get_deployments()}:
            review["warnings"].append(
                f"名稱衝突：Deployment「{app_name}」已經存在，這次部署會更新現有的服務（例如換 image "
                f"或副本數），不會建立新的服務。如果你是想建一個不一樣的新服務，請換一個名字。 / "
                f"Name conflict: a Deployment named '{app_name}' already exists — this will update it "
                f"in place, not create a new one. Use a different name if you meant to create a separate service."
            )
        port = parsed.get("port")
        if port:
            port = int(port)
            conflicts = [s for s in k8s_get_services()
                        if s["port"] == port and s["app"] != app_name]
            if conflicts:
                other = conflicts[0]["app"] or conflicts[0]["name"]
                review["warnings"].append(
                    f"Port 衝突：port {port} 已經被「{other}」這個服務用掉了。Docker Desktop 的 K8s "
                    f"一個 port 只能真的對外綁一個服務，這次部署完可能會拿不到外部連線位址、瀏覽器連不到，"
                    f"建議換一個 port。 / "
                    f"Port conflict: port {port} is already used by '{other}'. Only one LoadBalancer "
                    f"service can be externally reachable per port on Docker Desktop, so this deployment "
                    f"may not get a working external address — consider using a different port."
                )
    except Exception as e:
        review["warnings"].append(f"conflict check unavailable: {e}")

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
.btn-sm:disabled{opacity:.4;cursor:not-allowed;border-color:var(--border2);color:inherit}
.btn-sm:disabled:hover{border-color:var(--border2);color:inherit}
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
.think-row{display:flex;align-items:center;gap:9px;padding:11px 15px;background:var(--surface);border:1px solid var(--border);border-radius:12px;width:fit-content;font-size:13px;color:var(--text2)}
.spinner{width:14px;height:14px;border:2px solid var(--border2);border-top-color:var(--green);border-radius:50%;animation:spin .7s linear infinite;flex-shrink:0}
.think-dots span{animation:blink 1.4s infinite both}
.think-dots span:nth-child(2){animation-delay:.2s}
.think-dots span:nth-child(3){animation-delay:.4s}
@keyframes blink{0%,80%,100%{opacity:0}40%{opacity:1}}

/* ── 使用說明書 / K8s 小百科：topbar 按鈕 ────────────────────── */
.icon-btn{display:inline-flex;align-items:center;justify-content:center;gap:5px;height:32px;padding:0 11px;border:none;border-radius:999px;cursor:pointer;font-family:inherit;font-size:12px;font-weight:700;letter-spacing:.2px;transition:background .15s,transform .1s}
.icon-btn:active{transform:scale(.93)}
.icon-btn svg{flex-shrink:0}
.lang-btn{background:var(--surface2);color:var(--text2)}
.lang-btn:hover{background:var(--border)}
.guide-btn{width:32px;padding:0;background:var(--green-light);color:var(--green)}
.guide-btn:hover{background:var(--green-mid)}
/* ── 是/否確認彈窗（名稱已存在 / port 被佔用）───────────────── */
.confirm-dialog{background:#fff;border-radius:14px;max-width:420px;width:100%;padding:20px 22px;box-shadow:0 20px 60px rgba(0,0,0,.3)}
.confirm-dialog-title{font-size:15px;font-weight:750;color:#111;margin-bottom:8px}
.confirm-dialog-body{font-size:13px;color:var(--text2);line-height:1.6}
.confirm-dialog-actions{display:flex;justify-content:flex-end;gap:8px;margin-top:18px}
#manual-overlay{position:fixed;inset:0;background:rgba(15,23,42,.45);z-index:200;align-items:center;justify-content:center;padding:24px;display:none}
/* [hidden] 是 HTML 標準屬性，但 #manual-overlay 這條 ID 規則的 specificity 比瀏覽器內建的
   [hidden]{display:none} 規則高，等於蓋掉它——這是之前「一登入就跳出來、關不掉」的根因。
   改用 JS 直接控制 style.display，不依賴 [hidden] 屬性，徹底避開這個 specificity 陷阱。 */
.manual-panel{background:#fff;border-radius:16px;max-width:680px;width:100%;max-height:min(720px,88vh);display:flex;flex-direction:column;box-shadow:0 20px 60px rgba(0,0,0,.3)}
.manual-panel-head{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;padding:18px 22px;border-bottom:1px solid var(--border)}
.manual-panel-title{font-size:16px;font-weight:750;color:#111}
.manual-panel-sub{font-size:12px;color:var(--text3);margin-top:3px}
.manual-close{border:none;background:var(--surface2);color:var(--text2);width:30px;height:30px;border-radius:50%;cursor:pointer;font-size:14px;flex-shrink:0}
.manual-close:hover{background:var(--border)}
.manual-panel-body{overflow-y:auto;padding:8px 22px 22px}
.manual-panel-body details{border:1px solid var(--border);border-radius:12px;margin-top:10px;overflow:hidden}
.manual-panel-body summary{cursor:pointer;padding:13px 16px;font-weight:700;font-size:13.5px;color:#222;list-style:none;background:var(--surface2)}
.manual-panel-body summary::-webkit-details-marker{display:none}
.manual-panel-body summary::before{content:'▸ ';color:var(--green)}
.manual-panel-body details[open] summary::before{content:'▾ '}
/* 2026-09-15 Healer 視覺化：一行式 Pod 清單，永遠不換行、名字太長用省略號截斷
   （flex:1 1 auto + overflow:hidden 在名字上；其他固定寬度欄位用 flex:0 0 auto，
   不會被擠爆）。詳情彈窗照抄 #manual-overlay 的 style.display 開關模式，不用
   [hidden]，避開同一個 CSS specificity 陷阱。 */
.pod-row{display:flex;align-items:center;gap:10px;padding:9px 12px;border-radius:8px;
  cursor:pointer;white-space:nowrap;overflow:hidden;border:1px solid transparent}
.pod-row:hover{background:var(--bg);border-color:var(--border)}
.pod-name{flex:1 1 auto;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
  font-family:monospace;font-size:13px;color:var(--text)}
.pod-dot{flex:0 0 auto;width:9px;height:9px;border-radius:50%}
.pod-meta{flex:0 0 auto;font-size:12px;color:var(--text3);min-width:0}
.pod-meta.pod-meta-age{display:none}
@media (min-width:560px){.pod-meta.pod-meta-age{display:inline}}
#pod-detail-overlay{position:fixed;inset:0;background:rgba(15,23,42,.45);z-index:200;
  align-items:center;justify-content:center;padding:24px;display:none}
.pod-detail-section{margin-top:14px}
.pod-detail-section-title{font-size:12.5px;font-weight:700;color:var(--text2);
  text-transform:uppercase;letter-spacing:.03em;margin-bottom:6px}
.pod-chart-empty{font-size:12px;color:var(--text3);padding:16px;text-align:center;
  border:1px dashed var(--border);border-radius:8px}
.manual-section{padding:14px 16px;font-size:13px;color:var(--text2)}
.manual-section code{background:var(--surface2);padding:1px 6px;border-radius:5px;font-family:'DM Mono',monospace;font-size:12px;color:#B45309}
.term{border-bottom:1px dotted var(--green);color:var(--green);font-weight:600;cursor:pointer}
#term-tip{position:fixed;max-width:260px;background:#111827;color:#F1F5F9;font-size:12.5px;line-height:1.6;padding:10px 12px;border-radius:9px;box-shadow:0 8px 24px rgba(0,0,0,.3);z-index:250}

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
      <!-- Deploy Console 側邊欄入口 2026-09-13 移除：Chat 的多步部署流程（規格確認→資源+三方
           審查→執行→完成指路）已經是它的超集合，兩條路走同一套後端會分裂維護（這次才修好
           Deploy Console 法庭卡巢狀讀取少一層的舊 bug 就是徵兆）。#page-deploy 頁面與
           /api/deploy(/parse) 路由完全沒刪，網址列仍可進（見 AGENT_RULES.md 新手友善原則）。 -->
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
      <!-- Dataset / Knowledge Base 是給開發者整理訓練資料 / RAG 索引用的內部工具，
           一般使用者不需要看到，2026-09-13 依使用者要求從側邊欄移除（新手友善原則，見 AGENT_RULES.md）。
           後端 /api/dataset/*、/api/rag/* 路由與 #page-dataset、#page-kb 都刻意保留，開發者仍可直接呼叫 API。 -->
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
    <!-- Docker/K8s 沒開時的新手警告：整條顯眼橫幅，不是小圓點而已 -->
    <div id="docker-warning" style="display:none;background:#FEF3C7;border:1px solid #F59E0B;border-radius:10px;padding:12px 16px;margin-bottom:12px;font-size:13px;color:#92400E;align-items:flex-start;gap:10px">
      <span style="font-size:18px;line-height:1">⚠️</span>
      <div>
        <div style="font-weight:700" id="docker-warning-title">Docker 沒有啟動 / Docker is not running</div>
        <div style="margin-top:2px" id="docker-warning-body">請先開啟 Docker Desktop，等它完全啟動後重新整理這個頁面。<br>Please start Docker Desktop, wait until it's fully running, then refresh this page.</div>
      </div>
    </div>

    <!-- Status Bar -->
    <div class="status-bar">
      <div class="status-pill">
        <div class="dot" id="model-dot"></div>
        <span id="model-status">Model loading...</span>
      </div>
      <div class="status-pill">
        <div class="dot {% if k8s %}green{% else %}red{% endif %}" id="k8s-dot"></div>
        <span id="k8s-status">K8s {% if k8s %}Connected{% else %}Simulation{% endif %}</span>
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
            <!-- 這是「系統聽懂了什麼」的白話摘要，不是給開發者看的資料集內部欄位
                 （is_k8s/complexity/language/dataset ground truth JSON 這些 2026-09-13 已移除，
                 新手友善原則見 AGENT_RULES.md）。 -->
            <div class="enrich-grid">
              <div class="enrich-field">
                <div class="lbl">App name</div>
                <div class="val" id="enrich-appname">—</div>
              </div>
              <div class="enrich-field">
                <div class="lbl">Image</div>
                <div class="val" id="enrich-image">—</div>
              </div>
              <div class="enrich-field">
                <div class="lbl">Pods</div>
                <div class="val" id="enrich-pods">—</div>
              </div>
              <div class="enrich-field">
                <div class="lbl">Port</div>
                <div class="val" id="enrich-port">—</div>
              </div>
            </div>
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
        <div class="pager" id="pods-pager" style="display:none;align-items:center;justify-content:space-between;padding:12px 4px 2px;font-size:13px;color:var(--text2)">
          <button class="btn-sm" id="pods-prev" onclick="podsGoPage(-1)">‹ 上一頁 / Prev</button>
          <span id="pods-page-info">第 1 頁 / 共 1 頁</span>
          <button class="btn-sm" id="pods-next" onclick="podsGoPage(1)">下一頁 / Next ›</button>
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
        <div class="pager" id="deps-pager" style="display:none;align-items:center;justify-content:space-between;padding:12px 4px 2px;font-size:13px;color:var(--text2)">
          <button class="btn-sm" id="deps-prev" onclick="depsGoPage(-1)">‹ 上一頁 / Prev</button>
          <span id="deps-page-info">第 1 頁 / 共 1 頁</span>
          <button class="btn-sm" id="deps-next" onclick="depsGoPage(1)">下一頁 / Next ›</button>
        </div>
      </div>
    </div>

    <!-- Chat Page -->
    <div class="page active" id="page-chat" style="padding:0;overflow:hidden">
      <div class="chat-shell">
        <div class="chat-topbar">
          <div>
            <div class="chat-title" id="chat-title-txt">ZeroTouch K8s Assistant</div>
            <div class="chat-subtitle" id="chat-subtitle-txt">Chat, deploy, inspect, and recover Kubernetes services</div>
          </div>
          <div style="display:flex;align-items:center;gap:10px">
            <button class="icon-btn lang-btn" onclick="setLang(uiLang==='zh'?'en':'zh')" title="Switch language" aria-label="Switch language">
              <svg viewBox="0 0 16 16" fill="none" width="14" height="14">
                <circle cx="8" cy="8" r="6" stroke="currentColor" stroke-width="1.3"/>
                <path d="M2 8h12M8 2c1.8 1.8 2.6 4 2.6 6s-.8 4.2-2.6 6c-1.8-1.8-2.6-4-2.6-6s.8-4.2 2.6-6z" stroke="currentColor" stroke-width="1.1"/>
              </svg>
              <span id="lang-btn-txt">EN</span>
            </button>
            <button class="icon-btn guide-btn" onclick="openManual()" title="使用說明書 / User guide" aria-label="使用說明書 / User guide">
              <svg viewBox="0 0 16 16" fill="none" width="17" height="17">
                <circle cx="8" cy="8" r="6.5" stroke="currentColor" stroke-width="1.4"/>
                <path d="M6.1 6.3c.1-1.05 1-1.8 2-1.8 1.1 0 2 .78 2 1.75 0 .72-.42 1.13-1.05 1.6-.58.42-.95.78-.95 1.4" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/>
                <circle cx="8" cy="11.4" r=".65" fill="currentColor"/>
              </svg>
            </button>
            <div class="status-pill"><div class="dot green"></div><span id="workspace-status-txt">Workspace ready</span></div>
          </div>
        </div>
        <div class="chat-messages" id="chat-messages"></div>
        <div class="chat-composer">
          <div class="composer-box">
            <textarea class="chat-input" id="chat-input" placeholder="Message ZeroTouch K8s..." rows="1" oninput="autoGrowChatInput(this)" onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();sendChat()}"></textarea>
            <button class="chat-send" onclick="sendChat()" aria-label="Send message" id="chat-send-txt">Send</button>
          </div>
          <div class="composer-hint" id="composer-hint-txt">Ask about Kubernetes, deploy services, list pods, scale workloads, or troubleshoot failures.</div>
        </div>
      </div>
    </div>

    <!-- 使用說明書 + K8s 小科普：新手友善原則（AGENT_RULES.md），點擊展開段落、
         點名詞看小框框註解，不用另外開分頁或去查資料。 -->
    <div id="manual-overlay" onclick="if(event.target===this)closeManual()">
      <div class="manual-panel">
        <div class="manual-panel-head">
          <div>
            <div class="manual-panel-title" id="manual-title">📖 User Guide</div>
            <div class="manual-panel-sub" id="manual-sub">Terms with a dotted underline are clickable definitions.</div>
          </div>
          <button class="manual-close" onclick="closeManual()" aria-label="Close">✕</button>
        </div>
        <div class="manual-panel-body" id="manual-body"></div>
      </div>
      <div id="term-tip" hidden></div>
    </div>

    <!-- 通用是/否確認彈窗：部署第一步偵測到名稱已存在或 port 被佔用時用，
         askConfirm() 回傳 Promise<boolean>，跟 startClarify 那種純文字提示不同，
         這個會真的擋住流程等使用者按下按鈕。 -->
    <div id="confirm-dialog-overlay" style="display:none;position:fixed;inset:0;background:rgba(15,23,42,.45);z-index:300;align-items:center;justify-content:center;padding:24px">
      <div class="confirm-dialog">
        <div class="confirm-dialog-title" id="confirm-dialog-title"></div>
        <div class="confirm-dialog-body" id="confirm-dialog-body"></div>
        <div class="confirm-dialog-actions">
          <button class="deploy-confirm-btn" id="confirm-dialog-no">No</button>
          <button class="deploy-confirm-btn primary" id="confirm-dialog-yes">Yes</button>
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
      <div class="page-sub">你自己的 Pod 一覽 + 自動自癒 / Your pods at a glance + auto self-healing</div>
      <div id="healer-bg-banner" style="margin-bottom:14px;padding:10px 14px;border-radius:10px;font-size:13px"></div>
      <div class="grid-3" style="margin-bottom:16px">
        <div class="card"><div class="card-title">Total Pods</div><div class="stat-num" id="healer-count">--</div><div class="stat-label">你的 Pod 總數 / your pods</div></div>
        <div class="card"><div class="card-title">Last Scan</div><div class="stat-num" style="font-size:14px" id="healer-time">--</div><div class="stat-label">Scan time</div></div>
        <div class="card"><div class="card-title">Status</div><div class="stat-num" id="healer-fixed">OK</div><div class="stat-label">Healer state</div></div>
      </div>
      <div class="card">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;flex-wrap:wrap;gap:10px">
          <span style="font-size:13px;color:var(--text2)">點一個 Pod 看完整狀態跟趨勢圖 / Click a pod for full status &amp; trend charts</span>
          <div style="display:flex;gap:10px">
            <button class="btn-primary" onclick="loadHealer()">Scan Now</button>
            <button class="btn-primary" onclick="healerAutoFix()" style="background:#DC2626">Auto Fix All</button>
          </div>
        </div>
        <div id="healer-list"><div style="color:var(--text3);font-size:13px">Loading...</div></div>
      </div>
      <div class="card" style="margin-top:16px">
        <div class="card-title">自動修復紀錄 / Auto-heal history</div>
        <div style="font-size:12px;color:var(--text3);margin-bottom:8px">系統背景每 30 秒自動掃描一次，偵測到異常 Pod 會自動診斷根因並嘗試修復（非單純刪除）。/ The system scans every 30s in the background; on detecting an unhealthy pod it automatically diagnoses the root cause and attempts a matching fix (not just a blind delete).</div>
        <div id="healer-bg-list"><div style="color:var(--text3);font-size:13px">--</div></div>
      </div>
    </div>

    <!-- Pod 詳情彈窗（Healer 視覺化）：仿照 #manual-overlay 的 style.display 開關
         模式（不用 [hidden]，之前踩過 CSS specificity 的坑，見 openManual/closeManual）。 -->
    <div id="pod-detail-overlay" onclick="if(event.target===this)closePodDetail()">
      <div class="manual-panel" style="max-width:640px">
        <div class="manual-panel-head">
          <div>
            <div class="manual-panel-title" id="pod-detail-title" style="font-family:monospace;font-size:16px">pod-name</div>
            <div class="manual-panel-sub" id="pod-detail-sub">--</div>
          </div>
          <button class="manual-close" onclick="closePodDetail()" aria-label="Close">✕</button>
        </div>
        <div class="manual-panel-body" id="pod-detail-body"></div>
      </div>
    </div>

    <div class="page" id="page-metrics">
      <div class="page-title">Metrics</div>
      <div class="page-sub">Prometheus observability</div>
      <div class="grid-3" style="margin-bottom:16px">
        <div class="card"><div class="card-title">Prometheus</div><div class="stat-num" id="prom-status">--</div><div class="stat-label">Connection</div></div>
        <div class="card"><div class="card-title">Running Pods</div><div class="stat-num" id="prom-pods">--</div><div class="stat-label">你的 namespace / your namespace</div></div>
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
            <div style="margin-top:10px;font-size:11px;color:var(--text3)">Full UI: <a id="prom-full-ui-link" href="#" target="_blank" style="color:var(--green)">--</a></div>
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
const k8sEnabled = {{ 'true' if k8s else 'false' }};
// 2026-09-15：Chat 歷史用 localStorage 存，是跟著瀏覽器走、不是跟著登入帳號走——
// 同一台瀏覽器換帳號登入會看到上一個帳號的聊天記錄。用 CURRENT_USER 把 key 綁定到
// 帳號，換帳號登入時就不會看到別人（或自己上一個帳號）的聊天內容。
const CURRENT_USER = "{{ username }}";

// ══════════════════════════════════════════════════════════════════
//  中英切換（Chat 分頁 + 使用說明書）。專有名詞（Pod/Deployment/...）
//  兩種語言都固定用英文，只有說明文字跟著切換。
// ══════════════════════════════════════════════════════════════════
let uiLang = 'zh';
try { uiLang = localStorage.getItem('zt_lang') || 'zh'; } catch(e){}

const I18N = {
  zh: {
    langBtn: 'EN',
    manualBtn: '說明書',
    chatTitle: 'ZeroTouch K8s 助理',
    chatSubtitle: '聊天、部署、檢視並修復 Kubernetes 服務',
    workspaceReady: '系統就緒',
    sendBtn: '送出',
    composerHint: '用自然語言部署服務、查詢 Pod 狀態、調整副本數，或詢問任何 Kubernetes 問題。',
    emptyTitle: '有什麼我能協助你處理叢集的？',
    emptyDesc: '用自然語言描述你想做的事：部署服務、檢視運作狀態、排查錯誤，或詢問任何 Kubernetes 概念。',
    cards: [
      {title:'部署服務', example:'deploy 3 nginx:latest pods for web-frontend', send:'deploy 3 nginx:latest pods for web-frontend'},
      {title:'檢視叢集狀態', example:'list pods and show deployments', send:'list pods'},
      {title:'學習 Kubernetes', example:'Explain Deployment vs Service', send:'Explain Kubernetes Deployment vs Service'},
      {title:'排查問題', example:'How do I debug CrashLoopBackOff?', send:'How do I debug CrashLoopBackOff?'},
    ],
    manualTitle: '📖 使用說明書',
    manualSub: '名詞下方有虛線底線的可以點擊展開簡明定義',
  },
  en: {
    langBtn: '中文',
    manualBtn: 'Guide',
    chatTitle: 'ZeroTouch K8s Assistant',
    chatSubtitle: 'Chat, deploy, inspect, and recover Kubernetes services',
    workspaceReady: 'Workspace ready',
    sendBtn: 'Send',
    composerHint: 'Ask about Kubernetes, deploy services, list pods, scale workloads, or troubleshoot failures.',
    emptyTitle: 'How can I help with your cluster?',
    emptyDesc: 'Use natural language to deploy services, inspect workloads, troubleshoot failures, or ask Kubernetes questions.',
    cards: [
      {title:'Deploy a service', example:'deploy 3 nginx:latest pods for web-frontend', send:'deploy 3 nginx:latest pods for web-frontend'},
      {title:'Check cluster status', example:'list pods and show deployments', send:'list pods'},
      {title:'Learn Kubernetes', example:'Explain Deployment vs Service', send:'Explain Kubernetes Deployment vs Service'},
      {title:'Troubleshoot', example:'How do I debug CrashLoopBackOff?', send:'How do I debug CrashLoopBackOff?'},
    ],
    manualTitle: '📖 User Guide',
    manualSub: 'Terms with a dotted underline are clickable for a concise definition.',
  },
};

function setLang(l){
  uiLang = (l === 'en') ? 'en' : 'zh';
  try { localStorage.setItem('zt_lang', uiLang); } catch(e){}
  applyLang();
}

function applyLang(){
  const t = I18N[uiLang];
  const set = (id, text) => { const el = document.getElementById(id); if(el) el.textContent = text; };
  set('lang-btn-txt', t.langBtn);
  set('manual-btn-txt', t.manualBtn);
  set('chat-title-txt', t.chatTitle);
  set('chat-subtitle-txt', t.chatSubtitle);
  set('workspace-status-txt', t.workspaceReady);
  set('chat-send-txt', t.sendBtn);
  set('composer-hint-txt', t.composerHint);
  set('manual-title', t.manualTitle);
  set('manual-sub', t.manualSub);
  renderManualBody();
  // 只有在聊天室是空的（歡迎畫面）時才需要重繪，避免打斷正在進行的多步流程卡片
  const ch = typeof currentChat === 'function' ? currentChat() : null;
  if(ch && !ch.messages.length && typeof renderMessages === 'function') renderMessages();
}

// ── 使用說明書內容：中英兩份獨立寫，專有名詞一律保留英文 ──
const MANUAL_ZH = `
  <details open>
    <summary>🚀 三步驟快速上手</summary>
    <div class="manual-section">
      <ol style="margin:0;padding-left:20px;line-height:1.9">
        <li>在 Chat 輸入部署需求，例如「<code>deploy 3 nginx pods for shop, port 80</code>」，系統會解析為結構化規格供你確認。</li>
        <li>確認規格後選擇「下一步」，系統會計算此次部署所需的資源用量，並執行一次安全性、成本與效能的自動化審查；審查通過後才能執行部署。</li>
        <li>部署完成後，畫面會提供連結，導向 <b>Pods</b> / <b>Deployments</b> 頁面確認執行結果。</li>
      </ol>
    </div>
  </details>

  <details>
    <summary>💬 Chat 還能協助你做什麼</summary>
    <div class="manual-section">
      <p style="margin:0 0 8px">除了部署，你也可以直接在 Chat 輸入以下指令：</p>
      <ul style="margin:0;padding-left:20px;line-height:2">
        <li><code>list pods</code> ／ <code>顯示所有部署</code> — 檢視目前叢集內的運作狀態</li>
        <li><code>web-frontend 有沒有壞掉</code> — 精確檢查單一服務的健康狀態，異常時附上根因分析與修復建議</li>
        <li><code>查看 api-gateway 細節</code> — 檢視單一 <span class="term" data-def="Kubernetes 中最小的可部署運算單位，用於封裝並執行你的應用程式容器。">Pod</span> 的詳細狀態</li>
        <li><code>scale web-frontend to 5</code> — 調整副本數（執行前會先出示確認畫面）</li>
        <li><code>rollback api-gateway</code> — 回滾至上一個版本</li>
        <li><code>什麼是 Deployment</code> — 詢問任何 Kubernetes 概念，系統會以簡明的方式回答</li>
      </ul>
    </div>
  </details>

  <details>
    <summary>🗺️ 側邊欄各頁面說明</summary>
    <div class="manual-section">
      <ul style="margin:0;padding-left:20px;line-height:2">
        <li><b>Pods</b>：目前所有 <span class="term" data-def="Kubernetes 中最小的可部署運算單位，用於封裝並執行你的應用程式容器。">Pod</span> 的狀態、IP、所在節點與重啟次數。</li>
        <li><b>Deployments</b>：各服務要求與實際的副本數量，以及所使用的映像版本。</li>
        <li><b>Healer</b>：掃描異常 <span class="term" data-def="Kubernetes 中最小的可部署運算單位，用於封裝並執行你的應用程式容器。">Pod</span>（如 <span class="term" data-def="容器持續啟動失敗並反覆重啟，通常代表應用程式本身發生錯誤或設定有誤。">CrashLoopBackOff</span>），可一鍵修復重建。</li>
        <li><b>GitOps Log</b>：部署歷史紀錄，每次部署皆會留下版本，便於回滾比對。</li>
        <li><b>Metrics</b>：Prometheus 監控狀態，檢視叢集目前的運作規模。</li>
      </ul>
    </div>
  </details>

  <details>
    <summary>📚 K8s 小百科</summary>
    <div class="manual-section">
      <p style="margin:0 0 10px;color:var(--text2)">以下名詞皆可點擊展開簡明定義。</p>
      <p style="line-height:2.3">
        <span class="term" data-def="Kubernetes 中最小的可部署運算單位，用於封裝並執行你的應用程式容器。">Pod</span>、
        <span class="term" data-def="描述所需狀態的宣告式設定（例如映像版本與副本數量），Kubernetes 會持續協調實際狀態以符合此設定，並在 Pod 異常時自動重建。">Deployment</span>、
        <span class="term" data-def="由 Deployment 自動建立，負責維持指定數量的 Pod 正常運作，一般不需直接操作。">ReplicaSet</span>、
        <span class="term" data-def="提供固定不變的存取位址，將流量轉發至背後的一組 Pod，不受 Pod 重建後 IP 變動的影響。">Service</span>、
        <span class="term" data-def="叢集內用於劃分資源的邊界。本系統每個帳號會有自己的 namespace，你部署的東西只會建立在你自己的 namespace，其他帳號看不到。">Namespace</span>、
        <span class="term" data-def="打包完成的「應用程式 + 執行環境」，例如 nginx:latest；冒號後方為版本標籤（tag），未指定時預設為 latest。">Image</span>、
        <span class="term" data-def="欲維持運作的 Pod 數量。設定多個副本可提升容錯能力並分攤流量負載。">Replicas</span>、
        <span class="term" data-def="服務對外接受連線的埠號，例如網頁伺服器常用 80，Redis 常用 6379。">Port</span>、
        <span class="term" data-def="叢集中的一台運算機器，Pod 會被排程至某個 Node 上執行。本系統以 Docker Desktop 內建的 Kubernetes 模擬節點環境。">Node</span>、
        <span class="term" data-def="由一台或多台 Node 組成的完整 Kubernetes 系統。">Cluster</span>、
        <span class="term" data-def="容器持續啟動失敗並反覆重啟，通常代表應用程式本身發生錯誤或設定有誤。">CrashLoopBackOff</span>、
        <span class="term" data-def="無法取得指定的映像檔，常見原因為名稱錯誤、版本不存在，或私有倉庫權限不足。">ImagePullBackOff</span>、
        <span class="term" data-def="Pod 使用的記憶體超過設定上限，遭系統強制終止。">OOMKilled</span>、
        <span class="term" data-def="Kubernetes 內部用於描述資源的設定檔格式。本系統的設計目標即是讓使用者無需編寫 YAML。">YAML</span>、
        <span class="term" data-def="Kubernetes 官方提供的命令列工具。本系統的核心價值在於讓使用者無需學習此工具即可完成部署與維運。">kubectl</span>、
        <span class="term" data-def="將每次部署的設定變更記錄為 Git 版本，以利追蹤歷史與執行回滾。本系統會在每次部署時自動完成此流程。">GitOps</span>、
        <span class="term" data-def="將服務退回至前一個穩定版本。於 Chat 輸入「rollback 應用程式名稱」即可執行。">Rollback</span>
      </p>
    </div>
  </details>
`;

const MANUAL_EN = `
  <details open>
    <summary>🚀 Quick Start in 3 Steps</summary>
    <div class="manual-section">
      <ol style="margin:0;padding-left:20px;line-height:1.9">
        <li>Describe what you need in Chat, e.g. "<code>deploy 3 nginx pods for shop, port 80</code>". The system parses it into a structured spec for you to confirm.</li>
        <li>After confirming, choose "Next" — the system calculates the resource footprint for this deployment and runs an automated security, cost, and performance review. Deployment proceeds only after the review passes.</li>
        <li>Once deployed, you'll get direct links to the <b>Pods</b> / <b>Deployments</b> pages to verify the result.</li>
      </ol>
    </div>
  </details>

  <details>
    <summary>💬 What Else Chat Can Do</summary>
    <div class="manual-section">
      <p style="margin:0 0 8px">Beyond deployment, you can type these directly in Chat:</p>
      <ul style="margin:0;padding-left:20px;line-height:2">
        <li><code>list pods</code> / <code>show deployments</code> — inspect the current cluster state</li>
        <li><code>is web-frontend healthy</code> — precisely check a single service's health, with root-cause analysis and remediation suggestions when something is wrong</li>
        <li><code>describe pod api-gateway</code> — inspect the detailed status of a single <span class="term" data-def="The smallest deployable unit in Kubernetes, used to package and run your application container(s).">Pod</span></li>
        <li><code>scale web-frontend to 5</code> — adjust replica count (a confirmation step is shown first)</li>
        <li><code>rollback api-gateway</code> — revert to the previous version</li>
        <li><code>what is a Deployment</code> — ask about any Kubernetes concept and get a plain-language answer</li>
      </ul>
    </div>
  </details>

  <details>
    <summary>🗺️ Sidebar Pages</summary>
    <div class="manual-section">
      <ul style="margin:0;padding-left:20px;line-height:2">
        <li><b>Pods</b>: status, IP, node, and restart count for every <span class="term" data-def="The smallest deployable unit in Kubernetes, used to package and run your application container(s).">Pod</span>.</li>
        <li><b>Deployments</b>: desired vs. actual replica counts and the image version in use.</li>
        <li><b>Healer</b>: scans for unhealthy <span class="term" data-def="The smallest deployable unit in Kubernetes, used to package and run your application container(s).">Pods</span> (e.g. <span class="term" data-def="A container keeps failing to start and restarting repeatedly, usually indicating an application bug or misconfiguration.">CrashLoopBackOff</span>) and can remediate them with one click.</li>
        <li><b>GitOps Log</b>: deployment history — every deployment leaves a version for rollback comparison.</li>
        <li><b>Metrics</b>: Prometheus monitoring status and current cluster scale.</li>
      </ul>
    </div>
  </details>

  <details>
    <summary>📚 Kubernetes Glossary</summary>
    <div class="manual-section">
      <p style="margin:0 0 10px;color:var(--text2)">Click any term below to expand a concise definition.</p>
      <p style="line-height:2.3">
        <span class="term" data-def="The smallest deployable unit in Kubernetes, used to package and run your application container(s).">Pod</span>,
        <span class="term" data-def="A declarative description of the desired state (e.g. image version and replica count). Kubernetes continuously reconciles the actual state to match it, automatically recreating Pods that fail.">Deployment</span>,
        <span class="term" data-def="Automatically created by a Deployment to maintain the specified number of running Pods; you generally don't interact with it directly.">ReplicaSet</span>,
        <span class="term" data-def="A stable, fixed address that routes traffic to a group of Pods behind it, unaffected by Pod IP changes after recreation.">Service</span>,
        <span class="term" data-def="A boundary used to partition resources within a cluster. This system gives each account its own namespace — what you deploy only lands in your own namespace and is invisible to other accounts.">Namespace</span>,
        <span class="term" data-def="A packaged 'application + runtime environment', e.g. nginx:latest. The part after the colon is the version tag; it defaults to latest if omitted.">Image</span>,
        <span class="term" data-def="The number of identical Pods to keep running. Multiple replicas improve fault tolerance and distribute load.">Replicas</span>,
        <span class="term" data-def="The network port a service accepts connections on, e.g. 80 for web servers, 6379 for Redis.">Port</span>,
        <span class="term" data-def="A machine in the cluster that Pods are scheduled onto. This system simulates nodes using Docker Desktop's built-in Kubernetes.">Node</span>,
        <span class="term" data-def="The complete Kubernetes system, made up of one or more Nodes.">Cluster</span>,
        <span class="term" data-def="A container keeps failing to start and restarting repeatedly, usually indicating an application bug or misconfiguration.">CrashLoopBackOff</span>,
        <span class="term" data-def="The specified image could not be retrieved — commonly due to a typo, a missing tag, or insufficient registry permissions.">ImagePullBackOff</span>,
        <span class="term" data-def="The Pod exceeded its configured memory limit and was forcibly terminated by the system.">OOMKilled</span>,
        <span class="term" data-def="The configuration file format Kubernetes uses internally to describe resources. This system is designed so you never need to write YAML yourself.">YAML</span>,
        <span class="term" data-def="Kubernetes' official command-line tool. This system's core value is letting you deploy and operate without ever learning it.">kubectl</span>,
        <span class="term" data-def="Recording every deployment's configuration as a Git version, enabling history tracking and rollback. This system does this automatically on every deployment.">GitOps</span>,
        <span class="term" data-def="Reverting a service to its previous stable version. Type 'rollback &lt;app-name&gt;' in Chat to trigger it.">Rollback</span>
      </p>
    </div>
  </details>
`;

function renderManualBody(){
  const body = document.getElementById('manual-body');
  if(body) body.innerHTML = (uiLang === 'en') ? MANUAL_EN : MANUAL_ZH;
}

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

// ── 使用說明書 / K8s 小百科 ──
function openManual(){
  renderManualBody();
  document.getElementById('manual-overlay').style.display = 'flex';
}
function closeManual(){
  document.getElementById('manual-overlay').style.display = 'none';
  const tip = document.getElementById('term-tip');
  if(tip) tip.hidden = true;
}
document.addEventListener('keydown', function(e){
  if(e.key === 'Escape') closeManual();
});

// 通用是/否確認彈窗。回傳 Promise<boolean>：true=按了 Yes、false=按了 No 或關掉。
function askConfirm(title, bodyHtml, yesLabel, noLabel){
  return new Promise(function(resolve){
    const overlay = document.getElementById('confirm-dialog-overlay');
    document.getElementById('confirm-dialog-title').textContent = title;
    document.getElementById('confirm-dialog-body').innerHTML = bodyHtml;
    const yesBtn = document.getElementById('confirm-dialog-yes');
    const noBtn = document.getElementById('confirm-dialog-no');
    yesBtn.textContent = yesLabel || (uiLang === 'en' ? 'Yes, continue' : '是，繼續');
    noBtn.textContent = noLabel || (uiLang === 'en' ? 'No, go back' : '否，回去修改');
    overlay.style.display = 'flex';
    function done(v){
      overlay.style.display = 'none';
      yesBtn.onclick = null; noBtn.onclick = null;
      resolve(v);
    }
    yesBtn.onclick = function(){ done(true); };
    noBtn.onclick = function(){ done(false); };
  });
}
// 專有名詞點一下展開小框框註解：用一個共用 tooltip，點哪個詞就移到那個詞下面顯示。
document.getElementById('manual-body') && document.getElementById('manual-body').addEventListener('click', function(e){
  const term = e.target.closest('.term');
  const tip = document.getElementById('term-tip');
  if(!term){ if(tip) tip.hidden = true; return; }
  e.stopPropagation();
  const def = term.getAttribute('data-def') || '';
  if(!tip.hidden && tip.dataset.forTerm === def){ tip.hidden = true; return; }
  tip.textContent = def;
  tip.dataset.forTerm = def;
  tip.hidden = false;
  const r = term.getBoundingClientRect();
  const w = Math.min(260, window.innerWidth - 24);
  tip.style.width = w + 'px';
  tip.style.left = Math.max(12, Math.min(r.left, window.innerWidth - w - 12)) + 'px';
  tip.style.top = (r.bottom + 6) + 'px';
});

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
  if(name === 'healer') { loadPodList(); loadHealerBgStatus(); }
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

    const k8sDot = document.getElementById('k8s-dot');
    const k8sTxt = document.getElementById('k8s-status');
    if(k8sDot) k8sDot.className = 'dot ' + (d.k8s ? 'green' : 'red');
    if(k8sTxt) k8sTxt.textContent = 'K8s ' + (d.k8s ? 'Connected' : 'Simulation');

    // 新手警告：Docker 沒開 / 開了但 K8s 連不上，兩種訊息分開講清楚原因跟解法
    const warn = document.getElementById('docker-warning');
    const wTitle = document.getElementById('docker-warning-title');
    const wBody = document.getElementById('docker-warning-body');
    if(warn){
      if(!d.docker_running){
        wTitle.textContent = 'Docker 沒有啟動 / Docker is not running';
        wBody.innerHTML = (d.docker_message ? escHtml(d.docker_message)+'<br>' : '') +
          '請先開啟 Docker Desktop，等它完全啟動（圖示變綠/穩定）後重新整理這個頁面。<br>' +
          "Please start Docker Desktop, wait until it's fully running, then refresh this page.";
        warn.style.display = 'flex';
      } else if(!d.k8s){
        wTitle.textContent = 'Docker 已啟動，但 Kubernetes 連不上 / Docker is running, but Kubernetes is unreachable';
        wBody.innerHTML = '請確認 Docker Desktop 設定裡的 Kubernetes 功能已開啟（Settings → Kubernetes → Enable Kubernetes），啟動可能需要 1–2 分鐘。<br>' +
          'Please check that Kubernetes is enabled in Docker Desktop settings (Settings → Kubernetes → Enable Kubernetes) — it can take 1–2 minutes to start.';
        warn.style.display = 'flex';
      } else {
        warn.style.display = 'none';
      }
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

  // 白話摘要：系統聽懂的部署規格（不是資料集內部欄位，一般使用者看得懂就好）
  const out = p.output || p;
  document.getElementById('enrich-appname').textContent = out.app_name || p.app_name || '—';
  document.getElementById('enrich-image').textContent = out.image || p.image || '—';
  document.getElementById('enrich-pods').textContent = out.pods || p.pods || '—';
  document.getElementById('enrich-port').textContent = out.port || p.port || '—';
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

  // review.agents 是 orchestrate() 的完整結果，三個代理在 review.agents.agents 底下
  const agents = review && review.agents && review.agents.agents;
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
    verdict.textContent = '✓ 通過 / Approved — ' + (review.reason || '三個代理檢查全部通過 / All agent checks passed');
    actions.innerHTML = `<button class="deploy-confirm-btn primary" id="court-deploy-btn">確認部署 / Deploy</button>`;
    document.getElementById('court-deploy-btn').onclick = () => courtProceedDeploy(myId, originalText, parsed);
  } else if(decision === 'warn'){
    verdict.className = 'result-box court-verdict warn';
    verdict.textContent = '⚠ 警告 / Warning — ' + (review.reason || '發現警告 / Warnings found') +
      (review.warnings && review.warnings.length ? '\n' + review.warnings.map(w => '· ' + w).join('\n') : '');
    actions.innerHTML = `<button class="deploy-confirm-btn" id="court-cancel-btn">取消 / Cancel</button>
      <button class="deploy-confirm-btn primary" id="court-deploy-btn">仍要部署 / Deploy anyway</button>`;
    document.getElementById('court-deploy-btn').onclick = () => courtProceedDeploy(myId, originalText, parsed);
    document.getElementById('court-cancel-btn').onclick = () => closeCourtPanel(myId);
  } else {
    verdict.className = 'result-box court-verdict error';
    verdict.textContent = '✗ 阻擋 / Blocked — ' + (review.reason || '部署被阻擋 / Deployment blocked') +
      (review.blockers && review.blockers.length ? '\n' + review.blockers.map(b => '· ' + b).join('\n') : '');
    actions.innerHTML = `<button class="deploy-confirm-btn" id="court-close-btn">關閉 / Close</button>`;
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
      verdict.textContent = '✗ 部署被阻擋 / Deployment blocked — ' + (d.error || d.reason || '');
      actions.querySelectorAll('button').forEach(b => b.disabled = false);
      return;
    }
    const p = d.parsed || parsed;
    if(d.k8s_deploy && d.k8s_deploy.ok === false){
      verdict.className = 'result-box court-verdict warn';
      verdict.textContent = `⚠ GitOps 已提交，但 K8s 實際部署失敗 / GitOps committed, but the K8s deploy failed：${d.k8s_deploy.message}`;
      actions.innerHTML = `<button class="deploy-confirm-btn" id="court-close-btn">關閉 / Close</button>`;
      document.getElementById('court-close-btn').onclick = () => closeCourtPanel(myId);
      return;
    }
    verdict.className = 'result-box court-verdict success';
    verdict.textContent = `✓ 部署成功 / Deployed — App: ${p.app_name}  ·  Image: ${p.image}  ·  Pods: ${p.pods}${p.port ? '  ·  Port: ' + p.port : ''}`;
    actions.innerHTML = `<button class="deploy-confirm-btn" id="court-close-btn">關閉 / Close</button>`;
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
// 2026-09-15：Pods/Deployments 列表加分頁，每頁最多 20 筆，超過就要換頁——
// 後端 k8s_get_pods/k8s_get_deployments 已經改成用真正的 creation_timestamp
// 排序（最新部署排最上面），這裡只需要照後端給的順序切頁，不用再排一次。
const PAGE_SIZE = 20;
let _podsAll = [];
let _podsPage = 1;

function _renderPager(prefix, page, totalPages){
  const pager = document.getElementById(prefix+'-pager');
  if(totalPages <= 1){ pager.style.display = 'none'; return; }
  pager.style.display = 'flex';
  document.getElementById(prefix+'-page-info').textContent = `第 ${page} 頁 / 共 ${totalPages} 頁 (Page ${page} of ${totalPages})`;
  document.getElementById(prefix+'-prev').disabled = (page <= 1);
  document.getElementById(prefix+'-next').disabled = (page >= totalPages);
}

function podsGoPage(delta){
  const totalPages = Math.max(1, Math.ceil(_podsAll.length / PAGE_SIZE));
  _podsPage = Math.min(totalPages, Math.max(1, _podsPage + delta));
  renderPodsPage();
}

function renderPodsPage(){
  const tbody = document.getElementById('pods-tbody');
  if(!tbody) return;
  if(!_podsAll.length){
    tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;padding:32px;color:var(--text3)">No pods found</td></tr>';
    _renderPager('pods', 1, 1);
    return;
  }
  const totalPages = Math.max(1, Math.ceil(_podsAll.length / PAGE_SIZE));
  _podsPage = Math.min(totalPages, Math.max(1, _podsPage));
  const start = (_podsPage - 1) * PAGE_SIZE;
  const pageItems = _podsAll.slice(start, start + PAGE_SIZE);
  tbody.innerHTML = pageItems.map(p => `
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
  _renderPager('pods', _podsPage, totalPages);
}

async function loadPods(){
  const tbody = document.getElementById('pods-tbody');
  if(!tbody) return;
  tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;padding:32px;color:var(--text3)">Loading...</td></tr>';
  try {
    const r = await fetch('/api/pods');
    const d = await r.json();
    _podsAll = d.pods || [];
    // 重新整理清單時，如果原本停在的頁數超過新的總頁數（例如刪掉 pod 後），拉回最後一頁。
    const totalPages = Math.max(1, Math.ceil(_podsAll.length / PAGE_SIZE));
    if(_podsPage > totalPages) _podsPage = totalPages;
    renderPodsPage();
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
let _depsAll = [];
let _depsPage = 1;

function depsGoPage(delta){
  const totalPages = Math.max(1, Math.ceil(_depsAll.length / PAGE_SIZE));
  _depsPage = Math.min(totalPages, Math.max(1, _depsPage + delta));
  renderDepsPage();
}

function renderDepsPage(){
  const tbody = document.getElementById('deps-tbody');
  if(!tbody) return;
  if(!_depsAll.length){
    tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;padding:32px;color:var(--text3)">No deployments found</td></tr>';
    _renderPager('deps', 1, 1);
    return;
  }
  const totalPages = Math.max(1, Math.ceil(_depsAll.length / PAGE_SIZE));
  _depsPage = Math.min(totalPages, Math.max(1, _depsPage));
  const start = (_depsPage - 1) * PAGE_SIZE;
  const pageItems = _depsAll.slice(start, start + PAGE_SIZE);
  tbody.innerHTML = pageItems.map(dep => `
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
  _renderPager('deps', _depsPage, totalPages);
}

async function loadDeployments(){
  const tbody = document.getElementById('deps-tbody');
  if(!tbody) return;
  tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;padding:32px;color:var(--text3)">Loading...</td></tr>';
  try {
    const r = await fetch('/api/deployments');
    const d = await r.json();
    _depsAll = d.deployments || [];
    const totalPages = Math.max(1, Math.ceil(_depsAll.length / PAGE_SIZE));
    if(_depsPage > totalPages) _depsPage = totalPages;
    renderDepsPage();
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

// ══════════════════════════════════════════════════════════════════
//  Healer 視覺化（2026-09-15）：一行式 Pod 清單（#healer-list）+ 點擊進入的
//  詳情彈窗（#pod-detail-overlay，含隨時間變化的趨勢圖）。
// ══════════════════════════════════════════════════════════════════

async function loadPodList(){
  const el = document.getElementById('healer-list');
  el.innerHTML = '<div style="color:var(--text3);font-size:13px">Loading...</div>';
  try{
    const r = await fetch('/api/pods'); const d = await r.json();
    const pods = d.pods || [];
    document.getElementById('healer-count').textContent = pods.length;
    document.getElementById('healer-time').textContent = new Date().toLocaleTimeString();
    if(!pods.length){
      el.innerHTML = '<div style="color:var(--text3);font-size:13px">目前沒有任何 Pod，用 Chat 部署一個試試看 / No pods yet — try deploying one from Chat</div>';
      return;
    }
    el.innerHTML = pods.map(p => {
      const readyCond = (p.conditions||[]).find(c=>c.type==='Ready');
      const isReady = readyCond ? readyCond.status==='True' : false;
      let color, label;
      if(p.phase==='Running' && isReady){ color='#16A34A'; label='Running'; }
      else if(p.phase==='Pending'){ color='#D97706'; label='Pending'; }
      else { color='#DC2626'; label=p.phase||'Unknown'; }
      return `<div class="pod-row" onclick="openPodDetail('${escHtml(p.name)}')">
        <span class="pod-dot" style="background:${color}" title="${escHtml(label)}"></span>
        <span class="pod-name">${escHtml(p.name)}</span>
        <span class="pod-meta">${escHtml(label)}</span>
        <span class="pod-meta">↻ ${p.restarts||0}</span>
        <span class="pod-meta pod-meta-age">${escHtml(p.age||'')}</span>
      </div>`;
    }).join('');
  }catch(e){ el.innerHTML = '<div style="color:var(--text3)">Error: '+e+'</div>'; }
}

async function loadHealer(){
  // 「Scan Now」按鈕：主動打 /api/healer/scan 取得異常清單摘要，掃完刷新一行式清單
  // （清單本身平時只顯示現況，不會自動跑診斷；按這顆才會真的觸發一次掃描）。
  document.getElementById('healer-time').textContent = new Date().toLocaleTimeString();
  try{
    const r = await fetch('/api/healer/scan'); const d = await r.json();
    const issues = d.issues || [];
    await loadPodList();
    if(issues.length){
      alert(`掃描完成，發現 ${issues.length} 個異常 Pod，點清單裡標紅點的 Pod 可以看詳情跟修復。 / `+
            `Scan complete — found ${issues.length} unhealthy pod(s). Click a red-dot pod below for details/fix.`);
    } else {
      alert('掃描完成，所有 Pod 健康 / Scan complete — all pods healthy.');
    }
  }catch(e){ alert('掃描失敗 / Scan failed: '+e); }
}

let _podDetailCurrent = null;

function closePodDetail(){
  document.getElementById('pod-detail-overlay').style.display = 'none';
  _podDetailCurrent = null;
}

async function openPodDetail(name){
  _podDetailCurrent = name;
  document.getElementById('pod-detail-overlay').style.display = 'flex';
  document.getElementById('pod-detail-title').textContent = name;
  document.getElementById('pod-detail-sub').textContent = 'Loading...';
  document.getElementById('pod-detail-body').innerHTML = '<div style="color:var(--text3);font-size:13px">Loading...</div>';
  try{
    const [detailR, histR] = await Promise.all([
      fetch(`/api/pods/${encodeURIComponent(name)}`),
      fetch(`/api/pods/${encodeURIComponent(name)}/history`),
    ]);
    const detailData = await detailR.json();
    const histData = await histR.json();
    if(_podDetailCurrent !== name) return;  // 使用者可能在等待期間換點了別的 Pod 或關掉
    renderPodDetail(name, detailData, histData.history || []);
  }catch(e){
    if(_podDetailCurrent !== name) return;
    document.getElementById('pod-detail-body').innerHTML = '<div style="color:#DC2626;font-size:13px">Error: '+e+'</div>';
  }
}

function renderPodDetail(name, detailData, history){
  const pod = (detailData.pods || [])[0];
  const sub = document.getElementById('pod-detail-sub');
  const body = document.getElementById('pod-detail-body');
  if(!pod){
    sub.textContent = detailData.message || '找不到這個 Pod / Not found';
    body.innerHTML = detailData.deployment
      ? `<div class="pod-detail-section"><div class="pod-detail-section-title">Deployment</div>
         <div style="font-size:13px">${escHtml(detailData.deployment.health_summary||'')}</div></div>`
      : '';
    return;
  }
  sub.textContent = pod.healthy ? '✅ Healthy' : ('⚠️ ' + (pod.health_summary || 'Unhealthy'));
  let html = '';
  html += `<div class="pod-detail-section"><div class="pod-detail-section-title">容器狀態 / Containers</div>`;
  html += (pod.containers||[]).map(c => `
    <div style="display:flex;justify-content:space-between;gap:10px;font-size:13px;padding:6px 0;border-bottom:1px solid var(--border)">
      <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${escHtml(c.name)} <span style="color:var(--text3)">(${escHtml(c.image)})</span></span>
      <span style="flex-shrink:0;color:${c.ready?'#16A34A':'#DC2626'}">${c.ready ? 'ready' : escHtml((c.state||'')+(c.reason?'/'+c.reason:''))} · restarts ${c.restart_count}</span>
    </div>`).join('');
  const ru = pod.real_usage || {};
  html += `<div style="font-size:12px;color:var(--text3);padding-top:6px">實際使用 / Actual usage：` +
    (ru.available
      ? `CPU ${ru.cpu_cores!=null ? ru.cpu_cores.toFixed(3)+' 核' : 'N/A'}　記憶體 ${ru.memory_mi!=null ? ru.memory_mi+' Mi' : 'N/A'}`
      : `Prometheus 未連線，無法取得實際用量 / Prometheus unreachable, actual usage unavailable`) +
    `</div>`;
  html += `</div>`;
  if(pod.events && pod.events.length){
    html += `<div class="pod-detail-section"><div class="pod-detail-section-title">最近事件 / Recent Events</div>`;
    html += pod.events.map(e => `<div style="font-size:12px;color:var(--text2);padding:3px 0">[${escHtml(e.type)}] ${escHtml(e.reason)}: ${escHtml(e.message)}</div>`).join('');
    html += `</div>`;
  }
  if(!pod.healthy){
    html += `<div class="pod-detail-section" style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
      <button class="btn-primary" onclick="diagnosePodDetail('${escHtml(name)}')" style="background:var(--surface);color:var(--text);border:1px solid var(--border)">診斷根因 / Diagnose</button>
      <button class="btn-primary" onclick="fixPod('${escHtml(name)}')" style="background:#DC2626">Fix</button>
    </div>
    <div id="pod-diagnosis-result" class="pod-detail-section" style="font-size:13px;display:none"></div>`;
  }
  html += `<div class="pod-detail-section"><div class="pod-detail-section-title">重啟次數趨勢 / Restart Trend</div>${renderRestartChart(history)}</div>`;
  html += `<div class="pod-detail-section"><div class="pod-detail-section-title">健康狀態時間軸 / Health Timeline</div>${renderHealthTimeline(history)}</div>`;
  body.innerHTML = html;
}

async function diagnosePodDetail(name){
  let el = document.getElementById('pod-diagnosis-result');
  if(!el) return;
  el.style.display = 'block';
  el.textContent = '診斷中... / Diagnosing...';
  try{
    const r = await fetch(`/api/pods/${encodeURIComponent(name)}?diagnose=1`);
    const d = await r.json();
    const diag = ((d.pods||[])[0]||{}).diagnosis;
    el.innerHTML = diag
      ? `<b>根因 / Root cause：</b>${escHtml(diag.root_cause||'-')}<br><b>建議 / Suggestion：</b>${escHtml(diag.suggestion||'-')}`
      : '無法取得診斷（可能是監控模型尚未啟動） / Diagnosis unavailable';
  }catch(e){ el.textContent = 'Error: '+e; }
}

function _fmtChartTime(iso){
  try{ return new Date(iso).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'}); }
  catch(e){ return ''; }
}

function renderRestartChart(history){
  if(!history || history.length < 2){
    return '<div class="pod-chart-empty">觀察中，累積更多資料後會顯示趨勢圖（背景每 30 秒取樣一次） / '+
           'Collecting data — the trend chart appears once enough samples are recorded (sampled every 30s)</div>';
  }
  const w=560, h=100, pad=24;
  const restarts = history.map(s => s.restarts||0);
  const maxR = Math.max(1, ...restarts);
  const stepX = (w - pad*2) / (history.length - 1);
  const pts = restarts.map((v,i) => [pad + i*stepX, h - pad - (v/maxR)*(h-pad*2)]);
  const path = pts.map((p,i) => (i===0?'M':'L') + p[0].toFixed(1) + ',' + p[1].toFixed(1)).join(' ');
  return `<svg viewBox="0 0 ${w} ${h}" style="width:100%;height:${h}px;display:block">
    <path d="${path}" fill="none" stroke="#2563EB" stroke-width="2"/>
    ${pts.map(p=>`<circle cx="${p[0].toFixed(1)}" cy="${p[1].toFixed(1)}" r="2.5" fill="#2563EB"/>`).join('')}
    <text x="${pad}" y="${h-4}" font-size="10" fill="#9CA3AF">${escHtml(_fmtChartTime(history[0].t))}</text>
    <text x="${w-pad}" y="${h-4}" font-size="10" fill="#9CA3AF" text-anchor="end">${escHtml(_fmtChartTime(history[history.length-1].t))}</text>
    <text x="${w-pad}" y="14" font-size="11" fill="#2563EB" text-anchor="end">目前 / current: ${restarts[restarts.length-1]}</text>
  </svg>`;
}

function renderHealthTimeline(history){
  if(!history || history.length < 2){
    return '<div class="pod-chart-empty">觀察中，累積更多資料後會顯示趨勢圖（背景每 30 秒取樣一次） / '+
           'Collecting data — the trend chart appears once enough samples are recorded (sampled every 30s)</div>';
  }
  const w=560, h=28;
  const segW = w / history.length;
  const rects = history.map((s,i) => {
    const healthy = s.total>0 && s.ready===s.total && s.phase==='Running';
    return `<rect x="${(i*segW).toFixed(1)}" y="0" width="${Math.ceil(segW)}" height="${h}" fill="${healthy?'#16A34A':'#DC2626'}"/>`;
  }).join('');
  return `<svg viewBox="0 0 ${w} ${h}" style="width:100%;height:${h}px;display:block;border-radius:4px;overflow:hidden">${rects}</svg>
    <div style="display:flex;justify-content:space-between;font-size:10px;color:#9CA3AF;margin-top:2px">
      <span>${escHtml(_fmtChartTime(history[0].t))}</span><span>${escHtml(_fmtChartTime(history[history.length-1].t))}</span>
    </div>`;
}

async function fixPod(pod){
  const r=await fetch('/api/healer/fix',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({pod_name:pod})});
  const d=await r.json();
  let msg=d.message||d.error||'Done';
  if(d.root_cause) msg=`根因 / Root cause：${d.root_cause}\n修復動作 / Action：${d.action||'-'}\n\n${msg}`;
  alert(msg);
  loadPodList();
  loadHealerBgStatus();
  if(_podDetailCurrent === pod) openPodDetail(pod);  // 修復後如果詳情頁還開著，重新整理它
}

async function healerAutoFix(){
  if(!confirm('Auto fix all issues?'))return;
  const r=await fetch('/api/healer/auto_fix',{method:'POST'});
  const d=await r.json();
  let msg='Fixed: '+d.fixed+', Failed: '+d.failed;
  if(Array.isArray(d.details) && d.details.length){
    msg+='\n\n'+d.details.map(x=>`- ${x.pod}: ${x.root_cause||x.error||'-'} → ${x.action||'-'}`).join('\n');
  }
  alert(msg);
  loadPodList();
  loadHealerBgStatus();
}

async function loadHealerBgStatus(){
  const banner=document.getElementById('healer-bg-banner');
  const list=document.getElementById('healer-bg-list');
  if(!banner||!list) return;
  try{
    const r=await fetch('/api/healer/status'); const d=await r.json();
    if(d.running){
      banner.style.background='#ECFDF5'; banner.style.border='1px solid #6EE7B7'; banner.style.color='#065F46';
      banner.textContent=`🟢 自動監控中，每 30 秒自動掃描一次${d.last_scan?'，上次掃描：'+new Date(d.last_scan).toLocaleTimeString():''} / Auto-monitoring active, scans every 30s`;
    } else if(d.message){
      // K8s 有連線，但背景執行緒死掉或卡住了——這是真正的異常，不是「K8s 沒連線」這種
      // 正常情況，用紅色橫幅+具體建議動作顯示，不能只顯示跟正常運行時一樣的灰/黃色調。
      banner.style.background='#FEF2F2'; banner.style.border='1px solid #FCA5A5'; banner.style.color='#991B1B';
      banner.textContent='🔴 '+d.message;
    } else {
      banner.style.background='#FFFBEB'; banner.style.border='1px solid #FCD34D'; banner.style.color='#92400E';
      banner.textContent='⚠️ 未啟動自動監控（K8s 未連線），只能手動掃描 / Auto-monitoring is not running (K8s not connected) — manual scan only';
    }
    const acts=d.recent_actions||[];
    list.innerHTML = acts.length ? acts.map(a=>`
      <div style="border:1px solid var(--border);border-radius:8px;padding:10px;margin-bottom:6px">
        <div style="display:flex;justify-content:space-between;font-size:12px">
          <span style="font-weight:600">${escHtml(a.pod||'')}</span>
          <span style="color:var(--text3)">${a.time?new Date(a.time).toLocaleTimeString():''}</span>
        </div>
        <div style="font-size:12px;color:#6B7280;margin-top:2px">${escHtml(a.reason||'')} → ${escHtml(a.root_cause||a.error||'-')} → ${escHtml(a.action||'-')} ${a.ok===false?'❌':'✅'}</div>
      </div>`).join('') : '<div style="color:var(--text3);font-size:13px">尚未發生自動修復事件 / No auto-heal events yet</div>';
  }catch(e){}
}

async function loadMetrics(){
  // 2026-09-15：之前這裡完全沒讀 m.prometheus_up（後端算好了但前端沒用），
  // 「Online」是只要這個 API 路由本身沒有拋例外就顯示，跟 Prometheus 有沒有
  // 真的連得上完全無關——即使 Prometheus 掛了，畫面還是會唬弄使用者說 Online。
  document.getElementById('prom-status').textContent='Checking';
  try{
    const r=await fetch('/api/metrics'); const d=await r.json();
    if(!d.connected){
      document.getElementById('prom-status').textContent='Error';
      document.getElementById('prom-pods').textContent='--';
      document.getElementById('prom-url').textContent='Error';
      document.getElementById('metrics-rows').innerHTML='<div style="color:var(--text3)">查詢失敗 / Query failed: '+escHtml(d.error||'')+'</div>';
      const errLink = document.getElementById('prom-full-ui-link');
      errLink.href = '#'; errLink.textContent = '無法取得位址 / Unavailable';
      return;
    }
    const m=d.metrics||{};
    const up = !!m.prometheus_up;
    document.getElementById('prom-status').textContent = up ? 'Online' : 'Offline';
    document.getElementById('prom-status').style.color = up ? 'var(--green)' : '#DC2626';
    document.getElementById('prom-pods').textContent=m.running_pods!=null?m.running_pods:'N/A';
    document.getElementById('prom-url').textContent=d.url||'localhost:9090';
    // 2026-09-15：這個連結之前是寫死 http://192.168.50.219:30922（很久以前某次
    // 遠端/NodePort 設定殘留下來的舊網址），跟現在真正部署的 Prometheus 位址完全
    // 對不上，點下去會連到不存在的地方——改成用 /api/metrics 剛回的真實 url，
    // 跟畫面上 Endpoint 那張卡片顯示的是同一個值，不會再兩邊講不同的位址。
    const fullUiLink = document.getElementById('prom-full-ui-link');
    const promUrl = d.url || '';
    fullUiLink.href = promUrl || '#';
    fullUiLink.textContent = promUrl ? 'Prometheus' : '無法取得位址 / Unavailable';
    const rows = [
      ['Prometheus', up ? 'UP' : 'DOWN'],
      ['你的 Pod 數 / Your Pods',m.pod_count!=null?m.pod_count:'N/A'],
      ['你的 Running Pods / Your Running',m.running_pods!=null?m.running_pods:'N/A'],
    ];
    if(up){
      rows.push(['整個叢集 Pod 數 / Cluster-wide Pods',m.cluster_pods!=null?m.cluster_pods:'N/A']);
    } else {
      // Prometheus 連不上，但 Pod 數字仍然是真的（改用 K8s API 直接查），要講清楚
      // 資料來源改變了，不能讓使用者以為 Prometheus 連得上、資料是它給的。
      rows.push(['資料來源 / Data source', '直接查 K8s API（Prometheus 連不上） / Direct K8s API (Prometheus unreachable)']);
    }
    document.getElementById('metrics-rows').innerHTML = rows
      .map(([k,v])=>'<div style="display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid var(--border)"><span style="color:var(--text2);font-size:12px">'+k+'</span><span style="font-size:12px;font-weight:500">'+v+'</span></div>').join('');
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

// ── Chat 多步動作流程狀態（部署 4 步 / 破壞性操作 2 步）──────────────
// 只存在記憶體，不寫 localStorage；卡片 HTML 存進 message.content，
// 每張卡另外內嵌一份 JSON 狀態 blob，重整後由 hydrateChatFlows() 判定是否失效。
let chatFlows = {};
let chatFlowSeq = 0;

function newFlow(kind, originalText, spec){
  const seq = ++chatFlowSeq;
  const id = 'flow_' + seq + '_' + Date.now();
  chatFlows[id] = {
    id, seq, kind, step:'spec',
    originalText: originalText || '',
    spec: spec || {}, review:null, resource:null,
    createdAt: Date.now(),
  };
  return chatFlows[id];
}

// async 續傳前先驗證：流程還在、且沒有被更新的同 id 流程蓋掉
function flowAlive(flow){
  return flow && chatFlows[flow.id] && chatFlows[flow.id].seq === flow.seq
    && !['cancelled','expired'].includes(chatFlows[flow.id].step);
}

// 把 flow 目前狀態重繪進它對應的聊天訊息，並存檔
function persistFlowCard(id){
  const flow = chatFlows[id];
  if(!flow) return;
  const html = renderFlowCard(flow);
  const ch = currentChat();
  if(ch){
    const msg = ch.messages.find(m => m.role==='assistant' && String(m.content||'').includes('data-flow-id="'+id+'"'));
    if(msg){ msg.content = html; saveChats(); }
  }
  const dom = document.querySelector('[data-flow-id="'+id+'"]');
  if(dom){ const wrap = document.createElement('div'); wrap.innerHTML = html; dom.replaceWith(wrap.firstElementChild); }
}

// 重整頁面後：非終態的流程一律標記失效（記憶體狀態已消失，按鈕會指向死 id）
function hydrateChatFlows(){
  const ch = currentChat();
  if(!ch) return;
  ch.messages.forEach(m => {
    if(m.role!=='assistant') return;
    const mm = String(m.content||'').match(/data-flow-state="([^"]*)"/);
    if(!mm) return;
    let st; try { st = JSON.parse(decodeURIComponent(mm[1])); } catch(e){ return; }
    if(['done','error','cancelled'].includes(st.step)) return;   // 終態卡照存的樣子
    st.step = 'expired';
    chatFlows[st.id] = st;
    m.content = renderFlowCard(st);
  });
  saveChats();
}

function _chatsKey(){ return 'k8s_chats_' + CURRENT_USER; }
function _currentChatKey(){ return 'k8s_current_chat_' + CURRENT_USER; }

function initChats(){
  try { chats = JSON.parse(localStorage.getItem(_chatsKey())||'[]'); } catch(e){ chats=[]; }
  currentChatId = localStorage.getItem(_currentChatKey()) || null;
  if(!chats.length){
    const id = 'chat_' + Date.now();
    chats.push({id, title:'New Chat', messages:[]});
    currentChatId = id;
    saveChats();
  }
  if(!currentChatId || !chats.find(ch=>ch.id===currentChatId)){
    currentChatId = chats[chats.length-1].id;
  }
  hydrateChatFlows();
  renderChatList();
  renderMessages();
}

function saveChats(){
  localStorage.setItem(_chatsKey(), JSON.stringify(chats));
  localStorage.setItem(_currentChatKey(), currentChatId||'');
}

function newChat(){
  if(!chats.length){
    try { chats = JSON.parse(localStorage.getItem(_chatsKey())||'[]'); } catch(e){ chats=[]; }
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
  hydrateChatFlows();
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
    const t = I18N[uiLang];
    const cardsHtml = t.cards.map(c =>
      `<div class="prompt-card" onclick="document.getElementById('chat-input').value=${JSON.stringify(c.send)};sendChat()">
         <strong>${escHtml(c.title)}</strong><span>${escHtml(c.example)}</span>
       </div>`).join('');
    msgs.innerHTML = `<div class="chat-empty">
      <div class="chat-empty-logo">K</div>
      <h1>${escHtml(t.emptyTitle)}</h1>
      <p>${escHtml(t.emptyDesc)}</p>
      <div class="prompt-grid">${cardsHtml}</div>
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

function appendTyping(label){
  const msgs = document.getElementById('chat-messages');
  let div = document.getElementById('typing-indicator');
  if(!div){
    div = document.createElement('div');
    div.className = 'msg ai'; div.id = 'typing-indicator'; div.style.marginBottom='16px';
    msgs.appendChild(div);
  }
  const txt = label || 'Thinking';
  div.innerHTML = '<div class="msg-avatar">K</div><div class="think-row">'+
    '<div class="spinner"></div><span data-role="think-label">'+escHtml(txt)+'</span>'+
    '<span class="think-dots"><span>.</span><span>.</span><span>.</span></span></div>';
  msgs.scrollTop = msgs.scrollHeight;
}
// 更新「思考中」文字但不重建元素（沒有 indicator 就建一個）
function setTyping(label){ appendTyping(label); }

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
      setDeployConfirmError(root, `GitOps 已提交，但 K8s 實際部署失敗 / GitOps committed, but the K8s deploy failed：${d.k8s_deploy.message}`);
      root.querySelectorAll('input,button').forEach(el=>el.disabled=false);
      if(badge){ badge.textContent='K8s failed'; badge.className='badge failed'; }
      return;
    }
    const p = d.parsed || parsed;
    const appName = p.app_name || parsed.app_name;
    const pods = p.pods || parsed.pods;
    const image = p.image || parsed.image;
    const port = p.port || parsed.port;
    const successText = `部署成功 / Deployed：${appName}，Pods: ${pods}，Image: ${image}${port ? '，Port: ' + port : ''}。可到 Pods / Deployments 頁查看狀態。 You can check status on the Pods / Deployments pages.`;
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

// \u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550
//  Chat \u610f\u5716\u8fa8\u8b58\uff08\u898f\u5247\u5c64\uff0c\u524d\u7aef\uff1b\u6bd4\u5c0d\u4e0d\u5230\u624d\u6253 /api/intent\uff09
// \u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550
const DESTRUCTIVE_KINDS = ['scale','update_image','rollback','delete','healer_fix','healer_auto_fix'];
const READ_ACTIONS = ['list_pods','list_deployments','gitops_log','cluster_metrics','healer_scan',
                      'describe_pod','pod_health','describe_deployment'];

function matchClientRule(text){
  const t = text.trim();
  const rules = [
    ['list_pods', /^(list|show|\u67e5\u770b|\u986f\u793a|\u5217\u51fa)\s*(all\s*|\u6240\u6709|\u5168\u90e8)?\s*(pods?|\u5bb9\u5668)/i, null],
    ['list_deployments', /^(list|show|\u67e5\u770b|\u986f\u793a|\u5217\u51fa)\s*(all\s*|\u6240\u6709|\u5168\u90e8)?\s*(deploy(ment)?s?|\u90e8\u7f72)/i, null],
    ['gitops_log', /^(gitops|deploy history|git ?log)|\u90e8\u7f72(\u7d00\u9304|\u6b77\u53f2|\u8a18\u9304)/i, null],
    ['cluster_metrics', /^(metrics|cluster (status|health)|\u53e2\u96c6(\u72c0\u614b|\u5065\u5eb7)|\u6307\u6a19)/i, null],
    ['healer_auto_fix', /^(auto ?fix|fix all|\u81ea\u52d5\u4fee\u5fa9|\u5168\u90e8\u4fee\u5fa9)/i, null],
    ['healer_fix', /^(?:fix|\u4fee\u5fa9)\s+(\S+)/i, m=>({pod_name:m[1]})],
    ['healer_scan', /^(healer|scan)\b|\u6383\u63cf.*(pod|\u58de|\u7570\u5e38)/i, null],
    ['pod_health', /([a-zA-Z0-9][\w.-]*)\s*(?:\u9019\u500b)?\s*(?:pod|deployment|\u90e8\u7f72)?\s*(?:\u6709\u6c92\u6709\u58de|\u58de\u4e86\u6c92|\u58de\u6389\u4e86\u55ce|\u58de\u4e86\u55ce|\u6b63\u5e38\u55ce|\u5065\u5eb7\u55ce|\u639b\u4e86\u55ce|\u639b\u6389\u4e86\u55ce|\u9084\u6d3b\u8457\u55ce|\u6c92\u4e8b\u5427|\u6709\u554f\u984c\u55ce|ok\s*\u55ce)/i, m=>({name:m[1]})],
    ['pod_health', /^is\s+([a-zA-Z0-9][\w.-]*)\s+(?:ok|okay|healthy|broken|down|up|crashing|running|alive)/i, m=>({name:m[1]})],
    ['describe_deployment', /(?:deployment|\u90e8\u7f72)\s+([a-zA-Z0-9][\w.-]*)\s*(?:\u7684?\s*(?:\u72c0\u614b|\u7d30\u7bc0|\u8a73\u60c5|status))?/i, m=>({name:m[1]})],
    ['describe_deployment', /([a-zA-Z0-9][\w.-]*)\s*(?:\u9019\u500b)?\s*(?:deployment|\u90e8\u7f72)\s*(?:\u7684?\s*(?:\u72c0\u614b|\u7d30\u7bc0|\u8a73\u60c5))/i, m=>({name:m[1]})],
    ['describe_pod', /(?:\u67e5\u770b|detail(?:s)?\s*(?:of)?|describe|\u6aa2\u8996)\s*(?:pod\s+)?([a-zA-Z0-9][\w.-]*)/i, m=>({name:m[1]})],
    ['describe_pod', /([a-zA-Z0-9][\w.-]*)\s*(?:\u9019\u500b)?\s*pod\s*(?:\u7684?\s*(?:\u7d30\u7bc0|\u72c0\u614b|\u8a73\u60c5|\u8cc7\u8a0a))/i, m=>({name:m[1]})],
    ['describe_pod', /([a-zA-Z0-9][\w.-]*)\s*(?:\u7684\s*(?:\u7d30\u7bc0|\u8a73\u60c5|\u8a73\u7d30\u72c0\u614b))/i, m=>({name:m[1]})],
    ['delete', /^(?:delete|remove|\u522a\u9664|del)\s+(\S+)/i, m=>({name:m[1]})],
    ['scale', /scale\s+(\S+)\s+to\s+(\d+)/i, m=>({name:m[1],replicas:parseInt(m[2],10)})],
    ['scale', /\u628a?\s*(\S+?)\s*(?:\u64f4|\u7e2e|\u8abf).*?(\d+)/i, m=>({name:m[1],replicas:parseInt(m[2],10)})],
    ['update_image', /update\s+(\S+)\s+to\s+(\S+)/i, m=>({name:m[1],image:m[2]})],
    ['update_image', /\u628a?\s*(\S+?)\s*\u7684?\s*(?:image|\u6620\u50cf|\u93e1\u50cf)\s*(?:\u63db\u6210|\u6539\u6210|\u6539\u70ba|to)?\s*(\S+)/i, m=>({name:m[1],image:m[2]})],
    ['rollback', /rollback\s+(\S+)/i, m=>({name:m[1]})],
    ['rollback', /(\S+)\s*(?:\u56de\u6efe|\u56de\u5fa9|\u9084\u539f)/i, m=>({name:m[1]})],
  ];
  for(const [action, rx, extract] of rules){
    const m = t.match(rx);
    if(!m) continue;
    const args = extract ? extract(m) : {};
    if(extract && Object.values(args).some(v=>v===undefined||v===''||(typeof v==='number'&&isNaN(v)))) continue;
    return {action, args, source:'rule'};
  }
  // deploy\uff1a\u6cbf\u7528\u539f\u672c\u7684\u8907\u5408\u5224\u65b7
  if(/^(deploy|start|launch|run|spin\s+up|\u90e8\u7f72|\u4f48\u7f72|\u90e8\u5c6c|\u8d77\s)/i.test(t) ||
     ((/\u5e6b\u6211|\u8acb/i.test(t)) && /(deploy|\u90e8\u7f72|\u4f48\u7f72|\u90e8\u5c6c|pods?|pod|\u8d77)/i.test(t) &&
      (/\d+\s*(pods?|replicas?|\u500b|\u526f\u672c)/i.test(t) || /(nginx|redis|postgres|node|python|golang|image|\u6620\u50cf|\u93e1\u50cf)/i.test(t)))){
    return {action:'deploy', args:{}, source:'rule'};
  }
  return null;
}

// \u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550
//  \u591a\u6b65\u6d41\u7a0b\u5361\u7247 renderer\uff08HTML \u5b57\u4e32\uff0c\u5b58\u9032 message.content\uff09
// \u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550
function flowStateAttrs(flow){
  const st = {id:flow.id, seq:flow.seq, kind:flow.kind, step:flow.step,
             originalText:flow.originalText, spec:flow.spec, review:flow.review, resource:flow.resource};
  return `data-flow-id="${flow.id}" data-flow-seq="${flow.seq}" data-flow-step="${flow.step}" `+
         `data-flow-state="${encodeURIComponent(JSON.stringify(st))}"`;
}
function flowShell(flow, inner){
  return `<div class="deploy-confirm" ${flowStateAttrs(flow)}>${inner}</div>`;
}
function esc(v){ return escHtml(String(v==null?'':v)); }

function renderFlowCard(flow){
  if(flow.step==='expired'){
    return flowShell(flow, `<div class="deploy-confirm-head"><div>
      <div class="deploy-confirm-title">\u6b64\u8acb\u6c42\u5df2\u5931\u6548 / Request expired</div>
      <div class="deploy-confirm-sub">\u9801\u9762\u91cd\u65b0\u6574\u7406\u5f8c\u6d41\u7a0b\u72c0\u614b\u5df2\u907a\u5931\uff0c\u5982\u4ecd\u9700\u8981\u8acb\u91cd\u65b0\u8f38\u5165\u3002<br>The flow state was lost on reload \u2014 please resend if you still need it.</div>
      </div><span class="badge failed">Expired</span></div>`);
  }
  if(flow.kind==='deploy'){
    if(flow.step==='spec') return renderSpecCard(flow);
    if(flow.step==='review') return renderReviewCard(flow);
    if(flow.step==='executing') return renderBusyCard(flow, 'Deploying');
    if(flow.step==='done') return renderDoneCard(flow);
    if(flow.step==='error') return renderErrorCard(flow);
  } else {
    if(flow.step==='spec') return renderDestructiveCard(flow);
    if(flow.step==='executing') return renderBusyCard(flow, 'Working');
    if(flow.step==='done') return renderActionDoneCard(flow);
    if(flow.step==='error') return renderErrorCard(flow);
  }
  return flowShell(flow, '<div class="deploy-confirm-sub">\u2026</div>');
}

function renderBusyCard(flow, label){
  return flowShell(flow, `<div class="deploy-confirm-head">
    <div style="display:flex;align-items:center;gap:10px">
      <div class="spinner"></div>
      <div>
        <div class="deploy-confirm-title">${esc(label)}</div>
      </div>
    </div>
    <span class="badge pending">Working</span></div>`);
}

// \u2500\u2500 B1 \u898f\u683c\u78ba\u8a8d\u5361\uff08\u90e8\u7f72\u7b2c 1 \u6b65\uff09\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
function renderSpecCard(flow){
  const s = flow.spec || {};
  const out = s.output || s;
  const g = (k)=> esc(out[k] != null ? out[k] : (s[k] != null ? s[k] : ''));
  const disabled = '';
  return flowShell(flow, `
    <div class="deploy-confirm-head"><div>
      <div class="deploy-confirm-title">\u9019\u662f\u4f60\u60f3\u8981\u7684\u90e8\u7f72\u55ce\uff1f / Is this what you want to deploy?</div>
      <div class="deploy-confirm-sub">\u6709\u932f\u5c31\u76f4\u63a5\u6539\uff0c\u7136\u5f8c\u6309\u300c\u4e0b\u4e00\u6b65\u300d\u770b\u8cc7\u6e90\u7528\u91cf\u8207\u5be9\u67e5\u3002<br>Edit anything wrong, then continue to the resource &amp; review step.</div>
    </div><span class="badge pending">Step 1 / 3</span></div>
    <div class="deploy-confirm-grid">
      <div class="deploy-confirm-field"><label>App name</label><input data-field="app_name" value="${g('app_name')}" placeholder="my-app" ${disabled}></div>
      <div class="deploy-confirm-field"><label>Image</label><input data-field="image" value="${g('image')}" placeholder="nginx:latest" ${disabled}></div>
      <div class="deploy-confirm-field"><label>Pods</label><input data-field="pods" type="number" min="1" max="100" value="${g('pods')||1}" ${disabled}></div>
      <div class="deploy-confirm-field"><label>Port</label><input data-field="port" type="number" min="1" max="65535" value="${g('port')||80}" ${disabled}></div>
      <div class="deploy-confirm-field"><label>Memory / pod</label><input data-field="memory" value="${g('memory')}" placeholder="\u9078\u586b e.g. 128Mi" ${disabled}></div>
      <div class="deploy-confirm-field"><label>CPU / pod</label><input data-field="cpu" value="${g('cpu')}" placeholder="\u9078\u586b e.g. 100m" ${disabled}></div>
    </div>
    <div class="deploy-confirm-error" data-role="error"></div>
    <div class="deploy-confirm-actions">
      <button class="deploy-confirm-btn" onclick="cancelFlow('${flow.id}')">\u53d6\u6d88 / Cancel</button>
      <button class="deploy-confirm-btn primary" onclick="flowToReview('${flow.id}')">\u4e0b\u4e00\u6b65\uff1a\u6aa2\u8996\u8cc7\u6e90 / Next</button>
    </div>`);
}
function readSpecCard(id){
  const root = document.querySelector(`[data-flow-id="${id}"]`);
  if(!root) return null;
  const get = f => (root.querySelector(`[data-field="${f}"]`)?.value||'').trim();
  const spec = { app_name:get('app_name'), image:get('image'),
                 pods:parseInt(get('pods'),10), port:parseInt(get('port'),10) };
  const mem = get('memory'), cpu = get('cpu');
  if(mem) spec.memory = mem;
  if(cpu) spec.cpu = cpu;
  return {root, spec};
}

// \u2500\u2500 B2 \u8cc7\u6e90 + \u4e09\u65b9\u5be9\u67e5\u5361\uff08\u90e8\u7f72\u7b2c 2 \u6b65\uff09\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
function resourceRow(label, val){
  return `<tr><td style="padding:4px 10px 4px 0;color:var(--text3)">${esc(label)}</td>
          <td style="padding:4px 0;font-family:'DM Mono',monospace">${esc(val)}</td></tr>`;
}
function resourceTableHTML(rs){
  if(!rs) return '';
  const nb = rs.node_bound ? `\uff08\u53d7${rs.node_bound==='cpu'?'CPU':'\u8a18\u61b6\u9ad4'}\u9650\u5236 / ${rs.node_bound}-bound\uff09` : '';
  const src = rs.node_source==='llm' ? '\u6a21\u578b\u5224\u65b7 / from model' : '\u975c\u614b\u5bb9\u91cf\u4f30\u7b97 / static-capacity estimate';
  return `<table style="border-collapse:collapse;font-size:12px;margin:4px 0 10px">
    ${resourceRow('\u526f\u672c\u6578 / Replicas', rs.replicas)}
    ${resourceRow('\u6bcf\u500b Pod \u8a18\u61b6\u9ad4 / Mem per pod', rs.per_pod_mem)}
    ${resourceRow('\u6bcf\u500b Pod CPU / CPU per pod', rs.per_pod_cpu)}
    ${resourceRow('\u7e3d\u8a18\u61b6\u9ad4\uff08\u526f\u672c\u00d7\u6bcf\u500b\uff09/ Total mem', rs.total_mem_gib!=null ? rs.total_mem_gib+' GiB' : '\u2014')}
    ${resourceRow('\u7e3d CPU / Total CPU', rs.total_cpu_cores!=null ? rs.total_cpu_cores+' cores' : '\u2014')}
    ${resourceRow('\u9810\u4f30\u7bc0\u9ede\u6578 / Est. nodes', (rs.node_count!=null ? rs.node_count : '\u2014') + ' ' + nb)}
    ${resourceRow('\u7bc0\u9ede\u6578\u4f86\u6e90 / Node est. source', src)}
    ${resourceRow('\u9810\u4f30\u6bcf\u6708\u6210\u672c / Est. monthly', rs.monthly_usd!=null ? ('$'+rs.monthly_usd+' USD') : '\u2014')}
  </table>`;
}
function agentMiniCard(title, badge, issues, summary){
  const pills = (issues||[]).map(i=>{
    const sev = (i.severity||'info');
    const cls = ['critical','high'].includes(sev)?'complex':(sev==='medium'?'medium':'simple');
    return `<span class="pill ${cls}">${esc(sev)}: ${esc(i.message||'')}</span>`;
  }).join(' ');
  return `<div style="border:1px solid var(--border);border-radius:9px;padding:8px 10px;margin:4px 0">
    <div style="display:flex;justify-content:space-between;font-size:12px;font-weight:700">
      <span>${esc(title)}</span><span style="color:var(--text3)">${esc(badge||'')}</span></div>
    <div style="margin:4px 0">${pills}</div>
    <div style="font-size:11.5px;color:var(--text2)">${esc(summary||'')}</div></div>`;
}
function agentTrioHTML(review){
  const a = review && review.agents && review.agents.agents;
  if(!a) return '<div class="deploy-confirm-sub">\uff08\u5be9\u67e5\u8cc7\u6599\u4e0d\u5b8c\u6574 / review data incomplete\uff09</div>';
  const s=a.security||{}, c=a.cost||{}, p=a.perf||{};
  const cEst = c.cost_estimate && c.cost_estimate.estimated_usd;
  return agentMiniCard('\u5b89\u5168 Security', (typeof s.score==='number'?s.score+'/100':''), s.issues, s.summary)
       + agentMiniCard('\u6210\u672c Cost', (cEst!=null?('$'+cEst+'/mo'):''), c.issues, c.summary)
       + agentMiniCard('\u6548\u80fd Performance', (p.hpa_yaml?'\u5efa\u8b70 HPA / HPA suggested':''), p.issues, p.summary);
}
function renderReviewCard(flow){
  const rv = flow.review || {};
  const decision = rv.decision || 'block';
  let banner, actions;
  if(decision==='approve'){
    banner = `<div class="result-box court-verdict success">\u2713 \u901a\u904e / Approved \u2014 ${esc(rv.reason||'\u4e09\u65b9\u6aa2\u67e5\u5168\u90e8\u901a\u904e')}</div>`;
    actions = `<button class="deploy-confirm-btn" onclick="cancelFlow('${flow.id}')">\u53d6\u6d88 / Cancel</button>
      <button class="deploy-confirm-btn primary" onclick="flowExecuteDeploy('${flow.id}')">\u78ba\u8a8d\u90e8\u7f72 / Deploy</button>`;
  } else if(decision==='warn'){
    const ws = (rv.warnings||[]).map(w=>'\u00b7 '+esc(w)).join('<br>');
    banner = `<div class="result-box court-verdict warn">\u26a0 \u8b66\u544a / Warning \u2014 ${esc(rv.reason||'')}<br>${ws}</div>`;
    actions = `<button class="deploy-confirm-btn" onclick="cancelFlow('${flow.id}')">\u53d6\u6d88 / Cancel</button>
      <button class="deploy-confirm-btn primary" onclick="flowExecuteDeploy('${flow.id}')">\u4ecd\u8981\u90e8\u7f72 / Deploy anyway</button>`;
  } else {
    const bs = (rv.blockers||[]).map(b=>'\u00b7 '+esc(b)).join('<br>');
    banner = `<div class="result-box court-verdict error">\u2717 \u963b\u64cb / Blocked \u2014 ${esc(rv.reason||'')}<br>${bs}</div>`;
    actions = `<button class="deploy-confirm-btn" onclick="cancelFlow('${flow.id}')">\u95dc\u9589 / Close</button>`;
  }
  return flowShell(flow, `
    <div class="deploy-confirm-head"><div>
      <div class="deploy-confirm-title">\u8cc7\u6e90\u7528\u91cf\u8207\u4e09\u65b9\u5be9\u67e5 / Resources &amp; review</div>
      <div class="deploy-confirm-sub">${esc(flow.spec.app_name||'')} \u00b7 ${esc(flow.spec.image||'')}</div>
    </div><span class="badge pending">Step 2 / 3</span></div>
    ${resourceTableHTML(flow.resource)}
    ${agentTrioHTML(flow.review)}
    <div style="margin:10px 0">${banner}</div>
    <div class="deploy-confirm-actions">${actions}</div>`);
}

// \u2500\u2500 B3 \u7834\u58de\u6027\u64cd\u4f5c\u78ba\u8a8d\u5361 \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
function destructiveSummary(flow){
  const a = flow.spec || {};
  if(flow.kind==='scale') return `${esc(a.name)}\uff1a${a.current!=null?a.current:'?'} \u2192 ${esc(a.replicas)} \u526f\u672c / replicas`;
  if(flow.kind==='update_image') return `${esc(a.name)} image\uff1a${esc(a.current||'?')} \u2192 ${esc(a.image)}`;
  if(flow.kind==='rollback') return `${esc(a.name)} \u2192 \u56de\u6efe\u5230\u4e0a\u4e00\u7248 / roll back to previous version`;
  if(flow.kind==='delete') return `\u522a\u9664 Deployment\u300c${esc(a.name)}\u300d+ \u5176 Service\uff08\u4e0d\u53ef\u5fa9\u539f / cannot be undone\uff09`;
  if(flow.kind==='healer_fix') return `\u522a\u9664 Pod\u300c${esc(a.pod_name)}\u300d\u8b93 ReplicaSet \u91cd\u5efa`;
  if(flow.kind==='healer_auto_fix') return `\u522a\u9664\u6240\u6709\u7570\u5e38 Pod\uff08CrashLoopBackOff / OOMKilled / ImagePull\u2026 \uff09\u8b93\u5176\u91cd\u5efa`;
  return '';
}
function renderDestructiveCard(flow){
  const k8sOff = (flow.kind==='scale'||flow.kind==='update_image') && k8sEnabled===false;
  return flowShell(flow, `
    <div class="deploy-confirm-head"><div>
      <div class="deploy-confirm-title">\u78ba\u8a8d\u64cd\u4f5c / Confirm action</div>
      <div class="deploy-confirm-sub">${destructiveSummary(flow)}</div>
    </div><span class="badge pending">Confirm</span></div>
    <div class="deploy-confirm-error" data-role="error"></div>
    ${k8sOff ? '<div class="deploy-confirm-note">K8s \u672a\u9023\u7dda\uff0c\u9019\u500b\u64cd\u4f5c\u7121\u6cd5\u57f7\u884c / K8s not connected \u2014 this action cannot run.</div>' : ''}
    <div class="deploy-confirm-actions">
      <button class="deploy-confirm-btn" onclick="cancelFlow('${flow.id}')">\u53d6\u6d88 / Cancel</button>
      <button class="deploy-confirm-btn primary" onclick="flowExecuteAction('${flow.id}')" ${k8sOff?'disabled':''}>\u78ba\u8a8d / Confirm</button>
    </div>`);
}

// \u2500\u2500 B4 \u5b8c\u6210 / \u932f\u8aa4\u5361 \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
function navBtn(page, label){
  return `<button class="deploy-confirm-btn" onclick="showPage('${page}')">${esc(label)}</button>`;
}
function renderDoneCard(flow){
  const p = flow.spec || {};
  return flowShell(flow, `
    <div class="deploy-confirm-head"><div>
      <div class="deploy-confirm-title">\u2713 \u90e8\u7f72\u5b8c\u6210 / Deployed</div>
      <div class="deploy-confirm-sub">${esc(p.app_name)} \u00b7 Pods ${esc(p.pods)} \u00b7 ${esc(p.image)}${p.port?(' \u00b7 Port '+esc(p.port)):''}</div>
    </div><span class="badge success">Done</span></div>
    <div class="deploy-confirm-note">\u53ef\u5230\u4ee5\u4e0b\u5206\u9801\u67e5\u770b\u525b\u525b\u90e8\u7f72\u7684\u5167\u5bb9 / Check what was just deployed:</div>
    <div class="deploy-confirm-actions" style="justify-content:flex-start">
      ${navBtn('pods','\u524d\u5f80 Pods')} ${navBtn('deployments','\u524d\u5f80 Deployments')} ${navBtn('gitops','GitOps \u7d00\u9304')}
    </div>`);
}
function renderActionDoneCard(flow){
  return flowShell(flow, `
    <div class="deploy-confirm-head"><div>
      <div class="deploy-confirm-title">\u2713 \u5b8c\u6210 / Done</div>
      <div class="deploy-confirm-sub">${esc(flow.resultMsg || destructiveSummary(flow))}</div>
    </div><span class="badge success">Done</span></div>
    <div class="deploy-confirm-actions" style="justify-content:flex-start">
      ${navBtn('pods','\u524d\u5f80 Pods')} ${navBtn('deployments','\u524d\u5f80 Deployments')}
    </div>`);
}
function renderErrorCard(flow){
  const e = flow.errorMsg || '\u64cd\u4f5c\u5931\u6557 / action failed';
  const list = (flow.blockers||[]).map(b=>'\u00b7 '+esc(b)).join('<br>');
  return flowShell(flow, `
    <div class="deploy-confirm-head"><div>
      <div class="deploy-confirm-title">\u2717 \u672a\u5b8c\u6210 / Not completed</div>
      <div class="deploy-confirm-sub">${esc(e)}<br>${list}</div>
    </div><span class="badge failed">Failed</span></div>
    <div class="deploy-confirm-actions">
      <button class="deploy-confirm-btn" onclick="cancelFlow('${flow.id}')">\u95dc\u9589 / Close</button>
      ${flow.retryStep ? `<button class="deploy-confirm-btn primary" onclick="retryFlow('${flow.id}')">\u91cd\u8a66 / Retry</button>` : ''}
    </div>`);
}

// \u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550
//  \u6d41\u7a0b\u9a45\u52d5
// \u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550
function cancelFlow(id){
  const flow = chatFlows[id];
  if(!flow) return;
  flow.step = 'cancelled';
  flow.errorMsg = '\u5df2\u53d6\u6d88 / Cancelled';
  flow.retryStep = null;
  const html = flowShell(flow, `<div class="deploy-confirm-head"><div>
      <div class="deploy-confirm-title">\u5df2\u53d6\u6d88 / Cancelled</div></div>
      <span class="badge failed">Cancelled</span></div>`);
  const ch = currentChat();
  if(ch){
    const msg = ch.messages.find(m => m.role==='assistant' && String(m.content||'').includes('data-flow-id="'+id+'"'));
    if(msg){ msg.content = html; saveChats(); }
  }
  const dom = document.querySelector('[data-flow-id="'+id+'"]');
  if(dom){ const w=document.createElement('div'); w.innerHTML=html; dom.replaceWith(w.firstElementChild); }
}
function retryFlow(id){
  const flow = chatFlows[id];
  if(!flow || !flow.retryStep) return;
  flow.step = flow.retryStep;
  flow.errorMsg = null; flow.blockers = null;
  persistFlowCard(id);
}
function setFlowError(root, msg){
  const el = root && root.querySelector('[data-role="error"]');
  if(el){ el.style.display = msg?'block':'none'; el.textContent = msg||''; }
}

async function flowToReview(id){
  const flow = chatFlows[id];
  if(!flowAlive(flow)) return;
  const data = readSpecCard(id);
  if(!data) return;
  const {root, spec} = data;
  setFlowError(root, '');
  if(!spec.app_name || !spec.image){ setFlowError(root, 'App name \u548c image \u5fc5\u586b / required.'); return; }
  if(!Number.isInteger(spec.pods) || spec.pods<1 || spec.pods>100){ setFlowError(root, 'Pods \u5fc5\u9808\u5728 1\u2013100 \u4e4b\u9593.'); return; }
  if(!Number.isInteger(spec.port) || spec.port<1 || spec.port>65535){ setFlowError(root, 'Port \u5fc5\u9808\u5728 1\u201365535 \u4e4b\u9593.'); return; }
  if(spec.memory && !/^\d+(Mi|Gi|Ki|M|G)$/.test(spec.memory)){ setFlowError(root, 'Memory \u683c\u5f0f\u9808\u5982 128Mi \u6216 1Gi.'); return; }

  // 名稱已存在 / port 被佔用：在第一步就先問清楚，不是等審查跑完才在一堆警告裡看到。
  try{
    const cr = await fetch('/api/deploy/conflicts?app_name='+encodeURIComponent(spec.app_name)+'&port='+encodeURIComponent(spec.port));
    const cd = await cr.json();
    if(!flowAlive(flow)) return;
    if(cd.name_exists){
      const ok = await askConfirm(
        uiLang==='en' ? 'Name already exists' : '名稱已存在',
        uiLang==='en'
          ? `A Deployment named <b>${escHtml(spec.app_name)}</b> already exists. Continuing will <b>update it in place</b> instead of creating a new one. Replace it?`
          : `名稱「<b>${escHtml(spec.app_name)}</b>」已經存在，繼續的話會<b>直接取代／更新</b>現有的服務，不會建立新的。是否取代？`
      );
      if(!flowAlive(flow)) return;
      if(!ok) return;
    }
    if(cd.port_conflict){
      const ok = await askConfirm(
        uiLang==='en' ? 'Port already in use' : 'Port 已被佔用',
        uiLang==='en'
          ? `Port <b>${escHtml(spec.port)}</b> is already used by <b>${escHtml(cd.port_conflict)}</b>. Only one service can be externally reachable on this port, so this one may not be reachable from your browser. Continue anyway?`
          : `Port <b>${escHtml(spec.port)}</b> 已經被「<b>${escHtml(cd.port_conflict)}</b>」佔用，同一個 port 只有一個服務能真的對外連線，這次部署可能連不到。是否仍要繼續？`
      );
      if(!flowAlive(flow)) return;
      if(!ok) return;
    }
  }catch(e){ /* 檢查本身失敗就不擋流程，後面的審查步驟還會再檢查一次 */ }

  flow.spec = spec;
  // 換成 spinner 卡（不動 flow.step；結尾 persistFlowCard 會依真實狀態重繪）
  const busyHtml = renderBusyCard(flow, 'Reviewing');
  const dom = document.querySelector('[data-flow-id="'+id+'"]');
  if(dom){ const w=document.createElement('div'); w.innerHTML=busyHtml; dom.replaceWith(w.firstElementChild); }
  try{
    const r = await fetch('/api/deploy/parse',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({parsed:spec})});
    const d = await r.json();
    if(!flowAlive(flow)) return;
    if(d.error || d.rejected){
      flow.step='error';
      flow.errorMsg = d.error || d.reason || '\u5be9\u67e5\u672a\u901a\u904e / review failed';
      flow.blockers = (d.review && d.review.blockers) || [];
      flow.retryStep = 'spec';
    } else {
      flow.review = d.review || {};
      flow.resource = d.resource_summary || null;
      flow.spec = Object.assign({}, spec, {app_name:(d.raw&&d.raw.app_name)||spec.app_name});
      flow.step = 'review';
    }
  }catch(e){
    if(!flowAlive(flow)) return;
    flow.step='error'; flow.errorMsg='\u9023\u7dda\u932f\u8aa4 / network error: '+e; flow.retryStep='spec';
  }
  persistFlowCard(id);
}

async function flowExecuteDeploy(id){
  const flow = chatFlows[id];
  if(!flowAlive(flow)) return;
  flow.step = 'executing';
  persistFlowCard(id);
  try{
    const r = await fetch('/api/deploy',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({input:flow.originalText, parsed:flow.spec})});
    const d = await r.json();
    if(!flowAlive(flow)) return;
    if(d.error || d.rejected){
      flow.step='error'; flow.errorMsg = d.error || d.reason || '\u90e8\u7f72\u88ab\u963b\u64cb / blocked';
      flow.blockers = (d.review && d.review.blockers) || []; flow.retryStep='review';
    } else if(d.k8s_deploy && d.k8s_deploy.ok === false){
      flow.step='error';
      flow.errorMsg = 'GitOps \u5df2\u63d0\u4ea4\uff0c\u4f46 K8s \u5be6\u969b\u90e8\u7f72\u5931\u6557 / GitOps committed but K8s deploy failed\uff1a'+d.k8s_deploy.message;
      flow.retryStep='review';
    } else {
      flow.step='done';
      if(d.parsed) flow.spec = Object.assign({}, flow.spec, {
        app_name:d.parsed.app_name, image:d.parsed.image, pods:d.parsed.pods, port:d.parsed.port});
      loadStats && loadStats();
    }
  }catch(e){
    if(!flowAlive(flow)) return;
    flow.step='error'; flow.errorMsg='\u9023\u7dda\u932f\u8aa4 / network error: '+e; flow.retryStep='review';
  }
  persistFlowCard(id);
}

async function startDestructiveFlow(kind, args){
  const flow = newFlow(kind, '', Object.assign({}, args));
  if((kind==='scale'||kind==='update_image') && args.name){
    try{
      const r = await fetch('/api/deployments'); const d = await r.json();
      const dep = (d.deployments||[]).find(x=>x.name===args.name);
      if(dep){ flow.spec.current = (kind==='scale') ? dep.replicas : dep.image; }
    }catch(e){}
  }
  appendMsg('assistant', renderFlowCard(flow));
}

async function flowExecuteAction(id){
  const flow = chatFlows[id];
  if(!flowAlive(flow)) return;
  const a = flow.spec || {};
  let url, body;
  if(flow.kind==='scale'){ url='/api/scale'; body={name:a.name, replicas:a.replicas}; }
  else if(flow.kind==='update_image'){ url='/api/update'; body={name:a.name, image:a.image}; }
  else if(flow.kind==='rollback'){ url='/api/rollback'; body={app_name:a.name}; }
  else if(flow.kind==='delete'){ url='/api/delete'; body={name:a.name}; }
  else if(flow.kind==='healer_fix'){ url='/api/healer/fix'; body={pod_name:a.pod_name}; }
  else if(flow.kind==='healer_auto_fix'){ url='/api/healer/auto_fix'; body={}; }
  else return;
  flow.step='executing'; persistFlowCard(id);
  try{
    const r = await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const d = await r.json();
    if(!flowAlive(flow)) return;
    const ok = (d.success !== false) && !d.error;
    if(ok){
      flow.step='done';
      flow.resultMsg = d.message || (flow.kind==='healer_auto_fix' ? `\u5df2\u8655\u7406 ${d.fixed||0} \u500b Pod` : destructiveSummary(flow));
      loadStats && loadStats();
    } else {
      flow.step='error'; flow.errorMsg = d.error || d.message || '\u64cd\u4f5c\u5931\u6557 / failed'; flow.retryStep='spec';
    }
  }catch(e){
    if(!flowAlive(flow)) return;
    flow.step='error'; flow.errorMsg='\u9023\u7dda\u932f\u8aa4 / network error: '+e; flow.retryStep='spec';
  }
  persistFlowCard(id);
}

async function startDeployFlow(text){
  try{
    const r = await fetch('/api/deploy/parse',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({input:text})});
    const d = await r.json();
    if(d.error || d.rejected){
      appendMsg('assistant', '\u7121\u6cd5\u90e8\u7f72 / Cannot deploy\uff1a' + (d.error || d.reason || 'blocked'));
      return;
    }
    const raw = d.raw || d.parsed || {};
    const flow = newFlow('deploy', text, {
      app_name: raw.app_name, image: raw.image, pods: raw.pods, port: raw.port,
      memory: raw.memory, cpu: raw.cpu,
    });
    flow.review = d.review || null;
    flow.resource = d.resource_summary || null;
    appendMsg('assistant', renderFlowCard(flow));
  }catch(e){
    appendMsg('assistant', '\u89e3\u6790\u90e8\u7f72\u9700\u6c42\u6642\u9023\u7dda\u932f\u8aa4 / network error: ' + e);
  }
}

async function runReadAction(action){
  const map = {
    list_pods:['/api/pods', d=>{
      const ps=d.pods||[]; return ps.length ? '\u57f7\u884c\u4e2d\u7684 Pods / Running Pods:\n'+ps.map(p=>`- ${p.name} [${p.phase}] ${(p.containers&&p.containers[0]&&p.containers[0].image)||''}`).join('\n') : '\u76ee\u524d\u6c92\u6709 Pod / No pods.';
    }],
    list_deployments:['/api/deployments', d=>{
      const ds=d.deployments||[]; return ds.length ? 'Deployments:\n'+ds.map(x=>`- ${x.name}  ${x.ready}/${x.replicas} ready  ${x.image||''}`).join('\n') : '\u76ee\u524d\u6c92\u6709 Deployment / None.';
    }],
    gitops_log:['/api/gitops', d=>{
      const cs=d.commits||[]; return cs.length ? '\u90e8\u7f72\u7d00\u9304 / GitOps log:\n'+cs.map(c=>`- ${c.time}  ${c.app||''}  ${c.message}`).join('\n') : '\u6c92\u6709\u7d00\u9304 / No commits.';
    }],
    cluster_metrics:['/api/metrics', d=>{
      const m=d.metrics||{}; return `\u53e2\u96c6\u6307\u6a19 / Cluster metrics:\n- Prometheus: ${d.connected?'online':'offline'}\n- Pods: ${m.pod_count!=null?m.pod_count:'?'} (running ${m.running_pods!=null?m.running_pods:'?'})`;
    }],
    healer_scan:['/api/healer/scan', d=>{
      const is=d.issues||[]; return is.length ? `Healer \u6383\u5230 ${is.length} \u500b\u554f\u984c / issues:\n`+is.map(i=>`- ${i.pod_name||i.pod} : ${i.reason||i.status}`).join('\n') : '\u6c92\u6709\u7570\u5e38 Pod / No unhealthy pods.';
    }],
  };
  // \u9700\u8981\u53c3\u6578\u7684\u55ae\u4e00\u7269\u4ef6\u67e5\u8a62
  if(action==='describe_pod' || action==='pod_health' || action==='describe_deployment'){
    const name = (window.__lastIntentArgs && window.__lastIntentArgs.name) || '';
    if(!name){ appendMsg('assistant','\u8981\u67e5\u54ea\u4e00\u500b\uff1f\u8acb\u8b1b\u6e05\u695a\u540d\u7a31 / which one? give a name.'); return; }
    try{
      if(action==='describe_deployment'){
        const r = await fetch('/api/deployments/'+encodeURIComponent(name));
        const d = await r.json();
        if(!d.found){ appendMsg('assistant', d.message || ('\u627e\u4e0d\u5230 '+name)); return; }
        appendMsg('assistant', fmtDeployDetail(d.deployment));
      } else {
        const url = '/api/pods/'+encodeURIComponent(name) + (action==='pod_health'?'?diagnose=1':'');
        const r = await fetch(url);
        const d = await r.json();
        if(!d.found || !(d.pods||[]).length){
          let msg = d.message || ('\u627e\u4e0d\u5230\u7b26\u5408 '+name+' \u7684 pod');
          if(d.deployment) msg += '\n\n' + fmtDeployDetail(d.deployment);
          appendMsg('assistant', msg); return;
        }
        appendMsg('assistant', (action==='pod_health'?fmtPodHealth:fmtPodDetail)(d.pods, name));
      }
    }catch(e){ appendMsg('assistant', 'Error: '+e); }
    return;
  }
  const entry = map[action];
  if(!entry){ appendMsg('assistant', '\uff08\u672a\u652f\u63f4\u7684\u67e5\u8a62 / unsupported\uff09'); return; }
  try{
    const r = await fetch(entry[0]); const d = await r.json();
    appendMsg('assistant', entry[1](d));
  }catch(e){ appendMsg('assistant', 'Error: '+e); }
}

function _contLine(c){
  return `  \u00b7 ${c.name} [${c.state}${c.reason?'/'+c.reason:''}] ready=${c.ready?'yes':'no'} restarts=${c.restart_count}`
    + (c.message?`\n    ${String(c.message).slice(0,160)}`:'');
}
function fmtPodDetail(pods, name){
  return pods.map(p=>{
    const evs = (p.events||[]).slice(0,3).map(e=>`  event ${e.reason}: ${String(e.message).slice(0,120)}`).join('\n');
    return `Pod ${p.name}\n`
      + `phase: ${p.phase}   node: ${p.node||'-'}   ip: ${p.ip||'-'}   age: ${p.age}   restarts: ${p.restarts}\n`
      + `health: ${p.healthy?'\u2705 \u6b63\u5e38':'\u274c '+p.health_summary}\n`
      + `containers:\n${(p.containers||[]).map(_contLine).join('\n')}`
      + (evs?`\nrecent events:\n${evs}`:'');
  }).join('\n\n');
}
function fmtPodHealth(pods, name){
  const bad = pods.filter(p=>!p.healthy);
  const head = bad.length
    ? `\u274c ${name}\uff1a${bad.length}/${pods.length} \u500b pod \u6709\u554f\u984c`
    : `\u2705 ${name}\uff1a${pods.length} \u500b pod \u90fd\u6b63\u5e38\u904b\u884c`;
  const lines = pods.map(p=>{
    let s = `  - ${p.name}: ${p.healthy?'\u6b63\u5e38':p.health_summary}\uff08\u91cd\u555f ${p.restarts} \u6b21\uff09`;
    if(!p.healthy){
      const ev = (p.events||[])[0];
      if(ev) s += `\n    \u6700\u8fd1\u4e8b\u4ef6: ${ev.reason} \u2014 ${String(ev.message).slice(0,140)}`;
      if(p.diagnosis){
        const dg = p.diagnosis;
        s += `\n    \ud83d\udd0e \u6839\u56e0: ${dg.root_cause||'-'}`;
        if(dg.suggestion) s += `\n    \ud83d\udca1 \u5efa\u8b70: ${dg.suggestion}`;
      }
    }
    return s;
  }).join('\n');
  return head + '\n' + lines;
}
function fmtDeployDetail(d){
  const conds = (d.conditions||[]).map(c=>`  \u00b7 ${c.type}=${c.status}${c.reason?' ('+c.reason+')':''}`).join('\n');
  return `Deployment ${d.name}\n`
    + `health: ${d.healthy?'\u2705 ':'\u274c '}${d.health_summary}\n`
    + `replicas: ${d.ready}/${d.replicas} ready, ${d.available} available, ${d.updated} updated\n`
    + `image: ${d.image}   age: ${d.age}`
    + (conds?`\nconditions:\n${conds}`:'');
}

function startClarify(intent){
  const g = (intent.args && intent.args.guess) || intent.action;
  const label = {scale:'\u64f4\u7e2e\u526f\u672c',update_image:'\u66f4\u65b0\u6620\u50cf',rollback:'\u56de\u6efe',delete:'\u522a\u9664',
                 healer_fix:'\u4fee\u5fa9 Pod',healer_auto_fix:'\u81ea\u52d5\u4fee\u5fa9'}[g] || g;
  appendMsg('assistant', `\u6211\u4e0d\u592a\u78ba\u5b9a\u4f60\u662f\u8981\u300c${label}\u300d\u9084\u662f\u53ea\u662f\u5728\u554f\u554f\u984c\u3002\u5982\u679c\u8981\u57f7\u884c\uff0c\u8acb\u8b1b\u6e05\u695a\u4e00\u9ede\uff0c\u4f8b\u5982\uff1a\n`+
    `- scale <deployment> to <\u6578\u91cf>\n- delete <deployment>\n- rollback <deployment>\n`+
    `I'm not sure if you want to run "${g}" or just asking \u2014 please phrase it as an explicit command.`);
}

async function runQA(text){
  setTyping('Thinking');
  try{
    const hist = (currentChat()?.messages||[]).slice(-10).map(m=>({role:m.role==='assistant'?'assistant':'user',content:m.content}));
    const r = await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:text, history:hist})});
    const d = await r.json();
    appendMsg('assistant', d.reply||d.error||'No response', d.sources);
  }catch(e){ appendMsg('assistant','Connection error: '+e); }
}

async function sendChat(){
  const inp = document.getElementById('chat-input');
  const text = inp.value.trim();
  if(!text) return;
  inp.value = '';
  inp.style.height = '';
  if(!currentChatId) newChat();
  appendMsg('user', text);
  appendTyping('Understanding');

  let intent = matchClientRule(text);
  if(!intent){
    try{
      const r = await fetch('/api/intent',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:text})});
      intent = await r.json();
    }catch(e){ intent = {action:'qa', args:{}, source:'error'}; }
  }

  const action = intent.action || 'qa';
  try{
    if(action==='qa'){ await runQA(text); }
    else if(action==='clarify'){ startClarify(intent); }
    else if(READ_ACTIONS.includes(action)){ window.__lastIntentArgs = intent.args||{}; setTyping('Fetching'); await runReadAction(action); }
    else if(action==='deploy'){ setTyping('Parsing'); await startDeployFlow(text); }
    else if(DESTRUCTIVE_KINDS.includes(action)){ setTyping('Preparing'); await startDestructiveFlow(action, intent.args||{}); }
    else { await runQA(text); }
  }catch(e){
    appendMsg('assistant','\u8655\u7406\u6642\u767c\u751f\u932f\u8aa4 / error: '+e);
  }finally{
    removeTyping();
  }
}


window.addEventListener('DOMContentLoaded', function(){
  if(loggedIn){
    applyLang();
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
    USERS[username] = {"password_hash": hash_password(password), "created_at": datetime.now().isoformat(),
                       "violation_count": 0, "banned": False}
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "users.json"), "w", encoding="utf-8") as _uf:
        json.dump(USERS, _uf)
    _ensure_user_namespace(_user_namespace(username))
    session["username"] = username
    return redirect("/")

@app.route("/auth/login", methods=["POST"])
def login():
    username = request.form.get("username","").strip()
    password = request.form.get("password","")
    stored_user = USERS.get(username)
    stored_hash = stored_user.get("password_hash", "") if isinstance(stored_user, dict) else stored_user
    if verify_password(password, stored_hash):
        if isinstance(stored_user, dict) and stored_user.get("banned"):
            # 帳號已被封鎖（通常是 Gemini 判定累積達到門檻自動觸發，見 _ban_user）。
            # 不能只回 401——要講清楚原因，不然使用者會以為是打錯密碼。
            return render_template_string(
                HTML, logged_in=False, page='login',
                error="This account has been banned for repeated malicious behavior. / 此帳號因累積多次惡意行為已被封鎖。",
                k8s=K8S_ENABLED, username='')
        # 舊帳號可能是「純字串密碼」或「dict 但裡面存的是弱雜湊（沒加鹽的 sha256）」，
        # 登入成功那一刻密碼是明文可用的，順便升級成 pbkdf2，不用等使用者自己改密碼。
        if not isinstance(stored_user, dict) or not stored_hash.startswith("pbkdf2_sha256$"):
            created = stored_user.get("created_at") if isinstance(stored_user, dict) else None
            USERS[username] = {"password_hash": hash_password(password),
                               "created_at": created or datetime.now().isoformat(),
                               "violation_count": 0, "banned": False}
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "users.json"), "w", encoding="utf-8") as _uf:
                json.dump(USERS, _uf)
        _ensure_user_namespace(_user_namespace(username))
        session["username"] = username
        return redirect("/")
    return render_template_string(HTML, logged_in=False, page='login', error="Invalid username or password", k8s=K8S_ENABLED, username='')

@app.route("/auth/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect("/")


@app.before_request
def _enforce_ban():
    """集中檢查封鎖狀態，不用在 ~25 個路由裡各自加一次判斷。即使被封鎖的當下使用者
    還有一個活著的 session（登入時還沒被封鎖），這裡會在下一次任何請求時擋下並
    清掉 session——不需要額外做 session 撤銷機制，符合「不能靜默失敗」：不是裸的
    401/403，要講清楚原因。"""
    username = session.get("username")
    if username and USERS.get(username, {}).get("banned"):
        session.clear()
        if request.path.startswith("/api/"):
            return jsonify({"error": "This account has been banned for repeated malicious behavior. / 此帳號因累積多次惡意行為已被封鎖。"}), 403
        return render_template_string(
            HTML, logged_in=False, page='login',
            error="This account has been banned for repeated malicious behavior. / 此帳號因累積多次惡意行為已被封鎖。",
            k8s=K8S_ENABLED, username='')
    return None


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


# ── Chat 意圖辨識：規則層（前端規則的 Python 對應，當安全網）──────────
# 每條 (action, compiled_regex, group->arg 對應)。第一條命中即回傳。
_INTENT_RULES = [
    ("list_pods", re.compile(r"^(list|show|查看|顯示|列出)\s*(all\s*|所有|全部)?\s*(pods?|容器)", re.I), {}),
    ("list_deployments", re.compile(r"^(list|show|查看|顯示|列出)\s*(all\s*|所有|全部)?\s*(deploy(ment)?s?|部署)", re.I), {}),
    ("gitops_log", re.compile(r"^(gitops|deploy history|git ?log)|部署(紀錄|歷史|記錄)", re.I), {}),
    ("cluster_metrics", re.compile(r"^(metrics|cluster (status|health)|叢集(狀態|健康)|指標)", re.I), {}),
    ("healer_auto_fix", re.compile(r"^(auto ?fix|fix all|自動修復|全部修復)", re.I), {}),
    ("healer_fix", re.compile(r"^(fix|修復)\s+(\S+)", re.I), {2: "pod_name"}),
    ("healer_scan", re.compile(r"^(healer|scan)\b|掃描.*(pod|壞|異常)", re.I), {}),
    # 單一 pod 健康：名字 + 健康疑問句
    ("pod_health", re.compile(r"([a-zA-Z0-9][\w.-]*)\s*(?:這個)?\s*(?:pod|deployment|部署)?\s*(?:有沒有壞|壞了沒|壞掉了嗎|壞了嗎|正常嗎|健康嗎|掛了嗎|掛掉了嗎|還活著嗎|沒事吧|有問題嗎|ok\s*嗎)", re.I), {1: "name"}),
    ("pod_health", re.compile(r"^is\s+([a-zA-Z0-9][\w.-]*)\s+(?:ok|okay|healthy|broken|down|up|crashing|running|alive)", re.I), {1: "name"}),
    # 單一 deployment 狀態
    ("describe_deployment", re.compile(r"(?:deployment|部署)\s+([a-zA-Z0-9][\w.-]*)\s*(?:的?\s*(?:狀態|細節|詳情|status)|status)?", re.I), {1: "name"}),
    ("describe_deployment", re.compile(r"([a-zA-Z0-9][\w.-]*)\s*(?:這個)?\s*(?:deployment|部署)\s*(?:的?\s*(?:狀態|細節|詳情))", re.I), {1: "name"}),
    # 單一 pod 細節
    ("describe_pod", re.compile(r"(?:查看|detail(?:s)?\s*(?:of)?|describe|檢視)\s*(?:pod\s+)?([a-zA-Z0-9][\w.-]*)", re.I), {1: "name"}),
    ("describe_pod", re.compile(r"([a-zA-Z0-9][\w.-]*)\s*(?:這個)?\s*pod\s*(?:的?\s*(?:細節|狀態|詳情|資訊))", re.I), {1: "name"}),
    ("describe_pod", re.compile(r"([a-zA-Z0-9][\w.-]*)\s*(?:的\s*(?:細節|詳情|詳細狀態))", re.I), {1: "name"}),
    ("delete", re.compile(r"^(delete|remove|刪除|del)\s+(\S+)", re.I), {2: "name"}),
    ("scale", re.compile(r"scale\s+(\S+)\s+to\s+(\d+)", re.I), {1: "name", 2: "replicas"}),
    ("scale", re.compile(r"把?\s*(\S+?)\s*(?:擴|縮|調).*?(\d+)", re.I), {1: "name", 2: "replicas"}),
    ("update_image", re.compile(r"update\s+(\S+)\s+to\s+(\S+)", re.I), {1: "name", 2: "image"}),
    ("update_image", re.compile(r"把?\s*(\S+?)\s*的?\s*(?:image|映像|鏡像)\s*(?:換成|改成|改為|to)?\s*(\S+)", re.I), {1: "name", 2: "image"}),
    ("rollback", re.compile(r"rollback\s+(\S+)", re.I), {1: "name"}),
    ("rollback", re.compile(r"(\S+)\s*(?:回滾|回復|還原)", re.I), {1: "name"}),
    # deploy：前端有更完整的複合 regex，這裡只當 server 端安全網（args 交給 /api/deploy/parse 解析）
    ("deploy", re.compile(r"^(deploy|start|launch|run|spin\s+up|部署|佈署|部屬)", re.I), {}),
    ("deploy", re.compile(r"(?:幫我|請).{0,6}(?:deploy|部署|佈署|部屬|跑|起).{0,8}(?:\d+\s*(?:個|pods?|副本)|nginx|redis|postgres|mysql|node|python|image|映像|鏡像)", re.I), {}),
]
_INTENT_INT_ARGS = {"replicas", "pods", "port"}


def _rule_intent(message: str):
    """規則比對一句訊息 -> {action, args} 或 None。前端 matchClientRule 的 server 端鏡像。"""
    msg = (message or "").strip()
    if not msg:
        return None
    for action, rx, groupmap in _INTENT_RULES:
        m = rx.search(msg)
        if not m:
            continue
        args = {}
        for gi, key in groupmap.items():
            try:
                val = m.group(gi)
            except (IndexError, re.error):
                val = None
            if val is None:
                continue
            val = val.strip().strip(".,，。")
            if key in _INTENT_INT_ARGS:
                try:
                    val = int(val)
                except ValueError:
                    continue
            args[key] = val
        # 有 group 需求卻沒抓到 -> 視為未命中，交給模型
        if groupmap and len(args) < len(groupmap):
            continue
        return {"action": action, "args": args}
    return None


_CLUSTER_Q_HINTS = (
    "壞", "掛", "當機", "crash", "crashloop", "健康", "正常", "沒事", "ok",
    "狀態", "status", "幾個", "多少", "哪個", "哪些", "which", "restart", "重啟",
    "running", "ready", "unavailable", "pod", "deployment", "部署", "副本", "replica",
    "image", "映像", "節點", "node", "叢集", "cluster",
)


def _cluster_snapshot_for(message: str, namespace: str = None) -> str:
    """問題牽涉即時叢集狀態時，回傳一段精簡快照文字（給接地問答）；否則回空字串。
    只查傳入的 namespace（預設呼叫端使用者自己的 namespace），不會洩漏其他帳號的
    Deployment 資訊給接地問答用。"""
    namespace = namespace or NS
    if not K8S_ENABLED:
        return ""
    low = (message or "").lower()
    deps = k8s_get_deployments(namespace)
    dep_names = [d["name"] for d in deps]
    named = [n for n in dep_names if n and n.lower() in low]
    if not named and not any(h in low for h in _CLUSTER_Q_HINTS):
        return ""
    lines = []
    lines.append("Deployments（your namespace）：")
    for d in deps:
        flag = "OK" if d["ready"] == d["replicas"] and d["replicas"] else "異常"
        lines.append(f"  - {d['name']}: {d['ready']}/{d['replicas']} ready, image={d['image']} [{flag}]")
    # 不健康的 pod（掃每個 deployment 的 pod）
    unhealthy = []
    for name in dep_names:
        for pd in k8s_describe_pod(name, namespace):
            if not pd["healthy"]:
                unhealthy.append(f"  - {pd['name']}: {pd['health_summary']}, 重啟 {pd['restarts']} 次")
    if unhealthy:
        lines.append("不健康的 Pod：")
        lines.extend(unhealthy[:8])
    else:
        lines.append("所有 Pod 目前健康。")
    # 使用者點名的物件，補詳情
    for n in named[:2]:
        dd = k8s_describe_deployment(n, namespace)
        if dd:
            lines.append(f"\n{n} 詳情：{dd['health_summary']}；conditions=" +
                         "; ".join(f"{c['type']}={c['status']}({c['reason']})" for c in dd["conditions"][:3]))
        for pd in k8s_describe_pod(n, namespace)[:3]:
            evs = "；".join(f"{e['reason']}: {e['message'][:80]}" for e in pd["events"][:2])
            lines.append(f"  Pod {pd['name']}: phase={pd['phase']}, "
                         + ", ".join(f"{c['name']}[{c['state']}{('/'+c['reason']) if c['reason'] else ''}]" for c in pd["containers"])
                         + (f"；事件: {evs}" if evs else ""))
    text = "\n".join(lines)
    return text[:2000]


_POSITIVE_STATUS_WORDS = (
    "健康", "正常", "沒問題", "沒有問題", "沒有異常", "運作正常", "運行正常",
    "healthy", "is fine", "is ok", "is okay", "running fine", "running well",
    "no issues", "is running", "運行良好",
)
_IDENTIFIER_CANDIDATE_RE = re.compile(r"\b[a-zA-Z][a-zA-Z0-9]*(?:-[a-zA-Z0-9]+)+\b")


def _verify_grounded_reply(message: str, reply: str, namespace: str = None) -> str:
    """
    2026-09-14：輸出端事實核對（跟輸入端擋 prompt injection 是完全獨立的第二道防線，
    見 docs/security_review.md 9 節「方法 1」）。不管模型是被注入攻擊說服、還是單純
    自己幻覈，只要回覆對一個「叢集裡實際不存在的服務/Deployment」講出肯定的健康狀態，
    這裡都用真實 K8s 清單核對、攔截並改成更正訊息——不相信生成過程，只驗證結果，
    跟 guardian 對 LLM 產生的 YAML 一定要驗證過才能部署是同一種哲學。只查傳入的
    namespace（呼叫端使用者自己的），避免拿別的帳號的清單來核對。

    已知限制：只抓「連字號命名」的候選字（例如 ghost-service-xyz999），單字不含連字號
    的假名稱（例如 ghostapp）抓不到——這是精確度／覆蓋率的取捨，寧可少擋不要把一般
    英文單字誤判成服務名稱。
    """
    namespace = namespace or NS
    if not K8S_ENABLED:
        return reply
    try:
        real_names = {d["name"].lower() for d in k8s_get_deployments(namespace)}
    except Exception:
        return reply
    candidates = set(m.group(0) for m in _IDENTIFIER_CANDIDATE_RE.finditer(message))
    candidates |= set(m.group(0) for m in _IDENTIFIER_CANDIDATE_RE.finditer(reply))
    ghost_names = [c for c in candidates if c.lower() not in real_names and len(c) >= 4]
    if not ghost_names:
        return reply
    low_reply = reply.lower()
    for name in ghost_names:
        # 模型講到名稱時常常會把連字號念成空格（實測抓到過："ghost-service-xyz999"
        # 被模型講成 "Ghost service xyz999"），單純字串比對會漏掉這種情況，
        # 用連字號可以是空白/連字號/無分隔的寬鬆比對。
        pattern = re.escape(name.lower()).replace(r"\-", r"[-\s]*")
        m = re.search(pattern, low_reply)
        if not m:
            continue
        idx = m.start()
        window = low_reply[max(0, idx - 60): idx + 60]
        if any(w in window for w in _POSITIVE_STATUS_WORDS):
            return (
                f"更正：剛才的回覆可能不準確——`{name}` 目前叢集中並不存在，不應該被說成健康或正常。"
                f"請確認名稱是否正確，或到 Pods / Deployments 頁面查看實際清單。 / "
                f"Correction: the previous answer may be inaccurate — `{name}` does not exist in the "
                f"current cluster and should not have been described as healthy. Please double-check "
                f"the name, or check the Pods/Deployments page for the real list."
            )
    return reply


def _looks_like_system_help(message: str) -> bool:
    low = (message or '').lower()
    product_terms = (
        'zerotouch', 'this system', 'this app', 'use this', 'how to use', 'how to deploy',
        'healer', 'gitops', 'metrics',
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
            "直接在 Chat 打字描述你要部署的東西就可以了。建議格式：\n\n"
            "`deploy <數量> <image> pods for <app-name>, port <port>`\n\n"
            "範例：\n"
            "- `deploy 3 nginx:latest pods for web-frontend, port 80`\n"
            "- `spin up 4 node:20-alpine pods for api-gateway, port 3000`\n"
            "- `幫我部署 5 個 redis pods 給 cache-service port 6379`\n\n"
            "送出後系統會解析 replicas/image/app/port，跑 Guardian、Agent review、dry-run，K8s 連線正常時才建立 Deployment。"
        )

    return (
        "這套 ZeroTouch K8s 主要有幾個區塊：\n\n"
        "- `Chat`：問問題、部署服務、查 Pod 狀態/健康、scale/update/rollback，一個地方全部搞定。\n"
        "- `Pods`：查看 Pod 狀態、IP、node、restart。\n"
        "- `Deployments`：查看 app image、replicas、ready 數與刪除部署。\n"
        "- `Healer`：掃描 CrashLoopBackOff/OOMKilled/ImagePullBackOff 等異常 Pod，並刪除重建。\n"
        "- `GitOps Log`：看部署歷史與 rollback 線索。\n"
        "- `Metrics`：看 Prometheus 與叢集基本指標。\n\n"
        "如果你要部署，直接輸入例如：`deploy 3 nginx:latest pods for web-frontend, port 80`。"
    )


def _fallback_chat_reply(message: str, history: list = None, namespace: str = None) -> tuple:
    """回傳 (reply, sources)。只有走到 RAG 知識庫的分支才會有非空的 sources。"""
    namespace = namespace or NS
    text = message.strip()
    low = text.lower()
    greetings = ("hi", "hello", "hey", "嗨", "你好", "哈囉", "早安", "午安", "晚安")
    if any(g in low for g in greetings):
        return "嗨，我是 ZeroTouch K8s Assistant。你可以跟我閒聊，也可以問 Kubernetes、查 Pods/Deployments、排查錯誤，或用自然語言部署服務。", []
    if "pod" in low or "pods" in low or "容器" in low:
        pods = k8s_get_pods(namespace=namespace)
        if pods:
            lines = [f"- {p['name']} [{p['phase']}] app={p.get('app') or '-'} restarts={p.get('restarts', 0)}" for p in pods[:12]]
            return "目前 Pods：\n" + "\n".join(lines), []
    if "deployment" in low or "deployments" in low or "部署" in low:
        deps = k8s_get_deployments(namespace)
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
    if "username" not in session:
        return jsonify({"error": "Not authenticated"}), 401
    ready, loading = _model_status()
    docker_running, docker_message = _check_docker()
    k8s_live = _check_k8s_live()
    return jsonify({
        "model_ready": ready, "model_loading": loading, "claude_api": claude_available(),
        "k8s": k8s_live,                       # 即時探測，不是啟動時的舊值
        "docker_running": docker_running,
        "docker_message": docker_message,
    })

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

    # Gemini 惡意誘導/控制行為偵測（第二層輔助判斷，本地 core/model_server.py 的
    # _looks_like_prompt_injection() 是第一層確定性防線）。只在真的會送進 LLM 的訊息上
    # 跑，不對上面已經走 system_help 短路的訊息跑，省額度。累犯到門檻直接封鎖帳號＋
    # 刪除該帳號的 namespace（見 _record_violation/_ban_user）。
    try:
        from core.gemini_client import classify_malicious_intent
        verdict = classify_malicious_intent(message)
    except Exception:
        verdict = {"malicious": False, "reason": "gemini unavailable"}
    if verdict.get("malicious"):
        banned_now = _record_violation(session["username"])
        if banned_now:
            session.clear()
            return jsonify({
                "reply": "偵測到多次惡意誘導/操縱行為，此帳號已被封鎖並移除所有部署的資源。 / "
                         "Repeated malicious/manipulative behavior detected — this account has been "
                         "banned and all its deployed resources have been removed.",
                "banned": True,
            }), 403
        return jsonify({
            "reply": f"這則訊息被判定為嘗試誘導/操縱系統行為（{verdict.get('reason','')}），已記一次違規。"
                     f"累積達到門檻將自動封鎖帳號。 / "
                     f"This message was flagged as an attempt to manipulate the system "
                     f"({verdict.get('reason','')}). A violation has been recorded; repeated "
                     f"violations will result in an automatic ban.",
        })

    user_ns = _user_namespace(session["username"])
    # 問題若牽涉即時叢集狀態，先撈一份精簡快照當背景資料塞給模型（接地問答）。
    grounded = message
    snap = _cluster_snapshot_for(message, user_ns)
    if snap:
        grounded = f"[現況]\n{snap}\n\n[問題]\n{message}"
    reply, sources = chat_llama(grounded, history)
    if reply.startswith("[Local Model unavailable]"):
        reply, sources = _fallback_chat_reply(message, history, user_ns)
    else:
        reply = _verify_grounded_reply(message, reply, user_ns)
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


@app.route("/api/intent", methods=["POST"])
def api_intent():
    """把一句聊天訊息分類成動作。前端 matchClientRule 沒命中才會打這裡。
    回 {action, args, confidence, source}。source: rule | llm | llm_unavailable。
    action 可能是 clarify（模型不確定的破壞性操作）或 qa（純問答，交回 /api/chat）。"""
    if "username" not in session:
        return jsonify({"error": "Not authenticated"}), 401
    msg = (request.get_json() or {}).get("message", "").strip()
    if not msg:
        return jsonify({"action": "qa", "args": {}, "confidence": 0.0, "source": "empty"})
    hit = _rule_intent(msg)
    if hit:
        return jsonify({**hit, "confidence": 1.0, "source": "rule"})
    try:
        result = classify_intent(msg)
    except Exception:
        result = {"action": "qa", "args": {}, "confidence": 0.0, "source": "llm_unavailable"}
    return jsonify(result)


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


_NODE_CAPACITY_HINT_RE = re.compile(
    r"(node|節點).{0,15}(cpu|core|記憶體|memory|gi|mi)", re.IGNORECASE
)


def _mentions_node_capacity(text: str) -> bool:
    """使用者是否真的講了「每個 node 有多少 cpu/記憶體」這種話。"""
    return bool(_NODE_CAPACITY_HINT_RE.search(text or ""))


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
        # 只有使用者自己真的講了「每個 node 多少 cpu/記憶體」，才信任模型自己算出來的
        # node_count/total_cpu 等欄位——實測發現模型會在使用者沒提 node 容量的情況下
        # 自己冒出這些欄位（例如問「我想要高可用的服務」），而且算出來的數字前後不一致
        # （total_cpu 跟 cpu×pods 對不上）。沒有明確依據時一律用 cost_agent 的固定公式算，
        # 不要相信模型自己報的數字。
        real_capacity = k8s_get_node_capacity()  # 真的查叢集節點，不是 core.config 那個固定假設值
        if parsed.get("node_count") is not None and _mentions_node_capacity(user_input):
            review["node_estimate"] = {"node_count": parsed["node_count"], "source": "llm"}
        else:
            from agents.cost_agent import estimate_node_count
            kwargs = {"node_capacity": real_capacity} if real_capacity else {}
            result = estimate_node_count(parsed.get("cpu"), parsed.get("memory"), parsed.get("pods", 1), **kwargs)
            result["source"] = "calculated"
            review["node_estimate"] = result

        # 「需要幾個節點」跟「單一個 pod 大到連一個節點都放不下」是兩件不同的事：前者只是
        # 貴、後者是不管幾個節點都排不進去，Pod 會卡在 Pending 永遠不會變 Running，但
        # k8s_deploy() 建立 Deployment 物件本身還是會回報「已部署」成功，使用者看不出來
        # （這是實測抓到的：部署 64Gi/32 CPU 的 pod，API 回 ok=true，但 kubectl describe
        #  顯示 FailedScheduling: Insufficient cpu, Insufficient memory）。這裡直接擋掉，
        # 不是等它卡住之後才靠使用者自己問 pod_health 才發現。
        if real_capacity:
            from agents.cost_agent import _parse_cpu_millicores, _parse_memory_bytes
            node_cpu_mc = _parse_cpu_millicores(real_capacity.get("cpu")) or 0
            node_mem_b = _parse_memory_bytes(real_capacity.get("memory")) or 0
            pod_cpu_mc = _parse_cpu_millicores(parsed.get("cpu")) or 0
            pod_mem_b = _parse_memory_bytes(parsed.get("memory")) or 0
            over_cpu = node_cpu_mc and pod_cpu_mc > node_cpu_mc
            over_mem = node_mem_b and pod_mem_b > node_mem_b
            if over_cpu or over_mem:
                review["decision"] = "block"
                review["reason"] = "單一 Pod 的資源需求超出叢集最大節點的容量，無論建立幾個副本都不可能排程成功"
                review.setdefault("blockers", []).append(
                    f"資源需求超出叢集容量：這個 Pod 要求 "
                    f"{f'{pod_cpu_mc}m CPU' if over_cpu else ''}{'、' if over_cpu and over_mem else ''}"
                    f"{f'{pod_mem_b // (1024**2)}Mi 記憶體' if over_mem else ''}，"
                    f"但叢集裡最大的節點只有 {node_cpu_mc}m CPU / {node_mem_b // (1024**2)}Mi 記憶體可用。"
                    f"這不是「需要更多節點」的問題——單一 Pod 一定要能塞進某一個節點才會被排程，"
                    f"不管有幾個節點都一樣排不進去，部署下去 Pod 會卡在 Pending 永遠不會變 Running。"
                    f"請降低這次部署要求的 memory/cpu。 / "
                    f"Resource request exceeds this cluster's largest node — no number of nodes helps, "
                    f"since a single Pod must fit on ONE node. It would stay stuck Pending forever. "
                    f"Please lower the requested memory/cpu."
                )
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


def _synth_input_from_spec(spec: dict) -> str:
    """從結構化規格合成一句自然語言（override 但沒帶原始文字時用）。"""
    spec = spec or {}
    return (f"deploy {spec.get('pods', 1)} {spec.get('image', 'nginx:latest')} pods "
            f"for {spec.get('app_name', 'auto-app')}, port {spec.get('port', 80)}")


def _resource_summary(parsed: dict, review: dict) -> dict:
    """把 review 深層巢狀攤平成前端好用的資源摘要，給 Chat 的「檢視資源」步驟顯示。
    每個 pod 的 cpu/mem 若使用者沒指定就標「未指定」，不編造精度。"""
    parsed = parsed or {}
    review = review or {}
    cost = (((review.get("agents") or {}).get("agents") or {}).get("cost") or {})
    est = cost.get("cost_estimate") or {}
    ne = review.get("node_estimate") or {}
    node_bound = None
    if ne.get("cpu_bound") and ne.get("memory_bound"):
        node_bound = "cpu" if ne["cpu_bound"] >= ne["memory_bound"] else "memory"
    elif ne.get("cpu_bound"):
        node_bound = "cpu"
    elif ne.get("memory_bound"):
        node_bound = "memory"
    return {
        "replicas": parsed.get("pods"),
        "per_pod_cpu": parsed.get("cpu") or "未指定 / unset",
        "per_pod_mem": parsed.get("memory") or "未指定 / unset",
        "total_cpu_cores": est.get("cpu_cores"),
        "total_mem_gib": est.get("memory_gib"),
        "node_count": ne.get("node_count"),
        "node_source": ne.get("source"),
        "node_bound": node_bound,
        "monthly_usd": est.get("estimated_usd"),
        "cost_note": est.get("note"),
        "decision": review.get("decision"),
    }


@app.route("/api/deploy/conflicts")
def api_deploy_conflicts():
    """第一步（規格確認）就先問使用者：名稱已存在／port 被佔用，是否仍要繼續。
    比 /api/deploy/parse 快很多（不跑 guardian/agents/dry-run），適合在使用者
    按「下一步」的當下就先問清楚，而不是等審查跑完才在一堆警告裡看到。"""
    if "username" not in session:
        return jsonify({"error": "Not authenticated"}), 401
    user_ns = _user_namespace(session["username"])
    app_name = (request.args.get("app_name") or "").strip()
    port_raw = (request.args.get("port") or "").strip()
    name_exists = bool(app_name) and app_name in {d["name"] for d in k8s_get_deployments(user_ns)}
    port_conflict = None
    if port_raw:
        try:
            port = int(port_raw)
            for s in k8s_get_services(all_namespaces=True):
                if s["port"] == port and s["app"] != app_name:
                    port_conflict = s["app"] or s["name"]
                    break
        except ValueError:
            pass
    return jsonify({"name_exists": name_exists, "port_conflict": port_conflict})


@app.route("/api/deploy/parse", methods=["POST"])
def api_deploy_parse():
    if "username" not in session:
        return jsonify({"error": "Not authenticated"}), 401
    data = request.get_json() or {}
    user_input = data.get("input", "").strip()
    parsed_override = data.get("parsed")
    if not user_input and parsed_override:
        user_input = _synth_input_from_spec(parsed_override)
    if not user_input or len(user_input) < 3:
        return jsonify({"error": "Input too short"}), 400
    parsed, enriched, review, error = _prepare_deploy(user_input, parsed_override)
    if error:
        payload, status = error
        return jsonify(payload), status
    return jsonify({
        "parsed": enriched, "raw": parsed, "review": review, "k8s": K8S_ENABLED,
        "resource_summary": _resource_summary(parsed, review),
    })


@app.route("/api/deploy", methods=["POST"])
def api_deploy():
    if "username" not in session:
        return jsonify({"error": "Not authenticated"}), 401
    user_ns = _user_namespace(session["username"])
    _ensure_user_namespace(user_ns)
    data = request.get_json() or {}
    user_input = data.get("input", "").strip()
    parsed_override = data.get("parsed")
    if not user_input and parsed_override:
        user_input = _synth_input_from_spec(parsed_override)
    if not user_input or len(user_input) < 3:
        return jsonify({"error": "Input too short"}), 400

    parsed, enriched, review, error = _prepare_deploy(user_input, parsed_override)
    if error:
        payload, status = error
        return jsonify(payload), status

    gitops_result = None
    try:
        from gitops.manifest_writer import write_manifest
        gitops_result = write_manifest(parsed, repo_path=ROOT, namespace=user_ns, dry_run=False, commit=True)
    except Exception as e:
        gitops_result = {"ok": False, "message": str(e), "files": [], "commit_sha": None}
        review.setdefault("warnings", []).append(f"GitOps manifest write failed: {e}")

    threading.Thread(target=save_gold_sample, args=(user_input, parsed), daemon=True).start()

    # 同步呼叫（不是背景執行緒）：讓回應反映 K8s 是不是真的部署成功，而不是「沒報錯就當作成功」。
    # k8s_deploy() 內部四個 API 呼叫都已加上 _request_timeout=(5,10) 且 retries=0，不會無限期卡住這個 request。
    k8s_deploy_result = None
    if K8S_ENABLED:
        ok, message = k8s_deploy(parsed["app_name"], parsed["image"], parsed["pods"], parsed.get("port", 80), parsed.get("memory"), parsed.get("cpu"), namespace=user_ns)
        k8s_deploy_result = {"ok": ok, "message": message}
        if not ok:
            review.setdefault("warnings", []).append(f"K8s 部署失敗：{message}")

    return jsonify({"parsed": enriched, "k8s": K8S_ENABLED, "review": review, "gitops": gitops_result,
                    "k8s_deploy": k8s_deploy_result, "resource_summary": _resource_summary(parsed, review)})

@app.route("/api/pods")
def api_pods():
    if "username" not in session:
        return jsonify({"error": "Not authenticated"}), 401
    return jsonify({"pods": k8s_get_pods(namespace=_user_namespace(session["username"]))})

@app.route("/api/deployments")
def api_deployments():
    if "username" not in session:
        return jsonify({"error": "Not authenticated"}), 401
    return jsonify({"deployments": k8s_get_deployments(_user_namespace(session["username"]))})


@app.route("/api/pods/<name>/history")
def api_pod_history(name):
    """給 Healer 視覺化的趨勢圖用：這個 Pod 過去累積的輕量快照（重啟次數/健康狀態
    隨時間變化）。只回傳使用者自己 namespace 的資料。"""
    if "username" not in session:
        return jsonify({"error": "Not authenticated"}), 401
    user_ns = _user_namespace(session["username"])
    return jsonify({"history": _get_pod_history(user_ns, name)})


@app.route("/api/pods/<name>")
def api_pod_detail(name):
    """單一 pod（或某 app 的一組 pod）的詳細狀態 + 健康判定。
    ?diagnose=1 且該 pod 不健康時，附上 healer 的 LLM 根因診斷。"""
    if "username" not in session:
        return jsonify({"error": "Not authenticated"}), 401
    user_ns = _user_namespace(session["username"])
    pods = k8s_describe_pod(name, user_ns)
    if not pods:
        dd = k8s_describe_deployment(name, user_ns)
        if dd:
            return jsonify({"pods": [], "found": False, "deployment": dd,
                            "message": (f"'{name}' 是一個 Deployment，但目前沒有任何 Pod 在跑"
                                        f"（{dd['ready']}/{dd['replicas']} ready）。可能全部啟動失敗。")}), 200
        return jsonify({"pods": [], "found": False,
                        "message": f"找不到符合 '{name}' 的 pod 或 deployment"}), 404
    for pd in pods:
        pd["real_usage"] = _pod_real_usage(user_ns, pd["name"])
    if request.args.get("diagnose") == "1":
        for pd in pods:
            if pd["healthy"]:
                continue
            bad = next((c for c in pd["containers"]
                        if c["reason"] or c["last_reason"] or c["restart_count"] >= 5), None)
            ctx = {
                "pod_name": pd["name"], "container": (bad or {}).get("name", ""),
                "reason": (bad or {}).get("reason") or (bad or {}).get("last_reason") or pd["phase"],
                "restart_count": pd["restarts"],
                "logs": (bad or {}).get("message", ""),
                "events": [{"reason": e["reason"], "message": e["message"]} for e in pd["events"]],
            }
            try:
                from llama_client import diagnose_with_llm
                pd["diagnosis"] = diagnose_with_llm(ctx)
            except Exception as e:
                pd["diagnosis"] = None
    return jsonify({"pods": pods, "found": True})


@app.route("/api/deployments/<name>")
def api_deployment_detail(name):
    if "username" not in session:
        return jsonify({"error": "Not authenticated"}), 401
    d = k8s_describe_deployment(name, _user_namespace(session["username"]))
    if not d:
        return jsonify({"found": False, "message": f"找不到 deployment '{name}'"}), 404
    return jsonify({"deployment": d, "found": True})


@app.route("/api/delete", methods=["POST"])
def api_delete():
    if "username" not in session:
        return jsonify({"error": "Not authenticated"}), 401
    name = (request.get_json() or {}).get("name","").strip()
    if not name:
        return jsonify({"error": "Name required"}), 400
    ok, msg = k8s_delete_deployment(name, _user_namespace(session["username"]))
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
    user_ns = _user_namespace(session["username"])
    blocked, risk_msg = _check_scale_risk(name, replicas, user_ns)
    if blocked:
        return jsonify({"success": False, "error": risk_msg, "blocked_reason": "resource_exceeded"}), 409
    try:
        api = k8s_client.AppsV1Api()
        body = {"spec": {"replicas": replicas}}
        api.patch_namespaced_deployment_scale(name=name, namespace=user_ns, body=body)
        return jsonify({"success": True, "message": f"已調整 {name} replicas={replicas}"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/update", methods=["POST"])
def api_update():
    if "username" not in session:
        return jsonify({"error": "Not authenticated"}), 401
    if not K8S_ENABLED:
        return jsonify({"success": False, "error": "K8s 未連線"}), 503
    user_ns = _user_namespace(session["username"])
    data = request.get_json() or {}
    name = data.get("name", "").strip()
    image = data.get("image", "").strip()
    if not name or not image:
        return jsonify({"success": False, "error": "name and image are required"}), 400
    try:
        api = k8s_client.AppsV1Api()
        dep = api.read_namespaced_deployment(name, user_ns)
        if not dep.spec.template.spec.containers:
            return jsonify({"success": False, "error": "deployment has no containers"}), 400
        old_image = dep.spec.template.spec.containers[0].image
        annotations = dep.spec.template.metadata.annotations or {}
        annotations["zerotouch.k8s/previous-image"] = old_image
        annotations["zerotouch.k8s/updated-at"] = datetime.utcnow().isoformat()
        dep.spec.template.metadata.annotations = annotations
        dep.spec.template.spec.containers[0].image = image
        api.patch_namespaced_deployment(name, user_ns, dep)
        return jsonify({"success": True, "message": f"已更新 {name} image={image}", "previous_image": old_image})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/rollback", methods=["POST"])
def api_rollback():
    if "username" not in session:
        return jsonify({"error": "Not authenticated"}), 401
    user_ns = _user_namespace(session["username"])
    data = request.get_json() or {}
    app_name = (data.get("app_name") or data.get("name") or "").strip()
    if not app_name:
        return jsonify({"success": False, "error": "app_name required"}), 400
    try:
        from gitops.rollback import rollback
        result = rollback(app_name, namespace=user_ns, repo_path=ROOT, strategy="auto", dry_run=False)
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
        dep = api.read_namespaced_deployment(app_name, user_ns)
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
        api.patch_namespaced_deployment(app_name, user_ns, dep)
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
    if "username" not in session:
        return jsonify({"error": "Not authenticated"}), 401
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
    if "username" not in session:
        return jsonify({"error": "Not authenticated"}), 401
    import subprocess
    data = request.get_json() or {}
    flags = data.get("flags", "--skip-output")
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "enrich_dataset.py")
    if not os.path.exists(script):
        return jsonify({"success": False, "error": "enrich_dataset.py not found", "log": ""})
    # enrich_dataset.py 用了 `dict | None` union 語法，需要 Python 3.10+；
    # sys.executable 這台是 3.9，改用 PATH 上的 python3（Anaconda，3.11）。
    py = shutil.which("python3") or shutil.which("python") or sys.executable
    cmd = [py, script] + (flags.split() if flags else [])
    # 子行程的 print() 在 Windows 預設用 cp950 寫 stdout，父行程用 utf-8 讀就變亂碼。
    # 逼子行程也用 utf-8。
    child_env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    try:
        result = subprocess.run(cmd, cwd=ROOT, capture_output=True, env=child_env,
                                encoding="utf-8", errors="replace", timeout=600)
        log = (result.stdout or "") + ("\nSTDERR:\n" + result.stderr if result.stderr else "")
        return jsonify({"success": result.returncode == 0, "log": log})
    except subprocess.TimeoutExpired:
        return jsonify({"success": False, "log": "Timeout after 600s", "error": "timeout"})
    except Exception as e:
        return jsonify({"success": False, "log": str(e), "error": str(e)})

@app.route("/api/gitops")
def api_gitops():
    if "username" not in session: return jsonify({"error": "Not authenticated"}), 401
    user_ns = _user_namespace(session["username"])
    import subprocess
    try:
        # 只看自己 namespace 底下的歷史（manifests/<namespace>/），不要把所有帳號的
        # GitOps 紀錄混在一起顯示——這也是隔離範圍的一部分，不是只有 Pod/Deployment。
        result = subprocess.run(
            ["git", "log", "--pretty=format:%H|%s|%ai", "--", f"manifests/{user_ns}/"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            capture_output=True, encoding="utf-8", errors="replace", timeout=10
        )
        commits = []
        for line in (result.stdout or "").strip().split("\n"):
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
    user_ns = _user_namespace(session["username"])
    try:
        from healer.pod_watcher import scan_once
        issues = scan_once(namespace=user_ns)
        return jsonify({"issues": issues or []})
    except Exception as e:
        try:
            from kubernetes import client as k8s_client, config as k8s_config
            k8s_config.load_kube_config()
            v1 = k8s_client.CoreV1Api()
            pods = v1.list_namespaced_pod(namespace=user_ns)
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


def _real_heal_pod(pod_name: str, namespace: str = "default") -> dict:
    """對單一 pod 執行真正的診斷＋補救（規則層/LLM 根因分析 → healer.remediate 對應動作），
    不是單純刪除 pod。刪除只是 remediate 眾多動作之一（CrashLoopBackOff 才會用到），
    OOMKilled 會改記憶體 limit、ImagePullBackOff 會嘗試 rollout undo，各自對應。"""
    from kubernetes import client as k8s_client, config as k8s_config
    k8s_config.load_kube_config()
    v1 = k8s_client.CoreV1Api()
    pod = v1.read_namespaced_pod(name=pod_name, namespace=namespace)
    from healer.pod_watcher import _check_pod, _get_pod_logs, _get_pod_events
    issue = _check_pod(pod)
    if not issue:
        return {"ok": True, "action": "none", "message": f"Pod {pod_name} 目前狀態正常，無需修復 / already healthy", "root_cause": None}
    logs = _get_pod_logs(issue["pod_name"], issue["namespace"], issue["container"])
    events = _get_pod_events(issue["pod_name"], issue["namespace"])
    context = {**issue, "logs": logs, "events": events}
    from healer.diagnose import diagnose_issue
    diagnosis = diagnose_issue(context)
    from healer.remediate import remediate
    result = remediate(issue, diagnosis)
    result["root_cause"] = diagnosis.get("root_cause")
    result["severity"] = diagnosis.get("severity")
    return result


# 背景自動監控狀態（記憶體內，重啟 web_demo.py 會清空；不用資料庫，量小且非關鍵資料）。
# 2026-09-14 修正：之前 "running" 只在啟動那一刻設一次 True，之後永遠不會再檢查，
# 如果背景執行緒中途死掉，這裡會一直謊報「運行中」——直接違反 AGENT_RULES.md 的
# 核心原則「判斷即時、不用舊值唬弄」。現在改成即時查真正的執行緒物件是否還活著，
# 並且額外比對 last_scan 有沒有久到不正常（執行緒卡住/掛住不會被 is_alive() 抓到，
# 因為卡住的執行緒技術上還「活著」，只是沒有在前進，只能靠比對時間戳才能發現）。
_healer_bg_state = {"last_scan": None, "recent_actions": [], "started_at": None, "interval": 30}
_healer_bg_lock = threading.Lock()
_healer_bg_thread = None  # 全域保存 Thread 物件本身，供 /api/healer/status 查 is_alive()
_HEALER_BG_MAX_HISTORY = 50

# 2026-09-15：Pod 時間序列記錄（Healer 視覺化的趨勢圖用），跟 _healer_bg_state
# 是不同的東西——那個只記「有問題、被修復過」的 Pod，這裡記「每一個 Pod 每一刻的
# 輕量快照」，不管有沒有問題都記，才能畫出「這段時間大部分健康還是一直在壞」的圖。
# key 是 pod 名稱（不是 Deployment 名稱）——K8s 裡 Pod 名稱不是永久的，如果 Healer
# 真的刪掉壞 Pod 讓它重建，新 Pod 會有新名稱、歷史自然斷掉重新開始，這是已知、
# 可接受的限制（沒有跨 Pod 名稱拼接歷史，那是要比對 app label 的更大工程）。
_pod_history = {}  # key: f"{namespace}/{pod_name}" -> list[sample dict]
_pod_history_lock = threading.Lock()
_POD_HISTORY_MAX_SAMPLES = 120  # 30 秒一次，120 筆 ≈ 1 小時


def _pod_light_snapshot(pod) -> dict:
    """給時間序列用的輕量快照，不像 _pod_detail() 那麼重（不查 events，那個較貴）。"""
    statuses = pod.status.container_statuses or []
    return {
        "t": datetime.utcnow().isoformat(),
        "phase": (pod.status.phase if pod.status else "") or "Unknown",
        "restarts": sum(cs.restart_count for cs in statuses),
        "ready": sum(1 for cs in statuses if cs.ready),
        "total": len(statuses),
    }


def _get_pod_history(namespace: str, name: str) -> list:
    with _pod_history_lock:
        return list(_pod_history.get(f"{namespace}/{name}", []))


def _healer_background_loop(interval: int = 30):
    """比照 healer/pod_watcher.py 的 watch_forever()，但常駐在 web_demo.py process 裡，
    讓部署完成後不需要使用者手動開另一個 terminal 跑 --watch 就會自動掃描+修復。
    每個 pod 問題用 (namespace, pod_name, reason) 當 key，避免同一個問題重複觸發修復
    造成無限刪除/修改迴圈；問題消失後 key 會被清掉，之後再發生會重新處理。

    掃全叢集（namespace=""，scan_once 本來就支援空字串＝全部 namespace），不是只掃
    default——每個帳號有自己的 namespace 之後，這樣新使用者的 namespace 也會被自動
    監控到，「零接觸自癒」才對所有使用者都成立，不是只對舊的共用資源成立。跟使用者
    主動觸發的 /api/healer/scan／fix／auto_fix（只看/只修自己 namespace）是不同範圍，
    刻意設計成不同——背景監控要顧到所有人，主動操作不能讓一個使用者修到別人的 Pod。"""
    seen = set()
    stop = threading.Event()
    while not stop.wait(interval):
        if not K8S_ENABLED:
            continue
        try:
            from healer.pod_watcher import scan_once
            issues = scan_once(namespace="") or []
            now_keys = set()
            for issue in issues:
                key = f"{issue['namespace']}/{issue['pod_name']}/{issue['reason']}"
                now_keys.add(key)
                if key in seen:
                    continue
                seen.add(key)
                try:
                    result = _real_heal_pod(issue["pod_name"], issue["namespace"])
                    entry = {
                        "time": datetime.utcnow().isoformat(), "pod": issue["pod_name"],
                        "namespace": issue["namespace"], "reason": issue["reason"],
                        "root_cause": result.get("root_cause"), "action": result.get("action"),
                        "ok": result.get("ok"), "message": result.get("message"),
                    }
                except Exception as e:
                    entry = {
                        "time": datetime.utcnow().isoformat(), "pod": issue["pod_name"],
                        "namespace": issue["namespace"], "reason": issue["reason"],
                        "ok": False, "error": str(e),
                    }
                with _healer_bg_lock:
                    _healer_bg_state["recent_actions"].insert(0, entry)
                    _healer_bg_state["recent_actions"] = _healer_bg_state["recent_actions"][:_HEALER_BG_MAX_HISTORY]
            seen &= now_keys
            with _healer_bg_lock:
                _healer_bg_state["last_scan"] = datetime.utcnow().isoformat()
        except Exception:
            pass

        # Pod 時間序列快照——跟上面「掃問題、修復」是獨立的邏輯，故意包在自己的
        # try/except，避免這段（新功能，較不成熟）萬一出錯拖累上面已經穩定運作的
        # 自動修復邏輯。查全叢集所有 Pod（不是只有問題 Pod），所有帳號都要有趨勢圖。
        try:
            core = k8s_client.CoreV1Api()
            for pod in core.list_pod_for_all_namespaces().items:
                key = f"{pod.metadata.namespace}/{pod.metadata.name}"
                snap = _pod_light_snapshot(pod)
                with _pod_history_lock:
                    hist = _pod_history.setdefault(key, [])
                    hist.append(snap)
                    del hist[:-_POD_HISTORY_MAX_SAMPLES]
        except Exception:
            pass


def _healer_bg_liveness() -> dict:
    """即時算出背景自動修復迴圈的真實狀態，不用啟動時設過一次就不再檢查的舊旗標。"""
    with _healer_bg_lock:
        last_scan = _healer_bg_state["last_scan"]
        interval = _healer_bg_state.get("interval", 30)
        started_at = _healer_bg_state.get("started_at")
        recent = list(_healer_bg_state["recent_actions"])

    thread_alive = bool(_healer_bg_thread is not None and _healer_bg_thread.is_alive())
    now = datetime.utcnow()
    stale = False
    if K8S_ENABLED and thread_alive:
        if last_scan is None:
            # 剛啟動、第一次掃描還沒發生前，給一次 interval 的寬限期，不要誤判成卡住。
            try:
                grace_until = datetime.fromisoformat(started_at) + timedelta(seconds=interval * 1.5)
                stale = now > grace_until
            except Exception:
                stale = False
        else:
            try:
                last_dt = datetime.fromisoformat(last_scan)
                stale = (now - last_dt).total_seconds() > interval * 3
            except Exception:
                stale = False

    running = bool(K8S_ENABLED and thread_alive and not stale)
    message = None
    if K8S_ENABLED and not thread_alive:
        message = (
            "自動監控背景執行緒已經停止（可能因未預期錯誤中斷），現在只能靠手動 Scan Now / "
            "Auto Fix All，不會自動偵測新問題。請重新啟動 web_demo.py 以恢復自動監控。 / "
            "The background auto-heal thread has stopped — automatic detection is currently off "
            "(manual Scan/Fix still works). Restart web_demo.py to recover it."
        )
    elif K8S_ENABLED and stale:
        message = (
            "自動監控似乎卡住了（太久沒有新的掃描紀錄，可能在等一個沒有逾時設定的 K8s API 呼叫）。"
            "建議重新啟動 web_demo.py 確認。 / "
            "Auto-monitoring looks stuck (no recent scan, possibly blocked on a K8s API call with "
            "no timeout). Consider restarting web_demo.py."
        )

    return {
        "running": running, "thread_alive": thread_alive, "last_scan": last_scan,
        "recent_actions": recent, "message": message,
    }


@app.route("/api/healer/status")
def api_healer_status():
    if "username" not in session: return jsonify({"error": "Not authenticated"}), 401
    return jsonify(_healer_bg_liveness())


@app.route("/api/healer/fix", methods=["POST"])
def api_healer_fix():
    if "username" not in session: return jsonify({"error": "Not authenticated"}), 401
    data = request.get_json() or {}
    pod_name = data.get("pod_name", "")
    if not pod_name:
        return jsonify({"success": False, "error": "No pod name"})
    try:
        result = _real_heal_pod(pod_name, _user_namespace(session["username"]))
        return jsonify({
            "success": result.get("ok", False),
            "message": result.get("message"),
            "root_cause": result.get("root_cause"),
            "action": result.get("action"),
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route("/api/healer/auto_fix", methods=["POST"])
def api_healer_auto_fix():
    if "username" not in session: return jsonify({"error": "Not authenticated"}), 401
    try:
        from healer.pod_watcher import scan_once
        issues = scan_once(namespace=_user_namespace(session["username"])) or []
        fixed, failed, details = 0, 0, []
        for issue in issues:
            try:
                result = _real_heal_pod(issue["pod_name"], issue["namespace"])
                if result.get("ok"):
                    fixed += 1
                else:
                    failed += 1
                details.append({
                    "pod": issue["pod_name"], "action": result.get("action"),
                    "root_cause": result.get("root_cause"), "ok": result.get("ok"),
                    "message": result.get("message"),
                })
            except Exception as e:
                failed += 1
                details.append({"pod": issue["pod_name"], "ok": False, "error": str(e)})
        return jsonify({"fixed": fixed, "failed": failed, "details": details})
    except Exception as e:
        return jsonify({"fixed": 0, "failed": 0, "error": str(e)})


@app.route("/api/metrics")
def api_metrics():
    if "username" not in session: return jsonify({"error": "Not authenticated"}), 401
    # 2026-09-15 修好一個多租戶隔離漏掉的地方：這幾條 Prometheus 查詢原本寫死
    # namespace="default"（多租戶改成每人一個 namespace 之前留下的），改成每人
    # 一個 namespace 之後從沒更新過，導致這頁一直顯示舊的共用 default namespace
    # 的統計（例如顯示 9 個 pod，其實是 auto-app+my-cache+zt-smoke 加總，不是
    # 使用者自己部署的數量）——跟 Healer 頁面顯示的「Total Pods」互相矛盾，
    # 使用者實測時直接發現這個不一致。
    user_ns = _user_namespace(session["username"])
    # localhost 在 Windows 會先試 IPv6 ::1、逾時才 fallback 到 127.0.0.1，每個查詢多等好幾秒。
    # 直接用 127.0.0.1 省掉那段。可用 PROMETHEUS_URL 覆寫。
    prom_url = os.environ.get("PROMETHEUS_URL", "http://127.0.0.1:9090").replace("localhost", "127.0.0.1")
    try:
        # 2026-09-15：這裡原本是自己土砲寫一個 urllib 版本的 prom_query()，完全沒用
        # observability/prometheus_client.py 這個早就寫好的模組——因為那時 Prometheus
        # 根本沒真的部署過，這段程式碼從沒被跑過也沒人發現。現在 kube-prometheus-stack
        # 真的裝上去了，改成呼叫這個模組，不要維護兩份重複的查詢邏輯。
        from observability.prometheus_client import PrometheusClient
        client = PrometheusClient(prom_url, timeout=2)

        def prom_query(q):
            results = client.query(q)
            if results:
                return results[0]["value"]
            return None

        prometheus_up = client.is_alive()

        metrics = {"prometheus_up": prometheus_up}
        if prometheus_up:
            try: metrics["pod_count"] = prom_query(f'count(kube_pod_info{{namespace="{user_ns}"}})')
            except Exception: metrics["pod_count"] = None
            try: metrics["running_pods"] = prom_query(f'count(kube_pod_status_phase{{phase="Running",namespace="{user_ns}"}})')
            except Exception: metrics["running_pods"] = None
            # kube_pods 保留「不分 namespace」的整叢集數字，但明確標成 cluster_pods，
            # 不要跟上面兩個「使用者自己」的數字混在一起看，前端要分開標示清楚。
            try: metrics["cluster_pods"] = prom_query("count(kube_pod_info)")
            except Exception: metrics["cluster_pods"] = None
        else:
            # 已經知道連不上，不用再浪費時間逐一嘗試（每次 timeout=2 秒，三次疊起來
            # 會多等 6 秒），直接跳到下面的 K8s API 備援。cluster_pods 沒有對應的
            # K8s API 備援（那需要跨所有使用者 namespace 加總，這裡先不做），
            # 保持 None，前端在 Prometheus 離線時本來就不會顯示這一項。
            metrics["pod_count"] = None
            metrics["running_pods"] = None
            metrics["cluster_pods"] = None

        if K8S_ENABLED and (metrics["pod_count"] is None or metrics["running_pods"] is None):
            pods = k8s_get_pods(namespace=user_ns)
            if metrics["pod_count"] is None:
                metrics["pod_count"] = len(pods)
            if metrics["running_pods"] is None:
                metrics["running_pods"] = len([p for p in pods if p.get("phase") == "Running"])
            metrics["source"] = "k8s-api-direct (Prometheus unreachable)" if not prometheus_up else "prometheus+k8s-fallback"
        metrics["namespace"] = user_ns
        return jsonify({"connected": True, "url": prom_url, "metrics": metrics})
    except Exception as e:
        return jsonify({"connected": False, "error": str(e)})

if __name__ == "__main__":
    print("=" * 60)
    print("  ZeroTouch K8s Web Demo v2")
    print("=" * 60)
    print(f"  K8s   : {'Connected' if K8S_ENABLED else 'Simulation'}")
    print(f"  Open  : http://localhost:5050")
    if K8S_ENABLED:
        _HEALER_BG_INTERVAL = 30
        _healer_bg_thread = threading.Thread(
            target=_healer_background_loop, daemon=True, kwargs={"interval": _HEALER_BG_INTERVAL})
        _healer_bg_thread.start()
        _healer_bg_state["started_at"] = datetime.utcnow().isoformat()
        _healer_bg_state["interval"] = _HEALER_BG_INTERVAL
        print("  Healer: background auto-heal loop started (scan every 30s)")
    else:
        print("  Healer: background auto-heal loop NOT started (K8s not connected)")
    app.run(host="0.0.0.0", port=5050, debug=False)
