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
CPUS     = ["250m", "500m", "750m", "1", "2", "4"]

# node 容量預設清單（cpu 核心數, 記憶體），訓練時隨機挑選，避免模型只記住單一容量組合
NODE_CAPACITIES = [
    {"cpu": "2",  "memory": "4Gi"},
    {"cpu": "4",  "memory": "8Gi"},
    {"cpu": "8",  "memory": "16Gi"},
    {"cpu": "16", "memory": "32Gi"},
]

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

# 含 cpu 英文模板（10種，比照 memory 區塊風格）
CPU_EN = [
    "deploy {n} pods of {app} using {image}, cpu limit {cpu}",
    "start {app} with {n} replicas ({image}), set cpu to {cpu}",
    "run {image} as {app}, {n} pods, cpu={cpu}",
    "launch {app}: {n} pods, {image}, cpu limit {cpu}",
    "create {app} deployment, image {image}, {n} replicas, {cpu} cpu",
    "spin up {n} {image} pods for {app}, cpu {cpu}",
    "deploy {app} service, {n} pods, {image}, cpu limit {cpu}",
    "bring up {app} with {image}, {n} replicas, cpu {cpu}",
    "setup {n} {app} pods using {image}, cpu {cpu}",
    "start {n} replicas of {image} for {app}, cpu={cpu}",
]

# 含 cpu 中文模板（10種）
CPU_ZH = [
    "部署 {n} 個 {app}，image {image}，CPU 限制 {cpu}",
    "幫我跑 {app}，{n} 個 pod，用 {image}，CPU {cpu}",
    "建立 {app}，{image}，{n} 個副本，CPU 限制 {cpu}",
    "起 {n} 個 {image} 的 {app}，cpu limit {cpu}",
    "部署 {app}：image={image}，replicas={n}，cpu={cpu}",
    "幫我起 {n} 個 {app}，{image}，CPU {cpu}",
    "建立 {n} 個 {app} pod，使用 {image}，CPU 限制 {cpu}",
    "起 {app}，{n} 個 pod，{image}，cpu {cpu}",
    "部署 {image} 作為 {app}，{n} 個，CPU {cpu}",
    "我要 {n} 個 {app}，image {image}，CPU {cpu}",
]

# 含 node 容量說明的英文模板（10種）：讓模型學會根據給定容量換算 node_count
NODE_EN = [
    "deploy {n} pods of {app} using {image}, each pod needs {cpu} cpu and {mem} memory, assuming each node has {node_cpu} cpu and {node_mem} memory, how many nodes do I need?",
    "start {app} with {n} replicas ({image}), {cpu} cpu and {mem} memory per pod, node capacity is {node_cpu} cpu / {node_mem} memory",
    "run {image} as {app}, {n} pods, cpu={cpu}, memory={mem}, node capacity: {node_cpu} cpu, {node_mem} memory",
    "launch {app}: {n} pods, {image}, {cpu} cpu and {mem} memory each, our nodes have {node_cpu} cpu and {node_mem} memory",
    "create {app} deployment, image {image}, {n} replicas, {cpu} cpu, {mem} memory, each node offers {node_cpu} cpu / {node_mem} memory",
    "spin up {n} {image} pods for {app}, {cpu} cpu and {mem} memory per pod, node size {node_cpu} cpu / {node_mem} memory",
    "deploy {app} service, {n} pods, {image}, cpu {cpu}, memory {mem}, cluster nodes have {node_cpu} cpu and {node_mem} memory",
    "bring up {app} with {image}, {n} replicas, {cpu} cpu / {mem} memory each, node capacity {node_cpu} cpu / {node_mem} memory",
    "setup {n} {app} pods using {image}, cpu {cpu}, memory {mem}, given nodes with {node_cpu} cpu and {node_mem} memory",
    "start {n} replicas of {image} for {app}, cpu={cpu}, memory={mem}, node has {node_cpu} cpu and {node_mem} memory",
]

# 含 node 容量說明的中文模板（10種）
NODE_ZH = [
    "部署 {n} 個 {app}，image {image}，每個 pod 需要 {cpu} CPU 和 {mem} 記憶體，假設每個 node 有 {node_cpu} CPU 和 {node_mem} 記憶體，需要幾個 node？",
    "幫我跑 {app}，{n} 個 pod，用 {image}，每個 pod {cpu} CPU、{mem} 記憶體，node 容量是 {node_cpu} CPU / {node_mem} 記憶體",
    "建立 {app}，{image}，{n} 個副本，cpu={cpu}，memory={mem}，node 容量：{node_cpu} CPU、{node_mem} 記憶體",
    "起 {n} 個 {image} 的 {app}，每個 {cpu} CPU、{mem} 記憶體，我們的 node 有 {node_cpu} CPU 和 {node_mem} 記憶體",
    "部署 {app}：image={image}，replicas={n}，{cpu} CPU，{mem} 記憶體，每個 node 提供 {node_cpu} CPU / {node_mem} 記憶體",
    "幫我起 {n} 個 {app}，{image}，每個 pod {cpu} CPU 和 {mem} 記憶體，node 規格 {node_cpu} CPU / {node_mem} 記憶體",
    "建立 {n} 個 {app} pod，使用 {image}，CPU {cpu}，記憶體 {mem}，叢集 node 有 {node_cpu} CPU 和 {node_mem} 記憶體",
    "起 {app}，{n} 個 pod，{image}，每個 {cpu} CPU / {mem} 記憶體，node 容量 {node_cpu} CPU / {node_mem} 記憶體",
    "部署 {image} 作為 {app}，{n} 個，CPU {cpu}，記憶體 {mem}，已知 node 有 {node_cpu} CPU 和 {node_mem} 記憶體",
    "我要 {n} 個 {app}，image {image}，cpu={cpu}，memory={mem}，node 有 {node_cpu} CPU 和 {node_mem} 記憶體",
]


