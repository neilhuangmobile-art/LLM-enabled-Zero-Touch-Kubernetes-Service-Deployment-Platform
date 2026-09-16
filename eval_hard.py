"""
eval_hard.py
困難版評估：測試口語化、模糊、混合語言等真實使用情境
同時跑「原始模型」和「微調模型」，直接產出對比報告
執行：python eval_hard.py
"""
import re
import json
import os
import sys as _sys
_sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core.config import BASE_MODEL, DEPLOY_ADAPTER_PATH, EVAL_HARD_REPORT as REPORT_PATH  # 先於 transformers import

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

# ==============================
# 困難測試案例（60筆）
# 格式：(輸入, 期望pods, 期望image關鍵字, 期望port或None, 期望memory或None)
# ==============================
TEST_CASES = [

    # ── 類型1：口語化模糊中文（20筆）
    # 沒有明確說 image，需要從語意推斷
    ("我要跑兩個 nginx，幫我部署一下",                          2,  "nginx",    None,  None),
    ("幫我把 redis 起三個，快取用的",                           3,  "redis",    None,  None),
    ("資料庫開一個就好，用 postgres",                           1,  "postgres", None,  None),
    ("node 的 api 服務，我需要四個",                            4,  "node",     None,  None),
    ("python 服務給我五個 pod",                                 5,  "python",   None,  None),
    ("起六個 golang 的服務",                                    6,  "golang",   None,  None),
    ("mysql 資料庫，兩個副本",                                  2,  "mysql",    None,  None),
    ("mongo 起三個，document 資料庫用",                         3,  "mongo",    None,  None),
    ("elasticsearch 我要兩個",                                  2,  "elasticsearch", None, None),
    ("給我跑一個 grafana 監控",                                 1,  "grafana",  None,  None),
    ("rabbitmq 訊息佇列，開四個",                               4,  "rabbitmq", None,  None),
    ("nginx 反向代理，需要三個",                                3,  "nginx",    None,  None),
    ("幫我弄個 redis 快取，兩個就好",                           2,  "redis",    None,  None),
    ("java 服務用 openjdk，起五個",                             5,  "openjdk",  None,  None),
    ("traefik 的 ingress，一個就夠",                            1,  "traefik",  None,  None),
    ("跑個 mysql 出來，要三個",                                 3,  "mysql",    None,  None),
    ("postgres 資料庫要高可用，起三個",                         3,  "postgres", None,  None),
    ("node 後端服務，我要六個 pod",                             6,  "node",     None,  None),
    ("python 爬蟲服務，四個 pod",                               4,  "python",   None,  None),
    ("golang 微服務，起兩個",                                   2,  "golang",   None,  None),

    # ── 類型2：縮寫 + 混合語言（20筆）
    ("deploy 那個 pg 3 個",                                    3,  "postgres", None,  None),
    ("起個 redis x2",                                          2,  "redis",    None,  None),
    ("nginx x5 拜託",                                          5,  "nginx",    None,  None),
    ("跑 mongo，要 4 個 pod",                                  4,  "mongo",    None,  None),
    ("mysql x3 please",                                        3,  "mysql",    None,  None),
    ("py api 給我 3 個 pod，用 python:3.11-slim",              3,  "python",   None,  None),
    ("node backend x4，port 3000",                             4,  "node",     3000,  None),
    ("pg db 一個，port 5432",                                  1,  "postgres", 5432,  None),
    ("redis cache x3，port 6379",                              3,  "redis",    6379,  None),
    ("nginx lb x2，開 80",                                     2,  "nginx",    80,    None),
    ("ES 兩個，port 9200",                                     2,  "elasticsearch", 9200, None),
    ("rabbitmq x4，port 5672",                                 4,  "rabbitmq", 5672,  None),
    ("mongo x2，port 27017",                                   2,  "mongo",    27017, None),
    ("grafana 一個，port 3000",                                1,  "grafana",  3000,  None),
    ("golang svc x3，port 8080",                               3,  "golang",   8080,  None),
    ("py worker x5，記憶體 512Mi",                             5,  "python",   None,  "512"),
    ("redis x2，ram 256Mi",                                    2,  "redis",    None,  "256"),
    ("pg x1，memory 1Gi",                                      1,  "postgres", None,  "1"),
    ("node api x4，mem 512Mi",                                 4,  "node",     None,  "512"),
    ("nginx x3，記憶體 128Mi",                                 3,  "nginx",    None,  "128"),

    # ── 類型3：需要語意推理（20筆）
    ("我的快取服務要擴到五個，用 redis",                        5,  "redis",    None,  None),
    ("網頁伺服器需要三個做負載均衡，nginx",                     3,  "nginx",    None,  None),
    ("資料庫要高可用，postgres 起三個副本",                     3,  "postgres", None,  None),
    ("API 服務流量變大，node 給我六個",                         6,  "node",     None,  None),
    ("機器學習模型推論服務，python 四個 pod",                   4,  "python",   None,  None),
    ("訊息佇列要擴容，rabbitmq 起五個",                         5,  "rabbitmq", None,  None),
    ("搜尋功能需要 elasticsearch，兩個節點",                    2,  "elasticsearch", None, None),
    ("監控系統用 grafana，一個就夠",                            1,  "grafana",  None,  None),
    ("微服務架構需要 traefik 做路由，起兩個",                   2,  "traefik",  None,  None),
    ("文件資料庫 mongo 需要三個副本",                           3,  "mongo",    None,  None),
    ("後端 API 用 golang，需要四個 pod 處理請求",               4,  "golang",   None,  None),
    ("Java 應用程式需要 openjdk，起三個",                       3,  "openjdk",  None,  None),
    ("MySQL 主從架構，起兩個",                                  2,  "mysql",    None,  None),
    ("Redis 要給 session 用，記憶體給 256Mi，兩個",             2,  "redis",    None,  "256"),
    ("Postgres 生產環境，需要 1Gi 記憶體，一個",                1,  "postgres", None,  "1"),
    ("Node.js API 要對外，port 3000，三個 pod",                 3,  "node",     3000,  None),
    ("nginx 當作前端入口，port 80，兩個",                       2,  "nginx",    80,    None),
    ("Python Flask 服務，port 5000，四個 pod",                  4,  "python",   5000,  None),
    ("Golang gRPC 服務，port 8080，三個 pod，記憶體 256Mi",     3,  "golang",   8080,  "256"),
    ("MySQL 資料庫對外，port 3306，兩個，記憶體 512Mi",         2,  "mysql",    3306,  "512"),
]


