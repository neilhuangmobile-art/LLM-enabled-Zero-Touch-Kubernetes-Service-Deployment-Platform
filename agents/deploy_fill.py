"""
部署需求的補全與查核（2026-11-07，ZeroTouch 與 Claude Code 對照實驗之後）。

對照實驗（docs/bench_20261106/）找到的缺口，這裡用固定規則補，不靠模型：
1. 情境描述沒講映像檔：網站類的情境預填建議映像檔，畫面上標明是建議，使用者照常確認。
2. 模型捏造映像檔（英文需求被補成 website:latest）：映像檔要在原句出現、或是常見的官方映像檔，
   否則當作沒講，交給第 1 項或請使用者補。
3. 資料庫沒有密碼就啟動失敗：自動產生密碼，存進 Secret，部署單上可以改。
4. 背景程式沒給啟動指令，容器一啟動就結束：先建一個保持運作的空殼，畫面說明要換成自己的程式。
5. 句子裡的危險要求（特權、掛主機目錄、刪系統元件）不會照做，要明白告訴使用者，不能默默拿掉。

每個函式都只看傳進來的參數，不碰 K8s、不呼叫模型，方便測試。
"""
from __future__ import annotations

import re
import secrets
from typing import Dict, List, Optional, Tuple

from agents.cost_agent import _image_name

# ── 1. 情境預填映像檔 ────────────────────────────────────────────────
SCENARIO_WEB_IMAGE = "nginx:1.27"
# 看得出要的是網站或網頁才預填；資料庫、背景程式這類情境不猜
_WEB_CUE_RE = re.compile(r"網站|網頁|网站|网页|頁面|页面|首頁|公告頁|活動頁|報名|报名|招生|作業繳交|繳交|"
                         r"商店|購物|搶購|电商|電商|landing|website|web ?site|web ?page|homepage|page\b|site\b",
                         re.IGNORECASE)


def suggest_scenario_image(text: str, has_scenario: bool) -> Optional[str]:
    """情境部署沒講映像檔時的建議值；不是網站類的情境回 None（交給使用者決定）。"""
    if has_scenario and _WEB_CUE_RE.search(text or ""):
        return SCENARIO_WEB_IMAGE
    return None


# ── 2. 映像檔查核 ────────────────────────────────────────────────────
# 常見的官方映像檔：模型把「資料庫」「網頁伺服器」對到這些是合理的推論，不算捏造
KNOWN_IMAGES = {
    "nginx", "nginx-unprivileged", "httpd", "apache", "caddy", "traefik", "haproxy",
    "redis", "memcached", "postgres", "mysql", "mariadb", "mongo", "cassandra", "couchdb", "neo4j",
    "influxdb", "elasticsearch", "rabbitmq", "minio", "adminer", "phpmyadmin",
    "node", "python", "golang", "openjdk", "eclipse-temurin", "ruby", "php", "busybox", "alpine",
    "ubuntu", "debian", "wordpress", "ghost", "grafana", "prometheus", "jenkins", "tomcat",
    "registry", "nextcloud", "gitea", "node-red", "uptime-kuma", "hello-world",
}


# 一般名詞：句子裡出現「website」不代表使用者指定了叫 website 的映像檔（對照實驗 A20 就是這樣被捏造出來的）
_GENERIC_WORDS = {"website", "web", "site", "webpage", "page", "homepage", "app", "application", "myapp", "my-app",
                  "server", "api", "service", "db", "database", "frontend", "backend", "worker", "portal", "shop",
                  "store", "blog", "event", "school", "demo", "test"}


def image_grounded(image: str, text: str) -> bool:
    """映像檔有沒有根據：完整名稱在原句出現過、名稱（不是一般名詞）在原句出現過，或是常見的官方映像檔。"""
    if not image:
        return False
    base = _image_name(image)
    low = (text or "").lower()
    if base in KNOWN_IMAGES or image.lower() in low:
        return True
    return base not in _GENERIC_WORDS and re.search(r"(?<![a-z0-9])" + re.escape(base) + r"(?![a-z0-9])", low) is not None


