"""
training/fetch_hf_dataset.py
從 Hugging Face 下載公開資料集，轉換為本專案格式，補充訓練資料。

執行：python training/fetch_hf_dataset.py
產出：追加到 dataset/finetune_samples.jsonl

支援的資料來源：
  1. 本地範本擴充（無需網路，隨機組合增量生成）
  2. HuggingFace: iezepov/kubernetes-manifests-instructions（K8s 專用）
  3. HuggingFace: json 格式的 instruction-following 資料集（通用）
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import re
import random
from core.config import DATASET_PATH, DATASET_DIR

random.seed(None)  # 每次產生不同亂數，避免重複資料

# ══════════════════════════════════════════════════════════════════
# 來源 1：擴充本地範本（離線可用，快速產生 1000+ 筆多樣化資料）
# ══════════════════════════════════════════════════════════════════

# 大幅擴充 app 名稱、image、數字範圍
EXTENDED_IMAGES = [
    "nginx:latest", "nginx:1.25-alpine", "nginx:1.24",
    "redis:7-alpine", "redis:latest", "redis:6-alpine",
    "postgres:15", "postgres:14-alpine", "postgres:16",
    "mysql:8.0", "mysql:5.7", "mariadb:10.11",
    "mongo:6", "mongo:5", "mongo:7",
    "node:20-alpine", "node:18-slim", "node:16-alpine",
    "python:3.11-slim", "python:3.10-alpine", "python:3.12-slim",
    "golang:1.21-alpine", "golang:1.22-alpine",
    "openjdk:17-slim", "openjdk:21-slim", "eclipse-temurin:17",
    "rabbitmq:3-management", "rabbitmq:3-alpine",
    "grafana/grafana:latest", "grafana/grafana:10.0.0",
    "prom/prometheus:latest", "prom/prometheus:v2.48.0",
    "elasticsearch:8.11.0", "elasticsearch:7.17.0",
    "apache/kafka:latest", "bitnami/kafka:3.6",
    "traefik:v3.0", "traefik:v2.10",
    "haproxy:2.8-alpine", "envoyproxy/envoy:v1.28-latest",
    "vault:1.15", "hashicorp/consul:1.17",
    "minio/minio:latest", "bitnami/minio:latest",
    "jenkins/jenkins:lts-jdk17", "gitlab/gitlab-ce:latest",
    "sonarqube:community", "nexus3:latest",
    "ubuntu:22.04", "debian:bookworm-slim", "alpine:3.19",
    "busybox:latest", "curlimages/curl:latest",
    "k8s.gcr.io/pause:3.9", "gcr.io/distroless/base:latest",
]

EXTENDED_APPS = [
    "web-frontend", "api-gateway", "user-service", "auth-service",
    "payment-service", "order-service", "notification-service",
    "data-processor", "cache-server", "db-primary", "db-replica",
    "message-broker", "log-collector", "metrics-server",
    "search-engine", "file-storage", "image-resizer",
    "email-worker", "report-generator", "scheduler",
    "proxy-server", "load-balancer", "session-store",
    "event-bus", "task-queue", "worker-pool", "cron-runner",
    "backup-service", "audit-logger", "config-server",
    "service-registry", "circuit-breaker", "rate-limiter",
    "content-delivery", "media-transcoder", "pdf-renderer",
    "ml-inference", "feature-store", "data-pipeline",
    "etl-worker", "stream-processor", "batch-processor",
]

EXTENDED_PORTS = [80, 443, 3000, 3001, 3306, 5000, 5432, 5672, 6379,
                  8080, 8081, 8443, 8888, 9090, 9092, 9200, 9300,
                  15672, 27017, 28017]
EXTENDED_MEMORIES = ["64Mi", "128Mi", "256Mi", "512Mi", "1Gi", "2Gi", "4Gi"]

# 大範圍 pods（1~50，涵蓋真實場景）
POD_WEIGHTS = (
    list(range(1, 11)) * 5 +   # 1~10 各 5 份（常見）
    list(range(11, 21)) * 2 +  # 11~20 各 2 份
    list(range(21, 51))        # 21~50 各 1 份
)

EN_EXTENDED = [
    "deploy {n} pods of {app} using {image}",
    "start {n} replicas of {app} with image {image}",
    "run {app} with {n} pods, image: {image}",
    "launch {n} instances of {image} for {app}",
    "scale {app} to {n} pods using {image}",
    "i need {n} pods for {app} running {image}",
    "create deployment {app} with {n} replicas and {image}",
    "spin up {n} {image} containers named {app}",
    "please deploy {n} {image} pods as {app}",
    "setup {app} service with {n} replicas of {image}",
    "initialize {app} using {image}, need {n} pods",
    "provision {n} pods running {image} for {app}",
    "k8s: create {app}, image={image}, replicas={n}",
    "get {app} running: {n} pods, {image}",
    "bring up {n}-pod deployment for {app} using {image}",
    "deploy {image} as {app} with {n} instances",
    "we need {n} running pods for {app}, use {image}",
    "start up {n}-pod deployment for {app} ({image})",
    "create {n} {image} pods for {app} service",
    "run {n} replicas of {app} container ({image})",
    "kubectl: deploy {app} x{n} with {image}",
    "roll out {app} using {image}, target {n} replicas",
    "bring {app} online with {n} {image} pods",
    "configure {app}: image={image}, desired_replicas={n}",
    "new deployment: name={app}, image={image}, count={n}",
]

ZH_EXTENDED = [
    "部署 {n} 個 {app} 的 pod，使用 {image}",
    "幫我起 {n} 個 {app}，映像檔是 {image}",
    "建立 {app} 服務，{n} 個 replica，使用 {image}",
    "我需要 {n} 個 {app} pod，image 用 {image}",
    "請部署 {image} 作為 {app}，需要 {n} 個副本",
    "把 {app} 擴展到 {n} 個 pod，用 {image}",
    "建立 {n} 個 {image} 容器，命名為 {app}",
    "幫我跑 {app}，{n} 個 pod，image: {image}",
    "啟動 {app}：{n} 個 pod，使用 {image} 映像",
    "k8s 部署 {app}，image={image}，replicas={n}",
    "我要 {n} 個 {app} 的 replica，image 是 {image}",
    "新增 {app} deployment，{n} 個 pod，跑 {image}",
    "部署 {image} 服務叫做 {app}，要 {n} 個",
    "幫忙建 {n} 個 pod 跑 {app}，容器用 {image}",
    "起一個 {app} 的 deployment，{n} 個副本，{image}",
    "生產環境跑 {n} 個 {app}，用 {image}",
    "幫我把 {app} 開 {n} 個，image 選 {image}",
    "線上部署 {app}，{image}，{n} 個副本",
    "開 {n} 個 {image} 叫做 {app}",
    "滾動更新 {app}，新版本 {image}，{n} 個 pod",
    "部署新版 {app}：{image}，調整到 {n} 個副本",
    "幫我建一個 {n} 副本的 {app}，跑 {image}",
]

PORT_EN_EXT = [
    "deploy {n} pods of {app} using {image}, expose port {port}",
    "start {app} with {n} replicas, image {image}, open port {port}",
    "run {image} as {app}, {n} pods, port {port}",
    "launch {n} {app} pods ({image}), container port {port}",
    "create {app}: image={image}, replicas={n}, port={port}",
    "setup {n} {app} pods using {image}, container port {port}",
    "deploy {app} ({image}) with {n} pods, binding port {port}",
    "bring up {n} {image} pods named {app}, listen on port {port}",
]

PORT_ZH_EXT = [
    "部署 {n} 個 {app}，image {image}，開放 {port} port",
    "幫我跑 {app}，{n} 個 pod，用 {image}，port {port}",
    "建立 {app} deployment，{image}，{n} 個副本，開 port {port}",
    "起 {n} 個 {image} 的 {app}，container port {port}",
    "部署 {app}：image={image}，replicas={n}，port={port}",
    "幫我起 {n} 個 {app}，{image}，開放 port {port}",
    "開 {n} 個 {app} pod，{image}，對外 port {port}",
    "建 {app}，{image}，{n} 副本，暴露 {port} 號埠",
]

MEM_EN_EXT = [
    "deploy {n} pods of {app} using {image}, memory limit {mem}",
    "start {app} with {n} replicas ({image}), set memory to {mem}",
    "run {image} as {app}, {n} pods, memory={mem}",
    "create {app} deployment, image {image}, {n} replicas, {mem} memory",
    "spin up {n} {image} pods for {app}, memory {mem}",
    "deploy {app} with resource limit: image={image}, replicas={n}, memory={mem}",
]

MEM_ZH_EXT = [
    "部署 {n} 個 {app}，image {image}，記憶體限制 {mem}",
    "幫我跑 {app}，{n} 個 pod，用 {image}，記憶體 {mem}",
    "建立 {app}，{image}，{n} 個副本，ram 限制 {mem}",
    "起 {n} 個 {image} 的 {app}，memory limit {mem}",
    "部署 {app}：image={image}，replicas={n}，memory={mem}",
    "開 {n} 個 {app}，{image}，記憶體上限 {mem}",
]


def _gen_extended(count: int = 2000) -> list:
    """生成多樣化訓練樣本，數字範圍擴展到 1~50。"""
    samples = []
    for _ in range(count):
        n     = random.choice(POD_WEIGHTS)
        app   = random.choice(EXTENDED_APPS)
        image = random.choice(EXTENDED_IMAGES)
        r     = random.random()

        if r < 0.35:      # 35% 純基本
            tmpl = random.choice(EN_EXTENDED + ZH_EXTENDED)
            inp  = tmpl.format(n=n, app=app, image=image)
            out  = {"pods": n, "image": image, "app_name": app}

        elif r < 0.55:    # 20% 含 port
            port = random.choice(EXTENDED_PORTS)
            tmpl = random.choice(PORT_EN_EXT + PORT_ZH_EXT)
            inp  = tmpl.format(n=n, app=app, image=image, port=port)
            out  = {"pods": n, "image": image, "app_name": app, "port": port}

        elif r < 0.70:    # 15% 含 memory
            mem  = random.choice(EXTENDED_MEMORIES)
            tmpl = random.choice(MEM_EN_EXT + MEM_ZH_EXT)
            inp  = tmpl.format(n=n, app=app, image=image, mem=mem)
            out  = {"pods": n, "image": image, "app_name": app, "memory": mem}

        elif r < 0.82:    # 12% 含 port + memory
            port = random.choice(EXTENDED_PORTS)
            mem  = random.choice(EXTENDED_MEMORIES)
            inp  = (f"deploy {n} pods of {app} using {image}, "
                    f"port {port}, memory {mem}")
            out  = {"pods": n, "image": image, "app_name": app,
                    "port": port, "memory": mem}

        else:             # 18% 模糊/口語化表達
            casual = [
                f"I want {n} {app} pods",
                f"give me {n} pods running {image}",
                f"spin up {n} containers for {app}",
                f"我要 {n} 個 {app}",
                f"幫我跑 {n} 個 {image}",
                f"部署 {n} 個服務 {app}",
                f"{n} replicas of {app}",
                f"run {n}x {image}",
            ]
            inp = random.choice(casual)
            out = {"pods": n, "image": image, "app_name": app}

        samples.append({"input": inp, "output": out})

    random.shuffle(samples)
    return samples


# ══════════════════════════════════════════════════════════════════
# 來源 2：HuggingFace（需要網路）
# ══════════════════════════════════════════════════════════════════
def _try_hf_k8s_dataset() -> list:
    """
    嘗試從 HuggingFace 下載 K8s 相關 instruction dataset。
    下載失敗時靜默跳過。
    """
    samples = []
    try:
        from datasets import load_dataset
        print("🌐 嘗試從 HuggingFace 下載 K8s 資料集...")

        # 這個資料集包含 K8s YAML manifest 與說明
        ds = load_dataset("patrickloeber/kubernetes-manifests", split="train", trust_remote_code=True)

        for item in ds:
            # 反向：從 YAML manifest 提取 replicas/image，生成自然語言 input
            text = item.get("text", "") or item.get("content", "")
            if not text:
                continue

            replicas_m = re.search(r'replicas:\s*(\d+)', text)
            image_m    = re.search(r'image:\s*(\S+)',    text)
            name_m     = re.search(r'name:\s*(\S+)',     text)

            if not (replicas_m and image_m):
                continue

            n     = int(replicas_m.group(1))
            image = image_m.group(1).strip('"\'')
            app   = name_m.group(1).strip('"\'') if name_m else "auto-app"

            if not (1 <= n <= 100):
                continue

            inp = f"deploy {n} replicas of {app} using {image}"
            out = {"pods": n, "image": image, "app_name": app}
            samples.append({"input": inp, "output": out})

        print(f"✅ HuggingFace K8s 資料集：{len(samples)} 筆可用")

    except Exception as e:
        print(f"⚠️  HuggingFace 下載跳過（{e}）")

    return samples


# ══════════════════════════════════════════════════════════════════
# 主程式
# ══════════════════════════════════════════════════════════════════
def main():
    print("=" * 60)
    print("  📦 訓練資料擴充工具")
    print("=" * 60)

    # 讀取現有資料，避免重複
    existing = set()
    if os.path.exists(DATASET_PATH):
        with open(DATASET_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        existing.add(json.loads(line)["input"])
                    except Exception:
                        pass
        print(f"📊 現有資料：{len(existing)} 筆")

    new_samples = []

    # 來源 1：本地擴充範本
    print("\n[1/2] 生成本地擴充資料...")
    local = _gen_extended(2000)
    dedup = [s for s in local if s["input"] not in existing]
    new_samples.extend(dedup)
    print(f"      新增 {len(dedup)} 筆（去重後）")

    # 來源 2：HuggingFace
    print("\n[2/2] 嘗試 HuggingFace 資料集...")
    hf = _try_hf_k8s_dataset()
    hf_dedup = [s for s in hf if s["input"] not in existing]
    new_samples.extend(hf_dedup)
    if hf_dedup:
        print(f"      新增 {len(hf_dedup)} 筆")

    if not new_samples:
        print("\n✅ 沒有新資料需要寫入")
        return

    # 寫入
    with open(DATASET_PATH, "a", encoding="utf-8") as f:
        for s in new_samples:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")

    total_after = len(existing) + len(new_samples)
    print(f"\n✅ 完成！新增 {len(new_samples)} 筆")
    print(f"   資料集總量：{total_after} 筆")
    print(f"   儲存路徑：{DATASET_PATH}")
    print(f"\n💡 接下來：python training/train_local.py 重新訓練")


if __name__ == "__main__":
    main()
