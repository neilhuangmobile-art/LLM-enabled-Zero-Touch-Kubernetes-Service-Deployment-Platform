"""軟體目錄擴充（2026-11-19）：補常見但目錄沒有的軟體類別，並補強幾個常檢索不到的既有項目的情境描述。

依據：開發題組的錯誤歸因顯示「正確答案不在候選裡」佔映像檔錯誤的三分之一（dev_scenario 24 題、dev_claude 30 題）。
這裡只補「軟體類別」（類別名稱與用途參考 awesome-selfhosted、各軟體官方說明），情境描述是一般化的說法，
不放任何測試題原句（catalog_seed.yaml 開頭的規則）。tag 由 rag/build_catalog.py 到 Docker Hub 驗證。

  python rag/knowledge/seed_additions_20261119.py   # 寫入 catalog_seed.yaml（重複執行不會重複加）
  python rag/build_catalog.py && python -c "from rag import field_retriever as f; f.build_index()"
"""
import os
import re

SEED = os.path.join(os.path.dirname(os.path.abspath(__file__)), "catalog_seed.yaml")

NEW = '''
# ── 2026-11-19 擴充：補常見類別（awesome-selfhosted）──────────────────────
- id: bookstack
  repo: linuxserver/bookstack
  tag: latest
  category: 維基或筆記
  scenarios: ["用書本、章節整理的知識庫", "團隊的操作手冊與規章文件", "需要權限管理的內部文件網站"]
  standalone: false
  requires: [mariadb]
  resources: {memory: 256Mi, cpu: 250m}
  per_replica_users: 100
  notes: "需要 MySQL／MariaDB"

- id: mediawiki
  repo: library/mediawiki
  tag: lts
  category: 維基或筆記
  scenarios: ["像維基百科那樣大家一起編輯的百科網站", "社群或課程的共同編輯知識庫", "有編輯歷史可以回溯的條目式文件"]
  standalone: true
  resources: {memory: 256Mi, cpu: 250m}
  per_replica_users: 100
  notes: "第一次開啟要跑安裝精靈；小型使用可選 SQLite"

- id: hedgedoc
  repo: linuxserver/hedgedoc
  tag: latest
  category: 共筆
  scenarios: ["多人同時編輯的 Markdown 共筆", "會議記錄、課堂筆記即時共寫", "可以分享連結的線上筆記"]
  standalone: false
  requires: [mariadb]
  resources: {memory: 256Mi, cpu: 250m}
  per_replica_users: 50
  notes: "需要資料庫"

- id: kanboard
  repo: kanboard/kanboard
  tag: latest
  category: 專案管理
  scenarios: ["看板式的待辦與工作分派", "小團隊追蹤任務進度", "把工作拆成卡片在欄位間移動"]
  standalone: true
  resources: {memory: 128Mi, cpu: 100m}
  per_replica_users: 50
  notes: "預設用 SQLite，單一容器即可"

- id: redmine
  repo: library/redmine
  tag: "5"
  category: 專案管理
  scenarios: ["回報與追蹤程式錯誤的系統", "問題單、需求單的處理流程", "專案的議題追蹤與工時紀錄"]
  standalone: true
  resources: {memory: 512Mi, cpu: 250m}
  per_replica_users: 50
  notes: "小型使用可用 SQLite"

- id: influxdb
  repo: library/influxdb
  tag: "2"
  category: 資料庫
  scenarios: ["存放隨時間變化的感測數值", "時間序列資料庫", "記錄設備每隔幾秒回報的數據"]
  standalone: true
  resources: {memory: 512Mi, cpu: 250m}
  per_replica_users: 0
  notes: "資料要保存需掛載資料目錄"

- id: loki
  repo: grafana/loki
  tag: latest
  category: 日誌
  scenarios: ["集中收集與查詢多台機器的紀錄檔", "搭配 Grafana 查看日誌"]
  standalone: true
  resources: {memory: 256Mi, cpu: 250m}
  per_replica_users: 0

- id: fluent-bit
  repo: fluent/fluent-bit
  tag: latest
  category: 日誌
  scenarios: ["在背景收集紀錄檔並轉送到集中的地方", "輕量的日誌收集程式"]
  standalone: true
  resources: {memory: 64Mi, cpu: 50m}
  per_replica_users: 0

- id: netdata
  repo: netdata/netdata
  tag: stable
  category: 監控
  scenarios: ["即時看主機的 CPU、記憶體、硬碟用量", "伺服器效能儀表板"]
  standalone: true
  resources: {memory: 256Mi, cpu: 250m}
  per_replica_users: 20
  notes: "要看主機資訊需要額外權限"

- id: mosquitto
  repo: library/eclipse-mosquitto
  tag: "2"
  category: 訊息佇列
  scenarios: ["物聯網裝置傳資料用的 MQTT 伺服器", "感測器與控制器之間的訊息轉送"]
  standalone: true
  resources: {memory: 64Mi, cpu: 50m}
  per_replica_users: 0

- id: nats
  repo: library/nats
  tag: "2"
  category: 訊息佇列
  scenarios: ["輕量快速的訊息傳遞", "程式之間發布與訂閱訊息"]
  standalone: true
  resources: {memory: 64Mi, cpu: 100m}
  per_replica_users: 0

- id: valkey
  repo: valkey/valkey
  tag: "8"
  category: 快取
  scenarios: ["與 Redis 相容的開源快取", "暫存資料、計數器"]
  standalone: true
  resources: {memory: 128Mi, cpu: 100m}
  per_replica_users: 0

- id: gogs
  repo: gogs/gogs
  tag: latest
  category: 程式碼管理
  scenarios: ["輕量的自架 Git 程式碼倉庫", "小團隊放程式碼、看版本紀錄"]
  standalone: true
  resources: {memory: 128Mi, cpu: 100m}
  per_replica_users: 50

- id: ctfd
  repo: ctfd/ctfd
  tag: latest
  category: 競賽
  scenarios: ["資安搶旗（CTF）競賽平台", "解題計分與排行榜"]
  standalone: true
  resources: {memory: 512Mi, cpu: 250m}
  per_replica_users: 100

- id: rclone
  repo: rclone/rclone
  tag: latest
  category: 備份同步
  scenarios: ["把檔案同步或備份到雲端硬碟", "定期複製資料到另一個地方"]
  standalone: true
  resources: {memory: 128Mi, cpu: 100m}
  per_replica_users: 0

- id: restic
  repo: restic/restic
  tag: latest
  category: 備份同步
  scenarios: ["加密、增量的檔案備份", "保留多個版本的備份"]
  standalone: true
  resources: {memory: 128Mi, cpu: 100m}
  per_replica_users: 0

- id: curl
  repo: curlimages/curl
  tag: latest
  category: 工具
  scenarios: ["定時去呼叫某個網址", "定期觸發其他系統的網頁或 API"]
  standalone: true
  resources: {memory: 32Mi, cpu: 50m}
  per_replica_users: 0
  notes: "通常搭配定時執行"

- id: wiremock
  repo: wiremock/wiremock
  tag: latest
  category: 開發工具
  scenarios: ["模擬後端 API 回傳假資料", "前端開發時先用的假伺服器"]
  standalone: true
  resources: {memory: 256Mi, cpu: 250m}
  per_replica_users: 50

- id: scipy-notebook
  repo: jupyter/scipy-notebook
  tag: latest
  category: 開發工具
  scenarios: ["含科學計算套件的 Jupyter 筆記本", "資料分析、畫圖的線上環境"]
  standalone: true
  resources: {memory: 1Gi, cpu: 500m}
  per_replica_users: 1
'''

