"""
eval_baseline.py
使用「未微調」的原始 LLaMA-3 跑同樣 100 筆測試
用來與 eval_model.py 的結果做對比
執行：python eval_baseline.py
產出：eval_baseline_report.json
"""
import re
import json
import os
import sys as _sys
_sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core.config import BASE_MODEL, EVAL_BASELINE_REPORT as REPORT_PATH  # 先於 transformers import

import torch
from datetime import datetime
from transformers import AutoTokenizer, AutoModelForCausalLM, BitsAndBytesConfig

SYSTEM_PROMPT = (
    "You are an AI that converts Kubernetes deployment requests into JSON.\n"
    "ONLY output a valid JSON object. No explanation, no markdown, no extra text.\n"
    "Required fields: pods (integer), image (string), app_name (string)\n"
    "Optional fields: port (integer), memory (string, e.g. 256Mi)\n"
    'Example: {"pods": 3, "image": "nginx:latest", "app_name": "web-frontend", "port": 80}'
)

TEST_CASES = [
    ("deploy 3 pods of nginx:latest",                           3,  "nginx",         None,  None),
    ("start 5 replicas of redis:7-alpine for cache-server",     5,  "redis",         None,  None),
    ("launch 2 postgres:15 pods named db-primary",              2,  "postgres",      None,  None),
    ("spin up 1 node:20-alpine container for api-gateway",      1,  "node",          None,  None),
    ("run 4 python:3.11-slim pods for data-processor",          4,  "python",        None,  None),
    ("create 6 golang:1.21-alpine pods for scheduler",          6,  "golang",        None,  None),
    ("deploy 2 mysql:8.0 pods named db-replica",                2,  "mysql",         None,  None),
    ("start 3 mongo:6 containers for search-engine",            3,  "mongo",         None,  None),
    ("launch 7 openjdk:17-slim pods for report-generator",      7,  "openjdk",       None,  None),
    ("spin up 5 traefik:v3.0 pods for proxy-server",            5,  "traefik",       None,  None),
    ("run 1 grafana/grafana:latest pod for metrics-server",     1,  "grafana",       None,  None),
    ("deploy 4 rabbitmq:3-management pods for message-broker",  4,  "rabbitmq",      None,  None),
    ("start 2 elasticsearch:8.11.0 pods for search-engine",     2,  "elasticsearch", None,  None),
    ("create 8 node:18-slim pods for email-worker",             8,  "node",          None,  None),
    ("launch 3 python:3.10-alpine pods for file-storage",       3,  "python",        None,  None),
    ("deploy 10 nginx:1.25-alpine pods for web-frontend",       10, "nginx",         None,  None),
    ("start 2 postgres:14-alpine pods for db-primary",          2,  "postgres",      None,  None),
    ("run 6 redis:latest pods for cache-server",                6,  "redis",         None,  None),
    ("spin up 4 golang:1.21-alpine pods for data-processor",    4,  "golang",        None,  None),
    ("create 1 mysql:8.0 pod for db-replica",                   1,  "mysql",         None,  None),
    ("部署 3 個 web-frontend 的 pod，使用 nginx:latest",        3,  "nginx",         None,  None),
    ("幫我起 5 個 auth-service，映像檔是 python:3.11-slim",     5,  "python",        None,  None),
    ("建立 2 個 redis:7-alpine 容器，命名為 cache-server",      2,  "redis",         None,  None),
    ("我需要 1 個 db-primary pod，image 用 postgres:15",        1,  "postgres",      None,  None),
    ("請部署 4 個 node:20-alpine 作為 api-gateway",             4,  "node",          None,  None),
    ("把 scheduler 部署起來，6 個 pod，golang:1.21-alpine",     6,  "golang",        None,  None),
    ("幫我建立 2 個 mysql:8.0 的 db-replica",                  2,  "mysql",         None,  None),
    ("起 3 個 mongo:6 容器，叫做 search-engine",               3,  "mongo",         None,  None),
    ("部署 7 個 openjdk:17-slim 的 report-generator",          7,  "openjdk",       None,  None),
    ("建立 5 個 traefik:v3.0 pod，命名為 proxy-server",        5,  "traefik",       None,  None),
    ("我要 1 個 grafana/grafana:latest 的 metrics-server",     1,  "grafana",       None,  None),
    ("幫我跑 4 個 rabbitmq:3-management，叫 message-broker",   4,  "rabbitmq",      None,  None),
    ("部署 2 個 elasticsearch:8.11.0 的 search-engine",        2,  "elasticsearch", None,  None),
    ("起 8 個 node:18-slim 容器，名稱 email-worker",           8,  "node",          None,  None),
    ("建立 3 個 python:3.10-alpine 的 file-storage",           3,  "python",        None,  None),
    ("部署 10 個 nginx:1.25-alpine 作為 web-frontend",         10, "nginx",         None,  None),
    ("幫我起 2 個 postgres:14-alpine 的 db-primary",           2,  "postgres",      None,  None),
    ("建立 6 個 redis:latest 的 cache-server pod",             6,  "redis",         None,  None),
    ("部署 4 個 golang:1.21-alpine 的 data-processor",        4,  "golang",        None,  None),
    ("我需要 1 個 mysql:8.0 pod，叫做 db-replica",             1,  "mysql",         None,  None),
    ("deploy 2 nginx:latest pods for web-frontend, port 80",       2,  "nginx",    80,    None),
    ("start 3 node:20-alpine for api-gateway, expose port 3000",   3,  "node",     3000,  None),
    ("run 1 postgres:15 pod for db-primary, container port 5432",  1,  "postgres", 5432,  None),
    ("launch 4 redis:7-alpine pods, port 6379",                    4,  "redis",    6379,  None),
    ("deploy 2 mysql:8.0 pods named db-replica, port 3306",        2,  "mysql",    3306,  None),
    ("start 5 node:18-slim pods for email-worker, port 8080",      5,  "node",     8080,  None),
    ("create 3 python:3.11-slim pods, expose port 5000",           3,  "python",   5000,  None),
    ("spin up 1 mongo:6 pod for search-engine, port 27017",        1,  "mongo",    27017, None),
    ("deploy 2 golang:1.21-alpine pods, container port 8443",      2,  "golang",   8443,  None),
    ("launch 6 nginx:1.25-alpine pods for proxy, port 443",        6,  "nginx",    443,   None),
    ("run 3 rabbitmq:3-management pods, port 5672",                3,  "rabbitmq", 5672,  None),
    ("start 2 elasticsearch:8.11.0 pods, port 9200",               2,  "elasticsearch", 9200, None),
    ("deploy 4 traefik:v3.0 pods, expose port 80",                 4,  "traefik",  80,    None),
    ("create 1 grafana/grafana:latest pod, port 3000",             1,  "grafana",  3000,  None),
    ("spin up 5 openjdk:17-slim pods for report-generator, port 8080", 5, "openjdk", 8080, None),
    ("部署 2 個 web-frontend，image nginx:latest，開放 port 80",       2,  "nginx",    80,    None),
    ("幫我跑 3 個 api-gateway，用 node:20-alpine，port 3000",          3,  "node",     3000,  None),
    ("建立 1 個 db-primary，image postgres:15，port 5432",             1,  "postgres", 5432,  None),
    ("起 4 個 redis:7-alpine 容器，開 port 6379",                     4,  "redis",    6379,  None),
    ("部署 2 個 mysql:8.0 的 db-replica，container port 3306",        2,  "mysql",    3306,  None),
    ("幫我起 5 個 node:18-slim 的 email-worker，port 8080",           5,  "node",     8080,  None),
    ("建立 3 個 python:3.11-slim pod，開放 port 5000",                3,  "python",   5000,  None),
    ("起 1 個 mongo:6 的 search-engine，port 27017",                  1,  "mongo",    27017, None),
    ("部署 2 個 golang:1.21-alpine pod，container port 8443",         2,  "golang",   8443,  None),
    ("幫我跑 6 個 nginx:1.25-alpine 的 proxy，port 443",              6,  "nginx",    443,   None),
    ("建立 3 個 rabbitmq:3-management pod，開 port 5672",             3,  "rabbitmq", 5672,  None),
    ("起 2 個 elasticsearch:8.11.0 容器，port 9200",                  2,  "elasticsearch", 9200, None),
    ("部署 4 個 traefik:v3.0 pod，開放 port 80",                      4,  "traefik",  80,    None),
    ("幫我建立 1 個 grafana/grafana:latest，port 3000",               1,  "grafana",  3000,  None),
    ("起 5 個 openjdk:17-slim 的 report-generator，port 8080",        5,  "openjdk",  8080,  None),
    ("deploy 2 nginx:latest for web-frontend, memory limit 256Mi",         2,  "nginx",    None, "256"),
    ("start 3 python:3.11-slim pods, memory 512Mi",                        3,  "python",   None, "512"),
    ("launch 1 elasticsearch:8.11.0 pod, memory=2Gi",                      1,  "elasticsearch", None, "2"),
    ("run 4 redis:7-alpine pods for cache-server, memory 128Mi",           4,  "redis",    None, "128"),
    ("deploy 2 postgres:15 pods, set memory to 1Gi",                       2,  "postgres", None, "1"),
    ("start 5 node:20-alpine pods, memory limit 512Mi",                    5,  "node",     None, "512"),
    ("create 3 golang:1.21-alpine pods, memory=256Mi",                     3,  "golang",   None, "256"),
    ("spin up 6 mysql:8.0 pods for db-replica, ram limit 1Gi",             6,  "mysql",    None, "1"),
    ("launch 2 mongo:6 pods, memory 512Mi",                                2,  "mongo",    None, "512"),
    ("deploy 4 python:3.10-alpine pods, memory limit 256Mi",               4,  "python",   None, "256"),
    ("start 1 rabbitmq:3-management pod, memory=2Gi",                      1,  "rabbitmq", None, "2"),
    ("run 3 traefik:v3.0 pods, memory 128Mi",                              3,  "traefik",  None, "128"),
    ("create 8 node:18-slim pods for email-worker, memory 256Mi",          8,  "node",     None, "256"),
    ("deploy 2 openjdk:17-slim pods, set memory to 512Mi",                 2,  "openjdk",  None, "512"),
    ("spin up 5 grafana/grafana:latest pods, memory limit 1Gi",            5,  "grafana",  None, "1"),
    ("部署 2 個 web-frontend，image nginx:latest，記憶體限制 256Mi",        2,  "nginx",    None, "256"),
    ("幫我跑 4 個 data-processor，python:3.11-slim，記憶體 512Mi",         4,  "python",   None, "512"),
    ("建立 1 個 elasticsearch:8.11.0 pod，memory 2Gi",                     1,  "elasticsearch", None, "2"),
    ("起 4 個 redis:7-alpine 容器，記憶體 128Mi",                          4,  "redis",    None, "128"),
    ("部署 2 個 postgres:15 pod，記憶體設定 1Gi",                          2,  "postgres", None, "1"),
    ("幫我起 5 個 node:20-alpine，memory limit 512Mi",                     5,  "node",     None, "512"),
    ("建立 3 個 golang:1.21-alpine pod，記憶體 256Mi",                     3,  "golang",   None, "256"),
    ("起 6 個 mysql:8.0 的 db-replica，ram 限制 1Gi",                     6,  "mysql",    None, "1"),
    ("部署 2 個 mongo:6 容器，記憶體 512Mi",                               2,  "mongo",    None, "512"),
    ("幫我跑 4 個 python:3.10-alpine pod，記憶體限制 256Mi",               4,  "python",   None, "256"),
    ("建立 1 個 rabbitmq:3-management pod，memory 2Gi",                    1,  "rabbitmq", None, "2"),
    ("起 3 個 traefik:v3.0 容器，記憶體 128Mi",                            3,  "traefik",  None, "128"),
    ("部署 8 個 node:18-slim 的 email-worker，記憶體 256Mi",               8,  "node",     None, "256"),
    ("幫我起 2 個 openjdk:17-slim pod，記憶體設定 512Mi",                  2,  "openjdk",  None, "512"),
    ("建立 5 個 grafana/grafana:latest pod，記憶體限制 1Gi",               5,  "grafana",  None, "1"),
]


