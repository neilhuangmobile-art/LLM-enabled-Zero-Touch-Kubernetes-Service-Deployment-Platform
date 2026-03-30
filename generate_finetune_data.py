# generate_finetune_data.py
# 生成高品質訓練資料，數字範圍 1~10，確保準確
import json
import random
import os

random.seed(42)

IMAGES = [
    "nginx:latest", "nginx:1.25-alpine",
    "redis:7-alpine", "redis:latest",
    "postgres:15", "postgres:14-alpine",
    "mysql:8.0", "mongo:6",
    "node:20-alpine", "node:18-slim",
    "python:3.11-slim", "python:3.10-alpine",
    "golang:1.21-alpine", "openjdk:17-slim",
    "rabbitmq:3-management", "grafana/grafana:latest",
    "prom/prometheus:latest", "elasticsearch:8.11.0",
    "apache/kafka:latest", "traefik:v3.0",
]

APP_NAMES = [
    "web-frontend", "api-gateway", "user-service",
    "auth-service", "payment-service", "order-service",
    "notification-service", "data-processor", "cache-server",
    "db-primary", "message-broker", "log-collector",
    "metrics-server", "search-engine", "file-storage",
    "image-resizer", "email-worker", "report-generator",
    "scheduler", "proxy-server",
]

PORTS    = [80, 443, 3000, 3001, 5000, 5432, 6379, 8080, 8443, 9090, 27017]
MEMORIES = ["128Mi", "256Mi", "512Mi", "1Gi", "2Gi"]

# 英文基本模板（20種）
EN_TEMPLATES = [
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
]

# 中文基本模板（20種）
ZH_TEMPLATES = [
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
    "建立 {n} 個 {app} 容器，使用 {image}",
    "我想要 {n} 個 {image} 的 {app}",
    "幫我部署 {app}，{n} 個 pod，{image}",
    "起 {n} 個 {app}，image 選 {image}",
    "部署 {n} 個 {image} 叫做 {app}",
]

# 含 port 英文模板（10種）
PORT_EN = [
    "deploy {n} pods of {app} using {image}, expose port {port}",
    "start {app} with {n} replicas, image {image}, open port {port}",
    "run {image} as {app}, {n} pods, port {port}",
    "launch {n} {app} pods ({image}), container port {port}",
    "create {app}: image={image}, replicas={n}, port={port}",
    "spin up {n} {image} pods for {app}, expose port {port}",
    "deploy {app} service, {n} pods, {image}, port {port}",
    "bring up {app} with {image}, {n} replicas, open port {port}",
    "setup {n} {app} pods using {image}, container port {port}",
    "start {n} replicas of {image} for {app} on port {port}",
]

# 含 port 中文模板（10種）
PORT_ZH = [
    "部署 {n} 個 {app}，image {image}，開放 {port} port",
    "幫我跑 {app}，{n} 個 pod，用 {image}，port {port}",
    "建立 {app} deployment，{image}，{n} 個副本，開 port {port}",
    "起 {n} 個 {image} 的 {app}，container port {port}",
    "部署 {app}：image={image}，replicas={n}，port={port}",
    "幫我起 {n} 個 {app}，{image}，開放 port {port}",
    "建立 {n} 個 {app} pod，使用 {image}，port {port}",
    "起 {app}，{n} 個 pod，{image}，對外開 {port}",
    "部署 {image} 作為 {app}，{n} 個，port {port}",
    "我要 {n} 個 {app}，image {image}，port {port}",
]

# 含 memory 英文模板（10種）
MEM_EN = [
    "deploy {n} pods of {app} using {image}, memory limit {mem}",
    "start {app} with {n} replicas ({image}), set memory to {mem}",
    "run {image} as {app}, {n} pods, memory={mem}",
    "launch {app}: {n} pods, {image}, ram limit {mem}",
    "create {app} deployment, image {image}, {n} replicas, {mem} memory",
    "spin up {n} {image} pods for {app}, memory {mem}",
    "deploy {app} service, {n} pods, {image}, memory limit {mem}",
    "bring up {app} with {image}, {n} replicas, memory {mem}",
    "setup {n} {app} pods using {image}, ram {mem}",
    "start {n} replicas of {image} for {app}, memory={mem}",
]

