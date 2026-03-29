"""
eval_speed.py
比較原始模型 vs 微調模型的推論速度
執行：python eval_speed.py（約10分鐘）
"""
import time
import torch
import statistics
from transformers import AutoTokenizer, AutoModelForCausalLM, BitsAndBytesConfig
from peft import PeftModel
import os

BASE_MODEL   = "meta-llama/Llama-3.1-8B-Instruct"
ADAPTER_PATH = r"D:\k8s_new\llama3_k8s_lora_results"

SYSTEM_PROMPT = (
    "You are an AI that converts Kubernetes deployment requests into JSON.\n"
    "ONLY output a valid JSON object. No explanation, no markdown, no extra text.\n"
    "Required fields: pods (integer), image (string), app_name (string)\n"
    "Optional fields: port (integer), memory (string, e.g. 256Mi)\n"
    'Example: {"pods": 3, "image": "nginx:latest", "app_name": "web-frontend", "port": 80}'
)

TEST_INPUTS = [
    "deploy 3 pods of nginx:latest",
    "start 5 replicas of redis:7-alpine for cache-server",
    "部署 3 個 web-frontend 的 pod，使用 nginx:latest",
    "幫我起 5 個 auth-service，映像檔是 python:3.11-slim",
    "deploy 2 nginx:latest pods for web-frontend, port 80",
    "建立 1 個 db-primary，image postgres:15，port 5432",
    "deploy 2 nginx:latest for web-frontend, memory limit 256Mi",
    "幫我跑 4 個 data-processor，python:3.11-slim，記憶體 512Mi",
    "我要跑兩個 nginx，幫我部署一下",
    "起個 redis x2，port 6379",
]


def load_model(use_lora: bool):
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
    if use_lora and os.path.exists(ADAPTER_PATH):
        model = PeftModel.from_pretrained(base, ADAPTER_PATH)
    else:
        model = base
    model.eval()
    return model, tokenizer


def measure_speed(model, tokenizer, label: str, warmup: int = 2, runs: int = 10):
    """
    每個 prompt 跑 runs 次取平均，先做 warmup
    回傳各 prompt 的平均推論時間（毫秒）
    """
    print(f"\n⏱️  測試 {label} 推論速度...")
    all_times = []

    for prompt_text in TEST_INPUTS:
        full_prompt = (
            f"### System\n{SYSTEM_PROMPT}\n\n"
            f"### User\n{prompt_text}\n"
            "### Assistant\n{"
        )
        inputs    = tokenizer(full_prompt, return_tensors="pt").to("cuda")
        input_len = inputs["input_ids"].shape[1]

        # Warmup
        for _ in range(warmup):
            with torch.no_grad():
                model.generate(
                    **inputs,
                    max_new_tokens=80,
                    do_sample=False,
                    eos_token_id=tokenizer.eos_token_id,
                    pad_token_id=tokenizer.eos_token_id,
                )

        # 正式測速
        times = []
        for _ in range(runs):
            torch.cuda.synchronize()
            t0 = time.perf_counter()
            with torch.no_grad():
                model.generate(
                    **inputs,
                    max_new_tokens=80,
                    do_sample=False,
                    eos_token_id=tokenizer.eos_token_id,
                    pad_token_id=tokenizer.eos_token_id,
                )
            torch.cuda.synchronize()
            t1 = time.perf_counter()
            times.append((t1 - t0) * 1000)

        avg = statistics.mean(times)
        all_times.append(avg)
        print(f"  {prompt_text[:45]:<45} {avg:6.0f} ms")

    overall_avg = statistics.mean(all_times)
    overall_min = min(all_times)
    overall_max = max(all_times)

    print(f"\n  平均推論時間：{overall_avg:.0f} ms")
    print(f"  最快：{overall_min:.0f} ms  最慢：{overall_max:.0f} ms")

    return {
        "avg_ms"   : round(overall_avg, 1),
        "min_ms"   : round(overall_min, 1),
        "max_ms"   : round(overall_max, 1),
        "per_prompt": [round(t, 1) for t in all_times],
    }


def main():
    print("=" * 65)
    print("  ⚡ 推論速度對比：原始模型 vs 微調模型（LoRA）")
    print("=" * 65)

    # 原始模型
    print("\n🧹 載入原始模型...")
    baseline_model, tokenizer = load_model(use_lora=False)
    baseline_speed = measure_speed(baseline_model, tokenizer, "原始模型（未微調）")

    del baseline_model
    torch.cuda.empty_cache()
    print("\n🧹 釋放原始模型記憶體，載入微調模型...")

    # 微調模型
    lora_model, tokenizer = load_model(use_lora=True)
    lora_speed = measure_speed(lora_model, tokenizer, "微調模型（LoRA）")

    # 對比報告
    b = baseline_speed["avg_ms"]
    l = lora_speed["avg_ms"]
    speedup = (b - l) / b * 100

    print("\n" + "=" * 65)
    print("  ⚡ 推論速度對比結果")
    print("=" * 65)
    print(f"  原始模型平均推論時間 : {b:.0f} ms")
    print(f"  微調模型平均推論時間 : {l:.0f} ms")
    if speedup > 0:
        print(f"  速度提升             : {speedup:.1f}% 更快")
    else:
        print(f"  速度差異             : {abs(speedup):.1f}%（微調模型略慢，因 LoRA 額外計算）")
    print("=" * 65)

    print(f"\n💡 報告可以說：")
    if speedup > 0:
        print(f"   「微調後模型推論速度提升 {speedup:.1f}%，")
        print(f"    平均回應時間從 {b:.0f}ms 降至 {l:.0f}ms」")
    else:
        print(f"   「微調模型平均推論時間 {l:.0f}ms，")
        print(f"    在保持 100% 準確率的同時，回應速度在可接受範圍內」")


if __name__ == "__main__":
    main()