def load_baseline_model():
    """只載入原始模型，完全不載入 LoRA 權重"""
    print("🧹 載入原始 LLaMA-3（未微調）...")
    bnb_config = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_compute_dtype=torch.float16,
    )
    tokenizer = AutoTokenizer.from_pretrained(BASE_MODEL)
    tokenizer.pad_token = tokenizer.eos_token

    model = AutoModelForCausalLM.from_pretrained(
        BASE_MODEL,
        quantization_config=bnb_config,
        device_map={"": 0}
    )
    model.eval()
    print("✅ 原始基礎模型載入完成（無 LoRA）")
    return model, tokenizer


def get_output(model, tokenizer, prompt_text: str) -> str:
    input_ids = tokenizer.apply_chat_template(
        [{"role": "system", "content": SYSTEM_PROMPT},
         {"role": "user", "content": prompt_text}],
        add_generation_prompt=True, return_tensors="pt",
    ).to(model.device)
    input_len = input_ids.shape[1]

    with torch.no_grad():
        outputs = model.generate(
            input_ids=input_ids,
            attention_mask=torch.ones_like(input_ids),
            max_new_tokens=120,
            do_sample=False,
            eos_token_id=tokenizer.eos_token_id,
            pad_token_id=tokenizer.pad_token_id or tokenizer.eos_token_id,
        )

    new_tokens = outputs[0][input_len:]
    return tokenizer.decode(new_tokens, skip_special_tokens=True).strip()


