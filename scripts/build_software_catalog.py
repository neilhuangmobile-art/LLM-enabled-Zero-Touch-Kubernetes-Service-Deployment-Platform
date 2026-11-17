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


if __name__ == "__main__":
    main()