# 含 memory 中文模板（10種）
MEM_ZH = [
    "部署 {n} 個 {app}，image {image}，記憶體限制 {mem}",
    "幫我跑 {app}，{n} 個 pod，用 {image}，記憶體 {mem}",
    "建立 {app}，{image}，{n} 個副本，ram 限制 {mem}",
    "起 {n} 個 {image} 的 {app}，memory limit {mem}",
    "部署 {app}：image={image}，replicas={n}，memory={mem}",
    "幫我起 {n} 個 {app}，{image}，記憶體 {mem}",
    "建立 {n} 個 {app} pod，使用 {image}，記憶體限制 {mem}",
    "起 {app}，{n} 個 pod，{image}，memory {mem}",
    "部署 {image} 作為 {app}，{n} 個，記憶體 {mem}",
    "我要 {n} 個 {app}，image {image}，記憶體 {mem}",
]


def make(tmpl, n, app, image, port=None, mem=None):
    fmt = {"n": n, "app": app, "image": image}
    out = {"pods": n, "image": image, "app_name": app}
    if port is not None:
        fmt["port"] = port
        out["port"] = port
    if mem is not None:
        fmt["mem"] = mem
        out["memory"] = mem
    return {"input": tmpl.format(**fmt), "output": out}


def generate():
    samples = []

    # 每個數字 1~10，每種模板都跑過一次，確保每個數字都有足夠訓練樣本
    for n in range(1, 11):
        for tmpl in EN_TEMPLATES:
            app   = random.choice(APP_NAMES)
            image = random.choice(IMAGES)
            samples.append(make(tmpl, n, app, image))

        for tmpl in ZH_TEMPLATES:
            app   = random.choice(APP_NAMES)
            image = random.choice(IMAGES)
            samples.append(make(tmpl, n, app, image))

        for tmpl in PORT_EN:
            app   = random.choice(APP_NAMES)
            image = random.choice(IMAGES)
            port  = random.choice(PORTS)
            samples.append(make(tmpl, n, app, image, port=port))

        for tmpl in PORT_ZH:
            app   = random.choice(APP_NAMES)
            image = random.choice(IMAGES)
            port  = random.choice(PORTS)
            samples.append(make(tmpl, n, app, image, port=port))

        for tmpl in MEM_EN:
            app   = random.choice(APP_NAMES)
            image = random.choice(IMAGES)
            mem   = random.choice(MEMORIES)
            samples.append(make(tmpl, n, app, image, mem=mem))

        for tmpl in MEM_ZH:
            app   = random.choice(APP_NAMES)
            image = random.choice(IMAGES)
            mem   = random.choice(MEMORIES)
            samples.append(make(tmpl, n, app, image, mem=mem))

    random.shuffle(samples)
    return samples


def main():
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from core.config import DATASET_PATH
    os.makedirs(os.path.dirname(DATASET_PATH), exist_ok=True)

    samples = generate()

    with open(DATASET_PATH, "w", encoding="utf-8") as f:
        for s in samples:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")

    total       = len(samples)
    images_used = set(s["output"]["image"]    for s in samples)
    apps_used   = set(s["output"]["app_name"] for s in samples)
    with_port   = sum(1 for s in samples if "port"   in s["output"])
    with_mem    = sum(1 for s in samples if "memory" in s["output"])

    print(f"✅ 生成完成！共 {total} 筆訓練資料")
    print(f"📁 儲存路徑：{DATASET_PATH}")
    print(f"\n📊 資料統計：")
    print(f"   數字範圍    : 1 ~ 10（每個數字各 {total//10} 筆）")
    print(f"   不同 image  : {len(images_used)} 種")
    print(f"   不同 app    : {len(apps_used)} 種")
    print(f"   含 port     : {with_port} 筆")
    print(f"   含 memory   : {with_mem} 筆")
    print(f"\n📋 前 3 筆預覽：")
    for s in samples[:3]:
        print(f"   input : {s['input']}")
        print(f"   output: {s['output']}")
        print()


if __name__ == "__main__":
    main()