def load_model(use_lora: bool):
    label = "微調模型 (LoRA)" if use_lora else "原始模型（未微調）"
    print(f"\n🧹 載入 {label}...")

    bnb_config = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_compute_dtype=torch.float16,
    )
    tokenizer = AutoTokenizer.from_pretrained(BASE_MODEL)
    tokenizer.pad_token = tokenizer.eos_token

    model = AutoModelForCausalLM.from_pretrained(
        BASE_MODEL, quantization_config=bnb_config, device_map={"": 0}
    )

    if use_lora and DEPLOY_ADAPTER_PATH and os.path.isdir(DEPLOY_ADAPTER_PATH):
        from peft import PeftModel
        model = PeftModel.from_pretrained(model, DEPLOY_ADAPTER_PATH)
        print(f"✅ 掛載部署 LoRA：{DEPLOY_ADAPTER_PATH}")
    elif use_lora:
        print("ℹ️  未設定 DEPLOY_ADAPTER_PATH，此輪等同 base 模型")
    else:
        print("✅ base 模型載入完成（無 LoRA）")

    model.eval()
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
        "actual"  : {
            "pods": actual_pods, "image": actual_image,
            "port": actual_port, "memory": actual_mem
        },
    }


def run_eval(model, tokenizer, label: str):
    total      = len(TEST_CASES)
    stats      = {"pods": 0, "image": 0, "port": 0, "memory": 0, "all": 0}
    port_total = sum(1 for t in TEST_CASES if t[3] is not None)
    mem_total  = sum(1 for t in TEST_CASES if t[4] is not None)
    details    = []

    print("\n" + "=" * 70)
    print(f"  📊 {label}")
    print(f"  測試案例：{total} 筆  |  開始：{datetime.now().strftime('%H:%M:%S')}")
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
            print(f"         期望: pods={exp_pods} img={exp_image} port={exp_port} mem={exp_mem}")
            print(f"         實際: {result['actual']}")

        details.append({"input": inp, **result})

        if (i + 1) % 20 == 0:
            print(f"\n  ── [{i+1}/{total}] pods 準確率：{stats['pods']/(i+1)*100:.1f}% ──\n")

    def pct(n, d): return round(n / d * 100, 1) if d else 0

    accuracy = {
        "pods"  : pct(stats["pods"],   total),
        "image" : pct(stats["image"],  total),
        "port"  : pct(stats["port"],   port_total),
        "memory": pct(stats["memory"], mem_total),
        "all"   : pct(stats["all"],    total),
    }

    print(f"\n  結束：{datetime.now().strftime('%H:%M:%S')}")
    return accuracy, details


