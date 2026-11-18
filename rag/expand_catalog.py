r"""
軟體目錄擴充（2026-11-15）：從公開授權來源自動補軟體，輸出 rag/knowledge/catalog_ext.yaml。

來源（下載到 D:\rag_sources，不進版控）：
  awesome-selfhosted-data   CC BY-SA 3.0：軟體名稱、英文簡介、分類、原始碼網址、星數
  docker-library/official-images、docker-library/docs   Apache 2.0／MIT：官方映像檔清單與簡介
資訊來源分工：
  - 映像檔是否存在、連接埠、資料目錄：Docker Hub（之後由 build_catalog.py 依版本讀取，與人工目錄相同）
  - 中文情境描述、類別、能否單一容器運作：Gemini 依英文簡介改寫（標記 generated: true，尚未人工確認）
  - 人工寫的 catalog_seed.yaml 優先；同一軟體不重複收錄

三個階段，各自暫存結果，中斷後可以接著跑：
  python rag/expand_catalog.py select    # A 篩選候選（星數、仍在維護、有 Docker 版）
  python rag/expand_catalog.py resolve   # B 在 Docker Hub 找對應映像檔與版本（循序，約 20 分鐘）
  python rag/expand_catalog.py describe  # C Gemini 產生中文情境描述 → catalog_ext.yaml
這台電腦遇到大量平行連線會卡住，所以全部循序下載。
"""
import glob
import json
import os
import re
import sys
import time
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parents[1]
SRC = Path(os.environ.get("RAG_SOURCES", r"D:\rag_sources"))
STAGE = SRC / "stage"
KDIR = ROOT / "rag" / "knowledge"
UA = {"User-Agent": "zerotouch-catalog-builder"}
MIN_STARS = int(os.environ.get("RAG_MIN_STARS", "2000"))
SKIP_TAG_WORDS = ("dev", "nightly", "beta", "alpha", "rc", "edge", "test", "unstable", "snapshot", "arm", "windows")


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def _seed_ids() -> set:
    seed = yaml.safe_load(open(KDIR / "catalog_seed.yaml", encoding="utf-8"))
    out = set()
    for e in seed:
        out.add(e["id"].lower())
        out.add(e["repo"].split("/")[-1].lower())
    return out