# 既有項目補情境（一般化說法）
MORE_SCENARIOS = {
    "wordpress": ["自己可以在後台改文字、換照片、發消息的網站", "形象官網或作品集網站，要有管理後台"],
    "dokuwiki": ["團隊知識庫、操作步驟文件", "共同整理的筆記與說明文件"],
    "redis": ["減少資料庫查詢、讓網頁回應變快", "流量尖峰時把常用資料先記起來"],
    "rabbitmq": ["大量請求同時湧入時讓工作排隊依序處理", "把寄信、通知等耗時工作交給背景處理"],
    "uptime-kuma": ["固定檢查多個網站是否正常並在掛掉時通知", "記錄網站的正常運作時間"],
}


def main():
    s = open(SEED, encoding="utf-8").read()
    if "2026-11-19 擴充" not in s:
        s = s.rstrip() + "\n" + NEW
    for sid, extra in MORE_SCENARIOS.items():
        m = re.search(rf"^- id: {re.escape(sid)}\n(?:  .*\n)*?  scenarios: \[(.*)\]$", s, re.M)
        if not m:
            raise SystemExit(f"找不到 {sid} 的 scenarios")
        cur = m.group(1)
        add = [x for x in extra if f'"{x}"' not in cur]
        if add:
            new = cur + ", " + ", ".join(f'"{x}"' for x in add)
            s = s[:m.start(1)] + new + s[m.end(1):]
    open(SEED, "w", encoding="utf-8").write(s)
    print("寫入", SEED)


if __name__ == "__main__":
    main()
