"""
eval_model.py
LLaMA-3 K8s LoRA 模型準確率評估
測試範圍：pods 1~10，中英文，含 port，含 memory
執行：python eval_model.py
產出：eval_report.json
"""
import re
import json
import os

import sys as _sys
_sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core.config import BASE_MODEL, ADAPTER_PATH, EVAL_REPORT as REPORT_PATH, SYSTEM_PROMPT  # 需先於 transformers import，才能讓 .env 的 HF_HOME 生效

import torch
from datetime import datetime
from transformers import AutoTokenizer, AutoModelForCausalLM, BitsAndBytesConfig
from peft import PeftModel

# ==============================
# 測試案例：pods 1~10 各類情境
# 格式：(輸入, 期望pods, 期望image關鍵字, 期望port或None, 期望memory或None)
# ==============================
TEST_CASES = [

    # ── 英文基本：每個數字 1~10 各一筆（10筆）
    ("deploy 1 pod of nginx:latest for web-frontend",              1,  "nginx",    None, None),
    ("start 2 replicas of redis:7-alpine for cache-server",        2,  "redis",    None, None),
    ("launch 3 postgres:15 pods named db-primary",                 3,  "postgres", None, None),
    ("spin up 4 node:20-alpine containers for api-gateway",        4,  "node",     None, None),
    ("run 5 python:3.11-slim pods for data-processor",             5,  "python",   None, None),
    ("create 6 golang:1.21-alpine pods for scheduler",             6,  "golang",   None, None),
    ("deploy 7 mysql:8.0 pods named db-replica",                   7,  "mysql",    None, None),
    ("start 8 mongo:6 containers for search-engine",               8,  "mongo",    None, None),
    ("launch 9 openjdk:17-slim pods for report-generator",         9,  "openjdk",  None, None),
    ("spin up 10 nginx:1.25-alpine pods for proxy-server",         10, "nginx",    None, None),

    # ── 中文基本：每個數字 1~10 各一筆（10筆）
    ("部署 1 個 web-frontend 的 pod，使用 nginx:latest",           1,  "nginx",    None, None),
    ("幫我起 2 個 cache-server，映像檔是 redis:7-alpine",          2,  "redis",    None, None),
    ("建立 db-primary 服務，3 個 replica，使用 postgres:15",       3,  "postgres", None, None),
    ("我需要 4 個 api-gateway pod，image 用 node:20-alpine",       4,  "node",     None, None),
    ("請部署 5 個 python:3.11-slim 作為 data-processor",          5,  "python",   None, None),
    ("把 scheduler 部署起來，6 個 pod，golang:1.21-alpine",        6,  "golang",   None, None),
    ("幫我建立 7 個 mysql:8.0 的 db-replica",                     7,  "mysql",    None, None),
    ("起 8 個 mongo:6 容器，叫做 search-engine",                  8,  "mongo",    None, None),
    ("部署 9 個 openjdk:17-slim 的 report-generator",             9,  "openjdk",  None, None),
    ("建立 10 個 nginx:1.25-alpine pod，命名為 proxy-server",      10, "nginx",    None, None),

    # ── 含 port 英文（10筆）
    ("deploy 1 nginx:latest pod for web-frontend, port 80",        1,  "nginx",    80,   None),
    ("start 2 node:20-alpine pods for api-gateway, port 3000",     2,  "node",     3000, None),
    ("run 3 postgres:15 pods for db-primary, container port 5432", 3,  "postgres", 5432, None),
    ("launch 4 redis:7-alpine pods, port 6379",                    4,  "redis",    6379, None),
    ("deploy 5 mysql:8.0 pods named db-replica, port 3306",        5,  "mysql",    3306, None),
    ("start 6 python:3.11-slim pods, expose port 5000",            6,  "python",   5000, None),
    ("spin up 7 mongo:6 pods for search-engine, port 27017",       7,  "mongo",    27017,None),
    ("create 8 golang:1.21-alpine pods, container port 8080",      8,  "golang",   8080, None),
    ("launch 9 nginx:1.25-alpine pods for proxy, port 443",        9,  "nginx",    443,  None),
    ("run 10 openjdk:17-slim pods, expose port 8443",              10, "openjdk",  8443, None),

    # ── 含 port 中文（10筆）
    ("部署 1 個 web-frontend，image nginx:latest，開放 port 80",   1,  "nginx",    80,   None),
    ("幫我跑 2 個 api-gateway，用 node:20-alpine，port 3000",      2,  "node",     3000, None),
    ("建立 3 個 db-primary，image postgres:15，port 5432",         3,  "postgres", 5432, None),
    ("起 4 個 redis:7-alpine 容器，開 port 6379",                  4,  "redis",    6379, None),
    ("部署 5 個 mysql:8.0 的 db-replica，container port 3306",     5,  "mysql",    3306, None),
    ("建立 6 個 python:3.11-slim pod，開放 port 5000",             6,  "python",   5000, None),
    ("起 7 個 mongo:6 的 search-engine，port 27017",               7,  "mongo",    27017,None),
    ("部署 8 個 golang:1.21-alpine pod，container port 8080",      8,  "golang",   8080, None),
    ("幫我跑 9 個 nginx:1.25-alpine 的 proxy，port 443",           9,  "nginx",    443,  None),
    ("建立 10 個 openjdk:17-slim pod，開放 port 8443",             10, "openjdk",  8443, None),

    # ── 含 memory 英文（10筆）
    ("deploy 1 nginx:latest pod for web-frontend, memory 256Mi",   1,  "nginx",    None, "256"),
    ("start 2 python:3.11-slim pods, memory 512Mi",                2,  "python",   None, "512"),
    ("launch 3 elasticsearch:8.11.0 pods, memory 2Gi",             3,  "elasticsearch", None, "2"),
    ("run 4 redis:7-alpine pods, memory 128Mi",                    4,  "redis",    None, "128"),
    ("deploy 5 postgres:15 pods, set memory to 1Gi",               5,  "postgres", None, "1"),
    ("start 6 node:20-alpine pods, memory limit 512Mi",            6,  "node",     None, "512"),
    ("create 7 golang:1.21-alpine pods, memory 256Mi",             7,  "golang",   None, "256"),
    ("spin up 8 mysql:8.0 pods, ram limit 1Gi",                    8,  "mysql",    None, "1"),
    ("launch 9 mongo:6 pods, memory 512Mi",                        9,  "mongo",    None, "512"),
    ("deploy 10 openjdk:17-slim pods, memory limit 256Mi",         10, "openjdk",  None, "256"),

    # ── 含 memory 中文（10筆）
    ("部署 1 個 web-frontend，image nginx:latest，記憶體限制 256Mi", 1, "nginx",   None, "256"),
    ("幫我跑 2 個 data-processor，python:3.11-slim，記憶體 512Mi",  2, "python",   None, "512"),
    ("建立 3 個 elasticsearch:8.11.0 pod，memory 2Gi",              3, "elasticsearch", None, "2"),
    ("起 4 個 redis:7-alpine 容器，記憶體 128Mi",                   4, "redis",    None, "128"),
    ("部署 5 個 postgres:15 pod，記憶體設定 1Gi",                   5, "postgres", None, "1"),
    ("幫我起 6 個 node:20-alpine，memory limit 512Mi",              6, "node",     None, "512"),
    ("建立 7 個 golang:1.21-alpine pod，記憶體 256Mi",              7, "golang",   None, "256"),
    ("起 8 個 mysql:8.0 的 db-replica，ram 限制 1Gi",              8, "mysql",    None, "1"),
    ("部署 9 個 mongo:6 容器，記憶體 512Mi",                        9, "mongo",    None, "512"),
    ("建立 10 個 openjdk:17-slim pod，記憶體限制 256Mi",            10, "openjdk",  None, "256"),

    # ── 含 CPU 英文（10筆）：格式 (輸入, pods, image, port, memory, cpu)
    ("deploy 1 nginx:latest pod for web-frontend, 500m cpu",       1,  "nginx",    None, None, "500m"),
    ("start 2 python:3.11-slim pods, 1 cpu core",                  2,  "python",   None, None, "1"),
    ("launch 3 elasticsearch:8.11.0 pods, 2 CPU cores",            3,  "elasticsearch", None, None, "2"),
    ("run 4 redis:7-alpine pods, cpu limit 250m",                  4,  "redis",    None, None, "250m"),
    ("deploy 5 postgres:15 pods, set cpu to 4 cores",              5,  "postgres", None, None, "4"),
    ("start 6 node:20-alpine pods, cpu limit 1 core",              6,  "node",     None, None, "1"),
    ("create 7 golang:1.21-alpine pods, 750m cpu",                 7,  "golang",   None, None, "750m"),
    ("spin up 8 mysql:8.0 pods, cpu 2 cores",                      8,  "mysql",    None, None, "2"),
    ("launch 9 mongo:6 pods, 500m cpu",                            9,  "mongo",    None, None, "500m"),
    ("deploy 10 openjdk:17-slim pods, cpu limit 3 cores",          10, "openjdk",  None, None, "3"),

    # ── 含 CPU 中文（10筆）
    ("部署 1 個 web-frontend，image nginx:latest，CPU 500m",        1, "nginx",    None, None, "500m"),
    ("幫我跑 2 個 data-processor，python:3.11-slim，1 顆 CPU",      2, "python",   None, None, "1"),
    ("建立 3 個 elasticsearch:8.11.0 pod，2 CPU 核心",              3, "elasticsearch", None, None, "2"),
    ("起 4 個 redis:7-alpine 容器，CPU 限制 250m",                  4, "redis",    None, None, "250m"),
    ("部署 5 個 postgres:15 pod，CPU 設定 4 核心",                  5, "postgres", None, None, "4"),
    ("幫我起 6 個 node:20-alpine，CPU limit 1 顆",                  6, "node",     None, None, "1"),
    ("建立 7 個 golang:1.21-alpine pod，750m CPU",                  7, "golang",   None, None, "750m"),
    ("起 8 個 mysql:8.0 的 db-replica，CPU 2 核心",                8, "mysql",    None, None, "2"),
    ("部署 9 個 mongo:6 容器，CPU 500m",                            9, "mongo",    None, None, "500m"),
    ("建立 10 個 openjdk:17-slim pod，CPU 限制 3 核心",             10, "openjdk",  None, None, "3"),

    # ── Node 數量：模型真正判斷的測試案例（10筆，格式多7個欄位 expected_node_count）
    # prompt 裡明講 node 容量，答案用 agents.cost_agent.estimate_node_count() 算出（跟訓練資料同一套公式）
    ("deploy 6 pods of web-frontend using nginx:latest, each pod needs 2 cpu and 4Gi memory, assuming each node has 2 cpu and 4Gi memory, how many nodes do I need?", 6, "nginx", None, None, "2", 6),
    ("start api-gateway with 10 replicas (node:20-alpine), 500m cpu and 512Mi memory per pod, node capacity is 4 cpu / 8Gi memory", 10, "node", None, None, "500m", 2),
    ("run elasticsearch:8.11.0 as search-engine, 9 pods, cpu=1, memory=2Gi, node capacity: 8 cpu, 16Gi memory", 9, "elasticsearch", None, None, "1", 2),
    ("launch db-primary: 5 pods, postgres:15, 4 cpu and 1Gi memory each, our nodes have 16 cpu and 32Gi memory", 5, "postgres", None, None, "4", 2),
    ("create cache-server deployment, image redis:7-alpine, 7 replicas, 750m cpu, 2Gi memory, each node offers 2 cpu / 4Gi memory", 7, "redis", None, None, "750m", 4),
    ("spin up 10 mysql:8.0 pods for db-replica, 250m cpu and 256Mi memory per pod, node size 4 cpu / 8Gi memory", 10, "mysql", None, None, "250m", 1),
    ("deploy message-broker service, 8 pods, rabbitmq:3-management, cpu 2, memory 4Gi, cluster nodes have 8 cpu and 16Gi memory", 8, "rabbitmq", None, None, "2", 2),
    ("bring up log-collector with golang:1.21-alpine, 6 replicas, 1 cpu / 1Gi memory each, node capacity 16 cpu / 32Gi memory", 6, "golang", None, None, "1", 1),
    ("setup 3 auth-service pods using python:3.11-slim, cpu 4, memory 2Gi, given nodes with 2 cpu and 4Gi memory", 3, "python", None, None, "4", 6),
    ("start 9 replicas of mongo:6 for metrics-server, cpu=500m, memory=1Gi, node has 4 cpu and 8Gi memory", 9, "mongo", None, None, "500m", 2),

    ("部署 6 個 web-frontend，image nginx:latest，每個 pod 需要 2 CPU 和 4Gi 記憶體，假設每個 node 有 2 CPU 和 4Gi 記憶體，需要幾個 node？", 6, "nginx", None, None, "2", 6),
    ("幫我跑 api-gateway，10 個 pod，用 node:20-alpine，每個 pod 500m CPU、512Mi 記憶體，node 容量是 4 CPU / 8Gi 記憶體", 10, "node", None, None, "500m", 2),
    ("建立 search-engine，elasticsearch:8.11.0，9 個副本，cpu=1，memory=2Gi，node 容量：8 CPU、16Gi 記憶體", 9, "elasticsearch", None, None, "1", 2),
    ("起 5 個 postgres:15 的 db-primary，每個 4 CPU、1Gi 記憶體，我們的 node 有 16 CPU 和 32Gi 記憶體", 5, "postgres", None, None, "4", 2),
    ("部署 cache-server：image=redis:7-alpine，replicas=7，750m CPU，2Gi 記憶體，每個 node 提供 2 CPU / 4Gi 記憶體", 7, "redis", None, None, "750m", 4),
    ("幫我起 10 個 db-replica，mysql:8.0，每個 pod 250m CPU 和 256Mi 記憶體，node 規格 4 CPU / 8Gi 記憶體", 10, "mysql", None, None, "250m", 1),
    ("建立 8 個 message-broker pod，使用 rabbitmq:3-management，CPU 2，記憶體 4Gi，叢集 node 有 8 CPU 和 16Gi 記憶體", 8, "rabbitmq", None, None, "2", 2),
    ("起 log-collector，6 個 pod，golang:1.21-alpine，每個 1 CPU / 1Gi 記憶體，node 容量 16 CPU / 32Gi 記憶體", 6, "golang", None, None, "1", 1),
    ("部署 python:3.11-slim 作為 auth-service，3 個，CPU 4，記憶體 2Gi，已知 node 有 2 CPU 和 4Gi 記憶體", 3, "python", None, None, "4", 6),
    ("我要 9 個 metrics-server，image mongo:6，cpu=500m，memory=1Gi，node 有 4 CPU 和 8Gi 記憶體", 9, "mongo", None, None, "500m", 2),
]

