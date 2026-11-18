# RAG 介面說明（給多代理架構使用，2026-11-15）

推理代理需要查資料時，只呼叫 `rag/field_retriever.py` 的這幾個函式。RAG 之後會持續最佳化（門檻、檢索方式），**函式名稱與回傳格式不會變**。

```python
from rag import field_retriever as fr
from rag.router import route
```

## 1. 什麼時候要查：`route()`

```python
route(action, field=None, base_complete=False, has_extra=False, scenario=False) -> list[str]
```

回傳要查的資料庫；**空清單代表不用查**（例如六個基礎欄位齊全的部署走快路徑、擴縮刪除等操作）。

## 2. 補欄位：`retrieve_for_field()`

```python
fr.retrieve_for_field(query, field, top_k=3, version=None, method=None) -> list[dict]
```

| 參數 | 說明 |
|---|---|
| `query` | 使用者原句 |
| `field` | `image`、`port`、`volume`、`env`、`memory`、`cpu`、`replicas`（`app_name` 不由 RAG 決定，一律回傳空） |
| `version` | 已經知道是哪個軟體時傳軟體名稱或映像檔（例如 `"ghost"`、`"postgres:16"`），直接查目錄，分數 1.0 |

回傳（依分數排序；**分數不足回傳空清單，代理應改為反問使用者**）：

```python
[{"value": "ghost:5", "field": "image", "score": 0.77,
  "evidence": "軟體目錄 ghost：個人部落格或日記網站…",       # 可直接顯示在確認卡「建議值，依據：…」
  "source": "catalog#ghost",
  "related": {"image": "ghost:5", "ports": [2368], "volumes": ["/var/lib/ghost/content"],
              "standalone": True, "requires": [], "env_required": [], "standalone_env": {...},
              "resources": {"memory": "256Mi", "cpu": "250m"}, "per_replica_users": 150, "notes": "…"}}]
```

- `related` 一次帶回同一個軟體的所有已知資訊（連接埠與資料目錄是從 Docker Hub 映像檔設定讀出的，可信），補完映像檔後其他欄位不必再查一次。
- `standalone: False` 表示需要搭配其他服務（`requires`），系統一次只部署一個服務時要提醒使用者，或套用 `standalone_env` 改成單一容器。
- `per_replica_users` 是每個副本約可服務的同時在線人數；`0` 表示資料庫等有狀態服務，副本固定 1。

## 3. 額外需求：`retrieve_requirements()`

```python
fr.retrieve_requirements(query, top_k=5) -> list[dict]
# [{"need": "persistent_storage", "title": "需要保存資料的空間", "score": 0.78,
#   "setting": "建立 PersistentVolumeClaim，掛載到映像檔的資料目錄", "k8s": ["PersistentVolumeClaim", "volumeMounts"],
#   "values": "容量：使用者有說就照用；沒說時…", "confirm": True}]
```

需求代碼：`persistent_storage`、`scheduled_job`、`background_no_expose`、`custom_command`、`credentials`、`env_config`、`external_port`、`extra_ports`、`scale_by_users`、`high_availability`、`short_lived`、`resource_hint`、`multi_service`。`confirm: True` 的要在確認卡讓使用者確認。

## 4. 失敗經驗：`retrieve_lessons()`

```python
fr.retrieve_lessons(query, stage=None, top_k=3)
# [{"id": "L07", "title": "使用者給的密碼被系統產生的密碼覆蓋", "wrong": "…", "correct": "…", "fields": [...], "status": "open"}]
```

`stage` 可用：`意圖判斷`、`補欄位`、`額外欄位`、`審查`、`部署後修復`。建議放進推理代理的提示，提醒它避免重犯。

## 5. 知識與疑難排解：`retrieve_knowledge()`

```python
fr.retrieve_knowledge(query, kind="concept", top_k=3)       # K8s 概念（官方文件＋專案文件）
fr.retrieve_knowledge(query, kind="troubleshoot", top_k=3)  # 症狀 → 原因 → 修法
```

## 6. 已知軟體直接查：`lookup_software()`

```python
fr.lookup_software("mongodb")   # 會轉成正式名稱 mongo:7，找不到回傳 None
```

## 7. 確認卡回饋：`log_feedback()`（之後多臂老虎機使用）

```python
fr.log_feedback(query, field, suggested, final)   # 使用者在確認卡上按確認或改值後呼叫
```

## 注意

- 第一次呼叫會載入向量模型（約 10 秒），之後每次約 30 毫秒。
- 沒有向量索引時（例如 CI）自動改用 BM25，回傳格式相同。
- 索引重建：`python -c "from rag import field_retriever as fr; fr.build_index()"`；軟體目錄重建：`python rag/build_catalog.py`。
- 知識庫內容在 `rag/knowledge/`；**不可把開發集、盲測集的題目放進去**（`tests/test_field_retriever.py` 會檢查）。