# ── A 篩選 ────────────────────────────────────────────────────────────
def stage_select():
    STAGE.mkdir(parents=True, exist_ok=True)
    have = _seed_ids()
    official = {Path(p).name for p in glob.glob(str(SRC / "official-images" / "library" / "*"))}
    cands = []
    for f in sorted(glob.glob(str(SRC / "awesome-selfhosted-data" / "software" / "*.yml"))):
        s = yaml.safe_load(open(f, encoding="utf-8"))
        # 不要求標示 Docker 平台（很多 PHP 軟體沒標但有映像檔；B 階段會確認映像檔存在）
        # 沒有星數的是 GitHub 以外的專案（Codeberg、GitLab），一併收進來
        stars = s.get("stargazers_count")
        if s.get("archived") or (stars is not None and stars < MIN_STARS):
            continue
        sid = _slug(s["name"])
        if sid in have:
            continue
        cands.append({"id": sid, "name": s["name"], "description": s.get("description", ""),
                      "tags": s.get("tags", []), "source_code_url": s.get("source_code_url", ""),
                      "website_url": s.get("website_url", ""), "stars": s.get("stargazers_count", 0),
                      "origin": "awesome-selfhosted", "official": sid in official})
    # 官方映像檔中，awesome-selfhosted 沒收的基礎服務（資料庫、佇列、執行環境等）
    names = {c["id"] for c in cands} | have
    for name in sorted(official):
        if name in names or name in ("hello-world", "buildpack-deps", "docker", "cirros"):
            continue  # 測試用或建置用映像檔，不是新手會部署的服務
        short = SRC / "docs" / name / "README-short.txt"
        desc = short.read_text(encoding="utf-8").strip() if short.exists() else ""
        if not desc or re.search(r"deprecated|DEPRECATED", desc):
            continue
        cands.append({"id": name, "name": name, "description": desc, "tags": ["Docker Official Image"],
                      "source_code_url": "", "website_url": "", "stars": 0,
                      "origin": "docker-official", "official": True})
    json.dump(cands, open(STAGE / "a_candidates.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"A：候選 {len(cands)} 個（awesome-selfhosted {sum(c['origin'] == 'awesome-selfhosted' for c in cands)}、"
          f"官方映像檔 {sum(c['origin'] == 'docker-official' for c in cands)}）")


# ── B 找映像檔 ────────────────────────────────────────────────────────
def _hub_repo(ns: str, name: str):
    r = requests.get(f"https://hub.docker.com/v2/repositories/{ns}/{name}/", headers=UA, timeout=30)
    if r.status_code != 200:
        return None
    j = r.json()
    return {"repo": f"{ns}/{name}", "pull_count": j.get("pull_count", 0), "last_updated": j.get("last_updated") or ""}


def _pick_tag(repo: str):
    r = requests.get(f"https://hub.docker.com/v2/repositories/{repo}/tags/",
                     params={"page_size": 50, "ordering": "last_updated"}, headers=UA, timeout=30)
    if r.status_code != 200:
        return None
    tags = [t["name"] for t in r.json().get("results", [])
            if any((i.get("os"), i.get("architecture")) == ("linux", "amd64") for i in t.get("images") or [])]
    if "latest" in tags:
        return "latest"
    stable = [t for t in tags if not any(w in t.lower() for w in SKIP_TAG_WORDS)]
    return stable[0] if stable else None


# 映像檔名稱不規則的熱門軟體（依各專案官方說明；B 階段仍會到 Docker Hub 確認存在）
OVERRIDES = {"owncloud": ("owncloud", "server"), "gitlab": ("gitlab", "gitlab-ce"), "nodebb": ("nodebb", "docker"),
             "bookstack": ("linuxserver", "bookstack"), "flarum": ("mondedie", "flarum"),
             "limesurvey": ("martialblog", "limesurvey"), "gogs": ("gogs", "gogs"), "phpbb": ("bitnami", "phpbb"),
             "nextcloud": ("library", "nextcloud"), "wordpress": ("library", "wordpress")}


def _repo_candidates(c: dict):
    sid, out = c["id"], []
    if sid in OVERRIDES:
        out.append(OVERRIDES[sid])
    if c["official"]:
        out.append(("library", sid))
    m = re.match(r"https://github\.com/([^/]+)/([^/#?]+)", c.get("source_code_url") or "")
    if m:
        out.append((m.group(1).lower(), m.group(2).lower().removesuffix(".git")))
    out += [(sid, sid), ("linuxserver", sid), (sid.replace("-", ""), sid.replace("-", ""))]
    seen, uniq = set(), []
    for x in out:
        if x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq


def stage_resolve():
    cands = json.load(open(STAGE / "a_candidates.json", encoding="utf-8"))
    path = STAGE / "b_resolved.json"
    done = json.load(open(path, encoding="utf-8")) if path.exists() else {}
    for i, c in enumerate(cands):
        if c["id"] in done:
            continue
        found = []
        for ns, name in _repo_candidates(c):
            try:
                h = _hub_repo(ns, name)
            except Exception:  # noqa: BLE001
                h = None
            if h and h["last_updated"] >= "2024-06":
                found.append(h)
            time.sleep(0.2)
        res = {"id": c["id"], "status": "not_found"}
        if found:
            best = found[0] if c["official"] and found[0]["repo"].startswith("library/") else \
                max(found, key=lambda h: h["pull_count"])
            try:
                tag = _pick_tag(best["repo"])
            except Exception:  # noqa: BLE001
                tag = None
            res = {"id": c["id"], "status": "ok" if tag else "no_tag", **best, "tag": tag}
        done[c["id"]] = res
        if (i + 1) % 20 == 0:
            json.dump(done, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
            ok = sum(v["status"] == "ok" for v in done.values())
            print(f"  B：{i + 1}/{len(cands)}，找到 {ok}", flush=True)
    json.dump(done, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    ok = sum(v["status"] == "ok" for v in done.values())
    print(f"B：完成 {len(done)} 個，找到映像檔 {ok} 個")


# ── C 中文描述 ────────────────────────────────────────────────────────
DESC_PROMPT = """你在替「用一句話部署服務到 Kubernetes」的系統整理軟體目錄。系統的使用者是完全不懂技術的新手，
會用生活化的中文描述需求（例如「想讓社團成員討論活動」）。請依每個軟體的英文簡介與分類，替它寫目錄資料。

規則：
- scenarios：4 句繁體中文（台灣用語）情境描述，從使用者的角度寫「想做什麼」，不可出現軟體名稱或英文技術詞，每句 8～25 字，四句角度不同。
- category：中文服務類型，盡量用這些之一：網站、部落格、論壇、維基或筆記、檔案分享、相片、影音、聊天、表單或問卷、
  專案管理、程式碼託管、自動化流程、監控、監控儀表板、資料分析、資料庫、資料庫管理、快取、訊息佇列、搜尋引擎、
  密碼管理、電子商務、記帳、行事曆、書籤、RSS 閱讀、電子郵件、智慧家庭、遊戲伺服器、背景程式、執行環境、網路工具、其他。
- standalone：只用一個容器就能正常運作填 true；一定要另外架資料庫等其他服務才能用填 false。不確定填 null。
- requires：standalone 為 false 時需要的服務（例如 ["postgres"]），否則空陣列。
- notes：一句中文注意事項（例如需要設定管理員密碼、第一次啟動要跑設定精靈）；沒有就空字串。
- 你不確定的事不要編造。

軟體清單：
{items}

只輸出 JSON 陣列，每個元素：{{"id":"", "category":"", "scenarios":["","","",""], "standalone":true, "requires":[], "notes":""}}"""

RES_BY_CAT = {"資料庫": ("512Mi", "500m", 0), "搜尋引擎": ("1Gi", "500m", 0), "訊息佇列": ("512Mi", "250m", 0),
              "快取": ("256Mi", "250m", 0), "網站": ("128Mi", "100m", 300), "背景程式": ("128Mi", "100m", 0),
              "執行環境": ("256Mi", "250m", 0)}


def stage_describe():
    sys.path.insert(0, str(ROOT / "scripts" / "rag"))
    from gen_data import ask_list  # noqa: E402
    cands = {c["id"]: c for c in json.load(open(STAGE / "a_candidates.json", encoding="utf-8"))}
    res = json.load(open(STAGE / "b_resolved.json", encoding="utf-8"))
    todo = [cands[k] for k, v in res.items() if v["status"] == "ok"]
    path = STAGE / "c_described.json"
    done = json.load(open(path, encoding="utf-8")) if path.exists() else {}
    batch = [c for c in todo if c["id"] not in done]
    for i in range(0, len(batch), 25):
        part = batch[i:i + 25]
        items = "\n".join(f"- id={c['id']}；名稱={c['name']}；分類={'、'.join(c['tags'])}；簡介={c['description']}" for c in part)
        out = ask_list(DESC_PROMPT.format(items=items), temperature=0.4)
        for o in out:
            if isinstance(o, dict) and o.get("id") in cands and len(o.get("scenarios") or []) >= 2:
                done[o["id"]] = o
        json.dump(done, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(f"  C：{min(i + 25, len(batch))}/{len(batch)}，已完成 {len(done)}", flush=True)
    # 組成 catalog_ext.yaml（格式同 catalog_seed.yaml）
    ext = []
    for k, d in done.items():
        r, c = res[k], cands[k]
        mem, cpu, users = RES_BY_CAT.get(d.get("category"), ("256Mi", "250m", 150))
        ext.append({"id": k, "repo": r["repo"], "tag": r["tag"], "category": d.get("category") or "其他",
                    "scenarios": d["scenarios"][:4], "standalone": d.get("standalone"),
                    "requires": d.get("requires") or [], "resources": {"memory": mem, "cpu": cpu},
                    "per_replica_users": users, "notes": d.get("notes") or "",
                    "generated": True, "origin": c["origin"], "official": c["official"],
                    "upstream": {"name": c["name"], "description": c["description"],
                                 "url": c.get("source_code_url") or c.get("website_url"), "stars": c["stars"]}})
    header = ("# 自動擴充的軟體目錄（rag/expand_catalog.py 產生，請勿手動修改；要修正請寫進 catalog_seed.yaml，人工條目優先）\n"
              "# 軟體清單與英文簡介來源：awesome-selfhosted-data（CC BY-SA 3.0，https://github.com/awesome-selfhosted/awesome-selfhosted-data）\n"
              "#   與 docker-library/official-images（Apache 2.0）、docker-library/docs（MIT）。本檔為改編作品，以 CC BY-SA 3.0 釋出。\n"
              "# scenarios、category、standalone 由 Gemini 依英文簡介改寫（generated: true），尚未逐筆人工確認。\n")
    with open(KDIR / "catalog_ext.yaml", "w", encoding="utf-8") as f:
        f.write(header)
        yaml.safe_dump(ext, f, allow_unicode=True, sort_keys=False, width=200)
    print(f"C：寫出 {len(ext)} 筆 → {KDIR / 'catalog_ext.yaml'}")


# ── T 範本版（不用 LLM）：中文類別＋固定類別說明＋原始英文名稱、簡介、分類 ──────────────
# 類別說明由人工依 awesome-selfhosted 的分類撰寫；同類軟體共用同一句，避免每個軟體各自的口語寫法造成偏差。
TAG_ZH = {
    "Analytics": ("資料分析", "網站流量與使用數據分析"),
    "Archiving and Digital Preservation (DP)": ("網頁封存", "保存網頁與數位資料"),
    "Automation": ("自動化流程", "自動執行工作流程與排程任務"),
    "Booking and Scheduling": ("預約排程", "線上預約與排時間"),
    "Bookmarks and Link Sharing": ("書籤", "收藏與分享網址"),
    "Calendar & Contacts": ("行事曆", "行事曆與聯絡人同步"),
    "Communication - Custom Communication Systems": ("聊天", "團隊即時通訊與聊天"),
    "Communication - Email - Complete Solutions": ("電子郵件", "自架電子郵件伺服器"),
    "Communication - Email - Mailing Lists and Newsletters": ("電子報", "寄送電子報與郵件清單"),
    "Communication - Email - Webmail Clients": ("電子郵件", "網頁版收發信件"),
    "Communication - IRC": ("聊天", "IRC 聊天室"),
    "Communication - Social Networks and Forums": ("論壇", "論壇、社群討論區"),
    "Content Management Systems (CMS)": ("網站", "架設與管理網站內容"),
    "Customer Relationship Management (CRM)": ("客戶管理", "管理客戶與業務往來"),
    "DNS": ("網路工具", "網域名稱解析與廣告阻擋"),
    "Database Management": ("資料庫管理", "用網頁管理與查看資料庫"),
    "Document Management": ("文件管理", "掃描、整理與搜尋文件"),
    "Document Management - E-books": ("電子書", "管理與閱讀電子書"),
    "E-commerce": ("電子商務", "網路商店與購物車"),
    "Feed Readers": ("RSS 閱讀", "訂閱與閱讀新聞、部落格更新"),
    "File Transfer & Synchronization": ("檔案分享", "雲端硬碟與檔案同步"),
    "File Transfer - Object Storage & File Servers": ("檔案儲存", "物件儲存與檔案伺服器"),
    "File Transfer - Peer-to-peer Filesharing": ("檔案分享", "點對點檔案傳送"),
    "File Transfer - Single-click & Drag-n-drop Upload": ("檔案分享", "拖拉上傳後產生分享連結"),
    "File Transfer - Web-based File Managers": ("檔案分享", "用網頁瀏覽與管理伺服器上的檔案"),
    "Games": ("遊戲", "網頁遊戲與遊戲伺服器"),
    "Games - Administrative Utilities & Control Panels": ("遊戲伺服器", "管理遊戲伺服器"),
    "Generative Artificial Intelligence (GenAI)": ("AI 工具", "自架生成式 AI 介面"),
    "Health and Fitness": ("健康", "記錄健康與運動"),
    "Internet of Things (IoT)": ("智慧家庭", "連接與控制物聯網裝置"),
    "Inventory Management": ("庫存管理", "管理物品與庫存"),
    "Manufacturing": ("生產管理", "工廠與製造流程管理"),
    "Maps and Global Positioning System (GPS)": ("地圖", "地圖與位置追蹤"),
    "Media Management": ("影音", "整理影音收藏"),
    "Media Streaming - Audio Streaming": ("音樂", "串流播放音樂與 Podcast"),
    "Media Streaming - Multimedia Streaming": ("影音", "家庭影音串流"),
    "Media Streaming - Video Streaming": ("影音", "影片上傳與串流"),
    "Miscellaneous": ("其他", "其他工具"),
    "Money, Budgeting & Management": ("記帳", "記帳與預算管理"),
    "Network Utilities": ("網路工具", "網路監測與管理工具"),
    "Note-taking & Editors": ("維基或筆記", "線上筆記與文件編輯"),
    "Office Suites": ("辦公室文件", "線上文件、試算表、簡報協作"),
    "Password Managers": ("密碼管理", "保存與分享密碼"),
    "Pastebins": ("貼文分享", "貼上文字或程式碼產生分享連結"),
    "Personal Dashboards": ("首頁儀表板", "個人首頁與服務入口"),
    "Photo Galleries": ("相片", "相簿與照片分享"),
    "Polls and Events": ("表單或問卷", "投票、問卷與活動報名"),
    "Proxy": ("網路工具", "反向代理與網址轉發"),
    "Remote Access": ("遠端連線", "從瀏覽器遠端操作電腦"),
    "Resource Planning": ("企業資源規劃", "企業資源規劃（ERP）"),
    "Search Engines": ("搜尋引擎", "全文搜尋與網站搜尋"),
    "Self-hosting Solutions": ("自架管理", "管理自架伺服器與服務"),
    "Software Development - API Management": ("開發工具", "API 管理與測試"),
    "Software Development - Feature Toggle": ("開發工具", "功能開關管理"),
    "Software Development - IDE & Tools": ("線上寫程式", "線上程式編輯與開發工具"),
    "Software Development - Localization": ("翻譯管理", "多語系翻譯管理"),
    "Software Development - Low Code": ("低程式碼", "不寫程式就能建立內部工具與資料表"),
    "Software Development - Project Management": ("專案管理", "專案、程式碼與議題管理"),
    "Software Development - Testing": ("開發工具", "軟體測試工具"),
    "Task Management & To-do Lists": ("待辦清單", "待辦事項與看板"),
    "Ticketing": ("客服工單", "客服工單與問題追蹤"),
    "Time Tracking": ("工時記錄", "記錄工作時間"),
    "URL Shorteners": ("短網址", "縮短網址"),
    "Video Surveillance": ("監視錄影", "管理監視攝影機"),
    "Web Servers": ("網站", "網頁伺服器"),
    "Wikis": ("維基或筆記", "團隊知識庫與維基"),
}
OFFICIAL_ZH = {}
for _ids, _cat, _desc in [
    ("aerospike arangodb cassandra clickhouse couchbase couchdb crate influxdb neo4j orientdb percona rethinkdb",
     "資料庫", "儲存與查詢資料的資料庫"),
    ("almalinux alt amazonlinux archlinux debian fedora mageia oraclelinux photon rockylinux ubuntu neurodebian",
     "作業系統基底", "Linux 作業系統基底映像檔"),
    ("amazoncorretto azul-zulu clojure dart eclipse-temurin elixir erlang golang groovy haskell haxe hylang "
     "ibm-semeru-runtimes ibmjava jruby julia nim openjdk perl pypy r-base rakudo-star ruby rust sapmachine swift "
     "swipl bash gcc", "執行環境", "程式語言執行環境"),
    ("composer gradle maven", "建置工具", "程式建置與套件管理工具"),
    ("jetty open-liberty tomcat tomee websphere-liberty", "網站", "Java 網頁應用程式伺服器"),
    ("varnish hitch krakend api-firewall spiped", "網路工具", "快取、加密或 API 閘道"),
    ("nats eclipse-mosquitto zookeeper", "訊息佇列", "服務之間傳遞訊息"),
    ("storm spark", "資料分析", "大量資料運算"),
    ("chronograf kapacitor telegraf kibana logstash fluentd", "監控", "收集與檢視監控數據、日誌"),
    ("solr", "搜尋引擎", "全文搜尋引擎"),
    ("backdrop", "網站", "架設與管理網站內容"),
    ("bonita convertigo", "低程式碼", "建立流程與內部應用"),
    ("friendica", "論壇", "社群網路"),
    ("geonetwork", "地圖", "地理資料目錄"),
    ("monica", "客戶管理", "記錄人際關係與聯絡人"),
    ("postfixadmin", "電子郵件", "管理郵件伺服器帳號"),
    ("rocket.chat", "聊天", "團隊即時通訊與聊天"),
    ("teamspeak", "聊天", "語音聊天"),
    ("silverpeas xwiki", "維基或筆記", "團隊知識庫與協作"),
    ("sonarqube", "開發工具", "程式碼品質檢查"),
    ("satosa", "網路工具", "身分驗證代理"),
    ("eggdrop irssi", "聊天", "IRC 聊天機器人與客戶端"),
    ("gazebo ros", "其他", "機器人模擬與開發"),
]:
    for _i in _ids.split():
        OFFICIAL_ZH[_i] = (_cat, _desc)


def stage_template():
    cands = {c["id"]: c for c in json.load(open(STAGE / "a_candidates.json", encoding="utf-8"))}
    res = json.load(open(STAGE / "b_resolved.json", encoding="utf-8"))
    ext = []
    for k, r in res.items():
        if r["status"] != "ok" or k not in cands:
            continue
        c = cands[k]
        if c["origin"] == "docker-official":
            cat, zh = OFFICIAL_ZH.get(k, ("其他", "其他工具"))
        else:
            tags = [t for t in c["tags"] if t in TAG_ZH] or ["Miscellaneous"]
            cat = TAG_ZH[tags[0]][0]
            zh = "、".join(dict.fromkeys(TAG_ZH[t][1] for t in tags))
        mem, cpu, users = RES_BY_CAT.get(cat, ("256Mi", "250m", 150))
        ext.append({"id": k, "repo": r["repo"], "tag": r["tag"], "category": cat,
                    "scenarios": [f"{cat}：{zh}", f"{c['name']}：{c['description']}", "分類：" + ", ".join(c["tags"])],
                    "standalone": None, "requires": [], "resources": {"memory": mem, "cpu": cpu},
                    "per_replica_users": users, "notes": "", "generated": True, "writer": "template",
                    "origin": c["origin"], "official": c["official"],
                    "upstream": {"name": c["name"], "description": c["description"],
                                 "url": c.get("source_code_url") or c.get("website_url"), "stars": c["stars"]}})
    header = ("# 自動擴充的軟體目錄：範本版（rag/expand_catalog.py template 產生，不使用 LLM）\n"
              "# 軟體清單與英文簡介來源：awesome-selfhosted-data（CC BY-SA 3.0）、docker-library/official-images（Apache 2.0）。\n"
              "# 本檔為改編作品，以 CC BY-SA 3.0 釋出。中文類別與類別說明為人工撰寫的固定對照（TAG_ZH、OFFICIAL_ZH）。\n")
    with open(KDIR / "catalog_ext_template.yaml", "w", encoding="utf-8") as f:
        f.write(header)
        yaml.safe_dump(ext, f, allow_unicode=True, sort_keys=False, width=300)
    print(f"T：寫出 {len(ext)} 筆 → {KDIR / 'catalog_ext_template.yaml'}")


if __name__ == "__main__":
    {"select": stage_select, "resolve": stage_resolve, "describe": stage_describe, "template": stage_template}[sys.argv[1]]()
