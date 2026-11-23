"""重抓 rag/software_catalog.json（多代理人部署解析選軟體用的知識表，2026-11-17）。

來源（都是公開資料，不是自己寫的說明）：
- Docker 官方映像檔名單：github.com/docker-library/official-images 的 library/ 目錄
- 每個官方映像檔的一句說明：github.com/docker-library/docs/<名稱>/README-short.txt
- 廠商自己發布的常用映像檔（PUBLISHERS）：Docker Hub 的描述；描述沒有內容的改用該專案 GitHub 的描述（GITHUB_DESC）
- LinuxServer.io 的自架軟體映像檔（api.linuxserver.io，2026-11-17 加）：說明、port、資料夾都取自它的官方設定資料。
  只收有寫 port 的、沒停止維護的。映像檔名稱用 Docker Hub 上的 linuxserver/<名稱>

用法：python scripts/build_software_catalog.py
"""
from __future__ import annotations

import concurrent.futures as cf
import datetime
import json
import os

import requests

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "rag", "software_catalog.json")
# 不是 Docker 官方映像檔、但很常用（agents/pipeline/defaults.py 查得到 port 的）。minio/minio 2026-11-17 在 Docker Hub 查不到，沒放
PUBLISHERS = ["gitea/gitea", "grafana/grafana", "prom/prometheus", "louislam/uptime-kuma", "nodered/node-red", "jenkins/jenkins"]
GITHUB_DESC = {"prom/prometheus": "prometheus/prometheus", "grafana/grafana": "grafana/grafana"}


def main() -> None:
    lst = requests.get("https://api.github.com/repos/docker-library/official-images/contents/library", timeout=30).json()
    names = sorted(x["name"] for x in lst if x["type"] == "file")

    def short(n):
        r = requests.get(f"https://raw.githubusercontent.com/docker-library/docs/master/{n}/README-short.txt", timeout=30)
        return n, r.text.strip() if r.status_code == 200 else ""

    with cf.ThreadPoolExecutor(8) as ex:
        official = dict(ex.map(short, names))
    items = [{"image": n, "desc": d, "official": True, "deprecated": d.upper().startswith("DEPRECATED"),
              "source": f"github.com/docker-library/docs/{n}/README-short.txt"} for n, d in sorted(official.items()) if d]
    for p in PUBLISHERS:
        if p in GITHUB_DESC:
            d = requests.get(f"https://api.github.com/repos/{GITHUB_DESC[p]}", timeout=30).json().get("description") or ""
            src = f"api.github.com/repos/{GITHUB_DESC[p]} description"
        else:
            r = requests.get(f"https://hub.docker.com/v2/repositories/{p}/", timeout=30)
            d = (r.json().get("description") or "") if r.status_code == 200 else ""
            src = f"hub.docker.com/v2/repositories/{p}/ description"
        if d:
            items.append({"image": p, "desc": d, "official": False, "deprecated": False, "source": src})
    lsio = requests.get("https://api.linuxserver.io/api/v1/images?include_config=true&include_deprecated=false",
                        timeout=60).json()["data"]["repositories"]["linuxserver"]
    for it in lsio:
        cfg = it.get("config") or {}
        ports = [p for p in cfg.get("ports") or [] if not p.get("optional")]
        vols = [v for v in cfg.get("volumes") or [] if not v.get("optional")]
        if it.get("deprecated") or not ports or not str(ports[0].get("internal", "")).isdigit():
            continue
        items.append({"image": f"linuxserver/{it['name']}", "desc": (it.get("description") or "").strip()[:300],
                      "official": False, "deprecated": False, "port": int(ports[0]["internal"]),
                      "data_path": vols[0]["path"] if vols else None,
                      "source": f"api.linuxserver.io/api/v1/images（{it.get('github_url', '')}）"})
    doc = {"_about": "部署解析選軟體用的知識表（多代理人流程）。official=Docker 官方映像檔，說明取自 docker-library/docs 的 "
                     "README-short.txt；其餘是廠商自己發布的常用映像檔，說明取自 Docker Hub 或該專案的 GitHub 描述；"
                     "linuxserver/* 取自 LinuxServer.io 官方 API，port 與 data_path 是它設定資料裡必填的第一個。"
                     "重抓：scripts/build_software_catalog.py",
           "fetched": datetime.date.today().isoformat(), "items": items}
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    print(f"{len(items)} 筆 → {OUT}")


# ── 2026-11-23：補常見自架軟體（--add-selfhosted）──────────────────────────
# holdout 題組：Gemini 建議的軟體有 22% 不在知識表，選錯軟體的 13 題有 6 題是這個原因。
# 來源是公開的 awesome-selfhosted-data，依 GitHub 星數取前 N 名（客觀門檻，不照任何題組挑）。
# 只新增、不覆蓋：已經在表裡的（含 Docker 官方映像檔同名的）一律跳過，現有 port 不變。
SELFHOSTED_TAR = "https://codeload.github.com/awesome-selfhosted/awesome-selfhosted-data/tar.gz/refs/heads/master"
MIN_PULLS = 100_000            # 太冷門的可能是同名的別人
WEB_PORTS = (80, 8080, 3000, 8000, 5000, 9000, 8081)


