r"""
C1 軟體目錄建立程式（2026-11-15）

把人工寫的 rag/knowledge/catalog_seed.yaml，補上從權威來源自動取得的資訊，輸出 catalog.json：
- 連接埠、資料目錄：Docker Hub 映像檔設定（ExposedPorts、Volumes），依版本讀取。
- 環境變數名稱：docker-library/docs 的 content.md（只有官方映像檔有），擷取 `### \`VAR\`` 標題。
- 映像檔或版本在 Docker Hub 不存在 → 該條目標記為 invalid，不進目錄（避免推薦不存在的映像檔）。

這台電腦遇到大量平行連線會卡住，所以逐一循序下載。
用法：python rag/build_catalog.py
"""
import json
import os
import re
import sys
import time

import requests
import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
SEED = os.path.join(HERE, "knowledge", "catalog_seed.yaml")
EXT = os.environ.get("RAG_CATALOG_EXT_FILE") or os.path.join(HERE, "knowledge", "catalog_ext.yaml")
OUT = os.environ.get("RAG_CATALOG_OUT") or os.path.join(HERE, "knowledge", "catalog.json")
# 已讀過的映像檔設定（之前建過的 catalog.json，分號分隔），同一個 repo:tag 直接沿用，避免 Docker Hub 匿名次數上限
CACHE_FILES = [x for x in (os.environ.get("RAG_CATALOG_CACHE") or "").split(";") if x]
ACCEPT = ",".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
])
UA = {"User-Agent": "zerotouch-catalog-builder"}


def image_config(repo: str, tag: str) -> dict:
    """讀 Docker Hub 映像檔設定；回傳 {ports, volumes, env_names}，失敗丟例外。"""
    tok = requests.get("https://auth.docker.io/token",
                       params={"service": "registry.docker.io", "scope": f"repository:{repo}:pull"},
                       headers=UA, timeout=30).json()["token"]
    h = {"Authorization": f"Bearer {tok}", "Accept": ACCEPT, **UA}
    r = requests.get(f"https://registry-1.docker.io/v2/{repo}/manifests/{tag}", headers=h, timeout=30)
    r.raise_for_status()
    m = r.json()
    if "manifests" in m:  # 多架構清單：取 linux/amd64
        cands = [x for x in m["manifests"]
                 if x.get("platform", {}).get("os") == "linux" and x["platform"].get("architecture") == "amd64"]
        if not cands:
            raise ValueError("沒有 linux/amd64 版本")
        m = requests.get(f"https://registry-1.docker.io/v2/{repo}/manifests/{cands[0]['digest']}",
                         headers=h, timeout=30).json()
    cfg = requests.get(f"https://registry-1.docker.io/v2/{repo}/blobs/{m['config']['digest']}",
                       headers=h, timeout=60).json().get("config", {})
    ports = sorted({int(p.split("/")[0]) for p in (cfg.get("ExposedPorts") or {})})
    return {"ports": ports,
            "volumes": sorted(cfg.get("Volumes") or {}),
            "env_names": [e.split("=", 1)[0] for e in (cfg.get("Env") or [])]}


def library_doc_env(repo: str) -> list:
    """docker-library/docs 的 content.md 裡以 ### `VAR` 列出的環境變數（只有官方映像檔有）。"""
    if not repo.startswith("library/"):
        return []
    name = repo.split("/", 1)[1]
    r = requests.get(f"https://raw.githubusercontent.com/docker-library/docs/master/{name}/content.md",
                     headers=UA, timeout=30)
    if r.status_code != 200:
        return []
    found = re.findall(r"^###\s+`([A-Za-z_][A-Za-z0-9_.]*)`", r.text, re.M)
    return list(dict.fromkeys(found))


def main():
    seed = yaml.safe_load(open(SEED, encoding="utf-8"))
    # 自動擴充的目錄（rag/expand_catalog.py）：人工條目優先，同名不重複收錄
    if os.path.exists(EXT) and os.environ.get("RAG_CATALOG_EXT", "0") == "1":
        have = {e["id"] for e in seed} | {e["repo"].split("/")[-1] for e in seed}
        seed += [e for e in (yaml.safe_load(open(EXT, encoding="utf-8")) or [])
                 if e["id"] not in have and e["repo"].split("/")[-1] not in have]
    cache = {}
    for cf in CACHE_FILES:
        if os.path.exists(cf):
            for e in json.load(open(cf, encoding="utf-8"))["entries"]:
                cache[(e["repo"], str(e["tag"]))] = {"ports": e["ports"], "volumes": e["volumes"],
                                                     "doc_env": e.get("doc_env", [])}
    out, invalid = [], []
    for item in seed:
        repo, tag = item["repo"], str(item["tag"])
        try:
            cfg = cache.get((repo, tag)) or image_config(repo, tag)
        except Exception as e:  # noqa: BLE001
            invalid.append({"id": item["id"], "image": f"{repo}:{tag}", "error": str(e)[:160]})
            print(f"  ✗ {item['id']}: {repo}:{tag} 讀不到（{str(e)[:80]}）", flush=True)
            continue
        doc_env = cfg["doc_env"] if "doc_env" in cfg else library_doc_env(repo)
        image = (repo.split("/", 1)[1] if repo.startswith("library/") else repo) + f":{tag}"
        entry = {**item, "image": image, "ports": cfg["ports"], "volumes": cfg["volumes"],
                 "doc_env": doc_env,
                 "sources": [f"registry:{repo}:{tag}"]
                 + ([f"https://github.com/docker-library/docs/tree/master/{repo.split('/', 1)[1]}"]
                    if repo.startswith("library/") else [])
                 + ([f"awesome-selfhosted-data（CC BY-SA 3.0）：{(item.get('upstream') or {}).get('url', '')}"]
                    if item.get("origin") == "awesome-selfhosted" else [])}
        out.append(entry)
        print(f"  ✓ {item['id']}: {image} ports={cfg['ports']} volumes={cfg['volumes']} doc_env={len(doc_env)}", flush=True)
        if (repo, tag) not in cache:
            time.sleep(0.3)
    json.dump({"built_at": time.strftime("%Y-%m-%d %H:%M"), "entries": out, "invalid": invalid},
              open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"完成：{len(out)} 個有效、{len(invalid)} 個無效 → {OUT}")
    return 0 if out else 1


if __name__ == "__main__":
    sys.exit(main())
