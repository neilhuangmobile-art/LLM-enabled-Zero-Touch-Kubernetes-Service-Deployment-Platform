# API 端點文件

## Model Server（port 8765）

啟動：`python core/model_server.py`

### GET /health

健康檢查。

**Response**
```json
{"status": "ok", "model_loaded": true}
```

---

### POST /infer

自然語言轉 K8s JSON。

**Request**
```json
{
  "prompt": "deploy 3 nginx pods on port 80",
  "max_new_tokens": 128,
  "temperature": 0.3
}
```

**Response（成功）**
```json
{
  "result": {
    "pods": 3,
    "image": "nginx:latest",
    "app_name": "nginx",
    "port": 80
  }
}
```

**Response（解析失敗）**
```json
{
  "result": {
    "error": "解析失敗",
    "raw": "..."
  }
}
```

---

### GET /unload

卸載模型，釋放 VRAM。

**Response**
```json
{"status": "unloaded"}
```

---

## Web Demo（port 5000）

啟動：`python web_demo.py`

### GET /

Web UI 首頁。

---

### GET /api/status

系統狀態。

**Response**
```json
{
  "model_ready": true,
  "model_loading": false,
  "k8s": true
}
```

---

### POST /api/deploy

執行部署（非同步，AI 解析後立即回傳，K8s 操作在背景執行）。

**Request**
```json
{"input": "部署 3 個 nginx，port 80"}
```

**Response（成功）**
```json
{
  "parsed": {
    "pods": 3,
    "image": "nginx:latest",
    "app_name": "nginx",
    "port": 80
  },
  "k8s": true
}
```

**Response（錯誤）**
```json
{"error": "輸入不能為空或過短"}
```

---

### GET /api/pods

取得 K8s Pod 列表。

**Response**
```json
{
  "pods": [
    {
      "name": "nginx-7d9f8b-xyz",
      "app": "nginx",
      "phase": "Running",
      "ip": "10.244.0.5",
      "node": "docker-desktop",
      "age": "2024-01-01 12:00"
    }
  ]
}
```

---

### GET /api/deployments

取得 K8s Deployment 列表。

**Response**
```json
{
  "deployments": [
    {
      "name": "nginx",
      "replicas": 3,
      "ready": 3,
      "image": "nginx:latest",
      "age": "2024-01-01 12:00"
    }
  ]
}
```

---

## Python 模組 API

### `llama_client.ask_llama(prompt_text: str) -> dict`

主要推論入口。自動管理 Model Server 生命週期。

```python
from llama_client import ask_llama

result = ask_llama("deploy 2 redis pods with 256Mi memory")
# {"pods": 2, "image": "redis:7-alpine", "app_name": "redis", "memory": "256Mi"}
```

---

### `llama_client.save_gold_sample(user_input, corrected_json)`

儲存標註資料到訓練集。

```python
from llama_client import save_gold_sample

save_gold_sample("2 redis pods", {"pods": 2, "image": "redis:7-alpine", "app_name": "redis"})
```

---

### `agents.orchestrator.orchestrate(manifest_dict, save_report=False) -> dict`

多代理評估。

```python
from agents.orchestrator import orchestrate

result = orchestrate(manifest_dict, save_report=True)
# {
#   "decision": "approve" | "warn" | "block",
#   "reason": "...",
#   "agents": {"security": ..., "cost": ..., "perf": ...}
# }
```

---

### `rag.retriever.augment_prompt(prompt_text, top_k=2, max_context_chars=500) -> str`

RAG 知識增強。

```python
from rag.retriever import augment_prompt

enhanced = augment_prompt("deploy nginx with high memory")
```

---

### `healer.pod_watcher.scan_once(namespace="default") -> list`

單次掃描異常 Pod。

```python
from healer.pod_watcher import scan_once

anomalies = scan_once()
# [{"pod": "nginx-xyz", "issue": "CrashLoopBackOff", ...}]
```