# ── 3. 資料庫密碼 ────────────────────────────────────────────────────
# 映像檔 → 啟動一定要有的環境變數。值是 None 的會自動產生密碼。
_DB_ENV = {
    "postgres": {"POSTGRES_PASSWORD": None},
    "mysql": {"MYSQL_ROOT_PASSWORD": None},
    "mariadb": {"MARIADB_ROOT_PASSWORD": None},
    "mongo": {"MONGO_INITDB_ROOT_USERNAME": "admin", "MONGO_INITDB_ROOT_PASSWORD": None},
}


def db_required_env(image: str, existing: Optional[Dict] = None) -> Tuple[Dict[str, str], List[str]]:
    """回傳 (要補上的環境變數, 其中屬於密碼的名稱)。使用者已經給的不覆蓋。"""
    need = _DB_ENV.get(_image_name(image))
    if not need:
        return {}, []
    existing = existing or {}
    add, keys = {}, []
    for k, v in need.items():
        if str(existing.get(k) or "").strip():   # 空字串當作沒填（存檔時遮掉的密碼會變成空字串）
            continue
        if v is None:
            add[k] = secrets.token_urlsafe(18)
            keys.append(k)
        else:
            add[k] = v
    return add, keys


# ── 4. 背景程式空殼 ──────────────────────────────────────────────────
# 這些映像檔本身沒有要執行的程式，不給指令就會一啟動就結束
RUNTIME_IMAGES = {"python", "node", "golang", "openjdk", "eclipse-temurin", "ruby", "php",
                  "busybox", "alpine", "ubuntu", "debian"}
# 讓容器保持運作的待機指令；agents/cost_agent.recommended_probes 看到它就不加健康檢查
KEEPALIVE_COMMAND = ["sh", "-c", "echo 'ZeroTouch: 這是空殼，請換成你的程式'; while true; do sleep 3600; done"]
_SERVICE_CUE_RE = re.compile(r"網站|網頁|网站|网页|api|web|server|伺服器|服务器|http|頁面|页面", re.IGNORECASE)


def needs_keepalive(image: str, text: str, has_command: bool, port_mentioned: bool) -> bool:
    """執行環境類映像檔、沒給指令、看起來也不是要對外服務的，就需要空殼。"""
    if has_command or port_mentioned or _image_name(image) not in RUNTIME_IMAGES:
        return False
    return not _SERVICE_CUE_RE.search(text or "")


# ── 5. 句子裡的危險要求 ──────────────────────────────────────────────
_DANGER_PATTERNS = [
    (re.compile(r"privileged|特權|特权|控制主機|控制主机|控制整台", re.I), "特權模式（可以控制整台主機）"),
    (re.compile(r"hostpath|主機的?根目錄|主机的?根目录|主機目錄|主机目录|主機資料夾|掛載主機|挂载主机", re.I),
     "掛載主機的資料夾"),
    (re.compile(r"hostnetwork|host network|主機網路|主机网络", re.I), "使用主機網路"),
    (re.compile(r"hostpid|主機的?程序|主机的?进程", re.I), "看得到主機的程序"),
    (re.compile(r"kube-system|系統元件|系统组件", re.I), "動到 kube-system 的系統元件"),
    (re.compile(r"(?:刪|删|移除|清掉|清空|delete|remove).{0,12}(?:全部|所有|都|整個|all|everything)", re.I),
     "大量刪除"),
    (re.compile(r"忽略.{0,6}(?:規則|规则|限制|安全)|ignore (?:all |any |the )?(?:previous |safety )?(?:rules|instructions)",
                re.I), "要求忽略安全規則"),
]


def danger_requests(text: str) -> List[str]:
    """句子裡有哪些不會照做的危險要求（給畫面顯示用，不影響審查判斷）。"""
    found = []
    for rx, label in _DANGER_PATTERNS:
        if rx.search(text or "") and label not in found:
            found.append(label)
    return found


def danger_notice(found: List[str]) -> Optional[str]:
    if not found:
        return None
    return ("你的要求裡有這些部分不會照做：" + "、".join(found) +
            "。這個平台是多人共用的，這些操作會影響到別人或整台主機，所以只部署安全的部分。")