def pick_resources_for_target_node_count(target_node_count, replicas, node_capacity):
    """
    反推出一組 (cpu, memory) 讓 estimate_node_count(cpu, memory, replicas, node_capacity)
    大機率落在 target_node_count，避免隨機取值時 node_count 嚴重偏向 1（training label 失衡）。
    """
    from agents.cost_agent import _parse_cpu_millicores, _parse_memory_bytes, _mc_to_str, _bytes_to_mi

    node_cpu_mc = _parse_cpu_millicores(node_capacity["cpu"])
    node_mem_b  = _parse_memory_bytes(node_capacity["memory"])

    total_cpu_mc = node_cpu_mc * (target_node_count - 1) + random.randint(1, node_cpu_mc)
    total_mem_b  = node_mem_b  * (target_node_count - 1) + random.randint(1, node_mem_b)

    cpu_per_pod_mc = max(1, total_cpu_mc // replicas)
    mem_per_pod_b  = max(1024 * 1024, total_mem_b // replicas)  # 至少 1Mi

    return _mc_to_str(cpu_per_pod_mc), _bytes_to_mi(mem_per_pod_b)


def make(tmpl, n, app, image, port=None, mem=None, cpu=None, node_capacity=None):
    fmt = {"n": n, "app": app, "image": image}
    out = {"pods": n, "image": image, "app_name": app}
    if port is not None:
        fmt["port"] = port
        out["port"] = port
    if mem is not None:
        fmt["mem"] = mem
        out["memory"] = mem
    if cpu is not None:
        fmt["cpu"] = cpu
        out["cpu"] = cpu
    if node_capacity is not None:
        fmt["node_cpu"] = node_capacity["cpu"]
        fmt["node_mem"] = node_capacity["memory"]
        from agents.cost_agent import (
            estimate_node_count, _parse_cpu_millicores, _parse_memory_bytes, _mc_to_str, _bytes_to_mi,
        )
        result = estimate_node_count(cpu, mem, n, node_capacity=node_capacity)
        # Chain-of-Thought：把換算的中間步驟也寫進 output，讓模型模仿推理過程，
        # 而不是直接硬記「輸入組合 -> node_count」這個黑箱答案（後者訓練出來準確率很低）。
        total_cpu_mc  = _parse_cpu_millicores(cpu) * n
        total_mem_b   = _parse_memory_bytes(mem) * n
        out["total_cpu"]    = _mc_to_str(total_cpu_mc)
        out["total_memory"] = _bytes_to_mi(total_mem_b)
        out["cpu_bound_nodes"]    = result["cpu_bound"]
        out["memory_bound_nodes"] = result["memory_bound"]
        out["node_count"] = result["node_count"]
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

        for tmpl in CPU_EN:
            app   = random.choice(APP_NAMES)
            image = random.choice(IMAGES)
            cpu   = random.choice(CPUS)
            samples.append(make(tmpl, n, app, image, cpu=cpu))

        for tmpl in CPU_ZH:
            app   = random.choice(APP_NAMES)
            image = random.choice(IMAGES)
            cpu   = random.choice(CPUS)
            samples.append(make(tmpl, n, app, image, cpu=cpu))

        # node_count 是算術推理任務，額外重複取樣 NODE_REPEATS 次（每次重新隨機取值，不是逐字重複）
        # 加大訓練樣本量，讓模型有更多機會學到「除法 + 無條件進位 + 取最大值」這個模式
        NODE_REPEATS = 4
        for _ in range(NODE_REPEATS):
            for tmpl in NODE_EN:
                app   = random.choice(APP_NAMES)
                image = random.choice(IMAGES)
                node_capacity = random.choice(NODE_CAPACITIES)
                target_node_count = random.randint(1, 8)
                cpu, mem = pick_resources_for_target_node_count(target_node_count, n, node_capacity)
                samples.append(make(tmpl, n, app, image, mem=mem, cpu=cpu, node_capacity=node_capacity))

            for tmpl in NODE_ZH:
                app   = random.choice(APP_NAMES)
                image = random.choice(IMAGES)
                node_capacity = random.choice(NODE_CAPACITIES)
                target_node_count = random.randint(1, 8)
                cpu, mem = pick_resources_for_target_node_count(target_node_count, n, node_capacity)
                samples.append(make(tmpl, n, app, image, mem=mem, cpu=cpu, node_capacity=node_capacity))

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
    with_cpu    = sum(1 for s in samples if "cpu" in s["output"])
    with_node   = sum(1 for s in samples if "node_count" in s["output"])

    print(f"✅ 生成完成！共 {total} 筆訓練資料")
    print(f"📁 儲存路徑：{DATASET_PATH}")
    print(f"\n📊 資料統計：")
    print(f"   數字範圍    : 1 ~ 10（每個數字各 {total//10} 筆）")
    print(f"   不同 image  : {len(images_used)} 種")
    print(f"   不同 app    : {len(apps_used)} 種")
    print(f"   含 port     : {with_port} 筆")
    print(f"   含 memory   : {with_mem} 筆")
    print(f"   含 cpu      : {with_cpu} 筆")
    print(f"   含 node_count: {with_node} 筆")
    print(f"\n📋 前 3 筆預覽：")
    for s in samples[:3]:
        print(f"   input : {s['input']}")
        print(f"   output: {s['output']}")
        print()


if __name__ == "__main__":
    main()