class _Hub:
    """Docker Hub 網站 API（不是 registry 下載，不算下載次數）。每分鐘上限 180 次：每次間隔 0.4 秒，429 就等 retry-after。"""

    def __init__(self):
        import threading
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "zt-k8s-catalog-builder"
        self.lock, self.last = threading.Lock(), 0.0

    def get(self, url):
        import time
        r = None
        for _ in range(6):
            with self.lock:
                w = self.last + 0.4 - time.time()
                if w > 0:
                    time.sleep(w)
                self.last = time.time()
            try:
                r = self.s.get(url, timeout=30)
            except requests.RequestException as e:   # 逾時、連線中斷：等一下再試，重試完還是失敗就跳過這一筆（回 None）
                print(f"  重試 {url[:80]}：{type(e).__name__}")
                r = None
                time.sleep(5)
                continue
            if r.status_code != 429:
                return r
            time.sleep(int(r.headers.get("retry-after", "60")) + 1)
        return r


def _norm(x) -> str:
    import re
    return re.sub(r"[^a-z0-9]", "", str(x).lower())


def _pick_port(ports):
    """EXPOSE 有好幾個時：先找常見的網頁 port（照 WEB_PORTS 順序），都沒有才用第一個。"""
    ports = list(dict.fromkeys(ports or []))
    return next((w for w in WEB_PORTS if w in ports), ports[0] if ports else None)


def _selfhosted_apps(n: int):
    import io
    import re
    import tarfile
    import yaml
    data = requests.get(SELFHOSTED_TAR, timeout=120).content
    apps = []
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        for m in tf.getmembers():
            if not (m.isfile() and "/software/" in m.name and m.name.endswith(".yml")):
                continue
            d = yaml.safe_load(tf.extractfile(m).read().decode("utf-8")) or {}
            gh = re.match(r"https://github\.com/([^/]+)/([^/#?]+)", d.get("source_code_url") or "")
            if d.get("archived") or not d.get("stargazers_count") or not gh:
                continue
            apps.append({"name": d["name"], "desc": (d.get("description") or "").strip(), "stars": d["stargazers_count"],
                         "owner": gh.group(1), "repo": gh.group(2).removesuffix(".git")})
    return sorted(apps, key=lambda a: -a["stars"])[:n]


# 只收原作者自己發布的（Docker Hub 帳號對得上 GitHub 作者／專案／軟體名稱），或知名的重新打包者；
# 2026-11-23 第一版用「下載次數最多」，抓到不少別人重包的（fulhambuilds/vaultwarden、dyrnq/open-webui），部署出去有安全疑慮
TRUSTED_REPACKERS = {"bitnami"}
# 名稱開頭一樣、但其實是同一個專案的零件或周邊工具的
_COMPONENT = ("postgresql", "postgres", "mysql", "redis", "db", "worker", "backup", "exporter", "importer",
              "operator", "agent", "cli", "proxy", "sidecar", "migrate", "migrations", "init", "test", "dev")
# 帳號就是軟體名稱時，映像檔名稱是這些才收（halo/azuredevops-linuxagent、mailcow/watchdog 這種不收）
_GENERIC = ("server", "core", "app", "web", "platform", "community-edition", "ce")