def check(raw, exp_pods, exp_image, exp_port, exp_mem):
    pods_m  = re.search(r'"pods"\s*:\s*(\d+)',       raw)
    image_m = re.search(r'"image"\s*:\s*"([^"]+)"',  raw)
    port_m  = re.search(r'"port"\s*:\s*(\d+)',       raw)
    mem_m   = re.search(r'"memory"\s*:\s*"([^"]+)"', raw)

    actual_pods  = int(pods_m.group(1))  if pods_m  else None
    actual_image = image_m.group(1)      if image_m else None
    actual_port  = int(port_m.group(1))  if port_m  else None
    actual_mem   = mem_m.group(1)        if mem_m   else None

    pods_ok  = actual_pods == exp_pods
    image_ok = exp_image.lower() in (actual_image or "").lower()
    port_ok  = (exp_port is None) or (actual_port == exp_port)
    mem_ok   = (exp_mem  is None) or (exp_mem in (actual_mem or ""))

    return {
        "pods_ok" : pods_ok,
        "image_ok": image_ok,
        "port_ok" : port_ok,
        "mem_ok"  : mem_ok,
        "all_ok"  : pods_ok and image_ok and port_ok and mem_ok,
        "actual"  : {"pods": actual_pods, "image": actual_image, "port": actual_port, "memory": actual_mem},
    }