def print_comparison(baseline_acc, lora_acc):
    print("\n" + "=" * 70)
    print("  📊 微調前後對比報告")
    print("=" * 70)
    print(f"  {'指標':<12} {'原始模型':>12}   {'微調後 LoRA':>12}   {'提升':>8}")
    print(f"  {'-' * 52}")

    for key, label in [("pods","pods"),("image","image"),("port","port"),("memory","memory"),("all","整體命中")]:
        b = baseline_acc[key]
        l = lora_acc[key]
        delta = l - b
        arrow = "↑" if delta > 0 else ("→" if delta == 0 else "↓")
        print(f"  {label:<12} {b:>11.1f}%   {l:>11.1f}%   {arrow}{abs(delta):>6.1f}%")

    print("=" * 70)
    print(f"\n💡 報告可以說：")
    print(f"   「原始 LLaMA-3 在困難情境下整體命中率 {baseline_acc['all']}%，")
    print(f"    經過 LoRA 微調後提升至 {lora_acc['all']}%，")
    print(f"    pods 準確率從 {baseline_acc['pods']}% 提升至 {lora_acc['pods']}%」")


def main():
    print("=" * 70)
    print("  🔬 困難情境評估：口語化 / 縮寫 / 語意推理")
    print("  同時評估原始模型與微調模型，產出對比報告")
    print("=" * 70)

    # Step 1：跑原始模型
    baseline_model, tokenizer = load_model(use_lora=False)
    baseline_acc, baseline_details = run_eval(baseline_model, tokenizer, "原始模型（未微調）基準線")

    # 釋放記憶體再載入微調模型
    del baseline_model
    torch.cuda.empty_cache()
    print("\n🧹 釋放原始模型記憶體...")

    # Step 2：跑微調模型
    lora_model, tokenizer = load_model(use_lora=True)
    lora_acc, lora_details = run_eval(lora_model, tokenizer, "微調模型（LoRA）")

    # Step 3：對比報告
    print_comparison(baseline_acc, lora_acc)

    # 儲存報告
    report = {
        "timestamp"   : datetime.now().isoformat(),
        "test_type"   : "hard_cases",
        "total"       : len(TEST_CASES),
        "baseline"    : {"accuracy": baseline_acc, "details": baseline_details},
        "lora"        : {"accuracy": lora_acc,     "details": lora_details},
        "improvement" : {k: round(lora_acc[k] - baseline_acc[k], 1) for k in baseline_acc},
    }
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n📄 完整對比報告已儲存：{REPORT_PATH}")


if __name__ == "__main__":
    main()
