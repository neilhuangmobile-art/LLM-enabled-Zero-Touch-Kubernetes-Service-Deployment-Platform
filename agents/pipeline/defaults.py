"""用映像檔名稱查表的預設值（2026-11-16 陳冠穎決定：補預設值直接查表，不靠模型猜）。

資料來源是各映像檔在 Docker Hub 官方頁面寫的監聽 port 與資料目錄。查不到的映像檔回 None，
由推理代理補，或請使用者提供。第二階段會把這些表換成 RAG 資料表（Docker 官方映像檔說明）。"""
from __future__ import annotations

from typing import Dict, Optional, Tuple

from agents.cost_agent import KNOWN_PORTS, _image_name

# 容器監聽的 port（沿用 cost_agent.KNOWN_PORTS，再補常見的網站類映像檔）
LISTEN_PORTS = {
    **KNOWN_PORTS,
    "nginx": 80, "httpd": 80, "caddy": 80, "wordpress": 80, "nextcloud": 80, "mediawiki": 80, "phpmyadmin": 80,
    # 2026-11-19：映像檔沒寫 EXPOSE、軟體目錄抓不到 port 的：flarum 8888（mondediefr/docker-flarum README）、
    # Home Assistant 8123（官方文件預設）
    "flarum": 8888, "home-assistant": 8123,
    # 2026-11-19 目錄擴充：舊軟體表有這幾個但沒寫 port（Docker Hub 官方說明）
    "nats": 4222, "redmine": 3000, "eclipse-mosquitto": 1883,
    "ghost": 2368, "grafana": 3000, "gitea": 3000, "prometheus": 9090, "influxdb": 8086, "minio": 9000,
    "node-red": 1880, "uptime-kuma": 3001, "jenkins": 8080, "tomcat": 8080, "adminer": 8080,
}

# 要保存的資料放在容器的哪個資料夾
DATA_PATHS = {
    "postgres": "/var/lib/postgresql/data", "mysql": "/var/lib/mysql", "mariadb": "/var/lib/mysql",
    "mongo": "/data/db", "redis": "/data", "rabbitmq": "/var/lib/rabbitmq", "elasticsearch": "/usr/share/elasticsearch/data",
    "influxdb": "/var/lib/influxdb2", "minio": "/data", "wordpress": "/var/www/html", "nextcloud": "/var/www/html",
    "ghost": "/var/lib/ghost/content", "gitea": "/data", "grafana": "/var/lib/grafana", "prometheus": "/prometheus",
    "mediawiki": "/var/www/html/images", "jenkins": "/var/jenkins_home", "node-red": "/data", "uptime-kuma": "/app/data",
    "nginx": "/usr/share/nginx/html", "httpd": "/usr/local/apache2/htdocs",
}
# 執行環境類映像檔：資料夾由使用者的程式決定，先給 /data 並說明
RUNTIME_DATA_PATH = "/data"
RUNTIME_IMAGES = {"python", "node", "golang", "openjdk", "eclipse-temurin", "ruby", "php", "busybox", "alpine",
                  "ubuntu", "debian"}

# 保存空間掛上去之後要一起設的環境變數。postgres 的資料夾如果不是空的（例如有 lost+found）會啟動失敗，
# 官方文件建議把 PGDATA 指到子資料夾
DATA_ENV = {"postgres": {"PGDATA": "/var/lib/postgresql/data/pgdata"}}

# 管理網頁：(port, 映像檔 tag 要加的後綴)。rabbitmq 的管理網頁要用 management 版的映像檔
MGMT_PORTS: Dict[str, Tuple[int, str]] = {"rabbitmq": (15672, "management")}

# 部署單的保存空間上限（web_demo._sanitize_deploy_payload：1～5Gi）
MAX_STORAGE_GI = 5
DEFAULT_STORAGE = "1Gi"


def name(image: str) -> str:
    return _image_name(image or "")


def _catalog_entry(image: str) -> Optional[dict]:
    """知識表（rag/software_catalog.json）裡完整名稱一樣的那一筆。linuxserver/* 帶有它官方設定的 port、資料夾，
    要比用簡稱查表優先：例如 linuxserver/nextcloud 聽 443，官方的 nextcloud 聽 80，簡稱都是 nextcloud。"""
    from agents.pipeline import software_rag
    repo = software_rag.repo_of(image)
    return next((it for it in software_rag.items() if it["image"] == repo), None)


# 會自己保存資料或狀態的軟體類別（rag/knowledge/catalog.json 的 category）。gap_check._stateful_replicas 用：
# 系統推的副本數大於 1 時改成 1（兩份會搶同一份資料；資料要保存靠儲存空間，不是多開副本）。
# 日誌收集（fluent-bit）、備份同步（rclone、restic）本身不保存狀態，不列入。
STATEFUL_CATEGORIES = {
    "資料庫", "快取", "訊息佇列", "程式碼託管", "程式碼管理", "維基或筆記", "共筆", "檔案分享", "檔案同步", "密碼管理",
    "監控", "監控儀表板", "專案管理", "待辦事項", "相簿", "影音", "記帳", "工時記錄", "搜尋引擎", "文件管理", "書籤",
    "行事曆", "新聞訂閱", "映像檔倉庫", "智慧家庭", "遊戲伺服器", "預約", "短網址", "論壇", "問卷", "聊天",
    "線上資料表", "程式碼片段分享",
}

# 映像檔 EXPOSE 的第一個 port 不是網頁的：gogs 先列 SSH 22，網頁在 3000（gogs 官方 docker 說明）
PORT_OVERRIDES = {"gogs": 3000}


def listen_port(image: str) -> Optional[int]:
    if name(image) in PORT_OVERRIDES:
        return PORT_OVERRIDES[name(image)]
    it = _catalog_entry(image)
    if it and it.get("port"):
        return int(it["port"])
    return LISTEN_PORTS.get(name(image))


def data_path(image: str) -> Optional[str]:
    it = _catalog_entry(image)
    if it and it.get("data_path"):
        return it["data_path"]
    n = name(image)
    if n in DATA_PATHS:
        return DATA_PATHS[n]
    return RUNTIME_DATA_PATH if n in RUNTIME_IMAGES else None


def with_mgmt_tag(image: str) -> str:
    """rabbitmq → rabbitmq:management；rabbitmq:3.13 → rabbitmq:3.13-management；已經是管理版就不動。"""
    n = name(image)
    if n not in MGMT_PORTS:
        return image
    suffix = MGMT_PORTS[n][1]
    if ":" in image.split("/")[-1]:
        base, tag = image.rsplit(":", 1)
        if suffix in tag:
            return image
        return f"{base}:{suffix}" if tag == "latest" else f"{base}:{tag}-{suffix}"
    return f"{image}:{suffix}"