def evaluate():
    model, tokenizer = load_baseline_model()
    total      = len(TEST_CASES)
    stats      = {"pods": 0, "image": 0, "port": 0, "memory": 0, "all": 0}
    port_total = sum(1 for t in TEST_CASES if t[3] is not None)
    mem_total  = sum(1 for t in TEST_CASES if t[4] is not None)
    details    = []

    print("\n" + "=" * 70)
    print("  📊 LLaMA-3 原始模型評估（未微調基準線）")
    print(f"  測試案例：{total} 筆  |  開始時間：{datetime.now().strftime('%H:%M:%S')}")
    print("=" * 70)

    for i, (inp, exp_pods, exp_image, exp_port, exp_mem) in enumerate(TEST_CASES):
        raw    = get_output(model, tokenizer, inp)
        result = check(raw, exp_pods, exp_image, exp_port, exp_mem)

        if result["pods_ok"]:  stats["pods"]  += 1
        if result["image_ok"]: stats["image"] += 1
        if result["port_ok"]  and exp_port is not None: stats["port"]   += 1
        if result["mem_ok"]   and exp_mem  is not None: stats["memory"] += 1
        if result["all_ok"]:   stats["all"]   += 1

        icon = "✅" if result["all_ok"] else ("🟡" if (result["pods_ok"] and result["image_ok"]) else "❌")
        print(f"[{i+1:03d}/{total}] {icon}  {inp[:52]}")
        if not result["all_ok"]:
            print(f"         期望: pods={exp_pods} image={exp_image} port={exp_port} mem={exp_mem}")
            print(f"         實際: {result['actual']}")

        details.append({"input": inp, **result})

        if (i + 1) % 20 == 0:
            cur_acc = stats["pods"] / (i + 1) * 100
            print(f"\n  ── 目前進度 [{i+1}/{total}]  pods 準確率：{cur_acc:.1f}% ──\n")

    # 最終統計
    print("\n" + "=" * 70)
    print(f"  評估完成！共 {total} 筆  |  結束時間：{datetime.now().strftime('%H:%M:%S')}")
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
    print(f"  全部命中     : {bar(stats['all'],   total)}")

    # 對比提示
    print(f"\n📊 與微調模型對比（填入你的微調結果）：")
    print(f"  {'指標':<12} {'原始模型':>10}   {'微調後 (LoRA)':>14}")
    print(f"  {'-'*40}")
    print(f"  {'pods':<12} {stats['pods']/total*100:>9.1f}%   {'100.0%':>14}")
    print(f"  {'image':<12} {stats['image']/total*100:>9.1f}%   {'100.0%':>14}")
    print(f"  {'port':<12} {stats['port']/port_total*100 if port_total else 0:>9.1f}%   {'100.0%':>14}")
    print(f"  {'memory':<12} {stats['memory']/mem_total*100 if mem_total else 0:>9.1f}%   {'100.0%':>14}")
    print(f"  {'整體命中':<12} {stats['all']/total*100:>9.1f}%   {'100.0%':>14}")

    report = {
        "timestamp" : datetime.now().isoformat(),
        "model_type": "baseline_no_lora",
        "total"     : total,
        "accuracy"  : {
            "pods"  : round(stats["pods"]  / total * 100, 1),
            "image" : round(stats["image"] / total * 100, 1),
            "port"  : round(stats["port"]  / port_total * 100, 1) if port_total else None,
            "memory": round(stats["memory"]/ mem_total  * 100, 1) if mem_total  else None,
            "all"   : round(stats["all"]   / total * 100, 1),
        },
        "details": details,
    }
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n📄 基準線報告已儲存：{REPORT_PATH}")


if __name__ == "__main__":
    evaluate()