# ── Node 數量換算驗證案例：不經過模型，直接測 agents/cost_agent.estimate_node_count ──
# 格式：(cpu, memory, replicas, expected_node_count)，node 容量比照 core/config.NODE_CAPACITY 預設值 4 CPU / 8Gi
NODE_COUNT_CASES = [
    ("2",    "4Gi",   10, 5),   # 20 CPU / 40Gi -> ceil(20/4)=5, ceil(40/8)=5
    ("500m", "512Mi", 4,  1),   # 2 CPU / 2Gi   -> ceil(2/4)=1,  ceil(2/8)=1
    ("1",    "2Gi",   8,  2),   # 8 CPU / 16Gi  -> ceil(8/4)=2,  ceil(16/8)=2
    ("4",    "1Gi",   3,  3),   # 12 CPU / 3Gi  -> cpu bound: ceil(12/4)=3, mem: ceil(3/8)=1 -> max=3
    ("500m", "8Gi",   2,  2),   # 1 CPU / 16Gi  -> cpu:1, mem: ceil(16/8)=2 -> max=2
]


def load_model():
    print("🧹 載入模型中...")
    bnb_config = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_compute_dtype=torch.float16,
    )
    tokenizer = AutoTokenizer.from_pretrained(BASE_MODEL)
    tokenizer.pad_token = tokenizer.eos_token

    base = AutoModelForCausalLM.from_pretrained(
        BASE_MODEL, quantization_config=bnb_config, device_map={"": 0}
    )
    if os.path.exists(ADAPTER_PATH):
        print(f"✅ 合併 LoRA 權重：{ADAPTER_PATH}")
        model = PeftModel.from_pretrained(base, ADAPTER_PATH)
    else:
        print("⚠️  找不到 LoRA 權重，使用原始模型")
        model = base

    model.eval()
    return model, tokenizer


