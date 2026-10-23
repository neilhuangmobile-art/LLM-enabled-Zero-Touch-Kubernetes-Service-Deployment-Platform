"""
web_demo.py  —  ZeroTouch K8s Platform v2
Clean white UI + Login/Register + Pod Details + AI Chat + Real K8s
"""
import sys, os, shutil
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core.config import ensure_utf8_output
ensure_utf8_output()

import threading, json, urllib.request, hashlib, secrets, re, uuid, requests
from datetime import datetime, timezone, timedelta
from flask import Flask, request, jsonify, render_template_string, session, redirect, url_for

from core.config import ROOT, YAML_DIR, MODEL_SERVER_URL, ADMIN_EMAILS, healer_owns_namespace
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

# ── 上外網防護（2026-10-09，見 core/web_security.py、docs/setup.md）──────────
from core.config import PUBLIC_BASE_URL, WEB_PORT
from core.web_security import RateLimiter, check_request, too_many_message
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    # Lax：Google OAuth callback 是頂層 GET 導回，Lax 仍會帶 cookie，不影響登入
    SESSION_COOKIE_SAMESITE="Lax",
    # 對外網址是 https 時 cookie 只走 https（瀏覽器把 http://localhost 視為安全來源，本機測試不受影響）
    SESSION_COOKIE_SECURE=PUBLIC_BASE_URL.startswith("https://"),
    # 請求大小上限（2026-10-10 背景安全審查）：原本沒有設定，任何 API 都會先把整個請求讀進記憶體
    # 才檢查自己的上限（例如 compose 的 100 KB），送 1 GB 的請求就能拖垮伺服器。
    # 目前最大的合法請求是客戶回報器的 256 KB。
    MAX_CONTENT_LENGTH=1024 * 1024,
)
# 代理標頭（X-Forwarded-*）由 waitress 的 trusted_proxy 處理，見檔尾 __main__：只有
# 「只綁 127.0.0.1、前面一定是本機通道程式」時才信任，否則區網任何人都能偽造
# X-Forwarded-For 繞過頻率限制。（2026-10-09 實測：waitress 預設會先清掉不受信任的
# X-Forwarded-*，所以不能靠 werkzeug ProxyFix。）
_rate_limiter = RateLimiter()


def _external_url(path: str) -> str:
    """給外部用的完整網址（Google OAuth redirect_uri、客戶安裝指令、回報網址）。
    有 PUBLIC_BASE_URL 就直接用它組，不依賴各家通道會不會、怎麼帶代理標頭。"""
    if PUBLIC_BASE_URL:
        return PUBLIC_BASE_URL + path
    return request.host_url.rstrip("/") + path


@app.before_request
def _rate_limit():
    auth = request.headers.get("Authorization", "")
    token = auth[7:] if auth.startswith("Bearer ") else None
    allowed, retry, rule = check_request(
        _rate_limiter, request.path, request.method, request.remote_addr or "?",
        username=session.get("username"), token=token)
    if allowed:
        return None
    msg = too_many_message(retry)
    headers = {"Retry-After": str(max(1, int(retry + 0.999)))}
    if rule == "auth":
        # 登入/註冊是表單送出，回 JSON 新手看不懂，直接回登入頁並顯示原因
        page = "register" if request.path.startswith("/auth/register") else "login"
        return render_template_string(
            HTML, logged_in=False, page=page, error=msg, k8s=K8S_ENABLED, username='',
            google_login_available=_google_oauth_configured()), 429, headers
    return jsonify({"error": msg, "retry_after": headers["Retry-After"]}), 429, headers

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

# ── Google 登入（選用，2026-09-29 從隊友分支移植；沒設定 GOOGLE_CLIENT_ID/SECRET
#    時整個功能不出現，不是顯示了讓使用者點下去才碰到錯誤——AGENT_RULES.md
#    新手友善原則）。用專案已有的 requests 套件手動打 Google OAuth2 三個端點
#    （authorize → token exchange → userinfo），沒有加新依賴。─────────────────
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"


def _google_oauth_configured() -> bool:
    return bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)


def _derive_username_from_google(email: str) -> str:
    """從 Google 帳號的 email 衍生一個唯一的本地 username。撞名（不管是既有密碼
    帳號還是別的 Google 帳號）一律加短 hash 後綴變成新帳號，絕不覆蓋/合併既有
    帳號——自動合併等於「只要知道對方 username，就能用自己的 Google 帳號取得
    對方的 K8s namespace 存取權」，是帳號被冒用的風險。"""
    prefix = re.sub(r"[^a-z0-9]", "-", (email.split("@")[0] or "").lower()).strip("-")[:40]
    prefix = prefix or "google-user"
    # 2026-10-09：除了帳號名稱本身，namespace 也不能跟既有帳號撞（見 _user_namespace）
    if prefix not in USERS and not _namespace_taken(prefix):
        return prefix
    suffix = hashlib.sha256(email.encode()).hexdigest()[:6]
    candidate = f"{prefix}-{suffix}"
    n = 2
    while candidate in USERS or _namespace_taken(candidate):
        candidate = f"{prefix}-{suffix}-{n}"
        n += 1
    return candidate


def _is_admin_user(username: str) -> bool:
    """真正的管理員權限只歸屬於「用 Google 登入、且 email 在 ADMIN_EMAILS 允許清單裡」
    的帳號。絕不檢查 username 字串本身——密碼註冊帳號可以隨便取名叫 "admin"（這個
    專案的 users.json 裡剛好就有一個舊測試帳號是這樣），聊天內容裡自稱「我是管理員」
    更不可信，兩者都不能拿來判斷身分，否則任何人都能冒充。"""
    u = USERS.get(username)
    if not isinstance(u, dict) or u.get("auth_provider") != "google":
        return False
    email = (u.get("email") or "").strip().lower()
    return email in ADMIN_EMAILS

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


def _k8s_env(app_name, env, force=None):
    from gitops.manifest_writer import env_entries
    out = []
    for e in env_entries(app_name, env, force):
        if "valueFrom" in e:
            ref = e["valueFrom"]["secretKeyRef"]
            out.append(k8s_client.V1EnvVar(name=e["name"], value_from=k8s_client.V1EnvVarSource(
                secret_key_ref=k8s_client.V1SecretKeySelector(name=ref["name"], key=ref["key"]))))
        else:
            out.append(k8s_client.V1EnvVar(name=e["name"], value=e["value"]))
    return out


def k8s_deploy(app_name, image, replicas, port=80, memory=None, cpu=None, namespace=None,
               env=None, container_port_override=None, expose=True, dns_alias=False,
               command=None, args=None, volumes=None, secret_keys=None):
    """env／container_port_override／expose／dns_alias 是 docker-compose 匯入用的（2026-10-10）：
    環境變數、容器實際監聽的 port、要不要對外開放（compose 沒寫 ports 的服務只給內部連）、
    另外建一個跟服務同名的內部連線入口（其他服務才能照 compose 的寫法用「db:5432」連過來）。"""
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
        from agents.cost_agent import recommended_resources, recommended_env, recommended_probes, container_port
        # 使用者講的 port 是對外的 port；容器裡用映像檔實際監聽的 port（nginx 是 80），Service 負責轉過去
        cport = container_port(image, port, container_port_override)
        _res = recommended_resources(image, app_name, memory, cpu)
        resources = k8s_client.V1ResourceRequirements(requests=_res["requests"], limits=_res["limits"])
        # 健康檢查用 dict 傳給 client（序列化時原樣送出，欄位名稱就是 K8s 的 camelCase）
        _probes = recommended_probes(image, cport)
        from gitops.manifest_writer import pod_volumes, build_pvc_docs
        _pod_vols, _vol_mounts = pod_volumes(app_name, volumes)
        container = k8s_client.V1Container(
            name=app_name, image=image,
            ports=[k8s_client.V1ContainerPort(container_port=cport)],
            resources=resources,
            # 密碼類環境變數從 Secret 讀（值不寫進設定檔與 Git），其他的直接寫
            env=_k8s_env(app_name, {**recommended_env(image), **(env or {})}, secret_keys) or None,
            readiness_probe=_probes.get("readinessProbe"),
            liveness_probe=_probes.get("livenessProbe"),
            command=command or None, args=args or None,
            volume_mounts=_vol_mounts or None,
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
                    spec=k8s_client.V1PodSpec(containers=[container], volumes=_pod_vols or None),
                ),
            ),
        )
        svc = k8s_client.V1Service(
            api_version="v1", kind="Service",
            metadata=k8s_client.V1ObjectMeta(name=f"{app_name}-svc"),
            spec=k8s_client.V1ServiceSpec(
                selector={"app": app_name},
                ports=[k8s_client.V1ServicePort(port=port, target_port=cport)],
                type="LoadBalancer" if expose else "ClusterIP",
            ),
        )
        alias = None
        if dns_alias:
            alias = k8s_client.V1Service(
                api_version="v1", kind="Service",
                metadata=k8s_client.V1ObjectMeta(name=app_name, labels={"zerotouch/alias-of": app_name}),
                spec=k8s_client.V1ServiceSpec(selector={"app": app_name}, type="ClusterIP", ports=(
                    [k8s_client.V1ServicePort(name="app", port=cport, target_port=cport)] +
                    ([k8s_client.V1ServicePort(name="external", port=port, target_port=cport)] if port != cport else []))),
            )
        # 保存空間要先建好，Pod 才掛得上；已經存在的不動（PVC 建立後大部分欄位不能改，也不能動到資料）
        for _pvc in build_pvc_docs({"app_name": app_name, "volumes": volumes}, namespace):
            try:
                core.read_namespaced_persistent_volume_claim(_pvc["metadata"]["name"], namespace, _request_timeout=(5, 10))
            except Exception:
                core.create_namespaced_persistent_volume_claim(namespace, _pvc, _request_timeout=(5, 10))
        try:
            api.replace_namespaced_deployment(app_name, namespace, deploy, _request_timeout=(5, 10))
        except Exception:
            api.create_namespaced_deployment(namespace, deploy, _request_timeout=(5, 10))
        try:
            core.replace_namespaced_service(f"{app_name}-svc", namespace, svc, _request_timeout=(5, 10))
        except Exception:
            core.create_namespaced_service(namespace, svc, _request_timeout=(5, 10))
        if alias is not None:
            try:
                core.replace_namespaced_service(app_name, namespace, alias, _request_timeout=(5, 10))
            except Exception:
                core.create_namespaced_service(namespace, alias, _request_timeout=(5, 10))
        from gitops.manifest_writer import split_secret_env, secret_name
        _secret_vals = split_secret_env(env or {}, secret_keys)[1]
        if _secret_vals:
            sec = k8s_client.V1Secret(api_version="v1", kind="Secret", type="Opaque",
                                      metadata=k8s_client.V1ObjectMeta(name=secret_name(app_name)),
                                      string_data=_secret_vals)
            try:
                core.replace_namespaced_secret(secret_name(app_name), namespace, sec, _request_timeout=(5, 10))
            except Exception:
                core.create_namespaced_secret(namespace, sec, _request_timeout=(5, 10))
        os.makedirs(YAML_DIR, exist_ok=True)
        path = os.path.join(YAML_DIR, f"{app_name}.yaml")
        with open(path, "w", encoding="utf-8") as f:
            f.write(yaml_lib.dump(deploy.to_dict()))
            f.write("---\n")
            f.write(yaml_lib.dump(svc.to_dict()))
        return True, f"已部署 {app_name}"
    except Exception as e:
        return False, str(e)

def _last_restart_iso(pod):
    times = [cs.last_state.terminated.finished_at for cs in ((pod.status.container_statuses or []) if pod.status else [])
             if cs.last_state and cs.last_state.terminated and cs.last_state.terminated.finished_at]
    return max(times).isoformat() if times else None


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
            if (p.metadata.labels or {}).get("zerotouch/waker"):
                continue   # 休眠模式的喚醒器，不是使用者的 Pod
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
                # 最近一次重啟的時間：重啟次數是從 Pod 建立以來一直累加的，主機重開機時每個 Pod 都會被算一次，
                # 只看次數會把很久以前的重啟也當成現在有問題（2026-10-21 全系統檢查：17 個 Pod 全被標成需要注意）
                "last_restart_at": _last_restart_iso(p),
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
            # 休眠模式的喚醒器是平台自己的元件，不是使用者的服務
            if (d.metadata.labels or {}).get("zerotouch/waker"):
                continue
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
                # 休眠中：副本數是 0，sleep_replicas 是醒來時要開幾個（見 core/sleep_mode.py）
                "sleeping": bool((d.metadata.annotations or {}).get("zerotouch.k8s/sleep-replicas")),
                "sleep_replicas": int((d.metadata.annotations or {}).get("zerotouch.k8s/sleep-replicas") or 0),
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
                result.append({"name": s.metadata.name, "app": app, "port": p.port,
                               "namespace": s.metadata.namespace})
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


_CLEAN_USERNAME_RE = re.compile(r"[a-z0-9]([-a-z0-9]{0,48}[a-z0-9])?")


def _user_namespace(username: str) -> str:
    """把帳號名稱轉成合法的 K8s namespace 名稱（DNS-1123 label：小寫字母數字+連字號，
    開頭結尾不能是連字號，長度上限 63）。每個帳號固定對應一個 namespace，讓
    Pod/Deployment/Chat 等資源天然互相隔離（K8s 層級的隔離，不是 UI 濾掉而已）。

    2026-10-09 修正跨帳號隔離破口（docs/security_review.md 32 節）：舊規則只做轉小寫、
    非法字元換 -，`alice` 與 `Alice`、`bob_1` 與 `bob-1` 共用同一個 namespace，**任何兩個
    中文帳號都會變成同一個 user-unknown**，彼此看得到、刪得掉對方的 Pod。
    現在：本來就是乾淨格式的帳號維持原名（既有帳號的 namespace 不變），其他帳號加上帳號
    原文的雜湊後綴；註冊時再用 _namespace_taken() 擋掉刻意撞名。"""
    raw = username or ""
    if _CLEAN_USERNAME_RE.fullmatch(raw):
        return f"user-{raw}"
    safe = re.sub(r"[^a-z0-9-]", "-", raw.lower())
    safe = re.sub(r"-+", "-", safe).strip("-")[:40].strip("-") or "u"
    # 16 個十六進位字元：8 個（32 位元）可以被暴力碰撞（2026-10-09 背景安全審查第二輪）
    return f"user-{safe}-{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]}"


def _legacy_user_namespace(username: str) -> str:
    """2026-10-09 之前的舊規則（只為了保留舊名稱用，不要拿來決定實際 namespace）。"""
    safe = re.sub(r"[^a-z0-9-]", "-", (username or "").lower()).strip("-")[:50]
    return f"user-{safe or 'unknown'}"


def _namespace_taken(username: str) -> bool:
    """這個帳號名稱算出來的 namespace 是否已經屬於別的帳號。光靠雜湊不夠：有心人可以先算出
    `Alice` 的雜湊、搶先註冊乾淨格式的 `alice-<雜湊>` 撞到同一個 namespace。"""
    # 平台自己叢集的 namespace 與客戶端 repo/namespace（gitops.tenant_repo.tenant_slug）都要檢查：
    # 這是確定性的保證，不依賴雜湊長度。
    # 也要保留既有帳號「舊規則」下的 namespace 名稱：某帳號改規則後 namespace 換了，舊名稱
    # 空出來，新帳號若拿到同名 namespace 會繼承裡面殘留的 Pod 與以 namespace 為 key 的資料
    # （Pod 歷史、manifests/<namespace>/）——2026-10-09 背景安全審查第三輪。
    from gitops.tenant_repo import tenant_slug
    ns, slug = _user_namespace(username), tenant_slug(username)
    return any(ns in (_user_namespace(u), _legacy_user_namespace(u)) or tenant_slug(u) == slug
               for u in USERS if u != username)


# 每個帳號的保存空間上限（2026-10-10 背景安全審查）：compose 的具名 volume 會建 PVC，原本只限制
# 「每個服務最多 5 個、每個最多 5Gi」，沒限制一個帳號總共能建多少，一直部署就能把主機硬碟吃光。
# 用 K8s ResourceQuota 讓叢集本身強制執行（就算繞過平台也一樣），部署前另外先算給使用者看。
STORAGE_QUOTA_PVCS = 10
STORAGE_QUOTA_GI = 10


def _ensure_storage_quota(core, namespace: str):
    quota = k8s_client.V1ResourceQuota(
        metadata=k8s_client.V1ObjectMeta(name="zerotouch-storage"),
        spec=k8s_client.V1ResourceQuotaSpec(hard={
            "persistentvolumeclaims": str(STORAGE_QUOTA_PVCS), "requests.storage": f"{STORAGE_QUOTA_GI}Gi"}))
    try:
        core.replace_namespaced_resource_quota("zerotouch-storage", namespace, quota)
    except Exception:
        try:
            core.create_namespaced_resource_quota(namespace, quota)
        except Exception:
            pass


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
        _ensure_storage_quota(core, namespace)
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
    # 客戶自帶叢集：撤銷安裝與回報 token（回報器會開始收到 401）。客戶叢集裡的服務跑在
    # 客戶自己的電腦上，平台沒有權限、也不該去刪除。
    try:
        CLUSTERS.forget(username)
    except NameError:
        pass


def _sum_cluster_resource_requests(exclude_name: str = None, exclude_namespace: str = None):
    """加總目前叢集裡「全部 namespace、全部 Deployment」目前的資源需求
    （cpu millicores、memory bytes），用真實 replicas 數。exclude_name/exclude_namespace
    用來排除掉呼叫端要自己另外用新數字重新計算的那個 Deployment（scale 操作要用新
    replicas 取代舊值，不能疊加舊的）。查不到就回 (0, 0)，呼叫端要自己判斷要不要略過。"""
    from agents.cost_agent import _parse_cpu_millicores, _parse_memory_bytes
    apps_api = k8s_client.AppsV1Api()
    deployments = apps_api.list_deployment_for_all_namespaces().items
    total_cpu_mc = 0
    total_mem_b = 0
    for d in deployments:
        if exclude_name and d.metadata.name == exclude_name and d.metadata.namespace == exclude_namespace:
            continue
        containers = d.spec.template.spec.containers if d.spec.template.spec else []
        if not containers or not containers[0].resources or not containers[0].resources.requests:
            continue
        reps = d.spec.replicas or 1
        req = containers[0].resources.requests
        total_cpu_mc += (_parse_cpu_millicores(req.get("cpu")) or 0) * reps
        total_mem_b += (_parse_memory_bytes(req.get("memory")) or 0) * reps
    return total_cpu_mc, total_mem_b


def _pod_slots():
    """回傳 (叢集可放的 Pod 總數, 目前已佔用的 Pod 數)；查不到回 (None, None)。

    2026-10-10 組員極限測試發現：要求 100 個副本只排得下 95 個，剩下 5 個卡在 Pending
    （Too many pods），平台卻沒有任何警告。原因是 CPU、記憶體檢查都過了，但每個節點能放的
    Pod 數量也有上限（Docker Desktop 預設 110 個，包含系統元件），原本完全沒算。"""
    if not K8S_ENABLED:
        return None, None
    try:
        core = k8s_client.CoreV1Api()
        nodes = core.list_node().items
        total = 0
        for n in nodes:
            ready = any(c.type == "Ready" and c.status == "True" for c in (n.status.conditions or []))
            if ready and not n.spec.unschedulable:
                total += int((n.status.allocatable or {}).get("pods", "0") or 0)
        used = sum(1 for p in core.list_pod_for_all_namespaces().items
                   if (p.status.phase or "") not in ("Succeeded", "Failed"))
        return (total or None), used
    except Exception:
        return None, None


def _pod_slot_message(extra: int, total: int, used: int) -> str:
    return (f"叢集的 Pod 位置不夠：目前已經有 {used} 個 Pod（包含系統元件），這次要再增加 {extra} 個，"
            f"但叢集最多只能放 {total} 個。多出來的 Pod 會卡在 Pending 排不進去，畫面不會顯示錯誤。"
            f"請減少副本數，或先刪除不用的服務。")


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
        base_cpu_mc, base_mem_b = _sum_cluster_resource_requests(exclude_name=name, exclude_namespace=namespace)
        apps_api = k8s_client.AppsV1Api()
        target_req = {}
        try:
            target = apps_api.read_namespaced_deployment(name, namespace)
            # Pod 位置：只算這次「多出來」的 Pod
            slots, used = _pod_slots()
            extra = new_replicas - int(target.spec.replicas or 0)
            if slots and used is not None and extra > 0 and used + extra > slots:
                return True, _pod_slot_message(extra, slots, used)
            containers = target.spec.template.spec.containers if target.spec.template.spec else []
            if containers and containers[0].resources and containers[0].resources.requests:
                target_req = containers[0].resources.requests
        except Exception:
            pass
        total_cpu_mc = base_cpu_mc + (_parse_cpu_millicores(target_req.get("cpu")) or 0) * new_replicas
        total_mem_b = base_mem_b + (_parse_memory_bytes(target_req.get("memory")) or 0) * new_replicas
        over_cpu = node_cpu_mc and total_cpu_mc > node_cpu_mc
        over_mem = node_mem_b and total_mem_b > node_mem_b
        if over_cpu or over_mem:
            return True, (
                f"這個操作會讓整個叢集的資源需求超出節點容量：套用後全部 Deployment 合計約需要 "
                f"{total_cpu_mc}m CPU / {total_mem_b // (1024**2)}Mi 記憶體，"
                f"但叢集最大節點只有 {node_cpu_mc}m CPU / {node_mem_b // (1024**2)}Mi 可用。"
                f"多出來排不進去的 Pod 會卡在 Pending 狀態，不會顯示錯誤，容易被忽略。"
                f"已阻止這次操作，請降低副本數或先移除/縮小其他部署。"
            )
        return False, None
    except Exception:
        return False, None


def _my_deployment_resource_breakdown(namespace: str) -> dict:
    """列出單一 namespace 內每個 Deployment 的資源需求（cpu/mem × replicas），
    給監控台「我的部署」明細用。故意不擴充 `k8s_get_deployments()`——那個函式的
    回傳格式被 Pods/Deployments 頁面等既有呼叫端依賴，這裡另外寫一個只服務監控台
    的輕量版本，避免動到已經在用的介面。2026-09-29 從隊友分支移植。"""
    from agents.cost_agent import _parse_cpu_millicores, _parse_memory_bytes, _estimate_monthly_cost
    deployments = []
    total_cpu_mc = 0
    total_mem_b = 0
    if K8S_ENABLED:
        try:
            api = k8s_client.AppsV1Api()
            for d in api.list_namespaced_deployment(namespace).items:
                reps = d.spec.replicas or 0
                containers = d.spec.template.spec.containers if d.spec.template.spec else []
                cpu_mc = mem_b = 0
                if containers and containers[0].resources and containers[0].resources.requests:
                    req = containers[0].resources.requests
                    cpu_mc = (_parse_cpu_millicores(req.get("cpu")) or 0) * reps
                    mem_b = (_parse_memory_bytes(req.get("memory")) or 0) * reps
                total_cpu_mc += cpu_mc
                total_mem_b += mem_b
                deployments.append({
                    "name": d.metadata.name,
                    "replicas": reps,
                    "cpu_cores": round(cpu_mc / 1000, 2),
                    "mem_gib": round(mem_b / (1024 ** 3), 3),
                })
        except Exception:
            pass
    # 重用既有的計價公式，不重新發明——包一個假 manifest 餵給 _estimate_monthly_cost()，
    # 沿用它既有的 spec.replicas / spec.template.spec.containers[].resources.requests 介面。
    # 注意：total_cpu_mc/total_mem_b 為 0（沒有部署，或部署都沒填資源）時不能真的把 0
    # 傳進去——_estimate_monthly_cost() 是設計給「單一容器」用的，遇到沒填的欄位會用
    # `or 100`/`or 128Mi` 這種預設值頂上去（假設單一 Pod 一定會吃掉一點資源），但 0 在
    # Python 是 falsy，會被那個 `or` 誤判成「沒填」，讓「總量加總後真的是 0」的正常情況
    # 被套用不該套用的預設值，估出一個不存在的費用。
    if total_cpu_mc == 0 and total_mem_b == 0:
        monthly_usd = 0.0
    else:
        pseudo_manifest = {"spec": {"replicas": 1, "template": {"spec": {"containers": [
            {"resources": {"requests": {"cpu": f"{total_cpu_mc}m", "memory": str(total_mem_b)}}}
        ]}}}}
        monthly_usd = _estimate_monthly_cost(pseudo_manifest).get("estimated_usd")
    return {
        "deployments": deployments,
        "total_cpu_cores": round(total_cpu_mc / 1000, 2),
        "total_mem_gib": round(total_mem_b / (1024 ** 3), 2),
        "monthly_usd": monthly_usd,
    }


def _dashboard_summary(namespace: str) -> dict:
    """監控台頁面的完整資料：叢集空間總覽（K8s requests 加總跟 Prometheus 實際用量
    取較大值，比照 Kubecost 的 allocation = max(usage, requests) 概念）+ 使用者自己
    的部署明細 + 費用估算。任何一段查不到都優雅降級，不讓整頁掛掉（AGENT_RULES.md
    新手友善原則）。2026-09-29 從隊友分支移植。"""
    result = {
        "k8s_enabled": K8S_ENABLED,
        "prometheus_up": False,
        "cluster_capacity": None,
        "cluster_used": None,
        "cluster_used_source": None,
        "cluster_remaining": None,
        "my_deployments": [],
        "my_total_cpu_cores": None,
        "my_total_mem_gib": None,
        "my_monthly_usd": None,
        "cpu_trend": [],
        "mem_trend": [],
    }
    prom_url = os.environ.get("PROMETHEUS_URL", "http://127.0.0.1:9090").replace("localhost", "127.0.0.1")
    if K8S_ENABLED:
        # 2026-10-03：使用者看完 Prometheus 自己的折線圖介面後要求「這種折線圖也要
        # 出現在我自己的系統裡」——用 query_range()（observability/prometheus_client.py
        # 早就寫好、但從沒被呼叫過的函式）撈使用者自己 namespace 過去 30 分鐘的 CPU/
        # 記憶體實際用量，給前端畫成跟 Healer 頁一樣風格的 inline SVG 折線圖。獨立一個
        # try 區塊，查不到（Prometheus 沒連上/剛啟動資料還不夠）就是空陣列，前端會顯示
        # 「資料不足」而不是整頁掛掉，不影響上面容量總覽那段原本的邏輯。
        try:
            from observability.prometheus_client import PrometheusClient
            trend_client = PrometheusClient(prom_url, timeout=3)
            if trend_client.is_alive():
                cpu_range = trend_client.query_range(
                    f'sum(rate(container_cpu_usage_seconds_total{{namespace="{namespace}"}}[5m]))',
                    start_mins=30, step_secs=60)
                if cpu_range and cpu_range[0].get("values"):
                    result["cpu_trend"] = [
                        {"t": datetime.fromtimestamp(ts).isoformat(), "v": round(v, 3)}
                        for ts, v in cpu_range[0]["values"]
                    ]
                mem_range = trend_client.query_range(
                    f'sum(container_memory_working_set_bytes{{namespace="{namespace}"}})',
                    start_mins=30, step_secs=60)
                if mem_range and mem_range[0].get("values"):
                    result["mem_trend"] = [
                        {"t": datetime.fromtimestamp(ts).isoformat(), "v": round(v / (1024 ** 2), 1)}
                        for ts, v in mem_range[0]["values"]
                    ]
        except Exception:
            pass

        try:
            cap = k8s_get_node_capacity()
            if cap:
                from agents.cost_agent import _parse_cpu_millicores, _parse_memory_bytes
                node_cpu_mc = _parse_cpu_millicores(cap.get("cpu")) or 0
                node_mem_b = _parse_memory_bytes(cap.get("memory")) or 0
                req_cpu_mc, req_mem_b = _sum_cluster_resource_requests()

                used_cpu_mc, used_source_cpu = req_cpu_mc, "requests"
                used_mem_b, used_source_mem = req_mem_b, "requests"

                try:
                    from observability.prometheus_client import PrometheusClient
                    client = PrometheusClient(prom_url, timeout=2)
                    prometheus_up = client.is_alive()
                    result["prometheus_up"] = prometheus_up
                    if prometheus_up:
                        try:
                            actual_cpu_cores = client.cluster_cpu_usage_cores()
                            if actual_cpu_cores is not None:
                                actual_cpu_mc = actual_cpu_cores * 1000
                                if actual_cpu_mc > used_cpu_mc:
                                    used_cpu_mc, used_source_cpu = actual_cpu_mc, "usage"
                        except Exception:
                            pass
                        try:
                            actual_mem_b = client.cluster_memory_usage_bytes()
                            if actual_mem_b is not None and actual_mem_b > used_mem_b:
                                used_mem_b, used_source_mem = actual_mem_b, "usage"
                        except Exception:
                            pass
                except Exception:
                    pass

                result["cluster_capacity"] = {
                    "cpu_cores": round(node_cpu_mc / 1000, 2),
                    "mem_gib": round(node_mem_b / (1024 ** 3), 2),
                }
                result["cluster_used"] = {
                    "cpu_cores": round(used_cpu_mc / 1000, 2),
                    "mem_gib": round(used_mem_b / (1024 ** 3), 2),
                }
                result["cluster_used_source"] = {"cpu": used_source_cpu, "mem": used_source_mem}
                result["cluster_remaining"] = {
                    "cpu_cores": round(node_cpu_mc / 1000 - used_cpu_mc / 1000, 2),
                    "mem_gib": round(node_mem_b / (1024 ** 3) - used_mem_b / (1024 ** 3), 2),
                }
        except Exception:
            pass

        try:
            my = _my_deployment_resource_breakdown(namespace)
            result["my_deployments"] = my["deployments"]
            result["my_total_cpu_cores"] = my["total_cpu_cores"]
            result["my_total_mem_gib"] = my["total_mem_gib"]
            result["my_monthly_usd"] = my["monthly_usd"]
        except Exception:
            pass
    return result


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
        # 休眠模式的喚醒器與分時擴縮的設定：同名的新服務不能繼承舊的排程或例外
        try:
            api.delete_namespaced_deployment(("zt-waker-" + app_name)[:63].rstrip("-"), namespace)
        except Exception:
            pass
        try:
            from core import autoscale_store
            autoscale_store.forget_app(namespace, app_name)
        except Exception:
            pass
        # docker-compose 匯入建立的同名連線入口與密碼 Secret 也要一起刪（Secret 裡有密碼，不能留著）。
        # 連線入口只刪平台自己建的（有標籤），使用者自己同名的 Service 不動。
        try:
            alias = core.read_namespaced_service(app_name, namespace)
            if (alias.metadata.labels or {}).get("zerotouch/alias-of") == app_name:
                core.delete_namespaced_service(app_name, namespace)
        except Exception:
            pass
        try:
            from gitops.manifest_writer import secret_name
            core.delete_namespaced_secret(secret_name(app_name), namespace)
        except Exception:
            pass
        # 保存空間（compose 的具名 volume）：刪除確認卡已經說明資料會一起刪除
        removed_data = 0
        try:
            for pvc in core.list_namespaced_persistent_volume_claim(
                    namespace, label_selector=f"zerotouch/app={app_name}").items:
                core.delete_namespaced_persistent_volume_claim(pvc.metadata.name, namespace)
                removed_data += 1
        except Exception:
            pass
        return True, f"已刪除 {app_name}" + ("，連同它保存的資料" if removed_data else "")
    except Exception as e:
        return False, str(e)


def _build_agent_manifest(parsed):
    app_name = parsed.get("app_name", "auto-app")
    image = parsed.get("image", "nginx:latest")
    pods = int(parsed.get("pods", parsed.get("replicas", 1)))
    port = int(parsed.get("port", 80))
    memory = parsed.get("memory")
    cpu = parsed.get("cpu")
    # 審查看的必須是實際會部署的那一份：資源預設值與安全設定跟 k8s_deploy／Git 設定相同
    from agents.cost_agent import recommended_resources, recommended_env, recommended_probes, container_port
    cport = container_port(image, port, parsed.get("container_port"))
    container = {
        "name": app_name,
        "image": image,
        "ports": [{"containerPort": cport}],
        "resources": recommended_resources(image, app_name, memory, cpu),
        "securityContext": {"allowPrivilegeEscalation": False},
        **recommended_probes(image, cport),
    }
    env = {**recommended_env(image), **(parsed.get("env") or {})}
    if env:
        from gitops.manifest_writer import env_entries
        container["env"] = env_entries(app_name, env, parsed.get("secret_keys"))
    from gitops.manifest_writer import pod_volumes
    _pod_vols, _mounts = pod_volumes(app_name, parsed.get("volumes"))
    if _mounts:
        container["volumeMounts"] = _mounts
    for key in ("command", "args"):
        if parsed.get(key):
            container[key] = list(parsed[key])
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": app_name, "namespace": NS},
        "spec": {
            "replicas": pods,
            "selector": {"matchLabels": {"app": app_name}},
            "template": {
                "metadata": {"labels": {"app": app_name}},
                "spec": {"containers": [container], **({"volumes": _pod_vols} if _pod_vols else {})},
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
                f"或副本數），不會建立新的服務。如果你是想建一個不一樣的新服務，請換一個名字。"
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
                    f"建議換一個 port。"
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
<title>ZeroTouch</title>
<link rel="manifest" href="/manifest.webmanifest">
<meta name="theme-color" content="#0B1622">
<link rel="icon" href="/favicon.ico" sizes="any">
<link rel="apple-touch-icon" href="/icons/apple-touch-180.png">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="ZeroTouch">
<meta name="apple-mobile-web-app-status-bar-style" content="black">
<script src="/static/push-client.js" defer></script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Oxanium:wght@500;600;700&family=Noto+Sans+TC:wght@400;500;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<script>
// 主題在第一次繪製前就決定，避免先閃一下深色再變淺色
(function(){try{var t=localStorage.getItem('zt_theme');if(t==='light'||t==='dark')document.documentElement.dataset.theme=t;}catch(e){}})();
</script>
<style>
/* ══════════════════════════════════════════════════════════════════
   ZeroTouch 介面設計系統（2026-10-09 改版）
   方向：艦橋主控台。深海藍底＋細格線，系統資料是帶四角刻度的儀表模組；
   唯一品牌色是儀表訊號藍，綠／黃／紅只用來表示通過、提醒、擋下。
   深色為預設，淺色由系統設定或右上角切換決定。
   舊的變數名稱（--green、--surface…）保留成別名，行內樣式不用逐一改。
   ══════════════════════════════════════════════════════════════════ */
:root{
  --void:#0B1622; --panel:#0F2030; --panel-2:#142A3C; --panel-3:#1A3449;
  --grid:#1D3A50; --grid-2:#28506B; --grid-line:rgba(70,130,170,.10);
  --text:#DCE8F0; --text-2:#93AABB; --text-3:#62798A;
  --signal:#5CC8FF; --signal-soft:rgba(92,200,255,.12); --signal-line:rgba(92,200,255,.45);
  --pass:#3DD68C; --pass-soft:rgba(61,214,140,.13);
  --caution:#F5B547; --caution-soft:rgba(245,181,71,.14);
  --stop:#FF6B5B; --stop-soft:rgba(255,107,91,.14);
  --on-signal:#04111C; --scrim:rgba(3,10,16,.62); --shadow-pop:0 18px 50px rgba(0,0,0,.45);
  --f-display:"Oxanium","Noto Sans TC",system-ui,sans-serif;
  --f-body:"Noto Sans TC","Microsoft JhengHei",system-ui,sans-serif;
  --f-id:"JetBrains Mono",Consolas,monospace;
  --radius:6px; --radius-sm:5px;
  /* 舊名稱別名 */
  --bg:var(--panel-2); --surface:var(--panel); --surface2:var(--panel-2); --border:var(--grid); --border2:var(--grid-2);
  --text2:var(--text-2); --text3:var(--text-3);
  --green:var(--signal); --green-light:var(--signal-soft); --green-mid:var(--signal-line);
  --red:var(--stop); --red-light:var(--stop-soft); --yellow:var(--caution); --yellow-light:var(--caution-soft);
  --blue:var(--signal); --blue-light:var(--signal-soft);
  --shadow:none; --shadow-md:var(--shadow-pop);
  color-scheme:dark;
}
@media (prefers-color-scheme: light){ :root:not([data-theme="dark"]){
  --void:#E8EFF5; --panel:#FFFFFF; --panel-2:#F2F6FA; --panel-3:#E6EEF5;
  --grid:#CFDCE6; --grid-2:#B5C7D5; --grid-line:rgba(30,90,140,.07);
  --text:#0E1E2B; --text-2:#475D6D; --text-3:#7A8E9C;
  --signal:#0A6DB3; --signal-soft:rgba(10,109,179,.09); --signal-line:rgba(10,109,179,.4);
  --pass:#1C8A57; --pass-soft:rgba(28,138,87,.1); --caution:#A66A05; --caution-soft:rgba(166,106,5,.1);
  --stop:#C23A2B; --stop-soft:rgba(194,58,43,.09); --on-signal:#FFFFFF; --scrim:rgba(14,30,43,.38);
  --shadow-pop:0 18px 50px rgba(14,30,43,.18); color-scheme:light } }
:root[data-theme="light"]{
  --void:#E8EFF5; --panel:#FFFFFF; --panel-2:#F2F6FA; --panel-3:#E6EEF5;
  --grid:#CFDCE6; --grid-2:#B5C7D5; --grid-line:rgba(30,90,140,.07);
  --text:#0E1E2B; --text-2:#475D6D; --text-3:#7A8E9C;
  --signal:#0A6DB3; --signal-soft:rgba(10,109,179,.09); --signal-line:rgba(10,109,179,.4);
  --pass:#1C8A57; --pass-soft:rgba(28,138,87,.1); --caution:#A66A05; --caution-soft:rgba(166,106,5,.1);
  --stop:#C23A2B; --stop-soft:rgba(194,58,43,.09); --on-signal:#FFFFFF; --scrim:rgba(14,30,43,.38);
  --shadow-pop:0 18px 50px rgba(14,30,43,.18); color-scheme:light }

*{margin:0;padding:0;box-sizing:border-box}
html,body{height:100%;overflow:hidden}
body{font-family:var(--f-body);font-size:15px;line-height:1.65;color:var(--text);background-color:var(--void);
  background-image:linear-gradient(var(--grid-line) 1px,transparent 1px),linear-gradient(90deg,var(--grid-line) 1px,transparent 1px);
  background-size:40px 40px;background-position:-1px -1px}
button,input,textarea,select{font:inherit;color:inherit}
button{cursor:pointer}
a{color:var(--signal)}
:focus-visible{outline:2px solid var(--signal);outline-offset:2px}
::selection{background:var(--signal-soft)}
.mono,code{font-family:var(--f-id);font-size:.9em}
.num{font-family:var(--f-display);font-variant-numeric:tabular-nums}
*{scrollbar-width:thin;scrollbar-color:var(--grid-2) transparent}

/* ── 共用：模組框（四角刻度） ── */
.card,.module{position:relative;background:var(--panel);border:1px solid var(--grid);border-radius:4px;padding:20px}
.card::before,.card::after,.module::before,.module::after{content:"";position:absolute;width:10px;height:10px;border-color:var(--signal);border-style:solid;opacity:.75;pointer-events:none}
.card::before,.module::before{left:-1px;top:-1px;border-width:2px 0 0 2px}
.card::after,.module::after{right:-1px;bottom:-1px;border-width:0 2px 2px 0}
.card-title{font-size:13px;font-weight:500;color:var(--text-3);margin-bottom:10px}
.grid-2{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}
.grid-3{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px}
.stat-num{font-family:var(--f-display);font-size:30px;font-weight:600;line-height:1.15;font-variant-numeric:tabular-nums;color:var(--text)}
.stat-label{font-size:13px;color:var(--text-2);margin-top:4px}
.empty{text-align:center;padding:40px 20px;color:var(--text-3)}
.empty p{font-size:14px}

/* ── 按鈕 ── */
.btn-primary,.deploy-btn,.deploy-confirm-btn,.btn-sm,.btn{display:inline-flex;align-items:center;justify-content:center;gap:6px;border:1px solid var(--grid-2);background:var(--panel-2);color:var(--text);border-radius:var(--radius);font-weight:500;font-size:14px;padding:8px 16px;white-space:nowrap;transition:border-color .15s,background .15s,filter .15s,transform .1s}
.btn-primary:hover,.deploy-btn:hover,.deploy-confirm-btn:hover,.btn-sm:hover,.btn:hover{border-color:var(--signal)}
.btn-primary:active,.deploy-btn:active,.deploy-confirm-btn:active,.btn:active{transform:translateY(1px)}
.btn-primary,.deploy-btn,.deploy-confirm-btn.primary,.btn.primary{background:var(--signal);border-color:var(--signal);color:var(--on-signal)}
.btn-primary:hover,.deploy-btn:hover,.deploy-confirm-btn.primary:hover,.btn.primary:hover{filter:brightness(1.08)}
.btn-primary:disabled,.deploy-btn:disabled,.deploy-confirm-btn:disabled,.btn-sm:disabled,.btn:disabled{opacity:.45;cursor:not-allowed;filter:none;transform:none}
.btn-sm{padding:4px 11px;font-size:13px}
.btn-danger,.btn.danger{border-color:color-mix(in srgb,var(--stop) 55%,transparent);color:var(--stop);background:transparent}
.btn-danger:hover,.btn.danger:hover{background:var(--stop-soft);border-color:var(--stop)}
.btn-primary.danger{background:var(--stop);border-color:var(--stop);color:#fff}
.action-btns{display:flex;gap:6px;flex-wrap:wrap}

/* ── 登入／註冊 ── */
.auth-wrap{min-height:100vh;display:grid;place-items:center;padding:24px 16px;overflow-y:auto;height:100%}
.auth-card{width:100%;max-width:400px;padding:34px 32px}
.auth-logo{display:flex;align-items:center;gap:10px;margin-bottom:26px;color:var(--signal)}
.auth-logo span{font-family:var(--f-display);font-weight:700;font-size:21px;letter-spacing:.04em;color:var(--text)}
.auth-title{font-size:22px;font-weight:700;margin-bottom:4px}
.auth-sub{font-size:14px;color:var(--text-2);margin-bottom:24px}
.form-group{margin-bottom:16px}
.form-group label{display:block;font-size:13px;color:var(--text-2);margin-bottom:6px}
.form-group input,.deploy-input,.deploy-confirm-field input,.fgrid input,.inline-form input,textarea{width:100%;background:var(--panel-2);border:1px solid var(--grid-2);border-radius:var(--radius);padding:10px 12px;font-size:14.5px;color:var(--text);outline:none;transition:border-color .15s,box-shadow .15s}
.form-group input:focus,.deploy-input:focus,.deploy-confirm-field input:focus,.fgrid input:focus,.inline-form input:focus,textarea:focus{border-color:var(--signal);box-shadow:0 0 0 3px var(--signal-soft)}
.auth-card .btn-primary{width:100%;padding:11px;margin-top:4px}
.auth-divider{display:flex;align-items:center;gap:10px;margin:20px 0}
.auth-divider .line{flex:1;height:1px;background:var(--grid)}
.auth-divider span{font-size:13px;color:var(--text-3)}
.google-btn{display:flex;align-items:center;justify-content:center;gap:10px;width:100%;padding:10px;border:1px solid var(--grid-2);border-radius:var(--radius);background:var(--panel-2);color:var(--text);font-size:14.5px;text-decoration:none;transition:border-color .15s}
.google-btn:hover{border-color:var(--signal)}
.auth-link{text-align:center;margin-top:20px;font-size:14px;color:var(--text-2)}
.auth-link a{font-weight:500;text-decoration:none}
.auth-error{background:var(--stop-soft);border:1px solid color-mix(in srgb,var(--stop) 50%,transparent);color:var(--text);padding:10px 12px;border-radius:var(--radius);font-size:14px;margin-bottom:16px}

/* ── 外框：左側選單固定，右側內容各自捲動 ── */
.layout{display:flex;height:100vh;overflow:hidden}
.sidebar{width:248px;flex:none;height:100vh;display:flex;flex-direction:column;gap:14px;padding:18px 14px 14px;background:color-mix(in srgb,var(--panel) 92%,transparent);backdrop-filter:blur(6px);border-right:1px solid var(--grid);overflow:hidden}
.sidebar-logo{display:flex;align-items:center;gap:10px;padding:0 6px;color:var(--signal)}
.sidebar-logo span{font-family:var(--f-display);font-weight:700;font-size:20px;letter-spacing:.04em;color:var(--text)}
.new-chat-btn{width:100%;justify-content:center;background:transparent;border:1px dashed var(--signal-line);color:var(--signal);padding:8px 12px;border-radius:var(--radius);display:flex;align-items:center;gap:8px;font-weight:500;transition:background .15s,border-color .15s}
.new-chat-btn:hover{background:var(--signal-soft);border-style:solid}
.sidebar-nav{display:flex;flex-direction:column;gap:2px;flex:1;min-height:0;overflow-y:auto;overflow-x:hidden;margin:0 -6px;padding:0 6px}
.nav-section{font-size:12.5px;color:var(--text-3);padding:14px 10px 4px}
.nav-item{position:relative;display:flex;align-items:center;gap:10px;width:100%;padding:8px 10px;border:0;border-radius:var(--radius);background:transparent;color:var(--text-2);font-size:14.5px;text-align:left;transition:background .15s,color .15s}
.nav-item svg{width:16px;height:16px;flex:none;opacity:.85}
.new-chat-btn svg{width:16px;height:16px;flex:none}
.nav-item:hover{background:var(--panel-2);color:var(--text)}
.nav-item.active{background:var(--signal-soft);color:var(--text)}
.nav-item.active::before{content:"";position:absolute;left:-8px;top:8px;bottom:8px;width:3px;border-radius:0 3px 3px 0;background:var(--signal);box-shadow:0 0 12px var(--signal)}
.nav-item .count{margin-left:auto;font-family:var(--f-display);font-size:13px;color:var(--text-3)}
#chat-room-list{display:flex;flex-direction:column;gap:1px;max-height:220px;overflow-y:auto}
.chat-room-item{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:7px 10px;border-radius:var(--radius);font-size:14px;color:var(--text-2);transition:background .15s,color .15s}
.chat-room-item:hover{background:var(--panel-2);color:var(--text)}
.chat-room-item.active{background:var(--panel-2);color:var(--text)}
.chat-room-title{display:flex;align-items:center;gap:8px;overflow:hidden;flex:1;min-width:0}
.chat-room-title svg{display:none}
.chat-room-title span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.chat-room-delete{opacity:0;color:var(--text-3);padding:0 4px;flex:none;transition:opacity .15s,color .15s}
.chat-room-item:hover .chat-room-delete,.chat-room-item:focus-within .chat-room-delete{opacity:1}
.chat-room-delete:hover{color:var(--stop)}
/* ── 專案（2026-10-21）── */
.proj-section{display:flex;align-items:center;justify-content:space-between}
.proj-add{border:0;background:transparent;color:var(--text-3);font-size:17px;line-height:1;padding:1px 7px;border-radius:4px;cursor:pointer}
.proj-add:hover,.proj-add:focus-visible{color:var(--signal);background:var(--panel-2)}
.chat-room-title .proj-ico{display:inline-block;width:14px;height:14px;flex:none;color:var(--text-3)}
.chat-room-title .proj-badge{display:inline-flex;flex:none;overflow:visible}
.chat-room-item.proj-item{cursor:pointer}
.proj-empty-side{font-size:13px;color:var(--text-3);padding:2px 10px 6px}
.proj-tabs{display:flex;flex-wrap:wrap;gap:4px;border-bottom:1px solid var(--grid);margin-bottom:18px}
.proj-tabs button{border:0;background:transparent;color:var(--text-2);padding:8px 14px;font-size:14.5px;border-bottom:2px solid transparent;margin-bottom:-1px;cursor:pointer;white-space:nowrap}
.proj-tabs button:hover{color:var(--text)}
.proj-tabs button.active{color:var(--text);border-bottom-color:var(--signal);font-weight:500}
.proj-grid{display:grid;grid-template-columns:minmax(0,1.4fr) minmax(0,1fr);gap:18px;align-items:start}
@media (max-width:860px){.proj-grid{grid-template-columns:1fr}}
.proj-box{border:1px solid var(--grid);border-radius:6px;background:var(--panel);padding:16px 18px}
.proj-box h2{font-size:15.5px;font-weight:700;margin:0 0 6px}
.proj-box.danger{border-color:color-mix(in srgb,var(--stop) 45%,transparent)}
.proj-row{display:flex;align-items:center;gap:10px;padding:9px 0;border-bottom:1px solid var(--grid);font-size:14px}
.proj-row:last-child{border-bottom:0}
.proj-row .grow{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.proj-row.click{cursor:pointer}
.proj-row.click:hover .grow{color:var(--signal)}
.proj-muted{color:var(--text-3);font-size:13px}
.proj-box .lbl{display:block;font-size:13.5px;color:var(--text-2);margin:14px 0 6px}
.proj-box textarea{min-height:96px;resize:vertical}
.proj-box input[type=text],.proj-modal input[type=text]{width:100%;background:var(--panel-2);border:1px solid var(--grid-2);border-radius:var(--radius);padding:9px 12px;color:var(--text);font-size:14.5px}
.proj-box input[type=text]:focus,.proj-modal input[type=text]:focus{border-color:var(--signal);outline:none;box-shadow:0 0 0 3px var(--signal-soft)}
.proj-box select{background:var(--panel-2);border:1px solid var(--grid-2);border-radius:var(--radius);padding:7px 10px;color:var(--text);font-size:14px;min-width:0;flex:1}
.proj-actions{display:flex;gap:8px;justify-content:flex-end;align-items:center;margin-top:12px;flex-wrap:wrap}
.proj-msg{font-size:13.5px;margin-right:auto}
.proj-msg.ok{color:var(--pass)}.proj-msg.err{color:var(--stop)}
.proj-modal{padding:22px;max-width:520px}
.proj-modal h3{font-size:16px;font-weight:700;margin:0 0 12px}
.proj-modal .lbl{display:block;font-size:13.5px;color:var(--text-2);margin:12px 0 6px}
.proj-del label{display:flex;gap:8px;align-items:flex-start;padding:5px 0;font-size:14px;cursor:pointer}
.proj-del input[type=checkbox],.proj-del input[type=radio]{margin-top:3px;flex:none;accent-color:var(--signal);width:auto}
.proj-del .q{font-size:14px;font-weight:700;margin:14px 0 4px}
.proj-del .note{display:block;font-size:12.5px;color:var(--text-3)}
.proj-del .warn-t{color:var(--stop);font-size:12.5px}
.proj-warn{font-size:13.5px;background:var(--caution-soft);border:1px solid var(--caution);border-radius:4px;padding:8px 10px;margin-top:10px}
.proj-stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.proj-stat{border:1px solid var(--grid);border-radius:6px;background:var(--panel);padding:14px 16px;display:flex;flex-direction:column;gap:2px}
.proj-stat b{font-size:24px;font-weight:700;font-family:var(--f-id)}
.proj-stat span{font-size:13.5px;color:var(--text-2)}
.proj-stat em{font-style:normal;font-size:12.5px;color:var(--text-3)}
.proj-stat.warn{border-color:var(--caution)}
.proj-stat.warn em{color:var(--caution)}
.proj-err{font-size:13.5px;background:var(--stop-soft);border:1px solid color-mix(in srgb,var(--stop) 50%,transparent);border-radius:4px;padding:8px 10px;margin-top:10px}
/* 分時擴縮：「我的服務」頁的閒置天數、設定排程彈窗（2026-10-21） */
.as-idle{display:inline-flex;align-items:center;gap:6px;font-size:13.5px;color:var(--text-2)}
.as-idle select{background:var(--panel-2);border:1px solid var(--grid-2);border-radius:var(--radius);padding:3px 6px;color:var(--text);font-size:13.5px}
.as-ed-ex{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
#as-ed-plan .zt-w{margin-top:12px}
.proj-modal:has(#as-ed-plan){max-width:640px}
.sys-panel{border:1px solid var(--grid);border-radius:8px;background:var(--panel);padding:10px 12px;display:flex;flex-direction:column;gap:7px;font-size:13.5px}
.status-pill{display:flex;align-items:center;gap:8px;min-width:0}
.status-pill .val{margin-left:auto;color:var(--text-3);font-size:13px;white-space:nowrap}
.dot{width:8px;height:8px;border-radius:50%;background:var(--text-3);flex:none}
.dot.green{background:var(--pass);box-shadow:0 0 8px var(--pass)}
.dot.red{background:var(--stop);box-shadow:0 0 8px var(--stop)}
.dot.yellow{background:var(--caution);box-shadow:0 0 8px var(--caution);animation:lamp 1.4s ease-in-out infinite}
@keyframes lamp{50%{opacity:.35}}
.sidebar-footer{border-top:1px solid var(--grid);padding:10px 4px 0}
.user-info{display:flex;align-items:center;gap:10px;min-width:0}
.user-avatar{width:30px;height:30px;flex:none;border-radius:7px;border:1px solid var(--signal);background:var(--signal-soft);color:var(--signal);display:grid;place-items:center;font-family:var(--f-display);font-weight:600}
.user-name{flex:1;min-width:0;font-size:14px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.admin-badge{font-size:12px;color:var(--signal);border:1px solid var(--signal-line);border-radius:4px;padding:0 6px;margin-left:6px}
.logout-btn{background:none;border:0;color:var(--text-3);font-size:13px;padding:2px 6px;border-radius:4px}
.logout-btn:hover{color:var(--stop)}

.main{flex:1;min-width:0;height:100vh;display:flex;flex-direction:column;overflow:hidden}
.status-bar{height:52px;flex:none;display:flex;align-items:center;gap:14px;padding:0 28px;border-bottom:1px solid var(--grid);background:color-mix(in srgb,var(--void) 82%,transparent);backdrop-filter:blur(6px)}
.topbar-title{font-size:16px;font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.topbar-tools{margin-left:auto;display:flex;align-items:center;gap:8px}
#clock{font-family:var(--f-display);font-size:13.5px;color:var(--text-3);font-variant-numeric:tabular-nums;margin-right:4px}
.icon-btn{display:inline-flex;align-items:center;justify-content:center;gap:6px;height:32px;padding:0 11px;border:1px solid var(--grid-2);border-radius:var(--radius);background:var(--panel);color:var(--text-2);font-size:13.5px;transition:border-color .15s,color .15s}
.icon-btn:hover{border-color:var(--signal);color:var(--text)}

/* 整條警告橫幅：新手一定看得到，而且寫清楚怎麼做 */
.alert-banner{display:none;gap:12px;align-items:flex-start;padding:12px 28px;border-bottom:1px solid;font-size:14px;flex:none}
.alert-banner.show{display:flex}
.alert-banner.stop{background:var(--stop-soft);border-color:var(--stop)}
.alert-banner.caution{background:var(--caution-soft);border-color:var(--caution)}
.alert-banner .alert-mark{width:4px;align-self:stretch;border-radius:2px;flex:none}
.alert-banner.stop .alert-mark{background:var(--stop)}
.alert-banner.caution .alert-mark{background:var(--caution)}
.alert-banner .alert-title{font-weight:700}
.alert-banner.stop .alert-title{color:var(--stop)}
.alert-banner.caution .alert-title{color:var(--caution)}
.alert-banner .alert-body{color:var(--text-2);margin-top:2px}
.alert-banner code{background:var(--panel);border:1px solid var(--grid);border-radius:4px;padding:1px 6px}

.page{display:none;flex:1;min-height:0;overflow-y:auto;padding:28px 32px 48px}
.page.active{display:block;animation:pageIn .22s ease-out}
@keyframes pageIn{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}
.page > *{max-width:1280px;margin-left:auto;margin-right:auto}
.page-title{font-size:24px;font-weight:700;margin-bottom:4px}
.page-sub{font-size:14.5px;color:var(--text-2);margin-bottom:22px}

/* ── 表格 ── */
.table-wrap{overflow-x:auto;border:1px solid var(--grid);border-radius:4px;background:var(--panel)}
table{width:100%;border-collapse:collapse;font-size:14px}
th{text-align:left;padding:10px 16px;font-size:13px;font-weight:500;color:var(--text-3);border-bottom:1px solid var(--grid);white-space:nowrap;background:var(--panel-2)}
td{padding:11px 16px;border-bottom:1px solid color-mix(in srgb,var(--grid) 65%,transparent);vertical-align:middle}
tr:last-child td{border-bottom:0}
tbody tr{transition:background .12s}
tbody tr:hover td{background:var(--signal-soft)}
.badge,.pill,.status-tag,.cond-badge{display:inline-flex;align-items:center;gap:6px;padding:2px 9px;border-radius:999px;font-size:13px;font-weight:500;border:1px solid transparent;white-space:nowrap}
.badge::before{content:"";width:6px;height:6px;border-radius:50%;background:currentColor}
.badge.running,.pill.simple,.pill.true,.status-tag.ok,.cond-true{background:var(--pass-soft);color:var(--pass)}
.badge.pending,.pill.medium{background:var(--caution-soft);color:var(--caution)}
.badge.failed,.pill.complex,.pill.false,.status-tag.reject,.cond-false{background:var(--stop-soft);color:var(--stop)}
.badge.unknown,.pill.en,.pill.zhtw{background:var(--panel-2);color:var(--text-2);border-color:var(--grid)}
.badge.running::before{animation:beat 2.4s ease-out infinite}
@keyframes beat{0%{box-shadow:0 0 0 0 color-mix(in srgb,currentColor 60%,transparent)}70%,100%{box-shadow:0 0 0 6px transparent}}
.pager{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:12px 2px 0;font-size:13.5px;color:var(--text-3)}

/* ── 彈窗、抽屜、提示 ── */
.modal-bg,#manual-overlay,#pod-detail-overlay,#confirm-dialog-overlay{position:fixed;inset:0;background:var(--scrim);z-index:200;align-items:center;justify-content:center;padding:24px}
.modal-bg{display:none}.modal-bg.open{display:flex}
#manual-overlay,#pod-detail-overlay{display:none}
.modal,.manual-panel,.confirm-dialog{position:relative;background:var(--panel);border:1px solid var(--grid-2);border-radius:6px;box-shadow:var(--shadow-pop);width:100%;animation:popIn .2s ease-out}
@keyframes popIn{from{opacity:0;transform:translateY(8px) scale(.985)}to{opacity:1;transform:none}}
.modal{max-width:620px;max-height:85vh;overflow-y:auto}
.modal-header,.manual-panel-head{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;padding:18px 22px;border-bottom:1px solid var(--grid);position:sticky;top:0;background:var(--panel);z-index:1}
.modal-title,.manual-panel-title{font-size:16px;font-weight:700}
.manual-panel-sub{font-size:13px;color:var(--text-3);margin-top:3px}
.modal-close,.manual-close{flex:none;border:1px solid var(--grid-2);background:transparent;color:var(--text-2);border-radius:var(--radius);padding:2px 10px;font-size:13px}
.modal-close:hover,.manual-close:hover{border-color:var(--signal);color:var(--text)}
.modal-body{padding:18px 22px}
.manual-panel{max-width:700px;max-height:min(740px,88vh);display:flex;flex-direction:column}
.manual-panel-body{overflow-y:auto;padding:8px 22px 22px}
.manual-panel-body details{border:1px solid var(--grid);border-radius:4px;margin-top:10px;overflow:hidden}
.manual-panel-body summary{cursor:pointer;padding:12px 16px;font-weight:500;list-style:none;background:var(--panel-2);display:flex;gap:8px;align-items:center}
.manual-panel-body summary::-webkit-details-marker{display:none}
.manual-panel-body summary::before{content:"";width:7px;height:7px;border-right:2px solid var(--signal);border-bottom:2px solid var(--signal);transform:rotate(-45deg);transition:transform .15s;flex:none}
.manual-panel-body details[open] summary::before{transform:rotate(45deg)}
.manual-section{padding:14px 16px;font-size:14px;color:var(--text-2)}
.manual-section code{background:var(--panel-2);border:1px solid var(--grid);padding:1px 6px;border-radius:4px;color:var(--signal)}
.term{border-bottom:1px dotted var(--signal);color:var(--signal);cursor:pointer}
#term-tip{position:fixed;max-width:280px;background:var(--panel-3);color:var(--text);border:1px solid var(--signal-line);font-size:13.5px;line-height:1.6;padding:10px 12px;border-radius:6px;box-shadow:var(--shadow-pop);z-index:250}
/* 滑鼠停留提示（data-tip），外觀跟 #term-tip 一致，顏色跟著深淺色主題 */
#zt-tip{position:fixed;left:0;top:0;max-width:240px;background:var(--panel-3);color:var(--text);border:1px solid var(--signal-line);font-size:13px;line-height:1.5;padding:5px 10px;border-radius:6px;box-shadow:var(--shadow-pop);z-index:400;pointer-events:none;width:max-content;overflow-wrap:anywhere;opacity:0;transform:translateY(3px);transition:opacity .12s ease,transform .12s ease}
#zt-tip.on{opacity:1;transform:none}
.confirm-dialog{max-width:440px;padding:22px}
.confirm-dialog-title{font-size:16px;font-weight:700;margin-bottom:8px}
.confirm-dialog-body{font-size:14px;color:var(--text-2)}
.confirm-dialog-actions{display:flex;justify-content:flex-end;gap:8px;margin-top:18px}
.detail-section,.pod-detail-section{margin-bottom:18px}
.detail-section-title,.pod-detail-section-title{font-size:13px;font-weight:500;color:var(--text-3);margin-bottom:8px}
.detail-row{display:flex;justify-content:space-between;gap:16px;padding:8px 0;border-bottom:1px solid color-mix(in srgb,var(--grid) 65%,transparent);font-size:14px}
.detail-row:last-child{border-bottom:0}
.detail-key{color:var(--text-2)}
.detail-val{font-family:var(--f-id);font-size:13px;text-align:right;max-width:340px;overflow-wrap:anywhere}
.pod-chart-empty{font-size:13px;color:var(--text-3);padding:16px;text-align:center;border:1px dashed var(--grid-2);border-radius:4px}
.pod-row{display:flex;align-items:center;gap:10px;padding:9px 12px;border-radius:var(--radius);cursor:pointer;white-space:nowrap;overflow:hidden;border:1px solid transparent;transition:background .12s,border-color .12s}
.pod-row:hover{background:var(--signal-soft);border-color:var(--signal-line)}
.pod-name{flex:1 1 auto;overflow:hidden;text-overflow:ellipsis;font-family:var(--f-id);font-size:13.5px}
.pod-dot{flex:none;width:8px;height:8px;border-radius:50%}
.pod-meta{flex:none;font-size:13px;color:var(--text-3)}
.pod-meta.pod-meta-age{display:none}
@media (min-width:560px){.pod-meta.pod-meta-age{display:inline}}

/* ── 結果框、思考中 ── */
.result-box{display:none;padding:14px 16px;border-radius:4px;font-size:14px;margin-bottom:16px;white-space:pre-wrap;border:1px solid}
.result-box.success{background:var(--pass-soft);border-color:color-mix(in srgb,var(--pass) 50%,transparent)}
.result-box.error{background:var(--stop-soft);border-color:color-mix(in srgb,var(--stop) 50%,transparent)}
.result-box.warn{background:var(--caution-soft);border-color:color-mix(in srgb,var(--caution) 50%,transparent)}
.spinner{width:16px;height:16px;flex:none;border:2px solid var(--grid-2);border-top-color:var(--signal);border-radius:50%;animation:spin .7s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.typing{display:flex;gap:4px;padding:12px 16px;width:fit-content}
.typing span{width:6px;height:6px;background:var(--signal);border-radius:50%;animation:bounce .9s infinite}
.typing span:nth-child(2){animation-delay:.15s}.typing span:nth-child(3){animation-delay:.3s}
@keyframes bounce{50%{transform:translateY(-5px);opacity:.5}}
.think-row{display:flex;align-items:center;gap:10px;padding:10px 14px;width:fit-content;font-size:14px;color:var(--text-2);border:1px solid var(--grid);border-radius:var(--radius);background:var(--panel);position:relative;overflow:hidden}
.think-row::after{content:"";position:absolute;inset:0;background:linear-gradient(90deg,transparent,var(--signal-soft),transparent);transform:translateX(-100%);animation:sweep 1.6s ease-in-out infinite}
@keyframes sweep{to{transform:translateX(100%)}}
.think-dots span{animation:blink 1.4s infinite both}
.think-dots span:nth-child(2){animation-delay:.2s}.think-dots span:nth-child(3){animation-delay:.4s}
@keyframes blink{0%,80%,100%{opacity:0}40%{opacity:1}}
.loading-overlay{position:fixed;inset:0;background:var(--void);display:flex;flex-direction:column;align-items:center;justify-content:center;gap:16px;z-index:9999}

/* ── 對話 ── */
#page-chat{padding:0;overflow:hidden}
#page-chat > *{max-width:none}
.chat-shell{height:100%;display:flex;flex-direction:column;min-height:0}
.chat-topbar{display:none}
/* 對話欄自己有底色，把背景格線擋住；兩側留白露出格線 */
.chat-messages{flex:1;min-height:0;overflow-y:auto;overscroll-behavior:contain;padding:28px 24px 24px;
  background:linear-gradient(90deg,transparent calc(50% - 620px),color-mix(in srgb,var(--grid) 70%,transparent) calc(50% - 620px),var(--void) calc(50% - 619px),var(--void) calc(50% + 619px),color-mix(in srgb,var(--grid) 70%,transparent) calc(50% + 619px),transparent calc(50% + 620px))}
@media (max-width:1288px){.chat-messages{background:var(--void)}}
.msg{display:flex;gap:12px;max-width:1160px;margin:0 auto 20px}
.msg.user{flex-direction:row-reverse}
.msg-avatar{display:none}
.msg-bubble{max-width:100%;font-size:15px;line-height:1.7;min-width:0}
.msg.user .msg-bubble{max-width:78%;background:var(--signal-soft);border:1px solid var(--signal-line);padding:9px 15px;border-radius:12px 12px 3px 12px}
.msg.ai .msg-bubble{color:var(--text)}
.msg.ai .msg-bubble.enter,.msg.enter .msg-bubble{animation:msgIn .28s ease-out}
@keyframes msgIn{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
.msg-bubble pre{background:var(--panel-2);border:1px solid var(--grid);border-radius:4px;padding:12px 14px;overflow-x:auto;font-family:var(--f-id);font-size:13px;margin:8px 0}
.chat-empty{min-height:100%;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;padding:40px 16px}
.chat-empty-logo{width:56px;height:56px;color:var(--signal);margin-bottom:18px;display:grid;place-items:center}
.chat-empty-logo svg{width:56px;height:56px;animation:helm 18s linear infinite}
@keyframes helm{to{transform:rotate(360deg)}}
.chat-empty h1{font-size:28px;font-weight:700;margin-bottom:8px;text-wrap:balance}
.chat-empty p{font-size:15px;color:var(--text-2);max-width:56ch;margin-bottom:26px}
.prompt-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;width:100%;max-width:720px}
.prompt-card{text-align:left;background:var(--panel);border:1px solid var(--grid);border-radius:4px;padding:14px 16px;transition:border-color .15s,transform .15s,background .15s}
.prompt-card:hover{border-color:var(--signal);background:var(--panel-2);transform:translateY(-2px)}
.prompt-card strong{display:block;font-size:15px;font-weight:500;color:var(--text)}
.prompt-card span{display:block;font-size:13.5px;color:var(--text-3);margin-top:4px;font-family:var(--f-id)}
.chat-composer{flex:none;padding:12px 24px 18px;background:var(--void);border-top:1px solid var(--grid)}
.composer-box{max-width:1160px;margin:0 auto;display:flex;align-items:flex-end;gap:10px;background:var(--panel);border:1px solid var(--grid-2);border-radius:10px;padding:7px 7px 7px 16px;transition:border-color .2s,box-shadow .2s}
.composer-box:focus-within{border-color:var(--signal);box-shadow:0 0 0 3px var(--signal-soft)}
.chat-input{flex:1;min-height:40px;max-height:180px;resize:none;border:0;outline:none;background:transparent;font-size:15px;line-height:1.5;padding:8px 2px;box-shadow:none}
.chat-input:focus{box-shadow:none}
.chat-send{flex:none;height:40px;padding:0 18px;border:0;border-radius:7px;background:var(--signal);color:var(--on-signal);font-weight:500;transition:filter .15s}
.chat-send:hover{filter:brightness(1.08)}
/* 語音輸入：聆聽時按鈕外圈擴散、旁邊的音量條跳動 */
.chat-mic{flex:none;position:relative;width:40px;height:40px;border:1px solid var(--grid-2);border-radius:7px;background:var(--panel-2);color:var(--text-2);display:grid;place-items:center;transition:color .15s,border-color .15s,background .15s}
.chat-mic svg{width:18px;height:18px}
.chat-mic:hover{color:var(--signal);border-color:var(--signal-line)}
.chat-mic.listening{color:#fff;background:var(--stop);border-color:var(--stop)}
.chat-mic.listening::before,.chat-mic.listening::after{content:"";position:absolute;inset:-1px;border-radius:8px;border:2px solid var(--stop);animation:micRing 1.6s ease-out infinite;pointer-events:none}
.chat-mic.listening::after{animation-delay:.8s}
@keyframes micRing{from{opacity:.7;transform:scale(1)}to{opacity:0;transform:scale(1.55)}}
.mic-meter{display:none;align-items:center;gap:3px;height:40px;flex:none}
.mic-meter.on{display:flex}
.mic-meter i{width:3px;height:8px;border-radius:2px;background:var(--stop);animation:micBar .9s ease-in-out infinite}
.mic-meter i:nth-child(2){animation-delay:.15s}.mic-meter i:nth-child(3){animation-delay:.3s}.mic-meter i:nth-child(4){animation-delay:.45s}
@keyframes micBar{50%{height:22px}}
.composer-hint.voice{color:var(--text-2)}
.composer-hint.voice-err{color:var(--caution)}
@media (prefers-reduced-motion:reduce){.chat-mic.listening::before,.chat-mic.listening::after,.mic-meter i{animation:none}}


/* ── docker-compose 匯入 ── */
.chat-attach{flex:none;width:40px;height:40px;border:1px solid var(--grid-2);border-radius:7px;background:var(--panel-2);color:var(--text-2);display:grid;place-items:center;transition:color .15s,border-color .15s}
.chat-attach svg{width:18px;height:18px}
.chat-attach:hover{color:var(--signal);border-color:var(--signal-line)}
.cp-order{display:flex;flex-wrap:wrap;align-items:center;gap:0;margin:2px 0 14px;counter-reset:cp}
.cp-order li{list-style:none;display:flex;align-items:center;gap:8px;padding:6px 12px;border:1px solid var(--grid-2);border-radius:20px;background:var(--panel-2);font-family:var(--f-id);font-size:13px;color:var(--text);animation:popIn .35s both;animation-delay:calc(var(--i,0)*90ms)}
.cp-order li::before{counter-increment:cp;content:counter(cp);display:grid;place-items:center;width:18px;height:18px;border-radius:50%;background:var(--signal);color:var(--on-signal);font-size:11px}
.cp-order li.blocked{opacity:.55;border-style:dashed}
.cp-order li.blocked::before{background:var(--stop)}
.cp-order li+li{margin-left:26px;position:relative}
.cp-order li+li::after{content:"";position:absolute;left:-23px;top:50%;width:20px;height:1px;background:var(--grid-2)}
.cp-svc{border:1px solid var(--grid);border-radius:6px;background:var(--panel-2);margin-bottom:10px;overflow:hidden;animation:rowIn .35s both;animation-delay:calc(var(--i,0)*70ms)}
.cp-svc>summary{display:grid;grid-template-columns:minmax(0,1.1fr) minmax(0,1.6fr) minmax(0,1fr) 90px auto;gap:12px;align-items:center;padding:10px 14px;cursor:pointer;list-style:none;font-size:13.5px;color:var(--text-2)}
.cp-svc>summary::-webkit-details-marker{display:none}
.cp-svc>summary:hover{background:var(--panel-3)}
.cp-svc .nm{font-family:var(--f-id);color:var(--text);font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.cp-svc .im{font-family:var(--f-id);font-size:12.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.cp-svc .pt b{font-family:var(--f-id);color:var(--text);font-weight:500}
.cp-svc .rp,.cp-svc .pt{white-space:nowrap}
.cp-pill{justify-self:end;font-size:12px;padding:2px 9px;border-radius:10px;border:1px solid var(--grid-2);color:var(--text-3);white-space:nowrap}
.cp-pill.ok{color:var(--pass);border-color:color-mix(in srgb,var(--pass) 45%,transparent)}
.cp-pill.warn{color:var(--caution);border-color:color-mix(in srgb,var(--caution) 50%,transparent)}
.cp-pill.stop{color:var(--stop);border-color:color-mix(in srgb,var(--stop) 50%,transparent)}
.cp-pill.run{color:var(--signal);border-color:var(--signal-line)}
.cp-body{padding:4px 14px 14px;border-top:1px solid var(--grid)}
.cp-kv{display:flex;flex-wrap:wrap;gap:6px 18px;margin:10px 0;font-size:13px;color:var(--text-3)}
.cp-kv b{font-family:var(--f-id);font-weight:500;color:var(--text-2)}
.cp-env{font-family:var(--f-id);font-size:12.5px;color:var(--text-2);background:var(--panel);border:1px solid var(--grid);border-radius:5px;padding:8px 10px;margin:0 0 10px;max-height:150px;overflow:auto}
.cp-env span{display:block;white-space:nowrap}
.cp-probs{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:6px}
.cp-probs li{position:relative;padding-left:16px;font-size:13.5px;line-height:1.6;color:var(--text-2)}
.cp-probs li::before{content:"";position:absolute;left:1px;top:.62em;width:7px;height:7px;border-radius:50%;background:var(--text-3)}
.cp-probs li.warn::before{background:var(--caution)}
.cp-probs li.block::before{background:var(--stop)}
.cp-verdicts{display:flex;gap:6px;justify-self:end}
.cp-review{margin-top:10px;display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}
.cp-prog{height:3px;background:var(--panel-3);border-radius:2px;overflow:hidden;margin:4px 0 12px}
.cp-prog i{display:block;height:100%;background:var(--signal);transition:width .4s}
@media (max-width:760px){.cp-svc>summary{grid-template-columns:minmax(0,1fr) auto}.cp-svc .im,.cp-svc .pt,.cp-svc .rp{display:none}.cp-review{grid-template-columns:1fr}}

/* ── 分析報告 ── */
.rp{display:flex;flex-direction:column;gap:18px;padding:16px 18px 18px}
.rp-period{display:flex;gap:6px;flex-wrap:wrap}
.rp-period .chip.on{background:var(--signal);color:var(--on-signal);border-color:var(--signal)}
.rp-kpis{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px}
.rp-kpi{position:relative;padding:12px 14px 11px;border:1px solid var(--grid);border-radius:6px;background:var(--panel-2);overflow:hidden;animation:popIn .4s both;animation-delay:calc(var(--i,0)*70ms)}
.rp-kpi::after{content:"";position:absolute;left:0;right:0;bottom:0;height:2px;background:var(--kc,var(--signal));transform-origin:left;animation:barGrow .8s cubic-bezier(.2,.7,.2,1) both;animation-delay:calc(300ms + var(--i,0)*70ms)}
.rp-kpi span{display:block;font-size:12.5px;color:var(--text-3)}
.rp-kpi b{display:block;font-family:var(--f-id);font-size:28px;font-weight:600;line-height:1.25;color:var(--text);margin-top:2px}
.rp-kpi small{display:block;font-size:12.5px;color:var(--text-2);margin-top:1px}
.rp-sec{border-top:1px solid var(--grid);padding-top:14px}
.rp-sec>h3{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;margin:0 0 10px;font-size:15px;font-weight:600;color:var(--text)}
.rp-tag{font-size:11.5px;font-weight:500;padding:2px 8px;border-radius:10px;border:1px solid var(--grid-2);color:var(--text-3)}
.rp-tag.fact{border-color:var(--signal-line);color:var(--signal)}
.rp-tag.rule{border-color:color-mix(in srgb,var(--pass) 45%,transparent);color:var(--pass)}
.rp-tag.ai{border-color:color-mix(in srgb,var(--caution) 50%,transparent);color:var(--caution)}
.rp-chart{width:100%;height:auto;display:block}
.rp-chart .bar{transform-box:fill-box;transform-origin:bottom;animation:rpRise .7s cubic-bezier(.2,.7,.2,1) both}
@keyframes rpRise{from{transform:scaleY(0)}}
.rp-chart .axis{stroke:var(--grid);stroke-width:1}
.rp-chart text{fill:var(--text-3);font-size:11px;font-family:var(--f-id)}
.rp-legend{display:flex;gap:14px;flex-wrap:wrap;font-size:12.5px;color:var(--text-2);margin-top:6px}
.rp-legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px;vertical-align:-1px}
.rp-cols{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px}
.rp-cols h4{margin:0 0 8px;font-size:13px;font-weight:500;color:var(--text-3)}
.rp-hbars{display:flex;flex-direction:column;gap:8px}
.rp-hbar{display:grid;grid-template-columns:minmax(0,1fr) 44px;gap:4px 10px;font-size:13.5px;color:var(--text-2)}
.rp-hbar .lbl{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.rp-hbar b{font-family:var(--f-id);font-weight:500;text-align:right;color:var(--text)}
.rp-hbar .track{grid-column:1/-1;height:6px;border-radius:3px;background:var(--panel-3);overflow:hidden}
.rp-hbar .fill{height:100%;border-radius:3px;background:var(--hc,var(--signal));transform-origin:left;animation:barGrow .8s cubic-bezier(.2,.7,.2,1) both;animation-delay:calc(200ms + var(--i,0)*80ms)}
.rp-none{font-size:13.5px;color:var(--text-3)}
.rp-obs{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:7px}
.rp-obs li{position:relative;padding:8px 12px 8px 30px;border-radius:5px;background:var(--panel-2);font-size:14px;line-height:1.6;color:var(--text-2);animation:rowIn .4s both;animation-delay:calc(var(--i,0)*90ms)}
.rp-obs li::before{content:"";position:absolute;left:12px;top:15px;width:7px;height:7px;border-radius:50%;background:var(--text-3)}
.rp-obs li.good::before{background:var(--pass)}
.rp-obs li.warn::before{background:var(--caution)}
.rp-ai{margin:0;padding:12px 14px;border-left:3px solid var(--caution);background:color-mix(in srgb,var(--caution) 7%,transparent);border-radius:0 6px 6px 0}
.rp-ai p{margin:0 0 6px;font-size:14px;line-height:1.7;color:var(--text-2)}
.rp-ai p:last-child{margin-bottom:0}
.rp-ai .note{font-size:12.5px;color:var(--text-3)}
.rp-tl{list-style:none;margin:0;padding:0}
.rp-tl li{display:grid;grid-template-columns:110px 84px minmax(0,1fr);gap:10px;padding:7px 0;border-bottom:1px solid var(--grid);font-size:13.5px;color:var(--text-2)}
.rp-tl li:last-child{border-bottom:0}
.rp-tl time{font-family:var(--f-id);font-size:12.5px;color:var(--text-3)}
.rp-tl .k{font-size:12.5px;color:var(--text-3)}
.rp-tl .k.fail{color:var(--stop)}
.rp-tl .app{font-family:var(--f-id);color:var(--text);margin-right:6px}
.rp-foot{font-size:12.5px;color:var(--text-3);line-height:1.6}
.rp-pdf-hint{display:none;font-size:12.5px;color:var(--text-2);margin-top:4px}
.rp-pdf-hint.on{display:block}
#print-host{display:none}
@media (max-width:900px){.rp-kpis{grid-template-columns:repeat(2,minmax(0,1fr))}.rp-cols{grid-template-columns:1fr}.rp-tl li{grid-template-columns:90px minmax(0,1fr)}.rp-tl .k{display:none}}
@media print{
  html,body{height:auto!important;overflow:visible!important;background:#fff!important}
  body.print-report>*:not(#print-host){display:none!important}
  body.print-report #print-host{display:block!important;padding:0}
  #print-host .module{border:0!important;box-shadow:none!important;background:#fff!important}
  #print-host .module::before,#print-host .module::after{display:none!important}
  #print-host .filters,#print-host .rp-period,#print-host .rp-pdf-hint{display:none!important}
  #print-host *,#print-host *::before,#print-host *::after{animation:none!important}
  #print-host .rp-chart{max-width:820px}
  #print-host .rp-sec,#print-host .rp-kpi,#print-host .rp-chart{break-inside:avoid}
  #print-host .print-title{display:block!important;font-size:20px;font-weight:700;margin:0 0 4px}
}
.print-title{display:none}
/* 情境推算：部署單上說明副本數怎麼算出來、能省多少 */
.scn{position:relative;margin:2px 0 16px;border:1px solid var(--signal-line);border-radius:6px;background:linear-gradient(180deg,var(--signal-soft),transparent 70%);padding:14px 16px;overflow:hidden}
.scn::before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--signal)}
.scn-head{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;margin-bottom:10px}
.scn-tag{font-family:var(--f-id);font-size:11.5px;letter-spacing:.12em;color:var(--signal)}
.scn-head h3{margin:0;font-size:16px;font-weight:600;color:var(--text)}
.scn-head h3 b{font-family:var(--f-id);font-size:20px;color:var(--signal);margin:0 2px}
.scn-facts{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:12px}
.scn-fact{display:flex;flex-direction:column;gap:1px;padding:6px 10px;border:1px solid var(--grid-2);border-radius:5px;background:var(--panel);min-width:84px;animation:popIn .35s both;animation-delay:calc(var(--i,0)*70ms)}
.scn-fact span{font-size:12px;color:var(--text-3)}
.scn-fact b{font-family:var(--f-id);font-size:15px;font-weight:600;color:var(--text)}
.scn-fact em{font-style:normal;font-size:11.5px;color:var(--caution)}
.scn-steps{list-style:none;margin:0 0 4px;padding:0;counter-reset:scn;display:flex;flex-direction:column;gap:6px}
.scn-steps li{counter-increment:scn;position:relative;padding-left:28px;font-size:14px;line-height:1.6;color:var(--text-2);animation:rowIn .4s both;animation-delay:calc(200ms + var(--i,0)*140ms)}
.scn-steps li::before{content:counter(scn);position:absolute;left:0;top:2px;width:19px;height:19px;border-radius:50%;border:1px solid var(--signal-line);color:var(--signal);font-family:var(--f-id);font-size:11.5px;display:grid;place-items:center}
.scn-notes{list-style:none;margin:10px 0 0;padding:0;display:flex;flex-direction:column;gap:6px}
.scn-notes li{position:relative;padding:7px 10px 7px 26px;font-size:13.5px;line-height:1.55;color:var(--text-2);background:color-mix(in srgb,var(--caution) 9%,transparent);border-radius:5px}
.scn-notes li::before{content:"";position:absolute;left:11px;top:14px;width:6px;height:6px;border-radius:50%;background:var(--caution)}
.scn-save{margin-top:14px;padding-top:12px;border-top:1px dashed var(--grid-2)}
.scn-save-top{display:flex;align-items:baseline;justify-content:space-between;gap:12px;flex-wrap:wrap}
.scn-save-top span{font-size:13.5px;color:var(--text-2)}
.scn-save-top b{font-family:var(--f-id);font-size:22px;color:var(--pass)}
.scn-bars{display:flex;flex-direction:column;gap:7px;margin:10px 0}
.scn-bar{display:grid;grid-template-columns:120px 1fr 74px;align-items:center;gap:10px;font-size:13px;color:var(--text-3)}
.scn-bar .track{height:8px;border-radius:4px;background:var(--panel-3);overflow:hidden}
.scn-bar .fill{height:100%;border-radius:4px;background:var(--text-3);transform-origin:left;animation:barGrow .9s cubic-bezier(.2,.7,.2,1) both;animation-delay:.5s}
.scn-bar.mine .fill{background:var(--pass)}
.scn-bar b{font-family:var(--f-id);font-weight:500;color:var(--text-2);text-align:right}
@keyframes barGrow{from{transform:scaleX(0)}}
.scn-save ul{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:4px}
.scn-save li{display:flex;justify-content:space-between;gap:12px;font-size:13.5px;color:var(--text-2)}
.scn-save li b{font-family:var(--f-id);font-weight:500;color:var(--pass);white-space:nowrap}
.scn-basis{margin:8px 0 0;font-size:12.5px;color:var(--text-3)}
@media (max-width:640px){.scn-bar{grid-template-columns:88px 1fr 64px}}
.composer-hint{max-width:1160px;margin:6px auto 0;font-size:13px;color:var(--text-3)}
.chat-sources{margin-top:10px;display:flex;flex-wrap:wrap;gap:6px;font-size:13px;color:var(--text-3)}

/* ── 部署確認卡（Chat 流程，第二階段會再改成部署單模組） ── */
.deploy-confirm{position:relative;background:var(--panel);border:1px solid var(--grid);border-radius:4px;padding:16px;width:min(620px,100%)}
.deploy-confirm-head{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;margin-bottom:10px}
.deploy-confirm-title{font-size:15px;font-weight:700}
.deploy-confirm-sub{font-size:13px;color:var(--text-2);margin-top:2px}
.deploy-confirm-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;margin:12px 0}
.deploy-confirm-field label{display:block;font-size:13px;color:var(--text-3);margin-bottom:5px}
.deploy-confirm-field input{font-family:var(--f-id);font-size:13.5px;padding:8px 10px}
.field-hint{font-size:12.5px;color:var(--text-3);margin-top:4px}
.deploy-confirm-note{font-size:13.5px;color:var(--text-2);background:var(--panel-2);border:1px solid var(--grid);border-radius:4px;padding:9px 11px}
.deploy-confirm-error{display:none;font-size:13.5px;background:var(--stop-soft);border:1px solid color-mix(in srgb,var(--stop) 50%,transparent);border-radius:4px;padding:8px 10px;margin-top:10px}
.deploy-confirm-actions{display:flex;gap:8px;justify-content:flex-end;flex-wrap:wrap;margin-top:12px}
.deploy-confirm-btn{padding:7px 14px;font-size:14px}

/* ══ 對話互動模組（第二階段） ══ */
.msg.ai .msg-bubble{flex:1;min-width:0}
.id{font-family:var(--f-id);font-size:.92em}
.dim{color:var(--text-2)}
.t-stop{color:var(--stop)}
.t-warn{color:var(--caution)}
.w-lead{margin-bottom:10px}
.md{max-width:96ch}
.md p{margin:0 0 .6em}
.md p:last-child{margin-bottom:0}
.md ul,.md ol{margin:.2em 0 .7em;padding-left:1.4em}
.md li{margin:.15em 0}
.md li::marker{color:var(--signal)}
.md h4{font-size:15.5px;font-weight:700;margin:.8em 0 .3em}
.md code{background:var(--panel-2);border:1px solid var(--grid);border-radius:4px;padding:0 5px;color:var(--signal)}
.md pre code{background:none;border:0;padding:0;color:var(--text)}
.md strong{color:var(--text);font-weight:700}

.zt-w,.flow{padding:0;width:100%}
.module-head{display:flex;align-items:center;gap:10px 14px;flex-wrap:wrap;padding:12px 18px;border-bottom:1px solid var(--grid)}
.module-head h2{margin:0;font-size:15.5px;font-weight:700}
.module-head .meta{color:var(--text-3);font-size:13.5px;display:flex;gap:10px;flex-wrap:wrap}
.module-head .meta b{font-weight:500}
.filters{margin-left:auto;display:flex;gap:6px;flex-wrap:wrap}
.chip{border:1px solid var(--grid-2);background:transparent;color:var(--text-2);border-radius:999px;padding:3px 12px;font-size:13.5px;transition:background .15s,border-color .15s,color .15s}
.chip:hover{border-color:var(--signal);color:var(--text)}
.chip[aria-pressed="true"]{background:var(--signal);border-color:var(--signal);color:var(--on-signal)}
.chip.ghost{border-style:dashed}
.zt-w .table-wrap,.flow .table-wrap{border:0;border-radius:0;background:transparent}
.zt-w th{background:transparent}
.zt-w th button{background:none;border:0;padding:0;color:inherit;font:inherit;display:inline-flex;align-items:center;gap:4px}
.zt-w th button:hover{color:var(--text)}
.zt-w th[aria-sort] button{color:var(--signal)}
.zt-w th[aria-sort="ascending"] button::after{content:"";border:4px solid transparent;border-bottom-color:currentColor;margin-top:-4px}
.zt-w th[aria-sort="descending"] button::after{content:"";border:4px solid transparent;border-top-color:currentColor;margin-top:4px}
.zt-w td{white-space:nowrap}
.zt-w td.wrap{white-space:normal}
.zt-w tbody tr[data-act]{cursor:pointer}
.zt-w tbody tr[data-act]:hover td:first-child,.zt-w tbody tr[data-act]:focus-visible td:first-child{box-shadow:inset 2px 0 0 var(--signal)}
.zt-w tbody tr:focus-visible{outline:none}
.zt-w tbody tr:focus-visible td{background:var(--signal-soft)}
td.r,th.r{text-align:right}
tr.enter{animation:rowIn .3s ease both}
@keyframes rowIn{from{opacity:0;transform:translateX(-6px)}to{opacity:1;transform:none}}
.state{display:inline-flex;align-items:center;gap:7px;font-size:14px}
.state i{width:7px;height:7px;border-radius:50%;flex:none}
.s-run{color:var(--pass)} .s-run i{background:var(--pass);animation:beat 2.4s ease-out infinite}
.s-done{color:var(--text-2)} .s-done i{background:var(--text-3)}
.s-pend,.s-warn{color:var(--caution)} .s-pend i,.s-warn i{background:var(--caution)}
.s-fail{color:var(--stop)} .s-fail i{background:var(--stop)}
.s-sleep{color:var(--text-2)} .s-sleep i{background:var(--signal);animation:sleepy 3.2s ease-in-out infinite}
@keyframes sleepy{50%{opacity:.2;transform:scale(.7)}}
/* 分時擴縮與休眠 */
.as-btns{display:inline-flex;gap:6px;flex-wrap:wrap;justify-content:flex-end}
.as-sched{font-size:13.5px;max-width:340px}
.zt-w td.id .as-note{font-family:var(--f-body);font-size:13px}
.as-msg{padding:10px 18px;border-top:1px solid var(--grid);font-size:14px;color:var(--text)}
.as-save{padding:14px 18px;border-top:1px solid var(--grid);display:flex;flex-direction:column;gap:8px;font-size:14px}
.as-save-top{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap}
.as-save-top b{color:var(--pass);font-family:var(--f-display)}
.as-bar{height:8px;border-radius:4px;background:color-mix(in srgb,var(--pass) 28%,transparent);overflow:hidden}
.as-bar-fill{height:100%;background:var(--signal);border-radius:4px;transform-origin:left;animation:asGrow .9s cubic-bezier(.2,.8,.2,1) both}
@keyframes asGrow{from{transform:scaleX(0)}}
.as-foot{display:flex;justify-content:flex-end;gap:8px;padding:12px 18px;border-top:1px solid var(--grid)}
.zt-w .pager{padding:10px 18px;border-top:1px solid var(--grid);justify-content:flex-end;gap:6px}
.zt-w .pager .info{margin-right:auto}
.zt-w .pager button{min-width:32px;height:30px;padding:0 6px;border:1px solid var(--grid-2);background:transparent;border-radius:5px;font-family:var(--f-display);font-size:14px;color:var(--text-2);display:inline-grid;place-items:center}
.zt-w .pager button:hover:not(:disabled){border-color:var(--signal);color:var(--text)}
.zt-w .pager button[aria-current]{background:var(--signal);border-color:var(--signal);color:var(--on-signal)}
.zt-w .pager button:disabled{opacity:.35;cursor:default}
.zt-w .pager .gap{color:var(--text-3);padding:0 2px}
.w-empty{padding:28px 18px;text-align:center;color:var(--text-2)}
.w-empty b{display:block;color:var(--text);font-size:15.5px;margin-bottom:4px}
.w-empty .btn{margin-top:12px}
.reps{display:inline-flex;gap:3px;vertical-align:middle;margin-right:8px}
.reps i{width:7px;height:14px;border-radius:2px;background:var(--grid-2)}
.reps i.on{background:var(--pass)}
.reps.short i.on{background:var(--caution)}

/* 時間軸（部署紀錄） */
.timeline{list-style:none;margin:0;padding:6px 18px 4px}
.timeline li{position:relative;display:grid;grid-template-columns:120px minmax(0,1fr) auto;gap:4px 16px;align-items:center;padding:10px 0 10px 18px;border-left:1px solid var(--grid)}
.timeline li::before{content:"";position:absolute;left:-4px;top:17px;width:7px;height:7px;border-radius:50%;background:var(--signal);box-shadow:0 0 0 3px var(--panel)}
.timeline time{font-family:var(--f-display);font-size:13.5px;color:var(--text-3)}
.timeline .what{min-width:0;overflow-wrap:anywhere}
.timeline .app{display:inline-block;font-family:var(--f-id);font-size:13px;border:1px solid var(--signal-line);color:var(--signal);border-radius:4px;padding:0 6px;margin-right:8px}
.timeline .hash{font-family:var(--f-id);font-size:12.5px;color:var(--text-3);margin-left:8px}

/* 問題清單（自動修復） */
.issues{list-style:none;margin:0;padding:0}
.issues li{display:flex;align-items:center;gap:14px;flex-wrap:wrap;padding:12px 18px;border-bottom:1px solid color-mix(in srgb,var(--grid) 65%,transparent)}
.issues li:last-child{border-bottom:0}
.issues .what{flex:1;min-width:200px}
.issues .what .id{display:block}
.radar{width:42px;height:42px;margin:0 auto 10px;border-radius:50%;border:1px solid var(--pass);position:relative;overflow:hidden}
.radar::before{content:"";position:absolute;inset:0;background:conic-gradient(from 0deg,transparent 0deg,color-mix(in srgb,var(--pass) 45%,transparent) 60deg,transparent 61deg);animation:spin 2.4s linear infinite}
.radar::after{content:"";position:absolute;left:50%;top:50%;width:6px;height:6px;margin:-3px;border-radius:50%;background:var(--pass)}

/* 用量儀表 */
.gauges{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:0}
.gauge-cell{display:flex;align-items:center;gap:16px;padding:16px 18px;border-right:1px solid var(--grid)}
.gauge-cell:last-child{border-right:0}
.ring{width:84px;height:84px;flex:none}
.ring circle{fill:none;stroke-width:8}
.ring .track{stroke:var(--panel-3)}
.ring .val{stroke:var(--signal);stroke-linecap:round;transform:rotate(-90deg);transform-origin:50% 50%;animation:ringIn .9s cubic-bezier(.2,.8,.2,1) both}
.ring.hot .val{stroke:var(--caution)} .ring.full .val{stroke:var(--stop)}
.ring text{fill:var(--text);font-family:var(--f-display);font-size:19px;font-weight:600}
@keyframes ringIn{from{stroke-dashoffset:226}}
.gauge-cell .lbl{color:var(--text-3);font-size:13px}
.gauge-cell .big{font-family:var(--f-display);font-size:20px;font-weight:600}
.facts{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));border-top:1px solid var(--grid)}
.facts div{padding:12px 18px;border-right:1px solid var(--grid)}
.facts div:last-child{border-right:0}
.facts dt{font-size:13px;color:var(--text-3)}
.facts dd{margin:0;font-family:var(--f-display);font-size:18px;font-weight:600}
.w-note{padding:10px 18px;border-top:1px solid var(--grid);font-size:13px;color:var(--text-3)}

/* 詳情（抽屜與對話內共用） */
.pd{display:flex;flex-direction:column;gap:16px}
.pd-top{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.kv{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px 18px;margin:0}
.kv dt{font-size:13px;color:var(--text-3)}
.kv dd{margin:0;font-size:14.5px;overflow-wrap:anywhere}
.bar-g{display:flex;flex-direction:column;gap:5px;font-size:13.5px}
.bar-g .row{display:flex;justify-content:space-between;color:var(--text-2)}
.bar-g .bar{height:6px;border-radius:3px;background:var(--panel-3);overflow:hidden}
.bar-g .bar span{display:block;height:100%;background:var(--signal);width:0;transition:width .8s cubic-bezier(.2,.8,.2,1)}
.sec-t{font-size:13px;color:var(--text-3);margin-bottom:8px}
.ctrs{display:flex;flex-direction:column;gap:8px}
.ctr{border:1px solid var(--grid);border-radius:4px;padding:10px 12px;display:grid;grid-template-columns:minmax(0,1fr) auto;gap:2px 12px}
.ctr .id{overflow-wrap:anywhere}
.ctr .msgline{grid-column:1 / -1;color:var(--text-2);font-size:13.5px;overflow-wrap:anywhere}
.events{list-style:none;margin:0;padding:0;border-left:1px solid var(--grid);display:flex;flex-direction:column;gap:12px}
.events li{position:relative;padding-left:16px;font-size:14px;overflow-wrap:anywhere}
.events li::before{content:"";position:absolute;left:-4px;top:8px;width:7px;height:7px;border-radius:50%;background:var(--text-3)}
.events li.bad::before{background:var(--stop)}
.events b{font-weight:500;margin-right:6px}
.events .cnt{color:var(--text-3);font-size:12.5px;margin-left:6px}
.diag{border:1px solid color-mix(in srgb,var(--caution) 50%,transparent);background:var(--caution-soft);border-radius:4px;padding:12px 14px;font-size:14.5px;display:flex;flex-direction:column;gap:4px}
.diag b{color:var(--caution)}
.diag .fix{color:var(--text-2)}
.actions{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.btn.danger{border-color:color-mix(in srgb,var(--stop) 55%,transparent);color:var(--stop);background:transparent}
.inline-form{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.inline-form input{width:auto;flex:1;min-width:120px;padding:7px 10px;font-size:14px}
.inline-form input[type=number]{flex:0 0 90px;min-width:0}
.form-err{color:var(--stop);font-size:13.5px;min-height:1em}
.zt-w .pd{padding:16px 18px}
.pd + .pd{border-top:1px solid var(--grid)}
.mini-list{display:flex;flex-direction:column;border:1px solid var(--grid);border-radius:4px}
.mini-list button{display:flex;align-items:center;gap:10px;justify-content:space-between;text-align:left;background:none;border:0;border-bottom:1px solid var(--grid);padding:9px 12px;color:var(--text)}
.mini-list button:last-child{border-bottom:0}
.mini-list button:hover{background:var(--signal-soft)}
.mini-list .id{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}

/* 抽屜 */
.zt-scrim{position:fixed;inset:0;background:var(--scrim);z-index:180;opacity:0;pointer-events:none;transition:opacity .2s}
.zt-scrim.open{opacity:1;pointer-events:auto}
.zt-drawer{position:fixed;top:0;right:0;bottom:0;width:min(480px,100%);background:var(--panel);border-left:1px solid var(--signal);box-shadow:var(--shadow-pop);z-index:190;transform:translateX(102%);transition:transform .3s cubic-bezier(.2,.8,.2,1);display:flex;flex-direction:column;padding-top:env(safe-area-inset-top,0px)}
.zt-drawer.open{transform:none}
.zt-drawer header{display:flex;align-items:flex-start;gap:12px;padding:18px 20px;border-bottom:1px solid var(--grid)}
.zt-drawer header .k{font-size:13px;color:var(--text-3)}
.zt-drawer header h3{margin:2px 0 0;font-family:var(--f-id);font-size:15.5px;font-weight:500;overflow-wrap:anywhere}
.zt-drawer header button{margin-left:auto;flex:none}
.zt-drawer .body{flex:1;overflow-y:auto;padding:18px 20px 28px}
.skeleton{display:flex;flex-direction:column;gap:12px}
.skeleton i{display:block;height:14px;border-radius:4px;background:linear-gradient(90deg,var(--panel-2),var(--panel-3),var(--panel-2));background-size:200% 100%;animation:shimmer 1.2s linear infinite}
.skeleton i:nth-child(2){width:70%}.skeleton i:nth-child(3){width:85%}.skeleton i:nth-child(4){width:55%}
@keyframes shimmer{to{background-position:-200% 0}}

/* 部署單與流程卡 */
.steps{list-style:none;display:flex;gap:6px;margin:0 0 0 auto;padding:0;font-size:13px;color:var(--text-3)}
.steps li{display:flex;align-items:center;gap:6px}
.steps li + li::before{content:"";width:16px;height:1px;background:var(--grid-2)}
.steps li.on{color:var(--signal)}
.steps li.done{color:var(--text-2)}
.flow .body{padding:16px 18px;display:flex;flex-direction:column;gap:14px}
.flow .lead{color:var(--text-2);font-size:14.5px}
.fgrid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px 14px}
.fgrid label{display:block;font-size:13px;color:var(--text-3);margin-bottom:5px}
.fgrid input{font-family:var(--f-id);font-size:14px;padding:8px 10px}
.fgrid .hint{font-size:12.5px;color:var(--text-3);margin-top:4px}
.flow .err{display:none;border:1px solid color-mix(in srgb,var(--stop) 55%,transparent);background:var(--stop-soft);border-radius:4px;padding:9px 12px;font-size:14px}
.flow .foot{display:flex;gap:8px;justify-content:flex-end;flex-wrap:wrap;padding:12px 18px;border-top:1px solid var(--grid);background:color-mix(in srgb,var(--panel-2) 60%,transparent)}
.spec{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));border-bottom:1px solid var(--grid);margin:0}
.spec div{padding:12px 18px;border-right:1px solid var(--grid);min-width:0}
.spec div:last-child{border-right:0}
.spec dt{font-size:13px;color:var(--text-3)}
.spec dd{margin:2px 0 0;font-size:21px;font-family:var(--f-display);font-weight:600;overflow-wrap:anywhere}
.spec dd small{font-size:13.5px;font-weight:400;color:var(--text-2);margin-left:4px;font-family:var(--f-body)}
.review{display:grid;grid-template-columns:repeat(3,minmax(0,1fr))}
.reviewer{position:relative;padding:14px 18px;border-right:1px solid var(--grid);overflow:hidden;min-height:112px}
.reviewer:last-child{border-right:0}
.reviewer .r-head{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:10px;padding-bottom:10px;border-bottom:1px dashed var(--grid)}
.reviewer .role{margin:0;font-size:17px;font-weight:700;color:var(--text);letter-spacing:.03em}
.reviewer .verdict{font-weight:700;font-size:13.5px;display:inline-flex;align-items:center;gap:6px;padding:1px 10px;border:1px solid currentColor;border-radius:999px;white-space:nowrap}
.reviewer .verdict i{width:7px;height:7px;border-radius:50%}
.reviewer p{margin:0;font-size:14px;color:var(--text-2)}
.r-list{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:7px}
.r-list li{position:relative;padding-left:16px;font-size:14px;line-height:1.6;color:var(--text-2)}
.r-list li::before{content:"";position:absolute;left:1px;top:.6em;width:7px;height:7px;border-radius:50%;background:var(--text-3)}
.r-list li.sev-critical::before,.r-list li.sev-high::before{background:var(--stop)}
.r-list li.sev-medium::before{background:var(--caution)}
.r-more{margin-top:8px;font-size:13px;color:var(--text-3)}
.reviewer.ok .verdict{color:var(--pass)} .reviewer.ok .verdict i{background:var(--pass)}
.reviewer.warn .verdict{color:var(--caution)} .reviewer.warn .verdict i{background:var(--caution)}
.reviewer.stop .verdict{color:var(--stop)} .reviewer.stop .verdict i{background:var(--stop)}
.reviewer .scan{position:absolute;inset:0;background:linear-gradient(180deg,transparent 0%,var(--signal-soft) 80%,var(--signal) 100%);transform:translateY(-100%);opacity:0;pointer-events:none}
.flow.replay .reviewer.waiting .verdict,.flow.replay .reviewer.waiting .r-list,.flow.replay .reviewer.waiting p,.flow.replay .reviewer.waiting .r-more,
.flow.replay .reviewer.scanning .verdict,.flow.replay .reviewer.scanning .r-list,.flow.replay .reviewer.scanning p,.flow.replay .reviewer.scanning .r-more{opacity:0}
.flow.replay .reviewer.waiting::after,.flow.replay .reviewer.scanning::after{content:"審查中";position:absolute;left:18px;top:62px;color:var(--text-3);font-size:13.5px}
.flow.replay .reviewer.scanning .scan{animation:scan .7s ease-in-out both}
@keyframes scan{0%{transform:translateY(-100%);opacity:1}100%{transform:translateY(0);opacity:0}}
.reviewer .verdict,.reviewer p,.reviewer .r-list,.reviewer .r-more{transition:opacity .25s}
.res-line{display:flex;flex-wrap:wrap;gap:6px 22px;padding:12px 18px;border-top:1px solid var(--grid);font-size:14px;color:var(--text-2)}
.res-line b{font-family:var(--f-display);color:var(--text);font-weight:600}
.res-line .neg{color:var(--stop)}
.decision{display:flex;align-items:center;gap:16px;flex-wrap:wrap;padding:14px 18px;border-top:1px solid var(--grid)}
.decision .say{flex:1;min-width:220px}
.decision .say b{display:block;font-size:16px}
.decision .say ul{margin:4px 0 0;padding-left:1.2em;color:var(--text-2);font-size:14px}
.seal{width:76px;height:76px;flex:none;color:var(--pass)}
.seal.warn{color:var(--caution)} .seal.stop{color:var(--stop)}
.seal svg{width:100%;height:100%;overflow:visible}
.seal .ring-o{fill:none;stroke:currentColor;stroke-width:2.5;stroke-dasharray:227;stroke-dashoffset:0}
.seal .ticks{fill:none;stroke:currentColor;stroke-width:1;stroke-dasharray:2 5;opacity:.7;transform-origin:38px 38px;animation:spin 14s linear infinite}
.seal text{fill:currentColor;font-family:var(--f-body);font-weight:700;font-size:19px;transform-origin:38px 38px}
.flow.replay .seal.armed .ring-o{stroke-dashoffset:227}
.flow.replay .seal.armed .ticks,.flow.replay .seal.armed text{opacity:0}
.flow.replay .seal.on .ring-o{animation:draw .6s ease-out forwards}
.flow.replay .seal.on .ticks{animation:fadeT .3s .45s both,spin 14s linear .45s infinite}
.flow.replay .seal.on text{animation:pop .35s .5s cubic-bezier(.2,1.6,.4,1) both}
.flow.replay .seal.on{animation:flash .9s .5s ease-out}
@keyframes draw{from{stroke-dashoffset:227}to{stroke-dashoffset:0}}
@keyframes fadeT{from{opacity:0}to{opacity:.7}}
@keyframes pop{0%{opacity:0;transform:scale(1.6)}100%{opacity:1;transform:scale(1)}}
@keyframes flash{30%{filter:drop-shadow(0 0 14px currentColor)}}
.busy{display:flex;align-items:center;gap:14px;padding:18px}
.busy .scanbar{flex:1;height:3px;border-radius:2px;background:var(--panel-3);overflow:hidden;position:relative}
.busy .scanbar::after{content:"";position:absolute;inset:0;width:35%;background:linear-gradient(90deg,transparent,var(--signal),transparent);animation:slide 1.2s ease-in-out infinite}
@keyframes slide{from{transform:translateX(-100%)}to{transform:translateX(300%)}}
.delta{display:flex;align-items:center;gap:14px;flex-wrap:wrap;font-size:15px}
.delta .from{color:var(--text-2);text-decoration:line-through;text-decoration-color:var(--text-3)}
.delta .to{font-family:var(--f-display);font-size:22px;font-weight:600}
.delta .arrow{width:28px;height:2px;background:var(--signal);position:relative}
.delta .arrow::after{content:"";position:absolute;right:-1px;top:-4px;border:5px solid transparent;border-left-color:var(--signal);border-right:0}
.flow.danger{border-color:color-mix(in srgb,var(--stop) 60%,transparent)}
.flow.danger::before,.flow.danger::after{border-color:var(--stop)}
.flow.done-card::before,.flow.done-card::after{border-color:var(--pass)}
.flow.fail-card::before,.flow.fail-card::after{border-color:var(--stop)}
.legs-wrap{position:relative;padding:18px 18px 16px}
.legs{list-style:none;margin:0;padding:0;display:grid;grid-template-columns:repeat(4,minmax(0,1fr))}
.legs-line{position:absolute;left:25px;right:25px;top:24px;height:2px;background:var(--grid-2)}
.legs-line span{position:absolute;inset:0 auto 0 0;background:linear-gradient(90deg,var(--signal),color-mix(in srgb,var(--signal) 40%,transparent));box-shadow:0 0 10px var(--signal);transition:width .8s ease}
.legs li{position:relative;z-index:1;padding-top:22px;font-size:14px;color:var(--text-3);padding-right:8px}
.legs li::before{content:"";position:absolute;top:0;left:0;width:14px;height:14px;border-radius:50%;background:var(--panel);border:2px solid var(--grid-2)}
.legs li.done{color:var(--text)} .legs li.done::before{background:var(--signal);border-color:var(--signal)}
.legs li.now{color:var(--text);font-weight:700} .legs li.now::before{border-color:var(--signal);animation:ping 1.6s ease-out infinite}
.legs li.bad{color:var(--stop);font-weight:700} .legs li.bad::before{border-color:var(--stop);background:var(--stop)}
.legs li span{display:block;font-weight:400;font-size:13px;color:var(--text-3)}
@keyframes ping{0%{box-shadow:0 0 0 0 var(--signal)}100%{box-shadow:0 0 0 8px transparent}}
@media (max-width:760px){
  .spec,.facts{grid-template-columns:repeat(2,minmax(0,1fr))}
  .spec div:nth-child(2){border-right:0}
  .spec div:nth-child(-n+2){border-bottom:1px solid var(--grid)}
  .review,.gauges{grid-template-columns:1fr}
  .reviewer,.gauge-cell{border-right:0;border-bottom:1px solid var(--grid)}
  .legs{grid-template-columns:repeat(2,minmax(0,1fr));row-gap:14px}
  .legs-line{display:none}
  .fgrid{grid-template-columns:1fr}
  .timeline li{grid-template-columns:1fr}
  .steps{margin-left:0;width:100%}
}

details.why{margin-top:10px;border-top:1px solid var(--grid);padding-top:8px;font-size:14px}
details.why summary{cursor:pointer;color:var(--text-3)}
details.why ul{margin:6px 0 0;padding-left:1.2em;color:var(--text-2)}

/* ══ 頁面（第三階段） ══ */
.page-head{display:flex;align-items:flex-end;justify-content:space-between;gap:12px 24px;flex-wrap:wrap;margin-bottom:20px}
.page-head .page-title{margin:0 0 4px}
.page-head .page-sub{margin:0;max-width:72ch}
.page-actions{display:flex;gap:8px;flex-wrap:wrap}
.page-stack{display:flex;flex-direction:column;gap:18px}
.page-stack > *{min-width:0}
.chart-box{padding:14px 18px 12px}
.quiet tr.enter,.quiet li.enter{animation:none}
.status-strip{display:flex;align-items:center;gap:10px;border:1px solid var(--grid);border-radius:4px;padding:11px 16px;background:var(--panel);font-size:14.5px}
.status-strip .lamp-s{width:9px;height:9px;border-radius:50%;background:var(--text-3);flex:none}
.status-strip.ok{border-color:color-mix(in srgb,var(--pass) 45%,transparent)}
.status-strip.ok .lamp-s{background:var(--pass);box-shadow:0 0 8px var(--pass);animation:beat 2.4s ease-out infinite}
.status-strip.caution{border-color:var(--caution);background:var(--caution-soft)}
.status-strip.caution .lamp-s{background:var(--caution)}
.status-strip.stop{border-color:var(--stop);background:var(--stop-soft)}
.status-strip.stop .lamp-s{background:var(--stop)}
.status-strip.busy{border-color:var(--signal-line);background:var(--signal-soft)}
#dash-mon div{display:flex;flex-direction:column;gap:2px}
#dash-mon .dim{font-size:13px}
.pod-chart-empty{margin:0}
.logbox{margin:0;max-height:260px;overflow:auto;background:var(--void);border:1px solid var(--grid);border-radius:4px;padding:10px 12px;font-family:var(--f-id);font-size:12.5px;line-height:1.6;color:var(--text-2);white-space:pre-wrap;word-break:break-all}
.module{padding:0}
.push-box{padding:14px 18px;display:flex;flex-direction:column;gap:10px;color:var(--text-2);font-size:14px;line-height:1.7}
.push-box ol{margin:0;padding-left:20px}
.push-box .t-warn{color:var(--caution)}
/* ── 預約與行事曆（2026-10-23） ── */
.nav-badge{margin-left:auto;min-width:18px;height:18px;padding:0 5px;border-radius:9px;background:var(--caution);color:#1a1200;font-size:11px;font-weight:700;display:inline-flex;align-items:center;justify-content:center}
.nav-badge[hidden]{display:none}
.sched-box{border:1px solid var(--signal-line);background:var(--signal-soft);border-radius:var(--radius);padding:12px 14px;margin:10px 0}
.sched-box .t{font-weight:600;margin-bottom:8px;display:flex;align-items:center;gap:8px}
.sched-pick{display:flex;flex-wrap:wrap;align-items:center;gap:6px}
.sched-pick select{background:var(--panel);color:var(--text);border:1px solid var(--grid-2);border-radius:8px;padding:6px 8px;font:inherit}
.sched-pick span{color:var(--text-2)}
.sched-when{margin-top:8px;font-size:14px}
.sched-when b{color:var(--signal)}
.sched-when .bad{color:var(--stop)}
.sched-kv{display:grid;grid-template-columns:auto 1fr;gap:6px 14px;margin:4px 0 0;font-size:14px}
.sched-kv dt{color:var(--text-3)} .sched-kv dd{margin:0}
.st-pill{font-size:12px;padding:2px 9px;border-radius:999px;border:1px solid var(--grid-2);color:var(--text-2);white-space:nowrap}
.st-pill.scheduled{border-color:var(--signal-line);color:var(--signal)}
.st-pill.due{border-color:var(--caution);color:var(--caution);background:var(--caution-soft)}
.st-pill.done{border-color:var(--pass);color:var(--pass)}
.st-pill.failed,.st-pill.blocked{border-color:var(--stop);color:var(--stop)}
.sch-row{display:flex;flex-wrap:wrap;align-items:center;gap:8px 12px;padding:10px 0;border-top:1px solid var(--grid)}
.sch-row:first-child{border-top:0}
.sch-row .when{font-variant-numeric:tabular-nums;color:var(--text-2);min-width:150px}
.sch-row .what{flex:1;min-width:180px}
.sch-row .what .res{display:block;font-size:12.5px;color:var(--text-3)}
.sch-row .acts{display:flex;gap:6px}
.sch-row.del .what b{color:var(--stop)}
.due-overlay{position:fixed;inset:0;z-index:320;background:rgba(5,12,20,.72);display:flex;align-items:center;justify-content:center;padding:16px}
.due-overlay[hidden]{display:none}
.due-dialog{width:min(640px,100%);max-height:88vh;overflow:auto;background:var(--panel);border:1px solid var(--caution);border-radius:14px;box-shadow:0 20px 60px rgba(0,0,0,.45)}
.due-head{padding:18px 20px 6px}.due-head h2{margin:0 0 6px;font-size:20px}.due-head p{margin:0;color:var(--text-2);font-size:14px;line-height:1.7}
#due-list{padding:4px 20px}
.due-item{border:1px solid var(--grid);border-radius:12px;padding:12px 14px;margin:10px 0}
.due-item.del{border-color:var(--stop)}
.due-item .sum{font-weight:600}
.due-item .meta2{font-size:13px;color:var(--text-2);margin:2px 0 10px}
.due-item .warn{color:var(--stop);font-size:13px;margin-bottom:8px}
.due-item .out{margin-top:8px;font-size:14px}
.due-foot{padding:6px 20px 18px;display:flex;justify-content:flex-end}
.cal-wrap{display:grid;grid-template-columns:minmax(0,1.6fr) minmax(280px,1fr);gap:16px;align-items:start}
@media (max-width:1000px){.cal-wrap{grid-template-columns:1fr}}
.cal-month .module-head h2{min-width:120px;text-align:center}
.cal-legend{display:flex;flex-wrap:wrap;gap:4px 12px;margin-left:auto;font-size:12px}
.mk{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:4px;vertical-align:1px}
.mk.task{background:var(--signal)}.mk.due{background:var(--caution)}.mk.dep{background:var(--pass)}.mk.bad{background:var(--stop)}.mk.note{background:var(--text-2);border-radius:2px}
.cal-grid{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:1px;background:var(--grid);border-top:1px solid var(--grid)}
.cal-grid .wd{background:var(--panel-2);text-align:center;font-size:12px;color:var(--text-3);padding:6px 0}
.cal-cell{background:var(--panel);min-height:84px;min-width:0;overflow:hidden;padding:6px 8px;text-align:left;border:0;color:var(--text);font:inherit;cursor:pointer;display:flex;flex-direction:column;gap:4px}
.cal-cell:hover{background:var(--panel-2)}
.cal-cell.out{opacity:.35;cursor:default}
.cal-cell.sel{outline:2px solid var(--signal);outline-offset:-2px}
.cal-cell .n{font-size:13px;font-variant-numeric:tabular-nums}
.cal-cell.today .n{background:var(--signal);color:var(--on-signal);border-radius:999px;padding:0 7px;align-self:flex-start;font-weight:700}
.cal-cell .mks{display:flex;flex-wrap:wrap;gap:3px;align-items:center;font-size:11px;color:var(--text-2)}
.cal-cell .chip{font-size:11px;line-height:1.4;padding:0 5px;border-radius:4px;background:var(--signal-soft);color:var(--signal);overflow:hidden;white-space:nowrap;text-overflow:ellipsis;max-width:100%}
.cal-cell .chip.due{background:var(--caution-soft);color:var(--caution)}
.cal-day-body{padding:12px 16px 16px;display:flex;flex-direction:column;gap:14px}
.cal-day-body h3{font-size:13px;color:var(--text-3);margin:0 0 4px;font-weight:600}
.cal-day-body textarea{width:100%;min-height:80px;background:var(--panel-2);color:var(--text);border:1px solid var(--grid-2);border-radius:8px;padding:8px;font:inherit;resize:vertical}
.cal-ev{font-size:13.5px;padding:6px 0;border-top:1px solid var(--grid);display:flex;gap:8px}
.cal-ev:first-child{border-top:0}
.cal-ev time{color:var(--text-3);font-variant-numeric:tabular-nums}
.cal-ev .k{font-weight:600}.cal-ev .k.bad{color:var(--stop)}
.card.module,.auth-card{padding:34px 32px}
.pd + .pd{padding-top:18px;margin-top:2px}
.zt-drawer .pd + .pd{border-top:1px solid var(--grid)}

@media (max-width:900px){
  .sidebar{width:216px}
  .page{padding:22px 18px 40px}
  .grid-3,.grid-2,.prompt-grid,.court-agents,.enrich-grid{grid-template-columns:1fr}
  .status-bar,.alert-banner{padding-left:18px;padding-right:18px}
}
@media (max-width:640px){
  .layout{flex-direction:column}
  .sidebar{width:100%;height:auto;flex-direction:row;flex-wrap:wrap;align-items:center;padding:10px 16px;border-right:0;border-bottom:1px solid var(--grid)}
  .sidebar-nav{flex-direction:row;flex-wrap:nowrap;overflow-x:auto;flex:1 1 100%;order:3}
  .sidebar-nav .nav-section,#chat-room-list,#project-list,.sys-panel,.sidebar-footer,.new-chat-wrap{display:none}
  .nav-item{width:auto;white-space:nowrap}
  .nav-item.active::before{display:none}
  .main{height:auto;flex:1;min-height:0}
  .deploy-confirm-grid{grid-template-columns:1fr}
}
@media (prefers-reduced-motion: reduce){*,*::before,*::after{animation:none!important;transition:none!important}}

</style>
</head>
<body>

{% if not logged_in and page != 'register' %}
<!-- ── Login Page ── -->
<div class="auth-wrap">
  <div class="auth-card card">
    <div class="auth-logo"><svg width="26" height="26" viewBox="0 0 26 26" aria-hidden="true"><circle cx="13" cy="13" r="11" fill="none" stroke="currentColor" stroke-width="2"/><g stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><path d="M13 3.5v19M3.5 13h19M6.3 6.3l13.4 13.4M19.7 6.3 6.3 19.7"/></g><circle cx="13" cy="13" r="3.4" fill="var(--panel)" stroke="currentColor" stroke-width="2"/></svg><span>ZeroTouch</span></div>
    <div class="auth-title">登入</div>
    <div class="auth-sub">用自然語言部署和管理你的 Kubernetes 服務</div>
    {% if error %}<div class="auth-error">{{ error }}</div>{% endif %}
    <form method="POST" action="/auth/login">
      <div class="form-group">
        <label for="login-username">帳號</label>
        <input id="login-username" type="text" name="username" autocomplete="username" required autofocus>
      </div>
      <div class="form-group">
        <label for="login-password">密碼</label>
        <input id="login-password" type="password" name="password" autocomplete="current-password" required>
      </div>
      <button class="btn-primary" type="submit">登入</button>
    </form>
    {% if google_login_available %}
    <div class="auth-divider"><div class="line"></div><span>或</span><div class="line"></div></div>
    <a href="/auth/google/login" class="google-btn">
      <svg width="18" height="18" viewBox="0 0 18 18"><path fill="#4285F4" d="M17.64 9.2c0-.64-.06-1.25-.16-1.84H9v3.48h4.84c-.21 1.13-.84 2.09-1.8 2.73v2.27h2.92c1.7-1.57 2.68-3.88 2.68-6.64z"/><path fill="#34A853" d="M9 18c2.43 0 4.47-.8 5.96-2.18l-2.92-2.27c-.8.54-1.84.86-3.04.86-2.34 0-4.32-1.58-5.03-3.71H.96v2.34C2.44 15.98 5.48 18 9 18z"/><path fill="#FBBC05" d="M3.97 10.7c-.18-.54-.28-1.11-.28-1.7s.1-1.16.28-1.7V4.96H.96A8.996 8.996 0 000 9c0 1.45.35 2.83.96 4.04l3.01-2.34z"/><path fill="#EA4335" d="M9 3.58c1.32 0 2.51.45 3.44 1.35l2.59-2.59C13.46.89 11.43 0 9 0 5.48 0 2.44 2.02.96 4.96l3.01 2.34C4.68 5.16 6.66 3.58 9 3.58z"/></svg>
      使用 Google 帳號登入
    </a>
    {% endif %}
    <div class="auth-link">還沒有帳號？<a href="/auth/register">註冊</a></div>
  </div>
</div>

{% elif page == 'register' %}
<!-- ── Register Page ── -->
<div class="auth-wrap">
  <div class="auth-card card">
    <div class="auth-logo"><svg width="26" height="26" viewBox="0 0 26 26" aria-hidden="true"><circle cx="13" cy="13" r="11" fill="none" stroke="currentColor" stroke-width="2"/><g stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><path d="M13 3.5v19M3.5 13h19M6.3 6.3l13.4 13.4M19.7 6.3 6.3 19.7"/></g><circle cx="13" cy="13" r="3.4" fill="var(--panel)" stroke="currentColor" stroke-width="2"/></svg><span>ZeroTouch</span></div>
    <div class="auth-title">建立帳號</div>
    <div class="auth-sub">註冊後就能用對話部署第一個服務</div>
    {% if error %}<div class="auth-error">{{ error }}</div>{% endif %}
    <form method="POST" action="/auth/register">
      <div class="form-group">
        <label for="reg-username">帳號</label>
        <input id="reg-username" type="text" name="username" autocomplete="username" required autofocus>
      </div>
      <div class="form-group">
        <label for="reg-password">密碼</label>
        <input id="reg-password" type="password" name="password" autocomplete="new-password" required>
      </div>
      <div class="form-group">
        <label for="reg-confirm">再輸入一次密碼</label>
        <input id="reg-confirm" type="password" name="confirm" autocomplete="new-password" required>
      </div>
      <button class="btn-primary" type="submit">建立帳號</button>
    </form>
    {% if google_login_available %}
    <div class="auth-divider"><div class="line"></div><span>或</span><div class="line"></div></div>
    <a href="/auth/google/login" class="google-btn">
      <svg width="18" height="18" viewBox="0 0 18 18"><path fill="#4285F4" d="M17.64 9.2c0-.64-.06-1.25-.16-1.84H9v3.48h4.84c-.21 1.13-.84 2.09-1.8 2.73v2.27h2.92c1.7-1.57 2.68-3.88 2.68-6.64z"/><path fill="#34A853" d="M9 18c2.43 0 4.47-.8 5.96-2.18l-2.92-2.27c-.8.54-1.84.86-3.04.86-2.34 0-4.32-1.58-5.03-3.71H.96v2.34C2.44 15.98 5.48 18 9 18z"/><path fill="#FBBC05" d="M3.97 10.7c-.18-.54-.28-1.11-.28-1.7s.1-1.16.28-1.7V4.96H.96A8.996 8.996 0 000 9c0 1.45.35 2.83.96 4.04l3.01-2.34z"/><path fill="#EA4335" d="M9 3.58c1.32 0 2.51.45 3.44 1.35l2.59-2.59C13.46.89 11.43 0 9 0 5.48 0 2.44 2.02.96 4.96l3.01 2.34C4.68 5.16 6.66 3.58 9 3.58z"/></svg>
      使用 Google 帳號登入
    </a>
    {% endif %}
    <div class="auth-link">已經有帳號了？<a href="/">登入</a></div>
  </div>
</div>

{% else %}
<!-- ── Main App ── -->
<div class="layout">
  <!-- 左側選單：固定不跟著內容捲動 -->
  <aside class="sidebar" aria-label="主選單">
    <div class="sidebar-logo"><svg width="26" height="26" viewBox="0 0 26 26" aria-hidden="true"><circle cx="13" cy="13" r="11" fill="none" stroke="currentColor" stroke-width="2"/><g stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><path d="M13 3.5v19M3.5 13h19M6.3 6.3l13.4 13.4M19.7 6.3 6.3 19.7"/></g><circle cx="13" cy="13" r="3.4" fill="var(--panel)" stroke="currentColor" stroke-width="2"/></svg><span>ZeroTouch</span></div>
    <div class="new-chat-wrap"><button class="new-chat-btn" type="button" onclick="newChat()"><svg viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="M8 3v10M3 8h10" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>新對話</button></div>
    <nav class="sidebar-nav" aria-label="功能">
      <button class="nav-item active" type="button" data-page="chat" onclick="showPage('chat')"><svg viewBox="0 0 16 16" fill="none"><path d="M2.5 3.5a2 2 0 012-2h7a2 2 0 012 2v5a2 2 0 01-2 2H8l-3.5 3v-3a2 2 0 01-2-2v-5z" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/></svg>對話</button>
      <button class="nav-item" type="button" data-page="services" onclick="showPage('services')"><svg viewBox="0 0 16 16" fill="none"><rect x="2" y="2.5" width="12" height="4" rx="1" stroke="currentColor" stroke-width="1.5"/><rect x="2" y="9.5" width="12" height="4" rx="1" stroke="currentColor" stroke-width="1.5"/></svg>我的服務</button>
      <button class="nav-item" type="button" data-page="healer" onclick="showPage('healer')"><svg viewBox="0 0 16 16" fill="none"><path d="M1.5 8.5h3l1.5-4 3 7 1.5-3h4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>自動修復</button>
      <button class="nav-item" type="button" data-page="calendar" onclick="showPage('calendar')"><svg viewBox="0 0 16 16" fill="none"><rect x="2" y="3" width="12" height="11" rx="1.5" stroke="currentColor" stroke-width="1.5"/><path d="M2 6.5h12M5.5 1.5v3M10.5 1.5v3" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>行事曆<span class="nav-badge" id="nav-due-badge" hidden></span></button>
      <button class="nav-item" type="button" data-page="dashboard" onclick="showPage('dashboard')"><svg viewBox="0 0 16 16" fill="none"><path d="M2.5 11a5.5 5.5 0 1111 0" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/><path d="M8 11l2.6-3.2" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>資源總覽</button>
      <button class="nav-item" type="button" data-page="gitops" onclick="showPage('gitops')"><svg viewBox="0 0 16 16" fill="none"><circle cx="5" cy="4" r="2" stroke="currentColor" stroke-width="1.5"/><circle cx="11" cy="12" r="2" stroke="currentColor" stroke-width="1.5"/><circle cx="11" cy="4" r="2" stroke="currentColor" stroke-width="1.5"/><path d="M5 6v1a3 3 0 003 3h1M11 6v2" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>部署紀錄</button>
      <div class="nav-section proj-section"><span>專案</span><button type="button" class="proj-add" onclick="openProjectModal()" data-tip="新增專案" aria-label="新增專案">+</button></div>
      <div id="project-list"></div>
      <div class="nav-section">最近的對話</div>
      <div id="chat-room-list"></div>
    </nav>
    <div class="sys-panel" aria-label="系統狀態">
      <div class="status-pill"><div class="dot {% if k8s %}green{% else %}red{% endif %}" id="k8s-dot"></div><span>Kubernetes</span><span class="val" id="k8s-status">{% if k8s %}已連線{% else %}未連線{% endif %}</span></div>
      <div class="status-pill"><div class="dot" id="model-dot"></div><span>AI 模型</span><span class="val" id="model-status">檢查中</span></div>
    </div>
    <div class="sidebar-footer">
      <div class="user-info">
        <div class="user-avatar">{{ username[0].upper() }}</div>
        <div class="user-name">{{ username }}{% if is_admin %}<span class="admin-badge">管理員</span>{% endif %}</div>
        <form method="POST" action="/auth/logout" style="margin:0">
          <button class="logout-btn" type="submit">登出</button>
        </form>
      </div>
    </div>
  </aside>

  <div class="main">
    <div class="status-bar">
      <div class="topbar-title" id="topbar-title">對話</div>
      <div class="topbar-tools">
        <span id="clock"></span>
        <button class="icon-btn" type="button" onclick="openManual()">說明書</button>
        <button class="icon-btn" type="button" id="theme-btn" onclick="toggleTheme()">淺色</button>
      </div>
    </div>

    <!-- 整條警告橫幅：AI 模型沒在執行、Docker 或 Kubernetes 連不上時，直接講原因跟怎麼做 -->
    <div class="alert-banner stop" id="model-warning" role="alert">
      <div class="alert-mark"></div>
      <div><div class="alert-title" id="model-warning-title"></div><div class="alert-body" id="model-warning-body"></div></div>
    </div>
    <div class="alert-banner stop" id="docker-warning" role="alert">
      <div class="alert-mark"></div>
      <div><div class="alert-title" id="docker-warning-title"></div><div class="alert-body" id="docker-warning-body"></div></div>
    </div>

    <!-- 我的服務：服務（Deployment）與 Pod 用跟對話裡同一套模組 -->
    <div class="page" id="page-services">
      <div class="page-head"><div><h1 class="page-title">我的服務</h1>
        <p class="page-sub">你部署的每個服務，以及它底下實際在跑的 Pod。點任一列可以看詳情，服務還可以擴縮、更新映像檔、回滾或刪除。</p></div>
        <div class="page-actions"><button type="button" class="btn primary" data-act="example" data-text="部署 2 個 nginx:1.27，開 80 port">部署新服務</button></div></div>
      <div class="page-stack"><div id="svc-deps"></div><div id="svc-autoscale"></div><div id="svc-pods"></div></div>
    </div>

    <!-- Chat Page -->
    <div class="page" id="page-project">
      <div class="page-head"><div><h1 class="page-title" id="proj-title">專案</h1>
        <p class="page-sub" id="proj-sub"></p></div>
        <div class="page-actions"><button type="button" class="btn primary" id="proj-newchat-btn" onclick="projectNewChat()">在這個專案開始新對話</button></div></div>
      <div class="proj-tabs" role="tablist" aria-label="專案分頁">
        <button type="button" role="tab" data-ptab="overview" onclick="setProjTab('overview')">總覽</button>
        <button type="button" role="tab" data-ptab="summary" onclick="setProjTab('summary')">摘要</button>
        <button type="button" role="tab" data-ptab="services" onclick="setProjTab('services')">服務</button>
        <button type="button" role="tab" data-ptab="schedule" onclick="setProjTab('schedule')">排程</button>
        <button type="button" role="tab" data-ptab="files" onclick="setProjTab('files')">檔案</button>
        <button type="button" role="tab" data-ptab="settings" onclick="setProjTab('settings')">設定</button>
      </div>
      <div id="proj-body"></div>
    </div>

    <div class="page active" id="page-chat" style="padding:0;overflow:hidden">
      <div class="chat-shell">
        <div class="chat-messages" id="chat-messages"></div>
        <div class="chat-composer">
          <div class="composer-box">
            <label for="chat-input" style="position:absolute;left:-9999px">輸入訊息</label>
            <textarea class="chat-input" id="chat-input" placeholder="說說你想部署什麼，或問服務的狀況" rows="1" oninput="autoGrowChatInput(this)" onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();sendChat()}"></textarea>
            <input type="file" id="compose-file" accept=".yml,.yaml,text/yaml,application/x-yaml" hidden onchange="onComposeFile(this)">
            <button class="chat-attach" type="button" onclick="document.getElementById('compose-file').click()" aria-label="上傳 docker-compose 或 Kubernetes YAML 檔" data-tip="上傳 docker-compose 或 Kubernetes YAML 檔"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12.5 12.6 20.9a5.5 5.5 0 0 1-7.8-7.8l8.5-8.5a3.7 3.7 0 0 1 5.2 5.2l-8.5 8.5a1.8 1.8 0 0 1-2.6-2.6l7.8-7.8"/></svg></button>
            <span class="mic-meter" id="mic-meter" aria-hidden="true"><i></i><i></i><i></i><i></i></span>
            <button class="chat-mic" type="button" id="chat-mic" onclick="toggleVoice()" aria-label="語音輸入" data-tip="語音輸入"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3"/></svg></button>
            <button class="chat-send" type="button" onclick="sendChat()" id="chat-send-txt">送出</button>
          </div>
          <div class="composer-hint" id="composer-hint-txt">部署、擴縮、刪除之前都會先讓你確認。</div>
        </div>
      </div>
    </div>

    <!-- 使用說明書 + K8s 小科普：新手友善原則（AGENT_RULES.md），點擊展開段落、
         點名詞看小框框註解，不用另外開分頁或去查資料。 -->
    <div id="manual-overlay" onclick="if(event.target===this)closeManual()">
      <div class="manual-panel">
        <div class="manual-panel-head">
          <div>
            <div class="manual-panel-title" id="manual-title">使用說明書</div>
            <div class="manual-panel-sub" id="manual-sub">有虛線底線的名詞，點一下可以看簡單說明</div>
          </div>
          <button class="manual-close" type="button" onclick="closeManual()">關閉</button>
        </div>
        <div class="manual-panel-body" id="manual-body"></div>
      </div>
      <div id="term-tip" hidden></div>
    </div>

    <!-- 通用是/否確認彈窗：部署第一步偵測到名稱已存在或 port 被佔用時用，
         askConfirm() 回傳 Promise<boolean>，跟 startClarify 那種純文字提示不同，
         這個會真的擋住流程等使用者按下按鈕。 -->
    <div id="confirm-dialog-overlay" style="display:none;z-index:300">
      <div class="confirm-dialog">
        <div class="confirm-dialog-title" id="confirm-dialog-title"></div>
        <div class="confirm-dialog-body" id="confirm-dialog-body"></div>
        <div class="confirm-dialog-actions">
          <button class="deploy-confirm-btn" type="button" id="confirm-dialog-no">否</button>
          <button class="deploy-confirm-btn primary" type="button" id="confirm-dialog-yes">是</button>
        </div>
      </div>
    </div>

    <!-- 專案：新增專案、刪除專案的卡片（內容由 JS 填） -->
    <div class="modal-bg" id="proj-modal-bg" onclick="if(event.target===this) closeProjModal()">
      <div class="modal proj-modal" role="dialog" aria-modal="true" id="proj-modal"></div>

    <!-- 預約時間到了：打開網站或網站開著時跳出來，使用者按「確定執行」才會執行 -->
    <div id="due-overlay" class="due-overlay" hidden role="dialog" aria-modal="true" aria-labelledby="due-title">
      <div class="due-dialog">
        <div class="due-head"><h2 id="due-title">⏰ 預約時間到了</h2>
          <p>下面的預約已經到了你約定的時間。<b>系統不會自動執行</b>，請確認現在要不要做；部署會再審查一次，因為叢集狀況可能已經變了。</p></div>
        <div id="due-list"></div>
        <div class="due-foot"><button type="button" class="btn" data-act="due-close">全部稍後再說</button></div>
      </div>
    </div>

    <div class="page" id="page-calendar">
      <div class="page-head"><div><h1 class="page-title">行事曆</h1>
        <p class="page-sub">預約的操作、平台做過的事、你的備註都在這裡。在對話裡說「10 月 25 日下午 3 點部署 2 個 nginx」，或在確認卡上按「改成預約」就能預約；時間到了會提醒你，<b>確認後才會執行</b>。</p></div></div>
      <div class="cal-wrap">
        <section class="module cal-month">
          <div class="module-head"><button type="button" class="btn btn-sm" data-act="cal-prev" aria-label="上個月">‹</button>
            <h2 id="cal-title">行事曆</h2><button type="button" class="btn btn-sm" data-act="cal-next" aria-label="下個月">›</button>
            <button type="button" class="btn btn-sm" data-act="cal-today">今天</button>
            <span class="meta cal-legend"><span><i class="mk task"></i>預約</span><span><i class="mk due"></i>等你確認</span><span><i class="mk dep"></i>部署／變更</span><span><i class="mk bad"></i>修復／擋下</span><span><i class="mk note"></i>備註</span></span></div>
          <div class="cal-grid" id="cal-grid" role="grid"></div>
        </section>
        <section class="module cal-day"><div class="module-head"><h2 id="cal-day-title">選一天</h2></div><div id="cal-day-body" class="cal-day-body"></div></section>
      </div>
    </div>

    <div class="page" id="page-gitops">
      <div class="page-head"><div><h1 class="page-title">部署紀錄</h1>
        <p class="page-sub">每一次部署都會寫進 Git，留下可以追查的版本；連上你自己的叢集時，擴縮、更新、回滾也都會寫進來。每個服務最新的那一筆可以回滾到上一版，回滾前會先請你確認。</p></div></div>
      <div class="page-stack"><div id="gitops-w"></div></div>
    </div>

    <div class="page" id="page-healer">
      <div class="page-head"><div><h1 class="page-title">自動修復</h1>
        <p class="page-sub">Healer 每 30 秒在背景檢查一次你的 Pod。發現問題會先找出原因，再套用對應的修復，例如調高記憶體上限或恢復上一個能用的映像檔，不只是把 Pod 刪掉重來。</p></div></div>
      <div class="page-stack">
        <div class="status-strip" id="heal-bg" role="status"><span class="lamp-s"></span><span>讀取中</span></div>
        <section class="module" id="push-panel"><div class="module-head"><h2>手機通知</h2><span class="meta"><span>Healer 發現問題或修好時通知你</span></span></div>
          <div id="push-body"></div></section>
        <div id="heal-processing"></div>
        <div id="heal-scan"></div>
        <section class="module"><div class="module-head"><h2>自動修復紀錄</h2><span class="meta"><span>最近的處理結果，最新的在最上面</span></span></div>
          <div id="heal-history"></div></section>
      </div>
    </div>

    <div class="page" id="page-dashboard">
      <div class="page-head"><div><h1 class="page-title">資源總覽</h1>
        <p class="page-sub">部署前先看看叢集還剩多少空間。數字每 30 秒自動更新；同一時間可能有其他使用者在部署，送出部署時的審查結果才是準的。</p></div></div>
      <div class="page-stack">
        <div id="dash-res"></div>
        <div class="grid-2">
          <section class="module"><div class="module-head"><h2>CPU 用量趨勢</h2><span class="meta"><span>過去 30 分鐘，單位：核</span></span></div><div class="chart-box" id="dash-cpu-trend"></div></section>
          <section class="module"><div class="module-head"><h2>記憶體用量趨勢</h2><span class="meta"><span>過去 30 分鐘，單位：MiB</span></span></div><div class="chart-box" id="dash-mem-trend"></div></section>
        </div>
        <section class="module"><div class="module-head"><h2>監控元件</h2><span class="meta"><span>用量數字從哪裡來</span></span></div><dl class="facts" id="dash-mon"></dl></section>
      </div>
    </div>

  </div>
</div>

<!-- 對話模組點進去的詳情抽屜（2026-10-09 改版第二階段） -->
<div class="zt-scrim" id="zt-scrim" data-act="close-drawer"></div>
<aside class="zt-drawer" id="zt-drawer" aria-hidden="true" aria-labelledby="zt-drawer-title">
  <header><div style="min-width:0"><div class="k" id="zt-drawer-kind"></div><h3 id="zt-drawer-title"></h3></div>
    <button type="button" class="btn btn-sm" data-act="close-drawer">關閉</button></header>
  <div class="body" id="zt-drawer-body"></div>
</aside>

{% endif %}

<script>
// ── Auth guard ──
const loggedIn = {{ 'true' if logged_in else 'false' }};
const k8sEnabled = {{ 'true' if k8s else 'false' }};
// 2026-09-15：Chat 歷史用 localStorage 存，是跟著瀏覽器走、不是跟著登入帳號走——
// 同一台瀏覽器換帳號登入會看到上一個帳號的聊天記錄。用 CURRENT_USER 把 key 綁定到
// 帳號，換帳號登入時就不會看到別人（或自己上一個帳號）的聊天內容。
const CURRENT_USER = "{{ username }}";
const IS_ADMIN = {{ 'true' if is_admin else 'false' }};

// ══════════════════════════════════════════════════════════════════
//  中英切換（Chat 分頁 + 使用說明書）。專有名詞（Pod/Deployment/...）
//  兩種語言都固定用英文，只有說明文字跟著切換。
// ══════════════════════════════════════════════════════════════════
// 2026-10-09 改版：介面只用中文（專有名詞與系統名稱用英文），不再提供語言切換
let uiLang = 'zh';

const I18N = {
  zh: {
    langBtn: 'EN',
    manualBtn: '說明書',
    chatTitle: 'ZeroTouch K8s 助理',
    chatSubtitle: '聊天、部署、檢視並修復 Kubernetes 服務',
    workspaceReady: '系統就緒',
    sendBtn: '送出',
    composerHint: '部署、擴縮、刪除之前都會先讓你確認。',
    emptyTitle: '想部署什麼，或想知道服務哪裡出了問題？',
    emptyDesc: '直接用中文說。部署前會先列出規格、跑完安全、成本、效能三項審查，你確認後才會真的部署。',
    cards: [
      {title:'部署服務', example:'部署 2 個 nginx:1.27，開 80 port', send:'部署 2 個 nginx:1.27，開 80 port'},
      {title:'查看我的 Pod', example:'列出我所有的 Pod', send:'列出我所有的 Pod'},
      {title:'學 Kubernetes', example:'Deployment 跟 Service 差在哪', send:'Deployment 跟 Service 差在哪？'},
      {title:'排查問題', example:'Pod 一直 CrashLoopBackOff 怎麼辦', send:'Pod 一直 CrashLoopBackOff 怎麼辦？'},
    ],
    manualTitle: '使用說明書',
    manualSub: '有虛線底線的名詞，點一下可以看簡單說明',
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
    manualTitle: 'User Guide',
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
    <summary>三步驟上手</summary>
    <div class="manual-section">
      <ol style="margin:0;padding-left:20px;line-height:1.9">
        <li>在「對話」用中文說你想部署什麼，例如「<code>部署 2 個 nginx:1.27，開 80 port</code>」。系統會整理成一張部署單讓你確認，有不對的地方可以直接改。</li>
        <li>按「下一步：審查」，系統會算出這次要用多少資源，並跑完安全、成本、效能三項審查。通過才會出現「開始部署」。</li>
        <li>部署後，部署單會顯示實際進度，副本真的就緒才會顯示「運作中」。之後可以到「我的服務」查看。</li>
      </ol>
    </div>
  </details>

  <details>
    <summary>對話還能幫你做什麼</summary>
    <div class="manual-section">
      <p style="margin:0 0 8px">除了部署，也可以直接在對話裡問：</p>
      <ul style="margin:0;padding-left:20px;line-height:2">
        <li><code>列出我所有的 Pod</code>、<code>列出所有部署</code>：用表格列出來，可以篩選、排序、換頁，點任一列看詳情</li>
        <li><code>web-frontend 有沒有壞</code>：檢查單一服務的健康狀態，有狀況時附上 Healer 的診斷</li>
        <li><code>把 web-frontend 擴到 5 個</code>：調整副本數，執行前會先請你確認</li>
        <li><code>web-frontend 回滾</code>：回到上一版設定</li>
        <li><code>叢集狀態</code>：看 CPU、記憶體還剩多少</li>
        <li><code>Deployment 跟 Service 差在哪</code>：任何 Kubernetes 的觀念問題</li>
      </ul>
    </div>
  </details>

  <details>
    <summary>分時擴縮與休眠：沒人用的時候自動省資源</summary>
    <div class="manual-section">
      <ul style="margin:0;padding-left:20px;line-height:2">
        <li><code>shop-web 白天 3 個、晚上 1 個</code>：照時段自動調整副本數，確認卡會列出時段表和每月能省多少。也可以說「9 點到 18 點 3 個，其他時間 1 個」「平日 2 個，週末 1 個」。</li>
        <li><b>休眠</b>：服務連續 5 天沒有人連線，就把副本數降到 0，不佔資源。平台看的是實際收到的流量，健康檢查本身的流量不算。</li>
        <li><b>自動喚醒</b>：休眠中有人連進來，會先看到「服務正在喚醒」，通常幾秒到 1 分鐘就恢復原本的副本數。也可以說 <code>喚醒 shop-web</code>。</li>
        <li><b>例外清單</b>：<code>shop-web 不要休眠</code> 把它加進例外，<code>shop-web 可以休眠</code> 移出例外。</li>
        <li><code>閒置 3 天就休眠</code>：改天數（1 到 7 天）。<code>休眠設定</code>：看每個服務的狀態、排程與例外，也可以直接按按鈕操作。</li>
      </ul>
    </div>
  </details>

  <details>
    <summary>左側選單各頁面</summary>
    <div class="manual-section">
      <ul style="margin:0;padding-left:20px;line-height:2">
        <li><b>對話</b>：部署、查詢、修復都可以用說的完成。</li>
        <li><b>我的服務</b>：你部署的每個服務，以及底下的 <span class="term" data-def="Kubernetes 中最小的可部署運算單位，用於封裝並執行你的應用程式容器。">Pod</span>。點進去可以擴縮、更新映像檔、回滾或刪除。</li>
        <li><b>自動修復</b>：Healer 每 30 秒檢查一次，發現像 <span class="term" data-def="容器持續啟動失敗並反覆重啟，通常代表應用程式本身發生錯誤或設定有誤。">CrashLoopBackOff</span> 這類問題會先找原因，再套用對應的修復。這裡可以看處理紀錄。</li>
        <li><b>資源總覽</b>：叢集還剩多少 CPU 和記憶體、用量趨勢，以及依公有雲牌價估算的費用。</li>
        <li><b>部署紀錄</b>：每一次部署、擴縮、回滾都會寫進 Git，可以從這裡回滾到上一版。</li>
      </ul>
    </div>
  </details>

  <details>
    <summary>K8s 小百科</summary>
    <div class="manual-section">
      <p style="margin:0 0 10px;color:var(--text2)">每個名詞都可以點一下看簡單說明。</p>
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
        <span class="term" data-def="將服務退回至前一個穩定版本。在對話輸入「服務名稱 回滾」即可執行。">Rollback</span>
      </p>
    </div>
  </details>
`;

function renderManualBody(){
  const body = document.getElementById('manual-body');
  if(body) body.innerHTML = MANUAL_ZH;
}

// ── Clock ──
function updateClock(){
  const el = document.getElementById('clock');
  if(el) el.textContent = new Date().toLocaleTimeString('zh-TW', {hour12:false});
}
setInterval(updateClock, 1000);
updateClock();

function autoGrowChatInput(el){
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 180) + 'px';
}

// ── 語音輸入（瀏覽器內建的語音辨識，不用另外裝東西）──
// 辨識完只把文字放進輸入框，不自動送出，讓使用者先看過再按送出。
const HINT_DEFAULT = '部署、擴縮、刪除之前都會先讓你確認。';
let voiceRec = null, voiceBase = '', voiceHeard = false, voiceWait = null;
function setComposerHint(msg, cls){
  const h = document.getElementById('composer-hint-txt');
  if(!h) return;
  h.textContent = msg || HINT_DEFAULT;
  h.className = 'composer-hint' + (cls ? ' ' + cls : '');
}
function voiceUI(on){
  const b = document.getElementById('chat-mic'), m = document.getElementById('mic-meter');
  if(b){ b.classList.toggle('listening', on); b.setAttribute('aria-label', on ? '停止語音輸入' : '語音輸入'); b.dataset.tip = on ? '停止語音輸入' : '語音輸入'; }
  if(m) m.classList.toggle('on', on);
}
const VOICE_ERRORS = {
  'not-allowed':'麥克風權限被擋下了。請點網址列左邊的圖示，把麥克風改成允許，再按一次麥克風。',
  'service-not-allowed':'麥克風權限被擋下了。請點網址列左邊的圖示，把麥克風改成允許，再按一次麥克風。',
  'no-speech':'沒有聽到聲音。請靠近麥克風，按一下再說一次。',
  'audio-capture':'找不到麥克風。請確認麥克風有接上，或在系統設定裡選對輸入裝置。',
  'network':'語音辨識需要連上網路，瀏覽器會把聲音送到它的辨識服務轉成文字。請確認網路後再試一次。',
  'language-not-supported':'這個瀏覽器不支援中文語音辨識，請改用 Chrome 或 Edge。',
};
function toggleVoice(){
  if(voiceRec){ voiceRec.stop(); return; }
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if(!window.isSecureContext){ setComposerHint('語音輸入只能在 https 網址或本機網址使用。', 'voice-err'); return; }
  if(!SR){ setComposerHint('這個瀏覽器不支援語音輸入，請改用 Chrome 或 Edge，或直接打字。', 'voice-err'); return; }
  const inp = document.getElementById('chat-input');
  voiceBase = inp.value ? inp.value.replace(/\s+$/, '') + ' ' : '';
  voiceHeard = false;
  const rec = new SR();
  rec.lang = 'zh-TW';
  rec.interimResults = true;
  rec.continuous = false;
  rec.onstart = () => { clearTimeout(voiceWait); voiceUI(true); setComposerHint('正在聽，說完停一下就會自動結束，也可以再按一次麥克風結束。', 'voice'); };
  rec.onresult = ev => {
    let text = '';
    for(let i = 0; i < ev.results.length; i++) text += ev.results[i][0].transcript;
    voiceHeard = !!text.trim();
    inp.value = voiceBase + text;
    autoGrowChatInput(inp);
  };
  rec.onerror = ev => {
    if(ev.error === 'aborted') return;
    setComposerHint(VOICE_ERRORS[ev.error] || ('語音輸入失敗，請再試一次，或直接打字。（' + ev.error + '）'), 'voice-err');
  };
  rec.onend = () => {
    clearTimeout(voiceWait);
    voiceRec = null; voiceUI(false);
    const h = document.getElementById('composer-hint-txt');
    if(voiceHeard){ setComposerHint('辨識好的文字已經放進輸入框，確認沒問題再按送出。', 'voice'); inp.focus(); }
    else if(h && !h.classList.contains('voice-err')) setComposerHint('沒有聽到聲音。請靠近麥克風，按一下再說一次。', 'voice-err');
  };
  // 第一次使用時瀏覽器會先跳出麥克風權限詢問，這段時間不會有任何事件，要先講清楚在等什麼
  setComposerHint('正在開啟麥克風。如果瀏覽器詢問是否允許使用麥克風，請按允許。', 'voice');
  clearTimeout(voiceWait);
  voiceWait = setTimeout(() => {
    if(voiceRec === rec && !document.getElementById('chat-mic')?.classList.contains('listening')){
      setComposerHint('麥克風還沒開始收音。請看網址列附近有沒有權限詢問，按允許；或點網址列左邊的圖示，把麥克風改成允許後再按一次。', 'voice-err');
    }
  }, 8000);
  try { rec.start(); voiceRec = rec; }
  catch(e){ clearTimeout(voiceWait); voiceRec = null; voiceUI(false); setComposerHint('語音輸入啟動失敗，請再按一次。', 'voice-err'); }
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

// 滑鼠停留提示：取代瀏覽器預設的 title 提示框，外觀跟著網站主題。元素加 data-tip="文字" 就會套用，
// 不要同時留著 title（兩個提示框會一起出現）；建議另外加 aria-label 給螢幕報讀軟體。
(function(){
  let box = null, timer = null, cur = null;
  function hide(){
    clearTimeout(timer); timer = null; cur = null;
    if(box) box.classList.remove('on');
  }
  function show(t){
    if(!box){ box = document.createElement('div'); box.id = 'zt-tip'; box.setAttribute('role', 'tooltip'); document.body.appendChild(box); }
    box.textContent = t.dataset.tip || '';
    const r = t.getBoundingClientRect(), b = box.getBoundingClientRect(), gap = 8;
    let top = r.top - b.height - gap;
    if(top < 4) top = r.bottom + gap;   // 上面空間不夠就放下面
    const left = Math.max(4, Math.min(r.left + r.width / 2 - b.width / 2, window.innerWidth - b.width - 4));
    box.style.left = left + 'px';
    box.style.top = top + 'px';
    box.classList.add('on');
  }
  function arm(t, delay){
    if(cur === t) return;
    hide(); cur = t;
    timer = setTimeout(() => { if(cur === t && document.body.contains(t)) show(t); }, delay);
  }
  document.addEventListener('mouseover', e => {
    const t = e.target.closest && e.target.closest('[data-tip]');
    if(t) arm(t, 300); else if(cur) hide();
  });
  document.addEventListener('mouseout', e => { if(!e.relatedTarget) hide(); });
  document.addEventListener('focusin', e => {
    const t = e.target.closest && e.target.closest('[data-tip]');
    if(t) arm(t, 0);
  });
  document.addEventListener('focusout', hide);
  document.addEventListener('mousedown', hide, true);
  window.addEventListener('scroll', hide, true);
  window.addEventListener('blur', hide);
})();

// ── 深色／淺色切換：記在這台瀏覽器，沒選過就跟著系統設定 ──
function isDarkTheme(){
  const t = document.documentElement.dataset.theme;
  if(t) return t === 'dark';
  return !window.matchMedia('(prefers-color-scheme: light)').matches;
}
function syncThemeBtn(){
  const b = document.getElementById('theme-btn');
  if(b) b.textContent = isDarkTheme() ? '淺色' : '深色';
}
function toggleTheme(){
  const next = isDarkTheme() ? 'light' : 'dark';
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem('zt_theme', next); } catch(e){}
  syncThemeBtn();
}
syncThemeBtn();
try { window.matchMedia('(prefers-color-scheme: light)').addEventListener('change', syncThemeBtn); } catch(e){}

const PAGE_TITLES = {chat:'對話', services:'我的服務', healer:'自動修復', calendar:'行事曆', dashboard:'資源總覽', gitops:'部署紀錄', project:'專案'};
// 舊頁面名稱（書籤、舊卡片上的按鈕）導到合併後的新頁面
const PAGE_ALIAS = {pods:'services', deployments:'services', metrics:'dashboard', deploy:'chat', dataset:'chat', kb:'chat'};

// ── Page nav ──
function showPage(name){
  name = PAGE_ALIAS[name] || name;
  window.__page = name;
  const tt = document.getElementById('topbar-title');
  if(tt) tt.textContent = PAGE_TITLES[name] || '';
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  const page = document.getElementById('page-' + name);
  if(page) page.classList.add('active');
  const nav = document.querySelector(`.nav-item[data-page="${name}"]`);
  if(nav) nav.classList.add('active');

  if(name === 'chat') { initChats(); setTimeout(()=>document.getElementById('chat-input')?.focus(), 0); }
  if(name === 'services') loadServicesPage();
  if(name === 'gitops') loadGitopsPage();
  if(name === 'healer') loadHealerPage();
  if(name === 'calendar') loadCalendarPage();
  if(name === 'dashboard') loadDashboardPage();
  if(name === 'project') loadProjectPage();
  renderProjectList();
}

// ── Status polling ──
async function pollStatus(){
  try {
    const r = await fetch('/api/status');
    const d = await r.json();
    const dot = document.getElementById('model-dot');
    const txt = document.getElementById('stat-model');
    const ms  = document.getElementById('model-status');
    // 三種狀態分開講：就緒、正在載入、根本沒在執行（2026-10-09 之前沒在執行也顯示「載入中」，新手會一直等）
    const mWarn = document.getElementById('model-warning');
    if(d.model_ready){
      dot.className = 'dot green';
      if(ms) ms.textContent = '就緒';
      if(txt) txt.textContent = '就緒';
      if(mWarn) mWarn.classList.remove('show');
    } else if(d.model_loading){
      dot.className = 'dot yellow';
      if(ms) ms.textContent = '載入中';
      if(txt) txt.textContent = '載入中';
      if(mWarn){
        mWarn.className = 'alert-banner caution show';
        document.getElementById('model-warning-title').textContent = 'AI 模型正在載入，大約需要一分鐘。';
        document.getElementById('model-warning-body').textContent = '載入完成後這則提示會自動消失。這段時間一般問答會先用簡易回答。';
      }
    } else {
      dot.className = 'dot red';
      if(ms) ms.textContent = '未執行';
      if(txt) txt.textContent = '未執行';
      if(mWarn){
        mWarn.className = 'alert-banner stop show';
        document.getElementById('model-warning-title').textContent = 'AI 模型伺服器沒有在執行。';
        document.getElementById('model-warning-body').innerHTML = IS_ADMIN
          ? '一般問答會改用簡易回答，部署仍然可以用。請在專案資料夾開一個 PowerShell，執行 <code>python core/model_server.py</code>，大約一分鐘後這則提示會自動消失。'
          : '一般問答會改用簡易回答，部署仍然可以用。平台管理員重新啟動模型後，這則提示會自動消失。';
      }
    }

    const k8sDot = document.getElementById('k8s-dot');
    const k8sTxt = document.getElementById('k8s-status');
    if(k8sDot) k8sDot.className = 'dot ' + (d.k8s ? 'green' : 'red');
    if(k8sTxt) k8sTxt.textContent = d.customer_cluster
      ? (d.k8s ? '你的叢集已連線' : '你的叢集離線')
      : (d.k8s ? '已連線' : '未連線');

    // 新手警告：Docker 沒開 / 開了但 K8s 連不上，兩種訊息分開講清楚原因跟解法
    const warn = document.getElementById('docker-warning');
    const wTitle = document.getElementById('docker-warning-title');
    const wBody = document.getElementById('docker-warning-body');
    if(warn){
      if(d.customer_cluster){
        // 客戶自帶叢集（2026-10-09）：平台自己的 Docker/K8s 跟客戶無關，看的是客戶叢集的回報
        const cc = d.customer_cluster;
        if(cc.ok){
          warn.className = 'alert-banner stop';
        } else if(cc.code === 'awaiting_report'){
          // 平台剛重啟、還在等第一份回報：黃色提示，不是紅色警報
          wTitle.textContent = cc.title; wBody.innerHTML = escHtml(cc.howto || '');
          warn.className = 'alert-banner caution show';
        } else {
          wTitle.textContent = cc.title || '你的叢集目前離線';
          wBody.innerHTML = escHtml(cc.howto || '') + '<br>離線期間不能部署或修改服務，已經在跑的服務不受影響。';
          warn.className = 'alert-banner stop show';
        }
      } else if(!d.docker_running){
        wTitle.textContent = 'Docker 沒有啟動';
        wBody.innerHTML = (d.docker_message ? escHtml(d.docker_message)+'<br>' : '') +
          '請先開啟 Docker Desktop，等它完全啟動後重新整理這個頁面。';
        warn.className = 'alert-banner stop show';
      } else if(!d.k8s){
        wTitle.textContent = 'Docker 已啟動，但 Kubernetes 連不上';
        wBody.innerHTML = '請到 Docker Desktop 的 Settings 裡打開 Kubernetes 頁籤，勾選 Enable Kubernetes。啟動大約需要 1 到 2 分鐘。';
        warn.className = 'alert-banner stop show';
      } else {
        warn.className = 'alert-banner stop';
      }
    }
  } catch(e){}
}
if(loggedIn){ pollStatus(); setInterval(pollStatus, 4000); }
// 2026-10-03：使用者要求趨勢折線圖「要會動」——原本只在切換到 Dashboard 頁那一刻
// 抓一次資料，圖畫完就不會再更新，使用者留在頁面上看不到新資料點進來。改成每 30 秒
// 自動重撈一次，但只在 Dashboard 頁真的是目前作用中的分頁時才打 API（切到別頁就不會
// 浪費請求），跟 Prometheus 本身 60 秒一個取樣點的頻率對齊，半分鐘內一定會看到新點。
if(loggedIn){
  setInterval(() => {
    if(document.getElementById('page-dashboard')?.classList.contains('active')) loadDashboardPage(true);
  }, 30000);
}
// 2026-10-03：使用者要求「自癒系統在修復時要有轉圈動畫告訴使用者」——
// `loadHealerBgStatus()` 以前只在切進 Healer 頁那一刻打一次，使用者留在頁面上
// 看不到「正在修復中」狀態的變化（根因診斷打本地模型要 1-3 分鐘，使用者應該要
// 能親眼看著它從「正在修復」變成「修好了」，不用自己手動按重新整理）。這支 API
// 只讀記憶體內的狀態，不會連 K8s，5 秒一次的頻率不會造成負擔。
if(loggedIn){
  setInterval(() => {
    if(document.getElementById('page-healer')?.classList.contains('active')) loadHealerBg();
  }, 5000);
}

let chatHistory = [];


function _fmtChartTime(iso){
  // 背景取樣每 30 秒一次，只顯示到分鐘的話，資料還沒累積超過 1 分鐘時左右兩個
  // 時間標籤會顯示成一模一樣的字串（看起來像時間軸沒在動，但其實只是同一分鐘內
  // 的兩個取樣點）——補上秒數讓標籤能反映真實的取樣間隔。
  try{ return new Date(iso).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit', second:'2-digit'}); }
  catch(e){ return ''; }
}

function renderRestartChart(history){
  if(!history || history.length < 2){
    return '<div class="pod-chart-empty">還在累積資料，背景每 30 秒取樣一次，有兩筆以上就會畫出趨勢。</div>';
  }
  const w=560, h=100, pad=24;
  const restarts = history.map(s => s.restarts||0);
  const maxR = Math.max(1, ...restarts);
  const stepX = (w - pad*2) / (history.length - 1);
  const pts = restarts.map((v,i) => [pad + i*stepX, h - pad - (v/maxR)*(h-pad*2)]);
  const path = pts.map((p,i) => (i===0?'M':'L') + p[0].toFixed(1) + ',' + p[1].toFixed(1)).join(' ');
  return `<svg viewBox="0 0 ${w} ${h}" style="width:100%;height:${h}px;display:block">
    <path d="${path}" fill="none" stroke="var(--signal)" stroke-width="2"/>
    ${pts.map(p=>`<circle cx="${p[0].toFixed(1)}" cy="${p[1].toFixed(1)}" r="2.5" fill="var(--signal)"/>`).join('')}
    <text x="${pad}" y="${h-4}" font-size="10" fill="var(--text-3)">${escHtml(_fmtChartTime(history[0].t))}</text>
    <text x="${w-pad}" y="${h-4}" font-size="10" fill="var(--text-3)" text-anchor="end">${escHtml(_fmtChartTime(history[history.length-1].t))}</text>
    <text x="${w-pad}" y="14" font-size="11" fill="var(--signal)" text-anchor="end">目前 ${restarts[restarts.length-1]} 次</text>
  </svg>`;
}

// 2026-10-03：通用版折線圖，給 Dashboard 的 CPU/記憶體用量趨勢用——跟
// renderRestartChart() 同一套畫法（inline SVG，不用額外的圖表套件），只是改成
// 接受 {t, v} 格式的點陣列跟自訂顏色/單位，不綁死在「重啟次數」這個情境。
function renderTrendChart(points, color, unit){
  if(!points || points.length < 2){
    return '<div class="pod-chart-empty">資料還不夠。Prometheus 每分鐘取樣一次，再等幾分鐘就會畫出趨勢。</div>';
  }
  const w=560, h=110, pad=26;
  const vals = points.map(p => p.v||0);
  const maxV = Math.max(0.001, ...vals);
  const stepX = (w - pad*2) / (points.length - 1);
  const pts = vals.map((v,i) => [pad + i*stepX, h - pad - (v/maxV)*(h-pad*2)]);
  const path = pts.map((p,i) => (i===0?'M':'L') + p[0].toFixed(1) + ',' + p[1].toFixed(1)).join(' ');
  const last = vals[vals.length-1];
  return `<svg viewBox="0 0 ${w} ${h}" style="width:100%;height:${h}px;display:block">
    <path d="${path}" fill="none" stroke="${color}" stroke-width="2"/>
    ${pts.map(p=>`<circle cx="${p[0].toFixed(1)}" cy="${p[1].toFixed(1)}" r="2" fill="${color}"/>`).join('')}
    <text x="${pad}" y="${h-6}" font-size="10" fill="var(--text-3)">${escHtml(_fmtChartTime(points[0].t))}</text>
    <text x="${w-pad}" y="${h-6}" font-size="10" fill="var(--text-3)" text-anchor="end">${escHtml(_fmtChartTime(points[points.length-1].t))}</text>
    <text x="${w-pad}" y="16" font-size="11" fill="${color}" text-anchor="end">目前 ${last}${unit}</text>
  </svg>`;
}

function renderHealthTimeline(history){
  if(!history || history.length < 2){
    return '<div class="pod-chart-empty">還在累積資料，背景每 30 秒取樣一次，有兩筆以上就會畫出時間軸。</div>';
  }
  const w=560, h=28;
  const segW = w / history.length;
  const rects = history.map((s,i) => {
    const healthy = s.total>0 && s.ready===s.total && s.phase==='Running';
    return `<rect x="${(i*segW).toFixed(1)}" y="0" width="${Math.ceil(segW)}" height="${h}" fill="${healthy?'var(--pass)':'var(--stop)'}"/>`;
  }).join('');
  return `<svg viewBox="0 0 ${w} ${h}" style="width:100%;height:${h}px;display:block;border-radius:4px;overflow:hidden">${rects}</svg>
    <div style="display:flex;justify-content:space-between;font-size:10px;color:var(--text-3);margin-top:2px">
      <span>${escHtml(_fmtChartTime(history[0].t))}</span><span>${escHtml(_fmtChartTime(history[history.length-1].t))}</span>
    </div>`;
}

// ── Dashboard（2026-09-29 從隊友分支移植；沿用專案既有的靜態雙語標籤慣例，
// 不接入動態語言切換 I18N 系統——Metrics/Pods/Deployments 等頁面本來就是這樣） ──
// 2026-10-03：使用者要求「用量快要不足時變紅色」。統一用這個函式決定警示色，
// 長條圖、百分比文字、已用量/剩餘空間的數字都套用同一套門檻（>=80% 橘色警告、
// >=100% 紅色已超出），不要各處各自判斷、顏色標準兜不起來。
// ── Knowledge Base (RAG) ──
function kbConfColor(conf){
  return conf === 'high' ? 'var(--green)' : (conf === 'medium' ? 'var(--caution)' : 'var(--text3)');
}
function kbConfBadge(conf, score){
  return '<span style="font-size:11px;font-weight:600;color:'+kbConfColor(conf)+'">'+conf.toUpperCase()+' · '+(score*100).toFixed(0)+'%</span>';
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
    projectId: (currentChat() || {}).projectId || null,
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
function _chatsKey(){ return 'k8s_chats_' + CURRENT_USER; }
function _currentChatKey(){ return 'k8s_current_chat_' + CURRENT_USER; }

// 2026-10-21：對話改存伺服器（/api/chats，資料在 data/workspace.db，每個帳號只讀得到自己的），換瀏覽器也看得到。
// 「這台瀏覽器正在看哪一則」(currentChatId) 照舊存 localStorage。舊的 localStorage 對話第一次載入時自動搬上伺服器。
let chatsLoaded = false, _chatsLoading = null, _chatSaveTimer = null, _chatSaveFailed = false;
const _chatSent = {};   // 每則對話最後一次成功存到伺服器的內容，用來判斷哪些有變
function _chatPayload(ch){ return JSON.stringify({title:ch.title, messages:ch.messages, projectId:ch.projectId || null}); }

async function _putChat(ch){
  const body = _chatPayload(ch);
  const r = await fetch('/api/chats/' + encodeURIComponent(ch.id), {method:'PUT', headers:{'Content-Type':'application/json'}, body});
  const d = await r.json().catch(() => ({}));
  if(!r.ok || !d.ok) throw new Error(d.error || ('伺服器回應 ' + r.status));
  _chatSent[ch.id] = body;
}
function _chatSaveWarn(msg){
  if(msg){ _chatSaveFailed = true; setComposerHint(msg, 'voice-err'); }
  else if(_chatSaveFailed){ _chatSaveFailed = false; setComposerHint(''); }
}

async function _loadChatsFromServer(){
  const r = await fetch('/api/chats');
  const d = await r.json().catch(() => ({}));
  if(!r.ok || !d.ok) throw new Error(d.error || ('伺服器回應 ' + r.status));
  let list = (d.chats || []).map(ch => ({id:ch.id, title:ch.title, messages:ch.messages || [], projectId:ch.projectId || null}));
  // 完全沒有訊息的對話直接清掉（2026-10-21：空白對話不列進清單，舊版留下的也一併清理）
  list.filter(ch => !ch.messages.length).forEach(ch => { fetch('/api/chats/' + encodeURIComponent(ch.id), {method:'DELETE'}).catch(() => {}); });
  list = list.filter(ch => ch.messages.length);
  list.forEach(ch => { _chatSent[ch.id] = _chatPayload(ch); });
  if(!list.length){
    // 舊對話搬家：伺服器上還沒有、這台瀏覽器有 → 上傳，成功後把舊資料改名成備份（不刪）
    let old = [];
    try { old = JSON.parse(localStorage.getItem(_chatsKey()) || '[]'); } catch(e){ old = []; }
    if(Array.isArray(old) && old.length){
      old = old.filter(ch => ch && Array.isArray(ch.messages) && ch.messages.length);
      for(const ch of old){ ch.projectId = null; await _putChat(ch); }
      try {
        localStorage.setItem(_chatsKey() + '_backup', localStorage.getItem(_chatsKey()));
        localStorage.removeItem(_chatsKey());
      } catch(e){}
      list = old;
    }
  }
  return list;
}

async function initChats(){
  if(!chatsLoaded){
    if(!_chatsLoading){
      _chatsLoading = _loadChatsFromServer().then(list => { chats = list; }).catch(e => {
        _chatSaveWarn('讀不到伺服器上的對話（' + (e.message || e) + '），請確認網站還在執行後重新整理。');
      }).finally(() => { chatsLoaded = true; _chatsLoading = null; });
    }
    await _chatsLoading;
  }
  try { currentChatId = localStorage.getItem(_currentChatKey()) || currentChatId || null; } catch(e){}
  if(!currentChatId || !chats.find(ch=>ch.id===currentChatId)){
    // 剛登入或上次看的是還沒送出的空白對話：開一則空白對話（歡迎畫面），送出第一則訊息才會存檔、列進清單
    const draft = chats.find(ch => !ch.messages.length && !ch.projectId);
    if(draft) currentChatId = draft.id;
    else { currentChatId = 'chat_' + Date.now(); chats.push({id:currentChatId, title:'新對話', messages:[], projectId:null}); }
  }
  hydrateChatFlows();
  renderChatList();
  renderMessages();
}

// 呼叫點跟原本一樣多：改成把有變動的對話合併 0.8 秒後存到伺服器；存不進去會在輸入框下方明顯提示
function saveChats(){
  try { localStorage.setItem(_currentChatKey(), currentChatId||''); } catch(e){}
  clearTimeout(_chatSaveTimer);
  _chatSaveTimer = setTimeout(_flushChats, 800);
}
async function _flushChats(){
  _chatSaveTimer = null;
  for(const ch of chats.slice()){
    if(!ch.messages.length) continue;   // 空白對話送出第一則訊息才存
    if(_chatSent[ch.id] === _chatPayload(ch)) continue;
    try { await _putChat(ch); _chatSaveWarn(null); }
    catch(e){ _chatSaveWarn('對話沒有存到伺服器：' + (e.message || e) + '。請確認網站還在執行，重新整理前先複製重要內容。'); }
  }
}
// 關閉或離開頁面前，把還沒存的小量變動送出去（keepalive 有大小限制，大的對話靠前面的 0.8 秒存檔）
window.addEventListener('pagehide', () => {
  if(!_chatSaveTimer) return;
  chats.forEach(ch => {
    const body = _chatPayload(ch);
    if(!ch.messages.length || _chatSent[ch.id] === body || body.length > 60000) return;
    try { fetch('/api/chats/' + encodeURIComponent(ch.id), {method:'PUT', headers:{'Content-Type':'application/json'}, body, keepalive:true}); } catch(e){}
  });
});

// 2026-10-21：空白對話不列進「最近的對話」、也不存到伺服器，送出第一則訊息（或上傳檔案）後才正式成立；
// 已經在空白對話上再按「新對話」不會多開一則
function _dropEmptyChats(keepId){ chats = chats.filter(ch => ch.messages.length || ch.id === keepId); }
function newChat(projectId){
  if(!chatsLoaded){ initChats().then(() => newChat(projectId)); return; }
  const pid = (typeof projectId === 'string' && projectId) ? projectId : null;
  const cur = currentChat();
  if(cur && !cur.messages.length){
    cur.projectId = pid;
  } else {
    const id = 'chat_' + Date.now();
    chats.push({id, title:'新對話', messages:[], projectId: pid});
    currentChatId = id;
  }
  _dropEmptyChats(currentChatId);
  saveChats();
  showPage('chat');
  renderChatList();
  renderMessages();
  setTimeout(()=>document.getElementById('chat-input')?.focus(), 0);
}

function deleteChat(id, e){
  e.stopPropagation();
  chats = chats.filter(ch=>ch.id!==id);
  delete _chatSent[id];
  fetch('/api/chats/' + encodeURIComponent(id), {method:'DELETE'}).then(r => {
    if(!r.ok) throw new Error('伺服器回應 ' + r.status);
  }).catch(err => _chatSaveWarn('對話沒有從伺服器刪除：' + (err.message || err) + '，重新整理後可能還會看到。'));
  if(currentChatId===id) currentChatId = chats.length ? chats[chats.length-1].id : null;
  if(!chats.length){ saveChats(); newChat(); return; }   // 先存檔再開新對話（2026-10-21 修正，見 CHANGELOG）
  saveChats();
  renderChatList();
  renderMessages();
}

function switchChat(id){
  currentChatId = id;
  _dropEmptyChats(id);
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
  el.innerHTML = chats.filter(ch => ch.messages.length).reverse().map(ch=>`
    <div class="chat-room-item ${ch.id===currentChatId?'active':''}" onclick="switchChat('${ch.id}')">
      <span class="chat-room-title">${chatProjectBadge(ch)}<span>${escHtml(ch.title)}</span></span>
      <span class="chat-room-delete" role="button" tabindex="0" data-tip="刪除這個對話" aria-label="刪除這個對話" onclick="deleteChat('${ch.id}',event)">刪除</span>
    </div>`).join('');
}

// ══════════════════════════════════════════════════════════════════
//  專案（2026-10-21）：說明、專案指示、對話、服務、上傳的檔案。不做共享，只看得到自己的。
// ══════════════════════════════════════════════════════════════════
let projects = [], currentProjectId = null, projTab = 'overview', projData = null;
const PROJ_ICON = '<svg class="proj-ico" viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="M1.75 4.25a1 1 0 011-1h3.2l1.5 1.5h5.8a1 1 0 011 1v6.5a1 1 0 01-1 1H2.75a1 1 0 01-1-1v-8z" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/></svg>';
function escAttr(v){ return esc(v).replace(/"/g, '&quot;').replace(/'/g, '&#39;'); }
function projectById(id){ return projects.find(p => p.id === id); }
function chatProjectBadge(ch){
  const p = ch && ch.projectId ? projectById(ch.projectId) : null;
  return p ? `<span class="proj-badge" data-tip="專案：${escAttr(p.name)}" aria-label="專案：${escAttr(p.name)}">${PROJ_ICON}</span>` : '';
}
async function projFetch(url, method, body){
  const r = await fetch(url, {method: method || 'GET', headers: body ? {'Content-Type':'application/json'} : {}, body: body ? JSON.stringify(body) : undefined});
  const d = await r.json().catch(() => ({}));
  if(!r.ok || d.ok === false) throw new Error(d.error || ('伺服器回應 ' + r.status));
  return d;
}
async function loadProjects(){
  try { projects = (await projFetch('/api/projects')).projects || []; }
  catch(e){ projects = []; }
  renderProjectList();
  renderChatList();
}
function renderProjectList(){
  const el = document.getElementById('project-list');
  if(!el) return;
  if(!projects.length){ el.innerHTML = '<div class="proj-empty-side">還沒有專案，按右上的 + 建立。</div>'; return; }
  const onProj = currentPageName() === 'project';
  el.innerHTML = projects.map(p => `
    <div class="chat-room-item proj-item ${onProj && p.id === currentProjectId ? 'active' : ''}" role="button" tabindex="0" onclick="openProject('${p.id}')" onkeydown="if(event.key==='Enter')openProject('${p.id}')">
      <span class="chat-room-title">${PROJ_ICON}<span>${esc(p.name)}</span></span>
    </div>`).join('');
}
function openProject(id){
  currentProjectId = id;
  projTab = 'overview';
  showPage('project');
}
function setProjTab(t){ projTab = t; renderProjectPage(); }
async function loadProjectPage(){
  const body = document.getElementById('proj-body');
  renderProjectList();
  if(!currentProjectId){ showPage('chat'); return; }
  try {
    await initChats();
    projData = (await projFetch('/api/projects/' + encodeURIComponent(currentProjectId))).project;
  } catch(e){
    projData = null;
    body.innerHTML = `<div class="proj-box">${emptyBlock('讀不到這個專案。', e.message || String(e))}</div>`;
    return;
  }
  const i = projects.findIndex(p => p.id === projData.id);
  if(i >= 0){ projects[i] = Object.assign({}, projects[i], {name: projData.name}); renderProjectList(); }
  renderProjectPage();
}
function renderProjectPage(){
  const p = projData;
  if(!p) return;
  document.getElementById('proj-title').textContent = p.name;
  document.getElementById('proj-sub').textContent = p.description || '這個專案的對話、服務和上傳的檔案都整理在這裡。';
  const tt = document.getElementById('topbar-title');
  if(tt) tt.textContent = '專案：' + p.name;
  document.getElementById('proj-newchat-btn').textContent = `在「${p.name}」開始新對話`;
  document.querySelectorAll('.proj-tabs button').forEach(b => {
    const on = b.dataset.ptab === projTab;
    b.classList.toggle('active', on); b.setAttribute('aria-selected', on ? 'true' : 'false');
  });
  const body = document.getElementById('proj-body');
  if(projTab === 'summary'){ body.innerHTML = '<div class="proj-box"><p class="proj-muted">正在整理…</p></div>'; loadProjSummary(p); }
  else if(projTab === 'schedule'){ body.innerHTML = '<div class="proj-box"><p class="proj-muted">正在讀取排程…</p></div>'; loadProjSchedule(p); }
  else if(projTab === 'services') body.innerHTML = projServicesHTML(p);
  else if(projTab === 'files') body.innerHTML = projFilesHTML(p);
  else if(projTab === 'settings') body.innerHTML = projSettingsHTML(p);
  else body.innerHTML = projOverviewHTML(p);
}
function projTime(ts){
  const d = new Date(ts * 1000);
  return `${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}
function projOverviewHTML(p){
  const chatRows = p.chats.length ? p.chats.map(c => `
      <div class="proj-row click" role="button" tabindex="0" onclick="openProjectChat('${c.id}')" onkeydown="if(event.key==='Enter')openProjectChat('${c.id}')">
        <span class="grow">${esc(c.title)}</span><span class="proj-muted">${projTime(c.updated_at)}</span>
        <button type="button" class="btn btn-sm" data-tip="移出後變成一般對話，內容不變" onclick="event.stopPropagation();moveChatToProject('${c.id}', null)">移出專案</button></div>`).join('')
    : '<p class="proj-muted">還沒有對話。按右上的按鈕，在這個專案開始第一則對話；在專案裡部署的服務會自動歸到這個專案。</p>';
  // 2026-10-21：把現有的一般對話移進專案（已經在別的專案的對話要先從那個專案移出）
  const loose = chats.filter(ch => ch.messages.length && !ch.projectId).slice().reverse();
  const addChat = loose.length
    ? `<div class="proj-actions" style="justify-content:flex-start"><select id="proj-add-chat" aria-label="選擇要移進專案的對話">${loose.map(ch => `<option value="${escAttr(ch.id)}">${esc(ch.title)}</option>`).join('')}</select>
         <button type="button" class="btn" onclick="moveChatToProject(document.getElementById('proj-add-chat').value, currentProjectId)">移進專案</button></div>`
    : '<p class="proj-muted">沒有可以移進來的對話（已經在其他專案裡的對話，要先從那個專案移出）。</p>';
  return `<div class="proj-grid">
    <div class="proj-box"><h2>對話</h2>${chatRows}
      <div class="proj-actions" style="justify-content:flex-start"><span class="proj-msg" id="proj-chat-msg"></span></div>
      <label class="lbl">把現有的對話移進這個專案</label>${addChat}
      <p class="field-hint">移動對話不會移動它部署過的服務；服務要到「服務」分頁另外移。</p></div>
    <div class="proj-box"><h2>說明與專案指示</h2>
      <label class="lbl" for="proj-desc">說明</label>
      <textarea id="proj-desc" maxlength="2000" placeholder="這個專案在做什麼，例如：學校社團的網路商店">${esc(p.description)}</textarea>
      <label class="lbl" for="proj-inst">專案指示</label>
      <textarea id="proj-inst" maxlength="2000" placeholder="例如：映像檔都用公司的倉庫、web 至少開 2 個副本、記憶體上限 512Mi">${esc(p.instructions)}</textarea>
      <p class="field-hint">AI 回答這個專案裡的問題時會參考專案指示。部署時不會自動改設定，還是以你在部署單上確認的內容為準。</p>
      <div class="proj-actions"><span class="proj-msg" id="proj-ov-msg"></span><button type="button" class="btn primary" onclick="saveProjectText()">儲存</button></div>
    </div></div>`;
}
function projServicesHTML(p){
  const rows = p.services.length ? p.services.map(s => {
    const st = !s.exists ? '找不到這個服務，可能已經在別的地方被刪除了' : s.sleeping ? '休眠中' : `${s.ready}/${s.replicas} 個副本就緒`;
    return `<div class="proj-row"><span class="grow">${esc(s.name)}</span><span class="proj-muted">${esc(st)}</span>
      <button type="button" class="btn btn-sm" data-tip="移出後服務照常運作" onclick="projServiceAction('remove','${escAttr(s.name)}')">移出專案</button></div>`;
  }).join('') : '<p class="proj-muted">這個專案還沒有服務。在專案的對話裡部署，或從下面把現有的服務移進來。</p>';
  const add = p.available_services.length
    ? `<div class="proj-actions" style="justify-content:flex-start"><select id="proj-add-svc" aria-label="選擇要移進專案的服務">${p.available_services.map(n => `<option value="${escAttr(n)}">${esc(n)}</option>`).join('')}</select>
         <button type="button" class="btn" onclick="projServiceAction('add', document.getElementById('proj-add-svc').value)">移進專案</button></div>`
    : '<p class="proj-muted" style="margin-top:12px">目前沒有可以移進來的服務（每個服務只能屬於一個專案）。</p>';
  return `<div class="proj-box"><h2>服務</h2>${rows}
    <div class="proj-actions" style="justify-content:flex-start"><span class="proj-msg" id="proj-svc-msg"></span></div>
    <label class="lbl">把現有的服務移進這個專案</label>${add}</div>`;
}
// 2026-10-23：摘要分頁（只算這個專案裡的服務，數字都是平台紀錄算出來的）
async function loadProjSummary(p){
  let d;
  try { d = await projFetch('/api/projects/' + encodeURIComponent(p.id) + '/summary?days=7'); }
  catch(e){ d = {ok:false, error:e.message}; }
  if(projTab !== 'summary' || !projData || projData.id !== p.id) return;
  const body = document.getElementById('proj-body');
  if(!d.ok){ body.innerHTML = `<div class="proj-box">${emptyBlock('讀不到摘要。', d.error || '')}</div>`; return; }
  const sv = d.services;
  const tile = (n, label, sub, tone) => `<div class="proj-stat ${tone || ''}"><b>${esc(n)}</b><span>${esc(label)}</span>${sub ? `<em>${esc(sub)}</em>` : ''}</div>`;
  const tiles = [
    tile(sv.total, '個服務', sv.total ? (sv.attention ? `${sv.attention} 個有狀況` : (sv.sleeping ? `${sv.sleeping} 個休眠中` : '全部正常')) : '還沒有服務', sv.attention ? 'warn' : ''),
    tile(d.deploys, `近 ${d.days} 天部署`, d.deploys_failed ? `${d.deploys_failed} 次失敗` : ''),
    tile(d.changes, '次擴縮、換版本或回滾', ''),
    tile(d.heals, '次自動修復', d.heals_need_you ? `${d.heals_need_you} 次需要你處理` : '', d.heals_need_you ? 'warn' : ''),
    tile(d.blocked, '次審查擋下', ''),
  ].join('');
  const recent = d.recent.length ? d.recent.map(e => `<div class="proj-row"><span class="proj-muted" style="flex:none;width:92px">${projTime(e.ts)}</span>
      <span class="grow">${esc(e.app || '')}：${esc(e.detail || '')}</span></div>`).join('')
    : '<p class="proj-muted">這個專案還沒有紀錄，部署或操作之後就會出現在這裡。</p>';
  const sched = d.schedules.length ? d.schedules.map(x => `<div class="proj-row"><span class="grow">${esc(x.app)}：${esc(x.rows.map(r => r.when + ' ' + r.replicas + ' 個').join('；'))}</span>
      <span class="proj-muted">${x.next ? `下次 ${esc(x.next.at)} 變成 ${esc(x.next.replicas)} 個` : ''}</span></div>`).join('')
    : '<p class="proj-muted">這個專案的服務都沒有設定分時擴縮，可以到「排程」分頁設定。</p>';
  body.innerHTML = `<div class="page-stack">
    <div class="proj-stats">${tiles}</div>
    <div class="proj-box"><h2>最近的動態</h2>${recent}</div>
    <div class="proj-box"><h2>分時擴縮</h2>${sched}</div>
    <p class="field-hint">${d.days_without_incident != null && d.has_data ? `已經連續 ${esc(d.days_without_incident)} 天沒有需要修復的狀況。` : ''}數字都是從平台紀錄算出來的，只算這個專案裡的服務。</p>
  </div>`;
}
// 2026-10-23：排程分頁（沿用「我的服務」的設定彈窗與確認卡）
async function loadProjSchedule(p){
  let d;
  try { d = await getJSON('/api/autoscale'); } catch(e){ d = {ok:false, error:e.message}; }
  if(projTab !== 'schedule' || !projData || projData.id !== p.id) return;
  const body = document.getElementById('proj-body');
  if(d.ok === false){ body.innerHTML = `<div class="proj-box">${emptyBlock('排程現在不能用。', d.error || '')}</div>`; return; }
  const mine = new Set(p.services.map(s => s.name));
  const items = (d.items || []).filter(x => mine.has(x.name));
  const rows = items.length ? items.map(x => {
    const st = x.sleeping ? '休眠中' : `${x.ready}/${x.replicas} 個副本就緒`;
    const sch = x.schedule ? `${x.schedule.rows.map(r => r.when + ' ' + r.replicas + ' 個').join('；')}${x.schedule.next ? `（下次 ${x.schedule.next.at} 變成 ${x.schedule.next.replicas} 個）` : ''}` : '沒有排程';
    return `<div class="proj-row"><span class="grow"><b>${esc(x.name)}</b><span class="proj-muted">　${esc(st)}</span><br><span class="proj-muted">${esc(sch)}</span></span>
      ${d.remote ? '' : `<button type="button" class="btn btn-sm" data-act="as-suggest" data-name="${escAttr(x.name)}">建議時段</button>`}
      ${x.schedule ? `<button type="button" class="btn btn-sm" onclick="projCancelSchedule('${escAttr(x.name)}')">取消排程</button>` : ''}
      <button type="button" class="btn btn-sm primary" data-act="as-edit" data-name="${escAttr(x.name)}">${x.schedule ? '修改排程' : '設定排程'}</button></div>`;
  }).join('') : '<p class="proj-muted">這個專案還沒有服務。到「服務」分頁移入，或在專案的對話裡部署。</p>';
  body.innerHTML = `<div class="proj-box"><h2>分時擴縮</h2>${rows}
    <div class="proj-actions" style="justify-content:flex-start"><span class="proj-msg" id="proj-sched-msg"></span></div>
    <p class="field-hint">${d.remote ? esc(d.remote_note || '') : `閒置幾天就休眠、例外清單是整個帳號的設定，請到<a href="#" onclick="showPage('services');return false">我的服務</a>調整。`}</p></div>`;
}
async function projCancelSchedule(name){
  const d = await postJSON('/api/autoscale/cancel', {name});
  if(projData) await loadProjSchedule(projData);
  projSetMsg('proj-sched-msg', d.message || d.error || '', !!d.ok);
}
function projFilesHTML(p){
  const rows = p.files.length ? p.files.map(f => `
      <div class="proj-row"><span class="grow">${esc(f.name)}</span>
        <span class="proj-muted">${f.format === 'k8s' ? 'Kubernetes YAML' : 'docker-compose'}，${Math.max(1, Math.round(f.size / 1000))} KB，${projTime(f.created_at)}</span>
        <a class="btn btn-sm" href="/api/projects/${encodeURIComponent(p.id)}/files/${encodeURIComponent(f.id)}" download>下載</a>
        <button type="button" class="btn btn-sm danger" onclick="projDeleteFile('${f.id}','${escAttr(f.name)}')">刪除</button></div>`).join('')
    : '<p class="proj-muted">還沒有檔案。在這個專案的對話裡上傳 docker-compose 或 Kubernetes YAML，原始檔案會留在這裡。</p>';
  return `<div class="proj-box"><h2>檔案</h2>${rows}</div>`;
}
function projSettingsHTML(p){
  return `<div class="page-stack">
    <div class="proj-box"><h2>專案名稱</h2>
      <input type="text" id="proj-name" maxlength="40" value="${escAttr(p.name)}" aria-label="專案名稱">
      <div class="proj-actions"><span class="proj-msg" id="proj-name-msg"></span><button type="button" class="btn primary" onclick="renameProject()">儲存</button></div></div>
    <div class="proj-box danger"><h2>刪除專案</h2>
      <p class="proj-muted">可以選擇要一起刪掉哪些內容，其餘的移出專案或留在專案裡。部署紀錄和報告用的紀錄一律保留。</p>
      <div class="proj-actions"><button type="button" class="btn danger" onclick="openDeleteProject()">刪除專案…</button></div></div>
  </div>`;
}
function projSetMsg(id, text, ok){
  const el = document.getElementById(id);
  if(el){ el.textContent = text; el.className = 'proj-msg ' + (ok ? 'ok' : 'err'); }
}
async function saveProjectText(){
  try {
    projData = (await projFetch('/api/projects/' + encodeURIComponent(currentProjectId), 'PATCH',
      {description: document.getElementById('proj-desc').value, instructions: document.getElementById('proj-inst').value})).project;
    document.getElementById('proj-sub').textContent = projData.description || '這個專案的對話、服務和上傳的檔案都整理在這裡。';
    projSetMsg('proj-ov-msg', '已儲存。', true);
  } catch(e){ projSetMsg('proj-ov-msg', '沒有存成功：' + e.message, false); }
}
async function renameProject(){
  try {
    projData = (await projFetch('/api/projects/' + encodeURIComponent(currentProjectId), 'PATCH',
      {name: document.getElementById('proj-name').value})).project;
    const i = projects.findIndex(p => p.id === projData.id);
    if(i >= 0) projects[i].name = projData.name;
    renderProjectList(); renderChatList(); renderProjectPage();
    projSetMsg('proj-name-msg', '已儲存。', true);
  } catch(e){ projSetMsg('proj-name-msg', '沒有存成功：' + e.message, false); }
}
async function projServiceAction(action, name){
  if(!name) return;
  try {
    const d = await projFetch('/api/projects/' + encodeURIComponent(currentProjectId) + '/services', 'POST', {action, name});
    await loadProjectPage();
    projSetMsg('proj-svc-msg', d.message, true);
  } catch(e){ projSetMsg('proj-svc-msg', e.message, false); }
}
async function projDeleteFile(fid, name){
  if(!(await askConfirm('刪除檔案', `確定要刪除 <b>${esc(name)}</b> 嗎？已經部署的服務不受影響。`, '刪除', '取消'))) return;
  try { await projFetch('/api/projects/' + encodeURIComponent(currentProjectId) + '/files/' + encodeURIComponent(fid), 'DELETE'); }
  catch(e){ await askConfirm('沒有刪除成功', esc(e.message), '知道了', '關閉'); }
  loadProjectPage();
}
async function openProjectChat(id){
  await initChats();
  if(!chats.find(ch => ch.id === id)){ chatsLoaded = false; await initChats(); }
  switchChat(id);
}
async function moveChatToProject(chatId, pid){
  const ch = chats.find(c => c.id === chatId);
  if(!ch) return;
  const before = ch.projectId || null;
  ch.projectId = pid || null;
  clearTimeout(_chatSaveTimer); _chatSaveTimer = null;
  try { await _putChat(ch); }
  catch(e){ ch.projectId = before; projSetMsg('proj-chat-msg', '沒有移成功：' + (e.message || e), false); return; }
  renderChatList();
  await loadProjectPage();
  projSetMsg('proj-chat-msg', pid ? `已經把「${ch.title}」移進專案。` : `已經把「${ch.title}」移出專案，變成一般對話。`, true);
  _flushChats();
}
function projectNewChat(){ if(currentProjectId) newChat(currentProjectId); }

// ── 新增專案、刪除專案的卡片 ──
function openProjModal(html){
  document.getElementById('proj-modal').innerHTML = html;
  document.getElementById('proj-modal-bg').classList.add('open');
  setTimeout(() => { const f = document.querySelector('#proj-modal input[type=text], #proj-modal input'); if(f) f.focus(); }, 0);
}
function closeProjModal(){ document.getElementById('proj-modal-bg').classList.remove('open'); }
document.addEventListener('keydown', e => { if(e.key === 'Escape' && document.getElementById('proj-modal-bg')?.classList.contains('open')) closeProjModal(); });
function openProjectModal(){
  openProjModal(`<h3>新增專案</h3>
    <label class="lbl" for="np-name">專案名稱</label><input type="text" id="np-name" maxlength="40" placeholder="例如：網路商店" onkeydown="if(event.key==='Enter')createProject()">
    <label class="lbl" for="np-desc">說明（選填）</label><textarea id="np-desc" maxlength="2000" placeholder="這個專案在做什麼"></textarea>
    <div class="proj-err" id="np-err" hidden></div>
    <div class="confirm-dialog-actions"><button type="button" class="btn" onclick="closeProjModal()">取消</button>
      <button type="button" class="btn primary" onclick="createProject()">建立</button></div>`);
}
async function createProject(){
  const err = document.getElementById('np-err');
  try {
    const p = (await projFetch('/api/projects', 'POST', {name: document.getElementById('np-name').value, description: document.getElementById('np-desc').value})).project;
    projects.push(p);
    closeProjModal();
    openProject(p.id);
  } catch(e){ err.textContent = e.message; err.hidden = false; }
}
function openDeleteProject(){
  const p = projData;
  if(!p) return;
  const nSvc = p.services.length, hasInst = !!(p.instructions || '').trim();
  const svcList = nSvc ? '：' + p.services.map(s => esc(s.name)).join('、') : '';
  openProjModal(`<h3>刪除專案「${esc(p.name)}」</h3><div class="proj-del">
    <div class="q">請勾選想要移除的部分：</div>
    <label><input type="checkbox" id="pd-all" onchange="pdSync(this)"> <span>整個專案<span class="note">勾選後會自動勾選下列所有選項</span></span></label>
    <label><input type="checkbox" class="pd-part" data-k="chats" onchange="pdSync()"> <span>對話（${p.chats.length} 則）</span></label>
    <label><input type="checkbox" class="pd-part" data-k="services" onchange="pdSync()"> <span>服務（${nSvc} 個${svcList}）${nSvc ? '<span class="note warn-t">勾選會關掉並刪除正在運行的服務</span>' : ''}</span></label>
    <label><input type="checkbox" class="pd-part" data-k="files" onchange="pdSync()"> <span>檔案（上傳的 compose、YAML，${p.files.length} 個）</span></label>
    <label><input type="checkbox" class="pd-part" data-k="instructions" onchange="pdSync()"> <span>專案指示${hasInst ? '' : '（目前是空的）'}</span></label>
    <div id="pd-rest-box"><div class="q">未勾選的部分要怎麼處理？</div>
      <label><input type="radio" name="pd-rest" value="move" onchange="pdSync()"> <span>移出專案，然後刪除這個空專案<span class="note">對話會變成一般對話，服務照常運作、不屬於任何專案</span></span></label>
      <label><input type="radio" name="pd-rest" value="keep" onchange="pdSync()"> <span>保留在這個專案中（只刪除勾選的部分）</span></label></div>
    <div class="proj-warn" id="pd-lost" hidden></div>
    <div id="pd-confirm" hidden><label class="lbl" for="pd-name" style="display:block;padding:0;cursor:default">要刪除服務，請輸入專案名稱「${esc(p.name)}」確認：</label>
      <input type="text" id="pd-name" autocomplete="off" oninput="pdSync()"></div>
    <div class="proj-err" id="pd-err" hidden></div>
    </div>
    <div class="confirm-dialog-actions"><button type="button" class="btn" onclick="closeProjModal()">取消</button>
      <button type="button" class="btn primary danger" id="pd-go" disabled onclick="doDeleteProject()">刪除</button></div>`);
  pdSync();
}
function pdState(){
  const all = document.getElementById('pd-all').checked;
  const parts = {};
  document.querySelectorAll('.pd-part').forEach(c => { parts[c.dataset.k] = all || c.checked; });
  const r = document.querySelector('input[name="pd-rest"]:checked');
  return {all, parts, rest: all ? 'move' : (r ? r.value : null)};
}
function pdSync(src){
  const p = projData, allBox = document.getElementById('pd-all');
  if(src === allBox) document.querySelectorAll('.pd-part').forEach(c => { c.checked = allBox.checked; });
  const s = pdState();
  document.querySelectorAll('.pd-part').forEach(c => { c.disabled = s.all; });
  document.getElementById('pd-rest-box').hidden = s.all;
  // 移出時沒地方放的：檔案、專案指示
  const lost = [];
  if(s.rest === 'move' && !s.all){
    if(!s.parts.files && p.files.length) lost.push('files');
    if(!s.parts.instructions && (p.instructions || '').trim()) lost.push('instructions');
  }
  const lostBox = document.getElementById('pd-lost');
  if(lost.length){
    const links = [];
    if(lost.includes('files')) p.files.forEach(f => links.push(`<a href="/api/projects/${encodeURIComponent(p.id)}/files/${encodeURIComponent(f.id)}" download>${esc(f.name)}</a>`));
    if(lost.includes('instructions')) links.push('<a href="#" onclick="pdDownloadInstructions();return false">專案指示.txt</a>');
    lostBox.innerHTML = `${lost.includes('files') ? '檔案' : ''}${lost.length === 2 ? '和' : ''}${lost.includes('instructions') ? '專案指示' : ''}在專案外沒有地方放，會跟專案一起刪除。需要的話先下載備份：${links.join('、')}`;
    lostBox.hidden = false;
  } else lostBox.hidden = true;
  const needName = s.parts.services && p.services.length > 0;
  document.getElementById('pd-confirm').hidden = !needName;
  const nameOk = !needName || document.getElementById('pd-name').value.trim() === p.name;
  const anyPart = Object.values(s.parts).some(Boolean);
  const valid = s.rest && (s.rest === 'move' || anyPart) && nameOk;
  document.getElementById('pd-go').disabled = !valid;
}
function pdDownloadInstructions(){
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([projData.instructions || ''], {type:'text/plain;charset=utf-8'}));
  a.download = projData.name + '-專案指示.txt';
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}
async function doDeleteProject(){
  const s = pdState(), p = projData, btn = document.getElementById('pd-go'), err = document.getElementById('pd-err');
  btn.disabled = true; btn.textContent = '處理中…'; err.hidden = true;
  let d;
  try {
    const r = await fetch('/api/projects/' + encodeURIComponent(p.id) + '/delete', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({remove: Object.assign({all: s.all}, s.parts), rest: s.rest, confirm_name: (document.getElementById('pd-name') || {}).value || ''})});
    d = await r.json().catch(() => ({}));
    if(!r.ok || !d.ok) throw new Error(d.error || ('伺服器回應 ' + r.status));
  } catch(e){
    err.textContent = e.message; err.hidden = false; btn.textContent = '刪除'; pdSync();
    return;
  }
  closeProjModal();
  // 對話可能被刪除或移出：先把還沒存的變動送出，再從伺服器重新讀一次
  clearTimeout(_chatSaveTimer); await _flushChats();
  chatsLoaded = false; chats = [];
  await loadProjects();
  if(d.project_deleted){
    currentProjectId = null;
    await initChats();
    showPage('chat');
    setComposerHint(`已刪除專案「${p.name}」${d.deleted_services && d.deleted_services.length ? '，並刪除服務：' + d.deleted_services.join('、') : ''}。`);
  } else {
    await initChats();
    showPage('project');
  }
}

function renderMessages(){
  const msgs = document.getElementById('chat-messages');
  if(!msgs) return;
  const ch = currentChat();
  if(!ch || !ch.messages.length){
    const t = I18N[uiLang];
    // 2026-10-03 實測發現的真實 bug：這個 onclick 屬性本身是雙引號包起來
    // （onclick="..."），裡面卻塞進 JSON.stringify(c.send) 產生的「雙引號字串」
    // （例如 "deploy 3 nginx:latest..."）——HTML 屬性解析到第一個雙引號就會提前
    // 結束屬性值，後面整段變成語法錯誤的裸露文字，onclick 實際上被截斷成不完整
    // 的 JS，點了完全沒反應。改成外層屬性用單引號、裡面的 JS 字串一律用雙引號
    // （跟 showPodDetail 那處既有、驗證過安全的寫法一致），兩種引號不衝突。
    const cardsHtml = t.cards.map(c =>
      `<div class="prompt-card" onclick='document.getElementById("chat-input").value=${JSON.stringify(c.send)};sendChat()'>
         <strong>${escHtml(c.title)}</strong><span>${escHtml(c.example)}</span>
       </div>`).join('');
    msgs.innerHTML = `<div class="chat-empty">
      <div class="chat-empty-logo"><svg width="26" height="26" viewBox="0 0 26 26" aria-hidden="true"><circle cx="13" cy="13" r="11" fill="none" stroke="currentColor" stroke-width="2"/><g stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><path d="M13 3.5v19M3.5 13h19M6.3 6.3l13.4 13.4M19.7 6.3 6.3 19.7"/></g><circle cx="13" cy="13" r="3.4" fill="var(--panel)" stroke="currentColor" stroke-width="2"/></svg></div>
      <h1>${escHtml(t.emptyTitle)}</h1>
      <p>${escHtml(t.emptyDesc)}</p>
      <div class="prompt-grid">${cardsHtml}</div>
    </div>`;
    return;
  }
  msgs.innerHTML = ch.messages.map(m=>renderMsgHTML(m.role, m.content, m.sources, m.fmt)).join('');
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
    <summary style="cursor:pointer;color:var(--text3)">參考了知識庫的 ${sources.length} 段資料</summary>
    <div style="margin-top:6px">${items}</div>
  </details>`;
}

function escHtml(s){ return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

// 更新「思考中」文字但不重建元素（沒有 indicator 就建一個）
function setTyping(label){ appendTyping(label); }

function removeTyping(){ const el=document.getElementById('typing-indicator'); if(el) el.remove(); }

// \u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550
//  Chat \u610f\u5716\u8fa8\u8b58\uff08\u898f\u5247\u5c64\uff0c\u524d\u7aef\uff1b\u6bd4\u5c0d\u4e0d\u5230\u624d\u6253 /api/intent\uff09
// \u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550
const DESTRUCTIVE_KINDS = ['scale','update_image','rollback','delete','healer_fix','healer_auto_fix'];
const READ_ACTIONS = ['list_pods','list_deployments','gitops_log','cluster_metrics','healer_scan',
                      'describe_pod','pod_health','describe_deployment','report'];

function matchClientRule(text){
  const t = text.trim();
  const rules = [
    // 分時擴縮與休眠（2026-10-11，後端 _INTENT_RULES 有同樣的規則）；放最前面，「白天調成 3 個」不能被擴縮規則搶走
    ['as_wake', /^(?:請|幫我)?\s*(?:喚醒|叫醒|wake\s*(?:up)?)\s*([a-z0-9][a-z0-9-]*)/i, m=>({name:m[1]})],
    ['as_wake', /(?:把\s*)?([a-z0-9][a-z0-9-]*)\s*(?:喚醒|叫醒)/i, m=>({name:m[1]})],
    ['as_exempt_on', /([a-z0-9][a-z0-9-]*)\s*(?:不要|不用|別|不准|不可以|不能|永遠不要?)\s*(?:讓它|讓他)?\s*(?:進入|自動)?\s*休眠/i, m=>({name:m[1]})],
    ['as_exempt_on', /(?:把\s*)?([a-z0-9][a-z0-9-]*)\s*(?:加入|加到|放進|放到)\s*例外/i, m=>({name:m[1]})],
    ['as_exempt_off', /([a-z0-9][a-z0-9-]*)\s*(?:可以|允許|恢復)\s*(?:自動)?\s*休眠/i, m=>({name:m[1]})],
    ['as_exempt_off', /(?:把\s*)?([a-z0-9][a-z0-9-]*)\s*(?:移出|拿出|從)\s*例外/i, m=>({name:m[1]})],
    ['as_cancel', /(?:取消|刪除|關掉|停止|移除)\s*([a-z0-9][a-z0-9-]*)\s*的?\s*(?:分時|排程|時段)/i, m=>({name:m[1]})],
    ['as_suggest', /(?:建議|推薦)\s*([a-z0-9][a-z0-9-]*)\s*的?\s*(?:時段|排程|分時)/i, m=>({name:m[1]})],
    ['as_suggest', /([a-z0-9][a-z0-9-]*)\s*(?:的)?\s*(?:建議|推薦)\s*(?:的)?\s*(?:時段|排程|分時)/i, m=>({name:m[1]})],
    ['as_idle_days', /(?:閒置|沒人?用|沒有人用|沒有?流量)\s*(?:超過)?\s*(\d{1,2})\s*天.{0,10}休眠/i, m=>({days:parseInt(m[1],10)})],
    ['as_show', /(?:分時|排程|休眠|例外)(?:擴縮)?\s*(?:的)?\s*(?:設定|狀態|清單|名單|列表|情況)|^(?:列出|查看|顯示|看看?).{0,6}(?:排程|休眠|例外)/i, null],
    ['as_schedule', /^(?!.*(?:部署|佈署|部屬|deploy|擴|縮|調|掛掉|掛了|壞|當機|當掉|錯誤|出錯)).*(?:白天|晚上|夜間|晚間|深夜|凌晨|上班時間|辦公時間|平日|工作日|週末|周末|假日|\d{1,2}\s*(?:點|[:：]\d{2})).{0,40}?\d{1,3}\s*(?:個|副本|pods?)/i, null],
    // 分析報告：句子裡有「報告／報表／週報／月報」而且不是在要求部署
    ['report', /^(?!.*(?:\u90e8\u7f72|deploy|\u8d77\u4e00\u500b|\u958b\u4e00\u500b)).*(?:\u5206\u6790\u5831\u544a|\u5831\u544a|\u5831\u8868|\u9031\u5831|\u5468\u5831|\u6708\u5831|\breport\b)/i, () => ({q:text})],
    ['list_pods', /^(list|show|\u67e5\u770b|\u986f\u793a|\u5217\u51fa)\s*(all\s*|\u6240\u6709|\u5168\u90e8)?\s*(pods?|\u5bb9\u5668)/i, null],
    ['list_deployments', /^(list|show|\u67e5\u770b|\u986f\u793a|\u5217\u51fa)\s*(all\s*|\u6240\u6709|\u5168\u90e8)?\s*(deploy(ment)?s?|\u90e8\u7f72)/i, null],
    ['gitops_log', /^(gitops|deploy history|git ?log)|\u90e8\u7f72(\u7d00\u9304|\u6b77\u53f2|\u8a18\u9304)/i, null],
    ['cluster_metrics', /^(metrics|cluster (status|health)|\u53e2\u96c6(\u72c0\u614b|\u5065\u5eb7)|\u6307\u6a19)/i, null],
    ['healer_auto_fix', /^(auto ?fix|fix all|\u81ea\u52d5\u4fee\u5fa9|\u5168\u90e8\u4fee\u5fa9)/i, null],
    ['healer_fix', /^(?:fix|\u4fee\u5fa9)\s+(\S+)/i, m=>({pod_name:m[1]})],
    ['healer_scan', /^(healer|scan)\b|\u6383\u63cf.*(pod|\u58de|\u7570\u5e38)/i, null],
    ['pod_health', /([a-zA-Z0-9][\w.-]*)\s*(?:\u9019\u500b)?\s*(?:pod|deployment|\u90e8\u7f72)?\s*(?:\u6709\u6c92\u6709\u58de|\u58de\u4e86\u6c92|\u58de\u6389\u4e86\u55ce|\u58de\u4e86\u55ce|\u6b63\u5e38\u55ce|\u5065\u5eb7\u55ce|\u639b\u4e86\u55ce|\u639b\u6389\u4e86\u55ce|\u9084\u6d3b\u8457\u55ce|\u6c92\u4e8b\u5427|\u6709\u554f\u984c\u55ce|ok\s*\u55ce)/i, m=>({name:m[1]})],
    ['pod_health', /^is\s+([a-zA-Z0-9][\w.-]*)\s+(?:ok|okay|healthy|broken|down|up|crashing|running|alive)/i, m=>({name:m[1]})],
    ['describe_deployment', /(?:deployment|\u90e8\u7f72)\s+([a-zA-Z0-9][\w.-]*)\s*(?:\u7684?\s*(?:\u72c0\u614b|\u7d30\u7bc0|\u8a73\u60c5|status))/i, m=>({name:m[1]})],
    ['describe_deployment', /([a-zA-Z0-9][\w.-]*)\s*(?:\u9019\u500b)?\s*(?:deployment|\u90e8\u7f72)\s*(?:\u7684?\s*(?:\u72c0\u614b|\u7d30\u7bc0|\u8a73\u60c5))/i, m=>({name:m[1]})],
    ['describe_pod', /(?:\u67e5\u770b|detail(?:s)?\s*(?:of)?|describe|\u6aa2\u8996)\s*(?:pod\s+)?([a-zA-Z0-9][\w.-]*)/i, m=>({name:m[1]})],
    ['describe_pod', /([a-zA-Z0-9][\w.-]*)\s*(?:\u9019\u500b)?\s*pod\s*(?:\u7684?\s*(?:\u7d30\u7bc0|\u72c0\u614b|\u8a73\u60c5|\u8cc7\u8a0a))/i, m=>({name:m[1]})],
    ['describe_pod', /([a-zA-Z0-9][\w.-]*)\s*(?:\u7684\s*(?:\u7d30\u7bc0|\u8a73\u60c5|\u8a73\u7d30\u72c0\u614b))/i, m=>({name:m[1]})],
    ['delete', /^(?:delete|remove|\u522a\u9664|del)\s+(\S+)/i, m=>({name:m[1]})],
    ['scale', /scale\s+(\S+)\s+to\s+(\d+)/i, m=>({name:m[1],replicas:parseInt(m[2],10)})],
    ['scale', /\u628a?\s*(\S+?)\s*(?:\u64f4|\u7e2e|\u8abf).*?(\d+)/i, m=>({name:m[1],replicas:parseInt(m[2],10)})],
    ['update_image', /update\s+(\S+)\s+to\s+(\S+)/i, m=>({name:m[1],image:m[2]})],
    ['update_image', /\u628a?\s*(\S+?)\s*\u7684?\s*(?:image|\u6620\u50cf\u6a94?|\u93e1\u50cf\u6a94?)\s*(?:\u63db\u6210|\u6539\u6210|\u6539\u70ba|\u66f4\u65b0\u6210|\u66f4\u65b0\u70ba|to)?\s*(\S+)/i, m=>({name:m[1],image:m[2]})],
    ['rollback', /rollback\s+(\S+)/i, m=>({name:m[1]})],
    ['rollback', /(\S+)\s*(?:\u56de\u6efe|\u56de\u5fa9|\u9084\u539f)/i, m=>({name:m[1]})],
  ];
  for(const [action, rx, extract] of rules){
    const m = t.match(rx);
    if(!m) continue;
    // 「休眠模式是什麼？」這種問概念的句子交給問答，不要當成設定指令
    if(action.startsWith('as_') && action !== 'as_wake' && /[？?]\s*$|是什麼|怎麼|如何|為什麼|嗎/.test(t)) continue;
    const args = extract ? extract(m) : {};
    if(extract && Object.values(args).some(v=>v===undefined||v===''||(typeof v==='number'&&isNaN(v)))) continue;
    return {action, args, source:'rule'};
  }
  // deploy\uff1a\u6cbf\u7528\u539f\u672c\u7684\u8907\u5408\u5224\u65b7
  if(/^((?:deploy|start|launch|run)\b|spin\s+up|\u90e8\u7f72|\u4f48\u7f72|\u90e8\u5c6c|\u8d77\s)/i.test(t) ||
     ((/\u5e6b\u6211|\u8acb/i.test(t)) && /(deploy|\u90e8\u7f72|\u4f48\u7f72|\u90e8\u5c6c|pods?|pod|\u8d77)/i.test(t) &&
      (/\d+\s*(pods?|replicas?|\u500b|\u526f\u672c)/i.test(t) || /(nginx|redis|postgres|node|python|golang|image|\u6620\u50cf|\u93e1\u50cf)/i.test(t)))){
    return {action:'deploy', args:{}, source:'rule'};
  }
  return null;
}

// \u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550
//  \u591a\u6b65\u6d41\u7a0b\u5361\u7247 renderer\uff08HTML \u5b57\u4e32\uff0c\u5b58\u9032 message.content\uff09
// \u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550
// \u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550
//  \u6d41\u7a0b\u9a45\u52d5
// \u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550
function retryFlow(id){
  const flow = chatFlows[id];
  if(!flow || !flow.retryStep) return;
  flow.step = flow.retryStep;
  flow.errorMsg = null; flow.blockers = null;
  persistFlowCard(id);
}
async function flowToReview(id){
  const flow = chatFlows[id];
  if(!flowAlive(flow)) return;
  const data = readSpecCard(id);
  if(!data) return;
  const {root, spec} = data;
  setFlowError(root, '');
  if(!spec.app_name || !spec.image){ setFlowError(root, '請填寫服務名稱和映像檔。'); return; }
  // 2026-10-03\uff1aapp_name \u4e00\u5b9a\u8981\u5b57\u6bcd\u958b\u982d\u2014\u2014\u7cfb\u7d71\u6703\u53e6\u5916\u5efa\u4e00\u500b\u300c<app_name>-svc\u300d\u7684
  // Service\uff0cService \u540d\u7a31\u898f\u5247\u6bd4 Deployment \u66f4\u56b4\u683c\uff08DNS-1035\uff0c\u4e00\u5b9a\u8981\u5b57\u6bcd\u958b\u982d\uff09\uff0c
  // \u5728\u9019\u88e1\u5148\u64cb\uff0c\u4f7f\u7528\u8005\u4e0d\u7528\u7b49\u9001\u51fa\u53bb\u624d\u77e5\u9053\u3002\u8ddf\u5f8c\u7aef _prepare_deploy() \u540c\u4e00\u689d\u898f\u5247\u3002
  if(!/^[a-z]([-a-z0-9]*[a-z0-9])?$/.test(spec.app_name) || spec.app_name.length > 59){
    setFlowError(root,
      `App name\u300c${spec.app_name}\u300d\u683c\u5f0f\u4e0d\u7b26\u5408 K8s \u547d\u540d\u898f\u5247\uff1a\u5fc5\u9808\u4ee5\u5c0f\u5beb\u82f1\u6587\u5b57\u6bcd\u958b\u982d\uff0c\u53ea\u80fd\u5305\u542b\u5c0f\u5beb\u5b57\u6bcd\u3001\u6578\u5b57\u3001` +
      `\u9023\u5b57\u865f\uff08-\uff09\uff0c\u7d50\u5c3e\u4e0d\u80fd\u662f\u9023\u5b57\u865f\uff08\u7cfb\u7d71\u6703\u81ea\u52d5\u5efa\u7acb\u5c0d\u61c9\u7684 Service\uff0c\u540d\u7a31\u898f\u5247\u6bd4 Deployment \u66f4\u56b4\u683c\uff09\u3002` +
      `\u4f8b\u5982\u300c123\u300d\u8981\u6539\u6210\u300capp-123\u300d\u3002`);
    return;
  }
  if(!Number.isInteger(spec.pods) || spec.pods<1 || spec.pods>100){ setFlowError(root, '副本數要在 1 到 100 之間。'); return; }
  if(!Number.isInteger(spec.port) || spec.port<1 || spec.port>65535){ setFlowError(root, 'Port 要在 1 到 65535 之間。'); return; }
  if(spec.memory && !/^\d+(Mi|Gi|Ki|M|G)$/.test(spec.memory)){ setFlowError(root, '記憶體的寫法要像 128Mi 或 1Gi。'); return; }

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
      const ownerEn = cd.port_conflict_other_account ? 'a service in another account' : `<b>${escHtml(cd.port_conflict)}</b>`;
      const ownerZh = cd.port_conflict_other_account ? '其他帳號的服務' : `「<b>${escHtml(cd.port_conflict)}</b>」`;
      const ok = await askConfirm(
        uiLang==='en' ? 'Port already in use' : 'Port 已被佔用',
        uiLang==='en'
          ? `Port <b>${escHtml(spec.port)}</b> is already used by ${ownerEn}. Only one service can be externally reachable on this port, so this one may not be reachable from your browser. Try a different port, or continue anyway?`
          : `Port <b>${escHtml(spec.port)}</b> 已經被${ownerZh}佔用，同一個 port 只有一個服務能真的對外連線，這次部署可能連不到。建議換一個 port；仍要繼續嗎？`
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
      flow.errorMsg = d.error || d.reason || '審查沒有通過。';
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
    flow.step='error'; flow.errorMsg='連線失敗，請確認網路後再試一次。（'+e+'）'; flow.retryStep='spec';
  }
  persistFlowCard(id);
  if(flow.step === 'review') playReview(id);
}

async function flowExecuteDeploy(id){
  const flow = chatFlows[id];
  if(!flowAlive(flow)) return;
  flow.step = 'executing';
  persistFlowCard(id);
  try{
    const r = await fetch('/api/deploy',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({input:flow.originalText, parsed:flow.spec, project_id:flow.projectId || undefined})});
    const d = await r.json();
    if(!flowAlive(flow)) return;
    if(d.error || d.rejected){
      flow.step='error'; flow.errorMsg = d.error || d.reason || '部署被擋下了。';
      flow.blockers = (d.review && d.review.blockers) || []; flow.retryStep='review';
    } else if(d.k8s_deploy && d.k8s_deploy.ok === false){
      flow.step='error';
      flow.errorMsg = '部署紀錄已經寫入，但叢集實際部署失敗：'+d.k8s_deploy.message;
      flow.retryStep='review';
    } else {
      flow.step='done';
      flow.pendingSync = !!(d.k8s_deploy && d.k8s_deploy.pending_sync);
      flow.rollout = {state:'syncing'};
      if(d.parsed) flow.spec = Object.assign({}, flow.spec, {
        app_name:d.parsed.app_name, image:d.parsed.image, pods:d.parsed.pods, port:d.parsed.port});
      loadStats && loadStats();
    }
  }catch(e){
    if(!flowAlive(flow)) return;
    flow.step='error'; flow.errorMsg='連線失敗，請確認網路後再試一次。（'+e+'）'; flow.retryStep='review';
  }
  persistFlowCard(id);
  if(flow.step === 'done') trackRollout(id);
  if(flow.step === 'done') offerScheduleAfterDeploy(flow);
}

// 2026-10-21：「平日 2 個、週末 1 個……幫我部署」這種句子，部署完成後提示可以接著設定分時擴縮
// （分時擴縮只能設定在已經部署的服務上，所以不在部署的同時套用）
function schedulePhraseOf(text){
  const t = String(text || '');
  const m = t.match(/(?:白天|晚上|夜間|晚間|深夜|凌晨|上班時間|辦公時間|平日|工作日|週末|周末|假日|\d{1,2}\s*(?:點|[:：]\d{2}))/);
  if(!m) return '';
  const rest = t.slice(m.index);
  let end = 0;
  for(const x of rest.matchAll(/\d{1,3}\s*(?:個|副本|pods?)/gi)){ if(x.index > 80) break; end = x.index + x[0].length; }
  return end ? rest.slice(0, end).replace(/["“”「」]/g, '').trim() : '';
}
function offerScheduleAfterDeploy(flow){
  if(flow.kind !== 'deploy' || !flow.spec || !flow.spec.app_name) return;
  const phrase = schedulePhraseOf(flow.originalText);
  if(!phrase) return;
  const ch = currentChat();
  if(!ch || !ch.messages.some(m => String(m.content || '').includes('data-flow-id="' + flow.id + '"'))) return;
  const sentence = flow.spec.app_name + ' ' + phrase;
  appendMsg('assistant', `<div>你剛剛也提到時段。服務部署好了，要接著設定分時擴縮嗎？分時擴縮只能設定在已經部署的服務上，所以剛剛沒有一起套用。
    <div style="margin-top:8px"><button type="button" class="btn btn-sm" data-act="example" data-text="${escAttr(sentence)}">把「${esc(sentence)}」放進輸入框</button></div></div>`);
}

// 預約相關句子的規則（跟伺服器的 _SCHED_*_RE 同一套）
const SCHED_TIME_HINT_RE = /今天|明天|後天|今日|明日|分鐘後|小時後|鐘頭後|天後|之後|以後|下週|下周|下個?禮拜|下星期|週[一二三四五六日天]|周[一二三四五六日天]|星期[一二三四五六日天]|禮拜[一二三四五六日天]|\d+\s*月\s*\d+|[一二三四五六七八九十]+月|\d{1,2}\s*[:：]\s*\d{2}|\d\s*點|[一二兩三四五六七八九十]點|早上|上午|中午|下午|晚上|凌晨|傍晚|預約|排程|排定|定時|到時候|每天|每週|每周|每小時|\d{4}-\d{1,2}-\d{1,2}|\b\d{1,2}\/\d{1,2}\b/;
function matchScheduleRule(text){
  if(/(?:取消|刪掉|刪除|不要).{0,20}預約|預約.{0,20}(?:取消|不要了|刪掉)/.test(text)) return 'cancel_schedule';
  if(/預約.{0,20}(?:改到|改成|改在|延到|延後到|提前到|挪到|換到)/.test(text)) return 'reschedule';
  if(/(?:我有|有沒有|有哪些|有什麼|列出|查看|顯示|看一下).{0,6}(?:預約|排程)|預約(?:清單|列表|了什麼|了哪些)|(?:今天|明天|後天|這週|這周|本週|下週|下周|這個月|下個月).{0,4}(?:要做什麼|有什麼事|有什麼預約|有哪些預約|的預約|行程)/.test(text)) return 'list_schedule';
  return null;
}

async function startDestructiveFlow(kind, args, sched){
  const flow = newFlow(kind, '', Object.assign({}, args));
  if(sched && SCHED_KINDS.includes(kind)){ flow.schedOn = true; flow.when = sched.when || {ts:defaultSchedTs()}; }
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
      flow.pendingSync = !!d.pending_sync;
      flow.resultMsg = d.message || (flow.kind==='healer_auto_fix' ? `已處理 ${d.fixed||0} 個 Pod。` : '已完成：' + destructiveSummary(flow) + '。');
      loadStats && loadStats();
    } else {
      flow.step='error'; flow.errorMsg = d.error || d.message || '操作失敗。'; flow.retryStep='spec';
    }
  }catch(e){
    if(!flowAlive(flow)) return;
    flow.step='error'; flow.errorMsg='連線失敗，請確認網路後再試一次。（'+e+'）'; flow.retryStep='spec';
  }
  persistFlowCard(id);
}

async function startDeployFlow(text, sched, originalText){
  try{
    const r = await fetch('/api/deploy/parse',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({input:text})});
    const d = await r.json();
    if(Array.isArray(d.missing) && d.missing.length && d.parsed && !d.rejected){
      // 2026-10-21：句子沒講到映像檔等必要欄位時，不再顯示英文錯誤，直接出部署單讓使用者補（審查流程不變）
      const p = d.parsed;
      const what = {app_name:'服務名稱（例如 shop-web）', image:'映像檔：要跑哪個程式，例如 nginx:1.27、redis:7', pods:'副本數：要同時開幾個'};
      appendMsg('assistant', '還差' + d.missing.map(k => what[k] || k).join('；') + '。請在下面的部署單補上，再按「下一步」。', null, 'text');
      const flow = newFlow('deploy', text, {app_name:p.app_name || '', image:p.image || '', pods:p.pods || 1, port:p.port || 80,
                                             memory:p.memory, cpu:p.cpu});
      if(sched){ flow.schedOn = true; flow.when = sched.when || {ts:defaultSchedTs()}; flow.fullText = originalText || text; }   // 預約（2026-10-23 合併時補上）
      appendMsg('assistant', renderFlowCard(flow));
      return;
    }
    if(d.error || d.rejected){
      appendMsg('assistant', '沒辦法部署：' + (d.error || d.reason || '審查沒有通過。'), null, 'text');
      return;
    }
    const raw = d.raw || d.parsed || {};
    const flow = newFlow('deploy', text, {
      app_name: raw.app_name, image: raw.image, pods: raw.pods, port: raw.port,
      memory: raw.memory, cpu: raw.cpu,
    });
    flow.review = d.review || null;
    flow.resource = d.resource_summary || null;
    flow.scenario = d.scenario || null;
    if(sched){ flow.schedOn = true; flow.when = sched.when || {ts:defaultSchedTs()}; flow.fullText = originalText || text; }
    appendMsg('assistant', renderFlowCard(flow));
  }catch(e){
    appendMsg('assistant', '解析部署需求時連線失敗，請再試一次。（' + e + '）', null, 'text');
  }
}

// 訊息排隊依序處理（2026-10-21 全系統檢查）：上一則還在處理時再送一則，快的那則會先回答、排在慢的前面，
// 而且快的一回來「思考中」就消失，使用者以為慢的那則已經結束。現在使用者可以連續送出，但照順序回答。
let chatQueue = Promise.resolve();
let chatPending = 0;
async function sendChat(){
  const inp = document.getElementById('chat-input');
  const text = inp.value.trim();
  if(!text) return;
  inp.value = '';
  inp.style.height = '';
  if(!currentChatId) newChat();
  appendMsg('user', text);
  chatPending++;
  if(chatPending > 1) appendTyping('排隊中，前一則回答完就會處理這則');
  const job = chatQueue.then(() => handleChat(text));
  chatQueue = job.catch(() => {});
  try { await job; }
  finally {
    chatPending--;
    if(chatPending > 0) appendTyping('處理下一則訊息');
  }
}

async function handleChat(text){
  // 貼上的是 docker-compose 內容：直接走匯入流程
  if(looksLikeCompose(text)){
    try{ await startComposeFlow(text, '貼上的內容'); }
    finally{ removeTyping(); }
    return;
  }
  appendTyping('Understanding');

  // 預約相關的查詢／取消／改時間：句子裡常帶著時間和「部署、刪除」，要在讀時間之前先判斷
  const schedIntent = matchScheduleRule(text);
  if(schedIntent){
    try{ setTyping('Fetching'); await runScheduleIntent(schedIntent, text); }
    catch(e){ appendMsg('assistant', '讀取預約時發生錯誤：' + e, null, 'text'); }
    finally{ removeTyping(); }
    return;
  }

  // 一句話帶時間（「明天早上 9 點部署 2 個 nginx」）：讀出時間、把時間那段拿掉，剩下的照常判斷要做什麼
  let when = null, schedHint = false, body = text;
  // 2026-10-23 合併時補上：分時擴縮的句子（「shop-web 9 點到 18 點 3 個，其他時間 1 個」）也帶著時間，不能被當成一次性預約
  const asHit = matchClientRule(text);
  if(SCHED_TIME_HINT_RE.test(text) && !(asHit && String(asHit.action || '').startsWith('as_'))){
    try{
      const w = await fetch('/api/schedule/when', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({text})}).then(r => r.json());
      if(w.recurring){
        removeTyping();
        appendMsg('assistant', `<div class="md"><p>「${escHtml(w.matched || '')}」這種重複排程目前不支援，預約只能排一次性的操作。</p><p>想依時段自動調整副本數，請使用分時擴縮功能；想排一次性的操作，可以說「明天早上 9 點把 web 擴到 3 個」。</p></div>`);
        return;
      }
      if(w.found){ when = {ts:w.ts, display:w.display}; body = w.rest || text; }
      else if(w.hint){ schedHint = true; body = text.replace(/預約|排程|排定|定時|到時候|屆時/g, '').trim() || text; }
    }catch(e){ /* 讀不出時間就當成一般句子處理 */ }
  }

  let intent = matchClientRule(body);
  if(!intent){
    try{
      const r = await fetch('/api/intent',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:body})});
      intent = await r.json();
    }catch(e){ intent = {action:'qa', args:{}, source:'error'}; }
  }

  const action = intent.action || 'qa';
  const sched = (when || schedHint) ? {when, on:true} : null;
  if(sched && !SCHED_KINDS.includes(action) && action !== 'qa'){
    removeTyping();
    appendMsg('assistant', '<div class="md"><p>預約只能排<b>部署、擴縮、換映像檔、刪除</b>這四種操作。這個要求請在需要的時候直接說，或換個說法，例如「明天早上 9 點把 web 擴到 3 個」。</p></div>');
    return;
  }
  try{
    if(action==='qa'){ await runQA(text); }
    else if(action==='clarify'){ startClarify(intent); }
    else if(READ_ACTIONS.includes(action)){ window.__lastIntentArgs = intent.args||{}; setTyping('Fetching'); await runReadAction(action); }
    else if(action.startsWith('as_')){ setTyping('Working'); await runAutoscaleAction(action, intent.args||{}, text); }
    else if(action==='deploy'){ setTyping('Parsing'); await startDeployFlow(body, sched, text); }
    else if(DESTRUCTIVE_KINDS.includes(action)){ setTyping('Preparing'); await startDestructiveFlow(action, intent.args||{}, sched); }
    else { await runQA(text); }
  }catch(e){
    appendMsg('assistant','處理的時候發生錯誤：'+e, null, 'text');
  }finally{
    removeTyping();
  }
}



// ══════════════════════════════════════════════════════════════════
//  對話互動模組（2026-10-09 改版第二階段）
//  系統相關的回答不再是純文字：Pod、服務、部署紀錄、自動修復、資源用量都變成可以
//  篩選、排序、換頁、點進去看詳情的模組。模組的資料與狀態（頁碼、篩選）存在 HTML 的
//  data 屬性裡，跟訊息一起寫進 localStorage，重新整理後照樣能操作；所有點擊都用
//  事件委派處理，不靠綁在元素上的函式，所以重新渲染不會失效。
//  動態只用在兩種地方：回應使用者的操作，以及反映真實狀態（審查、部署進度）。
// ══════════════════════════════════════════════════════════════════
const TYPING_LABELS = {Thinking:'思考中', Understanding:'理解你的需求', Fetching:'查詢中', Parsing:'解析部署需求',
  Preparing:'準備中', Reviewing:'安全、成本、效能三項審查進行中', Deploying:'正在部署', Working:'處理中'};
const W_PER = 10;
const CHEV_L = '<svg width="14" height="14" viewBox="0 0 16 16" aria-hidden="true"><path d="M10 3L5 8l5 5" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>';
const CHEV_R = '<svg width="14" height="14" viewBox="0 0 16 16" aria-hidden="true"><path d="M6 3l5 5-5 5" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>';

function encState(o){ return encodeURIComponent(JSON.stringify(o == null ? null : o)); }
function decState(s){ try { return JSON.parse(decodeURIComponent(s || '')); } catch(e){ return null; } }
function zUid(p){ return p + '_' + Date.now().toString(36) + Math.random().toString(36).slice(2, 6); }
function nowHM(){ return new Date().toLocaleTimeString('zh-TW', {hour12:false, hour:'2-digit', minute:'2-digit'}); }
function reducedMotion(){ return window.matchMedia('(prefers-reduced-motion: reduce)').matches; }
function isHtmlContent(c){ return /^\s*</.test(String(c == null ? '' : c)); }

async function getJSON(url){
  const r = await fetch(url);
  if(r.status === 401) throw new Error('登入已經過期，請重新整理頁面後再登入。');
  let d = null;
  try { d = await r.json(); } catch(e){ throw new Error('伺服器回應格式不正確（HTTP ' + r.status + '），請稍後再試。'); }
  if(r.status >= 500) throw new Error((d && (d.error || d.message)) || ('伺服器發生錯誤（HTTP ' + r.status + '）'));
  return d;
}

// ── 一般問答：先跳脫再排版（模型輸出不能直接當 HTML，否則等於 XSS） ──
function mdInline(s){
  return escHtml(String(s))
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[^\w])_([^_\n]+)_(?=[^\w]|$)/g, '$1<em>$2</em>');
}
function mdToHtml(src){
  const lines = String(src == null ? '' : src).replace(/\r/g, '').split('\n');
  const out = []; let inCode = false, code = [], list = null;
  const closeList = () => { if(list){ out.push('</' + list + '>'); list = null; } };
  for(const ln of lines){
    if(/^\s*```/.test(ln)){
      if(inCode){ out.push('<pre><code>' + escHtml(code.join('\n')) + '</code></pre>'); code = []; inCode = false; }
      else { closeList(); inCode = true; }
      continue;
    }
    if(inCode){ code.push(ln); continue; }
    let m;
    if((m = ln.match(/^\s*[-*•·]\s+(.*)/))){ if(list !== 'ul'){ closeList(); out.push('<ul>'); list = 'ul'; } out.push('<li>' + mdInline(m[1]) + '</li>'); continue; }
    if((m = ln.match(/^\s*\d+[.)、]\s*(.*)/))){ if(list !== 'ol'){ closeList(); out.push('<ol>'); list = 'ol'; } out.push('<li>' + mdInline(m[1]) + '</li>'); continue; }
    closeList();
    if((m = ln.match(/^\s*#{1,4}\s+(.*)/))){ out.push('<h4>' + mdInline(m[1]) + '</h4>'); continue; }
    if(!ln.trim()) continue;
    out.push('<p>' + mdInline(ln) + '</p>');
  }
  if(inCode) out.push('<pre><code>' + escHtml(code.join('\n')) + '</code></pre>');
  closeList();
  return '<div class="md">' + out.join('') + '</div>';
}
// 給 /api/chat 的對話紀錄用：模組只送一行摘要，不把整段 HTML 和資料丟給模型
function msgPlainText(c){
  const s = String(c == null ? '' : c);
  if(!isHtmlContent(s)) return s.slice(0, 600);
  const m = s.match(/data-summary="([^"]*)"/);
  const div = document.createElement('div'); div.innerHTML = s;
  const lead = div.querySelector('.w-lead');
  const head = lead ? lead.textContent.trim() : '';
  if(m){ const t = document.createElement('textarea'); t.innerHTML = m[1]; return (head ? head + ' ' : '') + '[' + t.value + ']'; }
  return (div.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 300);
}

function renderMsgHTML(role, content, sources, fmt){
  if(role === 'user') return `<div class="msg user"><div class="msg-bubble">${escHtml(String(content == null ? '' : content))}</div></div>`;
  const body = (fmt !== 'text' && isHtmlContent(content)) ? content : mdToHtml(content);
  return `<div class="msg ai"><div class="msg-bubble">${body}${renderSourcesHTML(sources)}</div></div>`;
}
function appendMsg(role, content, sources, fmt){
  const ch = currentChat();
  if(!ch) return;
  const m = {role, content, sources};
  if(fmt) m.fmt = fmt;
  ch.messages.push(m);
  if(ch.messages.length === 1 && role === 'user'){
    ch.title = content.slice(0, 30) + (content.length > 30 ? '...' : '');
  }
  saveChats();
  renderChatList();
  const msgs = document.getElementById('chat-messages');
  if(ch.messages.length === 1) msgs.innerHTML = '';
  msgs.insertAdjacentHTML('beforeend', renderMsgHTML(role, content, sources, fmt));
  const added = msgs.lastElementChild;
  if(added) added.classList.add('enter');
  msgs.scrollTop = msgs.scrollHeight;
}
function appendTyping(label){
  const msgs = document.getElementById('chat-messages');
  let div = document.getElementById('typing-indicator');
  if(!div){
    div = document.createElement('div');
    div.className = 'msg ai'; div.id = 'typing-indicator';
    msgs.appendChild(div);
  } else if(div !== msgs.lastElementChild){
    msgs.appendChild(div);   // 永遠留在最底下，不要卡在後來送出的訊息上方
  }
  const txt = TYPING_LABELS[label] || label || '思考中';
  div.innerHTML = '<div class="think-row"><div class="spinner"></div><span data-role="think-label">' + escHtml(txt) + '</span></div>';
  msgs.scrollTop = msgs.scrollHeight;
}

async function runQA(text){
  setTyping('Thinking');
  try{
    const hist = (currentChat()?.messages || []).slice(-10).map(m => ({role: m.role === 'assistant' ? 'assistant' : 'user', content: msgPlainText(m.content)}));
    const r = await fetch('/api/chat', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({message:text, history:hist, project_id:currentChat()?.projectId || undefined})});
    const d = await r.json();
    removeTyping();
    appendMsg('assistant', d.reply || d.error || '沒有收到回覆，請再問一次。', d.sources, 'text');
  }catch(e){ removeTyping(); appendMsg('assistant', '連線失敗，請確認網路後再試一次。（' + e + '）', null, 'text'); }
}

// ── 狀態判讀 ──
function podState(p){
  const phase = p.phase || 'Unknown';
  const ready = (p.conditions || []).find(c => c.type === 'Ready');
  if(phase === 'Running' && (!ready || ready.status === 'True')){
    if(p.restarts < 5) return {cls:'run', label:'執行中', attn:false};
    // 重啟次數會一直累加（主機重開機每個 Pod 也會算一次），最近一小時內又重啟才算需要注意
    const mins = p.lastRestart ? (Date.now() - Date.parse(p.lastRestart)) / 60000 : null;
    if(mins != null && mins > 60) return {cls:'run', label:'執行中（曾重啟 ' + p.restarts + ' 次，最近一次在 ' + agoText(mins) + '）', attn:false};
    return {cls:'warn', label:'執行中，曾重啟 ' + p.restarts + ' 次', attn:true};
  }
  if(phase === 'Running') return {cls:'fail', label:'執行中但還沒準備好', attn:true};
  if(phase === 'Pending') return {cls:'pend', label:'等待中', attn:true};
  if(phase === 'Succeeded') return {cls:'done', label:'已完成', attn:false};
  if(phase === 'Failed') return {cls:'fail', label:'失敗', attn:true};
  return {cls:'pend', label:'狀態不明', attn:true};
}
function agoText(mins){
  if(mins < 120) return Math.round(mins) + ' 分鐘前';
  if(mins < 48 * 60) return Math.round(mins / 60) + ' 小時前';
  return Math.round(mins / 1440) + ' 天前';
}
function stateTag(s){ return `<span class="state s-${s.cls}"><i></i>${esc(s.label)}</span>`; }
function humanSummary(s){
  return String(s || '')
    .replace(/^phase=Pending/, '排程或啟動中')
    .replace(/^phase=(\w+)/, '狀態 $1')
    .replace(/容器尚未全部 ready/, '還有容器沒準備好')
    .replace(/(\d+) 個容器都 ready/, '$1 個容器都已就緒')
    .replace(/^(\d+)\/(\d+) ready$/, '$1/$2 個副本就緒')
    .replace(/^Running，/, '執行中，');
}
function healthTag(pd){
  if(pd.healthy) return stateTag({cls:'run', label:'健康'});
  if((pd.phase || '') === 'Pending') return stateTag({cls:'pend', label:'等待中'});
  return stateTag({cls:'fail', label:'有狀況'});
}
function slimPod(p){
  const c0 = (p.containers || [])[0] || {};
  return {name:p.name, app:p.app || '', phase:p.phase || 'Unknown', restarts:p.restarts || 0, age:p.age || '',
          lastRestart:p.last_restart_at || null,
          image:c0.image || '', node:p.node || '', conditions:(p.conditions || []).filter(c => c.type === 'Ready')};
}
function cleanCommitMsg(m){
  return String(m || '').replace(/^gitops:\s*/i, '').replace(/\s*\[[^\]]*\]\s*$/, '')
    .replace(/^deploy\s+(\S+)\s+to\s+\S+$/i, '部署 $1')
    .replace(/^rollback\s+(\S+).*$/i, '回滾 $1')
    .replace(/^scale\s+(\S+)\s+to\s+(\d+).*$/i, '把 $1 調成 $2 個副本')
    .replace(/^delete\s+(\S+).*$/i, '刪除 $1');
}

function esc(v){ return escHtml(String(v==null?'':v)); }

// ── 共用版面零件 ──
function pagerHTML(total, page, per){
  const pages = Math.max(1, Math.ceil(total / per));
  page = Math.min(Math.max(1, page), pages);
  if(total <= per) return '';
  const from = (page - 1) * per + 1, to = Math.min(page * per, total);
  const show = new Set([1, pages, page - 1, page, page + 1]);
  let nums = '', last = 0;
  for(let i = 1; i <= pages; i++){
    if(!show.has(i)) continue;
    if(i - last > 1) nums += '<span class="gap">…</span>';
    nums += `<button type="button" data-act="page" data-p="${i}" ${i === page ? 'aria-current="page"' : ''} aria-label="第 ${i} 頁">${i}</button>`;
    last = i;
  }
  return `<nav class="pager" aria-label="換頁"><span class="info">第 ${from} 到 ${to} 筆，共 ${total} 筆</span>
    <button type="button" data-act="page" data-p="${page - 1}" ${page <= 1 ? 'disabled' : ''} aria-label="上一頁">${CHEV_L}</button>${nums}
    <button type="button" data-act="page" data-p="${page + 1}" ${page >= pages ? 'disabled' : ''} aria-label="下一頁">${CHEV_R}</button></nav>`;
}
function sortTh(st, key, label, cls){
  const aria = st.k === key ? `aria-sort="${(st.dir || 1) > 0 ? 'ascending' : 'descending'}"` : '';
  return `<th scope="col" class="${cls || ''}" ${aria}><button type="button" data-act="sort" data-k="${key}">${label}</button></th>`;
}
function filterChips(st, opts){
  const f = st.f || 'all';
  return opts.map(([k, l]) => `<button type="button" class="chip" data-act="filter" data-f="${k}" aria-pressed="${f === k}">${l}</button>`).join('');
}
function pageSlice(arr, st){
  const pages = Math.max(1, Math.ceil(arr.length / W_PER));
  const p = Math.min(Math.max(1, st.p || 1), pages);
  return arr.slice((p - 1) * W_PER, p * W_PER);
}
function sortBy(arr, key, dir){
  return arr.slice().sort((a, b) => {
    const x = a[key], y = b[key];
    return (x > y ? 1 : x < y ? -1 : 0) * (dir || 1);
  });
}
function flowBtn(kind, args, label, cls){
  return `<button type="button" class="btn ${cls || ''}" data-act="flow" data-kind="${kind}" data-args="${encState(args)}">${label}</button>`;
}
function emptyBlock(title, desc, actionHtml){
  return `<div class="w-empty"><b>${esc(title)}</b>${esc(desc || '')}${actionHtml || ''}</div>`;
}
function exampleBtn(text, label){
  return `<button type="button" class="btn" data-act="example" data-text="${esc(text)}">${esc(label)}</button>`;
}

// ── 模組定義 ──
const W = {};
W.pods = {
  summary: d => `Pod 清單：共 ${d.items.length} 個，${d.items.filter(p => podState(p).attn).length} 個需要注意`,
  head: (d, st) => {
    const a = d.items.filter(p => podState(p).attn).length;
    return `<h2>Pod</h2><span class="meta"><span>共 ${d.items.length} 個</span>${a ? `<b class="t-warn">${a} 個需要注意</b>` : ''}<span>${esc(d.at)} 的資料</span></span>
      <div class="filters" role="group" aria-label="依狀態篩選">${filterChips(st, [['all','全部'], ['ok','正常'], ['attn','需要注意']])}
      <button type="button" class="chip ghost" data-act="refresh">重新整理</button></div>`;
  },
  body: (d, st) => {
    if(!d.items.length) return emptyBlock('目前沒有任何 Pod。', '部署一個服務之後，它的 Pod 會出現在這裡。', exampleBtn('部署 2 個 nginx:1.27，開 80 port', '部署一個試試看'));
    let rows = d.items.map(p => Object.assign({}, p, {attnKey:(podState(p).attn ? '0' : '1') + p.name}));
    if(st.f === 'ok') rows = rows.filter(p => !podState(p).attn);
    if(st.f === 'attn') rows = rows.filter(p => podState(p).attn);
    if(!rows.length) return emptyBlock(st.f === 'attn' ? '沒有需要注意的 Pod。' : '沒有符合條件的 Pod。', '');
    rows = sortBy(rows, st.k || 'attnKey', st.dir || 1);
    const slice = pageSlice(rows, st);
    return `<div class="table-wrap"><table><thead><tr>${sortTh(st, 'name', '名稱')}<th scope="col">狀態</th><th scope="col">映像檔</th>${sortTh(st, 'restarts', '重啟次數', 'r')}${sortTh(st, 'age', '建立時間', 'r')}</tr></thead><tbody>
      ${slice.map((p, i) => `<tr class="enter" style="animation-delay:${i * 25}ms" tabindex="0" data-act="open-pod" data-name="${esc(p.name)}">
        <td class="id">${esc(p.name)}</td><td>${stateTag(podState(p))}</td><td class="id dim">${esc(p.image)}</td>
        <td class="r num ${p.restarts >= 5 ? 't-stop' : ''}">${p.restarts}</td><td class="r dim">${esc(p.age)}</td></tr>`).join('')}
      </tbody></table></div>${pagerHTML(rows.length, st.p || 1, W_PER)}`;
  },
};
function repsHTML(ready, replicas){
  const n = Math.min(replicas, 12);
  return `<span class="reps ${ready < replicas ? 'short' : ''}" aria-hidden="true">${Array.from({length:n}, (_, i) => `<i class="${i < ready ? 'on' : ''}"></i>`).join('')}</span><span class="num">${ready}/${replicas}</span>`;
}
W.deployments = {
  summary: d => `服務清單：共 ${d.items.length} 個`,
  head: (d, st) => {
    const short = d.items.filter(x => (x.ready || 0) < (x.replicas || 0)).length;
    return `<h2>服務</h2><span class="meta"><span>共 ${d.items.length} 個</span>${short ? `<b class="t-warn">${short} 個還沒全部就緒</b>` : ''}<span>${esc(d.at)} 的資料</span></span>
      <div class="filters"><button type="button" class="chip ghost" data-act="refresh">重新整理</button></div>`;
  },
  body: (d, st) => {
    if(!d.items.length) return emptyBlock('目前沒有任何服務。', '用一句話描述你想部署的東西就可以開始。', exampleBtn('部署 2 個 nginx:1.27，開 80 port', '部署一個試試看'));
    const rows = sortBy(d.items, st.k || 'name', st.dir || 1);
    const slice = pageSlice(rows, st);
    return `<div class="table-wrap"><table><thead><tr>${sortTh(st, 'name', '名稱')}<th scope="col">就緒的副本</th><th scope="col">映像檔</th>${sortTh(st, 'updated', '最後更新', 'r')}</tr></thead><tbody>
      ${slice.map((x, i) => `<tr class="enter" style="animation-delay:${i * 25}ms" tabindex="0" data-act="open-dep" data-name="${esc(x.name)}">
        <td class="id">${esc(x.name)}</td><td>${x.sleeping ? stateTag({cls:'sleep', label:`休眠中，醒來開 ${x.sleep_replicas} 個`}) : repsHTML(x.ready || 0, x.replicas || 0)}</td><td class="id dim">${esc(x.image || '')}</td>
        <td class="r dim">${esc(x.updated || x.age || '')}</td></tr>`).join('')}
      </tbody></table></div>${pagerHTML(rows.length, st.p || 1, W_PER)}`;
  },
};
W.gitops = {
  summary: d => `部署紀錄：共 ${d.items.length} 筆`,
  head: d => `<h2>部署紀錄</h2><span class="meta"><span>共 ${d.items.length} 筆</span><span>${esc(d.at)} 的資料</span></span>
    <div class="filters"><button type="button" class="chip ghost" data-act="refresh">重新整理</button></div>`,
  body: (d, st) => {
    if(!d.items.length) return emptyBlock('還沒有任何部署紀錄。', '部署之後，這裡會留下一筆可以追查的版本。');
    const firstIdx = {};
    d.items.forEach((c, i) => { if(c.app && firstIdx[c.app] == null) firstIdx[c.app] = i; });
    const pages = Math.max(1, Math.ceil(d.items.length / W_PER));
    const p = Math.min(Math.max(1, st.p || 1), pages);
    const base = (p - 1) * W_PER;
    const slice = d.items.slice(base, base + W_PER);
    return `<ol class="timeline">${slice.map((c, i) => `<li class="enter" style="animation-delay:${i * 30}ms">
        <time>${esc(c.time || '')}</time>
        <div class="what">${c.app ? `<span class="app">${esc(c.app)}</span>` : ''}${esc(cleanCommitMsg(c.message))}<span class="hash">${esc(c.hash || '')}</span></div>
        <div>${(c.app && firstIdx[c.app] === base + i) ? flowBtn('rollback', {name:c.app}, '回滾到上一版', 'btn-sm') : ''}</div></li>`).join('')}</ol>
      ${pagerHTML(d.items.length, p, W_PER)}`;
  },
};
W.healer = {
  summary: d => d.items.length ? `Healer 掃描：${d.items.length} 個 Pod 需要修復` : ((d.watch || []).length ? `Healer 掃描：沒有需要自動修復的，但有 ${(d.watch || []).length} 個 Pod 狀態不正常` : 'Healer 掃描：全部正常'),
  head: d => `<h2>Healer 掃描結果</h2><span class="meta"><span>${esc(d.at)} 掃描</span></span>
    <div class="filters">${d.items.length > 1 ? flowBtn('healer_auto_fix', {}, '全部修復', 'btn-sm') : ''}<button type="button" class="chip ghost" data-act="refresh">重新掃描</button></div>`,
  body: (d, st) => {
    const watch = d.watch || [];
    const watchHtml = watch.length ? `<ul class="issues">${watch.slice(0, W_PER).map(w => `<li><div class="what"><span class="id">${esc(w.name)}</span><span class="t-warn">${esc(w.label)}</span></div>
        <button type="button" class="btn btn-sm" data-act="open-pod" data-name="${esc(w.name)}">看詳情</button></li>`).join('')}</ul>` : '';
    if(!d.items.length && watch.length) return `<div class="w-empty"><b>沒有需要自動修復的 Pod，但有 ${watch.length} 個狀態不正常。</b>它們可能剛啟動、或剛開始出狀況，還沒達到 Healer 自動修復的條件（例如重啟 3 次以上）。Healer 每 30 秒會再檢查一次，你也可以先看詳情。</div>${watchHtml}`;
    if(!d.items.length) return `<div class="w-empty"><div class="radar" aria-hidden="true"></div><b>所有 Pod 都正常。</b>Healer 每 30 秒會在背景自動掃描一次，有狀況會自動修復。</div>`;
    const slice = pageSlice(d.items, st);
    return `<ul class="issues">${slice.map((it, i) => {
      const pod = it.pod_name || it.pod || '';
      return `<li class="enter" style="animation-delay:${i * 30}ms"><div class="what"><span class="id">${esc(pod)}</span><span class="t-stop">${esc(it.reason || it.status || '狀態異常')}</span></div>
        <button type="button" class="btn btn-sm" data-act="open-pod" data-name="${esc(pod)}">看詳情</button>${flowBtn('healer_fix', {pod_name:pod}, '讓 Healer 修復', 'primary btn-sm')}</li>`;
    }).join('')}</ul>${pagerHTML(d.items.length, st.p || 1, W_PER)}`;
  },
};
function ringHTML(pct){
  const p = Math.max(0, Math.min(100, Number(pct) || 0));
  const C = 226.2;
  const cls = p >= 100 ? 'full' : p >= 80 ? 'hot' : '';
  return `<svg class="ring ${cls}" viewBox="0 0 84 84" aria-hidden="true"><circle class="track" cx="42" cy="42" r="36"/><circle class="val" cx="42" cy="42" r="36" stroke-dasharray="${C}" stroke-dashoffset="${(C * (1 - p / 100)).toFixed(1)}"/><text x="42" y="48" text-anchor="middle">${Math.round(p)}%</text></svg>`;
}
W.resources = {
  summary: d => {
    const cap = d.cluster_capacity || {}, used = d.cluster_used || {};
    return `叢集資源：CPU 已用 ${used.cpu_cores ?? '?'} / ${cap.cpu_cores ?? '?'} 核，記憶體已用 ${used.mem_gib ?? '?'} / ${cap.mem_gib ?? '?'} GiB`;
  },
  head: d => `<h2>叢集資源</h2><span class="meta"><span>${esc(d.at)} 的資料</span></span>
    <div class="filters"><button type="button" class="chip ghost" data-act="refresh">重新整理</button></div>`,
  body: d => {
    const cap = d.cluster_capacity, used = d.cluster_used || {}, rem = d.cluster_remaining || {};
    if(!cap) return emptyBlock('目前讀不到叢集容量。', d.k8s_enabled === false ? 'Kubernetes 沒有連線，請先確認 Docker Desktop 的 Kubernetes 已經啟動。' : '請稍後按「重新整理」再試一次。');
    const cpuPct = cap.cpu_cores ? used.cpu_cores / cap.cpu_cores * 100 : 0;
    const memPct = cap.mem_gib ? used.mem_gib / cap.mem_gib * 100 : 0;
    const usedNote = d.cluster_used_source && d.cluster_used_source.cpu === 'requests' ? 'CPU 以預留量計算' : '';
    return `<div class="gauges">
        <div class="gauge-cell">${ringHTML(cpuPct)}<div><div class="lbl">CPU</div><div class="big">${used.cpu_cores} / ${cap.cpu_cores} 核</div><div class="dim">還剩 ${rem.cpu_cores} 核</div></div></div>
        <div class="gauge-cell">${ringHTML(memPct)}<div><div class="lbl">記憶體</div><div class="big">${used.mem_gib} / ${cap.mem_gib} GiB</div><div class="dim">還剩 ${rem.mem_gib} GiB</div></div></div>
      </div>
      <dl class="facts">
        <div><dt>我的服務</dt><dd>${(d.my_deployments || []).length} 個</dd></div>
        <div><dt>我佔用的資源</dt><dd>${d.my_total_cpu_cores ?? 0} 核 / ${d.my_total_mem_gib ?? 0} GiB</dd></div>
        <div><dt>依公有雲牌價估算</dt><dd>每月 $${d.my_monthly_usd ?? 0}</dd></div>
      </dl>
      ${usedNote ? `<div class="w-note">${usedNote}，記憶體以實際用量計算。</div>` : ''}`;
  },
};
W.poddetail = {
  summary: d => `Pod 詳情：${d.name}，${d.pods.map(p => p.healthy ? '健康' : (p.health_summary || '有狀況')).join('；')}`,
  head: d => `<h2>Pod 詳情</h2><span class="meta"><span class="id">${esc(d.name)}</span><span>${esc(d.at)} 的資料</span></span>
    <div class="filters"><button type="button" class="chip ghost" data-act="refresh">重新整理</button></div>`,
  body: d => d.pods.map(pd => podDetailHTML(pd, {inline:true})).join(''),
};
W.depdetail = {
  summary: d => `服務詳情：${d.dep.name}，${d.dep.ready}/${d.dep.replicas} 個副本就緒`,
  head: d => `<h2>服務詳情</h2><span class="meta"><span class="id">${esc(d.dep.name)}</span><span>${esc(d.at)} 的資料</span></span>
    <div class="filters"><button type="button" class="chip ghost" data-act="refresh">重新整理</button></div>`,
  body: d => depDetailHTML(d.dep, null, {inline:true}),
};


// ── 分析報告 ──
// 三種內容分開標示：統計（從紀錄算出來）、系統觀察（固定規則依數字寫的）、AI 解讀（數字已核對）
const RP_DAYS = [[1, '今天'], [7, '7 天'], [30, '30 天'], [90, '90 天']];
const RP_KIND = {deploy:'部署', review:'審查', scale:'擴縮', update:'換映像檔', delete:'刪除', rollback:'回滾', heal:'自動修復', sleep:'休眠', wake:'喚醒'};
function rpCount(n, dec){ return `<b class="count-up" data-to="${Number(n) || 0}" data-dec="${dec || 0}">${esc(dec ? Number(n || 0).toFixed(dec) : (n || 0))}</b>`; }
function rpDaily(series){
  const days = series || [];
  if(!days.length) return '<p class="rp-none">沒有資料。</p>';
  const W = 760, H = 170, padL = 26, padB = 22, padT = 8;
  const max = Math.max(1, ...days.map(d => d.deploys + d.blocks + d.heals + d.ops));
  const bw = (W - padL - 4) / days.length;
  const gap = Math.min(4, bw * .25);
  const y = v => (H - padB) - v / max * (H - padB - padT);
  const parts = [['deploys', 'var(--signal)'], ['ops', 'var(--text-3)'], ['blocks', 'var(--caution)'], ['heals', 'var(--pass)']];
  const step = Math.max(1, Math.ceil(days.length / 8));
  let bars = '', labels = '';
  days.forEach((d, i) => {
    let acc = 0;
    const x = padL + i * bw + gap / 2;
    const tip = `${d.date}：部署 ${d.deploys}、其他操作 ${d.ops}、審查擋下 ${d.blocks}、自動修復 ${d.heals}`;
    bars += `<g data-tip="${esc(tip)}">`;
    parts.forEach(([k, c]) => {
      const v = d[k] || 0;
      if(!v) return;
      const y1 = y(acc + v), h = y(acc) - y1;
      bars += `<rect class="bar" x="${x.toFixed(1)}" y="${y1.toFixed(1)}" width="${Math.max(1, bw - gap).toFixed(1)}" height="${h.toFixed(1)}" rx="1.5" fill="${c}" style="animation-delay:${Math.min(900, i * 18)}ms"></rect>`;
      acc += v;
    });
    if(!acc) bars += `<rect x="${x.toFixed(1)}" y="${(H - padB - 1).toFixed(1)}" width="${Math.max(1, bw - gap).toFixed(1)}" height="1" fill="var(--grid-2)"></rect>`;
    bars += '</g>';
    if(i % step === 0) labels += `<text x="${(x + (bw - gap) / 2).toFixed(1)}" y="${H - 6}" text-anchor="middle">${esc(d.date.slice(5))}</text>`;
  });
  const grid = [0, .5, 1].map(f => { const v = Math.round(max * f); return `<line class="axis" x1="${padL}" x2="${W}" y1="${y(v).toFixed(1)}" y2="${y(v).toFixed(1)}" stroke-dasharray="${f ? '2 4' : ''}"/><text x="${padL - 6}" y="${(y(v) + 4).toFixed(1)}" text-anchor="end">${v}</text>`; }).join('');
  return `<svg class="rp-chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="每日活動">${grid}${bars}${labels}</svg>
    <div class="rp-legend"><span><i style="background:var(--signal)"></i>部署</span><span><i style="background:var(--text-3)"></i>擴縮、換映像檔等操作</span><span><i style="background:var(--caution)"></i>審查擋下</span><span><i style="background:var(--pass)"></i>自動修復</span></div>`;
}
function rpHBars(pairs, color, empty){
  if(!pairs || !pairs.length) return `<p class="rp-none">${esc(empty)}</p>`;
  const max = Math.max(...pairs.map(p => p[1]));
  return `<div class="rp-hbars">${pairs.map((p, i) => `<div class="rp-hbar" style="--i:${i};--hc:${color}"><span class="lbl" data-tip="${esc(p[0])}">${esc(p[0])}</span><b>${esc(p[1])}</b><div class="track"><div class="fill" style="width:${Math.max(4, Math.round(p[1] / max * 100))}%"></div></div></div>`).join('')}</div>`;
}
function rpTime(ts){
  const d = new Date(ts * 1000);
  return `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}
W.report = {
  summary: d => `分析報告：${d.period.start} 到 ${d.period.end}`,
  head: d => `<h2>分析報告</h2><span class="meta"><span>${esc(d.period.start)} 到 ${esc(d.period.end)}</span><span>${esc(d.at || '')} 產生</span></span>
    <div class="filters"><div class="rp-period" role="group" aria-label="報告期間">${RP_DAYS.map(([n, l]) => `<button type="button" class="chip ${d.period.days === n ? 'on' : 'ghost'}" data-act="report-days" data-days="${n}" aria-pressed="${d.period.days === n}">${l}</button>`).join('')}</div>
    <button type="button" class="chip ghost" data-act="report-pdf">下載 PDF</button></div>`,
  body: d => {
    const dp = d.deploys, rv = d.reviews, hl = d.heals, inc = d.incidents, live = d.live || {};
    const kpis = [
      ['部署成功', rpCount(dp.ok), dp.failed ? `另有 ${dp.failed} 次失敗` : `共 ${dp.apps} 個服務`, 'var(--signal)'],
      ['審查擋下', rpCount(rv.block), rv.total ? `共審查 ${rv.total} 次` : '還沒有審查紀錄', 'var(--caution)'],
      ['自動修復', rpCount(hl.total), hl.total ? `修好 ${hl.ok} 次` + (hl.needs_you ? `，${hl.needs_you} 次要你處理` : '') : '期間內沒有狀況', 'var(--pass)'],
      ['連續無狀況', inc.days_without_incident == null ? '<b>未知</b>' : rpCount(inc.days_without_incident), inc.last_incident ? `上次是 ${inc.last_incident}` : '天', 'var(--pass)'],
      ['目前服務', live.services == null ? '<b>未知</b>' : rpCount(live.services), live.monthly_usd != null ? `估計每月 $${live.monthly_usd}` : (live.unhealthy ? `${live.unhealthy} 個 Pod 不正常` : ''), 'var(--signal)'],
    ];
    const cats = Object.entries(rv.block_by_category || {});
    const ai = d.ai;
    let aiHTML;
    if(!d.has_data) aiHTML = '<p class="rp-none">期間內沒有紀錄，所以沒有產生 AI 解讀。</p>';
    else if(!ai || ai.unavailable) aiHTML = '<p class="rp-none">AI 解讀這次沒有產生（AI 服務沒有回應）。上面的統計和系統觀察不受影響。</p>';
    else aiHTML = `<div class="rp-ai">${ai.sentences.length ? ai.sentences.map(x => `<p>${esc(x)}</p>`).join('') : '<p>AI 寫的句子都提到統計裡沒有的數字，全部刪掉了。</p>'}
        ${ai.dropped ? `<p class="note">有 ${ai.dropped} 句提到統計裡查不到的數字，已經刪掉，避免讓你看到沒有根據的說法。</p>` : ''}</div>`;
    return `<div class="rp">
      <div class="print-title">ZeroTouch 分析報告</div>
      <div class="rp-kpis">${kpis.map((k, i) => `<div class="rp-kpi" style="--i:${i};--kc:${k[3]}"><span>${k[0]}</span>${k[1]}<small>${esc(k[2])}</small></div>`).join('')}</div>
      <section class="rp-sec"><h3>每日活動<span class="rp-tag fact">統計</span></h3>${rpDaily(d.series)}</section>
      <section class="rp-sec"><h3>部署前把關<span class="rp-tag fact">統計</span></h3>
        <div class="rp-cols"><div><h4>擋下的原因</h4>${rpHBars(cats, 'var(--caution)', '期間內沒有被擋下的部署。')}
          ${rv.blocked_cost_usd ? `<p class="rp-foot" style="margin-top:8px">因為成本或叢集容量被擋下的部署，如果上線估計每月要 $${esc(rv.blocked_cost_usd)}。</p>` : ''}</div>
          <div><h4>最常出現的審查提醒</h4>${rpHBars(rv.top_warnings, 'var(--signal)', '期間內沒有審查提醒。')}</div></div></section>
      <section class="rp-sec"><h3>自動修復<span class="rp-tag fact">統計</span></h3>
        <div class="rp-cols"><div><h4>原因</h4>${rpHBars(hl.top_causes, 'var(--pass)', '期間內 Healer 沒有處理任何狀況。')}</div>
          <div><h4>出狀況的服務</h4>${rpHBars(hl.top_apps, 'var(--stop)', '沒有。')}</div></div></section>
      <section class="rp-sec"><h3>系統觀察<span class="rp-tag rule">依統計規則產生</span></h3>
        ${(d.observations || []).length ? `<ul class="rp-obs">${d.observations.map((o, i) => `<li class="${esc(o.level)}" style="--i:${i}">${esc(o.text)}</li>`).join('')}</ul>` : '<p class="rp-none">沒有特別需要注意的地方。</p>'}</section>
      <section class="rp-sec"><h3>AI 解讀<span class="rp-tag ai">AI 根據上面的統計撰寫，數字已核對</span></h3>${aiHTML}</section>
      <section class="rp-sec"><h3>最近的紀錄<span class="rp-tag fact">統計</span></h3>
        ${(d.recent || []).length ? `<ol class="rp-tl">${d.recent.slice(0, 12).map(r => `<li><time>${rpTime(r.ts)}</time><span class="k ${r.outcome === 'fail' || r.outcome === 'block' ? 'fail' : ''}">${esc(RP_KIND[r.kind] || r.kind)}</span><span>${r.app ? `<span class="app">${esc(r.app)}</span>` : ''}${esc(r.detail || '')}</span></li>`).join('')}</ol>` : '<p class="rp-none">期間內沒有紀錄。</p>'}</section>
      <p class="rp-foot">資料來源：平台自己的操作紀錄${d.period.records_since ? `（從 ${esc(d.period.records_since)} 開始累積）` : ''}，只包含你自己帳號的服務。費用依公有雲牌價估算，放在你自己的電腦或學校叢集不另外收費。</p>
      <p class="rp-pdf-hint" role="status">列印視窗打開後，「目的地」選「另存為 PDF」再按儲存。</p>
    </div>`;
  },
};

// ── 分時擴縮與休眠（2026-10-11）──
function asWhen(iso){
  const d = new Date(iso || '');
  if(isNaN(d)) return '';
  return `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}
function asMsg(st){ return st.msg ? `<div class="as-msg ${st.err ? 't-stop' : ''}" role="status">${esc(st.msg)}</div>` : ''; }
function asUsd(v){ return esc((Number(v) || 0).toFixed(2)); }
W.autoscale = {
  summary: d => d.ok === false ? '分時與休眠：' + (d.error || '')
    : `分時與休眠：${(d.items || []).filter(x => x.sleeping).length} 個服務休眠中，${(d.items || []).filter(x => x.schedule).length} 個有排程`,
  head: d => `<h2>分時與休眠</h2><span class="meta">${d.ok === false ? '' : `<span>連續 ${esc(d.idle_days)} 天沒人用就休眠</span>`}<span>${esc(d.at)} 的資料</span></span>
    <div class="filters">${d.ok === false || d.remote ? '' : `<label class="as-idle">閒置 <select class="as-idle-sel" aria-label="閒置幾天就休眠">${[1,2,3,4,5,6,7].map(n => `<option value="${n}" ${n === Number(d.idle_days) ? 'selected' : ''}>${n}</option>`).join('')}</select> 天就休眠</label>
      <button type="button" class="chip ghost" data-act="as-idle-save">儲存天數</button>`}<button type="button" class="chip ghost" data-act="refresh">重新整理</button></div>`,
  body: (d, st) => {
    if(d.ok === false) return emptyBlock('這個功能現在不能用。', d.error || '');
    const items = d.items || [];
    if(!items.length) return asMsg(st) + emptyBlock('目前沒有任何服務。', '部署服務之後，可以在這裡設定分時擴縮和休眠。', exampleBtn('部署 2 個 nginx:1.27，開 80 port', '部署一個試試看'));
    const rows = pageSlice(items, st).map((x, i) => {
      const state = x.waking ? stateTag({cls:'pend', label:'喚醒中'})
        : x.sleeping ? stateTag({cls:'sleep', label:`休眠中，醒來開 ${x.sleep_replicas} 個`})
        : repsHTML(x.ready || 0, x.replicas || 0);
      const sched = x.schedule
        ? `<div>${esc(x.schedule.rows.map(r => r.when + ' ' + r.replicas + ' 個').join('；'))}</div>` +
          (x.schedule.next ? `<div class="dim">下次 ${esc(x.schedule.next.at)} 變成 ${esc(x.schedule.next.replicas)} 個</div>` : '')
        : '<span class="dim">沒有</span>';
      const note = x.sleeping ? `<div class="dim as-note">${esc(asWhen(x.sleeping_since))} 開始休眠${x.saving_usd ? `，每月約省 $${asUsd(x.saving_usd)}` : ''}</div>`
        : x.exempt ? '<div class="dim as-note">在例外清單，不會休眠</div>'
        : x.idle_note ? `<div class="dim as-note">${esc(x.idle_note)}</div>` : '';
      const btns = (x.sleeping && !x.waking ? `<button type="button" class="btn btn-sm primary" data-act="as-wake" data-name="${esc(x.name)}">喚醒</button>` : '') +
        (d.remote ? '' : `<button type="button" class="btn btn-sm" data-act="as-exempt" data-name="${esc(x.name)}" data-v="${x.exempt ? 0 : 1}">${x.exempt ? '允許休眠' : '不要休眠'}</button>`) +
        (x.schedule ? `<button type="button" class="btn btn-sm" data-act="as-cancel" data-name="${esc(x.name)}">取消排程</button>` : '') +
        (d.remote ? '' : `<button type="button" class="btn btn-sm" data-act="as-suggest" data-name="${esc(x.name)}">建議時段</button>`) +
        `<button type="button" class="btn btn-sm primary" data-act="as-edit" data-name="${esc(x.name)}">${x.schedule ? '修改排程' : '設定排程'}</button>`;
      return `<tr class="enter" style="animation-delay:${i * 25}ms"><td class="id">${esc(x.name)}${note}</td><td>${state}</td>
        <td class="as-sched">${sched}</td><td class="r"><div class="as-btns">${btns}</div></td></tr>`;
    }).join('');
    const notes = [];
    if(d.remote) notes.push(d.remote_note || '');
    if(d.prometheus_ok === false && !d.remote) notes.push('現在連不上 Prometheus，看不到流量，所以暫時不會讓任何服務休眠。');
    if(d.test_override_seconds) notes.push(`測試模式：閒置 ${d.test_override_seconds} 秒就會休眠。`);
    if(!d.remote) notes.push('休眠是看服務實際收到的網路流量，健康檢查本身的流量不算。休眠中有人連進來會自動喚醒，通常 30 秒到 1 分鐘恢復。');
    return asMsg(st) + `<div class="table-wrap"><table><thead><tr><th scope="col">服務</th><th scope="col">狀態</th><th scope="col">分時擴縮</th><th scope="col" class="r">操作</th></tr></thead><tbody>${rows}</tbody></table></div>
      ${pagerHTML(items.length, st.p || 1, W_PER)}
      <div class="w-note">${notes.map(esc).join('<br>')}</div>
      <div class="w-note">可以直接說：「shop-web 白天 3 個、晚上 1 個」「shop-web 不要休眠」「閒置 3 天就休眠」「喚醒 shop-web」。</div>`;
  },
};
W.asplan = {
  summary: d => `分時擴縮設定：${d.plan.app}，${d.rows.map(r => r.when + ' ' + r.replicas + ' 個').join('；')}` + (d.done ? '（已套用）' : ''),
  head: d => `<h2>分時擴縮</h2><span class="meta"><span class="id">${esc(d.plan.app)}</span>${d.done ? '<b class="state s-run"><i></i>已套用</b>' : ''}</span>`,
  body: (d, st) => {
    const sv = d.savings || {};
    const pct = Math.max(0, Math.min(100, Number(sv.saved_pct) || 0));
    const table = `<div class="table-wrap"><table><thead><tr><th scope="col">時段</th><th scope="col" class="r">副本數</th></tr></thead><tbody>
      ${d.rows.map((r, i) => `<tr class="enter" style="animation-delay:${i * 40}ms"><td>${esc(r.when)}</td><td class="r num">${esc(r.replicas)} 個</td></tr>`).join('')}</tbody></table></div>`;
    const save = `<div class="as-save"><div class="as-save-top"><span>跟整天開 ${esc(sv.peak)} 個相比</span><b>每月約省 $${asUsd(sv.saved_usd)}（${esc(sv.saved_pct)}%）</b></div>
      <div class="as-bar" aria-hidden="true"><div class="as-bar-fill" style="width:${100 - pct}%"></div></div>
      <div class="dim">排程後每月約 $${asUsd(sv.scheduled_usd)}，整天開滿約 $${asUsd(sv.always_peak_usd)}。依公有雲牌價估算，放在自己電腦或學校叢集不另外收費。</div></div>`;
    const nowLine = d.sleeping ? '這個服務現在在休眠，套用後醒來時會照時段開。'
      : `現在 ${esc(d.now)}，目前開 ${esc(d.current)} 個，套用後會${d.now_target === d.current ? '維持' : '調成'} ${esc(d.now_target)} 個` +
        (d.next ? `；下一次是 ${esc(d.next.at)} 變成 ${esc(d.next.replicas)} 個。` : '。');
    const notes = `<div class="w-note">${nowLine}${d.replaces ? '<br>這個服務原本已經有排程，套用後會取代舊的。' : ''}<br>只在時段切換的那一刻調整；中間你手動改的副本數會保留到下一個時段。</div>`;
    const foot = (d.done || d.closed) ? '' : `<div class="as-foot"><button type="button" class="btn" data-act="as-dismiss">不要套用</button><button type="button" class="btn primary" data-act="as-apply">套用</button></div>`;
    return table + save + notes + asMsg(st) + foot;
  },
};
// 2026-10-21：「我的服務」頁的「設定排程」：輸入時段 → 預覽（跟對話裡同一張確認卡）→ 套用
function openScheduleEditor(name){
  const ex = ['白天 3 個、晚上 1 個', '平日 2 個、週末 1 個', '9 點到 18 點 3 個，其他時間 1 個'];
  openProjModal(`<h3>設定 ${esc(name)} 的分時擴縮</h3>
    <label class="lbl" for="as-ed-text">什麼時段開幾個？</label>
    <input type="text" id="as-ed-text" placeholder="例如：白天 3 個、晚上 1 個" onkeydown="if(event.key==='Enter')previewSchedule('${escAttr(name)}')">
    <div class="as-ed-ex">${ex.map(x => `<button type="button" class="chip ghost" onclick="document.getElementById('as-ed-text').value='${escAttr(x)}'">${esc(x)}</button>`).join('')}</div>
    <p class="field-hint">白天是 08:00 到 20:00；平日是週一到週五。只在時段切換的那一刻調整，最少保留 1 個。</p>
    <div class="proj-err" id="as-ed-err" hidden></div>
    <div id="as-ed-plan"></div>
    <div class="confirm-dialog-actions"><button type="button" class="btn" onclick="closeProjModal()">關閉</button>
      <button type="button" class="btn primary" onclick="previewSchedule('${escAttr(name)}')">預覽</button></div>`);
}
// 2026-10-23：從流量建議時段，跟設定排程同一張確認卡
async function openScheduleSuggest(name){
  openProjModal(`<h3>${esc(name)} 的建議時段</h3><div id="as-ed-plan"><p class="proj-muted">正在整理過去的流量紀錄…</p></div>
    <div class="confirm-dialog-actions"><button type="button" class="btn" onclick="closeProjModal()">關閉</button></div>`);
  let d;
  try { d = await (await fetch('/api/autoscale/suggest?name=' + encodeURIComponent(name))).json(); }
  catch(e){ d = {ok:false, error:'讀取失敗：' + (e.message || e)}; }
  const host = document.getElementById('as-ed-plan');
  if(!host) return;
  host.innerHTML = d.ok ? `<p>${esc(d.explain || '')}</p>` + widgetHTML('asplan', d, {})
                        : `<div class="proj-warn">${esc(d.error || '沒辦法建議時段。')}</div>`;
}
async function previewSchedule(name){
  const text = (document.getElementById('as-ed-text').value || '').trim();
  const err = document.getElementById('as-ed-err'), host = document.getElementById('as-ed-plan');
  err.hidden = true; host.innerHTML = '';
  if(!text){ err.textContent = '請先輸入時段，或點一個範例。'; err.hidden = false; return; }
  const d = await postJSON('/api/autoscale/parse', {text: name + ' ' + text});
  if(!d.ok){ err.textContent = d.error || '看不懂這個時段設定。'; err.hidden = false; return; }
  host.innerHTML = widgetHTML('asplan', d, {});
}
async function postJSON(url, body){
  const r = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body || {})});
  if(r.status === 401) throw new Error('登入已經過期，請重新整理頁面後再登入。');
  let d = {};
  try { d = await r.json(); } catch(e){ d = {}; }
  if(!r.ok && !d.error) d.error = '伺服器回應 HTTP ' + r.status + '，請稍後再試。';
  if(r.status === 429) d.error = '操作太頻繁了，請等一下再試。';
  return d;
}
async function runAutoscaleAction(action, args, text){
  try{
    if(action === 'as_schedule'){
      const d = await postJSON('/api/autoscale/parse', {text});
      removeTyping();
      if(!d.ok){ appendMsg('assistant', d.error || '看不懂這個時段設定。', null, 'text'); return; }
      appendWidget('asplan', d, {}, '我把你說的時段整理成下面這張表，確認沒問題再按「套用」。');
      return;
    }
    if(action === 'as_suggest'){
      const r = await fetch('/api/autoscale/suggest?name=' + encodeURIComponent(args.name || ''));
      const d = await r.json().catch(() => ({}));
      removeTyping();
      if(!d.ok){ appendMsg('assistant', d.error || '沒辦法建議時段。', null, 'text'); return; }
      appendWidget('asplan', d, {}, d.explain || '');
      return;
    }
    if(action === 'as_show'){
      const data = await W_LOAD.autoscale();
      removeTyping();
      appendWidget('autoscale', data, {}, '');
      return;
    }
    const call = {
      as_wake: () => postJSON('/api/autoscale/wake', {name:args.name}),
      as_exempt_on: () => postJSON('/api/autoscale/exempt', {name:args.name, exempt:true}),
      as_exempt_off: () => postJSON('/api/autoscale/exempt', {name:args.name, exempt:false}),
      as_cancel: () => postJSON('/api/autoscale/cancel', {name:args.name}),
      as_idle_days: () => postJSON('/api/autoscale/settings', {idle_days:args.days}),
    }[action];
    const d = call ? await call() : {error:'這個指令目前還不支援。'};
    removeTyping();
    appendMsg('assistant', d.message || d.error || '沒有完成，請再試一次。', null, 'text');
  }catch(e){
    removeTyping();
    appendMsg('assistant', '處理的時候失敗了：' + (e.message || e), null, 'text');
  }
}

// 重新整理：重新抓資料，保留目前的篩選與頁碼
const W_LOAD = {
  autoscale: async () => Object.assign(await getJSON('/api/autoscale'), {at:nowHM()}),
  report: async (old) => Object.assign(await getJSON('/api/report?' + (old && old.period ? 'days=' + old.period.days : 'q=' + encodeURIComponent((old && old.q) || ''))), {at:nowHM()}),
  pods: async () => ({items:((await getJSON('/api/pods')).pods || []).map(slimPod), at:nowHM()}),
  deployments: async () => ({items:(await getJSON('/api/deployments')).deployments || [], at:nowHM()}),
  gitops: async () => ({items:(await getJSON('/api/gitops')).commits || [], at:nowHM()}),
  healer: async () => {
    // 同時看 Pod 狀態：Healer 只把達到自動修復門檻的列出來（例如重啟 3 次以上），
    // 剛開始出狀況的 Pod 不在清單裡，不能因此跟使用者說「全部正常」
    const [scan, pods] = await Promise.all([getJSON('/api/healer/scan'), getJSON('/api/pods').catch(() => ({pods:[]}))]);
    const items = scan.issues || [];
    const listed = new Set(items.map(i => i.pod_name || i.pod));
    const watch = (pods.pods || []).map(slimPod).filter(p => podState(p).attn && !listed.has(p.name)).map(p => ({name:p.name, label:podState(p).label}));
    return {items, watch, at:nowHM()};
  },
  resources: async () => Object.assign(await getJSON('/api/dashboard'), {at:nowHM()}),
  poddetail: async (old) => { const d = await getJSON('/api/pods/' + encodeURIComponent(old.name)); return {name:old.name, pods:(d.pods || []).slice(0, 3), at:nowHM()}; },
  depdetail: async (old) => { const d = await getJSON('/api/deployments/' + encodeURIComponent(old.dep.name)); if(!d.found) throw new Error(d.message || '找不到這個服務，可能已經被刪除。'); return {dep:d.deployment, at:nowHM()}; },
};

function widgetHTML(kind, data, state, wid){
  const def = W[kind];
  wid = wid || zUid('w');
  const st = state || {};
  return `<section class="module zt-w" data-w="${kind}" data-wid="${wid}" data-w-data="${encState(data)}" data-w-state="${encState(st)}" data-summary="${esc(def.summary(data))}">
    <div class="module-head">${def.head(data, st)}</div><div class="zt-w-body">${def.body(data, st)}</div></section>`;
}
function persistWidget(wid, html){
  const ch = currentChat(); if(!ch) return;
  const msg = ch.messages.find(m => m.role === 'assistant' && String(m.content || '').includes('data-wid="' + wid + '"'));
  if(!msg) return;
  const c = String(msg.content);
  const at = c.indexOf('<section class="module zt-w"');
  msg.content = (at > 0 ? c.slice(0, at) : '') + html;
  saveChats();
}
function rerenderWidget(el, patch, newData){
  const kind = el.dataset.w;
  const data = newData || decState(el.dataset.wData);
  const st = Object.assign(decState(el.dataset.wState) || {}, patch || {});
  const html = widgetHTML(kind, data, st, el.dataset.wid);
  persistWidget(el.dataset.wid, html);
  const tmp = document.createElement('div'); tmp.innerHTML = html;
  const neu = tmp.firstElementChild;
  el.replaceWith(neu);
  return neu;
}
async function refreshWidget(el){
  const kind = el.dataset.w;
  const btn = el.querySelector('[data-act="refresh"]');
  if(btn){ btn.disabled = true; btn.textContent = '讀取中'; }
  try{
    const data = await W_LOAD[kind](decState(el.dataset.wData) || {});
    rerenderWidget(el, {}, data);
  }catch(e){
    if(btn){ btn.disabled = false; btn.textContent = '重新整理失敗，再試一次'; }
  }
}
// 報告存成 PDF：複製到專用的列印容器，切淺色主題後打開瀏覽器的列印視窗，印完還原
function printReport(w){
  const hint = w.querySelector('.rp-pdf-hint');
  if(hint) hint.classList.add('on');
  let host = document.getElementById('print-host');
  if(!host){ host = document.createElement('div'); host.id = 'print-host'; document.body.appendChild(host); }
  host.innerHTML = '';
  const clone = w.cloneNode(true);
  clone.querySelectorAll('.count-up').forEach(el => { el.textContent = el.dataset.dec > 0 ? Number(el.dataset.to).toFixed(el.dataset.dec) : el.dataset.to; });
  host.appendChild(clone);
  const root = document.documentElement;
  const prevTheme = root.getAttribute('data-theme');
  root.setAttribute('data-theme', 'light');
  document.body.classList.add('print-report');
  const prevTitle = document.title;
  const data = decState(w.dataset.wData) || {};
  document.title = 'ZeroTouch 分析報告 ' + ((data.period && data.period.start) || '') + ' 到 ' + ((data.period && data.period.end) || '');
  const restore = () => {
    document.body.classList.remove('print-report');
    if(prevTheme) root.setAttribute('data-theme', prevTheme); else root.removeAttribute('data-theme');
    document.title = prevTitle;
    host.innerHTML = '';
    window.removeEventListener('afterprint', restore);
  };
  window.addEventListener('afterprint', restore);
  setTimeout(() => { window.print(); setTimeout(() => { if(document.body.classList.contains('print-report')) restore(); }, 1500); }, 60);
}
// 數字從 0 跳到目標值；新插入的元素自動處理，不用每個地方各自呼叫
function runCountUps(scope){
  (scope || document).querySelectorAll('.count-up:not([data-done])').forEach(el => {
    el.setAttribute('data-done', '1');
    const to = Number(el.dataset.to) || 0, dec = Number(el.dataset.dec) || 0;
    if(!to || window.matchMedia('(prefers-reduced-motion: reduce)').matches){ el.textContent = to.toFixed(dec); return; }
    const t0 = performance.now(), dur = 900;
    const tick = now => {
      const k = Math.min(1, (now - t0) / dur), e = 1 - Math.pow(1 - k, 3);
      el.textContent = (to * e).toFixed(dec);
      if(k < 1) requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  });
}
new MutationObserver(() => runCountUps()).observe(document.documentElement, {childList:true, subtree:true});

function appendWidget(kind, data, state, lead){
  appendMsg('assistant', (lead ? `<div class="w-lead">${esc(lead)}</div>` : '') + widgetHTML(kind, data, state));
}

// ── Chat 的系統查詢：全部改成互動模組 ──
async function runReadAction(action){
  const args = window.__lastIntentArgs || {};
  try{
    if(action === 'list_pods'){
      const data = await W_LOAD.pods();
      const a = data.items.filter(p => podState(p).attn).length;
      removeTyping();
      appendWidget('pods', data, {f:'all', p:1}, data.items.length
        ? `你目前有 ${data.items.length} 個 Pod${a ? `，其中 ${a} 個需要注意，已經排在最前面` : '，全部正常'}。點任一列可以看詳情。`
        : '');
    } else if(action === 'list_deployments'){
      const data = await W_LOAD.deployments();
      removeTyping();
      appendWidget('deployments', data, {p:1}, data.items.length ? `你目前有 ${data.items.length} 個服務。點任一列可以擴縮、更新映像檔、回滾或刪除。` : '');
    } else if(action === 'report'){
      const data = await W_LOAD.report({q:args.q || ''});
      removeTyping();
      appendWidget('report', data, {}, data.has_data
        ? `這是 ${data.period.start} 到 ${data.period.end} 的分析報告。數字都是從平台紀錄算出來的，AI 解讀另外標示。`
        : '這段期間還沒有紀錄。平台會從現在開始累積，部署、審查、修復之後就會出現在報告裡。');
    } else if(action === 'gitops_log'){
      const data = await W_LOAD.gitops();
      removeTyping();
      appendWidget('gitops', data, {p:1}, data.items.length ? '這是最近的部署紀錄，最新的在最上面。' : '');
    } else if(action === 'healer_scan'){
      const data = await W_LOAD.healer();
      removeTyping();
      appendWidget('healer', data, {p:1}, data.items.length ? `Healer 找到 ${data.items.length} 個有狀況的 Pod。` : '');
    } else if(action === 'cluster_metrics'){
      const data = await W_LOAD.resources();
      removeTyping();
      appendWidget('resources', data, {}, '');
    } else if(action === 'describe_pod' || action === 'pod_health'){
      const name = args.name || '';
      if(!name){ removeTyping(); appendMsg('assistant', '要查哪一個 Pod？請告訴我名稱，例如「web-frontend 的狀態」。', null, 'text'); return; }
      const d = await getJSON('/api/pods/' + encodeURIComponent(name) + (action === 'pod_health' ? '?diagnose=1' : ''));
      removeTyping();
      if(!d.found || !(d.pods || []).length){
        if(d.deployment){ appendWidget('depdetail', {dep:d.deployment, at:nowHM()}, {}, (d.message || '') + ' 下面是這個服務的狀態。'); return; }
        appendMsg('assistant', d.message || ('找不到名稱符合「' + name + '」的 Pod。可以先說「列出我所有的 Pod」看看有哪些。'), null, 'text');
        return;
      }
      const pods = d.pods.slice(0, 3);
      const bad = pods.filter(p => !p.healthy);
      const lead = action === 'pod_health'
        ? (bad.length ? `「${name}」有 ${bad.length} 個 Pod 有狀況。` : `「${name}」目前正常。`)
        : `「${name}」的詳細狀態：`;
      appendWidget('poddetail', {name, pods, at:nowHM()}, {}, lead);
    } else if(action === 'describe_deployment'){
      const name = args.name || '';
      if(!name){ removeTyping(); appendMsg('assistant', '要查哪一個服務？請告訴我名稱。', null, 'text'); return; }
      const d = await getJSON('/api/deployments/' + encodeURIComponent(name));
      removeTyping();
      if(!d.found){ appendMsg('assistant', d.message || ('找不到名稱是「' + name + '」的服務。'), null, 'text'); return; }
      appendWidget('depdetail', {dep:d.deployment, at:nowHM()}, {}, '');
    } else {
      removeTyping();
      appendMsg('assistant', '這個查詢目前還不支援。', null, 'text');
    }
  }catch(e){
    removeTyping();
    appendMsg('assistant', '查詢失敗：' + (e.message || e), null, 'text');
  }
}

// ── 詳情內容（抽屜與對話內共用） ──
function parseMi(v){
  if(v == null) return null;
  const m = String(v).match(/^([\d.]+)\s*(Ki|Mi|Gi|K|M|G)?$/);
  if(!m) return null;
  const n = parseFloat(m[1]), u = m[2] || '';
  return u === 'Gi' || u === 'G' ? n * 1024 : (u === 'Ki' || u === 'K') ? n / 1024 : (u ? n : n / 1048576);
}
function parseCores(v){
  if(v == null) return null;
  const s = String(v);
  if(s.endsWith('m')) return parseFloat(s) / 1000;
  const n = parseFloat(s); return isNaN(n) ? null : n;
}
function barG(label, valText, pct){
  const p = pct == null ? null : Math.max(0, Math.min(100, pct));
  return `<div class="bar-g"><div class="row"><span>${label}</span><span class="num">${valText}</span></div>${p == null ? '' : `<div class="bar"><span data-w="${p.toFixed(0)}"></span></div>`}</div>`;
}
function ctrStateText(c){
  if(c.state === 'running') return c.ready ? stateTag({cls:'run', label:'執行中'}) : stateTag({cls:'pend', label:'執行中，尚未就緒'});
  if(c.state === 'waiting') return stateTag({cls:(c.reason === 'ContainerCreating' || c.reason === 'PodInitializing') ? 'pend' : 'fail', label:'等待中' + (c.reason ? '：' + c.reason : '')});
  if(c.state === 'terminated') return stateTag({cls:'fail', label:'已終止' + (c.reason ? '：' + c.reason : '')});
  return stateTag({cls:'pend', label:'狀態不明'});
}
function diagHTML(dg){
  if(!dg) return '';
  const rc = dg.root_cause || dg.summary || '';
  const fix = dg.suggestion || dg.action || '';
  if(!rc && !fix) return '';
  return `<div class="diag"><div><b>Healer 診斷</b></div>${rc ? `<div>${esc(rc)}</div>` : ''}${fix ? `<div class="fix">建議：${esc(fix)}</div>` : ''}</div>`;
}
function podDetailHTML(pd, opts){
  opts = opts || {};
  const ctrs = pd.containers || [];
  const lim = (ctrs[0] && ctrs[0].limits) || {};
  const u = pd.real_usage || {};
  let usage = '';
  if(u.available){
    const memLim = parseMi(lim.memory), cpuLim = parseCores(lim.cpu);
    usage = barG('CPU', (u.cpu_cores != null ? Number(u.cpu_cores).toFixed(3) + ' 核' : '目前讀不到') + (cpuLim ? ' / ' + cpuLim + ' 核' : ''), cpuLim && u.cpu_cores != null ? u.cpu_cores / cpuLim * 100 : null)
          + barG('記憶體', (u.memory_mi != null ? Math.round(u.memory_mi) + ' Mi' : '目前讀不到') + (memLim ? ' / ' + Math.round(memLim) + ' Mi' : ''), memLim && u.memory_mi != null ? u.memory_mi / memLim * 100 : null);
  } else if(pd.real_usage){
    usage = '<div class="dim">即時用量暫時讀不到（監控元件沒有回應）。</div>';
  }
  const evs = (pd.events || []).map(e => `<li class="${e.type === 'Warning' ? 'bad' : ''}"><b>${esc(e.reason || '')}</b>${esc(e.message || '')}${e.count > 1 ? `<span class="cnt">共 ${e.count} 次</span>` : ''}</li>`).join('');
  const actions = [];
  if(!pd.healthy && !pd.diagnosis) actions.push(`<button type="button" class="btn" data-act="diagnose" data-name="${esc(pd.name)}">請 Healer 診斷</button>`);
  if(!pd.healthy) actions.push(flowBtn('healer_fix', {pod_name:pd.name}, '讓 Healer 修復', 'primary'));
  if(opts.inline) actions.push(`<button type="button" class="btn" data-act="open-pod" data-name="${esc(pd.name)}">在側邊看完整詳情</button>`);
  return `<div class="pd">
    <div class="pd-top">${healthTag(pd)}<span class="dim">${esc(humanSummary(pd.health_summary))}</span>${opts.inline ? `<span class="id dim">${esc(pd.name)}</span>` : ''}</div>
    ${diagHTML(pd.diagnosis)}
    <dl class="kv">
      <div><dt>所屬服務</dt><dd class="id">${esc(pd.app || '無')}</dd></div>
      <div><dt>重啟次數</dt><dd class="num">${pd.restarts ?? 0}</dd></div>
      <div><dt>節點</dt><dd class="id">${esc(pd.node || '尚未排程')}</dd></div>
      <div><dt>建立時間</dt><dd>${esc(pd.age || '')}</dd></div>
    </dl>
    ${usage ? `<div><div class="sec-t">即時用量</div><div class="ctrs">${usage}</div></div>` : ''}
    <div><div class="sec-t">容器</div><div class="ctrs">${ctrs.map(c => `<div class="ctr"><span class="id">${esc(c.image || c.name)}</span><span>${ctrStateText(c)}</span>
      ${c.message ? `<span class="msgline">${esc(String(c.message).slice(0, 200))}</span>` : ''}${c.note ? `<span class="msgline">${esc(c.note)}</span>` : ''}</div>`).join('') || '<div class="dim">沒有容器資訊。</div>'}</div></div>
    ${pd.logs ? `<div><div class="sec-t">最近的 log</div><pre class="logbox">${esc(String(pd.logs).trim().split('\n').slice(-30).join('\n'))}</pre></div>` : ''}
    ${evs ? `<div><div class="sec-t">最近事件</div><ol class="events">${evs}</ol></div>` : ''}
    ${actions.length ? `<div class="actions">${actions.join('')}</div>` : ''}
  </div>`;
}
function depDetailHTML(dep, pods, opts){
  opts = opts || {};
  const conds = (dep.conditions || []).filter(c => c.status !== 'True' && c.message).map(c => `<li class="bad"><b>${esc(c.type)}</b>${esc(c.message)}</li>`).join('');
  const podList = pods && pods.length ? `<div><div class="sec-t">這個服務的 Pod</div><div class="mini-list">${pods.map(p => `<button type="button" data-act="open-pod" data-name="${esc(p.name)}"><span class="id">${esc(p.name)}</span>${healthTag(p)}</button>`).join('')}</div></div>` : '';
  const n = esc(dep.name);
  const manage = opts.inline
    ? `<div class="actions"><button type="button" class="btn primary" data-act="open-dep" data-name="${n}">管理這個服務</button></div>`
    : `<div><div class="sec-t">擴縮副本</div><div class="inline-form"><input type="number" id="dr-replicas" min="0" max="50" value="${dep.replicas}" aria-label="副本數"><button type="button" class="btn" data-act="do-scale" data-name="${n}">套用</button></div></div>
       <div><div class="sec-t">更新映像檔</div><div class="inline-form"><input type="text" id="dr-image" value="${esc(dep.image || '')}" aria-label="新的映像檔"><button type="button" class="btn" data-act="do-image" data-name="${n}">更新</button></div></div>
       <div class="form-err" id="dr-err" role="alert"></div>
       <div class="actions">${flowBtn('rollback', {name:dep.name}, '回滾到上一版')}${flowBtn('delete', {name:dep.name}, '刪除這個服務', 'danger')}</div>
       <div class="dim">每個操作都會先在對話裡請你確認，不會直接執行。</div>`;
  return `<div class="pd">
    <div class="pd-top">${dep.healthy ? stateTag({cls:'run', label:'健康'}) : stateTag({cls:'warn', label:'還沒全部就緒'})}<span class="dim">${esc(humanSummary(dep.health_summary))}</span>${opts.inline ? `<span class="id dim">${n}</span>` : ''}</div>
    <dl class="kv">
      <div><dt>映像檔</dt><dd class="id">${esc(dep.image || '')}</dd></div>
      <div><dt>就緒的副本</dt><dd>${repsHTML(dep.ready || 0, dep.replicas || 0)}</dd></div>
      <div><dt>建立時間</dt><dd>${esc(dep.age || '')}</dd></div>
      <div><dt>無法使用的副本</dt><dd class="num ${dep.unavailable ? 't-stop' : ''}">${dep.unavailable ?? 0}</dd></div>
    </dl>
    ${conds ? `<div><div class="sec-t">需要注意</div><ol class="events">${conds}</ol></div>` : ''}
    ${podList}
    ${manage}
  </div>`;
}

// ── 側邊抽屜 ──
let _drawerLastFocus = null, _drawerSeq = 0;
function openDrawerShell(kindLabel, title){
  const dr = document.getElementById('zt-drawer');
  _drawerLastFocus = document.activeElement;
  document.getElementById('zt-drawer-kind').textContent = kindLabel;
  document.getElementById('zt-drawer-title').textContent = title;
  document.getElementById('zt-drawer-body').innerHTML = '<div class="skeleton" aria-label="讀取中"><i></i><i></i><i></i><i></i></div>';
  dr.classList.add('open'); dr.setAttribute('aria-hidden', 'false');
  document.getElementById('zt-scrim').classList.add('open');
  setTimeout(() => dr.querySelector('header button')?.focus(), 50);
  return ++_drawerSeq;
}
function fillDrawer(seq, html){
  if(seq !== _drawerSeq) return;
  const body = document.getElementById('zt-drawer-body');
  body.innerHTML = html;
  requestAnimationFrame(() => requestAnimationFrame(() => body.querySelectorAll('.bar span[data-w]').forEach(s => { s.style.width = s.dataset.w + '%'; })));
}
function closeDrawer(){
  const dr = document.getElementById('zt-drawer');
  if(!dr || !dr.classList.contains('open')) return;
  dr.classList.remove('open'); dr.setAttribute('aria-hidden', 'true');
  document.getElementById('zt-scrim').classList.remove('open');
  _drawerSeq++;
  if(_drawerLastFocus && document.contains(_drawerLastFocus)) _drawerLastFocus.focus();
}
async function openDepDrawer(name){
  const seq = openDrawerShell('服務詳情', name);
  try{
    const [d, pd] = await Promise.all([getJSON('/api/deployments/' + encodeURIComponent(name)), getJSON('/api/pods/' + encodeURIComponent(name)).catch(() => null)]);
    if(!d.found){ fillDrawer(seq, emptyBlock('找不到這個服務。', d.message || '它可能已經被刪除。')); return; }
    fillDrawer(seq, depDetailHTML(d.deployment, (pd && pd.pods) || []));
  }catch(e){ fillDrawer(seq, emptyBlock('讀取失敗。', e.message || String(e))); }
}
function goChatAndStart(kind, args){
  closeDrawer();
  if(!document.getElementById('page-chat')?.classList.contains('active')) showPage('chat');
  startDestructiveFlow(kind, args);
}

// ── 事件委派：模組、抽屜、流程卡上的所有按鈕 ──
document.addEventListener('click', function(e){
  const t = e.target.closest('[data-act]');
  if(!t || t.disabled) return;
  const act = t.dataset.act;
  const w = t.closest('.zt-w');
  if(act === 'page' && w){ rerenderWidget(w, {p:parseInt(t.dataset.p, 10) || 1}); return; }
  if(act === 'filter' && w){ rerenderWidget(w, {f:t.dataset.f, p:1}); return; }
  if(act === 'sort' && w){ const st = decState(w.dataset.wState) || {}; const k = t.dataset.k; rerenderWidget(w, {k, dir:st.k === k ? -(st.dir || 1) : 1, p:1}); return; }
  if(act === 'refresh' && w){ refreshWidget(w); return; }
  if(act === 'report-days' && w){
    const old = decState(w.dataset.wData) || {};
    w.querySelectorAll('[data-act="report-days"]').forEach(b => b.disabled = true);
    t.textContent = '產生中';
    W_LOAD.report({period:{days:parseInt(t.dataset.days, 10) || 30}}).then(data => rerenderWidget(w, {}, data))
      .catch(() => { t.textContent = '產生失敗，再試一次'; w.querySelectorAll('[data-act="report-days"]').forEach(b => b.disabled = false); });
    return;
  }
  if(act === 'report-pdf' && w){ printReport(w); return; }
  if(act === 'as-apply' && w){
    const data = decState(w.dataset.wData) || {};
    t.disabled = true; t.textContent = '套用中';
    postJSON('/api/autoscale/schedule', {plan:data.plan})
      .then(d => { if(d.ok && document.getElementById('svc-autoscale') && currentPageName() === 'services') loadServicesPage(true);
                   if(d.ok && currentPageName() === 'project' && projTab === 'schedule' && projData) loadProjSchedule(projData);
                   return d.ok ? rerenderWidget(w, {msg:d.message, err:false}, Object.assign({}, data, {done:true}))
                               : rerenderWidget(w, {msg:d.error || '沒有套用成功。', err:true}); })
      .catch(e => rerenderWidget(w, {msg:'沒有套用成功：' + (e.message || e), err:true}));
    return;
  }
  if(act === 'as-edit'){ openScheduleEditor(t.dataset.name); return; }
  if(act === 'as-suggest'){ openScheduleSuggest(t.dataset.name); return; }
  if(act === 'as-idle-save' && w){
    const sel = w.querySelector('.as-idle-sel');
    t.disabled = true;
    postJSON('/api/autoscale/settings', {idle_days: parseInt(sel && sel.value, 10)}).then(async d => {
      let data; try { data = await W_LOAD.autoscale(); } catch(e){ data = undefined; }
      rerenderWidget(w, {msg:d.message || d.error || '', err:!d.ok}, data);
    }).catch(e => rerenderWidget(w, {msg:'沒有儲存成功：' + (e.message || e), err:true}));
    return;
  }
  if(act === 'as-dismiss' && w){
    rerenderWidget(w, {msg:'沒有套用，設定維持原樣。', err:false}, Object.assign({}, decState(w.dataset.wData) || {}, {closed:true}));
    return;
  }
  if((act === 'as-wake' || act === 'as-exempt' || act === 'as-cancel') && w){
    t.disabled = true;
    const name = t.dataset.name;
    const req = act === 'as-wake' ? postJSON('/api/autoscale/wake', {name})
      : act === 'as-cancel' ? postJSON('/api/autoscale/cancel', {name})
      : postJSON('/api/autoscale/exempt', {name, exempt:t.dataset.v === '1'});
    req.then(async d => {
      let data;
      try { data = await W_LOAD.autoscale(); } catch(e){ data = undefined; }
      const neu = rerenderWidget(w, {msg:d.message || d.error || '', err:!d.ok}, data);
      // 喚醒要 30 秒到 1 分鐘，之後自動再抓一次狀態
      if(act === 'as-wake' && d.ok) setTimeout(() => { if(document.body.contains(neu)) refreshWidget(neu); }, 35000);
    }).catch(e => rerenderWidget(w, {msg:'沒有完成：' + (e.message || e), err:true}));
    return;
  }
  if(act === 'open-pod'){ openPodDrawer(t.dataset.name); return; }
  if(act === 'open-dep'){ openDepDrawer(t.dataset.name); return; }
  if(act === 'diagnose'){ openPodDrawer(t.dataset.name, true); return; }
  if(act === 'close-drawer'){ closeDrawer(); return; }
  if(act === 'flow'){ goChatAndStart(t.dataset.kind, decState(t.dataset.args) || {}); return; }
  if(act === 'example'){
    if(!document.getElementById('page-chat')?.classList.contains('active')) showPage('chat');
    const inp = document.getElementById('chat-input'); inp.value = t.dataset.text || ''; inp.focus(); return;
  }
  if(act === 'do-scale' || act === 'do-image'){
    const err = document.getElementById('dr-err');
    if(act === 'do-scale'){
      const v = parseInt(document.getElementById('dr-replicas').value, 10);
      if(!Number.isInteger(v) || v < 0 || v > 50){ err.textContent = '副本數要是 0 到 50 之間的整數。'; return; }
      goChatAndStart('scale', {name:t.dataset.name, replicas:v});
    } else {
      const v = (document.getElementById('dr-image').value || '').trim();
      if(!v || /\s/.test(v)){ err.textContent = '請輸入映像檔名稱，例如 nginx:1.27，中間不能有空白。'; return; }
      goChatAndStart('update_image', {name:t.dataset.name, image:v});
    }
    return;
  }
});
document.addEventListener('keydown', function(e){
  if(e.key === 'Escape') closeDrawer();
  if((e.key === 'Enter' || e.key === ' ') && e.target.matches && e.target.matches('tr[data-act]')){ e.preventDefault(); e.target.click(); }
});

// ══════════════════════════════════════════════════════════════════
//  部署單與操作確認卡（取代舊的 B1～B4 卡片；流程邏輯與按鈕行為不變）
// ══════════════════════════════════════════════════════════════════
function flowStateAttrs(flow){
  const st = {id:flow.id, seq:flow.seq, kind:flow.kind, step:flow.step, originalText:flow.originalText,
              spec:flow.spec, review:flow.review, resource:flow.resource, rollout:flow.rollout || null,
              resultMsg:flow.resultMsg || null, errorMsg:flow.errorMsg || null, blockers:flow.blockers || null,
              retryStep:flow.retryStep || null, pendingSync:!!flow.pendingSync, scenario:flow.scenario || null,
              compose:flow.compose || null, source:flow.source || null, projectId:flow.projectId || null,
              when:flow.when || null, schedOn:!!flow.schedOn, task:flow.task || null};
  return `data-flow-id="${flow.id}" data-flow-seq="${flow.seq}" data-flow-step="${flow.step}" data-flow-state="${encodeURIComponent(JSON.stringify(st))}" data-summary="${esc(flowSummary(flow))}"`;
}
function flowSummary(flow){
  const s = flow.spec || {};
  if(flow.kind === 'deploy') return `部署單 ${s.app_name || ''}（${s.image || ''}），狀態：${flow.step}`;
  if(flow.kind === 'compose') return `docker-compose 匯入：${((flow.compose || {}).services || []).map(x => x.app_name).join('、')}，狀態：${flow.step}`;
  return `操作確認：${destructiveSummary(flow)}，狀態：${flow.step}`;
}
function flowShell(flow, inner, cls){
  return `<div class="module flow ${cls || ''}" ${flowStateAttrs(flow)}>${inner}</div>`;
}
function stepsHTML(n){
  const names = ['確認規格', '審查', '部署'];
  return `<ol class="steps" aria-label="部署步驟">${names.map((x, i) => `<li class="${i + 1 === n ? 'on' : i + 1 < n ? 'done' : ''}" ${i + 1 === n ? 'aria-current="step"' : ''}>${x}</li>`).join('')}</ol>`;
}
function flowHead(title, right){ return `<div class="module-head"><h2>${title}</h2>${right || ''}</div>`; }

function renderFlowCard(flow){
  if(flow.step === 'expired'){
    return flowShell(flow, flowHead('這個請求已經失效', '') + `<div class="body"><div class="lead">重新整理頁面後，進行到一半的流程沒辦法接著做。需要的話，請再說一次你想做的事。</div></div>`);
  }
  if(flow.step === 'cancelled') return renderCancelledCard(flow);
  if(flow.step === 'scheduled') return renderScheduledCard(flow);
  if(flow.kind === 'compose') return renderComposeCard(flow);
  if(flow.kind === 'deploy'){
    if(flow.step === 'spec') return renderSpecCard(flow);
    if(flow.step === 'review') return renderReviewCard(flow);
    if(flow.step === 'executing') return renderBusyCard(flow, 'Deploying');
    if(flow.step === 'done') return renderDoneCard(flow);
    if(flow.step === 'error') return renderErrorCard(flow);
  } else {
    if(flow.step === 'spec') return renderDestructiveCard(flow);
    if(flow.step === 'executing') return renderBusyCard(flow, 'Working');
    if(flow.step === 'done') return renderActionDoneCard(flow);
    if(flow.step === 'error') return renderErrorCard(flow);
  }
  return flowShell(flow, '<div class="busy"><div class="spinner"></div></div>');
}

// ── docker-compose 匯入：檢視服務 → 審查 → 部署 → 完成 ──
// 每個服務都送進跟一句話部署相同的 /api/deploy/parse、/api/deploy，沒有任何服務能繞過把關。
function looksLikeCompose(text){
  const k8s = /(^|\n)apiVersion\s*:/.test(text) && /(^|\n)kind\s*:/.test(text);
  return k8s || (/(^|\n)services\s*:\s*\n/.test(text) && /\n\s+image\s*:|\n\s+build\s*:/.test(text));
}
async function onComposeFile(input){
  const f = input.files && input.files[0];
  input.value = '';
  if(!f) return;
  if(f.size > 100000){ setComposerHint('檔案太大，上限 100 KB。', 'voice-err'); return; }
  let text = '';
  try{ text = await f.text(); }catch(e){ setComposerHint('讀不到這個檔案，請再選一次。', 'voice-err'); return; }
  if(!currentChatId) newChat();
  appendMsg('user', `上傳 ${f.name}`, null, 'text');
  await startComposeFlow(text, f.name);
}
async function startComposeFlow(text, source){
  appendTyping('Parsing');
  try{
    const pid = currentChat()?.projectId || undefined;
    const r = await fetch('/api/import/parse', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({text, project_id:pid, filename:pid ? (source || '') : undefined})});
    const d = await r.json();
    removeTyping();
    if(d.error){ appendMsg('assistant', '沒辦法匯入這份檔案：' + d.error, null, 'text'); return; }
    if(d.project_file && !d.project_file.ok) appendMsg('assistant', '這份檔案沒有存進專案的「檔案」分頁：' + d.project_file.error + '（不影響這次部署）', null, 'text');
    const flow = newFlow('compose', source || '', {});
    flow.step = 'list';
    flow.source = source || '';
    flow.compose = {format: d.format || 'compose', services: d.services.map(s => Object.assign(s, {state:'', review:null, error:null})), problems: d.problems || []};
    appendMsg('assistant', renderFlowCard(flow));
  }catch(e){
    removeTyping();
    appendMsg('assistant', '匯入時連線失敗，請再試一次。（' + e + '）', null, 'text');
  }
}
const cpBlocked = s => (s.problems || []).some(p => p.level === 'block');
const cpSpec = s => ({app_name:s.app_name, image:s.image, pods:s.pods, port:s.port, memory:s.memory || undefined, cpu:s.cpu || undefined,
                      env:s.env, container_port:s.container_port, expose:s.expose, dns_alias:s.dns_alias,
                      command:s.command || undefined, args:s.args || undefined, volumes:s.volumes || undefined,
                      secret_keys:(s.secret_keys || []).length ? s.secret_keys : undefined});
const CP_LV = {ok:'通過', warn:'提醒', stop:'擋下'};
function cpVerdicts(rv){
  if(!rv) return '';
  const ag = (rv.agents && rv.agents.agents) || {};
  const one = (tag, key) => {
    const blocked = (rv.blockers || []).some(b => String(b).startsWith('[' + tag + ']'));
    const issues = (ag[key] && ag[key].issues) || [];
    const lv = blocked ? 'stop' : issues.some(i => ['critical', 'high', 'medium'].includes(i.severity)) ? 'warn' : 'ok';
    return `<span class="cp-pill ${lv}">${tag}${CP_LV[lv]}</span>`;
  };
  return `<span class="cp-verdicts">${one('安全', 'security')}${one('成本', 'cost')}${one('效能', 'perf')}</span>`;
}
function cpStatePill(s){
  if(cpBlocked(s)) return '<span class="cp-pill stop">無法部署</span>';
  const map = {reviewing:['run', '審查中'], approve:['ok', '審查通過'], warn:['warn', '通過但有提醒'], block:['stop', '審查擋下'],
               deploying:['run', '部署中'], deployed:['ok', '已部署'], failed:['stop', '失敗'], skipped:['', '略過']};
  const [c, t] = map[s.state] || ['', '待審查'];
  return `<span class="cp-pill ${c}">${t}</span>`;
}
function renderComposeCard(flow){
  const cp = flow.compose || {services:[], problems:[]};
  const svcs = cp.services;
  const ready = svcs.filter(s => !cpBlocked(s));
  const reviewedOk = svcs.filter(s => ['approve', 'warn'].includes(s.state));
  const stepN = {list:1, reviewing:2, review:2, deploying:3, done:3, error:3}[flow.step] || 1;
  const steps = `<ol class="steps" aria-label="匯入步驟">${['檢視服務', '審查', '部署'].map((x, i) => `<li class="${i + 1 === stepN ? 'on' : i + 1 < stepN ? 'done' : ''}">${x}</li>`).join('')}</ol>`;
  const order = `<ol class="cp-order" aria-label="部署順序">${svcs.map((s, i) => `<li class="${cpBlocked(s) ? 'blocked' : ''}" style="--i:${i}">${esc(s.app_name)}</li>`).join('')}</ol>`;
  const rows = svcs.map((s, i) => {
    const envKeys = Object.keys(s.env || {});
    const body = `<div class="cp-body">
      <div class="cp-kv"><span>映像檔 <b>${esc(s.image || '未指定')}</b></span><span>副本 <b>${esc(s.pods)}</b></span>
        <span>${s.expose ? `對外 port <b>${esc(s.port)}</b>，容器 <b>${esc(s.container_port)}</b>` : `只給內部連線，port <b>${esc(s.container_port)}</b>`}</span>
        ${s.memory ? `<span>記憶體 <b>${esc(s.memory)}</b></span>` : ''}${s.cpu ? `<span>CPU <b>${esc(s.cpu)}</b></span>` : ''}
        ${(s.depends_on || []).length ? `<span>依賴 <b>${esc(s.depends_on.join('、'))}</b></span>` : ''}
        <span>其他服務用 <b>${esc(s.app_name)}:${esc(s.container_port)}</b> 連它</span>
        ${(s.command || s.args) ? `<span>啟動指令 <b>${esc([].concat(s.command || [], s.args || []).join(' '))}</b></span>` : ''}
        ${(s.volumes || []).length ? `<span>資料空間 <b>${esc(s.volumes.map(v => v.mount + (v.kind === 'pvc' ? '（保存 ' + v.size + '）' : '（暫存）')).join('、'))}</b></span>` : ''}</div>
      ${envKeys.length ? `<div class="cp-env" aria-label="環境變數">${envKeys.map(k => `<span>${esc(k)} = ${/PASS|SECRET|TOKEN|KEY|CREDENTIAL|PRIVATE/i.test(k) || (s.secret_keys || []).includes(k) ? '********' : esc(s.env[k])}</span>`).join('')}</div>` : ''}
      ${(s.problems || []).length ? `<ul class="cp-probs">${s.problems.map(p => `<li class="${esc(p.level)}">${esc(p.text)}</li>`).join('')}</ul>` : ''}
      ${s.error ? `<ul class="cp-probs"><li class="block">${esc(s.error)}</li></ul>` : ''}
      ${s.review ? `<div class="cp-review">${reviewerOf('安全', (s.review.agents || {}).agents ? s.review.agents.agents.security : null, '', '安全', s.review)}${reviewerOf('成本', (s.review.agents || {}).agents ? s.review.agents.agents.cost : null, '', '成本', s.review)}${reviewerOf('效能', (s.review.agents || {}).agents ? s.review.agents.agents.perf : null, '', '效能', s.review)}</div>` : ''}
    </div>`;
    return `<details class="cp-svc" style="--i:${i}" ${cpBlocked(s) || s.state === 'block' || s.state === 'failed' ? 'open' : ''}>
      <summary><span class="nm">${esc(s.app_name)}</span><span class="im">${esc(s.image || '未指定映像檔')}</span>
        <span class="pt">${s.expose ? `對外 <b>${esc(s.port)}</b>` : '只給內部'}</span>
        <span class="rp">${esc(s.pods)} 個副本</span>${s.review && flow.step !== 'deploying' && flow.step !== 'done' ? cpVerdicts(s.review) : cpStatePill(s)}</summary>${body}</details>`;
  }).join('');
  const top = (cp.problems || []).length ? `<ul class="cp-probs" style="margin-bottom:12px">${cp.problems.map(p => `<li class="${esc(p.level)}">${esc(p.text)}</li>`).join('')}</ul>` : '';
  let lead = '', foot = '', prog = '';
  if(flow.step === 'list'){
    lead = `從${esc(flow.source || '檔案')}讀到 ${svcs.length} 個服務，會照下面的順序部署（${cp.format === 'k8s' ? '只給內部連的服務先部署' : '被依賴的先部署'}）。點任一個服務可以看完整設定和注意事項。`
      + (ready.length < svcs.length ? ` 有 ${svcs.length - ready.length} 個服務沒辦法部署，原因寫在該服務裡。` : '');
    foot = `<button type="button" class="btn" onclick="cancelFlow('${flow.id}')">取消</button>
      <button type="button" class="btn primary" onclick="composeReview('${flow.id}')" ${ready.length ? '' : 'disabled'}>審查 ${ready.length} 個服務</button>`;
  } else if(flow.step === 'reviewing' || flow.step === 'deploying'){
    const pool = flow.step === 'reviewing' ? ready : reviewedOk.concat(svcs.filter(s => ['deploying', 'deployed', 'failed'].includes(s.state)));
    const done = svcs.filter(s => flow.step === 'reviewing' ? ['approve', 'warn', 'block', 'failed'].includes(s.state) : ['deployed', 'failed'].includes(s.state)).length;
    const total = flow.step === 'reviewing' ? ready.length : svcs.filter(s => ['deploying', 'deployed', 'failed'].includes(s.state) || s.queued).length;
    lead = flow.step === 'reviewing' ? '每個服務都會經過安全、成本、效能三方審查，跟一句話部署完全一樣。' : '照順序一個一個部署，前一個完成才會開始下一個。';
    prog = `<div class="cp-prog" role="progressbar" aria-valuenow="${done}" aria-valuemax="${total}"><i style="width:${total ? Math.round(done / total * 100) : 0}%"></i></div>`;
    foot = `<div class="busy" style="padding:0"><div class="spinner"></div><div>${flow.step === 'reviewing' ? '審查中' : '部署中'}，${done}／${total}${flow.waitNote ? `<div class="dim">${esc(flow.waitNote)}</div>` : ''}</div></div>`;
  } else if(flow.step === 'review'){
    const blocked = svcs.filter(s => s.state === 'block').length;
    lead = `審查完成：${reviewedOk.length} 個可以部署` + (blocked ? `，${blocked} 個被擋下（展開看原因）` : '') + '。'
      + (reviewedOk.length < svcs.length ? ' 只會部署可以部署的服務；被略過的服務如果是別人依賴的，那些服務部署後可能連不上它。' : '');
    foot = `<button type="button" class="btn" onclick="cancelFlow('${flow.id}')">取消</button>
      <button type="button" class="btn primary" onclick="composeDeploy('${flow.id}')" ${reviewedOk.length ? '' : 'disabled'}>部署 ${reviewedOk.length} 個服務</button>`;
  } else if(flow.step === 'done'){
    const ok = svcs.filter(s => s.state === 'deployed').length, bad = svcs.filter(s => s.state === 'failed').length;
    lead = flow.pendingSync
      ? `已經把 ${ok} 個服務的設定送到你的叢集` + (bad ? `，${bad} 個失敗（展開看原因）` : '') + '。你叢集裡的 Flux 大約 30 到 60 秒內會套用，之後到「我的服務」就看得到。'
      : `部署完成：${ok} 個成功` + (bad ? `，${bad} 個失敗（展開看原因）` : '') + '。服務要等映像檔下載完、通過健康檢查才會開始接流量，通常需要幾十秒。';
    foot = `<button type="button" class="btn" onclick="showPage('services')">查看我的服務</button>`;
  }
  const title = cp.format === 'k8s' ? 'Kubernetes YAML 匯入' : 'docker-compose 匯入';
  return flowShell(flow, flowHead(title, steps) + `<div class="body"><div class="lead">${lead}</div>${prog}${order}${top}${rows}</div>`
    + (foot ? `<div class="foot">${foot}</div>` : ''));
}
// 部署相關的請求有頻率限制（每分鐘 10 次，core/web_security.py）。compose 一次送很多個，
// 被擋下（429）時照伺服器給的秒數等一下再送，不要直接算失敗。
async function cpFetch(flow, url, body){
  for(let attempt = 0; attempt < 8; attempt++){
    const r = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
    if(r.status !== 429) return r.json();
    const wait = Math.min(30, Math.max(1, parseInt(r.headers.get('Retry-After') || '6', 10)));
    flow.waitNote = `為了保護平台，部署請求每分鐘有次數上限，${wait} 秒後自動繼續。`;
    persistFlowCard(flow.id);
    await new Promise(res => setTimeout(res, wait * 1000));
    if(!flowAlive(flow)) throw new Error('cancelled');
    flow.waitNote = null;
  }
  throw new Error('請求次數太多，請稍後再試。');
}
async function composeReview(id){
  const flow = chatFlows[id];
  if(!flowAlive(flow)) return;
  flow.step = 'reviewing';
  const svcs = flow.compose.services;
  svcs.forEach(s => { if(!cpBlocked(s)){ s.state = 'reviewing'; s.review = null; s.error = null; } });
  persistFlowCard(id);
  for(const s of svcs){
    if(cpBlocked(s)) continue;
    try{
      const d = await cpFetch(flow, '/api/deploy/parse', {parsed:cpSpec(s)});
      if(!flowAlive(flow)) return;
      s.review = d.review || null;
      if(d.error){ s.state = 'block'; s.error = d.error; }
      else s.state = (d.review && d.review.decision) || (d.rejected ? 'block' : 'approve');
    }catch(e){
      if(!flowAlive(flow)) return;
      s.state = 'failed'; s.error = '連線失敗，請再試一次。（' + e + '）';
    }
    persistFlowCard(id);
  }
  flow.step = 'review';
  persistFlowCard(id);
}
async function composeDeploy(id){
  const flow = chatFlows[id];
  if(!flowAlive(flow)) return;
  flow.step = 'deploying';
  const svcs = flow.compose.services;
  svcs.forEach(s => { s.queued = ['approve', 'warn'].includes(s.state); if(!s.queued && !cpBlocked(s) && s.state !== 'block') s.state = 'skipped'; });
  persistFlowCard(id);
  for(const s of svcs){
    if(!s.queued) continue;
    s.state = 'deploying';
    persistFlowCard(id);
    try{
      const d = await cpFetch(flow, '/api/deploy', {parsed:cpSpec(s), project_id:flow.projectId || undefined});
      if(!flowAlive(flow)) return;
      const ok = !d.error && !d.rejected && !(d.k8s_deploy && d.k8s_deploy.ok === false);
      s.state = ok ? 'deployed' : 'failed';
      if(ok && d.k8s_deploy && d.k8s_deploy.pending_sync) flow.pendingSync = true;
      if(!ok) s.error = d.error || d.reason || (d.k8s_deploy && d.k8s_deploy.message) || '部署失敗。';
    }catch(e){
      if(!flowAlive(flow)) return;
      s.state = 'failed'; s.error = '連線失敗。（' + e + '）';
    }
    persistFlowCard(id);
  }
  flow.step = 'done';
  persistFlowCard(id);
}

function renderCancelledCard(flow){
  return flowShell(flow, flowHead('已取消', '') + `<div class="body"><div class="lead">${flow.kind === 'deploy' || flow.kind === 'compose' ? '這次部署沒有執行。' : '這個操作沒有執行。'}</div></div>`);
}
function renderBusyCard(flow, label){
  const txt = TYPING_LABELS[label] || label;
  const sub = label === 'Reviewing' ? '會檢查有沒有安全風險、要花多少資源、效能設定合不合理。'
            : label === 'Deploying' ? '正在寫入部署紀錄，並把設定送到叢集。' : '';
  return flowShell(flow, flowHead(flow.kind === 'deploy' ? '部署單' : '執行中', flow.kind === 'deploy' ? stepsHTML(label === 'Deploying' ? 3 : 2) : '')
    + `<div class="busy"><div class="spinner"></div><div style="min-width:0"><div>${esc(txt)}</div>${sub ? `<div class="dim">${sub}</div>` : ''}</div><div class="scanbar" aria-hidden="true"></div></div>`);
}

// 情境推算：把「怎麼算出副本數」「能省多少」攤開給使用者看。AI 判讀的欄位另外標示，
// 跟原句直接抽出來的數字分開，使用者才知道哪些要多看一眼。
function scenarioHTML(sc){
  if(!sc || !sc.applies) return '';
  const a = sc.attrs || {}, src = sc.sources || {};
  const aiMark = k => src[k] === 'model' ? '<em>AI 判讀</em>' : '';
  const facts = [];
  if(a.total_users) facts.push(['使用人數', a.total_users + ' 人', aiMark('total_users')]);
  if(a.concurrent_users) facts.push(['同時在線', a.concurrent_users + ' 人', aiMark('concurrent_users')]);
  if(a.duration_days) facts.push(['使用期間', (a.duration_days < 1 ? Math.round(a.duration_days * 24) + ' 小時' : a.duration_days + ' 天'), aiMark('duration_days')]);
  if(a.criticality) facts.push(['重要程度', {high:'高', normal:'一般', low:'測試用'}[a.criticality] || a.criticality, aiMark('criticality')]);
  const head = sc.replicas_applied
    ? `依你描述的情境，建議開 <b>${esc(sc.replicas)}</b> 個副本`
    : `你指定了 <b>${esc(sc.replicas)}</b> 個副本，下面是情境推算的參考`;
  let save = '';
  const sv = sc.savings;
  if(sv && sv.total_usd > 0){
    const max = Math.max(sv.baseline_usd, sv.scenario_usd, 0.01);
    const pct = v => Math.max(2, Math.round(v / max * 100));
    save = `<div class="scn-save">
      <div class="scn-save-top"><span>跟沒考慮情境的作法相比，估計可以省下</span><b>$${esc(sv.total_usd)}</b></div>
      <div class="scn-bars">
        <div class="scn-bar"><span>沒考慮情境</span><div class="track"><div class="fill" style="width:${pct(sv.baseline_usd)}%"></div></div><b>$${esc(sv.baseline_usd)}</b></div>
        <div class="scn-bar mine"><span>照情境推算</span><div class="track"><div class="fill" style="width:${pct(sv.scenario_usd)}%"></div></div><b>$${esc(sv.scenario_usd)}</b></div>
      </div>
      <ul>${(sv.items || []).map(i => `<li><span>${esc(i.label)}</span><b>省 $${esc(i.usd)}</b></li>`).join('')}</ul>
      <p class="scn-basis">${esc(sv.basis || '')}</p></div>`;
  }
  return `<section class="scn" aria-label="情境推算">
    <div class="scn-head"><span class="scn-tag">SCENARIO</span><h3>${head}</h3></div>
    ${facts.length ? `<div class="scn-facts">${facts.map((f, i) => `<div class="scn-fact" style="--i:${i}"><span>${f[0]}</span><b>${esc(f[1])}</b>${f[2]}</div>`).join('')}</div>` : ''}
    ${(sc.reasons || []).length ? `<ol class="scn-steps">${sc.reasons.map((r, i) => `<li style="--i:${i}">${esc(r)}</li>`).join('')}</ol>` : ''}
    ${(sc.notes || []).length ? `<ul class="scn-notes">${sc.notes.map(n => `<li>${esc(n)}</li>`).join('')}</ul>` : ''}
    ${save}</section>`;
}

function renderSpecCard(flow){
  const s = flow.spec || {};
  const out = s.output || s;
  const g = k => esc(out[k] != null ? out[k] : (s[k] != null ? s[k] : ''));
  return flowShell(flow, flowHead('部署單', stepsHTML(1)) + `<div class="body">
    <div class="lead">${flow.scenario && flow.scenario.applies ? '我依你描述的情境推算出下面的規格，推算過程如下。' : '我照你的描述整理成下面的規格。'}有不對的地方直接改，再按「下一步」。${flow.schedOn ? `<br><b>這是預約部署</b>${flow.when && flow.when.ts ? `（${esc(fmtTW(flow.when.ts))}）` : ''}，審查通過後可以在下一步調整時間。` : ''}</div>
    ${scenarioHTML(flow.scenario)}
    <div class="fgrid">
      <div><label for="${flow.id}-app">服務名稱</label><input id="${flow.id}-app" data-field="app_name" value="${g('app_name')}" placeholder="my-app"><div class="hint">小寫英文字母開頭，只能用小寫字母、數字和連字號，例如 web-frontend</div></div>
      <div><label for="${flow.id}-img">映像檔</label><input id="${flow.id}-img" data-field="image" value="${g('image')}" placeholder="nginx:1.27"></div>
      <div><label for="${flow.id}-pods">副本數</label><input id="${flow.id}-pods" data-field="pods" type="number" min="1" max="50" value="${g('pods') || 1}"></div>
      <div><label for="${flow.id}-port">Port</label><input id="${flow.id}-port" data-field="port" type="number" min="1" max="65535" value="${g('port') || 80}"></div>
      <div><label for="${flow.id}-mem">每個副本的記憶體（選填）</label><input id="${flow.id}-mem" data-field="memory" value="${g('memory')}" placeholder="例如 256Mi"></div>
      <div><label for="${flow.id}-cpu">每個副本的 CPU（選填）</label><input id="${flow.id}-cpu" data-field="cpu" value="${g('cpu')}" placeholder="例如 100m"></div>
    </div>
    <div class="err" data-role="error" role="alert"></div></div>
    <div class="foot"><button type="button" class="btn" onclick="cancelFlow('${flow.id}')">取消</button>
      <button type="button" class="btn primary" onclick="flowToReview('${flow.id}')">下一步：審查</button></div>`);
}
function readSpecCard(id){
  const root = document.querySelector(`[data-flow-id="${id}"]`);
  if(!root) return null;
  const get = f => (root.querySelector(`[data-field="${f}"]`)?.value || '').trim();
  const spec = {app_name:get('app_name'), image:get('image'), pods:parseInt(get('pods'), 10), port:parseInt(get('port'), 10)};
  const mem = get('memory'), cpu = get('cpu');
  if(mem) spec.memory = mem;
  if(cpu) spec.cpu = cpu;
  return {root, spec};
}
function setFlowError(root, msg){
  const el = root && root.querySelector('[data-role="error"]');
  if(el){ el.style.display = msg ? 'block' : 'none'; el.textContent = msg || ''; }
}

function splitImage(img){
  const s = String(img || '');
  const at = s.lastIndexOf(':');
  if(at > 0 && !s.slice(at).includes('/')) return [s.slice(0, at), s.slice(at + 1)];
  return [s, ''];
}
// 審查訊息拿掉程式內部的前綴，例如「[0] Deployment/nginx: 」
function cleanIssue(m){
  return String(m || '')
    .replace(/^\s*(?:\[[^\]]*\]\s*)+/, '')
    .replace(/^[A-Za-z]+\/[\w.-]+:\s*/, '')
    .replace(/'([^']+)'/g, '$1')
    .replace(/^\s*[✓✗⚠~]\s*/, '');
}
const SEV_RANK = {critical:0, high:1, medium:2, low:3, info:4};
function reviewerOf(title, a, extra, tag, rv){
  a = a || {};
  const blocked = ((rv && rv.blockers) || []).some(b => String(b).startsWith('[' + tag + ']'));
  const issues = (a.issues || []).slice().sort((x, y) => (SEV_RANK[x.severity] ?? 5) - (SEV_RANK[y.severity] ?? 5));
  // 「擋下」以 orchestrator 實際列出的阻斷原因為準（致命問題或安全分數過低），
  // 卡片上的每一項才會跟最終判定一致；其他中高嚴重度的問題只算提醒
  const worst = blocked ? 'stop' : issues.some(i => ['critical', 'high', 'medium'].includes(i.severity)) ? 'warn' : 'ok';
  const verdict = {ok:'通過', warn:'提醒', stop:'擋下'}[worst];
  // 每一條判斷到的問題各列一點，圓點顏色代表嚴重程度；沒有問題時寫一句摘要
  const listed = issues.filter(i => i.message);
  const SHOW = 4;
  const body = listed.length
    ? `<ul class="r-list">${listed.slice(0, SHOW).map(i => `<li class="sev-${esc(i.severity || 'info')}">${esc(cleanIssue(i.message))}</li>`).join('')}</ul>`
      + (listed.length > SHOW ? `<div class="r-more">還有 ${listed.length - SHOW} 項較輕微的建議</div>` : '')
    : `<p class="r-ok">${esc(cleanIssue(a.summary || extra || '沒有發現問題。'))}</p>`;
  return `<div class="reviewer ${worst}"><i class="scan" aria-hidden="true"></i>
    <div class="r-head"><h3 class="role">${title}</h3><span class="verdict"><i></i>${verdict}</span></div>${body}</div>`;
}
function renderReviewCard(flow){
  const rv = flow.review || {};
  const decision = rv.decision || 'block';
  const s = flow.spec || {};
  const [imgName, imgTag] = splitImage(s.image);
  const a = (rv.agents && rv.agents.agents) || {};
  const cEst = a.cost && a.cost.cost_estimate && a.cost.cost_estimate.estimated_usd;
  const rs = flow.resource;
  let res = '';
  if(rs){
    const parts = [];
    if(rs.total_cpu_cores != null || rs.total_mem_gib != null) parts.push(`總共需要 <b>${rs.total_cpu_cores ?? '?'}</b> 核、<b>${rs.total_mem_gib ?? '?'}</b> GiB`);
    if(rs.cluster_remaining_after){
      const r = rs.cluster_remaining_after;
      parts.push(`部署後叢集還剩 <b class="${r.cpu_cores < 0 ? 'neg' : ''}">${r.cpu_cores}</b> 核、<b class="${r.mem_gib < 0 ? 'neg' : ''}">${r.mem_gib}</b> GiB`);
    }
    if(rs.monthly_usd != null) parts.push(`依公有雲牌價估算每月 <b>$${rs.monthly_usd}</b>`);
    if(parts.length) res = `<div class="res-line">${parts.map(p => `<span>${p}</span>`).join('')}</div>`;
  }
  const seal = {approve:['ok', '放行'], warn:['warn', '注意'], block:['stop', '退回']}[decision] || ['stop', '退回'];
  let say, actions;
  if(decision === 'approve'){
    say = flow.schedOn ? `<b>可以預約</b><span class="dim">三項審查都通過了。時間到了會再審查一次，你確認後才部署。</span>` : `<b>可以部署</b><span class="dim">三項審查都通過了。</span>`;
    actions = schedActionsHTML(flow, `<button type="button" class="btn" onclick="cancelFlow('${flow.id}')">取消</button><button type="button" class="btn primary" onclick="flowExecuteDeploy('${flow.id}')">開始部署</button>`);
  } else if(decision === 'warn'){
    const ws = (rv.warnings || []).slice(0, 4).map(w => `<li>${esc(cleanIssue(w))}</li>`).join('');
    say = `<b>${flow.schedOn ? '可以預約' : '可以部署'}，但有幾點要注意</b>${ws ? `<ul>${ws}</ul>` : ''}`;
    actions = schedActionsHTML(flow, `<button type="button" class="btn" onclick="cancelFlow('${flow.id}')">取消</button><button type="button" class="btn primary" onclick="flowExecuteDeploy('${flow.id}')">我了解，仍要部署</button>`);
  } else {
    const bs = (rv.blockers || []).slice(0, 5).map(b => `<li>${esc(cleanIssue(b))}</li>`).join('');
    say = `<b>這次部署被擋下了</b>${bs ? `<ul>${bs}</ul>` : `<span class="dim">${esc(rv.reason || '')}</span>`}<span class="dim">修改規格後可以重新送審。</span>`;
    actions = `<button type="button" class="btn" onclick="cancelFlow('${flow.id}')">關閉</button><button type="button" class="btn primary" onclick="flowBackToSpec('${flow.id}')">修改規格</button>`;
  }
  return flowShell(flow, flowHead(`<span class="id">${esc(s.app_name || '')}</span>`, stepsHTML(2)) + `
    <dl class="spec">
      <div><dt>映像檔</dt><dd>${esc(imgName)}${imgTag ? `<small>${esc(imgTag)}</small>` : ''}</dd></div>
      <div><dt>副本</dt><dd>${esc(s.pods ?? '')}<small>個</small></dd></div>
      <div><dt>Port</dt><dd>${esc(s.port ?? '')}</dd></div>
      <div><dt>每個副本的記憶體</dt><dd>${s.memory ? esc(String(s.memory).replace(/(Mi|Gi)$/, '')) + `<small>${esc((String(s.memory).match(/(Mi|Gi)$/) || [''])[0])}</small>` : '<small>系統預設</small>'}</dd></div>
    </dl>
    <div class="review">
      ${reviewerOf('安全審查', a.security, '', '安全', rv)}
      ${reviewerOf('成本審查', a.cost, cEst != null ? `依公有雲牌價估算每月約 $${cEst}。` : '', '成本', rv)}
      ${reviewerOf('效能審查', a.perf, '', '效能', rv)}
    </div>
    ${res}
    ${flow.schedOn && decision !== 'block' ? `<div class="body" style="padding-bottom:0">${schedBoxHTML(flow)}</div>` : ''}
    <div class="decision">
      <div class="seal ${seal[0]}" aria-hidden="true"><svg viewBox="0 0 76 76"><circle class="ring-o" cx="38" cy="38" r="36"/><circle class="ticks" cx="38" cy="38" r="30"/><text x="38" y="45" text-anchor="middle">${seal[1]}</text></svg></div>
      <div class="say">${say}</div>
      <div class="actions">${actions}</div>
    </div>`, decision === 'block' ? 'danger' : '');
}
function flowBackToSpec(id){
  const flow = chatFlows[id];
  if(!flow) return;
  flow.step = 'spec'; flow.errorMsg = null; flow.blockers = null;
  persistFlowCard(id);
}
// 審查結果出來時播一次：三項依序掃描，最後蓋上封印。重新整理後只顯示最終結果，不重播。
function playReview(id){
  if(reducedMotion()) return;
  const el = document.querySelector(`[data-flow-id="${id}"]`);
  if(!el) return;
  const reviewers = [...el.querySelectorAll('.reviewer')];
  const seal = el.querySelector('.seal');
  el.classList.add('replay');
  reviewers.forEach(r => r.classList.add('waiting'));
  if(seal) seal.classList.add('armed');
  reviewers.forEach((r, i) => {
    setTimeout(() => { r.classList.remove('waiting'); void r.offsetWidth; r.classList.add('scanning'); }, 250 + i * 480);
    setTimeout(() => r.classList.remove('scanning'), 250 + i * 480 + 700);
  });
  setTimeout(() => { if(seal){ seal.classList.remove('armed'); void seal.offsetWidth; seal.classList.add('on'); } }, 250 + reviewers.length * 480 + 300);
  setTimeout(() => el.classList.remove('replay'), 250 + reviewers.length * 480 + 1600);
}

function destructiveSummary(flow){
  const a = flow.spec || {};
  if(flow.kind === 'scale') return `把「${a.name}」的副本從 ${a.current != null ? a.current : '?'} 個調成 ${a.replicas} 個`;
  if(flow.kind === 'update_image') return `把「${a.name}」的映像檔換成 ${a.image}`;
  if(flow.kind === 'rollback') return `把「${a.name}」回滾到上一版設定`;
  if(flow.kind === 'delete') return `刪除服務「${a.name}」和它的 Service`;
  if(flow.kind === 'healer_fix') return `讓 Healer 診斷並修復 Pod「${a.pod_name}」`;
  if(flow.kind === 'healer_auto_fix') return '讓 Healer 修復所有有狀況的 Pod';
  return '';
}
function renderDestructiveCard(flow){
  const a = flow.spec || {};
  const k8sOff = (flow.kind === 'scale' || flow.kind === 'update_image') && k8sEnabled === false;
  const titles = {scale:'調整副本數', update_image:'更新映像檔', rollback:'回滾', delete:'刪除服務', healer_fix:'修復 Pod', healer_auto_fix:'全部自動修復'};
  const confirmLabel = {scale:'確認調整', update_image:'確認更新', rollback:'確認回滾', delete:'確認刪除', healer_fix:'開始修復', healer_auto_fix:'開始修復'}[flow.kind] || '確認';
  let detail;
  if(flow.kind === 'scale') detail = `<div class="delta"><span class="id">${esc(a.name)}</span><span class="from num">${a.current != null ? esc(a.current) : '?'} 個</span><span class="arrow" aria-hidden="true"></span><span class="to">${esc(a.replicas)} 個副本</span></div>`;
  else if(flow.kind === 'update_image') detail = `<div class="delta"><span class="id">${esc(a.name)}</span><span class="from id">${esc(a.current || '目前版本')}</span><span class="arrow" aria-hidden="true"></span><span class="to id">${esc(a.image)}</span></div>`;
  else if(flow.kind === 'delete') detail = `<div class="lead">${esc(destructiveSummary(flow))}。<b class="t-stop">刪除之後沒辦法復原</b>，服務上的流量會中斷；如果這個服務有保存資料（例如從 docker-compose 匯入的資料庫），資料也會一起刪除。</div>`;
  else if(flow.kind === 'healer_fix') detail = `<div class="lead">${esc(destructiveSummary(flow))}。Healer 會先判斷原因，再套用對應的修復，例如調高記憶體上限或恢復上一個能用的映像檔。</div>`;
  else detail = `<div class="lead">${esc(destructiveSummary(flow))}。</div>`;
  const nowActs = `<button type="button" class="btn" onclick="cancelFlow('${flow.id}')">取消</button>
      <button type="button" class="btn ${flow.kind === 'delete' ? 'danger' : 'primary'}" onclick="flowExecuteAction('${flow.id}')" ${k8sOff ? 'disabled' : ''}>${confirmLabel}</button>`;
  return flowShell(flow, flowHead(titles[flow.kind] || '確認操作', `<span class="meta">${flow.schedOn ? '預約：時間到了提醒你確認後才執行' : '執行前請確認'}</span>`) + `<div class="body">
    ${detail}
    ${flow.schedOn ? schedBoxHTML(flow) : ''}
    <div class="err" data-role="error" role="alert"></div>
    ${k8sOff && !flow.schedOn ? '<div class="err" style="display:block">Kubernetes 目前沒有連線，這個操作沒辦法執行。</div>' : ''}</div>
    <div class="foot">${schedActionsHTML(flow, nowActs)}</div>`,
    flow.kind === 'delete' ? 'danger' : '');
}

// 部署之後的進度：每一步都要等系統真的回報才往前走
function legsHTML(flow){
  const ro = flow.rollout || {};
  const s = flow.spec || {};
  const legs = [
    {t:'審查通過', d:'done'},
    {t:'寫入部署紀錄', d:'done'},
    {t:flow.pendingSync ? '你的叢集同步中' : '叢集建立中', d:ro.state === 'running' ? 'done' : ro.state === 'timeout' ? 'bad' : 'now',
     sub:ro.state === 'running' ? '' : ro.state === 'timeout' ? '等太久了' : (flow.pendingSync ? '約 30 到 60 秒' : (ro.replicas ? `${ro.ready || 0}/${ro.replicas} 個副本就緒` : '確認中'))},
    {t:'運作中', d:ro.state === 'running' ? 'done' : '', sub:ro.state === 'running' ? `${ro.ready}/${ro.replicas} 個副本就緒` : '等待中'},
  ];
  const pct = ro.state === 'running' ? 100 : 66;
  return `<div class="legs-wrap"><div class="legs-line" aria-hidden="true"><span style="width:${pct}%"></span></div>
    <ol class="legs">${legs.map(l => `<li class="${l.d}">${l.t}<span>${l.sub || ''}</span></li>`).join('')}</ol></div>`;
}
function renderDoneCard(flow){
  const s = flow.spec || {};
  const ro = flow.rollout || {};
  let note = '';
  if(ro.state === 'timeout') note = `<div class="err" style="display:block">已經等了 5 分鐘，副本還沒全部就緒。可能是映像檔下載太慢或啟動失敗，點「查看服務」看原因，或說「${esc(s.app_name)} 有沒有壞」請 Healer 檢查。</div>`;
  return flowShell(flow, flowHead(`<span class="id">${esc(s.app_name || '')}</span>`, ro.state === 'running' ? '<span class="meta"><b class="state s-run"><i></i>部署完成</b></span>' : stepsHTML(3)) + legsHTML(flow) + `
    <div class="body" style="padding-top:0">${note}<div class="dim">${esc(s.image || '')}，${esc(s.pods ?? '')} 個副本${s.port ? `，Port ${esc(s.port)}` : ''}</div></div>
    <div class="foot" style="justify-content:flex-start">
      <button type="button" class="btn" data-act="open-dep" data-name="${esc(s.app_name || '')}">查看服務</button>
      ${ro.state && ro.state !== 'running' && !chatFlows[flow.id]?._tracking ? `<button type="button" class="btn" onclick="trackRollout('${flow.id}')">重新檢查進度</button>` : ''}
      <button type="button" class="btn" onclick="showPage('gitops')">部署紀錄</button>
    </div>`, ro.state === 'running' ? 'done-card' : '');
}
async function trackRollout(id){
  const flow = chatFlows[id];
  if(!flow || flow._tracking) return;
  const name = (flow.spec || {}).app_name;
  if(!name) return;
  flow._tracking = true;
  const started = Date.now();
  flow.rollout = Object.assign({}, flow.rollout || {}, {state:'syncing'});
  persistFlowCard(id);
  try{
    while(Date.now() - started < 5 * 60 * 1000){
      await new Promise(r => setTimeout(r, 3000));
      if(!chatFlows[id] || chatFlows[id] !== flow) return;
      try{
        const d = await getJSON('/api/deployments/' + encodeURIComponent(name));
        if(d.found){
          const dep = d.deployment;
          const changed = dep.ready !== flow.rollout.ready || dep.replicas !== flow.rollout.replicas;
          flow.rollout = {state:(dep.replicas > 0 && dep.ready >= dep.replicas) ? 'running' : 'syncing', ready:dep.ready || 0, replicas:dep.replicas || 0};
          if(flow.rollout.state === 'running'){ flow._tracking = false; persistFlowCard(id); return; }
          if(changed) persistFlowCard(id);
        }
      }catch(e){ /* 暫時讀不到就下一輪再試 */ }
    }
    flow.rollout = Object.assign({}, flow.rollout, {state:'timeout'});
  } finally {
    flow._tracking = false;
    persistFlowCard(id);
  }
}
function renderActionDoneCard(flow){
  return flowShell(flow, flowHead('完成', '<span class="meta"><b class="state s-run"><i></i>已執行</b></span>') + `<div class="body">
    <div class="lead">${esc(flow.resultMsg || destructiveSummary(flow))}</div>
    ${flow.pendingSync ? '<div class="dim">變更已經送出，你的叢集大約 30 到 60 秒內會同步完成。</div>' : ''}</div>
    <div class="foot" style="justify-content:flex-start">${(flow.spec || {}).name && flow.kind !== 'delete' ? `<button type="button" class="btn" data-act="open-dep" data-name="${esc(flow.spec.name)}">查看服務</button>` : ''}
      <button type="button" class="btn" onclick="showPage('gitops')">部署紀錄</button></div>`, 'done-card');
}
function renderErrorCard(flow){
  const list = (flow.blockers || []).slice(0, 6).map(b => `<li>${esc(b)}</li>`).join('');
  return flowShell(flow, flowHead('沒有完成', '<span class="meta"><b class="state s-fail"><i></i>失敗</b></span>') + `<div class="body">
    <div class="lead">${esc(flow.errorMsg || '操作失敗。')}</div>${list ? `<ul class="dim" style="margin:0;padding-left:1.2em">${list}</ul>` : ''}</div>
    <div class="foot"><button type="button" class="btn" onclick="cancelFlow('${flow.id}')">關閉</button>
      ${flow.retryStep ? `<button type="button" class="btn primary" onclick="retryFlow('${flow.id}')">再試一次</button>` : ''}</div>`, 'fail-card');
}
function cancelFlow(id){
  const flow = chatFlows[id];
  if(!flow) return;
  flow.step = 'cancelled'; flow.retryStep = null;
  persistFlowCard(id);
}

// 重新整理後：進行中的流程標成失效；部署完成但還沒確認運作中的，自動接著追進度
function hydrateChatFlows(){
  const ch = currentChat();
  if(!ch) return;
  const resume = [];
  ch.messages.forEach(m => {
    if(m.role !== 'assistant') return;
    const mm = String(m.content || '').match(/data-flow-state="([^"]*)"/);
    if(!mm) return;
    let st; try { st = JSON.parse(decodeURIComponent(mm[1])); } catch(e){ return; }
    if(st.kind === 'deploy' && st.step === 'done' && (!st.rollout || st.rollout.state !== 'running') && st.rollout?.state !== 'timeout'){
      chatFlows[st.id] = st; resume.push(st.id); return;
    }
    // 預約卡的按鈕操作的是伺服器上的預約（不靠記憶體），重新整理後照樣能用；狀態以伺服器為準重抓
    if(st.step === 'scheduled'){
      chatFlows[st.id] = st;
      if(st.task && st.task.id) getJSON('/api/schedule?status=all').then(d => {
        const cur = (d.tasks || []).find(x => x.id === st.task.id);
        if(cur && cur.status !== st.task.status){ st.task = cur; persistFlowCard(st.id); }
      }).catch(() => {});
      return;
    }
    if(['done', 'error', 'cancelled', 'expired'].includes(st.step)) return;
    st.step = 'expired';
    chatFlows[st.id] = st;
    m.content = renderFlowCard(st);
  });
  saveChats();
  if(resume.length) setTimeout(() => resume.forEach(id => trackRollout(id)), 500);
}

// ══════════════════════════════════════════════════════════════════
//  預約與行事曆（2026-10-23）
//  預約到期不自動執行：手機推播＋網站提示，使用者按「確定執行」才呼叫跟手動操作相同的 API。
//  時間一律以台北時間顯示與選擇（UTC+8，沒有日光節約），不受這台電腦的時區設定影響。
// ══════════════════════════════════════════════════════════════════
const SCHED_KINDS = ['deploy', 'scale', 'update_image', 'delete'];
const WD_ZH = '日一二三四五六';
const SCHED_STATUS = {scheduled:'已預約', due:'等你確認', running:'執行中', done:'已完成', failed:'失敗', blocked:'被擋下', cancelled:'已取消', expired:'已過期'};
function twParts(ts){ const d = new Date(ts * 1000 + 8 * 3600 * 1000); return {y:d.getUTCFullYear(), mo:d.getUTCMonth() + 1, d:d.getUTCDate(), h:d.getUTCHours(), mi:d.getUTCMinutes(), wd:d.getUTCDay()}; }
function twTs(y, mo, d, h, mi){ return Date.UTC(y, mo - 1, d, h, mi) / 1000 - 8 * 3600; }
const z2 = n => String(n).padStart(2, '0');
function fmtTW(ts){ const p = twParts(ts); return `${p.y}-${z2(p.mo)}-${z2(p.d)}（週${WD_ZH[p.wd]}）${z2(p.h)}:${z2(p.mi)}`; }
function fmtTWShort(ts){ const p = twParts(ts); return `${p.mo}/${p.d}（週${WD_ZH[p.wd]}）${z2(p.h)}:${z2(p.mi)}`; }
function twDay(ts){ const p = twParts(ts); return `${p.y}-${z2(p.mo)}-${z2(p.d)}`; }
function spanText(sec){
  sec = Math.max(0, Math.round(sec));
  const d = Math.floor(sec / 86400), h = Math.floor(sec % 86400 / 3600), m = Math.floor(sec % 3600 / 60);
  return d ? `${d} 天${h ? ` ${h} 小時` : ''}` : h ? `${h} 小時${m ? ` ${m} 分鐘` : ''}` : `${Math.max(1, m)} 分鐘`;
}
function countdown(ts){ const s = ts - Date.now() / 1000; return s > 0 ? `還有 ${spanText(s)}` : `已經過了 ${spanText(-s)}`; }
function defaultSchedTs(){ const t = Math.ceil((Date.now() / 1000 + 3600) / 300) * 300; return t; }
function schedPickerHTML(key, ts){
  const p = twParts(ts || defaultSchedTs());
  const opt = (from, to, sel, step, fmt) => { let o = ''; for(let i = from; i <= to; i += (step || 1)) o += `<option value="${i}" ${i === sel ? 'selected' : ''}>${fmt ? fmt(i) : i}</option>`; return o; };
  const mins = p.mi % 5 ? opt(p.mi, p.mi, p.mi, 1, z2) + opt(0, 55, -1, 5, z2) : opt(0, 55, p.mi, 5, z2);
  return `<div class="sched-pick" data-sched-key="${esc(key)}">
    <select data-sp="mo" aria-label="月">${opt(1, 12, p.mo)}</select><span>月</span>
    <select data-sp="d" aria-label="日">${opt(1, 31, p.d)}</select><span>日</span>
    <select data-sp="h" aria-label="時">${opt(0, 23, p.h, 1, z2)}</select><span>:</span>
    <select data-sp="mi" aria-label="分">${mins}</select></div>
    <div class="sched-when" data-role="sched-when">${schedWhenLine(ts || defaultSchedTs())}</div>`;
}
// 選的月日已經過了就是明年；2 月 30 日這種不存在的日期回傳 null
function readPicker(root){
  if(!root) return null;
  const v = k => parseInt(root.querySelector(`[data-sp="${k}"]`).value, 10);
  const mo = v('mo'), d = v('d'), h = v('h'), mi = v('mi');
  let y = twParts(Date.now() / 1000).y;
  let ts = twTs(y, mo, d, h, mi);
  if(ts < Date.now() / 1000 - 60){ y += 1; ts = twTs(y, mo, d, h, mi); }
  return twParts(ts).mo === mo ? ts : null;
}
function schedProblem(ts){
  if(!ts) return '這個日期不存在，請重新選。';
  const s = ts - Date.now() / 1000;
  if(s < 120) return `${fmtTW(ts)} 太近了，預約時間要在 2 分鐘以後；想馬上做請按「改成馬上執行」。`;
  if(s > 90 * 86400) return '預約最多只能排到 90 天內。';
  return '';
}
function schedWhenLine(ts){
  const bad = schedProblem(ts);
  return bad ? `<span class="bad">${esc(bad)}</span>` : `時間到了會提醒你確認：<b>${esc(fmtTW(ts))}</b>，${countdown(ts)}`;
}
document.addEventListener('change', e => {
  const sp = e.target.closest && e.target.closest('.sched-pick');
  if(!sp) return;
  const ts = readPicker(sp);
  const box = sp.parentElement;
  const w = box.querySelector('[data-role="sched-when"]');
  if(w) w.innerHTML = schedWhenLine(ts);
  const flow = chatFlows[sp.dataset.schedKey];
  if(flow && ts) flow.when = {ts};
  const btn = document.querySelector(`[data-sched-btn="${sp.dataset.schedKey}"]`);
  if(btn) btn.textContent = ts && !schedProblem(ts) ? `預約在 ${fmtTWShort(ts)}` : '預約';
});

// ── 確認卡上的「改成預約」 ──
function schedBoxHTML(flow){
  return `<div class="sched-box"><div class="t">預約時間<span class="dim" style="font-weight:400;font-size:13px">月／日　時：分（台北時間）</span></div>
    ${schedPickerHTML(flow.id, flow.when && flow.when.ts)}<div class="err" data-role="sched-error" role="alert"></div></div>`;
}
function schedActionsHTML(flow, nowActions){
  if(!SCHED_KINDS.includes(flow.kind)) return nowActions;
  if(flow.schedOn){
    const ts = (flow.when && flow.when.ts) || defaultSchedTs();
    return `<button type="button" class="btn" onclick="cancelFlow('${flow.id}')">取消</button>
      <button type="button" class="btn" onclick="flowSchedToggle('${flow.id}', false)">改成馬上執行</button>
      <button type="button" class="btn primary" data-sched-btn="${flow.id}" onclick="flowSchedule('${flow.id}')">${schedProblem(ts) ? '預約' : '預約在 ' + esc(fmtTWShort(ts))}</button>`;
  }
  return nowActions + `<button type="button" class="btn" onclick="flowSchedToggle('${flow.id}', true)">改成預約</button>`;
}
function flowSchedToggle(id, on){
  const flow = chatFlows[id];
  if(!flowAlive(flow)) return;
  flow.schedOn = !!on;
  if(on && !flow.when) flow.when = {ts:defaultSchedTs()};
  persistFlowCard(id);
}
async function flowSchedule(id){
  const flow = chatFlows[id];
  if(!flowAlive(flow)) return;
  const root = document.querySelector(`[data-flow-id="${id}"]`);
  const errEl = root && root.querySelector('[data-role="sched-error"]');
  const fail = m => { if(errEl){ errEl.style.display = 'block'; errEl.textContent = m; } };
  const ts = readPicker(root && root.querySelector('.sched-pick'));
  const bad = schedProblem(ts);
  if(bad) return fail(bad);
  const a = flow.spec || {};
  const body = {kind:flow.kind, run_at:ts, text:flow.originalText || ''};
  if(flow.kind === 'deploy') body.spec = a;
  else Object.assign(body, {name:a.name, replicas:a.replicas, image:a.image});
  const btn = root.querySelector(`[data-sched-btn="${id}"]`);
  if(btn){ btn.disabled = true; btn.textContent = '預約中'; }
  try{
    const r = await fetch('/api/schedule', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
    const d = await r.json();
    if(!flowAlive(flow)) return;
    if(!d.ok){ if(btn){ btn.disabled = false; btn.textContent = '再試一次'; } return fail(d.error || '預約失敗。'); }
    flow.task = d.task; flow.step = 'scheduled';
    persistFlowCard(id);
    refreshDueBadge();
  }catch(e){ if(btn){ btn.disabled = false; btn.textContent = '再試一次'; } fail('連線失敗，請再試一次。（' + e + '）'); }
}
function schedPill(s){ return `<span class="st-pill ${esc(s)}">${esc(SCHED_STATUS[s] || s)}</span>`; }
function schedRowActions(t){
  if(!['scheduled', 'due'].includes(t.status)) return '';
  return (t.status === 'due' ? `<button type="button" class="btn btn-sm primary" data-act="due-open">確認執行</button>` : '')
    + `<button type="button" class="btn btn-sm" data-act="sch-resched" data-id="${t.id}" data-ts="${t.run_at}">改時間</button>`
    + `<button type="button" class="btn btn-sm" data-act="sch-cancel" data-id="${t.id}" data-sum="${esc(t.summary)}">取消</button>`;
}
function renderScheduledCard(flow){
  const t = flow.task || {};
  const active = ['scheduled', 'due'].includes(t.status);
  const lead = active ? '時間到了會用手機推播和網站提示提醒你，<b>你確認之後才會執行</b>。這段期間可以隨時改時間或取消。'
    : esc(t.result || SCHED_STATUS[t.status] || '');
  return flowShell(flow, flowHead('預約', `<span class="meta">${schedPill(t.status)}</span>`) + `<div class="body">
    <div class="lead">${lead}</div>
    <dl class="sched-kv"><dt>預約編號</dt><dd>${esc('#' + t.id)}</dd><dt>時間</dt><dd>${esc(fmtTW(t.run_at))}${active ? `　<span class="dim">${countdown(t.run_at)}</span>` : ''}</dd><dt>內容</dt><dd>${esc(t.summary)}</dd></dl></div>
    <div class="foot" style="justify-content:flex-start"><button type="button" class="btn" data-act="sch-cal" data-ts="${t.run_at}">在行事曆查看</button>
      ${active ? `<button type="button" class="btn" data-act="sch-resched" data-id="${t.id}" data-ts="${t.run_at}">改時間</button>
      <button type="button" class="btn danger" data-act="sch-cancel" data-id="${t.id}" data-sum="${esc(t.summary)}">取消這個預約</button>` : ''}</div>`,
    active ? 'done-card' : '');
}
// 預約狀態改了：同步更新對話裡的預約卡、預約清單模組、行事曆、側欄數字
function schedChanged(task){
  if(task){
    Object.values(chatFlows).forEach(f => { if(f.task && f.task.id === task.id){ f.task = task; persistFlowCard(f.id); } });
  }
  document.querySelectorAll('.zt-w[data-w="schedule"]').forEach(w => refreshWidget(w));
  if(currentPageName() === 'calendar') loadCalendarPage(true);
  refreshDueBadge();
}
async function schedOp(id, op, body){
  const r = await fetch(`/api/schedule/${id}/${op}`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body || {})});
  const d = await r.json().catch(() => ({}));
  if(!d.ok) throw Object.assign(new Error(d.error || '操作失敗。'), {task:d.task});
  return d.task;
}
// 改時間：跳出跟確認卡一樣的「月日時分」選單
async function askNewTime(curTs){
  const ok = await askConfirm('改預約時間', `<p style="margin:0 0 10px">選新的時間（台北時間）：</p>${schedPickerHTML('dialog', curTs)}`, '改到這個時間', '不改了');
  if(!ok) return null;
  return readPicker(document.querySelector('#confirm-dialog-body .sched-pick'));
}
async function schedReschedule(id, curTs){
  const ts = await askNewTime(curTs);
  if(ts == null) return;
  const bad = schedProblem(ts);
  if(bad){ alertMsg(bad); return; }
  try{ schedChanged(await schedOp(id, 'reschedule', {run_at:ts})); }
  catch(e){ alertMsg(e.message); if(e.task) schedChanged(e.task); }
}
async function schedCancel(id, summary){
  const ok = await askConfirm('取消預約', `確定要取消「<b>${esc(summary || '#' + id)}</b>」嗎？取消後時間到了不會再提醒你，也不會執行。`, '確定取消', '不要取消');
  if(!ok) return;
  try{ schedChanged(await schedOp(id, 'cancel')); }
  catch(e){ alertMsg(e.message); if(e.task) schedChanged(e.task); }
}
function alertMsg(m){ askConfirm('沒有完成', esc(m), '知道了', '關閉'); }

// ── 預約清單模組（對話裡問「我有哪些預約」） ──
W.schedule = {
  summary: d => `預約清單：共 ${(d.items || []).length} 筆`,
  head: (d, st) => {
    const due = (d.items || []).filter(t => t.status === 'due').length;
    return `<h2>我的預約</h2><span class="meta"><span>共 ${(d.items || []).length} 筆</span>${due ? `<b class="t-warn">${due} 筆等你確認</b>` : ''}<span>${esc(d.at || '')} 的資料</span></span>
      <div class="filters" role="group" aria-label="依狀態篩選">${filterChips(st, [['active', '還沒執行'], ['due', '等你確認'], ['final', '已結束'], ['all', '全部']])}
      <button type="button" class="chip ghost" data-act="refresh">重新整理</button></div>`;
  },
  body: (d, st) => {
    const f = st.f || 'active';
    let rows = d.items || [];
    if(f === 'active') rows = rows.filter(t => ['scheduled', 'due'].includes(t.status));
    else if(f === 'due') rows = rows.filter(t => t.status === 'due');
    else if(f === 'final') rows = rows.filter(t => !['scheduled', 'due', 'running'].includes(t.status));
    if(d.only) rows = rows.filter(t => d.only.includes(t.id));
    if(!rows.length) return emptyBlock(f === 'active' ? '目前沒有還沒執行的預約。' : '沒有符合條件的預約。',
      '在對話裡說「明天早上 9 點部署 2 個 nginx」，或在確認卡按「改成預約」就能新增。');
    rows = rows.slice().sort((a, b) => f === 'final' ? b.run_at - a.run_at : a.run_at - b.run_at);
    const slice = pageSlice(rows, st);
    return `<div style="padding:4px 18px 10px">${slice.map(t => `<div class="sch-row ${t.kind === 'delete' ? 'del' : ''}">
      <span class="when">${esc(fmtTW(t.run_at))}</span>
      <span class="what"><b>${esc(t.summary)}</b>${t.result ? `<span class="res">${esc(t.result)}</span>` : ['scheduled', 'due'].includes(t.status) ? `<span class="res">${countdown(t.run_at)}</span>` : ''}</span>
      ${schedPill(t.status)}<span class="acts">${schedRowActions(t)}<button type="button" class="btn btn-sm" data-act="sch-cal" data-ts="${t.run_at}">行事曆</button></span></div>`).join('')}</div>
      ${pagerHTML(rows.length, st.p || 1, W_PER)}`;
  },
};
W_LOAD.schedule = async (old) => ({items:(await getJSON('/api/schedule?status=all')).tasks || [], only:(old && old.only) || null, at:nowHM()});

// 從一句話找出指的是哪一個預約：名稱、日期都比對，找不到或不只一個就列出來讓使用者選
function matchTasks(tasks, text, when){
  let rows = tasks.filter(t => ['scheduled', 'due'].includes(t.status));
  const names = rows.map(t => (t.args && (t.args.name || (t.args.spec || {}).app_name)) || '').filter(Boolean);
  const named = names.filter(n => text.includes(n));
  if(named.length) rows = rows.filter(t => named.includes((t.args && (t.args.name || (t.args.spec || {}).app_name)) || ''));
  if(when && when.found && !when.recurring){
    const sameDay = rows.filter(t => twDay(t.run_at) === twDay(when.ts));
    if(sameDay.length) rows = sameDay;
  }
  const kw = [['刪', 'delete'], ['部署', 'deploy'], ['擴', 'scale'], ['縮', 'scale'], ['映像', 'update_image'], ['版本', 'update_image']].filter(([k]) => text.includes(k)).map(([, v]) => v);
  if(kw.length && rows.some(t => kw.includes(t.kind))) rows = rows.filter(t => kw.includes(t.kind));
  return rows;
}
async function runScheduleIntent(action, text){
  const all = (await getJSON('/api/schedule?status=all')).tasks || [];
  removeTyping();
  if(action === 'list_schedule'){
    const w = await fetch('/api/schedule/when', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({text})}).then(r => r.json()).catch(() => ({}));
    let only = null, lead;
    const active = all.filter(t => ['scheduled', 'due'].includes(t.status));
    if(w.found && !w.recurring && /週|周|禮拜|星期/.test(text) && !/週[一二三四五六日天]/.test(text)){
      // 「下週要做什麼」：那一整週
      const p = twParts(w.ts), monday = twTs(p.y, p.mo, p.d, 0, 0) - ((p.wd + 6) % 7) * 86400;
      only = active.filter(t => t.run_at >= monday && t.run_at < monday + 7 * 86400).map(t => t.id);
      lead = `${fmtTW(monday).slice(0, 10)} 那一週有 ${only.length} 個預約。`;
    } else if(w.found && !w.recurring){
      only = active.filter(t => twDay(t.run_at) === twDay(w.ts)).map(t => t.id);
      lead = `${fmtTW(w.ts).slice(0, 14)}有 ${only.length} 個預約。`;
    } else {
      const due = active.filter(t => t.status === 'due').length;
      lead = active.length ? `你有 ${active.length} 個還沒執行的預約${due ? `，其中 ${due} 個已經到時間、等你確認` : ''}。` : '你目前沒有還沒執行的預約。';
    }
    appendWidget('schedule', {items:all, only, at:nowHM()}, {f:'active', p:1}, lead);
    return;
  }
  const w = await fetch('/api/schedule/when', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({text})}).then(r => r.json()).catch(() => ({}));
  if(action === 'reschedule'){
    // 「把 X 的預約改到 5 點」：新時間是句子裡「改到」後面那一段
    const tail = text.split(/改到|改成|改在|延到|延後到|提前到|挪到|換到/).slice(1).join(' ');
    const nw = tail ? await fetch('/api/schedule/when', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({text:tail})}).then(r => r.json()).catch(() => ({})) : {};
    const rows = matchTasks(all, text.split(/改到|改成|改在|延到|延後到|提前到|挪到|換到/)[0], null);
    if(rows.length !== 1){
      appendWidget('schedule', {items:all, only:rows.map(t => t.id), at:nowHM()}, {f:'active', p:1},
        rows.length ? `找到 ${rows.length} 個可能的預約，請在要改的那一列按「改時間」。` : '找不到符合的預約，下面是你所有還沒執行的預約。');
      return;
    }
    appendMsg('assistant', schedConfirmCardHTML('reschedule', rows[0], nw.found && !nw.recurring ? nw.ts : null));
    return;
  }
  const rows = matchTasks(all, text, w);
  if(rows.length !== 1){
    appendWidget('schedule', {items:all, only:rows.map(t => t.id), at:nowHM()}, {f:'active', p:1},
      rows.length ? `找到 ${rows.length} 個可能的預約，請在要取消的那一列按「取消」。` : '找不到符合的預約，下面是你所有還沒執行的預約。');
    return;
  }
  appendMsg('assistant', schedConfirmCardHTML('cancel', rows[0]));
}
// 取消／改時間的確認卡（兩步確認：這張卡是第一步，按鈕是第二步）
function schedConfirmCardHTML(op, t, newTs){
  const head = op === 'cancel' ? '取消預約' : '改預約時間';
  const body = op === 'cancel'
    ? `<div class="lead">要取消下面這個預約嗎？取消後時間到了不會提醒，也不會執行。</div>`
    : `<div class="lead">把下面這個預約改到新的時間：</div>`;
  const pick = op === 'reschedule' ? `<div class="sched-box">${schedPickerHTML('rs-' + t.id, newTs || t.run_at)}</div>` : '';
  return `<div class="module flow" data-sched-card="${t.id}">${flowHead(head, `<span class="meta">${schedPill(t.status)}</span>`)}<div class="body">${body}
    <dl class="sched-kv"><dt>預約編號</dt><dd>${esc('#' + t.id)}</dd><dt>原本的時間</dt><dd>${esc(fmtTW(t.run_at))}</dd><dt>內容</dt><dd>${esc(t.summary)}</dd></dl>${pick}
    <div class="err" data-role="error" role="alert"></div></div>
    <div class="foot"><button type="button" class="btn" data-act="sch-card-close">不用了</button>
      <button type="button" class="btn ${op === 'cancel' ? 'danger' : 'primary'}" data-act="${op === 'cancel' ? 'sch-card-cancel' : 'sch-card-resched'}" data-id="${t.id}">${op === 'cancel' ? '確定取消' : '改到這個時間'}</button></div></div>`;
}
function persistMsgCard(el, attr){
  const ch = currentChat(); if(!ch || !el) return;
  const key = `${attr}="${el.getAttribute(attr)}"`;
  const msg = ch.messages.find(m => m.role === 'assistant' && String(m.content || '').includes(key));
  if(msg){ msg.content = el.outerHTML; saveChats(); }
}
function closeSchedCard(card, text){
  card.querySelector('.foot').innerHTML = `<span class="dim">${esc(text)}</span>`;
  persistMsgCard(card, 'data-sched-card');
}

// ── 到期提示：打開網站時、以及網站開著時每 30 秒檢查一次 ──
function refreshDueBadge(){
  if(!loggedIn) return Promise.resolve([]);
  return getJSON('/api/schedule?status=due').then(d => {
    const n = (d.tasks || []).length, b = document.getElementById('nav-due-badge');
    if(b){ b.hidden = !n; b.textContent = n; }
    return d.tasks || [];
  }).catch(() => []);
}
async function pollDue(){
  if(!loggedIn || document.hidden) return;
  refreshDueBadge();
  let tasks = [];
  try{ tasks = (await getJSON('/api/schedule/due')).tasks || []; }catch(e){ return; }
  if(tasks.length) showDue(tasks);
}
function dueItemHTML(t, inChat){
  const del = t.kind === 'delete';
  return `<div class="due-item ${del ? 'del' : ''}" data-due-id="${t.id}">
    <div class="sum">${esc(t.summary)}</div><div class="meta2">約定的時間 ${esc(fmtTW(t.run_at))}，${countdown(t.run_at)}${t.kind === 'deploy' ? '。執行前會再審查一次' : ''}</div>
    ${del ? '<div class="warn">刪除後服務會停止，保存的資料也會一起刪除，沒辦法復原。</div>' : ''}
    <div class="actions" data-role="acts"><button type="button" class="btn ${del ? 'danger' : 'primary'}" data-act="due-run" data-id="${t.id}">確定執行</button>
      <button type="button" class="btn" data-act="due-cancel" data-id="${t.id}">取消這個預約</button>
      <button type="button" class="btn" data-act="due-snooze" data-id="${t.id}">稍後再說</button></div>
    <div class="out" data-role="out"></div></div>`;
}
function showDue(tasks){
  const ov = document.getElementById('due-overlay'), list = document.getElementById('due-list');
  if(!ov || !list) return;
  const have = new Set([...list.querySelectorAll('[data-due-id]')].map(x => +x.dataset.dueId));
  let added = 0;
  tasks.forEach(t => { if(!have.has(t.id)){ list.insertAdjacentHTML('beforeend', dueItemHTML(t)); added++; } });
  if(added || ov.hidden) ov.hidden = false;
  // 正在對話頁：對話裡也放一張同樣的卡
  if(currentPageName() === 'chat') tasks.forEach(t => {
    const ch = currentChat();
    if(ch && !ch.messages.some(m => String(m.content || '').includes(`data-due-card="${t.id}"`)))
      appendMsg('assistant', `<div class="module flow" data-due-card="${t.id}">${flowHead('預約時間到了', '<span class="meta">' + schedPill('due') + '</span>')}<div class="body">${dueItemHTML(t, true)}</div></div>`);
  });
}
function dueEls(id){ return [...document.querySelectorAll(`.due-item[data-due-id="${id}"]`)]; }
function dueSetOutcome(id, html, cls){
  dueEls(id).forEach(el => {
    const acts = el.querySelector('[data-role="acts"]'); if(acts) acts.remove();
    const out = el.querySelector('[data-role="out"]'); if(out){ out.className = 'out ' + (cls || ''); out.innerHTML = html; }
    const card = el.closest('[data-due-card]'); if(card) persistMsgCard(card, 'data-due-card');
  });
  const list = document.getElementById('due-list');
  if(list && !list.querySelector('[data-role="acts"]')){
    const foot = document.querySelector('#due-overlay .due-foot');
    if(foot) foot.innerHTML = '<button type="button" class="btn primary" data-act="due-close">關閉</button>';
  }
}
// 確定執行：先搶執行權（同一個預約不會被執行兩次），再呼叫跟手動操作完全相同的 API，最後回報結果
async function runScheduledTask(id){
  let t;
  try{ t = await schedOp(id, 'start'); }catch(e){ return {status:'failed', msg:e.message, task:e.task}; }
  let url, body;
  const a = t.args || {};
  if(t.kind === 'deploy'){ url = '/api/deploy'; body = {input:t.input || '', parsed:a.spec}; }
  else if(t.kind === 'scale'){ url = '/api/scale'; body = {name:a.name, replicas:a.replicas}; }
  else if(t.kind === 'update_image'){ url = '/api/update'; body = {name:a.name, image:a.image}; }
  else { url = '/api/delete'; body = {name:a.name}; }
  let status = 'failed', msg = '', pending = false;
  try{
    const r = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
    const x = await r.json().catch(() => ({}));
    if(t.kind === 'deploy'){
      if(x.error || x.rejected){
        status = (x.rejected || (x.review && x.review.decision === 'block')) ? 'blocked' : 'failed';
        const bs = ((x.review && x.review.blockers) || []).slice(0, 3).map(cleanIssue);
        msg = (status === 'blocked' ? '這次審查沒有通過，沒有部署' : (x.error || '部署失敗')) + (bs.length ? '：' + bs.join('；') : (x.error && status === 'blocked' ? '：' + x.error : ''));
      } else if(x.k8s_deploy && x.k8s_deploy.ok === false){ msg = '叢集實際部署失敗：' + x.k8s_deploy.message; }
      else { status = 'done'; pending = !!(x.k8s_deploy && x.k8s_deploy.pending_sync); msg = pending ? '已送到你的叢集，大約 30 到 60 秒內套用。' : '已部署，服務要幾十秒才會開始接流量。'; }
    } else {
      const ok = r.ok && x.success !== false && !x.error;
      status = ok ? 'done' : (r.status === 409 ? 'blocked' : 'failed');
      msg = ok ? (x.message || '已完成。') : (x.error || x.message || '操作失敗。');
    }
  }catch(e){ msg = '連線失敗：' + e; }
  let fin = null;
  try{ fin = await schedOp(id, 'done', {status, result:msg}); }catch(e){}
  return {status, msg, pending, task:fin || Object.assign({}, t, {status, result:msg})};
}
async function dueRun(id){
  dueEls(id).forEach(el => { el.querySelectorAll('[data-role="acts"] button').forEach(b => b.disabled = true);
    const out = el.querySelector('[data-role="out"]'); if(out) out.innerHTML = '<span class="dim">執行中…部署會先重新審查，大約需要十幾秒。</span>'; });
  const res = await runScheduledTask(id);
  const t = res.task || {};
  const ok = res.status === 'done';
  const link = ok && t.kind !== 'delete' ? ` <button type="button" class="btn btn-sm" data-act="open-dep" data-name="${esc((t.args || {}).name || ((t.args || {}).spec || {}).app_name || '')}">查看服務</button>` : '';
  dueSetOutcome(id, `${schedPill(res.status)} ${esc(res.msg)}${link}`, ok ? '' : 't-stop');
  // 部署成功而且人在對話頁：接著顯示部署進度，跟一般部署一樣追到運作中
  if(ok && t.kind === 'deploy' && currentPageName() === 'chat'){
    const spec = (t.args || {}).spec || {};
    const flow = newFlow('deploy', t.input || '', spec);
    flow.step = 'done'; flow.pendingSync = res.pending; flow.rollout = {state:'syncing'};
    appendMsg('assistant', renderFlowCard(flow));
    trackRollout(flow.id);
  }
  schedChanged(t);
}
document.addEventListener('click', async function(e){
  const t = e.target.closest('[data-act]');
  if(!t || t.disabled) return;
  const act = t.dataset.act, id = parseInt(t.dataset.id, 10);
  if(act === 'due-run'){ dueRun(id); return; }
  if(act === 'due-cancel'){
    try{ const task = await schedOp(id, 'cancel'); dueSetOutcome(id, schedPill('cancelled') + ' 已取消，不會執行。'); schedChanged(task); }
    catch(err){ dueSetOutcome(id, esc(err.message), 't-stop'); }
    return;
  }
  if(act === 'due-snooze'){
    try{ await schedOp(id, 'snooze'); dueSetOutcome(id, '<span class="dim">好，30 分鐘後或下次打開網站時再提醒你。</span>'); }catch(err){ dueSetOutcome(id, esc(err.message), 't-stop'); }
    return;
  }
  if(act === 'due-close'){
    const list = document.getElementById('due-list');
    const pending = [...list.querySelectorAll('[data-role="acts"]')].map(a => +a.closest('[data-due-id]').dataset.dueId);
    await Promise.all(pending.map(x => schedOp(x, 'snooze').catch(() => null)));
    document.getElementById('due-overlay').hidden = true;
    list.innerHTML = '';
    const foot = document.querySelector('#due-overlay .due-foot');
    if(foot) foot.innerHTML = '<button type="button" class="btn" data-act="due-close">全部稍後再說</button>';
    return;
  }
  if(act === 'due-open'){
    const tasks = (await getJSON('/api/schedule?status=due').catch(() => ({}))).tasks || [];
    if(tasks.length) showDue(tasks); return;
  }
  if(act === 'sch-cancel'){ schedCancel(id, t.dataset.sum); return; }
  if(act === 'sch-resched'){ schedReschedule(id, parseFloat(t.dataset.ts)); return; }
  if(act === 'sch-cal'){ calOpenDay(twDay(parseFloat(t.dataset.ts))); return; }
  if(act === 'sch-card-close'){ closeSchedCard(t.closest('[data-sched-card]'), '沒有變更。'); return; }
  if(act === 'sch-card-cancel' || act === 'sch-card-resched'){
    const card = t.closest('[data-sched-card]');
    const err = card.querySelector('[data-role="error"]');
    try{
      let task;
      if(act === 'sch-card-cancel') task = await schedOp(id, 'cancel');
      else {
        const ts = readPicker(card.querySelector('.sched-pick'));
        const bad = schedProblem(ts);
        if(bad){ err.style.display = 'block'; err.textContent = bad; return; }
        task = await schedOp(id, 'reschedule', {run_at:ts});
      }
      closeSchedCard(card, act === 'sch-card-cancel' ? `已取消預約 #${id}。` : `已改到 ${fmtTW(task.run_at)}。`);
      schedChanged(task);
    }catch(ex){ err.style.display = 'block'; err.textContent = ex.message; }
    return;
  }
  if(act === 'cal-prev' || act === 'cal-next'){ calShift(act === 'cal-prev' ? -1 : 1); return; }
  if(act === 'cal-today'){ calOpenDay(twDay(Date.now() / 1000)); return; }
  if(act === 'cal-day'){ calSelect(t.dataset.day); return; }
  if(act === 'cal-note-save'){ calSaveNote(t); return; }
});

// ── 行事曆頁 ──
let calMonth = null, calSel = null, calData = null;
function calOpenDay(day){
  calMonth = day.slice(0, 7); calSel = day;
  if(currentPageName() !== 'calendar') showPage('calendar'); else loadCalendarPage();
}
function calShift(n){
  const [y, m] = (calMonth || twDay(Date.now() / 1000).slice(0, 7)).split('-').map(Number);
  const d = new Date(Date.UTC(y, m - 1 + n, 1));
  calMonth = `${d.getUTCFullYear()}-${z2(d.getUTCMonth() + 1)}`;
  calSel = null;
  loadCalendarPage();
}
async function loadCalendarPage(quiet){
  const grid = document.getElementById('cal-grid');
  if(!grid) return;
  calMonth = calMonth || twDay(Date.now() / 1000).slice(0, 7);
  if(!quiet) grid.innerHTML = '<div class="skeleton" style="grid-column:1/-1;padding:18px"><i></i><i></i><i></i></div>';
  try{ calData = await getJSON('/api/calendar?month=' + calMonth); }
  catch(e){ grid.innerHTML = `<div style="grid-column:1/-1">${emptyBlock('行事曆讀取失敗。', e.message || String(e))}</div>`; return; }
  if(!calSel || calSel.slice(0, 7) !== calMonth) calSel = calData.today.slice(0, 7) === calMonth ? calData.today : calMonth + '-01';
  renderCalendar();
}
function calMarks(info){
  const tasks = info.tasks || [], evs = info.events || [];
  const live = tasks.filter(t => ['scheduled', 'due'].includes(t.status));
  const due = tasks.some(t => t.status === 'due');
  const bad = evs.some(e => e.kind === 'heal' || e.kind === 'review' || e.outcome === 'fail');
  const dep = evs.some(e => ['deploy', 'scale', 'update', 'delete', 'rollback'].includes(e.kind)) || tasks.some(t => t.status === 'done');
  return {live, due, bad, dep, note:!!info.note};
}
function renderCalendar(){
  const [y, m] = calMonth.split('-').map(Number);
  document.getElementById('cal-title').textContent = `${y} 年 ${m} 月`;
  const first = new Date(Date.UTC(y, m - 1, 1)).getUTCDay();
  const ndays = new Date(Date.UTC(y, m, 0)).getUTCDate();
  let html = '日一二三四五六'.split('').map(w => `<div class="wd">${w}</div>`).join('');
  for(let i = 0; i < first; i++) html += '<div class="cal-cell out" aria-hidden="true"></div>';
  for(let d = 1; d <= ndays; d++){
    const day = `${calMonth}-${z2(d)}`, info = calData.days[day] || {};
    const k = calMarks(info);
    const chips = k.live.slice(0, 2).map(t => `<span class="chip ${t.status === 'due' ? 'due' : ''}">${esc(fmtTW(t.run_at).slice(-5))} ${esc(t.summary)}</span>`).join('')
      + (k.live.length > 2 ? `<span class="chip">還有 ${k.live.length - 2} 個</span>` : '');
    html += `<button type="button" class="cal-cell ${day === calData.today ? 'today' : ''} ${day === calSel ? 'sel' : ''}" data-act="cal-day" data-day="${day}" aria-label="${m} 月 ${d} 日">
      <span class="n">${d}</span>${chips}<span class="mks">${k.due ? '<i class="mk due"></i>' : ''}${k.dep ? '<i class="mk dep"></i>' : ''}${k.bad ? '<i class="mk bad"></i>' : ''}${k.note ? '<i class="mk note"></i>' : ''}</span></button>`;
  }
  document.getElementById('cal-grid').innerHTML = html;
  renderCalDay();
}
function calSelect(day){
  calSel = day;
  document.querySelectorAll('.cal-cell.sel').forEach(c => c.classList.remove('sel'));
  const c = document.querySelector(`.cal-cell[data-day="${day}"]`); if(c) c.classList.add('sel');
  renderCalDay();
}
function renderCalDay(){
  const info = (calData && calData.days[calSel]) || {};
  const [y, m, d] = calSel.split('-').map(Number);
  const wd = new Date(Date.UTC(y, m - 1, d)).getUTCDay();
  document.getElementById('cal-day-title').textContent = `${m} 月 ${d} 日（週${WD_ZH[wd]}）`;
  const tasks = (info.tasks || []).slice().sort((a, b) => a.run_at - b.run_at);
  const evs = (info.events || []).slice().sort((a, b) => a.ts - b.ts);
  document.getElementById('cal-day-body').innerHTML = `
    <div><h3>預約</h3>${tasks.length ? tasks.map(t => `<div class="sch-row ${t.kind === 'delete' ? 'del' : ''}"><span class="when" style="min-width:auto">${esc(fmtTW(t.run_at).slice(-5))}</span>
      <span class="what"><b>${esc(t.summary)}</b>${t.result ? `<span class="res">${esc(t.result)}</span>` : ''}</span>${schedPill(t.status)}<span class="acts">${schedRowActions(t)}</span></div>`).join('')
      : '<div class="dim" style="font-size:13.5px">這天沒有預約。</div>'}</div>
    <div><h3>這天發生的事</h3>${evs.length ? evs.map(e => `<div class="cal-ev"><time>${esc(fmtTW(e.ts).slice(-5))}</time><span><span class="k ${e.kind === 'heal' || e.kind === 'review' || e.outcome === 'fail' ? 'bad' : ''}">${esc(e.label)}</span> ${esc(e.app || '')}${e.detail ? `<span class="dim">　${esc(e.detail)}</span>` : ''}</span></div>`).join('')
      : '<div class="dim" style="font-size:13.5px">平台這天沒有紀錄。</div>'}</div>
    <div><h3>備註</h3><textarea id="cal-note" maxlength="500" placeholder="記下這天要做的事，例如「下午跟組員對齊分時擴縮」">${esc(info.note || '')}</textarea>
      <div style="display:flex;gap:8px;align-items:center;margin-top:6px"><button type="button" class="btn btn-sm primary" data-act="cal-note-save">儲存備註</button><span class="dim" id="cal-note-msg" style="font-size:12.5px"></span></div></div>`;
}
async function calSaveNote(btn){
  const msg = document.getElementById('cal-note-msg');
  btn.disabled = true;
  try{
    const r = await fetch('/api/calendar/note', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({day:calSel, text:document.getElementById('cal-note').value})});
    const d = await r.json();
    if(!d.ok) throw new Error(d.error || '儲存失敗');
    await loadCalendarPage(true);
    const m2 = document.getElementById('cal-note-msg');
    if(m2) m2.textContent = '已儲存';
  }catch(e){ msg.textContent = e.message; btn.disabled = false; }
}

// 不太確定使用者要不要執行時的追問
function renderClarifyReasonHTML(intent){
  const reasons = intent.clarify_reasons || [];
  if(!reasons.length) return '';
  const ungrounded = intent.ungrounded_values || [];
  const proposed = Object.assign({}, intent.args || {}); delete proposed.guess;
  const confPct = Math.round((Number(intent.confidence) || 0) * 100);
  const labels = {
    low_confidence:`AI 對這個判斷的信心只有 ${confPct}%，低於 75% 的執行門檻`,
    missing_args:'少了執行這個動作需要的資訊',
    ungrounded_args:ungrounded.length ? `AI 猜的「${ungrounded.map(u => escHtml(String(u.value))).join('、')}」沒有出現在你的訊息裡，可能是憑空猜的` : 'AI 猜的內容沒有出現在你的訊息裡，可能是憑空猜的',
  };
  const lines = reasons.map(r => `<li>${labels[r] || escHtml(r)}</li>`).join('');
  const argsText = Object.keys(proposed).length ? escHtml(JSON.stringify(proposed)) : '';
  return `<details class="why"><summary>為什麼沒有直接執行</summary><div class="dim">AI 原本的判斷：${escHtml(intent.action || '')} ${argsText}</div><ul>${lines}</ul></details>`;
}
function startClarify(intent){
  const g = (intent.args && intent.args.guess) || intent.action;
  const label = {scale:'調整副本數', update_image:'更新映像檔', rollback:'回滾', delete:'刪除', healer_fix:'修復 Pod', healer_auto_fix:'全部自動修復'}[g] || g;
  appendMsg('assistant', `<div class="md"><p>我不太確定你是要「${escHtml(label)}」，還是只是在問問題。如果要執行，請講得具體一點，例如：</p>
    <ul><li><code>把 web-frontend 擴到 3 個</code></li><li><code>刪除 web-frontend</code></li><li><code>web-frontend 回滾</code></li></ul>${renderClarifyReasonHTML(intent)}</div>`);
}


// ══════════════════════════════════════════════════════════════════
//  頁面（2026-10-09 改版第三階段）：直接重用對話裡的模組，整站同一套外觀與互動
// ══════════════════════════════════════════════════════════════════
function currentPageName(){ return window.__page || 'chat'; }
async function mountWidget(hostId, kind, state, quiet){
  const host = document.getElementById(hostId);
  if(!host) return;
  const cur = host.querySelector('.zt-w');
  if(!cur) host.innerHTML = '<div class="module"><div class="skeleton" style="padding:18px"><i></i><i></i><i></i></div></div>';
  try{
    const data = await W_LOAD[kind]({});
    const st = cur ? (decState(cur.dataset.wState) || state || {}) : (state || {});
    host.classList.toggle('quiet', !!(cur && quiet));
    host.innerHTML = widgetHTML(kind, data, st, cur ? cur.dataset.wid : undefined);
  }catch(e){
    if(cur && quiet) return;   // 背景自動更新失敗就保留畫面上的舊資料，不要整塊變成錯誤
    host.innerHTML = `<div class="module">${emptyBlock('讀取失敗。', e.message || String(e))}<div style="padding:0 18px 18px;text-align:center"><button type="button" class="btn" onclick="showPage(currentPageName())">再試一次</button></div></div>`;
  }
}
function loadServicesPage(quiet){
  mountWidget('svc-deps', 'deployments', {p:1}, quiet);
  mountWidget('svc-autoscale', 'autoscale', {p:1}, quiet);   // 2026-10-21：分時擴縮與休眠，可以直接點按鈕設定
  mountWidget('svc-pods', 'pods', {f:'all', p:1}, quiet);
}
function loadGitopsPage(quiet){ mountWidget('gitops-w', 'gitops', {p:1}, quiet); }
function loadHealerPage(quiet){ mountWidget('heal-scan', 'healer', {p:1}, quiet); loadHealerBg(); if(!quiet) renderPushPanel(); }

// 手機通知設定區塊在 /static/push-client.js（手機監控版共用），這裡只呼叫 renderPushPanel()
// 舊程式（部署完成等地方）會呼叫 loadStats() 刷新數字：改成悄悄刷新目前所在的頁面
function loadStats(){
  const p = currentPageName();
  if(p === 'services') loadServicesPage(true);
  if(p === 'healer') loadHealerPage(true);
}
if(loggedIn){ setInterval(() => { if(currentPageName() === 'services' && !document.hidden) loadServicesPage(true); }, 15000); }

async function loadHealerBg(){
  const bg = document.getElementById('heal-bg'), proc = document.getElementById('heal-processing'), hist = document.getElementById('heal-history');
  if(!bg) return;
  try{
    const d = await getJSON('/api/healer/status');
    if(d.running){
      bg.className = 'status-strip ok';
      bg.innerHTML = `<span class="lamp-s"></span><span>Healer 正在背景監控，每 30 秒檢查一次${d.last_scan ? `，上次檢查 ${esc(new Date(d.last_scan).toLocaleTimeString('zh-TW', {hour12:false}))}` : ''}。</span>`;
    } else if(d.message){
      // K8s 有連線但背景執行緒停了：這是真正的異常，用紅色並照原樣講出原因
      bg.className = 'status-strip stop';
      bg.innerHTML = `<span class="lamp-s"></span><span>${esc(d.message)}</span>`;
    } else {
      bg.className = 'status-strip caution';
      bg.innerHTML = '<span class="lamp-s"></span><span>Kubernetes 沒有連線，Healer 暫停自動監控，目前只能手動掃描。</span>';
    }
    if(proc){
      proc.innerHTML = d.processing
        ? `<div class="status-strip busy"><span class="spinner"></span><span>正在修復 <span class="id">${esc(d.processing.pod || '')}</span>（${esc(d.processing.reason || '')}）。找原因要跑本地模型，大約 1 到 3 分鐘。</span></div>`
        : '';
    }
    const acts = d.recent_actions || [];
    if(hist){
      hist.innerHTML = acts.length
        ? `<ol class="timeline">${acts.map(a => `<li><time>${a.time ? esc(new Date(a.time).toLocaleString('zh-TW', {hour12:false, month:'numeric', day:'numeric', hour:'2-digit', minute:'2-digit'})) : ''}</time>
            <div class="what"><span class="app">${esc(a.pod || '')}</span>${a.reason ? esc(a.reason) + '，' : ''}原因：${esc(a.root_cause || a.error || '沒有找到')}，處理：${esc(a.action || '無')}
            ${a.llm_raw_guess ? `<div class="dim">AI 原本猜是「${esc(a.llm_raw_guess)}」，證據不足，沒有採用。</div>` : ''}</div>
            <div>${a.ok === false ? stateTag({cls:'fail', label:'沒修好'}) : a.needs_user ? stateTag({cls:'warn', label:'需要你處理'}) : stateTag({cls:'run', label:'已處理'})}</div></li>`).join('')}</ol>`
        : emptyBlock('還沒有自動修復過。', 'Pod 出狀況時，Healer 的處理過程會記在這裡。');
    }
  }catch(e){
    bg.className = 'status-strip stop';
    bg.innerHTML = `<span class="lamp-s"></span><span>讀不到 Healer 狀態：${esc(e.message || String(e))}</span>`;
  }
}

async function loadDashboardPage(quiet){
  mountWidget('dash-res', 'resources', {}, quiet);
  const cpuEl = document.getElementById('dash-cpu-trend'), memEl = document.getElementById('dash-mem-trend'), mon = document.getElementById('dash-mon');
  try{
    const [d, m] = await Promise.all([getJSON('/api/dashboard'), getJSON('/api/metrics').catch(() => null)]);
    if(!d.k8s_enabled || !d.cluster_capacity){
      const msg = '<div class="pod-chart-empty">Kubernetes 沒有連線，讀不到用量。請先確認 Docker Desktop 的 Kubernetes 已經啟動。</div>';
      cpuEl.innerHTML = msg; memEl.innerHTML = msg;
    } else if(d.customer_cluster){
      // 客戶叢集預設沒有指標收集元件，等再久都不會有趨勢，直接講原因
      const msg = '<div class="pod-chart-empty">你的叢集沒有安裝指標收集元件（metrics-server），所以沒有用量趨勢。上方的容量與已用量是依各服務設定的資源需求計算。</div>';
      cpuEl.innerHTML = msg; memEl.innerHTML = msg;
    } else {
      cpuEl.innerHTML = renderTrendChart(d.cpu_trend, 'var(--pass)', ' 核');
      memEl.innerHTML = renderTrendChart(d.mem_trend, 'var(--signal)', ' MiB');
    }
    const mm = (m && m.metrics) || {};
    const customer = (mm.source || '').indexOf('customer-cluster') === 0 || d.customer_cluster;
    let promText, promNote;
    if(customer){ promText = '不適用'; promNote = '數字來自你叢集裡的回報程式，每 15 秒更新'; }
    else if(!m || !m.connected){ promText = '讀不到'; promNote = (m && m.error) ? m.error : '監控資料暫時讀不到'; }
    else if(mm.prometheus_up){ promText = '運作中'; promNote = m.url ? `位址 ${m.url}` : ''; }
    else { promText = '連不上'; promNote = 'Pod 數字改成直接查 Kubernetes'; }
    mon.innerHTML = `<div><dt>Prometheus</dt><dd class="${promText === '運作中' ? '' : 't-warn'}">${esc(promText)}</dd><span class="dim">${esc(promNote)}</span></div>
      <div><dt>我執行中的 Pod</dt><dd>${mm.running_pods ?? '讀不到'}<span class="dim"> / ${mm.pod_count ?? '?'} 個</span></dd></div>
      <div><dt>整個叢集的 Pod</dt><dd>${customer ? '不適用' : (mm.cluster_pods ?? '讀不到')}</dd></div>`;
  }catch(e){
    const msg = `<div class="pod-chart-empty">讀取失敗：${esc(e.message || String(e))}</div>`;
    if(!quiet){ cpuEl.innerHTML = msg; memEl.innerHTML = msg; }
  }
}

// Pod 詳情抽屜加上 Healer 背景取樣的趨勢：重啟次數與健康狀態時間軸
async function openPodDrawer(name, diagnose){
  const seq = openDrawerShell('Pod 詳情', name);
  if(diagnose) fillDrawer(seq, '<div class="busy"><div class="spinner"></div><div>Healer 正在找原因，最久可能需要一到兩分鐘。</div></div>');
  try{
    const [d, h] = await Promise.all([
      getJSON('/api/pods/' + encodeURIComponent(name) + (diagnose ? '?diagnose=1' : '')),
      getJSON('/api/pods/' + encodeURIComponent(name) + '/history').catch(() => null),
    ]);
    if(!d.found || !(d.pods || []).length){ fillDrawer(seq, emptyBlock('找不到這個 Pod。', '它可能已經被重新建立或刪除，請重新查詢一次清單。')); return; }
    const history = (h && h.history) || [];
    const charts = `<div class="pd"><div><div class="sec-t">重啟次數趨勢</div>${renderRestartChart(history)}</div>
      <div><div class="sec-t">健康狀態時間軸（綠色是健康，紅色是有狀況）</div>${renderHealthTimeline(history)}</div></div>`;
    fillDrawer(seq, d.pods.slice(0, 3).map(p => podDetailHTML(p)).join('') + (d.pods.length === 1 ? charts : ''));
  }catch(e){ fillDrawer(seq, emptyBlock('讀取失敗。', e.message || String(e))); }
}

window.addEventListener('DOMContentLoaded', function(){
  // service worker：手機加到主畫面、離線說明頁、推播都靠它；註冊失敗不影響網站本身
  if('serviceWorker' in navigator && window.isSecureContext) navigator.serviceWorker.register('/sw.js').catch(() => {});
  if(loggedIn){
    applyLang();
    loadProjects();
    initChats();
    // 從通知點進來：/?page=healer&pod=<服務名稱>，直接打開那個服務的詳情
    const q = new URLSearchParams(location.search);
    const target = q.get('page');
    showPage(target && document.getElementById('page-' + (PAGE_ALIAS[target] || target)) ? target : 'chat');
    const pod = q.get('pod');
    if(pod && /^[a-z0-9][a-z0-9.\-]{0,252}$/.test(pod)) openPodDrawer(pod, false);
    if(q.has('page') || q.has('pod') || q.has('source')) history.replaceState(null, '', '/');
    // 預約到期提示：打開網站時檢查一次，之後每 30 秒；切回這個分頁時也馬上檢查
    setTimeout(pollDue, 1500);
    setInterval(pollDue, 30000);
    document.addEventListener('visibilitychange', () => { if(!document.hidden) pollDue(); });
  }
});
</script>
</body>
</html>
"""

# ── Routes ──────────────────────────────────────────────────
@app.route("/")
def index():
    if _is_phone() or session.get("readonly"):
        return _redirect_to_mobile()
    if "username" not in session:
        return render_template_string(HTML, logged_in=False, page='login', error=None, k8s=K8S_ENABLED, username='', google_login_available=_google_oauth_configured())
    return render_template_string(HTML, logged_in=True, page='app', error=None, k8s=K8S_ENABLED, username=session["username"], is_admin=session.get("is_admin", False))

@app.route("/auth/register", methods=["GET","POST"])
def register():
    if request.method == "GET":
        return render_template_string(HTML, logged_in=False, page='register', error=None, k8s=K8S_ENABLED, username='', google_login_available=_google_oauth_configured())
    username = request.form.get("username","").strip()
    password = request.form.get("password","")
    confirm  = request.form.get("confirm","")
    if not username or not password:
        return render_template_string(HTML, logged_in=False, page='register', error="請填寫帳號、密碼和確認密碼。", k8s=K8S_ENABLED, username='', google_login_available=_google_oauth_configured())
    if password != confirm:
        return render_template_string(HTML, logged_in=False, page='register', error="兩次輸入的密碼不一樣，請重新輸入。", k8s=K8S_ENABLED, username='', google_login_available=_google_oauth_configured())
    if username in USERS:
        return render_template_string(HTML, logged_in=False, page='register', error="這個帳號名稱已經有人用了，請換一個。", k8s=K8S_ENABLED, username='', google_login_available=_google_oauth_configured())
    if _namespace_taken(username):
        return render_template_string(
            HTML, logged_in=False, page='register',
            error="這個帳號名稱跟既有帳號太相似（系統會把它們視為同一個工作空間），請換一個名稱。",
            k8s=K8S_ENABLED, username='', google_login_available=_google_oauth_configured())
    if len(password) < 6:
        return render_template_string(HTML, logged_in=False, page='register', error="密碼至少要 6 個字元。", k8s=K8S_ENABLED, username='', google_login_available=_google_oauth_configured())
    USERS[username] = {"password_hash": hash_password(password), "created_at": datetime.now().isoformat(),
                       "violation_count": 0, "banned": False}
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "users.json"), "w", encoding="utf-8") as _uf:
        json.dump(USERS, _uf)
    _ensure_user_namespace(_user_namespace(username))
    session["username"] = username
    session["is_admin"] = False  # 密碼註冊帳號永遠不可能是管理員，見 _is_admin_user()
    return redirect("/")

def _login_page(error, monitor=False):
    """登入失敗時回到對應的登入頁：手機監控版回手機登入頁，其他回完整版登入頁。"""
    if monitor:
        return _mobile_login_page(error)
    return render_template_string(HTML, logged_in=False, page='login', error=error, k8s=K8S_ENABLED,
                                  username='', google_login_available=_google_oauth_configured())


@app.route("/auth/login", methods=["POST"])
def login():
    username = request.form.get("username","").strip()
    password = request.form.get("password","")
    # 從手機（或手機監控版的登入頁）登入：這個 session 只能查看
    monitor = request.form.get("mode") == "monitor" or _is_phone()
    stored_user = USERS.get(username)
    if isinstance(stored_user, dict) and stored_user.get("auth_provider") == "google" and "password_hash" not in stored_user:
        # 這個帳號是純 Google 登入建立的，沒有密碼可以比對——講清楚原因跟下一步
        # 動作，不要讓使用者以為自己打錯密碼一直重試（AGENT_RULES.md 新手友善原則）。
        return _login_page("這個帳號是用 Google 登入建立的，沒有密碼，請改按下方的「使用 Google 帳號登入」。", monitor)
    stored_hash = stored_user.get("password_hash", "") if isinstance(stored_user, dict) else stored_user
    if verify_password(password, stored_hash):
        if isinstance(stored_user, dict) and stored_user.get("banned"):
            # 帳號已被封鎖（通常是 Gemini 判定累積達到門檻自動觸發，見 _ban_user）。
            # 不能只回 401——要講清楚原因，不然使用者會以為是打錯密碼。
            return _login_page("這個帳號因為多次嘗試操縱系統，已經被封鎖。", monitor)
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
        session["is_admin"] = _is_admin_user(username)  # 密碼帳號恆為 False，保持呼叫一致避免未來改動漏掉
        session["readonly"] = monitor
        return redirect("/m" if monitor else "/")
    return _login_page("帳號或密碼不正確。", monitor)

@app.route("/auth/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect("/")


def _google_config_error_page():
    """GOOGLE_CLIENT_ID/SECRET 沒設定時，兩個 Google 路由共用的錯誤頁——不是
    404/500，講清楚原因（AGENT_RULES.md 新手友善原則：不能靜默失敗）。"""
    return render_template_string(
        HTML, logged_in=False, page='login',
        error="Google 登入尚未設定（需要在 .env 填入 GOOGLE_CLIENT_ID 和 GOOGLE_CLIENT_SECRET）。",
        k8s=K8S_ENABLED, username='', google_login_available=False)


@app.route("/auth/google/login")
def google_login():
    if not _google_oauth_configured():
        return _google_config_error_page()
    # CSRF 防護：標準 OAuth state 參數，callback 時核對，不符就拒絕。
    state = secrets.token_urlsafe(24)
    session["google_oauth_state"] = state
    session["google_login_mode"] = "monitor" if request.args.get("mode") == "monitor" else "full"
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": _external_url(url_for("google_callback")),
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "prompt": "select_account",
    }
    return redirect(f"{GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}")


@app.route("/auth/google/callback")
def google_callback():
    if not _google_oauth_configured():
        return _google_config_error_page()

    monitor = session.pop("google_login_mode", "full") == "monitor" or _is_phone()

    def _login_error(msg):
        return _login_page(msg, monitor)

    expected_state = session.pop("google_oauth_state", None)
    got_state = request.args.get("state")
    if not expected_state or not got_state or not secrets.compare_digest(expected_state, got_state):
        return _login_error(
            "Google 登入驗證失敗（連結可能已過期或遭到竄改），請重新登入一次。"
            )

    error_param = request.args.get("error")
    code = request.args.get("code")
    if error_param or not code:
        return _login_error(
            f"Google 登入已取消或失敗：{error_param or 'no code returned'}。"
            )

    try:
        token_resp = requests.post(GOOGLE_TOKEN_URL, data={
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": _external_url(url_for("google_callback")),
        }, timeout=10)
        token_resp.raise_for_status()
        access_token = token_resp.json().get("access_token")
        if not access_token:
            raise ValueError("回應裡沒有 access_token / no access_token in response")
        userinfo_resp = requests.get(
            GOOGLE_USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"}, timeout=10)
        userinfo_resp.raise_for_status()
        info = userinfo_resp.json()
    except Exception as e:
        return _login_error(f"連線 Google 時發生錯誤，請稍後再試。")

    google_sub = info.get("sub")
    email = info.get("email", "")
    email_verified = info.get("email_verified")
    if not google_sub or not email:
        return _login_error("無法從 Google 取得帳號資訊，請重新登入一次。 / Couldn't get account info from Google.")
    if email_verified is not True and str(email_verified).lower() != "true":
        # 沒驗證的 email 不能信任是本人擁有——不擋這關等於任何人都能宣稱擁有別人的 email。
        return _login_error(
            "這個 Google 帳號的 email 尚未驗證，無法用來登入。"
            )

    # 用 google_sub（Google 的穩定使用者 ID，不會變動）找既有帳號，不是用 email——
    # email 理論上可能變動，sub 才是真正穩定的識別碼。
    username = next(
        (u for u, d in USERS.items() if isinstance(d, dict) and d.get("google_sub") == google_sub),
        None,
    )
    if username is None:
        # 第一次用這個 Google 帳號登入：建立新帳號。刻意用 _derive_username_from_google()
        # 衍生一個獨立的 username，絕不去比對/借用既有帳號（見該函式的說明）。
        username = _derive_username_from_google(email)
        USERS[username] = {
            "auth_provider": "google",
            "google_sub": google_sub,
            "email": email,
            "created_at": datetime.now().isoformat(),
            "violation_count": 0,
            "banned": False,
        }
        _save_users()
        _ensure_user_namespace(_user_namespace(username))
    elif USERS[username].get("banned"):
        return _login_error("這個帳號因為多次嘗試操縱系統，已經被封鎖。")

    session["username"] = username
    session["is_admin"] = _is_admin_user(username)
    session["readonly"] = monitor
    return redirect("/m" if monitor else "/")


@app.before_request
def _enforce_ban():
    """集中檢查封鎖狀態，不用在 ~25 個路由裡各自加一次判斷。即使被封鎖的當下使用者
    還有一個活著的 session（登入時還沒被封鎖），這裡會在下一次任何請求時擋下並
    清掉 session——不需要額外做 session 撤銷機制，符合「不能靜默失敗」：不是裸的
    401/403，要講清楚原因。"""
    username = session.get("username")
    if username:
        # 每次請求都重新算一次 is_admin（而不是只在登入當下算），讓 ADMIN_EMAILS
        # 允許清單異動（例如之後加組員）在下一次請求就生效，不用使用者重新登入。
        session["is_admin"] = _is_admin_user(username)
    _u = USERS.get(username) if username else None
    if isinstance(_u, dict) and _u.get("banned"):
        session.clear()
        if request.path.startswith("/api/"):
            return jsonify({"error": "This account has been banned for repeated malicious behavior. / 此帳號因累積多次惡意行為已被封鎖。"}), 403
        return render_template_string(
            HTML, logged_in=False, page='login',
            error="這個帳號因為多次嘗試操縱系統，已經被封鎖。",
            k8s=K8S_ENABLED, username='', google_login_available=_google_oauth_configured())
    return None


# ══════════════════════════════════════════════════════════════════
# 客戶自帶叢集（GitOps 模式 C）：安裝腳本、安裝包、進度、回報器回報
# 2026-10-09 新增，設計見 cluster/onboarding.py、cluster/remote_state.py、gitops/tenant_repo.py
# ══════════════════════════════════════════════════════════════════
from core.config import TENANT_GITHUB_TOKEN, TENANT_GITHUB_ORG, DATA_DIR, TENANT_REPOS_DIR
from cluster.remote_state import ClusterRegistry, clean_report, ReportError, MAX_REPORT_BYTES
from cluster import onboarding as _onboarding

CLUSTERS = ClusterRegistry(os.path.join(DATA_DIR, "clusters.json"))


def _tenant_github_ready() -> bool:
    return bool(TENANT_GITHUB_TOKEN and TENANT_GITHUB_ORG)


def _tenant_repo_for(username: str):
    """這位客戶的 GitHub repo（沒有就建立）+ 本機工作副本。回傳 (TenantRepo, GitHubTenantHost, repo_info)。"""
    from gitops.tenant_repo import GitHubTenantHost, TenantRepo, tenant_slug
    host = GitHubTenantHost(TENANT_GITHUB_TOKEN, TENANT_GITHUB_ORG)
    slug = tenant_slug(username)
    info = host.ensure_repo(slug)
    repo = TenantRepo(username, info["https_url"], os.path.join(TENANT_REPOS_DIR, slug),
                      auth_header=host.auth_header(), branch=info["default_branch"])
    return repo, host, info


def _script_base_url() -> str:
    """給客戶電腦上的 PowerShell 用的網址。"""
    return _external_url("")


def _cluster_base_url() -> str:
    """給客戶叢集裡回報器用的網址。沒有對外網址（本機開發）時，Pod 裡的 localhost 指的是 Pod
    自己，要改成 Docker Desktop 提供的 host.docker.internal 才連得回這台電腦。"""
    base = _external_url("")
    if PUBLIC_BASE_URL:
        return base
    return re.sub(r"//(localhost|127\.0\.0\.1)(?=[:/]|$)", "//host.docker.internal", base)


_EXPIRED_SCRIPT = (
    "Write-Host '這個安裝指令已經失效或過期（有效 30 分鐘）。請回到 ZeroTouch 網頁，按「返回」再按「同意並繼續」"
    "取得新的指令。 / This install command has expired. Go back to the ZeroTouch page to get a new one.' "
    "-ForegroundColor Yellow\n")


@app.route("/onboard/<token>.ps1")
def onboard_script(token):
    username = CLUSTERS.user_for_onboard_token(token)
    if not username:
        # 這段會被 `irm | iex` 直接執行：回一段只會印出說明的腳本，不要回 HTML/404
        return _EXPIRED_SCRIPT, 200, {"Content-Type": "text/plain; charset=utf-8"}
    from gitops.tenant_repo import tenant_slug
    script = _onboarding.build_install_script(_script_base_url(), token, tenant_slug(username))
    return script, 200, {"Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-store"}


@app.route("/onboard/<token>/bundle.yaml")
def onboard_bundle(token):
    # 安裝包含唯讀 deploy key 與回報 token，只能下載一次（2026-10-09 背景安全審查：token replay）。
    # 安裝失敗要重跑時，網頁的「返回 → 同意並繼續」會給一個新的指令。
    username = CLUSTERS.claim_bundle(token)
    if not username:
        return ("# 這個安裝指令已經用過或已過期。請回到 ZeroTouch 網頁按「返回」再按「同意並繼續」取得新的指令。\n"
                "# This install command was already used or has expired. Get a new one from the ZeroTouch page.\n"), \
            410, {"Content-Type": "text/plain; charset=utf-8"}
    try:
        repo, host, info = _tenant_repo_for(username)
        repo.ensure_initialized()
        key = host.rotate_deploy_key(info["full_name"].split("/", 1)[1])
    except Exception as e:
        print(f"[onboard] bundle for {username} failed: {e}")
        return ("# 平台建立你的部署設定 repo 時失敗，請稍後再試；持續失敗請聯絡管理員。\n"
                "# The platform failed to prepare your config repo. Please retry later.\n"), 503, \
               {"Content-Type": "text/plain; charset=utf-8"}
    report_token = CLUSTERS.issue_report_token(username)  # 每次產生安裝包就輪換，舊回報器失效
    bundle = _onboarding.build_bundle(repo.namespace, username, _cluster_base_url(),
                                      info["ssh_url"], key, report_token)
    return bundle, 200, {"Content-Type": "application/yaml; charset=utf-8", "Cache-Control": "no-store"}


@app.route("/onboard/<token>/progress", methods=["POST"])
def onboard_progress(token):
    username = CLUSTERS.user_for_onboard_token(token)
    if not username:
        return jsonify({"error": "expired"}), 410
    data = request.get_json(silent=True) or {}
    CLUSTERS.record_progress(username, str(data.get("step", ""))[:20], bool(data.get("ok")),
                             str(data.get("detail", ""))[:500])
    return "", 204


@app.route("/agent/report", methods=["POST"])
def agent_report():
    auth = request.headers.get("Authorization", "")
    username = CLUSTERS.user_for_report_token(auth[7:] if auth.startswith("Bearer ") else "")
    if not username:
        return jsonify({"error": "invalid token"}), 401
    if USERS.get(username, {}).get("banned"):
        return jsonify({"error": "account disabled"}), 403
    if (request.content_length or 0) > MAX_REPORT_BYTES:
        return jsonify({"error": "report too large"}), 413
    # chunked 傳輸沒有 Content-Length，get_data() 會把整個 body 讀進記憶體——改成從串流最多讀
    # 上限 + 1 byte（2026-10-09 背景安全審查：resource exhaustion）
    raw = request.stream.read(MAX_REPORT_BYTES + 1)
    if len(raw) > MAX_REPORT_BYTES:
        return jsonify({"error": "report too large"}), 413
    try:
        report = clean_report(json.loads(raw.decode("utf-8")))
    except (ValueError, ReportError, UnicodeDecodeError):
        return jsonify({"error": "invalid report"}), 400
    CLUSTERS.store_report(username, report)
    _record_remote_history(username, report)
    try:
        from gitops.tenant_repo import tenant_slug
        _record_snapshot(tenant_slug(username), [
            {"phase": p.get("phase") or "Unknown",
             "restarts": sum(c.get("restart_count", 0) for c in p.get("containers") or []),
             "ready": sum(1 for c in p.get("containers") or [] if c.get("ready")),
             "total": len(p.get("containers") or [])} for p in report.get("pods") or []])
    except Exception:
        pass
    return "", 204


# ── 引導流程：同意書 → 執行指令 → 連線檢查（第 4 階段）────────────────
from core.config import CUSTOMER_CLUSTER_MODE
from cluster.remote_state import CONSENT_VERSION, ONBOARD_TOKEN_TTL, status_text
from cluster.pages import ONBOARD_HTML
import time as _time

# 還沒連線的帳號也可以使用的路徑（登入/登出、引導流程本身、回報器）
_ONBOARD_OPEN_PATHS = ("/onboarding", "/onboard/", "/auth/", "/agent/report", "/api/onboard/status",
                       # PWA 外殼：瀏覽器抓 manifest／圖示時不帶登入狀態，service worker 每頁都會註冊
                       "/manifest.webmanifest", "/sw.js", "/offline", "/icons/", "/favicon.ico", "/static/")


def _needs_onboarding(username) -> bool:
    """客戶自帶叢集模式下，非管理員帳號第一次連線成功前，只能使用引導流程。
    連線成功過的帳號之後就算叢集離線也可以進站（看自己的狀態），由主頁面顯示離線橫幅。"""
    return bool(CUSTOMER_CLUSTER_MODE and username and not session.get("is_admin")
                and not CLUSTERS.ever_connected(username))


@app.before_request
def _require_cluster_connection():
    if not _needs_onboarding(session.get("username")):
        return None
    if request.path.startswith(_ONBOARD_OPEN_PATHS) or request.path == "/m":
        return None
    if (_is_phone() or session.get("readonly")) and not request.path.startswith("/api/"):
        # 手機監控版只看狀態、不安裝任何東西，不走同意書；/m 會說明「請先用電腦完成連線」
        return redirect("/m")
    if request.path.startswith("/api/"):
        return jsonify({"error": "請先完成叢集連線（同意書 → 執行指令 → 連線檢查）。"
                                 , "onboarding": "/onboarding"}), 403
    return redirect("/onboarding")


@app.route("/onboarding")
def onboarding_page():
    username = session.get("username")
    if not username:
        return redirect("/")
    if not _needs_onboarding(username) and request.args.get("step") is None:
        return redirect("/")
    from gitops.tenant_repo import tenant_slug
    ns = tenant_slug(username)
    step = request.args.get("step", "")
    if not CLUSTERS.has_consent(username) or step == "consent":
        step = "consent"
    elif step != "check":
        step = "install"
    ctx = dict(step=step, username=username, namespace=ns, consent_version=CONSENT_VERSION,
               uninstall=_onboarding.uninstall_commands(ns), github_ready=_tenant_github_ready())
    if step == "install" and ctx["github_ready"]:
        # 同一個 token 在有效期內重新整理頁面要沿用（不然安裝進度會被清掉）；原文只存在使用者
        # 自己的 session（簽章 cookie），伺服器端只存雜湊
        tok = session.get("onboard_token")
        if not tok or CLUSTERS.user_for_onboard_token(tok) != username:
            tok = CLUSTERS.issue_onboard_token(username)
            session["onboard_token"] = tok
            session["onboard_token_exp"] = _time.time() + ONBOARD_TOKEN_TTL
        script_url = f"{_script_base_url()}/onboard/{tok}.ps1"
        ctx.update(command=f'irm "{script_url}" | iex', script_url=script_url,
                   expires_at=datetime.fromtimestamp(session.get("onboard_token_exp", _time.time())).strftime("%H:%M"))
    return render_template_string(ONBOARD_HTML, **ctx)


@app.route("/onboard/consent", methods=["POST"])
def onboard_consent():
    username = session.get("username")
    if not username:
        return redirect("/")
    CLUSTERS.record_consent(username)
    return redirect("/onboarding?step=install")


@app.route("/onboard/decline", methods=["POST"])
def onboard_decline():
    session.clear()
    return redirect("/")


@app.route("/onboard/back", methods=["POST"])
def onboard_back():
    username = session.get("username")
    if not username:
        return redirect("/")
    CLUSTERS.revoke_onboard_tokens(username)  # 返回 = 作廢目前的指令，下次產生新的
    session.pop("onboard_token", None)
    session.pop("onboard_token_exp", None)
    return redirect("/onboarding?step=consent")


@app.route("/api/onboard/status")
def api_onboard_status():
    username = session.get("username")
    if not username:
        return jsonify({"error": "Not authenticated"}), 401
    from gitops.tenant_repo import tenant_slug
    st = CLUSTERS.status(username)
    title, howto = status_text(st["code"], tenant_slug(username))
    prog = [{"step": p["step"], "ok": p["ok"]}
            for p in (CLUSTERS._data.get(username, {}).get("progress") or [])]
    return jsonify({**st, "title": title, "howto": howto, "progress": prog,
                    "mode": "customer" if CUSTOMER_CLUSTER_MODE else "local"})


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
_SCHED_CANCEL_RE = r"(?:取消|刪掉|刪除|不要).{0,20}預約|預約.{0,20}(?:取消|不要了|刪掉)"
_SCHED_RESCHED_RE = r"預約.{0,20}(?:改到|改成|改在|延到|延後到|提前到|挪到|換到)"
_SCHED_LIST_RE = (r"(?:我有|有沒有|有哪些|有什麼|列出|查看|顯示|看一下).{0,6}(?:預約|排程)|預約(?:清單|列表|了什麼|了哪些)|"
                  r"(?:今天|明天|後天|這週|這周|本週|下週|下周|這個月|下個月).{0,4}(?:要做什麼|有什麼事|有什麼預約|有哪些預約|的預約|行程)")
_SCHED_INTENTS = {"cancel_schedule", "reschedule", "list_schedule"}
_INTENT_RULES = [
    # 預約（2026-10-23）：放最前面，這幾句話裡常帶著時間和「部署、刪除」字眼，不能被當成要馬上執行
    ("cancel_schedule", re.compile(_SCHED_CANCEL_RE), {}),
    ("reschedule", re.compile(_SCHED_RESCHED_RE), {}),
    ("list_schedule", re.compile(_SCHED_LIST_RE), {}),
    # 分時擴縮與休眠（2026-10-11）：放最前面，「白天調成 3 個」不能被下面的擴縮規則搶走
    ("as_wake", re.compile(r"^(?:請|幫我)?\s*(?:喚醒|叫醒|wake\s*(?:up)?)\s*([a-z0-9][a-z0-9-]*)", re.I), {1: "name"}),
    ("as_wake", re.compile(r"(?:把\s*)?([a-z0-9][a-z0-9-]*)\s*(?:喚醒|叫醒)", re.I), {1: "name"}),
    ("as_exempt_on", re.compile(r"([a-z0-9][a-z0-9-]*)\s*(?:不要|不用|別|不准|不可以|不能|永遠不要?)\s*(?:讓它|讓他)?\s*(?:進入|自動)?\s*休眠", re.I), {1: "name"}),
    ("as_exempt_on", re.compile(r"(?:把\s*)?([a-z0-9][a-z0-9-]*)\s*(?:加入|加到|放進|放到)\s*例外", re.I), {1: "name"}),
    ("as_exempt_off", re.compile(r"([a-z0-9][a-z0-9-]*)\s*(?:可以|允許|恢復)\s*(?:自動)?\s*休眠", re.I), {1: "name"}),
    ("as_exempt_off", re.compile(r"(?:把\s*)?([a-z0-9][a-z0-9-]*)\s*(?:移出|拿出|從)\s*例外", re.I), {1: "name"}),
    ("as_cancel", re.compile(r"(?:取消|刪除|關掉|停止|移除)\s*([a-z0-9][a-z0-9-]*)\s*的?\s*(?:分時|排程|時段)", re.I), {1: "name"}),
    # 2026-10-23：從流量建議時段（「建議 shop-web 的時段」「幫 shop-web 建議時段」）
    ("as_suggest", re.compile(r"(?:建議|推薦)\s*([a-z0-9][a-z0-9-]*)\s*的?\s*(?:時段|排程|分時)", re.I), {1: "name"}),
    ("as_suggest", re.compile(r"([a-z0-9][a-z0-9-]*)\s*(?:的)?\s*(?:建議|推薦)\s*(?:的)?\s*(?:時段|排程|分時)", re.I), {1: "name"}),
    ("as_idle_days", re.compile(r"(?:閒置|沒人?用|沒有人用|沒有?流量)\s*(?:超過)?\s*(\d{1,2})\s*天.{0,10}休眠", re.I), {1: "days"}),
    ("as_show", re.compile(r"(?:分時|排程|休眠|例外)(?:擴縮)?\s*(?:的)?\s*(?:設定|狀態|清單|名單|列表|情況)|^(?:列出|查看|顯示|看看?).{0,6}(?:排程|休眠|例外)", re.I), {}),
    # 2026-10-21：排除「擴、縮、調、掛掉、壞、當機、錯誤」——「晚上 8 點把 web 擴到 3 個」是立即擴縮、「凌晨 3 點掛掉了 2 個」是在描述問題，不是設定時段
    ("as_schedule", re.compile(r"^(?!.*(?:部署|佈署|部屬|deploy|擴|縮|調|掛掉|掛了|壞|當機|當掉|錯誤|出錯)).*(?:白天|晚上|夜間|晚間|深夜|凌晨|上班時間|辦公時間|平日|工作日|週末|周末|假日|\d{1,2}\s*(?:點|[:：]\d{2})).{0,40}?\d{1,3}\s*(?:個|副本|pods?)", re.I), {}),
    ("report",re.compile(r"^(?!.*(?:部署|deploy|起一個|開一個)).*(?:分析報告|報告|報表|週報|周報|月報|\breport\b)", re.I), {}),
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
    # 單一 deployment 狀態——結尾的「狀態/細節/詳情/status」關鍵字一定要有，不能
    # 省略：2026-10-03 實測發現原本寫成 `(?:...)?`（整段可省略），導致「幫我部署
    # 2 個 nginx」這種部署指令，因為含有「部署」+空白+「2」（數字符合
    # `[a-zA-Z0-9]`），在完全沒有查詢狀態意圖的情況下也會配對成功，把「2」誤判成
    # 使用者要查的 deployment 名稱，排在下面真正的 deploy 規則之前被攔截，整句話
    # 變成查一個叫「2」的 deployment（查不到，顯示「找不到 deployment '2'」）。
    ("describe_deployment", re.compile(r"(?:deployment|部署)\s+([a-zA-Z0-9][\w.-]*)\s*(?:的?\s*(?:狀態|細節|詳情|status)|status)", re.I), {1: "name"}),
    ("describe_deployment", re.compile(r"([a-zA-Z0-9][\w.-]*)\s*(?:這個)?\s*(?:deployment|部署)\s*(?:的?\s*(?:狀態|細節|詳情))", re.I), {1: "name"}),
    # 單一 pod 細節
    ("describe_pod", re.compile(r"(?:查看|detail(?:s)?\s*(?:of)?|describe|檢視)\s*(?:pod\s+)?([a-zA-Z0-9][\w.-]*)", re.I), {1: "name"}),
    ("describe_pod", re.compile(r"([a-zA-Z0-9][\w.-]*)\s*(?:這個)?\s*pod\s*(?:的?\s*(?:細節|狀態|詳情|資訊))", re.I), {1: "name"}),
    ("describe_pod", re.compile(r"([a-zA-Z0-9][\w.-]*)\s*(?:的\s*(?:細節|詳情|詳細狀態))", re.I), {1: "name"}),
    ("delete", re.compile(r"^(delete|remove|刪除|del)\s+(\S+)", re.I), {2: "name"}),
    ("scale", re.compile(r"scale\s+(\S+)\s+to\s+(\d+)", re.I), {1: "name", 2: "replicas"}),
    ("scale", re.compile(r"把?\s*(\S+?)\s*(?:擴|縮|調).*?(\d+)", re.I), {1: "name", 2: "replicas"}),
    ("update_image", re.compile(r"update\s+(\S+)\s+to\s+(\S+)", re.I), {1: "name", 2: "image"}),
    # 「映像檔」的「檔」要一起吃掉，不然「映像檔換成 nginx:1.28」會把「檔換成」當成映像檔名稱（2026-10-23 全系統檢查）
    ("update_image", re.compile(r"把?\s*(\S+?)\s*的?\s*(?:image|映像檔?|鏡像檔?)\s*(?:換成|改成|改為|更新成|更新為|to)?\s*(\S+)", re.I), {1: "name", 2: "image"}),
    ("rollback", re.compile(r"rollback\s+(\S+)", re.I), {1: "name"}),
    ("rollback", re.compile(r"(\S+)\s*(?:回滾|回復|還原)", re.I), {1: "name"}),
    # deploy：前端有更完整的複合 regex，這裡只當 server 端安全網（args 交給 /api/deploy/parse 解析）
    ("deploy", re.compile(r"^((?:deploy|start|launch|run)\b|spin\s+up|部署|佈署|部屬)", re.I), {}),
    ("deploy", re.compile(r"(?:幫我|請).{0,6}(?:deploy|部署|佈署|部屬|跑|起).{0,8}(?:\d+\s*(?:個|pods?|副本)|nginx|redis|postgres|mysql|node|python|image|映像|鏡像)", re.I), {}),
]
_INTENT_INT_ARGS = {"replicas", "pods", "port", "days"}
_INTENT_NAME_ARGS = {"name", "pod_name"}
_PLAUSIBLE_RESOURCE_NAME_RE = re.compile(r"^[a-zA-Z0-9][\w.-]{0,62}$")
# image 跟 name 的格式不一樣（image 可以有 / 跟 :，例如 nginx:latest、
# registry.example.com/team/app:v1），但一樣可能被同一類垃圾字串污染，另外開
# 一條寬度略有不同的規則。
_PLAUSIBLE_IMAGE_RE = re.compile(r"^[a-zA-Z0-9][\w./:-]{0,127}$")
# 2026-10-07 大規模掃描（206 題）實測發現：_PLAUSIBLE_RESOURCE_NAME_RE 只檢查
# 字串「長得像不像合法識別碼」，不檢查語意——常見英文虛詞/泛稱詞剛好都是純小寫
# ASCII 字母，一樣會通過。實測到的真實案例：「delete everything in this
# cluster」被規則層當成要刪除一個叫 "everything" 的資源、信心 1.0 直接執行；
# 「remove the deployment named test-app」名稱被抓成 "the"，真正的目標
# test-app 完全沒抓到；「update the image of cache-server to redis:7」名稱
# "the"、映像 "of" 兩個欄位都是虛詞。這些詞在 K8s 裡幾乎不可能是真的資源名稱，
# 即使字面格式合法也要擋下來，讓請求 fall through 到有事實查核能力的
# classify_intent()，而不是用規則層的假 100% 信心直接放行垃圾值。
_IMPLAUSIBLE_NAME_WORDS = {
    "the", "a", "an", "of", "to", "for", "and", "or", "with", "from",
    "this", "that", "these", "those", "it", "its", "everything", "anything",
    "nothing", "something", "all", "any", "some", "none", "them", "they",
    "my", "your", "our", "his", "her", "their", "container", "service",
    "deployment", "pod", "image", "name", "cluster", "namespace", "resource", "app",
}


def _looks_like_resource_name(s: str) -> bool:
    """K8s 資源名稱只會是 ASCII 英數字/連字號/底線/點，長度上限 63。2026-10-06
    QA 掃描實測發現 `(\\S+)` 這種寬鬆比對會把整段中文句子都吃進去當名稱（例如
    「幫我把剛剛部署的東西回滾」的 name 被塞成「幫我把剛剛部署的東西」）——用這個
    檢查擋掉明顯不是識別碼的候選字串，讓請求 fall through 到後面有事實查核能力
    的 classify_intent()，而不是用規則層的假 100% 信心直接放行垃圾值。"""
    if s.lower() in _IMPLAUSIBLE_NAME_WORDS:
        return False
    return bool(_PLAUSIBLE_RESOURCE_NAME_RE.match(s))


def _looks_like_image_value(s: str) -> bool:
    """同上，但給 image 欄位用（格式容許 / 跟 :）。"""
    if s.lower() in _IMPLAUSIBLE_NAME_WORDS:
        return False
    return bool(_PLAUSIBLE_IMAGE_RE.match(s))


_QUESTION_MARKERS_RE = re.compile(
    r"是什麼|做什麼|是幹嘛|是幹什麼|怎麼用|有什麼用|用來做什麼|的意思|是不是|"
    r"[？?]$|\bwhat\s+(is|does|are)\b|\bhow\s+(do|does|is)\b",
    re.IGNORECASE,
)


def _rule_intent(message: str):
    """規則比對一句訊息 -> {action, args} 或 None。前端 matchClientRule 的 server 端鏡像。"""
    msg = (message or "").strip()
    if not msg:
        return None
    for action, rx, groupmap in _INTENT_RULES:
        m = rx.search(msg)
        if not m:
            continue
        # 2026-10-07 大規模掃描實測發現：gitops_log/healer_scan 這類「只看開頭
        # 關鍵字、不需要任何參數」的規則，會把「GitOps 是什麼」「Healer 這個功能
        # 是做什麼用的」這種明顯在問概念的句子，直接當成指令執行（真的去撈 git log
        # /真的去掃描 pod），答非所問。這類規則（groupmap 是空字典，代表沒有要求
        # 抽取任何具體目標）如果訊息本身帶有疑問句標記，視為沒命中，讓它 fall
        # through 到後面能正確分類成 qa 的 classify_intent()。
        # 預約相關的句子本來就常用問句（「我有哪些預約？」），而且只是查看或取消，不受這條限制
        if not groupmap and action not in _SCHED_INTENTS and _QUESTION_MARKERS_RE.search(msg):
            continue
        # 分時擴縮與休眠的設定指令：問句（「x 可以休眠嗎」）交給問答，不要直接改設定
        if action.startswith("as_") and action != "as_wake" and (_QUESTION_MARKERS_RE.search(msg) or "嗎" in msg):
            continue
        args = {}
        implausible = False
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
            elif key in _INTENT_NAME_ARGS and not _looks_like_resource_name(val):
                implausible = True
                break
            elif key == "image" and not _looks_like_image_value(val):
                implausible = True
                break
            args[key] = val
        if implausible:
            continue
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
    if not K8S_ENABLED and not _is_remote_ns(namespace):
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


_SHELL_WORDS = re.compile(r"\b(?:kubectl|docker|helm|kubeadm|minikube)\b", re.I)
# 系統內部標記（[現況]、[參考知識]）或 <pod-name> 這類佔位字，不該出現在給使用者的回覆裡
_INTERNAL_MARKS = re.compile(r"\[(?:現況|參考知識|問題)\]|<[a-z][a-z-]*-name>|<namespace>", re.I)


def _strip_shell_commands(message: str, reply: str) -> str:
    """零接觸：拿掉回覆裡叫使用者自己打的終端機指令（2026-10-09 展示時發現）。

    提示詞已經要求模型不要給指令，但 3B 模型沒有每次都聽話，知識庫文件裡也寫著
    「診斷指令：kubectl logs」，所以送出前再用確定性規則過濾：含指令的程式碼區塊、
    行內程式碼與整行都拿掉。使用者本來就在問這些工具時（例如「kubectl 是什麼」）不過濾。
    """
    if not reply or _SHELL_WORDS.search(message or ""):
        return reply
    out = re.sub(r"```[^\n]*\n.*?```", lambda m: "" if _SHELL_WORDS.search(m.group(0)) else m.group(0),
                 reply, flags=re.S)
    out = re.sub(r"`[^`\n]*`", lambda m: "" if _SHELL_WORDS.search(m.group(0)) else m.group(0), out)
    lines = [ln for ln in out.split("\n")
             if not _SHELL_WORDS.search(ln) and not _INTERNAL_MARKS.search(ln)]
    out = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    if not out:
        return ("這個問題通常要看服務的 log 才能判斷。請在對話裡說「服務名稱 有沒有壞」，"
                "或到「我的服務」點開有狀況的 Pod，看詳情裡的「最近的 log」。")
    return out


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
    if not K8S_ENABLED and not _is_remote_ns(namespace):
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
                f"請確認名稱是否正確，或到「我的服務」頁面查看實際清單。"
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
            "4. 只想修單一 Pod 就按該 Pod 的修復；想全部處理就按 「全部自動修復」。\n"
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
    if _is_remote_ns(_session_ns()):
        st = CLUSTERS.status(session["username"])
        title, howto = status_text(st["code"], _session_ns())
        return jsonify({"model_ready": ready, "model_loading": loading, "claude_api": claude_available(),
                        # 客戶叢集模式：平台自己的 Docker/K8s 跟客戶無關，回報的是客戶叢集的連線
                        "k8s": st["ok"], "docker_running": True, "docker_message": "",
                        "customer_cluster": {"ok": st["ok"], "code": st["code"], "title": title, "howto": howto,
                                             "last_report_at": st.get("last_report_at")}})
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
                "reply": "偵測到多次惡意誘導/操縱行為，此帳號已被封鎖並移除所有部署的資源。"
                         ,
                "banned": True,
            }), 403
        return jsonify({
            "reply": f"這則訊息被判定為嘗試誘導/操縱系統行為（{verdict.get('reason','')}），已記一次違規。"
                     f"累積達到門檻將自動封鎖帳號。"
                     ,
        })

    user_ns = _session_ns()
    # 問題若牽涉即時叢集狀態，先撈一份精簡快照當背景資料塞給模型（接地問答）。
    grounded = message
    snap = _cluster_snapshot_for(message, user_ns)
    if snap:
        grounded = f"[現況]\n{snap}\n\n[問題]\n{message}"
    # 專案裡的對話：把專案指示當成背景資料（2026-10-21；沒帶 project_id 就跟原本一樣）
    proj_note = _ws_project_instructions(data.get("project_id"))
    if proj_note:
        grounded = f"[專案指示（使用者為這個專案設定的偏好，僅供參考）]\n{proj_note}\n\n{grounded}"
    reply, sources = chat_llama(grounded, history)
    if reply.startswith("[Local Model unavailable]"):
        reply, sources = _fallback_chat_reply(message, history, user_ns)
    else:
        reply = _verify_grounded_reply(message, reply, user_ns)
        reply = _strip_shell_commands(message, reply)
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
    # docker-compose 匯入帶來的欄位（2026-10-10）：一律檢查格式與數量，不合格的直接丟掉
    env = parsed.get("env")
    if isinstance(env, dict) and env:
        clean_env = {}
        for k, v in list(env.items())[:50]:
            if isinstance(k, str) and re.match(r"^[A-Za-z_][A-Za-z0-9_.-]{0,100}$", k) and v is not None:
                clean_env[k] = str(v)[:4096]
        if clean_env:
            clean["env"] = clean_env
    try:
        cp = int(parsed.get("container_port") or 0)
        if 1 <= cp <= 65535:
            clean["container_port"] = cp
    except (TypeError, ValueError):
        pass
    if parsed.get("expose") is False:
        clean["expose"] = False
    if parsed.get("dns_alias") is True:
        clean["dns_alias"] = True
    for key in ("command", "args"):
        val = parsed.get(key)
        if isinstance(val, list) and val and all(isinstance(x, (str, int, float)) for x in val):
            clean[key] = [str(x)[:1000] for x in val[:50]]
    vols = []
    for v in (parsed.get("volumes") or [])[:5] if isinstance(parsed.get("volumes"), list) else []:
        if not isinstance(v, dict):
            continue
        name, mount = str(v.get("name") or ""), str(v.get("mount") or "")
        size = str(v.get("size") or "1Gi")
        if (re.match(r"^[a-z0-9]([-a-z0-9]{0,30}[a-z0-9])?$", name) and mount.startswith("/") and mount != "/"
                and ".." not in mount.split("/") and len(mount) <= 200 and v.get("kind") in ("pvc", "empty")
                and re.match(r"^[1-5]Gi$", size) and name not in {x["name"] for x in vols}):
            vols.append({"name": name, "mount": mount, "kind": v["kind"], "read_only": bool(v.get("read_only")),
                         "size": size if v["kind"] == "pvc" else None})
    if vols:
        clean["volumes"] = vols
    sk = parsed.get("secret_keys")
    if isinstance(sk, list) and clean.get("env"):
        keys = [str(k) for k in sk[:50] if str(k) in clean["env"]]
        if keys:
            clean["secret_keys"] = keys
    return clean


_NODE_CAPACITY_HINT_RE = re.compile(
    r"(node|節點).{0,15}(cpu|core|記憶體|memory|gi|mi)", re.IGNORECASE
)


def _mentions_node_capacity(text: str) -> bool:
    """使用者是否真的講了「每個 node 有多少 cpu/記憶體」這種話。"""
    return bool(_NODE_CAPACITY_HINT_RE.search(text or ""))


# 句子裡有沒有講到 port（「port 8080」「開 8080 埠」）。不認「:數字」，那多半是映像檔版本（postgres:16）
_PORT_MENTION_RE = re.compile(r"(?:port|埠|端口|連接埠)\s*[:：=]?\s*\d{2,5}|\d{2,5}\s*(?:port|埠|端口|連接埠)",
                              re.IGNORECASE)

# 情境線索：句子裡沒有明確人數或天數，但看得出是在描述使用情境，才值得多問 Gemini 一次
_SCENARIO_CUE_RE = re.compile(r"活動|社團|課程|比賽|期末|展示|報名|投票|班上|全班|營隊|workshop|event|class|party|hackathon",
                              re.IGNORECASE)


def _apply_scenario(user_input: str, parsed: dict):
    """情境解析部署（2026-10-09）：從使用者的描述推算副本數，附上推算理由與省錢估算。
    數字全部由 agents/scenario.py 的固定規則計算；Gemini 只補重要程度、要不要存資料，
    它給的數字要在原句裡找得到才採用。句子裡明講副本數時照使用者的，只提供建議。"""
    from agents.scenario import extract_by_rules, merge_model_attrs, has_scenario, plan, explicit_replicas
    rules = extract_by_rules(user_input)
    model = None
    if (has_scenario(rules) or _SCENARIO_CUE_RE.search(user_input)) and (
            rules.get("criticality") is None or rules.get("needs_persistence") is None):
        try:
            from core.gemini_client import gemini_extract_scenario
            model = gemini_extract_scenario(user_input)
        except Exception:
            model = None
    attrs = merge_model_attrs(user_input, rules, model)
    if not has_scenario(attrs):
        return None
    result = plan(attrs, parsed.get("image"), parsed.get("app_name"), explicit_replicas(user_input),
                  parsed.get("memory"), parsed.get("cpu"))
    if result["replicas_applied"]:
        parsed["pods"] = result["replicas"]
    return result


def _prepare_deploy(user_input: str, parsed_override: dict = None):
    parsed = _sanitize_deploy_payload(parsed_override) if parsed_override else ask_llama(user_input)
    if "error" in parsed:
        return None, None, None, ({"error": parsed["error"]}, 422)
    if not parsed_override:
        try:
            scenario = _apply_scenario(user_input, parsed)
        except Exception as e:
            print(f"[情境解析] 失敗，照原本的規格繼續：{e}")
            scenario = None
        if scenario:
            parsed["_scenario"] = scenario
        # 使用者沒講 port 時，常見服務用它自己的預設 port（redis 6379 等），不要一律給 80。
        # 2026-10-10：加上健康檢查後，port 錯了服務會一直停在未就緒；原本則是看起來正常、其實連不上。
        from agents.cost_agent import default_port
        _known = default_port(parsed.get("image"))
        if _known and int(parsed.get("port") or 80) == 80 and not _PORT_MENTION_RE.search(user_input):
            parsed["port"] = _known

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
        # 2026-10-21：多帶 missing，前端改成直接出部署單讓使用者補（error 字串維持原樣，其他呼叫端不受影響）
        return parsed, enriched, None, ({"error": f"Missing required field(s): {', '.join(missing)}", "parsed": enriched,
                                         "missing": missing}, 422)
    # 2026-10-03：app_name 格式不合法時（例如純數字開頭的「123」），過去完全沒在這裡
    # 擋下——Deployment 名稱本身符合 K8s 規則，但這個系統一定會另外建一個
    # "<app_name>-svc" 的 Service，Service 名稱用的是更嚴格的 DNS-1035 規則
    # （一定要字母開頭），導致一路通過三層審核、GitOps manifest 都寫進 git 了，
    # 最後真的呼叫 K8s API 才失敗，使用者看到一整段原始錯誤 JSON。這裡提早擋下，
    # 給清楚的原因跟修法，不用等到最後一步才知道（guardian/yaml_validator.py 的
    # _check_naming 也補了同一個檢查當防護網，避免其他路徑繞過這裡）。
    if not re.match(r"^[a-z]([-a-z0-9]*[a-z0-9])?$", str(parsed["app_name"])) or len(str(parsed["app_name"])) > 59:
        return parsed, enriched, None, ({
            "error": (
                f"App name「{parsed['app_name']}」格式不符合 K8s 命名規則：必須以小寫英文字母開頭，"
                f"只能包含小寫字母、數字、連字號（-），結尾不能是連字號，長度不超過 59 字元"
                f"（系統會自動建立對應的 Service，名稱規則比 Deployment 更嚴格，一定要字母開頭）。"
                f"例如「123」要改成「app-123」。"
            ),
            "parsed": enriched,
        }, 422)
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
                    f"請降低這次部署要求的 memory/cpu。"
                )
            elif K8S_ENABLED:
                # 使用者確認要的方向：單一 pod 本身放得下，不代表「這次部署的所有副本」+
                # 「叢集裡其他使用者已經部署的東西」疊加起來也放得下——一次要求很多副本，
                # 或剛好撞上其他人已經吃掉大部分資源的時機，一樣會讓多出來的 Pod 卡在
                # Pending 卻顯示部署成功。跟 _check_scale_risk 是同一種風險，差別是這個
                # Deployment 還沒建立，查不到它，要用這次請求的 cpu/mem × pods 直接算，
                # 不能用 read_namespaced_deployment 查舊值。
                try:
                    existing_cpu_mc, existing_mem_b = _sum_cluster_resource_requests()
                    pods_requested = parsed.get("pods", 1) or 1
                    agg_cpu_mc = existing_cpu_mc + pod_cpu_mc * pods_requested
                    agg_mem_b = existing_mem_b + pod_mem_b * pods_requested
                    agg_over_cpu = node_cpu_mc and agg_cpu_mc > node_cpu_mc
                    agg_over_mem = node_mem_b and agg_mem_b > node_mem_b
                    if agg_over_cpu or agg_over_mem:
                        review["decision"] = "block"
                        review["reason"] = "疊加叢集現有其他部署後，總資源需求會超出節點容量"
                        review.setdefault("blockers", []).append(
                            f"資源需求疊加超出叢集容量：這次要部署 {pods_requested} 個副本，加上叢集目前"
                            f"其他部署已經用掉的資源，套用後總計約需要 {agg_cpu_mc}m CPU / "
                            f"{agg_mem_b // (1024**2)}Mi 記憶體，但叢集最大節點只有 {node_cpu_mc}m CPU / "
                            f"{node_mem_b // (1024**2)}Mi 可用。多出來排不進去的 Pod 會卡在 Pending 狀態，"
                            f"畫面卻顯示部署成功，容易被忽略。請降低這次部署的副本數/資源需求，或先移除、"
                            f"縮小其他部署。"
                        )
                except Exception:
                    pass
    except Exception as e:
        review["node_estimate"] = None
        review.setdefault("warnings", []).append(f"node_estimate failed: {e}")
    # Pod 位置（2026-10-10 組員極限測試）：CPU、記憶體都夠，節點能放的 Pod 數量也可能不夠。
    # 只檢查平台自己的叢集；客戶叢集的帳號看的是平台叢集的數字，不準，跳過。
    try:
        ns = _session_ns() if has_request_context() and "username" in session else None
        if K8S_ENABLED and ns and not _is_remote_ns(ns):
            slots, used = _pod_slots()
            existing = 0
            try:
                existing = int(k8s_client.AppsV1Api().read_namespaced_deployment(parsed["app_name"], ns).spec.replicas or 0)
            except Exception:
                existing = 0
            extra = int(parsed.get("pods") or 1) - existing
            if slots and used is not None and extra > 0 and used + extra > slots:
                review["decision"] = "block"
                review["reason"] = "叢集的 Pod 位置不夠"
                review.setdefault("blockers", []).append("資源需求超出叢集容量：" + _pod_slot_message(extra, slots, used))
            # 保存空間配額：只算這次要新建的（同名的已經存在就沿用，不重複計算）
            new_pvcs = [v for v in (parsed.get("volumes") or []) if v.get("kind") == "pvc"]
            if new_pvcs:
                from gitops.manifest_writer import pvc_name
                from agents.cost_agent import _parse_memory_bytes
                existing = k8s_client.CoreV1Api().list_namespaced_persistent_volume_claim(ns).items
                names = {p.metadata.name for p in existing}
                used_gi = sum((_parse_memory_bytes((p.spec.resources.requests or {}).get("storage", "0")) or 0)
                              for p in existing) / (1024 ** 3)
                adding = [v for v in new_pvcs if pvc_name(parsed["app_name"], v["name"]) not in names]
                add_gi = sum(int(str(v.get("size") or "1Gi")[:-2]) for v in adding)
                if len(existing) + len(adding) > STORAGE_QUOTA_PVCS or used_gi + add_gi > STORAGE_QUOTA_GI:
                    review["decision"] = "block"
                    review["reason"] = "保存空間超過帳號上限"
                    review.setdefault("blockers", []).append(
                        f"保存空間超過上限：每個帳號最多 {STORAGE_QUOTA_PVCS} 個、合計 {STORAGE_QUOTA_GI}Gi。"
                        f"目前已經用了 {len(existing)} 個、{used_gi:g}Gi，這次要再新增 {len(adding)} 個、{add_gi}Gi。"
                        f"請先刪除不用的服務（它保存的資料會一起刪除），或減少資料空間。")
    except Exception:
        pass
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

    # 2026-09-17：使用者要求「部署前就能看到叢集還剩多少空間」，不要只在真的超量
    # 時才靠 _prepare_deploy 的 block 訊息告知——這裡把「節點總容量」「叢集現有
    # 其他部署已經用掉多少」「套用這次部署後還剩多少」都算出來，跟這次要部署的
    # 總量放在同一張表對照。查不到就回 None，前端要對應顯示「無法取得」而不是 0
    # 或留空白（避免看起來像「還有很多空間」的誤導）。
    cluster_capacity = cluster_used = cluster_remaining_after = None
    if K8S_ENABLED:
        try:
            cap = k8s_get_node_capacity()
            if cap:
                from agents.cost_agent import _parse_cpu_millicores, _parse_memory_bytes
                node_cpu_mc = _parse_cpu_millicores(cap.get("cpu")) or 0
                node_mem_b = _parse_memory_bytes(cap.get("memory")) or 0
                used_cpu_mc, used_mem_b = _sum_cluster_resource_requests()
                this_cpu_cores = est.get("cpu_cores") or 0
                this_mem_gib = est.get("memory_gib") or 0
                cluster_capacity = {
                    "cpu_cores": round(node_cpu_mc / 1000, 2),
                    "mem_gib": round(node_mem_b / (1024 ** 3), 2),
                }
                cluster_used = {
                    "cpu_cores": round(used_cpu_mc / 1000, 2),
                    "mem_gib": round(used_mem_b / (1024 ** 3), 2),
                }
                cluster_remaining_after = {
                    "cpu_cores": round(cluster_capacity["cpu_cores"] - cluster_used["cpu_cores"] - this_cpu_cores, 2),
                    "mem_gib": round(cluster_capacity["mem_gib"] - cluster_used["mem_gib"] - this_mem_gib, 2),
                }
        except Exception:
            cluster_capacity = cluster_used = cluster_remaining_after = None

    return {
        "replicas": parsed.get("pods"),
        "per_pod_cpu": parsed.get("cpu") or "未指定",
        "per_pod_mem": parsed.get("memory") or "未指定",
        "total_cpu_cores": est.get("cpu_cores"),
        "total_mem_gib": est.get("memory_gib"),
        "node_count": ne.get("node_count"),
        "node_source": ne.get("source"),
        "node_bound": node_bound,
        "monthly_usd": est.get("estimated_usd"),
        "cost_note": est.get("note"),
        "decision": review.get("decision"),
        "cluster_capacity": cluster_capacity,
        "cluster_used": cluster_used,
        "cluster_remaining_after": cluster_remaining_after,
    }


@app.route("/api/deploy/conflicts")
def api_deploy_conflicts():
    """第一步（規格確認）就先問使用者：名稱已存在／port 被佔用，是否仍要繼續。
    比 /api/deploy/parse 快很多（不跑 guardian/agents/dry-run），適合在使用者
    按「下一步」的當下就先問清楚，而不是等審查跑完才在一堆警告裡看到。"""
    if "username" not in session:
        return jsonify({"error": "Not authenticated"}), 401
    user_ns = _session_ns()
    app_name = (request.args.get("app_name") or "").strip()
    port_raw = (request.args.get("port") or "").strip()
    name_exists = bool(app_name) and app_name in {d["name"] for d in k8s_get_deployments(user_ns)}
    port_conflict = None
    port_conflict_other_account = False
    if port_raw:
        try:
            port = int(port_raw)
            for s in k8s_get_services(all_namespaces=True):
                if s["port"] != port:
                    continue
                if s.get("namespace") == user_ns:
                    if s["app"] == app_name:
                        continue  # 就是這次要更新的同一個服務，不算衝突
                    port_conflict = s["app"] or s["name"]
                else:
                    # 被別的帳號佔用：port 是 host 共用的，衝突一定要講，但不能把
                    # 對方的 app/service 名稱回傳出去——否則逐一試 port 就能列出其他
                    # 帳號部署了什麼（2026-10-08 安全審查，security_review.md 30 節）
                    port_conflict = "another account"
                    port_conflict_other_account = True
                break
        except ValueError:
            pass
    return jsonify({"name_exists": name_exists, "port_conflict": port_conflict,
                    "port_conflict_other_account": port_conflict_other_account})


@app.route("/api/import/parse", methods=["POST"])
@app.route("/api/compose/parse", methods=["POST"])
def api_compose_parse():
    """docker-compose 匯入第一步：只轉換成服務清單，不審查、不部署（2026-10-10）。
    審查與部署由前端對每個服務呼叫 /api/deploy/parse、/api/deploy，跟一句話部署走完全相同的把關。"""
    if "username" not in session:
        return jsonify({"error": "Not authenticated"}), 401
    from agents.compose_import import parse_compose, ComposeError, MAX_COMPOSE_BYTES
    from agents.cost_agent import default_port
    # 讀內容之前先看大小（JSON 包裝後會比原文大一些，留兩倍空間）
    if (request.content_length or 0) > MAX_COMPOSE_BYTES * 2:
        return jsonify({"error": f"檔案太大（上限 {MAX_COMPOSE_BYTES // 1000} KB）。"}), 413
    body = request.get_json(silent=True) or {}
    text = body.get("text") or ""
    if not isinstance(text, str):
        return jsonify({"error": "格式不對。"}), 400
    # 2026-10-21：同一個入口也接受 Kubernetes YAML（有 apiVersion、kind 就當成 K8s 設定檔）
    from agents.k8s_yaml_import import parse_k8s_yaml, looks_like_k8s
    try:
        if looks_like_k8s(text):
            result = parse_k8s_yaml(text, default_port)
        else:
            result = {**parse_compose(text, default_port), "format": "compose"}
    except ComposeError as e:
        return jsonify({"error": str(e)}), 400
    # 專案裡的對話上傳的檔案，原文存進專案的「檔案」分頁（沒帶 project_id 就跟原本一樣）
    if body.get("project_id"):
        result["project_file"] = _ws_save_upload(body.get("project_id"), body.get("filename"),
                                                 result.get("format") or "yaml", text)
    return jsonify(result)


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
        return jsonify({"error": "輸入內容太短，請描述清楚要部署什麼"}), 400
    parsed, enriched, review, error = _prepare_deploy(user_input, parsed_override)
    scenario = parsed.pop("_scenario", None) if isinstance(parsed, dict) else None
    if isinstance(enriched, dict):
        enriched.pop("_scenario", None)
    if error:
        payload, status = error
        return jsonify(payload), status
    return jsonify({
        "parsed": enriched, "raw": parsed, "review": review, "k8s": K8S_ENABLED,
        "resource_summary": _resource_summary(parsed, review),
        "scenario": scenario,
    })


@app.route("/api/deploy", methods=["POST"])
def api_deploy():
    if "username" not in session:
        return jsonify({"error": "Not authenticated"}), 401
    user_ns = _session_ns()
    _ensure_user_namespace(user_ns)
    data = request.get_json() or {}
    user_input = data.get("input", "").strip()
    parsed_override = data.get("parsed")
    if not user_input and parsed_override:
        user_input = _synth_input_from_spec(parsed_override)
    if not user_input or len(user_input) < 3:
        return jsonify({"error": "輸入內容太短，請描述清楚要部署什麼"}), 400

    parsed, enriched, review, error = _prepare_deploy(user_input, parsed_override)
    for d in (parsed, enriched):
        if isinstance(d, dict):
            d.pop("_scenario", None)
    if error:
        payload, status = error
        return jsonify(payload), status

    if _is_remote_ns(user_ns):
        # 客戶叢集：部署 = 對客戶 repo commit，由客戶叢集的 Flux 套用（不寫進平台自己的 repo，
        # 也不呼叫本機 k8s_deploy）
        blocked = _remote_write_guard(user_ns)
        if blocked:
            return jsonify({"error": blocked, "parsed": enriched, "review": review}), 409
        threading.Thread(target=save_gold_sample, args=(user_input, parsed), daemon=True).start()
        try:
            res = _tenant_repo(session["username"]).write_app(_with_resource_defaults(parsed))
            gitops_result = {"ok": True, "files": [f"apps/{res['app']}/"], "commit_sha": res.get("commit"),
                             "message": f"已寫入你的部署設定 repo（commit {str(res.get('commit') or '')[:7]}）"}
            k8s_result = {"ok": True, "pending_sync": True, "message": _SYNC_MSG}
        except Exception as e:
            gitops_result = {"ok": False, "files": [], "commit_sha": None, "message": str(e)}
            k8s_result = {"ok": False, "message": str(e)}
        if gitops_result.get("ok"):
            _ws_assign_after_deploy(data.get("project_id"), parsed.get("app_name"), user_ns)
        return jsonify({"parsed": enriched, "k8s": True, "review": review, "gitops": gitops_result,
                        "k8s_deploy": k8s_result, "resource_summary": _resource_summary(parsed, review),
                        "customer_cluster": True})

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
        ok, message = k8s_deploy(parsed["app_name"], parsed["image"], parsed["pods"], parsed.get("port", 80), parsed.get("memory"), parsed.get("cpu"), namespace=user_ns,
                                 env=parsed.get("env"), container_port_override=parsed.get("container_port"),
                                 expose=parsed.get("expose", True), dns_alias=parsed.get("dns_alias", False),
                                 command=parsed.get("command"), args=parsed.get("args"), volumes=parsed.get("volumes"),
                                 secret_keys=parsed.get("secret_keys"))
        k8s_deploy_result = {"ok": ok, "message": message}
        if not ok:
            review.setdefault("warnings", []).append(f"K8s 部署失敗：{message}")
        else:
            _ws_assign_after_deploy(data.get("project_id"), parsed.get("app_name"), user_ns)

    return jsonify({"parsed": enriched, "k8s": K8S_ENABLED, "review": review, "gitops": gitops_result,
                    "k8s_deploy": k8s_deploy_result, "resource_summary": _resource_summary(parsed, review)})

@app.route("/api/pods")
def api_pods():
    if "username" not in session:
        return jsonify({"error": "Not authenticated"}), 401
    return jsonify({"pods": k8s_get_pods(namespace=_session_ns())})

@app.route("/api/deployments")
def api_deployments():
    if "username" not in session:
        return jsonify({"error": "Not authenticated"}), 401
    return jsonify({"deployments": k8s_get_deployments(_session_ns())})


@app.route("/api/pods/<name>/history")
def api_pod_history(name):
    """給 Healer 視覺化的趨勢圖用：這個 Pod 過去累積的輕量快照（重啟次數/健康狀態
    隨時間變化）。只回傳使用者自己 namespace 的資料。"""
    if "username" not in session:
        return jsonify({"error": "Not authenticated"}), 401
    user_ns = _session_ns()
    return jsonify({"history": _get_pod_history(user_ns, name)})


def _pod_log_tail(pod_name: str, namespace: str, container: str, lines: int = 40) -> str:
    """有狀況的 Pod 最近的 log，給畫面直接顯示。先抓崩潰前那一次（最能看出為什麼當掉），
    剛被重建、還沒有上一次的 Pod 就改抓目前的；都抓不到回空字串。
    2026-10-09：healer 的 _get_pod_logs 失敗時會回傳整段 HTTP 錯誤原文，不能直接給使用者看。"""
    if not K8S_ENABLED:
        return ""
    v1 = k8s_client.CoreV1Api()
    for previous in (True, False):
        try:
            text = v1.read_namespaced_pod_log(name=pod_name, namespace=namespace, container=container or None,
                                              tail_lines=lines, previous=previous, _request_timeout=5)
            # 舊容器被清掉時 API 不丟例外，而是回「unable to retrieve container logs…」，不能當成日誌
            # （2026-10-21 全系統檢查：Healer 那份 10/10 修好了，這份漏掉，Pod 詳情的診斷又變成證據不足）
            from healer.pod_watcher import _LOG_UNAVAILABLE_RE
            if text and text.strip() and not _LOG_UNAVAILABLE_RE.search(text[:300]):
                return text[-4000:]
        except Exception:
            continue
    return ""


@app.route("/api/pods/<name>")
def api_pod_detail(name):
    """單一 pod（或某 app 的一組 pod）的詳細狀態 + 健康判定。
    ?diagnose=1 且該 pod 不健康時，附上 healer 的 LLM 根因診斷。"""
    if "username" not in session:
        return jsonify({"error": "Not authenticated"}), 401
    user_ns = _session_ns()
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
        # 2026-10-09：有狀況的 Pod 直接附上最近的 log，客戶不用自己打 kubectl logs（零接觸）。
        # 客戶叢集的回報器已經帶了 log；本機叢集這裡現抓（取崩潰前那一次，抓不到就是空字串）。
        if not pd["healthy"] and not pd.get("logs") and not _is_remote_ns(user_ns):
            first = (pd.get("containers") or [{}])[0].get("name", "")
            pd["logs"] = _pod_log_tail(pd["name"], user_ns, first, 40)
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
                "logs": pd.get("logs") or (bad or {}).get("message", ""),
                "events": [{"reason": e["reason"], "message": e["message"]} for e in pd["events"]],
            }
            try:
                # 2026-10-07 修 bug：原本直接打 diagnose_with_llm()，完全繞過
                # healer/diagnose.py 的規則層——背景自癒迴圈跟「Fix」按鈕都有
                # _has_concrete_signal() 幻覈防護，使用者手動點「Diagnose」看到的
                # 結果卻完全沒有，可能顯示幻覈出來的根因。改走同一個函式，順便
                # 自動帶上 llm_raw_guess（近失事件可見化）。
                from healer.diagnose import diagnose_issue
                pd["diagnosis"] = diagnose_issue(ctx)
            except Exception as e:
                pd["diagnosis"] = None
    return jsonify({"pods": pods, "found": True})


@app.route("/api/deployments/<name>")
def api_deployment_detail(name):
    if "username" not in session:
        return jsonify({"error": "Not authenticated"}), 401
    d = k8s_describe_deployment(name, _session_ns())
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
    ok, msg = k8s_delete_deployment(name, _session_ns())
    return jsonify({"success": ok, "message": msg, "error": None if ok else msg})



def _scale_check(name, replicas, ns, allow_zero=False):
    """擴縮前的檢查：副本上限＋叢集容量。使用者手動擴縮（/api/scale）與分時擴縮、休眠、喚醒共用同一套
    （2026-10-23，照接手說明「自動擴縮要經過 /api/scale 的檢查」）。只有平台內部的休眠可以到 0（allow_zero=True）。
    回傳 (通過與否, 錯誤訊息, HTTP 狀態碼)。"""
    from guardian.yaml_validator import max_replicas
    low = 0 if allow_zero else 1
    if not low <= replicas <= max_replicas():
        return False, f"副本數要在 {low} 到 {max_replicas()} 之間", 400
    if replicas > 0:
        blocked, risk_msg = _check_scale_risk(name, replicas, ns)
        if blocked:
            return False, risk_msg, 409
    return True, None, 200


@app.route("/api/scale", methods=["POST"])
def api_scale():
    if "username" not in session:
        return jsonify({"error": "Not authenticated"}), 401
    data = request.get_json() or {}
    if _is_remote_ns(_session_ns()):
        return _remote_scale(data)
    if not K8S_ENABLED:
        return jsonify({"success": False, "error": "K8s 未連線"}), 503
    name = data.get("name", "").strip()
    try:
        replicas = int(data.get("replicas", 1))
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "replicas must be an integer"}), 400
    if not name:
        return jsonify({"success": False, "error": "Name required"}), 400
    user_ns = _session_ns()
    ok, err, status = _scale_check(name, replicas, user_ns)
    if not ok:
        payload = {"success": False, "error": err}
        if status == 409:
            payload["blocked_reason"] = "resource_exceeded"
        return jsonify(payload), status
    try:
        api = k8s_client.AppsV1Api()
        body = {"spec": {"replicas": replicas}}
        api.patch_namespaced_deployment_scale(name=name, namespace=user_ns, body=body)
        return jsonify({"success": True, "message": f"已把 {name} 的副本調成 {replicas} 個。"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/update", methods=["POST"])
def api_update():
    if "username" not in session:
        return jsonify({"error": "Not authenticated"}), 401
    user_ns = _session_ns()
    data = request.get_json() or {}
    name = data.get("name", "").strip()
    image = data.get("image", "").strip()
    # 換映像檔不經過 _prepare_deploy 的完整 guardian 驗證，映像規則（禁止清單、
    # 挖礦程式關鍵字）要在這裡單獨檢查，本機與客戶叢集兩條路都適用。
    if image:
        from guardian.yaml_validator import check_image
        image_errors = check_image(image)
        if image_errors:
            return jsonify({"success": False, "error": "；".join(image_errors)}), 400
    if _is_remote_ns(user_ns):
        if not name or not image:
            return jsonify({"success": False, "error": "name and image are required"}), 400
        return _remote_git_op(user_ns, lambda r: r.set_image(name, image),
                              lambda res: f"已把 {name} 的映像檔改成 {image}（原本 {res.get('previous_image')}）。")
    if not K8S_ENABLED:
        return jsonify({"success": False, "error": "K8s 未連線"}), 503
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
        annotations["zerotouch.k8s/updated-at"] = _utc_now_iso()
        dep.spec.template.metadata.annotations = annotations
        dep.spec.template.spec.containers[0].image = image
        api.patch_namespaced_deployment(name, user_ns, dep)
        return jsonify({"success": True, "message": f"已把 {name} 的映像檔換成 {image}。", "previous_image": old_image})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/rollback", methods=["POST"])
def api_rollback():
    if "username" not in session:
        return jsonify({"error": "Not authenticated"}), 401
    user_ns = _session_ns()
    data = request.get_json() or {}
    app_name = (data.get("app_name") or data.get("name") or "").strip()
    if not app_name:
        return jsonify({"success": False, "error": "app_name required"}), 400
    # 名稱會被拼進 manifests/<namespace>/<app_name> 路徑並交給 kubectl，必須是合法
    # K8s 名稱，擋掉 `../user-別人/app` 這類路徑穿越（2026-10-08 安全審查，
    # docs/security_review.md 30 節）。gitops/rollback.py 內部也會再檢查一次。
    from gitops.rollback import is_valid_k8s_name
    if not is_valid_k8s_name(app_name):
        return jsonify({"success": False,
                        "error": "應用程式名稱不合法，只能用小寫英數字與連字號"
                                 }), 400
    if _is_remote_ns(user_ns):
        return _remote_git_op(user_ns, lambda r: r.revert_app(app_name),
                              lambda res: f"已把 {app_name} 回滾到上一版設定（{res.get('rolled_back_to')}）。")
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
        annotations["zerotouch.k8s/rolled-back-at"] = _utc_now_iso()
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
        # K8s client 的例外字串會夾帶整串 HTTP header（Audit-Id、flowschema UID 等），
        # 新手看不懂也不該外流；找不到這個 Deployment 給明確下一步（2026-10-08 端到端
        # 實測發現）
        if getattr(e, "status", None) == 404:
            return jsonify({
                "success": False,
                "error": f"找不到名為「{app_name}」的部署，請到「我的服務」頁面確認名稱"
                         ,
                "details": result.get("details", {}),
            }), 404
        print(f"[rollback] fallback failed for {user_ns}/{app_name}: {e}")
        return jsonify({"success": False,
                        "error": "回滾失敗，請稍後再試；若持續發生請到「自動修復」頁面查看狀態"
                                 ,
                        "details": result.get("details", {})}), 500

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
    user_ns = _session_ns()
    if _is_remote_ns(user_ns):
        try:
            rows = _tenant_repo(session["username"]).history(limit=20)
        except Exception as e:
            return jsonify({"commits": [], "error": str(e)})
        return jsonify({"commits": [{"hash": r["commit"], "message": r["message"], "time": r["time"][:16].replace("T", " "),
                                     "app": _app_from_commit(r["message"])}
                                    for r in rows], "customer_cluster": True})
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
    user_ns = _session_ns()
    if _is_remote_ns(user_ns):
        return jsonify({"issues": _rv.unhealthy_issues(_remote_report(user_ns), user_ns), "customer_cluster": True})
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
        return {"ok": True, "action": "none", "message": f"Pod {pod_name} 目前狀態正常，無需修復", "root_cause": None}
    logs = _get_pod_logs(issue["pod_name"], issue["namespace"], issue["container"])
    events = _get_pod_events(issue["pod_name"], issue["namespace"])
    context = {**issue, "logs": logs, "events": events}
    from healer.diagnose import diagnose_issue
    diagnosis = diagnose_issue(context)
    from healer.remediate import remediate
    result = remediate(issue, diagnosis)
    result["root_cause"] = diagnosis.get("root_cause")
    result["severity"] = diagnosis.get("severity")
    result["llm_raw_guess"] = diagnosis.get("llm_raw_guess")  # 近失事件可見化，見 healer/diagnose.py
    return result


# 背景自動監控狀態（記憶體內，重啟 web_demo.py 會清空；不用資料庫，量小且非關鍵資料）。
# 2026-09-14 修正：之前 "running" 只在啟動那一刻設一次 True，之後永遠不會再檢查，
# 如果背景執行緒中途死掉，這裡會一直謊報「運行中」——直接違反 AGENT_RULES.md 的
# 核心原則「判斷即時、不用舊值唬弄」。現在改成即時查真正的執行緒物件是否還活著，
# 並且額外比對 last_scan 有沒有久到不正常（執行緒卡住/掛住不會被 is_alive() 抓到，
# 因為卡住的執行緒技術上還「活著」，只是沒有在前進，只能靠比對時間戳才能發現）。
def _utc_now_iso() -> str:
    """統一的 UTC 時間字串產生器。2026-10-03 實測發現：這個檔案原本到處直接呼叫
    `datetime.utcnow().isoformat()`，產生的字串沒有時區標記（例如
    `2026-10-03T14:43:18`）。前端 JS 的 `new Date(...)` 看到沒有時區標記的
    ISO 字串，會把它當成「當地時間」解析，不是 UTC——這台機器在 UTC+8，畫面上
    顯示的 Healer 時間因此比實際晚了整整 8 小時。結尾加 "Z" 讓 JS 正確辨識成
    UTC 再轉當地時間顯示。"""
    return datetime.utcnow().isoformat() + "Z"


def _parse_utc_iso(s: str) -> datetime:
    """`_utc_now_iso()` 的反向轉換。Python 3.9 的 `datetime.fromisoformat()`
    不吃結尾 "Z"（Python 3.11 才支援），呼叫前先去掉；舊資料／沒有 "Z" 的字串
    也要能正常解析，所以用 `endswith` 判斷而不是直接硬切。"""
    return datetime.fromisoformat(s[:-1] if s.endswith("Z") else s)


_healer_bg_state = {
    "last_scan": None, "recent_actions": [], "started_at": None, "interval": 30,
    # 目前正在處理哪個 pod（不是「掃描完一輪」才更新，而是「開始處理這一個」就
    # 更新）——給前端畫「正在修復中」的轉圈動畫用，也用來跟 liveness 判斷區分
    # 「卡住」跟「正在忙，只是忙很久」（根因診斷固定會打一次本地 LLM，實測一次
    # 2-3 分鐘跑不掉，不能因為處理時間長就誤判成卡住）。
    "processing": None,
}
_healer_bg_lock = threading.Lock()
_healer_bg_thread = None  # 全域保存 Thread 物件本身，供 /api/healer/status 查 is_alive()
_HEALER_BG_MAX_HISTORY = 50

# 2026-09-15：Pod 時間序列記錄（Healer 視覺化的趨勢圖用），跟 _healer_bg_state
# 是不同的東西——那個只記「有問題、被修復過」的 Pod，這裡記「每一個 Pod 每一刻的
# 輕量快照」，不管有沒有問題都記，才能畫出「這段時間大部分健康還是一直在壞」的圖。
#
# 2026-10-03 改成用「推斷出來的 Deployment／app 名稱」當 key，不是用 Pod 名稱
# 本身——原本的設計（見下面曾經的註解）是用 Pod 名稱當 key，但 Healer 真的把壞
# Pod 修好的手段常常是「讓 K8s 重建一個新 Pod」（改 memory limit、rollout undo
# 都會觸發新的 ReplicaSet），新 Pod 一定會有新名稱，歷史因此每次都從空的重新
# 開始，使用者點進去只看得到「現在這個 Pod」的一小段平直綠線，完全看不出「這個
# 服務曾經壞過、後來被修好」的過程——這正是這個功能的核心賣點，卻被資料模型本身
# 擋住了。改成用 `_app_key_for_pod()` 推斷出的穩定名稱（這個專案建的 Deployment
# 一律 `metadata.name` = app_name = `labels.app`，格式固定是
# `<app_name>-<replicaset hash>-<pod hash>`），同一個服務不管重建幾次 Pod，
# 歷史都累積在同一個 key 底下，時間軸才看得出完整的壞掉→修復過程。
_pod_history = {}  # key: f"{namespace}/{app_name}" -> list[sample dict]
_pod_history_lock = threading.Lock()
_POD_HISTORY_MAX_SAMPLES = 120  # 30 秒一次，120 筆 ≈ 1 小時


def _app_key_for_pod_name(pod_name: str) -> str:
    """從 Pod 名稱推斷出穩定的 app／Deployment 名稱，拿掉最後兩段隨機後綴
    （ReplicaSet hash + Pod hash）。跟 healer/remediate.py 的
    `_infer_deployment_name()` 是同一套邏輯，這裡獨立一份是因為那邊回傳
    `Optional[str]`（找不到回 None），這裡需要一個保證有值的版本（找不到就
    退回原始 Pod 名稱，至少還是能用，只是退化回舊行為，不是整個失敗）。"""
    parts = pod_name.rsplit("-", 2)
    if len(parts) >= 2:
        return parts[0]
    return pod_name


def _pod_light_snapshot(pod) -> dict:
    """給時間序列用的輕量快照，不像 _pod_detail() 那麼重（不查 events，那個較貴）。"""
    statuses = pod.status.container_statuses or []
    return {
        "t": _utc_now_iso(),
        "phase": (pod.status.phase if pod.status else "") or "Unknown",
        "restarts": sum(cs.restart_count for cs in statuses),
        "ready": sum(1 for cs in statuses if cs.ready),
        "total": len(statuses),
    }


def _get_pod_history(namespace: str, name: str) -> list:
    """`name` 是使用者點進去那個「現在這個 Pod」的名稱，但儲存時是用推斷出來的
    app 名稱當 key（見上面 `_app_key_for_pod_name()` 的說明）——查的時候要做
    同樣的推斷，才能拿到「這個服務」的完整歷史，不是只有這一個 Pod incarnation
    的片段。"""
    with _pod_history_lock:
        app_key = _app_key_for_pod_name(name)
        return list(_pod_history.get(f"{namespace}/{app_key}", []))


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
                # 客戶叢集的 namespace（zt-）交給 _remote_healer_loop 用 Git 處理。平台自己的叢集剛好
                # 也是客戶叢集時（例如本機測試），兩個迴圈會搶同一個 Pod（2026-10-09 實測）。
                if _is_remote_ns(issue.get("namespace")):
                    continue
                # 同一個叢集上的另一個網站負責的 namespace 不碰（見 core/config.py 的 HEALER_*_NAMESPACES）
                if not healer_owns_namespace(issue.get("namespace") or ""):
                    continue
                key = f"{issue['namespace']}/{issue['pod_name']}/{issue['reason']}"
                now_keys.add(key)
                if key in seen:
                    continue
                seen.add(key)
                # 開始處理這一個 issue 就先登記「正在處理中」，不是等整批都處理完
                # 才更新狀態——根因診斷固定會打一次本地 LLM，實測一次 2-3 分鐘跑
                # 不掉，前端要能顯示「正在修復 X」的進度，liveness 判斷也要能用
                # 這個欄位分辨「卡住」跟「正在忙、只是忙很久」。
                with _healer_bg_lock:
                    _healer_bg_state["processing"] = {
                        "pod": issue["pod_name"], "namespace": issue["namespace"],
                        "reason": issue["reason"], "since": _utc_now_iso(),
                    }
                try:
                    result = _real_heal_pod(issue["pod_name"], issue["namespace"])
                    entry = {
                        "time": _utc_now_iso(), "pod": issue["pod_name"],
                        "namespace": issue["namespace"], "reason": issue["reason"],
                        "root_cause": result.get("root_cause"), "action": result.get("action"),
                        "ok": result.get("ok"), "message": result.get("message"),
                        "llm_raw_guess": result.get("llm_raw_guess"),
                    }
                except Exception as e:
                    entry = {
                        "time": _utc_now_iso(), "pod": issue["pod_name"],
                        "namespace": issue["namespace"], "reason": issue["reason"],
                        "ok": False, "error": str(e),
                    }
                with _healer_bg_lock:
                    _healer_bg_state["processing"] = None
                    _healer_bg_state["recent_actions"].insert(0, entry)
                    _healer_bg_state["recent_actions"] = _healer_bg_state["recent_actions"][:_HEALER_BG_MAX_HISTORY]
            seen &= now_keys
            with _healer_bg_lock:
                _healer_bg_state["last_scan"] = _utc_now_iso()
        except Exception:
            with _healer_bg_lock:
                _healer_bg_state["processing"] = None

        # Pod 時間序列快照——跟上面「掃問題、修復」是獨立的邏輯，故意包在自己的
        # try/except，避免這段（新功能，較不成熟）萬一出錯拖累上面已經穩定運作的
        # 自動修復邏輯。查全叢集所有 Pod（不是只有問題 Pod），所有帳號都要有趨勢圖。
        try:
            core = k8s_client.CoreV1Api()
            per_ns = {}
            for pod in core.list_pod_for_all_namespaces().items:
                app_key = pod.metadata.labels.get("app") if pod.metadata.labels else None
                key = f"{pod.metadata.namespace}/{app_key or _app_key_for_pod_name(pod.metadata.name)}"
                snap = _pod_light_snapshot(pod)
                with _pod_history_lock:
                    hist = _pod_history.setdefault(key, [])
                    hist.append(snap)
                    del hist[:-_POD_HISTORY_MAX_SAMPLES]
                if pod.metadata.namespace.startswith("user-"):
                    per_ns.setdefault(pod.metadata.namespace, []).append(snap)
            # 分析報告用的資源快照（每個帳號 10 分鐘最多一筆，見 _record_snapshot）
            for ns_name, lights in per_ns.items():
                _record_snapshot(ns_name, lights)
        except Exception:
            pass


def _healer_bg_liveness() -> dict:
    """即時算出背景自動修復迴圈的真實狀態，不用啟動時設過一次就不再檢查的舊旗標。

    2026-10-03 修復：原本只看「多久沒更新 last_scan」（> interval*3 = 90 秒）就
    判定卡住，但根因診斷固定會打一次本地 LLM，實測一次就要 2-3 分鐘，`last_scan`
    本來就要等整批 issue 都處理完才會更新——每次 Healer 正在認真修東西的時候，
    這個檢查反而會跳出「卡住了」的假警報，剛好是最不該報假警報的時刻。現在改成
    優先看 `processing`（正在處理中，見 `_healer_background_loop` 的說明）：
    只要有正在處理的紀錄、而且開始時間還在合理範圍內（給到 interval*20=10 分鐘，
    涵蓋多個 issue 要排隊處理的情況），就不算卡住，不管 `last_scan` 多舊。
    """
    with _healer_bg_lock:
        last_scan = _healer_bg_state["last_scan"]
        interval = _healer_bg_state.get("interval", 30)
        started_at = _healer_bg_state.get("started_at")
        recent = list(_healer_bg_state["recent_actions"])
        processing = _healer_bg_state.get("processing")

    thread_alive = bool(_healer_bg_thread is not None and _healer_bg_thread.is_alive())
    now = datetime.utcnow()
    stale = False
    if K8S_ENABLED and thread_alive:
        processing_recent = False
        if processing:
            try:
                since_dt = _parse_utc_iso(processing["since"])
                processing_recent = (now - since_dt).total_seconds() < interval * 20
            except Exception:
                processing_recent = False

        if processing_recent:
            stale = False
        elif last_scan is None:
            # 剛啟動、第一次掃描還沒發生前，給一次 interval 的寬限期，不要誤判成卡住。
            try:
                grace_until = _parse_utc_iso(started_at) + timedelta(seconds=interval * 1.5)
                stale = now > grace_until
            except Exception:
                stale = False
        else:
            try:
                last_dt = _parse_utc_iso(last_scan)
                stale = (now - last_dt).total_seconds() > interval * 3
            except Exception:
                stale = False

    running = bool(K8S_ENABLED and thread_alive and not stale)
    message = None
    if K8S_ENABLED and not thread_alive:
        message = (
            "自動監控背景執行緒已經停止（可能因未預期錯誤中斷），現在只能靠手動 Scan Now / "
            "「全部自動修復」，不會自動偵測新問題。請重新啟動 web_demo.py 以恢復自動監控。"
        )
    elif K8S_ENABLED and stale:
        message = (
            "自動監控似乎卡住了（太久沒有新的掃描紀錄，可能在等一個沒有逾時設定的 K8s API 呼叫）。"
            "建議重新啟動 web_demo.py 確認。"
        )

    return {
        "running": running, "thread_alive": thread_alive, "last_scan": last_scan,
        "recent_actions": recent, "message": message, "processing": processing,
    }


@app.route("/api/healer/status")
def api_healer_status():
    if "username" not in session: return jsonify({"error": "Not authenticated"}), 401
    d = _healer_bg_liveness()
    ns = _session_ns()
    d["recent_actions"] = _heal_history(ns)
    # 背景 Healer 一次只處理一個 Pod，「正在修復」是全平台共用的一份狀態；
    # 不是自己 namespace 的就不顯示，不然會看到別的帳號的服務名稱（2026-10-22 PWA 實測時發現）
    if (d.get("processing") or {}).get("namespace") != ns:
        d["processing"] = None
    return jsonify(d)


@app.route("/api/healer/fix", methods=["POST"])
def api_healer_fix():
    if "username" not in session: return jsonify({"error": "Not authenticated"}), 401
    data = request.get_json() or {}
    pod_name = data.get("pod_name", "")
    if not pod_name:
        return jsonify({"success": False, "error": "No pod name"})
    try:
        result = _real_heal_pod(pod_name, _session_ns())
        return jsonify({
            "success": result.get("ok", False),
            "message": result.get("message"),
            "root_cause": result.get("root_cause"),
            "action": result.get("action"),
            # 2026-10-07 近失事件可見化實測時發現：這個路由原本白名單式只挑 4 個欄位
            # jsonify，_real_heal_pod() 其實有回傳 llm_raw_guess，但這裡從沒被包含
            # 進回應，前端 fixPod() 讀 d.llm_raw_guess 永遠是 undefined——補上。
            "llm_raw_guess": result.get("llm_raw_guess"),
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route("/api/healer/auto_fix", methods=["POST"])
def api_healer_auto_fix():
    if "username" not in session: return jsonify({"error": "Not authenticated"}), 401
    try:
        from healer.pod_watcher import scan_once
        _ns = _session_ns()
        issues = (_rv.unhealthy_issues(_remote_report(_ns), _ns)[:_REMOTE_AUTOFIX_MAX] if _is_remote_ns(_ns)
                  else scan_once(namespace=_ns) or [])
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
                    "llm_raw_guess": result.get("llm_raw_guess"),
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
    user_ns = _session_ns()
    if _is_remote_ns(user_ns):
        pods = k8s_get_pods(namespace=user_ns)
        mine = [p for p in pods if p.get("app") != "zt-reporter"]
        return jsonify({"connected": True, "url": None, "metrics": {
            "prometheus_up": False, "namespace": user_ns, "cluster_pods": None,
            "pod_count": len(mine), "running_pods": len([p for p in mine if p.get("phase") == "Running"]),
            "source": "customer-cluster-report（你的叢集回報器，每 15 秒更新）"}})
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


@app.route("/api/dashboard")
def api_dashboard():
    """監控台頁面：叢集空間總覽（K8s requests 加總 vs Prometheus 實際用量，取較大值）
    + 使用者自己的部署明細 + 費用估算。見 _dashboard_summary()。2026-09-29 從隊友
    分支移植。"""
    if "username" not in session:
        return jsonify({"error": "Not authenticated"}), 401
    user_ns = _session_ns()
    return jsonify(_dashboard_summary(user_ns))


# ══════════════════════════════════════════════════════════════════
# 客戶自帶叢集第 5 階段：所有功能接上客戶叢集（2026-10-09）
# 做法：namespace 是客戶端（zt- 開頭）時，查詢類函式改讀回報器的最新報告
# （cluster/remote_view.py，回傳格式跟本機版完全相同），寫入類改成對客戶 repo 的 commit。
# 用「同名重新綁定」包裝原本的函式：Python 呼叫時才查全域名稱，所以所有既有呼叫端
# （Chat 接地問答、事實核對、衝突檢查、Dashboard…）自動走包裝層，本機版程式碼一行都沒改。
# ══════════════════════════════════════════════════════════════════
from flask import has_request_context
from cluster import remote_view as _rv
from cluster import remote_ops as _rops
from agents.cost_agent import (_parse_cpu_millicores as _pcpu, _parse_memory_bytes as _pmem,
                               _estimate_monthly_cost as _est_cost)

_SYNC_MSG = ("已送出，等你的叢集同步（約 30~60 秒），可以到「我的服務」頁面看進度。"
             )


def _is_remote_ns(ns) -> bool:
    return bool(ns) and str(ns).startswith("zt-")


def _session_ns() -> str:
    """目前登入者的 namespace：客戶叢集模式的非管理員 → 客戶叢集的 zt-…；其他 → 平台的 user-…。"""
    u = session.get("username")
    if CUSTOMER_CLUSTER_MODE and u and not session.get("is_admin"):
        from gitops.tenant_repo import tenant_slug
        return tenant_slug(u)
    return _user_namespace(u)


def _ctx_ns(ns=None):
    """沒有明確傳 namespace 的呼叫（節點容量、跨 namespace 的 port 衝突）用目前登入者的。"""
    if ns:
        return ns
    if has_request_context() and session.get("username"):
        return _session_ns()
    return None


def _remote_owner(ns):
    from gitops.tenant_repo import tenant_slug
    for u in list(CLUSTERS._data.keys()):
        if tenant_slug(u) == ns:
            return u
    return None


def _remote_report(ns) -> dict:
    u = _remote_owner(ns)
    return (CLUSTERS.latest_report(u)[1] if u else None) or {}


def _remote_write_guard(ns):
    """叢集離線時擋下寫入：commit 雖然會在叢集恢復後生效，但使用者會以為已經部署了。"""
    u = _remote_owner(ns)
    st = CLUSTERS.status(u) if u else {"code": "offline", "ok": False}
    if st["code"] == "awaiting_report":
        return "平台剛重新啟動，正在等你的叢集回報（約 15 秒），請稍後再試。"
    if st["code"] == "offline":
        return ("你的叢集目前離線（超過 2 分鐘沒有回報），請確認電腦開著、Docker Desktop 正在執行，"
                "恢復連線後再操作。")
    return None


_tenant_repo_cache = {}
_tenant_repo_cache_lock = threading.Lock()


def _tenant_repo(username):
    with _tenant_repo_cache_lock:
        repo = _tenant_repo_cache.get(username)
    if repo is None:
        repo, _, _ = _tenant_repo_for(username)
        with _tenant_repo_cache_lock:
            _tenant_repo_cache[username] = repo
    return repo


def _with_resource_defaults(parsed: dict) -> dict:
    """跟本機 k8s_deploy() 一樣：沒填 cpu/memory 就套 cost_agent 依映像檔類型推薦的值。"""
    p = dict(parsed)
    if p.get("cpu") and p.get("memory"):
        return p
    try:
        from agents.cost_agent import _APP_PROFILES, _detect_app_type
        req_cpu, req_mem, _, _ = _APP_PROFILES.get(_detect_app_type(p.get("image", ""), p.get("app_name", "")),
                                                   _APP_PROFILES["default"])
    except Exception:
        req_cpu, req_mem = 100, 128
    if not p.get("cpu"):
        p["cpu"] = f"{req_cpu}m"
    if not p.get("memory"):
        p["memory"] = f"{req_mem}Mi"
    return p


def _app_from_commit(msg: str) -> str:
    """從客戶 repo 的 commit 訊息取出 app 名稱（格式見 gitops/tenant_repo.py 的 _msg 呼叫處）：
    "deploy web (...)"、"delete web"、"rollback web to abc"、"restart web"、"scale to 5: web"、
    "update image -> x: web"、"set memory 512Mi: web"。"""
    m = (re.match(r"^(?:deploy|delete|rollback|restart|restore image of) ([a-z0-9-]+)", msg)
         or re.search(r": ([a-z0-9-]+) \(by ", msg))
    return m.group(1) if m else ""


def _remote_git_op(ns, op, describe):
    blocked = _remote_write_guard(ns)
    if blocked:
        return jsonify({"success": False, "error": blocked}), 409
    try:
        res = op(_tenant_repo(session["username"]))
    except Exception as e:  # TenantRepoError 訊息已是中英雙語
        return jsonify({"success": False, "error": str(e)}), 400
    return jsonify({"success": True, "pending_sync": True, "commit": res.get("commit"),
                    "message": describe(res) + " " + _SYNC_MSG, "error": None})


def _remote_scale(data):
    ns = _session_ns()
    name = (data.get("name") or "").strip()
    try:
        replicas = int(data.get("replicas", 1))
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "replicas must be an integer"}), 400
    if not name:
        return jsonify({"success": False, "error": "Name required"}), 400
    from guardian.yaml_validator import max_replicas
    if not 1 <= replicas <= max_replicas():
        return jsonify({"success": False, "error": f"副本數要在 1 到 {max_replicas()} 之間"}), 400
    blocked, risk_msg = _check_scale_risk(name, replicas, ns)
    if blocked:
        return jsonify({"success": False, "error": risk_msg, "blocked_reason": "resource_exceeded"}), 409
    return _remote_git_op(ns, lambda r: r.set_replicas(name, replicas),
                          lambda res: f"已把 {name} 調整為 {replicas} 個副本（原本 {res.get('previous_replicas')}）。")


_remote_history_last = {}


def _record_remote_history(username, report):
    from gitops.tenant_repo import tenant_slug
    now = _time.time()
    if now - _remote_history_last.get(username, 0) < 30:
        return
    _remote_history_last[username] = now
    ns = tenant_slug(username)
    with _pod_history_lock:
        for app, snap in _rv.light_snapshots(report, _utc_now_iso()).items():
            hist = _pod_history.setdefault(f"{ns}/{app}", [])
            hist.append(snap)
            del hist[:-_POD_HISTORY_MAX_SAMPLES]


# ── 查詢類包裝 ──
_local_k8s_get_pods = k8s_get_pods
_local_k8s_get_deployments = k8s_get_deployments
_local_k8s_get_services = k8s_get_services
_local_k8s_get_node_capacity = k8s_get_node_capacity
_local_k8s_describe_pod = k8s_describe_pod
_local_k8s_describe_deployment = k8s_describe_deployment
_local_pod_real_usage = _pod_real_usage
_local_ensure_user_namespace = _ensure_user_namespace
_local_sum_cluster_resource_requests = _sum_cluster_resource_requests
_local_check_scale_risk = _check_scale_risk
_local_dashboard_summary = _dashboard_summary
_local_k8s_delete_deployment = k8s_delete_deployment
_local_real_heal_pod = _real_heal_pod


def k8s_get_pods(app_name=None, namespace=None):
    if _is_remote_ns(namespace):
        return _rv.list_pods(_remote_report(namespace), app_name)
    return _local_k8s_get_pods(app_name, namespace)


def k8s_get_deployments(namespace=None):
    if _is_remote_ns(namespace):
        return _rv.list_deployments(_remote_report(namespace))
    return _local_k8s_get_deployments(namespace)


def k8s_get_services(namespace=None, all_namespaces=False):
    # 客戶叢集的 host port 綁在客戶自己的電腦上，跟平台和其他客戶都無關；平台只看得到
    # 客戶自己的 namespace（看不到客戶叢集裡其他程式佔用的 port，這是唯讀權限的取捨）
    target = namespace or (_ctx_ns() if all_namespaces else None)
    if _is_remote_ns(target):
        return _rv.list_services(_remote_report(target), target)
    return _local_k8s_get_services(namespace, all_namespaces)


def k8s_get_node_capacity(namespace=None):
    target = _ctx_ns(namespace)
    if _is_remote_ns(target):
        return _rv.node_capacity(_remote_report(target), _pmem)
    return _local_k8s_get_node_capacity()


def k8s_describe_pod(name, namespace=None):
    if _is_remote_ns(namespace):
        return _rv.describe_pods(_remote_report(namespace), name)
    return _local_k8s_describe_pod(name, namespace)


def k8s_describe_deployment(name, namespace=None):
    if _is_remote_ns(namespace):
        return _rv.describe_deployment(_remote_report(namespace), name)
    return _local_k8s_describe_deployment(name, namespace)


def _pod_real_usage(namespace, pod_name):
    if _is_remote_ns(namespace):
        return _rv.pod_usage(_remote_report(namespace), pod_name, _pcpu, _pmem)
    return _local_pod_real_usage(namespace, pod_name)


def _ensure_user_namespace(namespace):
    if _is_remote_ns(namespace):
        return  # 客戶叢集的 namespace 由安裝包建立，平台沒有權限也不需要建
    return _local_ensure_user_namespace(namespace)


def _sum_cluster_resource_requests(exclude_name=None, exclude_namespace=None):
    target = _ctx_ns(exclude_namespace)
    if _is_remote_ns(target):
        rep = _remote_report(target)
        if exclude_name:
            rep = dict(rep, pods=[p for p in rep.get("pods") or [] if p.get("app") != exclude_name])
        return _rv.requests_sum(rep, _pcpu, _pmem)
    return _local_sum_cluster_resource_requests(exclude_name, exclude_namespace)


def _check_scale_risk(name, new_replicas, namespace=None):
    if not _is_remote_ns(namespace):
        return _local_check_scale_risk(name, new_replicas, namespace)
    rep = _remote_report(namespace)
    cap = _rv.node_capacity(rep, _pmem)
    if not cap:
        return False, None
    node_cpu, node_mem = _pcpu(cap["cpu"]) or 0, _pmem(cap["memory"]) or 0
    base_cpu, base_mem = _sum_cluster_resource_requests(exclude_name=name, exclude_namespace=namespace)
    req = _rv._app_requests(rep).get(name, {})
    total_cpu = base_cpu + (_pcpu(req.get("cpu")) or 0) * new_replicas
    total_mem = base_mem + (_pmem(req.get("memory")) or 0) * new_replicas
    if (node_cpu and total_cpu > node_cpu) or (node_mem and total_mem > node_mem):
        return True, (f"這樣會超出你電腦叢集的容量：套用後合計約需要 {total_cpu}m CPU / {total_mem // (1024**2)}Mi "
                      f"記憶體，但你的節點只有 {node_cpu}m CPU / {node_mem // (1024**2)}Mi。多出來的 Pod 會卡在 "
                      f"Pending。請降低副本數。 / This would exceed your cluster's capacity; please lower the replica count.")
    return False, None


def _dashboard_summary(namespace):
    if not _is_remote_ns(namespace):
        return _local_dashboard_summary(namespace)
    rep = _remote_report(namespace)
    result = {"k8s_enabled": True, "prometheus_up": False, "cluster_capacity": None, "cluster_used": None,
              "cluster_used_source": None, "cluster_remaining": None, "my_deployments": [],
              "my_total_cpu_cores": None, "my_total_mem_gib": None, "my_monthly_usd": None,
              "cpu_trend": [], "mem_trend": [], "customer_cluster": True}
    cap = _rv.node_capacity(rep, _pmem)
    if cap:
        node_cpu, node_mem = _pcpu(cap["cpu"]) or 0, _pmem(cap["memory"]) or 0
        used_cpu, used_mem = _rv.requests_sum(rep, _pcpu, _pmem)
        result["cluster_capacity"] = {"cpu_cores": round(node_cpu / 1000, 2), "mem_gib": round(node_mem / 1024 ** 3, 2)}
        result["cluster_used"] = {"cpu_cores": round(used_cpu / 1000, 2), "mem_gib": round(used_mem / 1024 ** 3, 2)}
        # 平台只看得到客戶自己的 namespace，「已使用」不含客戶叢集裡其他程式
        result["cluster_used_source"] = {"cpu": "requests (your namespace)", "mem": "requests (your namespace)"}
        result["cluster_remaining"] = {"cpu_cores": round((node_cpu - used_cpu) / 1000, 2),
                                       "mem_gib": round((node_mem - used_mem) / 1024 ** 3, 2)}
    my = _rv.deployment_breakdown(rep, _pcpu, _pmem, _est_cost)
    result.update(my_deployments=my["deployments"], my_total_cpu_cores=my["total_cpu_cores"],
                  my_total_mem_gib=my["total_mem_gib"], my_monthly_usd=my["monthly_usd"])
    return result


# ── 寫入類包裝 ──
def k8s_delete_deployment(app_name, namespace=None):
    result = _k8s_delete_deployment_by_mode(app_name, namespace)
    # 2026-10-21 專案功能：服務刪掉後清掉它的專案歸屬，同名的新服務不會繼承（兩種叢集模式都經過這裡）
    if result and result[0] and namespace:
        try:
            from core import workspace_store
            workspace_store.forget_service(namespace, app_name)
        except Exception:
            pass
    return result


def _k8s_delete_deployment_by_mode(app_name, namespace=None):
    if not _is_remote_ns(namespace):
        return _local_k8s_delete_deployment(app_name, namespace)
    blocked = _remote_write_guard(namespace)
    if blocked:
        return False, blocked
    try:
        _tenant_repo(_remote_owner(namespace)).delete_app(app_name)
        return True, f"已刪除 {app_name}。{_SYNC_MSG}"
    except Exception as e:
        return False, str(e)


_REMOTE_HEAL_COOLDOWN = 180  # 秒
_remote_heal_recent = {}
_remote_heal_lock = threading.Lock()


def _real_heal_pod(pod_name, namespace="default"):
    if not _is_remote_ns(namespace):
        return _local_real_heal_pod(pod_name, namespace)
    rep = _remote_report(namespace)
    issue = next((i for i in _rv.unhealthy_issues(rep, namespace) if i["pod_name"] == pod_name), None)
    if not issue:
        return {"ok": True, "action": "none", "root_cause": None,
                "message": f"Pod {pod_name} 目前狀態正常，無需修復"}
    # 冷卻：修復 commit 之後、客戶叢集同步並換掉壞 Pod 之前（約 1 分鐘），回報裡還看得到同一個
    # 問題。這段時間手動修復跟背景迴圈都不能再對同一個服務動手，否則會疊加修復
    # （2026-10-09 端到端實測：設定因此在好壞之間來回跳了 5 次）。
    cool_key = (namespace, issue.get("app") or pod_name)
    with _remote_heal_lock:
        last = _remote_heal_recent.get(cool_key, 0)
        if _time.time() - last < _REMOTE_HEAL_COOLDOWN:
            return {"ok": True, "action": "in_progress", "root_cause": None, "pending_sync": True,
                    "message": f"{issue.get('app') or pod_name} 剛修復過，正在等你的叢集同步（最多約 "
                               f"{_REMOTE_HEAL_COOLDOWN // 60} 分鐘），先不重複處理。"}
        _remote_heal_recent[cool_key] = _time.time()  # 先佔住，避免兩個請求同時進來都動手
    from healer.diagnose import diagnose_issue
    diagnosis = diagnose_issue({**issue, "reason": issue["reason"]})
    dep = next((d for d in rep.get("deployments") or [] if d["name"] == issue.get("app")), {})
    blocked = _remote_write_guard(namespace)
    if blocked:
        result = {"ok": False, "action": diagnosis.get("action"), "message": blocked}
    else:
        result = _rops.heal(_tenant_repo(_remote_owner(namespace)), issue, diagnosis,
                            current_memory=dep.get("memory_limit"))
    if not result.get("pending_sync"):
        with _remote_heal_lock:  # 沒有真的送出修正（人工處理、失敗）→ 不佔冷卻時間
            _remote_heal_recent.pop(cool_key, None)
    result["root_cause"] = diagnosis.get("root_cause")
    result["severity"] = diagnosis.get("severity")
    result["llm_raw_guess"] = diagnosis.get("llm_raw_guess")
    return result


# 每位客戶的自動修復上限（2026-10-09 背景安全審查：resource exhaustion）。客戶可以不經過平台，
# 直接在自己叢集建一大堆會壞的 Pod，回報器最多回報 300 個；沒有上限的話，背景迴圈會對每個都跑一次
# LLM 診斷 + 一個 GitHub commit，一個客戶就能吃光平台的 GPU 與 GitHub API 額度、拖慢所有人。
_REMOTE_HEAL_PER_SCAN = 3
_REMOTE_HEAL_PER_HOUR = 10
_REMOTE_AUTOFIX_MAX = 5
_remote_heal_budget = {}  # username -> [時間戳記…]（最近一小時）


def _remote_heal_allowed(username) -> bool:
    now = _time.time()
    with _remote_heal_lock:
        recent = [t for t in _remote_heal_budget.get(username, []) if now - t < 3600]
        if len(recent) >= _REMOTE_HEAL_PER_HOUR:
            _remote_heal_budget[username] = recent
            return False
        recent.append(now)
        _remote_heal_budget[username] = recent
        return True


def _remote_healer_loop(interval: int = 30):
    """客戶叢集的背景自動修復：每 30 秒看每個已連線客戶的最新回報，同一個問題只處理一次
    （同本機 _healer_background_loop 的 seen 集合做法），修復紀錄併進同一個 Healer 狀態。"""
    from gitops.tenant_repo import tenant_slug
    seen = set()
    stop = threading.Event()
    while not stop.wait(interval):
        try:
            now_keys = set()
            for username in list(CLUSTERS._data.keys()):
                if not CLUSTERS.status(username)["ok"]:
                    continue
                ns = tenant_slug(username)
                handled = 0
                rep_now = _remote_report(ns)
                deps_now = {d["name"]: d for d in rep_now.get("deployments") or []}
                for issue in _rv.unhealthy_issues(rep_now, ns):
                    # 把目前的記憶體上限與映像檔也算進「是不是同一個狀況」：修過之後設定變了就算新狀況，
                    # 可以再處理一次（例如記憶體加倍一次不夠，會繼續加倍到修好或碰到 4Gi 上限）；
                    # 設定沒變就不重複處理。另有冷卻時間與每小時上限保護。
                    dep = deps_now.get(issue.get("app"), {})
                    key = (f"{ns}/{issue['app'] or issue['pod_name']}/{issue['reason']}/"
                           f"{dep.get('memory_limit')}/{dep.get('image')}")
                    now_keys.add(key)
                    if key in seen:
                        continue
                    # 超過這一輪或這一小時的上限：先不處理，也不記進 seen，下一輪/額度恢復後再處理
                    if handled >= _REMOTE_HEAL_PER_SCAN or not _remote_heal_allowed(username):
                        continue
                    handled += 1
                    seen.add(key)
                    try:
                        result = _real_heal_pod(issue["pod_name"], ns)
                        if result.get("action") == "in_progress":
                            continue  # 冷卻中、這次沒有動作，不寫進修復紀錄（避免出現沒做事卻打勾的紀錄）
                        entry = {"time": _utc_now_iso(), "pod": issue["pod_name"], "namespace": ns,
                                 "reason": issue["reason"], "root_cause": result.get("root_cause"),
                                 "action": result.get("action"), "ok": result.get("ok"),
                                 "message": result.get("message"), "llm_raw_guess": result.get("llm_raw_guess")}
                    except Exception as e:
                        entry = {"time": _utc_now_iso(), "pod": issue["pod_name"], "namespace": ns,
                                 "reason": issue["reason"], "ok": False, "error": str(e)}
                    with _healer_bg_lock:
                        _healer_bg_state["recent_actions"].insert(0, entry)
                        _healer_bg_state["recent_actions"] = _healer_bg_state["recent_actions"][:_HEALER_BG_MAX_HISTORY]
            seen &= now_keys
        except Exception as e:
            print(f"[remote healer] loop error: {e}")



# ── 事件紀錄與分析報告（2026-10-10）──────────────────────────────────
# 部署、審查判決、擴縮、換映像檔、刪除、回滾、Healer 修復、資源快照都寫進 data/events.db，
# 分析報告（agents/report.py）才能回答「過去這段時間發生了什麼」。紀錄一律帶 namespace，
# 查詢也只查目前登入帳號自己的 namespace（_session_ns()），沿用既有的多租戶隔離。
from core import event_store as _events
_events.init()

_EVENT_ROUTES = {"/api/deploy/parse", "/api/deploy", "/api/scale", "/api/update", "/api/delete", "/api/rollback"}


def _review_event_data(review: dict, extra: dict = None) -> dict:
    review = review or {}
    agents = ((review.get("agents") or {}).get("agents") or {})
    issues = []
    for key in ("security", "cost", "perf"):
        for i in (agents.get(key) or {}).get("issues") or []:
            if i.get("message") and i.get("severity") in ("critical", "high", "medium"):
                issues.append(str(i["message"])[:200])
    est = ((agents.get("cost") or {}).get("cost_estimate") or {})
    d = {"blockers": [str(b)[:300] for b in (review.get("blockers") or [])][:6],
         "issues": issues[:8], "monthly_usd": est.get("estimated_usd")}
    d.update(extra or {})
    return d


@app.after_request
def _record_events(resp):
    """操作結束後依回應內容記一筆。任何錯誤都吞掉，紀錄不能影響操作本身。"""
    try:
        if request.method != "POST" or request.path not in _EVENT_ROUTES or "username" not in session:
            return resp
        out = resp.get_json(silent=True) or {}
        body = request.get_json(silent=True) or {}
        ns, user = _session_ns(), session.get("username")
        path = request.path
        if path in ("/api/deploy/parse", "/api/deploy"):
            review = out.get("review")
            spec = out.get("raw") or out.get("parsed") or body.get("parsed") or {}
            app_name = spec.get("app_name") or (spec.get("output") or {}).get("app_name")
            rejected = bool(out.get("rejected"))
            # 審查：在「審查」那一步（帶著使用者確認過的規格）記一次；任何階段被擋下也記
            if review and (rejected or (path == "/api/deploy/parse" and body.get("parsed"))):
                _events.record(ns, "review", app_name, review.get("decision") or ("block" if rejected else None),
                               _review_event_data(review, {"image": spec.get("image"), "pods": spec.get("pods")}),
                               username=user)
            if path == "/api/deploy" and not rejected and resp.status_code < 400:
                kd = out.get("k8s_deploy") or {}
                ok = kd.get("ok") is not False and (out.get("gitops") or {}).get("ok", True) is not False
                rs = out.get("resource_summary") or {}
                _events.record(ns, "deploy", app_name, "ok" if ok else "fail",
                               {"image": spec.get("image"), "pods": spec.get("pods"), "port": spec.get("port"),
                                "monthly_usd": rs.get("monthly_usd"),
                                "error": None if ok else (kd.get("message") or (out.get("gitops") or {}).get("message"))},
                               username=user)
            elif path == "/api/deploy" and resp.status_code >= 400 and not rejected and out.get("error"):
                _events.record(ns, "deploy", app_name, "fail", {"error": str(out.get("error"))[:300]}, username=user)
            return resp
        kind = path.rsplit("/", 1)[-1]
        ok = bool(out.get("success")) and resp.status_code < 400
        name = (body.get("name") or body.get("app_name") or "").strip()[:100]
        data = {"error": None if ok else str(out.get("error") or out.get("message") or "")[:300]}
        if kind == "scale":
            data["replicas"] = body.get("replicas")
        if kind == "update":
            data["image"] = body.get("image")
            data["previous_image"] = out.get("previous_image")
        _events.record(ns, kind, name, "ok" if ok else "fail", data, username=user)
    except Exception as e:
        print(f"[事件紀錄] 略過：{e}")
    return resp


# Healer：手動修復、一鍵修復、背景自動修復最後都呼叫全域的 _real_heal_pod，在最外層包一層記錄
_real_heal_pod_unrecorded = _real_heal_pod
_HEAL_ACTION_LABEL = {"analyze_logs": "重新建立 Pod", "fix_image": "退回上一個版本", "increase_memory": "調高記憶體上限",
                      "fix_probe": "放寬健康檢查的等待時間", "restore_previous_image": "換回上一個映像檔",
                      "restart_app": "重新啟動", "set_memory_limit": "調高記憶體上限", "revert_app": "回到上一版",
                      "check_dependencies": "檢查相依服務", "fix_permissions": "檢查權限設定",
                      "create_config": "檢查設定檔", "fix_port_conflict": "檢查 Port 衝突", "manual_inspect": "需要人工檢查"}
# 實際改動叢集（或送出修正 commit）的處理方式；其他的只列出資訊，回傳 ok 只代表「查完了」，不能算修好
_HEAL_REPAIR_ACTIONS = {"analyze_logs", "fix_image", "increase_memory", "fix_probe", "restore_previous_image",
                        "restart_app", "set_memory_limit", "revert_app"}


def _heal_recently_recorded(namespace, app, action, pod=None, hours=6):
    """6 小時內已經記過就不再記（也不再推播）。網站每次重啟背景 Healer 都會重新檢查一次還沒解決的問題
    （2026-10-21 全系統檢查：orders-api 同一個問題一週被記 16 次，報告上的修復次數被灌水）。

    比對的是「服務＋處理方式」，真的有改動的處理再加上 Pod 名稱；**不比對原因文字**：原因是模型寫的，
    同一個問題每次措辭都不同（2026-10-22 手機實測：連不上資料庫 13 分鐘內通知 4 次；反過來，記憶體上限
    連調 3 次卻因為措辭剛好相同而沒有紀錄、沒有通知）。真的修過一次就會換成新的 Pod，所以每次實際的修復
    都會記到；只是查出原因、沒有改動的（例如請你檢查相依服務），同一個服務 6 小時內只記一次。"""
    try:
        for e in reversed(_events.query(namespace, since=_time.time() - hours * 3600, kinds=["heal"])):
            d = e.get("data") or {}
            if e.get("app") == app and d.get("action") == action and (pod is None or d.get("pod") == pod):
                return True
    except Exception:
        pass
    return False


def _real_heal_pod(pod_name, namespace="default"):
    result = _real_heal_pod_unrecorded(pod_name, namespace)
    try:
        action = (result or {}).get("action")
        if action and action not in ("none", "in_progress") and not _heal_recently_recorded(
                namespace, _app_key_for_pod_name(pod_name), action,
                pod_name if action in _HEAL_REPAIR_ACTIONS else None):
            outcome = ("ok" if result.get("ok") else "fail") if action in _HEAL_REPAIR_ACTIONS else "inspect"
            app_key = _app_key_for_pod_name(pod_name)
            label = _HEAL_ACTION_LABEL.get(action, action)
            _events.record(namespace, "heal", app_key, outcome,
                           {"pod": pod_name, "action": action, "action_label": label,
                            "root_cause": result.get("root_cause"), "message": str(result.get("message") or "")[:300],
                            "llm_raw_guess": result.get("llm_raw_guess")})
            # 推播跟紀錄用同一個去重：同一個問題 6 小時內只通知一次。點通知用服務名稱查，
            # Healer 重建 Pod 後舊的 Pod 名稱就查不到了
            _push.send_async(namespace, _push.heal_message(app_key, outcome, result.get("root_cause"), label, app_key))
    except Exception as e:
        print(f"[事件紀錄] Healer 紀錄略過：{e}")
    return result


def _heal_history(namespace: str, limit: int = None) -> list:
    """Healer 頁「自動修復紀錄」：從事件紀錄讀，最新的在最前面。

    原本讀 _healer_bg_state["recent_actions"]（只在記憶體），web_demo 重啟就清空，畫面會說
    「還沒有自動修復過」；而且那份清單是所有帳號共用的，會看到別人 namespace 的修復。
    改從事件紀錄讀，重開機後仍在，也只查目前帳號的 namespace。
    """
    limit = limit or _HEALER_BG_MAX_HISTORY
    try:
        rows = _events.query(namespace, since=_time.time() - 30 * 86400, kinds=["heal"])
    except Exception as e:
        print(f"[事件紀錄] 讀取修復紀錄失敗：{e}")
        return []
    out = []
    for r in reversed(rows[-limit:]):
        d = r.get("data") or {}
        outcome = r.get("outcome")
        out.append({
            "time": datetime.utcfromtimestamp(r["ts"]).isoformat() + "Z",
            "pod": d.get("pod") or r.get("app") or "",
            "root_cause": d.get("root_cause"),
            "action": d.get("action_label") or d.get("action"),
            # ok：真的改動且成功 True、改動失敗 False；只查出原因沒改動（inspect）為 None
            "ok": True if outcome == "ok" else (False if outcome == "fail" else None),
            "needs_user": outcome == "inspect",
            "llm_raw_guess": d.get("llm_raw_guess"),
        })
    return out


# 資源快照：每個 namespace 每 10 分鐘最多記一次（Pod 數、異常數、累計重啟）
_SNAPSHOT_EVERY = 600
_snapshot_last = {}


def _record_snapshot(namespace: str, lights: list):
    now = _time.time()
    if not namespace or now - _snapshot_last.get(namespace, 0) < _SNAPSHOT_EVERY:
        return
    _snapshot_last[namespace] = now
    unhealthy = sum(1 for x in lights if x.get("phase") != "Succeeded"
                    and not (x.get("phase") == "Running" and x.get("total") and x.get("ready") == x.get("total")))
    _events.record(namespace, "snapshot", None, None,
                   {"pods": len(lights), "unhealthy": unhealthy, "restarts": sum(int(x.get("restarts") or 0) for x in lights)})


def _report_live(ns: str) -> dict:
    live = {}
    try:
        pods = k8s_get_pods(namespace=ns) or []
        live["pods"] = len(pods)
        def _bad(p):
            if p.get("phase") == "Succeeded":
                return False
            ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in p.get("conditions") or [])
            return p.get("phase") != "Running" or not ready
        live["unhealthy"] = sum(1 for p in pods if _bad(p))
    except Exception:
        pass
    try:
        dash = _dashboard_summary(ns)
        live["services"] = len(dash.get("my_deployments") or [])
        live["cpu_cores"] = dash.get("my_total_cpu_cores")
        live["mem_gib"] = dash.get("my_total_mem_gib")
        live["monthly_usd"] = dash.get("my_monthly_usd")
    except Exception:
        pass
    return live


def _build_user_report(ns: str, days: int, with_ai: bool = True) -> dict:
    from agents.report import build_report, validate_narrative, facts_for_model
    since = _time.time() - days * 86400
    facts = build_report(_events.query(ns, since=since), days, live=_report_live(ns),
                         first_record_ts=_events.first_ts(ns))
    facts["ai"] = None
    if with_ai and facts["has_data"]:
        try:
            from core.gemini_client import gemini_report_narrative
            text = gemini_report_narrative(facts_for_model(facts))
        except Exception:
            text = None
        if text:
            facts["ai"] = validate_narrative(text, facts)
        else:
            facts["ai"] = {"sentences": [], "dropped": 0, "unavailable": True}
    facts["generated_at"] = _utc_now_iso()
    facts["namespace"] = ns
    return facts


@app.route("/api/report")
def api_report():
    if "username" not in session:
        return jsonify({"error": "Not authenticated"}), 401
    from agents.report import parse_period, MAX_DAYS
    try:
        days = int(request.args.get("days") or 0)
    except ValueError:
        days = 0
    days = max(1, min(MAX_DAYS, days)) if days else parse_period(request.args.get("q", ""))
    return jsonify(_build_user_report(_session_ns(), days, with_ai=request.args.get("ai", "1") != "0"))


# ── 分時擴縮與休眠模式（2026-10-11）──────────────────────────────────
# agents/timeslot.py：一句話時段 → 時段表、省錢換算；core/sleep_mode.py：閒置判斷與喚醒器；
# core/autoscale_store.py：使用者的設定（排程、閒置天數、例外清單）。
# 休眠只支援本機模式（要看平台自己的 Prometheus）；分時擴縮在客戶叢集也能用，副本數寫進客戶的部署設定 repo（2026-10-23）。
# 服務是不是在休眠、原本開幾個，記在 Deployment 自己的 annotation 上；背景迴圈每 10 秒對一次帳，
# 所以服務被手動擴縮、重新部署或刪除時，喚醒器和 Service 都會自動收回原狀，不用每條路徑各自處理。
from core import autoscale_store as _as_store
from core import sleep_mode as _sleep
from agents import timeslot as _ts

_as_lock = threading.Lock()
_as_busy = set()                     # 正在休眠或喚醒的 (namespace, 服務)，同一個服務不能同時被兩個執行緒處理
_as_state = {"idle": {}, "prometheus_ok": None, "last_idle_check": None}
_AS_TICK = 10
# 2026-10-21：分時擴縮／休眠的 K8s 呼叫一律設逾時（連線 5 秒、讀取 15 秒），叢集卡住時不會讓請求或背景迴圈跟著卡住
_AS_K8S_TIMEOUT = (5, 15)
_AS_SCHEDULE_EVERY = 30
_AS_IDLE_EVERY = 600
# 測試、展示用：把閒置期間縮成幾秒，只套用在 ZT_IDLE_OVERRIDE_NAMESPACES 列出的 namespace。正式運作不要設。
_AS_IDLE_OVERRIDE = int(os.environ.get("ZT_IDLE_SECONDS_OVERRIDE") or 0)
_AS_IDLE_OVERRIDE_NS = {x.strip() for x in os.environ.get("ZT_IDLE_OVERRIDE_NAMESPACES", "").split(",") if x.strip()}
if _AS_IDLE_OVERRIDE:
    _AS_IDLE_EVERY = 60
_AS_WEEKDAYS = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]


def _as_claim(ns, app) -> bool:
    with _as_lock:
        if (ns, app) in _as_busy:
            return False
        _as_busy.add((ns, app))
        return True


def _as_release(ns, app):
    with _as_lock:
        _as_busy.discard((ns, app))


def _as_iso_ts(s) -> float:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _as_fmt_when(dt) -> str:
    return f"{dt.strftime('%m-%d')}（{_AS_WEEKDAYS[dt.weekday()]}）{dt.strftime('%H:%M')}"


def _as_is_waker(dep) -> bool:
    return bool((dep.metadata.labels or {}).get(_sleep.LABEL_WAKER))


def _as_wait_ready(ns, name, timeout):
    apps = k8s_client.AppsV1Api()
    end = _time.time() + timeout
    while _time.time() < end:
        try:
            d = apps.read_namespaced_deployment(name, ns, _request_timeout=_AS_K8S_TIMEOUT)
            want = d.spec.replicas or 0
            if want and (d.status.ready_replicas or 0) >= want:
                return True
        except Exception:
            pass
        _time.sleep(3)
    return False


def _as_target_ports(svc, dep):
    """Service 轉進容器的 port（targetPort 可能是數字，也可能是容器 port 的名字）。"""
    named = {p.name: p.container_port for c in (dep.spec.template.spec.containers or [])
             for p in (c.ports or []) if p.name}
    out = []
    for p in svc.spec.ports or []:
        t = p.target_port
        if isinstance(t, int):
            out.append(t)
        elif isinstance(t, str) and t.isdigit():
            out.append(int(t))
        elif isinstance(t, str) and t in named:
            out.append(named[t])
        elif t is None:
            out.append(p.port)
    return out


def _as_services_for(ns, dep, redirected=False):
    """選到這個服務 Pod 的 Service；redirected=True 時找已經改接到它的喚醒器的。"""
    labels = dep.spec.template.metadata.labels or {}
    out = []
    for s in k8s_client.CoreV1Api().list_namespaced_service(ns, _request_timeout=_AS_K8S_TIMEOUT).items:
        sel = s.spec.selector or {}
        if redirected and sel.get(_sleep.LABEL_WAKER) == dep.metadata.name:
            out.append(s)
        elif not redirected and _sleep.selector_matches(sel, labels):
            out.append(s)
    return out


def _as_point_services(ns, svcs, to_waker_of=None):
    """把 Service 改接到喚醒器，或改回原本的 selector。selector 是 map，patch 會合併，舊的 key 要明確設成 None。"""
    core = k8s_client.CoreV1Api()
    for s in svcs:
        cur = dict(s.spec.selector or {})
        if to_waker_of:
            patch = {"metadata": {"annotations": {_sleep.ANN_ORIG_SELECTOR: _sleep.dump_selector(cur)}},
                     "spec": {"selector": {**{k: None for k in cur}, _sleep.LABEL_WAKER: to_waker_of}}}
        else:
            try:
                orig = json.loads((s.metadata.annotations or {}).get(_sleep.ANN_ORIG_SELECTOR) or "{}")
            except ValueError:
                orig = {}
            if not orig:
                continue
            patch = {"metadata": {"annotations": {_sleep.ANN_ORIG_SELECTOR: None}},
                     "spec": {"selector": {**{k: None for k in cur if k not in orig}, **orig}}}
        core.patch_namespaced_service(s.metadata.name, ns, patch, _request_timeout=_AS_K8S_TIMEOUT)


def _as_cost_per_replica_hour(dep) -> float:
    from agents.cost_agent import _parse_cpu_millicores, _parse_memory_bytes
    cs = dep.spec.template.spec.containers or []
    req = (cs[0].resources.requests or {}) if cs and cs[0].resources else {}
    return _ts.cost_per_replica_hour(_parse_cpu_millicores(req.get("cpu")) or 0, _parse_memory_bytes(req.get("memory")) or 0)


def _as_sleep(ns, app, why=""):
    """副本數降到 0。有 Service 的先開喚醒器、把 Service 改接過去，確定接得到人才把服務關掉。"""
    if not _as_claim(ns, app):
        return False, "這個服務正在處理中，請稍等。"
    apps = k8s_client.AppsV1Api()
    waker = _sleep.waker_name(app)
    try:
        dep = apps.read_namespaced_deployment(app, ns, _request_timeout=_AS_K8S_TIMEOUT)
        ann = dict(dep.metadata.annotations or {})
        cur = int(dep.spec.replicas or 0)
        if ann.get(_sleep.ANN_SLEEP_REPLICAS) or cur == 0:
            return False, f"{app} 已經在休眠，或副本數本來就是 0。"
        ok, msg, _status = _scale_check(app, 0, ns, allow_zero=True)
        if not ok:
            raise RuntimeError(msg)
        svcs = _as_services_for(ns, dep)
        if svcs:
            body = _sleep.waker_manifest(app, ns, [p for s in svcs for p in _as_target_ports(s, dep)])
            try:
                apps.create_namespaced_deployment(ns, body, _request_timeout=_AS_K8S_TIMEOUT)
            except Exception as e:
                if getattr(e, "status", None) != 409:
                    raise
                apps.replace_namespaced_deployment(waker, ns, body, _request_timeout=_AS_K8S_TIMEOUT)
            if not _as_wait_ready(ns, waker, 120):
                try:
                    apps.delete_namespaced_deployment(waker, ns, _request_timeout=_AS_K8S_TIMEOUT)
                except Exception:
                    pass
                raise RuntimeError("喚醒器沒有在 2 分鐘內啟動（可能是映像檔下載失敗），這次先不休眠")
            _as_point_services(ns, svcs, to_waker_of=app)
        apps.patch_namespaced_deployment(app, ns, {
            "metadata": {"annotations": {_sleep.ANN_SLEEP_REPLICAS: str(cur), _sleep.ANN_SLEEPING_SINCE: _utc_now_iso(),
                                         _sleep.ANN_WAKING: None}},
            "spec": {"replicas": 0}}, _request_timeout=_AS_K8S_TIMEOUT)
        _events.record(ns, "sleep", app, "ok", {"replicas": cur, "why": why, "auto_wake": bool(svcs)})
        _as_notify(ns, app, "💤 服務休眠", f"{app} 太久沒有人使用，已經休眠（原本 {cur} 個副本）。"
                   + ("有人連進來會自動喚醒。" if svcs else f"需要時請說「喚醒 {app}」。"))
        return True, (f"{app} 已經進入休眠（原本 {cur} 個副本）。" +
                      ("有人連進來時會自動喚醒。" if svcs else f"它沒有連線入口，需要時請說「喚醒 {app}」。"))
    except Exception as e:
        _events.record(ns, "sleep", app, "fail", {"error": str(e)[:300]})
        return False, f"{app} 沒有進入休眠：{e}"
    finally:
        _as_release(ns, app)


def _as_finish_wake(ns, dep):
    """Service 接回原本的服務、刪掉喚醒器、清掉休眠標記。重複呼叫沒有副作用。"""
    app = dep.metadata.name
    apps = k8s_client.AppsV1Api()
    try:
        _as_point_services(ns, _as_services_for(ns, dep, redirected=True))
    finally:
        try:
            apps.delete_namespaced_deployment(_sleep.waker_name(app), ns, _request_timeout=_AS_K8S_TIMEOUT)
        except Exception:
            pass
        try:
            apps.patch_namespaced_deployment(app, ns, {"metadata": {"annotations": {
                _sleep.ANN_SLEEP_REPLICAS: None, _sleep.ANN_SLEEPING_SINCE: None, _sleep.ANN_WAKING: None,
                _sleep.ANN_ACTIVE_SINCE: _utc_now_iso()}}}, _request_timeout=_AS_K8S_TIMEOUT)
        except Exception:
            pass   # 服務已經被刪掉


def _as_wake(ns, app, source="manual", replicas=None):
    """恢復休眠前的副本數，等全部就緒再把 Service 接回去（接回去之前，連線都由喚醒器回「正在喚醒」）。"""
    if not _as_claim(ns, app):
        return False, f"{app} 正在喚醒中，請稍等。"
    apps = k8s_client.AppsV1Api()
    target = None
    try:
        dep = apps.read_namespaced_deployment(app, ns, _request_timeout=_AS_K8S_TIMEOUT)
        ann = dict(dep.metadata.annotations or {})
        if not ann.get(_sleep.ANN_SLEEP_REPLICAS):
            _as_finish_wake(ns, dep)
            return True, f"{app} 沒有在休眠，正常運作中。"
        target = int(replicas or ann.get(_sleep.ANN_SLEEP_REPLICAS) or 1)
        ok, msg, _status = _scale_check(app, target, ns)
        if not ok:
            raise RuntimeError(msg)
        apps.patch_namespaced_deployment(app, ns, {"metadata": {"annotations": {_sleep.ANN_WAKING: _utc_now_iso()}},
                                                   "spec": {"replicas": target}}, _request_timeout=_AS_K8S_TIMEOUT)
        ready = _as_wait_ready(ns, app, 180)
        _as_finish_wake(ns, apps.read_namespaced_deployment(app, ns, _request_timeout=_AS_K8S_TIMEOUT))
        _events.record(ns, "wake", app, "ok" if ready else "fail",
                       {"replicas": target, "source": source,
                        "error": None if ready else "3 分鐘內沒有全部就緒"})
        if source == "traffic":
            _as_notify(ns, app, "☀ 服務已喚醒" if ready else "⚠ 喚醒還沒完成",
                       f"有人連到 {app}，" + (f"已經恢復成 {target} 個副本。" if ready else f"已經恢復成 {target} 個副本，但還沒全部就緒。"))
        return ready, (f"{app} 已經喚醒，{target} 個副本都就緒了。" if ready else
                       f"{app} 已經恢復成 {target} 個副本，但 3 分鐘內還沒全部就緒，請到服務詳情看原因。")
    except Exception as e:
        _events.record(ns, "wake", app, "fail", {"replicas": target, "source": source, "error": str(e)[:300]})
        return False, f"{app} 喚醒失敗：{e}"
    finally:
        _as_release(ns, app)


def _as_waker_hit(ns, app) -> bool:
    core = k8s_client.CoreV1Api()
    for p in core.list_namespaced_pod(ns, label_selector=f"{_sleep.LABEL_WAKER}={app}", _request_timeout=_AS_K8S_TIMEOUT).items:
        try:
            log = core.read_namespaced_pod_log(p.metadata.name, ns, since_seconds=_AS_TICK * 3, tail_lines=50, _request_timeout=_AS_K8S_TIMEOUT)
        except Exception:
            continue
        if _sleep.WAKE_MARK in (log or ""):
            return True
    return False


def _as_notify(ns, app, title, body):
    """分時擴縮／休眠的自動動作推播到手機（沿用 core/push.py；沒有訂閱的帳號什麼都不做）。同一個服務的通知用同一個 tag，新的取代舊的。"""
    try:
        _push.send_async(ns, {"title": title, "body": _push.clean_text(body), "url": "/m#services", "tag": f"autoscale-{app}"})
    except Exception as e:
        print(f"[分時擴縮] 推播失敗：{e}")


def _as_spawn(fn, *args):
    threading.Thread(target=fn, args=args, daemon=True).start()


def _as_tick_wake():
    """每 10 秒：有人連到喚醒器就叫醒；休眠中的服務被手動調過副本數就收尾；沒有主人的喚醒器收掉。"""
    apps = k8s_client.AppsV1Api()
    deps = [d for d in apps.list_deployment_for_all_namespaces(_request_timeout=_AS_K8S_TIMEOUT).items if d.metadata.namespace.startswith("user-")]
    by_key = {(d.metadata.namespace, d.metadata.name): d for d in deps}
    for d in deps:
        ns, name = d.metadata.namespace, d.metadata.name
        if _as_is_waker(d):
            app = d.metadata.labels[_sleep.LABEL_WAKER]
            owner = by_key.get((ns, app))
            if (ns, app) in _as_busy:
                continue
            if owner is None:
                try:
                    apps.delete_namespaced_deployment(name, ns, _request_timeout=_AS_K8S_TIMEOUT)
                except Exception:
                    pass
            elif not (owner.metadata.annotations or {}).get(_sleep.ANN_SLEEP_REPLICAS):
                _as_finish_wake(ns, owner)   # 重新部署過，休眠標記已經不在
            continue
        ann = d.metadata.annotations or {}
        if not ann.get(_sleep.ANN_SLEEP_REPLICAS) or (ns, name) in _as_busy:
            continue
        if (d.spec.replicas or 0) > 0:
            _as_spawn(_as_wake, ns, name, "manual", d.spec.replicas)   # 有人手動調了副本數
        elif _as_waker_hit(ns, name):
            _as_spawn(_as_wake, ns, name, "traffic")


def _as_apply_schedule(ns, app, plan, last_target, now=None, notify=False):
    """套用一個排程。只在「該開幾個」改變的那一刻動作，所以使用者在時段中間手動調整的副本數會保留到下一個時段。
    notify=True：背景自動套用（不是使用者按「套用」），動作後推播到手機。"""
    from guardian.yaml_validator import max_replicas
    target = _ts.replicas_at(plan, now or datetime.now(_ts.TZ))
    if target is None or target == last_target or (ns, app) in _as_busy:
        return None
    target = min(int(target), max_replicas())
    if _is_remote_ns(ns):
        return _as_apply_schedule_remote(ns, app, target, notify)
    apps = k8s_client.AppsV1Api()
    try:
        dep = apps.read_namespaced_deployment(app, ns, _request_timeout=_AS_K8S_TIMEOUT)
    except Exception:
        return None
    if (dep.metadata.annotations or {}).get(_sleep.ANN_SLEEP_REPLICAS):
        # 休眠中不叫醒它，只更新「醒來時要開幾個」
        apps.patch_namespaced_deployment(app, ns, {"metadata": {"annotations": {_sleep.ANN_SLEEP_REPLICAS: str(target)}}}, _request_timeout=_AS_K8S_TIMEOUT)
        _as_store.set_last_target(ns, app, target)
        return target
    cur = int(dep.spec.replicas or 0)
    if cur != target:
        ok, msg, _status = _scale_check(app, target, ns)
        if not ok:
            _events.record(ns, "scale", app, "fail", {"replicas": target, "source": "schedule", "error": msg})
            if notify:
                _as_notify(ns, app, "⚠ 分時擴縮沒有調成", f"{app} 依排程要調成 {target} 個，但{msg}，維持 {cur} 個。")
        else:
            apps.patch_namespaced_deployment_scale(app, ns, {"spec": {"replicas": target}}, _request_timeout=_AS_K8S_TIMEOUT)
            _events.record(ns, "scale", app, "ok", {"replicas": target, "source": "schedule", "from": cur})
            if notify:
                _as_notify(ns, app, "⏱ 分時擴縮", f"{app} 依排程從 {cur} 個調成 {target} 個。")
    _as_store.set_last_target(ns, app, target)
    return target


def _as_apply_schedule_remote(ns, app, target, notify=False):
    """客戶叢集（2026-10-23）：時段切換時把副本數寫進客戶的部署設定 repo（跟手動擴縮 _remote_scale 同一條路），
    客戶叢集的 Flux 拉下來套用。經過同一套 _scale_check，寫事件紀錄，背景自動套用時推播。"""
    dep = _as_remote_dep(ns, app)
    if dep is None:
        return None
    cur = int(dep.spec.replicas or 0)
    if cur != target:
        ok, msg, _status = _scale_check(app, target, ns)
        if ok:
            blocked = _remote_write_guard(ns)
            if blocked:
                ok, msg = False, blocked
        if ok:
            try:
                _tenant_repo(_remote_owner(ns)).set_replicas(app, target)
            except Exception as e:
                ok, msg = False, str(e)[:200]
        if ok:
            _events.record(ns, "scale", app, "ok", {"replicas": target, "source": "schedule", "from": cur, "via": "git"})
            if notify:
                _as_notify(ns, app, "⏱ 分時擴縮", f"{app} 依排程從 {cur} 個調成 {target} 個（已寫入你的部署設定，叢集同步後生效）。")
        else:
            _events.record(ns, "scale", app, "fail", {"replicas": target, "source": "schedule", "error": msg, "via": "git"})
            if notify:
                _as_notify(ns, app, "⚠ 分時擴縮沒有調成", f"{app} 依排程要調成 {target} 個，但{msg}")
    _as_store.set_last_target(ns, app, target)
    return target


def _as_tick_schedules():
    for s in _as_store.all_schedules():
        # 平台叢集（user-）要本機 K8s 連線；客戶叢集（zt-）走 Git，不需要（2026-10-23）
        if (s["namespace"].startswith("user-") and K8S_ENABLED) or _is_remote_ns(s["namespace"]):
            try:
                _as_apply_schedule(s["namespace"], s["app"], s["plan"], s["last_target"], notify=True)
            except Exception as e:
                print(f"[分時擴縮] {s['namespace']}/{s['app']} 套用失敗：{e}")


def _as_prom_url() -> str:
    return os.environ.get("PROMETHEUS_URL", "http://127.0.0.1:9090").replace("localhost", "127.0.0.1").rstrip("/")


def _as_prom_range(q: str, start: float, end: float, step: int):
    r = requests.get(_as_prom_url() + "/api/v1/query_range",
                     params={"query": q, "start": int(start), "end": int(end), "step": int(step)}, timeout=30)
    r.raise_for_status()
    d = r.json()
    if d.get("status") != "success":
        raise RuntimeError(d.get("error") or "Prometheus 查詢失敗")
    return d["data"]["result"]


def _as_prom_query(q: str, at: float = None):
    url = _as_prom_url()
    params = {"query": q}
    if at is not None:
        params["time"] = int(at)
    r = requests.get(url + "/api/v1/query", params=params, timeout=15)
    r.raise_for_status()
    d = r.json()
    if d.get("status") != "success":
        raise RuntimeError(d.get("error") or "Prometheus 查詢失敗")
    return d["data"]["result"]


_AS_TRAFFIC_EVERY = 600            # 每 10 分鐘看一次上一個整點小時存了沒（同一小時重複寫入會取代）
_AS_TRAFFIC_BACKFILL_DAYS = 10     # 第一次啟動時，從 Prometheus 補它現有的資料（它只保留約 10 天）
_AS_TRAFFIC_Q = 'sum by (namespace, pod) (increase(container_network_receive_packets_total{namespace=~"user-.+"}[1h]))'


def _as_traffic_rows(series, apps_by_ns):
    """Prometheus 結果 → [(namespace, app, 整點小時, 封包數, Pod 數)]。查詢時間是那一小時的結束，所以整點小時＝時間 − 3600。"""
    acc = {}
    for s_ in series:
        ns, pod = s_.get("metric", {}).get("namespace"), s_.get("metric", {}).get("pod") or ""
        app = next((a for a in apps_by_ns.get(ns, []) if _sleep.pod_belongs_to(a, pod)), None)
        if not app:
            continue
        values = s_.get("values") or ([s_["value"]] if s_.get("value") else [])
        for ts, v in values:
            try:
                val = float(v)
            except (TypeError, ValueError):
                continue
            key = (ns, app, int(float(ts)) // 3600 * 3600 - 3600)
            tot, n = acc.get(key, (0.0, 0))
            acc[key] = (tot + val, n + 1)
    return [(ns, app, hour, tot, n) for (ns, app, hour), (tot, n) in acc.items()]


def _as_tick_traffic(now=None):
    """2026-10-23：把每個服務每小時收到的封包數存進 autoscale.db（保留 60 天），給「建議時段」用。
    Prometheus 只保留約 10 天、而且沒有永久儲存，所以要自己存。第一次會把 Prometheus 現有的資料補進來。"""
    now = _time.time() if now is None else now
    hour_end = int(now) // 3600 * 3600
    backfilled = _as_store.meta_get("traffic_backfilled")
    if backfilled and _as_store.has_traffic_hour(hour_end - 3600):
        return
    apps_by_ns = {}
    for d in k8s_client.AppsV1Api().list_deployment_for_all_namespaces(_request_timeout=_AS_K8S_TIMEOUT).items:
        if d.metadata.namespace.startswith("user-") and not _as_is_waker(d):
            apps_by_ns.setdefault(d.metadata.namespace, []).append(d.metadata.name)
    if not apps_by_ns:
        return
    if not backfilled:
        series = _as_prom_range(_AS_TRAFFIC_Q, hour_end - _AS_TRAFFIC_BACKFILL_DAYS * 86400, hour_end, 3600)
        n = _as_store.add_traffic(_as_traffic_rows(series, apps_by_ns))
        _as_store.meta_set("traffic_backfilled", str(int(now)))
        print(f"[分時擴縮] 從 Prometheus 補進 {n} 筆每小時流量")
    else:
        _as_store.add_traffic(_as_traffic_rows(_as_prom_query(_AS_TRAFFIC_Q, at=hour_end), apps_by_ns))


def _as_tick_idle():
    """每 10 分鐘：太久沒人用的服務進入休眠。Prometheus 連不上就整個跳過（寧可不休眠）。"""
    import math
    now = _time.time()
    try:
        lowest = float(_as_prom_query("prometheus_tsdb_lowest_timestamp_seconds")[0]["value"][1])
        _as_state["prometheus_ok"] = True
    except Exception:
        _as_state["prometheus_ok"] = False
        return
    apps = k8s_client.AppsV1Api()
    for ns in [n.metadata.name for n in k8s_client.CoreV1Api().list_namespace(_request_timeout=_AS_K8S_TIMEOUT).items if n.metadata.name.startswith("user-")]:
        settings = _as_store.get_settings(ns)
        window = (_AS_IDLE_OVERRIDE if _AS_IDLE_OVERRIDE and (not _AS_IDLE_OVERRIDE_NS or ns in _AS_IDLE_OVERRIDE_NS)
                  else settings["idle_days"] * 86400)
        bucket = _sleep.bucket_for(window)
        notes = {}
        cand = [d for d in apps.list_namespaced_deployment(ns, _request_timeout=_AS_K8S_TIMEOUT).items
                if not _as_is_waker(d) and (d.spec.replicas or 0) > 0
                and not (d.metadata.annotations or {}).get(_sleep.ANN_SLEEP_REPLICAS)]
        for d in cand:
            if d.metadata.name in settings["exempt"]:
                notes[d.metadata.name] = "在例外清單裡，不會休眠"
        cand = [d for d in cand if d.metadata.name not in settings["exempt"]]
        if cand:
            series = {}
            try:
                for key, q in _sleep.traffic_queries(ns, window, bucket).items():
                    series[key] = {}
                    for r in _as_prom_query(q):
                        pod = (r.get("metric") or {}).get("pod")
                        val = float(r["value"][1])
                        if pod and not math.isnan(val):
                            series[key][pod] = val
            except Exception:
                continue
            rs_since = {}
            for rs in apps.list_namespaced_replica_set(ns, _request_timeout=_AS_K8S_TIMEOUT).items:
                for o in rs.metadata.owner_references or []:
                    if o.kind == "Deployment" and rs.metadata.creation_timestamp:
                        rs_since[o.name] = max(rs_since.get(o.name, 0), rs.metadata.creation_timestamp.timestamp())
            for d in cand:
                app = d.metadata.name
                active = max(d.metadata.creation_timestamp.timestamp() if d.metadata.creation_timestamp else now,
                             rs_since.get(app, 0), _as_iso_ts((d.metadata.annotations or {}).get(_sleep.ANN_ACTIVE_SINCE)))
                cs = d.spec.template.spec.containers or []
                expected = _sleep.expected_probe_packets_per_hour(_sleep.probes_of_container(cs[0])) if cs else 0
                mine = {k: {p: v for p, v in s.items() if _sleep.pod_belongs_to(app, p)} for k, s in series.items()}
                idle, why = _sleep.decide_idle(now, active, window, mine["busiest"], mine["typical"], expected, bucket, lowest)
                notes[app] = why
                if idle and (ns, app) not in _as_busy:
                    _as_spawn(_as_sleep, ns, app, why)
        _as_state["idle"][ns] = notes
    _as_state["last_idle_check"] = _utc_now_iso()


_as_last_run = {"schedules": 0.0, "idle": 0.0, "traffic": 0.0}


def _as_run_due(now=None):
    """分時擴縮與休眠的背景工作。2026-10-23 起掛在柏碩的預約背景迴圈 _schedule_loop 上（接手說明：不要第二套排程器），
    由它每 10 秒呼叫一次：喚醒每次都看、套用排程每 30 秒、閒置判斷照 _AS_IDLE_EVERY。K8s 沒連線時跳過。"""
    now = _time.time() if now is None else now
    if not K8S_ENABLED:
        # 平台本機的 K8s 沒連線：喚醒與閒置判斷跳過；客戶叢集的排程走 Git，照樣處理
        if now - _as_last_run["schedules"] >= _AS_SCHEDULE_EVERY:
            _as_last_run["schedules"] = now
            try:
                _as_tick_schedules()
            except Exception as e:
                print(f"[分時擴縮] _as_tick_schedules 失敗：{e}")
        return
    jobs = [(None, _as_tick_wake)]
    if now - _as_last_run["schedules"] >= _AS_SCHEDULE_EVERY:
        _as_last_run["schedules"] = now
        jobs.append(("schedules", _as_tick_schedules))
    if now - _as_last_run["idle"] >= _AS_IDLE_EVERY:
        _as_last_run["idle"] = now
        jobs.append(("idle", _as_tick_idle))
    if now - _as_last_run.get("traffic", 0.0) >= _AS_TRAFFIC_EVERY:
        _as_last_run["traffic"] = now
        jobs.append(("traffic", _as_tick_traffic))
    for _key, fn in jobs:
        try:
            fn()
        except Exception as e:
            print(f"[分時擴縮／休眠] {fn.__name__} 失敗：{e}")


# ── API ──
_AS_REMOTE_NO_SLEEP = ("休眠、喚醒和例外清單目前只支援平台自己的叢集：你的叢集回報的資料裡沒有網路流量，"
                       "平台判斷不出有沒有人在用。分時擴縮可以照常使用。")


def _as_guard(remote_ok=False):
    """remote_ok=True：分時擴縮（查詢、解析、套用、取消）在客戶叢集也能用，副本數寫進客戶的部署設定 repo（2026-10-23）。"""
    if "username" not in session:
        return None, (jsonify({"error": "Not authenticated"}), 401)
    ns = _session_ns()
    if _is_remote_ns(ns):
        if not remote_ok:
            return None, (jsonify({"ok": False, "error": _AS_REMOTE_NO_SLEEP}), 400)
        return ns, None
    if not K8S_ENABLED:
        return None, (jsonify({"ok": False, "error": "Kubernetes 沒有連線。請先確認 Docker Desktop 的 Kubernetes 已經啟動。"}), 503)
    return ns, None


class _AsClusterDown(Exception):
    """叢集沒有回應（逾時、連不上、客戶叢集沒有回報），跟「找不到這個服務」分開講。"""


@app.errorhandler(_AsClusterDown)
def _as_cluster_down(e):
    return jsonify({"ok": False, "error": str(e) or "Kubernetes 叢集沒有回應，請確認 Docker Desktop 的 Kubernetes 正常運作後再試一次。"}), 503


def _as_remote_dep(ns, app):
    """客戶叢集：從回報器的資料組出分時擴縮需要的欄位（跟 Deployment 物件同樣的取法）。沒有回報時丟 _AsClusterDown 並說明原因。"""
    rep = _remote_report(ns)
    if not rep:
        raise _AsClusterDown(_remote_write_guard(ns) or "還沒有收到你叢集的回報，請確認電腦開著、Docker Desktop 正在執行。")
    from types import SimpleNamespace as _NS
    for d in rep.get("deployments") or []:
        if d.get("name") == app and app != "zt-reporter":
            return _NS(metadata=_NS(name=app, annotations={}, labels={}),
                       spec=_NS(replicas=int(d.get("replicas") or 0), template=_NS(spec=_NS(containers=[]))),
                       status=_NS(ready_replicas=int(d.get("ready") or 0)))
    return None


def _as_read_dep(ns, app):
    if _is_remote_ns(ns):
        return _as_remote_dep(ns, app)
    try:
        d = k8s_client.AppsV1Api().read_namespaced_deployment(app, ns, _request_timeout=_AS_K8S_TIMEOUT)
        return None if _as_is_waker(d) else d
    except k8s_client.exceptions.ApiException as e:
        if e.status == 404:
            return None
        raise _AsClusterDown() from e
    except Exception as e:
        raise _AsClusterDown() from e


def _as_name_arg():
    name = str((request.get_json(silent=True) or {}).get("name") or "").strip()
    return name if _PLAUSIBLE_RESOURCE_NAME_RE.match(name or "-") else ""


def _as_not_found(name):
    return jsonify({"ok": False, "error": f"找不到名稱是「{name}」的服務。分時擴縮和休眠要設定在已經部署的服務上："
                                          f"可以先部署它，或說「列出我的服務」看看有哪些。"}), 404


def _as_plan_view(ns, plan, dep):
    now = datetime.now(_ts.TZ)
    nxt = _ts.next_change(plan, now)
    return {"ok": True, "plan": plan, "rows": _ts.describe(plan),
            "savings": _ts.savings(plan, _as_cost_per_replica_hour(dep)),
            "current": int(dep.spec.replicas or 0), "now": now.strftime("%H:%M"),
            "now_target": _ts.replicas_at(plan, now),
            "next": {"at": _as_fmt_when(nxt[0]), "replicas": nxt[1]} if nxt else None,
            "replaces": any(s["app"] == plan["app"] for s in _as_store.get_schedules(ns)),
            "sleeping": bool((dep.metadata.annotations or {}).get(_sleep.ANN_SLEEP_REPLICAS))}


@app.route("/api/autoscale")
def api_autoscale():
    ns, err = _as_guard(remote_ok=True)
    if err:
        return err
    settings = _as_store.get_settings(ns)
    scheds = {s["app"]: s for s in _as_store.get_schedules(ns)}
    now = datetime.now(_ts.TZ)
    items = []
    if _is_remote_ns(ns):
        rep = _remote_report(ns)
        if not rep:
            raise _AsClusterDown(_remote_write_guard(ns) or "還沒有收到你叢集的回報，請確認電腦開著、Docker Desktop 正在執行。")
        for d in _rv.list_deployments(rep):
            row = {"name": d["name"], "replicas": int(d.get("replicas") or 0), "ready": int(d.get("ready") or 0),
                   "sleeping": False, "waking": False, "exempt": False}
            s_ = scheds.get(d["name"])
            if s_:
                nxt = _ts.next_change(s_["plan"], now)
                row["schedule"] = {"rows": _ts.describe(s_["plan"]), "now": _ts.replicas_at(s_["plan"], now),
                                   "next": {"at": _as_fmt_when(nxt[0]), "replicas": nxt[1]} if nxt else None}
            items.append(row)
        items.sort(key=lambda x: x["name"])
        return jsonify({"ok": True, "remote": True, "remote_note": _AS_REMOTE_NO_SLEEP, "items": items,
                        "idle_days": settings["idle_days"]})
    try:
        dep_items = k8s_client.AppsV1Api().list_namespaced_deployment(ns, _request_timeout=_AS_K8S_TIMEOUT).items
    except Exception as e:
        raise _AsClusterDown() from e
    for d in dep_items:
        if _as_is_waker(d):
            continue
        app_name, ann = d.metadata.name, d.metadata.annotations or {}
        sleeping = bool(ann.get(_sleep.ANN_SLEEP_REPLICAS))
        row = {"name": app_name, "replicas": int(d.spec.replicas or 0), "ready": int(d.status.ready_replicas or 0),
               "sleeping": sleeping, "waking": sleeping and (bool(ann.get(_sleep.ANN_WAKING)) or (ns, app_name) in _as_busy),
               "sleep_replicas": int(ann.get(_sleep.ANN_SLEEP_REPLICAS) or 0),
               "sleeping_since": ann.get(_sleep.ANN_SLEEPING_SINCE),
               "exempt": app_name in settings["exempt"], "idle_note": _as_state["idle"].get(ns, {}).get(app_name)}
        if sleeping:
            row["saving_usd"] = round(_as_cost_per_replica_hour(d) * row["sleep_replicas"] * 730, 2)
        s = scheds.get(app_name)
        if s:
            nxt = _ts.next_change(s["plan"], now)
            row["schedule"] = {"rows": _ts.describe(s["plan"]), "now": _ts.replicas_at(s["plan"], now),
                               "next": {"at": _as_fmt_when(nxt[0]), "replicas": nxt[1]} if nxt else None}
        items.append(row)
    items.sort(key=lambda x: (not x["sleeping"], x["name"]))
    return jsonify({"ok": True, "idle_days": settings["idle_days"], "items": items,
                    "prometheus_ok": _as_state["prometheus_ok"], "last_idle_check": _as_state["last_idle_check"],
                    "test_override_seconds": (_AS_IDLE_OVERRIDE or None)
                    if (not _AS_IDLE_OVERRIDE_NS or ns in _AS_IDLE_OVERRIDE_NS) else None})


@app.route("/api/autoscale/parse", methods=["POST"])
def api_autoscale_parse():
    ns, err = _as_guard(remote_ok=True)
    if err:
        return err
    from guardian.yaml_validator import max_replicas
    r = _ts.parse_schedule(str((request.get_json(silent=True) or {}).get("text") or "")[:300])
    if not r["ok"]:
        return jsonify(r)
    plan = r["plan"]
    e = _ts.validate_plan(plan, max_replicas())
    if e:
        return jsonify({"ok": False, "error": e})
    dep = _as_read_dep(ns, plan["app"])
    if not dep:
        return _as_not_found(plan["app"])
    return jsonify(_as_plan_view(ns, plan, dep))


@app.route("/api/autoscale/suggest")
def api_autoscale_suggest():
    """2026-10-23：從過去的每小時流量學出建議的時段（agents/timeslot_learn.py，固定規則）。只是建議，使用者在確認卡按「套用」才生效。"""
    ns, err = _as_guard(remote_ok=True)
    if err:
        return err
    if _is_remote_ns(ns):
        return jsonify({"ok": False, "error": "你的叢集回報的資料裡沒有網路流量，沒辦法從流量建議時段；可以直接設定時段，例如「白天 3 個、晚上 1 個」。"}), 400
    name = str(request.args.get("name") or "").strip()
    if not _PLAUSIBLE_RESOURCE_NAME_RE.match(name or "-"):
        return jsonify({"ok": False, "error": "服務名稱的格式不對。"}), 400
    dep = _as_read_dep(ns, name)
    if not dep:
        return _as_not_found(name)
    from guardian.yaml_validator import max_replicas
    from agents.timeslot_learn import learn_schedule
    cs = dep.spec.template.spec.containers or []
    base = _sleep.expected_probe_packets_per_hour(_sleep.probes_of_container(cs[0])) if cs else 0
    ann = dep.metadata.annotations or {}
    cur = int(dep.spec.replicas or 0) or int(ann.get(_sleep.ANN_SLEEP_REPLICAS) or 0)
    r = learn_schedule(name, _as_store.traffic_for(ns, name, since=_time.time() - _as_store.TRAFFIC_KEEP_DAYS * 86400),
                       cur, baseline_per_pod=base or None, max_replicas=max_replicas())
    if not r["ok"]:
        return jsonify({"ok": False, "error": r["error"], "reason": r["reason"], "days": r.get("days"), "need": r.get("need")})
    return jsonify({**_as_plan_view(ns, r["plan"], dep), "explain": r["explain"], "days": r["days"]})


@app.route("/api/autoscale/schedule", methods=["POST"])
def api_autoscale_schedule():
    ns, err = _as_guard(remote_ok=True)
    if err:
        return err
    from guardian.yaml_validator import max_replicas
    plan = (request.get_json(silent=True) or {}).get("plan")
    e = _ts.validate_plan(plan, max_replicas())
    if e:
        return jsonify({"ok": False, "error": e}), 400
    # 只留認得的欄位（前端送回來的資料不能直接相信）
    plan = {"app": plan["app"], "base": plan.get("base"),
            "windows": [{"start": w["start"], "end": w["end"], "replicas": w["replicas"], "days": sorted(set(w["days"]))}
                        for w in plan["windows"]]}
    dep = _as_read_dep(ns, plan["app"])
    if not dep:
        return _as_not_found(plan["app"])
    peak = _ts.savings(plan, 0)["peak"]
    blocked, msg = _check_scale_risk(plan["app"], peak, ns)
    if blocked:
        return jsonify({"ok": False, "error": f"這個排程最多要開 {peak} 個副本，但是{msg}"}), 409
    _as_store.save_schedule(ns, plan)
    now = datetime.now(_ts.TZ)
    target = _as_apply_schedule(ns, plan["app"], plan, None, now)
    sleeping = bool((dep.metadata.annotations or {}).get(_sleep.ANN_SLEEP_REPLICAS))
    if _is_remote_ns(ns):
        return jsonify({"ok": True, "message": f"已經設定好 {plan['app']} 的分時擴縮。現在 {now.strftime('%H:%M')}，目前應該開 {target} 個副本，"
                        "已經寫入你的部署設定，你的叢集同步後（通常 1 分鐘內）就會生效。之後每個時段切換時都會自動寫入。"})
    return jsonify({"ok": True, "message": f"已經設定好 {plan['app']} 的分時擴縮。現在 {now.strftime('%H:%M')}，"
                    + (f"它正在休眠，醒來時會開 {target} 個副本。" if sleeping else f"目前應該開 {target} 個副本，已經調整好了。")})


@app.route("/api/autoscale/cancel", methods=["POST"])
def api_autoscale_cancel():
    ns, err = _as_guard(remote_ok=True)
    if err:
        return err
    name = _as_name_arg()
    if not _as_store.delete_schedule(ns, name):
        return jsonify({"ok": False, "error": f"「{name}」沒有設定分時擴縮。"}), 404
    return jsonify({"ok": True, "message": f"已經取消 {name} 的分時擴縮，副本數維持現在的數字，之後不會再自動調整。"})


@app.route("/api/autoscale/wake", methods=["POST"])
def api_autoscale_wake():
    ns, err = _as_guard()
    if err:
        return err
    name = _as_name_arg()
    dep = _as_read_dep(ns, name)
    if not dep:
        return _as_not_found(name)
    if not (dep.metadata.annotations or {}).get(_sleep.ANN_SLEEP_REPLICAS):
        return jsonify({"ok": True, "message": f"{name} 沒有在休眠，正常運作中。"})
    if (ns, name) in _as_busy:
        return jsonify({"ok": True, "message": f"{name} 已經在喚醒中，通常 30 秒到 1 分鐘就會恢復。"})
    _as_spawn(_as_wake, ns, name, "manual")
    return jsonify({"ok": True, "message": f"正在喚醒 {name}，通常 30 秒到 1 分鐘就會恢復。可以說「休眠設定」看進度。"})


@app.route("/api/autoscale/exempt", methods=["POST"])
def api_autoscale_exempt():
    ns, err = _as_guard()
    if err:
        return err
    name = _as_name_arg()
    on = bool((request.get_json(silent=True) or {}).get("exempt", True))
    if not _as_read_dep(ns, name):
        return _as_not_found(name)
    _as_store.set_exempt(ns, name, on)
    return jsonify({"ok": True, "message": (f"已經把 {name} 加進例外清單，它不會因為沒人使用而休眠。" if on else
                                             f"已經把 {name} 移出例外清單，太久沒人使用時會自動休眠。")})


@app.route("/api/autoscale/settings", methods=["POST"])
def api_autoscale_settings():
    ns, err = _as_guard()
    if err:
        return err
    try:
        days = int((request.get_json(silent=True) or {}).get("idle_days"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "天數要是整數。"}), 400
    if not _as_store.MIN_IDLE_DAYS <= days <= _as_store.MAX_IDLE_DAYS:
        return jsonify({"ok": False, "error": f"天數要在 {_as_store.MIN_IDLE_DAYS} 到 {_as_store.MAX_IDLE_DAYS} 天之間。"
                                              f"平台的監控只保留大約 10 天的流量紀錄，天數再長就判斷不出來有沒有人用。"}), 400
    _as_store.set_idle_days(ns, days)
    return jsonify({"ok": True, "message": f"已經改成：服務連續 {days} 天沒人使用就自動休眠。"})


# ══════════════════════════════════════════════════════════════════
#  對話存伺服器＋專案（2026-10-21）：資料在 core/workspace_store.py（data/workspace.db）。
#  對話、專案、檔案綁登入帳號（owner），服務歸屬綁 namespace；不做共享，讀寫只碰自己的資料。
# ══════════════════════════════════════════════════════════════════
from core import workspace_store as _ws


def _ws_guard():
    if "username" not in session:
        return None, None, (jsonify({"error": "Not authenticated"}), 401)
    return session["username"], _session_ns(), None


def _ws_assign_after_deploy(project_id, app_name, ns):
    """部署成功後把服務歸到專案（專案不是自己的就忽略，不影響部署結果）。"""
    if not project_id or not app_name or not has_request_context() or "username" not in session:
        return
    try:
        if _ws.get_project(session["username"], project_id):
            _ws.assign_service(ns, app_name, session["username"], project_id)
    except Exception as e:
        print(f"[project] 部署後歸到專案失敗：{e}")


def _ws_save_upload(project_id, filename, fmt, text):
    try:
        f = _ws.add_file(session["username"], str(project_id), filename or "upload.yaml", fmt, text)
        return {"ok": True, "name": f["name"]}
    except _ws.WorkspaceError as e:
        return {"ok": False, "error": str(e)}


def _ws_project_instructions(project_id) -> str:
    if not project_id or "username" not in session:
        return ""
    p = _ws.get_project(session["username"], str(project_id))
    return (p or {}).get("instructions") or ""


def _ws_err(e, status=400):
    return jsonify({"ok": False, "error": str(e)}), status


# ── 對話 ──
@app.route("/api/chats")
def api_chats_list():
    owner, _, err = _ws_guard()
    if err:
        return err
    return jsonify({"ok": True, "chats": _ws.list_chats(owner)})


@app.route("/api/chats/<chat_id>", methods=["PUT", "DELETE"])
def api_chat_put(chat_id):
    owner, _, err = _ws_guard()
    if err:
        return err
    if request.method == "DELETE":
        _ws.delete_chat(owner, chat_id)
        return jsonify({"ok": True})
    if (request.content_length or 0) > _ws.MAX_CHAT_BYTES * 2:
        return _ws_err("這則對話太長了，沒辦法再存。請開一則新對話繼續。", 413)
    body = request.get_json(silent=True) or {}
    try:
        _ws.put_chat(owner, chat_id, body.get("title"), body.get("messages"), body.get("projectId"))
    except _ws.WorkspaceError as e:
        return _ws_err(e)
    return jsonify({"ok": True})


# ── 專案 ──
def _ws_project_view(owner, ns, p):
    apps = _ws.services_of(ns, owner, p["id"])
    live = {d["name"]: d for d in (k8s_get_deployments(ns) or [])}
    services = [{"name": a, "exists": a in live,
                 "replicas": live.get(a, {}).get("replicas"), "ready": live.get(a, {}).get("ready"),
                 "sleeping": live.get(a, {}).get("sleeping", False)} for a in apps]
    owned = _ws.service_projects(ns, owner)
    available = [d["name"] for d in live.values() if d["name"] not in owned]
    return {**p, "chats": _ws.chats_of(owner, p["id"]), "services": services,
            "available_services": sorted(available), "files": _ws.list_files(owner, p["id"])}


@app.route("/api/projects", methods=["GET", "POST"])
def api_projects():
    owner, _, err = _ws_guard()
    if err:
        return err
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        try:
            p = _ws.create_project(owner, body.get("name"), body.get("description") or "")
        except _ws.WorkspaceError as e:
            return _ws_err(e)
        return jsonify({"ok": True, "project": p})
    return jsonify({"ok": True, "projects": _ws.list_projects(owner)})


@app.route("/api/projects/<pid>", methods=["GET", "PATCH"])
def api_project(pid):
    owner, ns, err = _ws_guard()
    if err:
        return err
    p = _ws.get_project(owner, pid)
    if not p:
        return _ws_err("找不到這個專案，可能已經被刪除了。", 404)
    if request.method == "PATCH":
        body = request.get_json(silent=True) or {}
        try:
            p = _ws.update_project(owner, pid, body.get("name"), body.get("description"), body.get("instructions"))
        except _ws.WorkspaceError as e:
            return _ws_err(e)
    return jsonify({"ok": True, "project": _ws_project_view(owner, ns, p)})


@app.route("/api/projects/<pid>/services", methods=["POST"])
def api_project_services(pid):
    owner, ns, err = _ws_guard()
    if err:
        return err
    if not _ws.get_project(owner, pid):
        return _ws_err("找不到這個專案，可能已經被刪除了。", 404)
    body = request.get_json(silent=True) or {}
    name, action = str(body.get("name") or "").strip(), body.get("action")
    if action == "remove":
        if _ws.service_projects(ns, owner).get(name) != pid:
            return _ws_err(f"「{name}」不在這個專案裡。", 404)
        _ws.unassign_service(ns, name, owner)
        return jsonify({"ok": True, "message": f"已經把 {name} 移出專案，服務照常運作。"})
    if action != "add":
        return _ws_err("不認得的動作。")
    if name not in {d["name"] for d in (k8s_get_deployments(ns) or [])}:
        return _ws_err(f"找不到服務「{name}」。", 404)
    other = _ws.service_projects(ns, owner).get(name)
    if other and other != pid:
        q = _ws.get_project(owner, other)
        return _ws_err(f"「{name}」已經在專案「{(q or {}).get('name', '')}」裡，一個服務只能屬於一個專案。請先從那個專案移出。", 409)
    _ws.assign_service(ns, name, owner, pid)
    return jsonify({"ok": True, "message": f"已經把 {name} 移進專案。"})


@app.route("/api/projects/<pid>/summary")
def api_project_summary(pid):
    """2026-10-23：專案摘要。只算這個專案裡的服務：事件紀錄依 app 過濾後交給 agents/report.build_report（固定規則，不用 AI）。"""
    owner, ns, err = _ws_guard()
    if err:
        return err
    p = _ws.get_project(owner, pid)
    if not p:
        return _ws_err("找不到這個專案，可能已經被刪除了。", 404)
    from agents.report import build_report
    try:
        days = max(1, min(30, int(request.args.get("days") or 7)))
    except ValueError:
        days = 7
    apps = set(_ws.services_of(ns, owner, pid))
    events = [e for e in _events.query(ns, since=_time.time() - days * 86400) if e.get("app") in apps] if apps else []
    r = build_report(events, days)
    view = _ws_project_view(owner, ns, p)
    svcs = view["services"]
    ops = r["ops"]
    scheds = {s_["app"]: s_ for s_ in _as_store.get_schedules(ns) if s_["app"] in apps}
    now = datetime.now(_ts.TZ)
    sched_rows = []
    for app_name, s_ in sorted(scheds.items()):
        nxt = _ts.next_change(s_["plan"], now)
        sched_rows.append({"app": app_name, "rows": _ts.describe(s_["plan"]),
                           "next": {"at": _as_fmt_when(nxt[0]), "replicas": nxt[1]} if nxt else None})
    return jsonify({"ok": True, "days": days, "has_data": r["has_data"],
                    "services": {"total": len(svcs),
                                 "ready": sum(1 for x in svcs if x["exists"] and not x["sleeping"] and x["replicas"] and x["ready"] == x["replicas"]),
                                 "sleeping": sum(1 for x in svcs if x["sleeping"]),
                                 "attention": sum(1 for x in svcs if not x["exists"] or (not x["sleeping"] and x["ready"] != x["replicas"]))},
                    "deploys": r["deploys"]["total"], "deploys_failed": r["deploys"]["failed"],
                    "changes": ops["scale"]["total"] + ops["update"]["total"] + ops["rollback"]["total"],
                    "heals": r["heals"]["total"], "heals_need_you": r["heals"]["needs_you"],
                    "blocked": r["reviews"]["block"], "days_without_incident": r["incidents"]["days_without_incident"],
                    "recent": r["recent"][:10], "schedules": sched_rows})


@app.route("/api/projects/<pid>/files/<fid>", methods=["GET", "DELETE"])
def api_project_file(pid, fid):
    owner, _, err = _ws_guard()
    if err:
        return err
    if request.method == "DELETE":
        if not _ws.delete_file(owner, pid, fid):
            return _ws_err("找不到這個檔案。", 404)
        return jsonify({"ok": True})
    f = _ws.get_file(owner, pid, fid)
    if not f:
        return _ws_err("找不到這個檔案。", 404)
    from urllib.parse import quote
    return app.response_class(f["content"], mimetype="text/yaml; charset=utf-8", headers={
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(f['name'])}",
        "X-Content-Type-Options": "nosniff"})


@app.route("/api/projects/<pid>/delete", methods=["POST"])
def api_project_delete(pid):
    """刪除專案（使用者設計的勾選卡片）：remove 是要刪掉的部分；rest＝move 時其餘移出並刪除空專案，
    rest＝keep 時其餘留在專案裡、專案保留。有服務刪除失敗時不刪專案本身。部署紀錄與事件紀錄一律保留。"""
    owner, ns, err = _ws_guard()
    if err:
        return err
    p = _ws.get_project(owner, pid)
    if not p:
        return _ws_err("找不到這個專案，可能已經被刪除了。", 404)
    body = request.get_json(silent=True) or {}
    rm = body.get("remove") or {}
    everything = bool(rm.get("all"))
    rm = {k: everything or bool(rm.get(k)) for k in ("chats", "services", "files", "instructions")}
    rest = "move" if everything else body.get("rest")
    if rest not in ("move", "keep"):
        return _ws_err("請選擇未勾選的部分要移出專案，還是保留在專案中。")
    if rest == "keep" and not any(rm.values()):
        return _ws_err("沒有勾選任何要移除的部分。")
    apps = _ws.services_of(ns, owner, pid)
    if rm["services"] and apps and str(body.get("confirm_name") or "").strip() != p["name"]:
        return _ws_err("要刪除服務，請輸入正確的專案名稱確認。")

    done, failed = [], []
    if rm["services"]:
        for a in apps:
            ok, msg = k8s_delete_deployment(a, ns)
            (done if ok else failed).append(a if ok else f"{a}（{msg}）")
    if rm["chats"]:
        _ws.delete_chats_of(owner, pid)
    if rm["files"]:
        _ws.delete_files_of(owner, pid)
    if rm["instructions"]:
        _ws.update_project(owner, pid, instructions="")
    if failed:
        return jsonify({"ok": False, "project_deleted": False, "deleted_services": done,
                        "error": "這些服務沒有刪掉：" + "、".join(failed)
                                 + "。專案先保留，請確認 Kubernetes 連線正常後再試一次。"}), 502
    deleted_project = rest == "move"
    if deleted_project:
        if not rm["chats"]:
            _ws.move_chats_out(owner, pid)
        if not rm["services"]:
            for a in apps:
                _ws.unassign_service(ns, a, owner)
        _ws.delete_project_row(owner, pid)
    return jsonify({"ok": True, "project_deleted": deleted_project, "deleted_services": done})


# ══════════════════════════════════════════════════════════════════
# 手機 App（PWA）與推播通知（2026-10-22）
# 加到手機主畫面後是全螢幕的 App；Healer 處理過問題就推播通知，點通知打開那個服務的詳情。
# service worker 刻意不快取任何 API 或頁面，避免手機上把舊的叢集狀態當成現況；
# 只快取一份離線說明頁，主機關機時至少講清楚發生什麼事。
# ══════════════════════════════════════════════════════════════════
from flask import send_from_directory, Response
from core import push as _push

_push.init()
_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
_THEME_COLOR = "#0B1622"

_SW_JS = r"""// ZeroTouch service worker：只負責離線說明頁與推播通知，不快取任何資料
const OFFLINE_CACHE = 'zt-offline-v1';
const OFFLINE_URL = '/offline';

self.addEventListener('install', e => {
  e.waitUntil(caches.open(OFFLINE_CACHE).then(c => c.add(new Request(OFFLINE_URL, {cache: 'reload'}))));
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(caches.keys()
    .then(ks => Promise.all(ks.filter(k => k !== OFFLINE_CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});

// 只處理「打開頁面」：連不上，或通道回 502/503/504（主機關機時 Tailscale Funnel 會這樣回）就顯示離線說明
self.addEventListener('fetch', e => {
  if (e.request.mode !== 'navigate') return;
  e.respondWith(fetch(e.request).then(r => {
    if ([502, 503, 504].includes(r.status)) return caches.match(OFFLINE_URL).then(o => o || r);
    return r;
  }).catch(() => caches.match(OFFLINE_URL)));
});

self.addEventListener('push', e => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (x) { d = {body: e.data ? e.data.text() : ''}; }
  e.waitUntil(self.registration.showNotification(d.title || 'ZeroTouch', {
    body: d.body || '', tag: d.tag || undefined, renotify: !!d.tag,
    icon: '/icons/icon-192.png', badge: '/icons/badge-96.png',
    data: {url: (typeof d.url === 'string' && d.url.startsWith('/')) ? d.url : '/?page=healer'},
  }));
});

self.addEventListener('notificationclick', e => {
  e.notification.close();
  const url = new URL(e.notification.data.url, self.location.origin).href;
  e.waitUntil(clients.matchAll({type: 'window', includeUncontrolled: true}).then(ws => {
    for (const w of ws) {
      if (new URL(w.url).origin === self.location.origin && 'navigate' in w) {
        return w.navigate(url).then(x => (x || w).focus());
      }
    }
    return clients.openWindow(url);
  }));
});
"""

_OFFLINE_HTML = """<!DOCTYPE html>
<html lang="zh-TW"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ZeroTouch｜平台離線</title><meta name="theme-color" content="#0B1622">
<style>
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:#0B1622;color:#DCE8F0;
font-family:"Noto Sans TC",system-ui,sans-serif;padding:16px;box-sizing:border-box}
main{max-width:440px;border:1px solid #28506B;background:#0F2030;border-radius:12px;padding:24px}
h1{font-size:20px;margin:0 0 12px;color:#F5B547}p{line-height:1.7;margin:0 0 12px;color:#93AABB}
ul{padding-left:20px;color:#93AABB;line-height:1.7}button{margin-top:8px;width:100%;padding:12px;border:0;border-radius:8px;
background:#5CC8FF;color:#0B1622;font-size:16px;font-weight:700}
</style></head><body><main>
<h1>平台目前連不上</h1>
<p>手機連到了網路，但 ZeroTouch 平台沒有回應。最常見的原因是平台的主機關機或重新開機中，通常幾分鐘內會自動恢復。</p>
<ul><li>你叢集裡已經部署的服務<b>照常運作</b>，不受影響。</li>
<li>平台離線期間 Healer 不會檢查，也<b>不會送通知</b>，沒收到通知不代表一切正常。</li></ul>
<p lang="en">The platform is unreachable, most likely because its host is off or restarting. Your deployed services keep running; Healer checks and notifications resume when the platform is back.</p>
<button onclick="location.reload()">重新整理 / Retry</button>
</main></body></html>"""


@app.route("/manifest.webmanifest")
def pwa_manifest():
    manifest = {
        "name": "ZeroTouch 部署平台", "short_name": "ZeroTouch",
        "description": "用一句話部署服務，Healer 自動修復並通知你",
        "id": "/", "start_url": "/m", "scope": "/", "display": "standalone",
        "background_color": _THEME_COLOR, "theme_color": _THEME_COLOR, "lang": "zh-TW",
        "icons": [
            {"src": "/icons/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": "/icons/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
            {"src": "/icons/maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
    }
    return Response(json.dumps(manifest, ensure_ascii=False), mimetype="application/manifest+json")


@app.route("/sw.js")
def pwa_service_worker():
    return Response(_SW_JS, mimetype="text/javascript",
                    headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


@app.route("/offline")
def pwa_offline():
    return Response(_OFFLINE_HTML, mimetype="text/html")


@app.route("/icons/<path:name>")
def pwa_icon(name):
    return send_from_directory(os.path.join(_STATIC_DIR, "icons"), name, max_age=86400)


@app.route("/favicon.ico")
def favicon():
    return send_from_directory(_STATIC_DIR, "favicon.ico", max_age=86400)


@app.route("/api/push/status")
def api_push_status():
    if "username" not in session:
        return jsonify({"error": "Not authenticated"}), 401
    key = _push.public_key()
    try:
        import pywebpush  # noqa: F401
    except Exception:
        key = None
    return jsonify({"available": bool(key), "public_key": key,
                    "devices": _push.count_for_user(session["username"]),
                    "max_devices": _push.MAX_DEVICES_PER_USER})


@app.route("/api/push/subscribe", methods=["POST"])
def api_push_subscribe():
    if "username" not in session:
        return jsonify({"error": "Not authenticated"}), 401
    body = request.get_json(silent=True) or {}
    err = _push.add(session["username"], _session_ns(), body.get("subscription"),
                    request.headers.get("User-Agent", ""))
    if err:
        return jsonify({"ok": False, "error": err}), 400
    return jsonify({"ok": True, "devices": _push.count_for_user(session["username"])})


@app.route("/api/push/unsubscribe", methods=["POST"])
def api_push_unsubscribe():
    if "username" not in session:
        return jsonify({"error": "Not authenticated"}), 401
    endpoint = str((request.get_json(silent=True) or {}).get("endpoint") or "")
    return jsonify({"ok": True, "removed": _push.remove(session["username"], endpoint)})


@app.route("/api/push/test", methods=["POST"])
def api_push_test():
    """只送給「這台裝置」：同一個帳號的其他裝置不會被測試通知吵到。"""
    if "username" not in session:
        return jsonify({"error": "Not authenticated"}), 401
    endpoint = str((request.get_json(silent=True) or {}).get("endpoint") or "")
    sub = _push.subscription(session["username"], endpoint)
    if not sub:
        return jsonify({"ok": False, "error": "這台裝置還沒開啟通知，請先按「開啟通知」。"}), 400
    r = _push.send_one(sub, {"title": "🔔 ZeroTouch 通知已開啟",
                             "body": "之後 Healer 發現問題或修好時，會用這種方式通知你。",
                             "url": "/?page=healer", "tag": "zt-test"})
    if r == "sent":
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "推播服務說這台裝置的訂閱已經失效，請重新開啟通知。" if r == "removed"
                    else "推播服務暫時沒有回應，請稍後再按一次「送測試通知」。"}), 502


# ══════════════════════════════════════════════════════════════════
# 預約任務與行事曆（2026-10-23）
# 預約到期**不自動執行**：背景檢查把到期的預約改成「等你確認」並推播到手機，使用者在電腦上按
# 「確定執行」才會呼叫跟手動操作完全相同的 API（部署會重新審查一次）。設計見 core/schedule_store.py。
# ══════════════════════════════════════════════════════════════════
from core import schedule_store as _sched
from core.schedule_time import parse_when as _parse_when, fmt_local as _fmt_local, TW as _TW

_sched.init()
_K8S_NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")


def _sched_task_view(t: dict) -> dict:
    """給前端的預約資料：時間一律附上台北時間的顯示字串。"""
    return {"id": t["id"], "kind": t["kind"], "args": t["args"], "summary": t["summary"], "input": t.get("input") or "",
            "run_at": t["run_at"], "display": _fmt_local(datetime.fromtimestamp(t["run_at"], timezone.utc)),
            "status": t["status"], "result": t.get("result"), "created_at": t["created_at"]}


def _sched_validate(data: dict):
    """回傳 (kind, args, summary, 錯誤訊息)。部署會先跑一次完整審查，沒過就不能預約。"""
    kind = data.get("kind")
    if kind == "deploy":
        spec = data.get("spec")
        if not isinstance(spec, dict) or len(json.dumps(spec)) > 8000:
            return kind, None, None, "部署規格不正確，請重新描述一次要部署什麼。"
        text = (data.get("text") or "").strip() or _synth_input_from_spec(spec)
        parsed, enriched, review, error = _prepare_deploy(text, spec)
        if error:
            payload, _status = error
            return kind, None, None, payload.get("error") or "審查沒有通過，不能預約。"
        if (review or {}).get("decision") == "block":
            reasons = "；".join(str(b) for b in (review.get("blockers") or [])[:3])
            return kind, None, None, "審查沒有通過，不能預約" + (f"：{reasons}" if reasons else "。")
        spec = {k: v for k, v in spec.items() if not str(k).startswith("_")}
        summary = f"部署 {parsed.get('app_name')}（{parsed.get('image')}，{parsed.get('pods')} 個副本）"
        return kind, {"spec": spec}, summary, None
    name = str(data.get("name") or "").strip()
    if kind not in ("scale", "update_image", "delete"):
        return kind, None, None, "只能預約部署、擴縮、換映像檔、刪除這四種操作。"
    if not _K8S_NAME_RE.match(name) or len(name) > 63:
        return kind, None, None, "服務名稱不正確。"
    if kind == "scale":
        from guardian.yaml_validator import max_replicas
        try:
            replicas = int(data.get("replicas"))
        except (TypeError, ValueError):
            return kind, None, None, "副本數要是整數。"
        if not 1 <= replicas <= max_replicas():
            return kind, None, None, f"副本數要在 1 到 {max_replicas()} 之間。"
        return kind, {"name": name, "replicas": replicas}, f"把 {name} 調成 {replicas} 個副本", None
    if kind == "update_image":
        image = str(data.get("image") or "").strip()
        from guardian.yaml_validator import check_image
        errs = check_image(image) if image else ["請指定新的映像檔。"]
        if errs:
            return kind, None, None, "；".join(errs)
        return kind, {"name": name, "image": image}, f"把 {name} 的映像檔換成 {image}", None
    return kind, {"name": name}, f"刪除服務 {name}", None


@app.route("/api/schedule/when", methods=["POST"])
def api_schedule_when():
    if "username" not in session:
        return jsonify({"error": "Not authenticated"}), 401
    text = str((request.get_json(silent=True) or {}).get("text") or "")[:500]
    return jsonify(_parse_when(text))


@app.route("/api/schedule", methods=["GET", "POST"])
def api_schedule():
    if "username" not in session:
        return jsonify({"error": "Not authenticated"}), 401
    user = session["username"]
    if request.method == "GET":
        which = request.args.get("status", "all")
        statuses = {"active": _sched.ACTIVE, "due": ("due",), "final": _sched.FINAL}.get(which)
        return jsonify({"tasks": [_sched_task_view(t) for t in _sched.list_tasks(user, statuses)]})
    data = request.get_json(silent=True) or {}
    try:
        run_at = float(data.get("run_at"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "請選擇預約時間。"}), 400
    try:
        _sched.check_time(run_at)
    except _sched.ScheduleError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    kind, args, summary, err = _sched_validate(data)
    if err:
        return jsonify({"ok": False, "error": err}), 400
    try:
        task = _sched.create(user, _session_ns(), kind, args, summary, run_at, data.get("text") or "")
    except _sched.ScheduleError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "task": _sched_task_view(task)})


@app.route("/api/schedule/due")
def api_schedule_due():
    if "username" not in session:
        return jsonify({"error": "Not authenticated"}), 401
    return jsonify({"tasks": [_sched_task_view(t) for t in _sched.due_for(session["username"])]})


@app.route("/api/schedule/<int:task_id>/<op>", methods=["POST"])
def api_schedule_op(task_id, op):
    if "username" not in session:
        return jsonify({"error": "Not authenticated"}), 401
    user = session["username"]
    data = request.get_json(silent=True) or {}
    try:
        if op == "cancel":
            t = _sched.cancel(user, task_id)
        elif op == "snooze":
            t = _sched.snooze(user, task_id)
        elif op == "reschedule":
            t = _sched.reschedule(user, task_id, float(data.get("run_at")))
        elif op == "start":
            t = _sched.start(user, task_id)
        elif op == "done":
            t = _sched.finish(user, task_id, str(data.get("status")), str(data.get("result") or ""))
        else:
            return jsonify({"ok": False, "error": "不支援的操作。"}), 404
    except (_sched.ScheduleError, TypeError, ValueError) as e:
        return jsonify({"ok": False, "error": str(e) if isinstance(e, _sched.ScheduleError) else "時間格式不正確。"}), 400
    if not t:
        cur = _sched.get(user, task_id)
        if not cur:
            return jsonify({"ok": False, "error": "找不到這個預約。"}), 404
        label = {"running": "正在執行", "done": "已經執行完成", "failed": "已經執行過（失敗）", "blocked": "已經執行過（被擋下）",
                 "cancelled": "已經取消", "expired": "已經過期", "scheduled": "還沒到期", "due": "等你確認"}.get(cur["status"], cur["status"])
        verb = {"cancel": "取消", "snooze": "延後提醒", "reschedule": "改時間", "start": "執行", "done": "回報結果"}.get(op, "操作")
        return jsonify({"ok": False, "error": f"這個預約{label}，不能再{verb}。", "task": _sched_task_view(cur)}), 409
    return jsonify({"ok": True, "task": _sched_task_view(t)})


_CAL_EVENT_LABEL = {"deploy": "部署", "scale": "擴縮", "update": "換版本", "delete": "刪除", "rollback": "回滾", "heal": "自動修復",
                    "sleep": "休眠", "wake": "喚醒"}   # 2026-10-23：分時擴縮的休眠、喚醒也顯示在行事曆


@app.route("/api/calendar")
def api_calendar():
    """行事曆：一個月的預約、平台紀錄到的事件、備註。手機版唯讀也可以看。"""
    if "username" not in session:
        return jsonify({"error": "Not authenticated"}), 401
    user = session["username"]
    month = request.args.get("month") or datetime.now(_TW).strftime("%Y-%m")
    if not re.fullmatch(r"\d{4}-\d{2}", month):
        return jsonify({"error": "month 格式要像 2026-10"}), 400
    y, m = int(month[:4]), int(month[5:])
    if not 1 <= m <= 12:
        return jsonify({"error": "月份不正確"}), 400
    start = datetime(y, m, 1, tzinfo=_TW)
    end = datetime(y + (m == 12), m % 12 + 1, 1, tzinfo=_TW)
    days = {}

    def day_of(ts):
        return datetime.fromtimestamp(ts, _TW).strftime("%Y-%m-%d")

    def slot(d):
        return days.setdefault(d, {"tasks": [], "events": [], "note": ""})

    for t in _sched.list_tasks(user, since=start.timestamp(), until=end.timestamp()):
        slot(day_of(t["run_at"]))["tasks"].append(_sched_task_view(t))
    try:
        for e in _events.query(_session_ns(), since=start.timestamp()):
            if e["ts"] >= end.timestamp():
                continue
            kind, outcome = e.get("kind"), e.get("outcome")
            if kind == "review":
                if outcome != "block":
                    continue
                label = "審查擋下"
            elif kind in _CAL_EVENT_LABEL:
                label = _CAL_EVENT_LABEL[kind]
            else:
                continue
            d = e.get("data") or {}
            evs = slot(day_of(e["ts"]))["events"]
            if len(evs) < 40:
                evs.append({"ts": e["ts"], "kind": kind, "label": label, "app": e.get("app"), "outcome": outcome,
                            "detail": str(d.get("root_cause") or d.get("action_label") or d.get("reason") or "")[:120]})
    except Exception as ex:
        print(f"[行事曆] 讀取事件紀錄失敗：{ex}")
    for d, text in _sched.notes_between(user, start.strftime("%Y-%m-%d"), (end - timedelta(days=1)).strftime("%Y-%m-%d")).items():
        slot(d)["note"] = text
    return jsonify({"month": month, "today": datetime.now(_TW).strftime("%Y-%m-%d"), "days": days})


@app.route("/api/calendar/note", methods=["POST"])
def api_calendar_note():
    if "username" not in session:
        return jsonify({"error": "Not authenticated"}), 401
    data = request.get_json(silent=True) or {}
    day = str(data.get("day") or "")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        return jsonify({"ok": False, "error": "日期格式不正確。"}), 400
    _sched.set_note(session["username"], day, str(data.get("text") or ""))
    return jsonify({"ok": True})


def _bg_tick(now, last_due, interval=30):
    """背景迴圈醒來一次要做的事：預約到期檢查（每 interval 秒）＋分時擴縮與休眠（_as_run_due）。回傳這次之後的 last_due。"""
    if now - last_due >= interval:
        last_due = now
        try:
            for t in _sched.collect_due():
                _push.send_async(t["namespace"], {
                    "title": "⏰ 預約時間到了",
                    "body": _push.clean_text(f"{t['summary']}。請用電腦打開平台，確認後才會執行。"),
                    "url": "/m#calendar", "tag": f"sched-{t['id']}"})
        except Exception as e:
            print(f"[預約] 檢查到期失敗，下一輪再試：{e}")
    _as_run_due(now)
    return last_due


def _schedule_loop(interval: int = 30):
    """到期的預約改成「等你確認」並推播到手機；不執行任何操作。
    2026-10-23：分時擴縮與休眠也掛在這條迴圈（不另開排程器），所以改成每 10 秒醒來；預約檢查照舊每 interval 秒。"""
    last_due = 0.0
    while True:
        last_due = _bg_tick(_time.time(), last_due, interval)
        _time.sleep(_AS_TICK)


# ══════════════════════════════════════════════════════════════════
# 手機監控版 /m（2026-10-22）：手機只看狀態，不能部署或修改
# 頁面在 core/mobile_page.py，只呼叫唯讀 API。限制由伺服器執行，不是只靠畫面：
# 手機登入的 session 標記 readonly，之後只放行下面白名單裡的 API，其他一律 403。
# ══════════════════════════════════════════════════════════════════
import re as _re_m
from core.mobile_page import MOBILE_HTML

_PHONE_UA_RE = _re_m.compile(r"iPhone|iPod|Android.*Mobile|Mobile.*Android", _re_m.I)

# 唯讀 session 能用的 API：(方法, 路徑規則)。用白名單，之後新增的 API 預設就會被擋
_READONLY_API = (
    ("GET", _re_m.compile(r"^/api/(status|deployments|pods|healer/status|push/status|dashboard|calendar|autoscale)$")),   # autoscale：2026-10-23 分時設定唯讀
    ("GET", _re_m.compile(r"^/api/pods/[^/]+(/history)?$")),
    ("POST", _re_m.compile(r"^/api/push/(subscribe|unsubscribe|test)$")),
)
# 唯讀 session 能開的頁面與檔案；其他頁面一律轉到 /m
_READONLY_PAGES = ("/m", "/auth/logout", "/manifest.webmanifest", "/sw.js", "/offline", "/icons/",
                   "/favicon.ico", "/static/")


def _is_phone() -> bool:
    """手機（iPhone、Android 手機）。iPad 和平板照舊用完整版。"""
    return bool(_PHONE_UA_RE.search(request.headers.get("User-Agent", "")))


def _redirect_to_mobile():
    pod = request.args.get("pod", "")
    if _re_m.fullmatch(r"[a-z0-9][a-z0-9.\-]{0,252}", pod or ""):
        return redirect("/m?pod=" + pod)
    return redirect("/m")


def _mobile_login_page(error=None):
    return render_template_string(MOBILE_HTML, view="login", error=error, username="",
                                  google_login_available=_google_oauth_configured(), needs_setup=False)


def _readonly_allowed(method: str, path: str) -> bool:
    if path.startswith("/api/"):
        if request.args.get("diagnose"):
            return False   # 診斷會跑模型、可能觸發後續處置，手機版只看既有結果
        return any(method == m and rx.match(path) for m, rx in _READONLY_API)
    return path == "/m" or path.startswith(_READONLY_PAGES[1:])


@app.before_request
def _readonly_guard():
    if not session.get("readonly") or "username" not in session:
        return None
    if _readonly_allowed(request.method, request.path):
        return None
    if request.path.startswith("/api/"):
        return jsonify({"error": "手機版只能查看狀態，部署、擴縮、刪除等操作請用電腦開啟平台。"
                                 " / The mobile app is view-only; please use a computer to make changes.",
                        "readonly": True}), 403
    return _redirect_to_mobile()


@app.route("/m")
def mobile_view():
    if "username" not in session:
        return _mobile_login_page()
    if _is_phone():
        # 手機上打開的 session 一律降為唯讀（例如之前用手機登入過完整版）
        session["readonly"] = True
    return render_template_string(MOBILE_HTML, view="app", error=None, username=session["username"],
                                  google_login_available=False,
                                  needs_setup=_needs_onboarding(session["username"]))


if __name__ == "__main__":
    print("=" * 60)
    print("  ZeroTouch K8s Web Demo v2")
    print("=" * 60)
    print(f"  K8s   : {'Connected' if K8S_ENABLED else 'Simulation'}")
    print(f"  Open  : http://localhost:{WEB_PORT}")
    if PUBLIC_BASE_URL:
        print(f"  Public: {PUBLIC_BASE_URL}  （只綁 127.0.0.1，外部連線要經過通道）")
    if K8S_ENABLED:
        _HEALER_BG_INTERVAL = 30
        _healer_bg_thread = threading.Thread(
            target=_healer_background_loop, daemon=True, kwargs={"interval": _HEALER_BG_INTERVAL})
        _healer_bg_thread.start()
        _healer_bg_state["started_at"] = _utc_now_iso()
        _healer_bg_state["interval"] = _HEALER_BG_INTERVAL
        print("  Healer: background auto-heal loop started (scan every 30s)")
        print("  Autoscale: schedule / idle-sleep / auto-wake run inside the schedule loop below"
              + (f"（測試模式：閒置 {_AS_IDLE_OVERRIDE} 秒就休眠）" if _AS_IDLE_OVERRIDE else ""))
    else:
        print("  Healer: background auto-heal loop NOT started (K8s not connected)")
    threading.Thread(target=_schedule_loop, daemon=True, kwargs={"interval": 30}).start()
    print("  Schedule: due-task checker started (every 30s, reminds only — never auto-executes)")
    if CUSTOMER_CLUSTER_MODE:
        threading.Thread(target=_remote_healer_loop, daemon=True, kwargs={"interval": 30}).start()
        print("  Healer: customer-cluster auto-heal loop started (every 30s, via Git)")
    # 有對外網址就只綁本機：外部流量一律經過通道（Tailscale Funnel / Cloudflare Tunnel），
    # ProxyFix 才能安全地信任 X-Forwarded-*。沒設定時維持舊行為，區網可直接連。
    _bind = "127.0.0.1" if PUBLIC_BASE_URL else "0.0.0.0"
    try:
        # 正式 WSGI 伺服器；Flask 內建的開發伺服器不是設計給公開網路用的
        from waitress import serve
        print(f"  Server: waitress（{_bind}:{WEB_PORT}，8 threads）")
        _proxy = {}
        if PUBLIC_BASE_URL:
            # 只信任本機通道程式送來的代理標頭，取得真實客戶端 IP（頻率限制用）與 https
            _proxy = dict(trusted_proxy="127.0.0.1", trusted_proxy_count=1,
                          trusted_proxy_headers={"x-forwarded-for", "x-forwarded-proto", "x-forwarded-host"})
        serve(app, host=_bind, port=WEB_PORT, threads=8, max_request_body_size=1024 * 1024, **_proxy)
    except ImportError:
        print("  Server: Flask 開發伺服器（未安裝 waitress，不要直接對外開放）")
        app.run(host=_bind, port=WEB_PORT, debug=False)