def _resolve(hub: _Hub, a: dict):
    """找這套軟體在 Docker Hub 上的映像檔：猜 <owner>/<repo>、<repo>/<repo>，再用名稱搜尋。
    只收原作者自己發布的（或 TRUSTED_REPACKERS），名稱完全一樣的優先、以軟體名稱開頭的其次（零件不收），
    同一級裡取下載次數最多、而且至少 MIN_PULLS 的。"""
    import re
    keys = {k for k in (_norm(a["repo"]), _norm(a["name"])) if k}
    owners = keys | {_norm(a["owner"])}
    o, r = a["owner"].lower(), a["repo"].lower()
    cands = {}
    for ns, rp in ((o, r), (o.replace("-", ""), r), (r, r)):
        x = hub.get(f"https://hub.docker.com/v2/repositories/{ns}/{rp}/")
        if x is not None and x.status_code == 200:
            cands[f"{ns}/{rp}"] = x.json().get("pull_count") or 0
    for q in (a["name"], a["repo"]):
        x = hub.get(f"https://hub.docker.com/v2/search/repositories/?query={requests.utils.quote(q)}&page_size=10")
        for it in (x.json().get("results") or []) if x is not None and x.status_code == 200 else []:
            name = (it.get("repo_name") or "").lower()
            if "/" in name:              # 沒有 / 的是 Docker 官方映像檔：知識表本來就有
                cands[name] = max(cands.get(name, 0), it.get("pull_count") or 0)

    def rank(name):
        ns, seg = name.split("/", 1)
        if _norm(ns) not in owners and ns not in TRUSTED_REPACKERS:
            return None
        raw, n = seg.split("/")[-1], _norm(seg.split("/")[-1])
        if n in keys:
            return 2
        part = raw.rsplit("-", 1)[-1] in _COMPONENT or n in _COMPONENT
        if any(n.startswith(k) and len(k) >= 4 for k in keys) and not part:
            return 1
        if _norm(ns) in keys and raw in _GENERIC:   # 帳號就是軟體名稱、映像檔是通用名稱：vaultwarden/server、fireflyiii/core
            return 1
        return None
    ranked = [(rank(k), v, k) for k, v in cands.items() if v >= MIN_PULLS]
    ranked = [t for t in ranked if t[0] is not None]
    if not ranked:
        return None
    _, pulls, img = max(ranked)
    ports, vols = [], []
    x = hub.get(f"https://hub.docker.com/v2/repositories/{img}/tags/?page_size=10&ordering=last_updated")
    tags = [t["name"] for t in (x.json().get("results") or [])] if x is not None and x.status_code == 200 else []
    for tag in ([t for t in ("latest", "stable", "release") if t in tags] or tags)[:3]:
        x = hub.get(f"https://hub.docker.com/v2/repositories/{img}/tags/{tag}/images")
        imgs = x.json() if x is not None and x.status_code == 200 else []
        one = next((i for i in imgs if i.get("architecture") == "amd64"), imgs[0] if imgs else None)
        if not one:
            continue
        for layer in one.get("layers") or []:
            ins = layer.get("instruction") or ""
            if ins.startswith("EXPOSE"):
                # UDP 的不算（syncthing 的 21027 是探索用，網頁在 8384）
                ports += [int(p) for p, proto in re.findall(r"(\d+)(?:/(tcp|udp))?", ins) if proto != "udp"]
            elif ins.startswith("VOLUME"):
                vols += re.findall(r"(/[\w./-]+)", ins)
        break
    return {"image": img, "desc": a["desc"][:300], "official": False, "deprecated": False,
            "port": _pick_port(ports), "data_path": vols[0] if vols else None, "pulls": pulls,
            "source": f"awesome-selfhosted-data（{a['name']}，github.com/{a['owner']}/{a['repo']}）＋Docker Hub 映像檔的 EXPOSE／VOLUME"}


FIELD_CATALOG = os.path.join(os.path.dirname(OUT), "knowledge", "catalog.json")


def _field_catalog_repos() -> set:
    """柏碩負責的 rag/knowledge/catalog.json 已經有的軟體（有人工確認過的 port 與中文情境），不重複加、不蓋掉。
    2026-11-23 第一次補的時候沒排除，syncthing 的 port 被 EXPOSE 的 22000 蓋掉了正確的 8384（claude 開發題組 C094 變錯）"""
    try:
        with open(FIELD_CATALOG, encoding="utf-8") as f:
            entries = json.load(f).get("entries") or []
    except (OSError, ValueError):
        return set()
    out = set()
    for e in entries:
        img = str(e.get("image") or e.get("repo") or "").lower().split("@")[0]
        last = img.rsplit("/", 1)[-1]
        if ":" in last:
            img = img[: len(img) - len(last)] + last.split(":")[0]
        out.add(img[len("library/"):] if img.startswith("library/") else img)
    return out


def add_selfhosted(n: int = 300, out: str = OUT) -> None:
    """out：寫到哪裡（預設直接更新知識表；先檢查時可以寫到別的地方）。"""
    with open(OUT, encoding="utf-8") as f:
        doc = json.load(f)
    items = doc["items"]
    have = {it["image"] for it in items} | _field_catalog_repos()
    official = {_norm(it["image"]) for it in items if it.get("official")}
    hub = _Hub()
    apps = [a for a in _selfhosted_apps(n) if _norm(a["repo"]) not in official and _norm(a["name"]) not in official]
    with cf.ThreadPoolExecutor(3) as ex:
        found = [x for x in ex.map(lambda a: _resolve(hub, a), apps) if x]
    added = []
    for x in found:
        seg = _norm(x["image"].split("/")[-1])
        if x["image"] in have or seg in official:
            continue
        if not x["port"]:
            x.pop("port")
        if not x["data_path"]:
            x.pop("data_path")
        x.pop("pulls")
        items.append(x)
        have.add(x["image"])
        added.append(x["image"])
    if "--add-selfhosted" not in doc["_about"]:
        doc["_about"] += ("；2026-11-23 起另外補 awesome-selfhosted-data 星數前 %d 名的自架軟體（說明取自該資料，"
                      "port／data_path 取自 Docker Hub 上映像檔的 EXPOSE／VOLUME，只新增不覆蓋：--add-selfhosted）" % n)
    doc["selfhosted_added"] = datetime.date.today().isoformat()
    with open(out, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    print(f"前 {n} 名：{len(apps)} 套不是官方映像檔，找到映像檔 {len(found)}，新增 {len(added)} 筆 → {out}")


if __name__ == "__main__":
    import sys
    if "--add-selfhosted" in sys.argv:
        # --out <路徑>：先寫到別的地方檢查，確認沒問題再直接更新知識表
        add_selfhosted(out=sys.argv[sys.argv.index("--out") + 1] if "--out" in sys.argv else OUT)
    else:
        main()