def get_output(model, tokenizer, prompt_text: str) -> str:
    full_prompt = (
        f"### System\n{SYSTEM_PROMPT}\n\n"
        f"### User\n{prompt_text}\n"
        "### Assistant\n{"
    )
    inputs    = tokenizer(full_prompt, return_tensors="pt").to("cuda")
    input_len = inputs["input_ids"].shape[1]

    with torch.no_grad():
        outputs = model.generate(
            **inputs,
            max_new_tokens=200,
            do_sample=False,
            eos_token_id=tokenizer.eos_token_id,
            pad_token_id=tokenizer.eos_token_id,
        )

    new_tokens = outputs[0][input_len:]
    raw = tokenizer.decode(new_tokens, skip_special_tokens=True).strip()
    if not raw.startswith("{"):
        raw = "{" + raw
    # 只取第一行，去掉垃圾字串
    return raw.split("\n")[0].strip()


def check(raw, exp_pods, exp_image, exp_port, exp_mem, exp_cpu=None, exp_node_count=None):
    pods_m  = re.search(r'"pods"\s*:\s*(\d+)',       raw)
    image_m = re.search(r'"image"\s*:\s*"([^"]+)"',  raw)
    port_m  = re.search(r'"port"\s*:\s*(\d+)',       raw)
    mem_m   = re.search(r'"memory"\s*:\s*"([^"]+)"', raw)
    cpu_m   = re.search(r'"cpu"\s*:\s*"?(\d+(?:\.\d+)?m?)"?', raw)
    node_m  = re.search(r'"node_count"\s*:\s*(\d+)', raw)

    actual_pods  = int(pods_m.group(1))  if pods_m  else None
    actual_image = image_m.group(1)      if image_m else None
    actual_port  = int(port_m.group(1))  if port_m  else None
    actual_mem   = mem_m.group(1)        if mem_m   else None
    actual_cpu   = cpu_m.group(1)        if cpu_m   else None
    actual_node_count = int(node_m.group(1)) if node_m else None

    pods_ok  = actual_pods == exp_pods
    image_ok = exp_image.lower() in (actual_image or "").lower()
    port_ok  = (exp_port is None) or (actual_port == exp_port)
    mem_ok   = (exp_mem  is None) or (exp_mem in (actual_mem or ""))
    cpu_ok   = (exp_cpu  is None) or (exp_cpu in (actual_cpu or ""))
    node_count_ok = (exp_node_count is None) or (actual_node_count == exp_node_count)
    schema_valid = pods_m is not None and image_m is not None

    return {
        "pods_ok" : pods_ok,
        "image_ok": image_ok,
        "port_ok" : port_ok,
        "mem_ok"  : mem_ok,
        "cpu_ok"  : cpu_ok,
        "node_count_ok": node_count_ok,
        "schema_valid": schema_valid,
        "all_ok"  : pods_ok and image_ok and port_ok and mem_ok and cpu_ok and node_count_ok,
        "actual"  : {
            "pods": actual_pods, "image": actual_image, "node_count": actual_node_count,
            "port": actual_port, "memory": actual_mem, "cpu": actual_cpu,
        },
    }


def evaluate():
    model, tokenizer = load_model()
    total      = len(TEST_CASES)
    stats      = {"pods": 0, "image": 0, "port": 0, "memory": 0, "cpu": 0, "node_count": 0, "all": 0, "schema_valid": 0}
    port_total = sum(1 for t in TEST_CASES if t[3] is not None)
    mem_total  = sum(1 for t in TEST_CASES if t[4] is not None)
    cpu_total  = sum(1 for t in TEST_CASES if len(t) > 5 and t[5] is not None)
    node_total = sum(1 for t in TEST_CASES if len(t) > 6 and t[6] is not None)
    pods_abs_err = []
    details    = []

    # 各數字的準確率統計
    per_num = {n: {"total": 0, "correct": 0} for n in range(1, 11)}

    print("\n" + "=" * 70)
    print("  📊 LLaMA-3 K8s LoRA 模型評估報告")
    print(f"  測試案例：{total} 筆  |  開始：{datetime.now().strftime('%H:%M:%S')}")
    print("=" * 70)

    for i, case in enumerate(TEST_CASES):
        inp, exp_pods, exp_image, exp_port, exp_mem = case[:5]
        exp_cpu = case[5] if len(case) > 5 else None
        exp_node_count = case[6] if len(case) > 6 else None

        raw    = get_output(model, tokenizer, inp)
        result = check(raw, exp_pods, exp_image, exp_port, exp_mem, exp_cpu, exp_node_count)

        if result["pods_ok"]:  stats["pods"]  += 1
        if result["image_ok"]: stats["image"] += 1
        if result["port_ok"]  and exp_port is not None: stats["port"]   += 1
        if result["mem_ok"]   and exp_mem  is not None: stats["memory"] += 1
        if result["cpu_ok"]   and exp_cpu  is not None: stats["cpu"]    += 1
        if result["node_count_ok"] and exp_node_count is not None: stats["node_count"] += 1
        if result["schema_valid"]: stats["schema_valid"] += 1
        if result["all_ok"]:   stats["all"]   += 1

        actual_pods = result["actual"]["pods"]
        if actual_pods is not None:
            pods_abs_err.append(abs(actual_pods - exp_pods))

        # 統計每個數字的準確率
        if exp_pods in per_num:
            per_num[exp_pods]["total"] += 1
            if result["pods_ok"]:
                per_num[exp_pods]["correct"] += 1

        icon = "✅" if result["all_ok"] else ("🟡" if (result["pods_ok"] and result["image_ok"]) else "❌")
        print(f"[{i+1:03d}/{total}] {icon}  {inp[:55]}")
        if not result["all_ok"]:
            print(f"         期望: pods={exp_pods} img={exp_image} port={exp_port} mem={exp_mem} cpu={exp_cpu} node_count={exp_node_count}")
            print(f"         實際: {result['actual']}")

        details.append({"input": inp, **result})

        if (i + 1) % 10 == 0:
            print(f"\n  ── [{i+1}/{total}] 目前 pods 準確率：{stats['pods']/(i+1)*100:.1f}% ──\n")

    # 最終統計
    print("\n" + "=" * 70)
    print(f"  ✅ 評估完成  |  結束：{datetime.now().strftime('%H:%M:%S')}")
    print("=" * 70)

    def bar(n, d):
        pct = n / d * 100 if d else 0
        b   = "█" * int(pct // 5) + "░" * (20 - int(pct // 5))
        return f"[{b}] {pct:.1f}%  ({n}/{d})"

    print(f"\n  pods  準確率 : {bar(stats['pods'],  total)}")
    print(f"  image 準確率 : {bar(stats['image'], total)}")
    if port_total:
        print(f"  port  準確率 : {bar(stats['port'],  port_total)}")
    if mem_total:
        print(f"  memory準確率 : {bar(stats['memory'],mem_total)}")
    if cpu_total:
        print(f"  cpu   準確率 : {bar(stats['cpu'],  cpu_total)}")
    if node_total:
        print(f"  node_count準確率 : {bar(stats['node_count'], node_total)}")
    print(f"  schema有效率 : {bar(stats['schema_valid'], total)}")
    print(f"  全部命中     : {bar(stats['all'],   total)}")

    pods_mae = sum(pods_abs_err) / len(pods_abs_err) if pods_abs_err else None
    if pods_mae is not None:
        print(f"  pods  MAE    : {pods_mae:.2f}")

    # 各數字準確率
    print(f"\n  各數字 pods 準確率：")
    for n in range(1, 11):
        s   = per_num[n]
        pct = s["correct"] / s["total"] * 100 if s["total"] else 0
        bar_s = "█" * int(pct // 10) + "░" * (10 - int(pct // 10))
        print(f"    n={n:2d}  [{bar_s}] {pct:.0f}%  ({s['correct']}/{s['total']})")

    print(f"\n💡 報告可以說：")
    print(f"   「本模型在 {total} 筆測試中（涵蓋 1~10 個 Pod、中英文、含 port、含 memory）")
    print(f"    pods 預測準確率 {stats['pods']/total*100:.1f}%，")
    print(f"    image 識別準確率 {stats['image']/total*100:.1f}%，")
    print(f"    整體完全命中率 {stats['all']/total*100:.1f}%」")

    report = {
        "timestamp": datetime.now().isoformat(),
        "total"    : total,
        "accuracy" : {
            "pods"  : round(stats["pods"]  / total * 100, 1),
            "image" : round(stats["image"] / total * 100, 1),
            "port"  : round(stats["port"]  / port_total * 100, 1) if port_total else None,
            "memory": round(stats["memory"]/ mem_total  * 100, 1) if mem_total  else None,
            "cpu"   : round(stats["cpu"]   / cpu_total  * 100, 1) if cpu_total  else None,
            "node_count": round(stats["node_count"] / node_total * 100, 1) if node_total else None,
            "schema_valid": round(stats["schema_valid"] / total * 100, 1),
            "all"   : round(stats["all"]   / total * 100, 1),
        },
        "pods_mae": round(pods_mae, 3) if pods_mae is not None else None,
        "per_number_accuracy": {
            str(n): round(s["correct"] / s["total"] * 100, 1) if s["total"] else 0
            for n, s in per_num.items()
        },
        "details": details,
    }
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n📄 完整報告已儲存：{REPORT_PATH}")


def test_node_count():
    """獨立驗證 estimate_node_count 的換算邏輯是否正確（不涉及 LLM，純規則計算）。"""
    import sys as _sys
    _sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from agents.cost_agent import estimate_node_count

    print("\n" + "=" * 70)
    print("  🧮 Node 數量換算驗證（規則計算，非模型準確度）")
    print("=" * 70)
    passed = 0
    for cpu, memory, replicas, expected in NODE_COUNT_CASES:
        result = estimate_node_count(cpu, memory, replicas)
        ok = result["node_count"] == expected
        passed += ok
        icon = "✅" if ok else "❌"
        print(f"  {icon} cpu={cpu} memory={memory} replicas={replicas} "
              f"-> got={result['node_count']} expected={expected}")
    print(f"\n  Node 換算通過率：{passed}/{len(NODE_COUNT_CASES)}")


if __name__ == "__main__":
    evaluate()
    test_node_count()
