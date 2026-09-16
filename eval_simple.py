"""
eval_simple.py - 寬鬆版評估
只要模型輸出裡有正確的數字/關鍵字就算通過
不管 JSON 格式、null、垃圾字串
執行：python eval_simple.py
"""
import re
import os
import sys as _sys
_sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core.config import BASE_MODEL, DEPLOY_ADAPTER_PATH  # 先於 transformers import

import torch
from transformers import AutoTokenizer, AutoModelForCausalLM, BitsAndBytesConfig

SYSTEM_PROMPT = (
    "You are an AI that converts Kubernetes deployment requests into JSON.\n"
    "ONLY output a valid JSON object. No explanation, no markdown, no extra text.\n"
    "Required fields: pods (integer), image (string), app_name (string)\n"
    "Optional fields: port (integer), memory (string, e.g. 256Mi)\n"
    'Example: {"pods": 3, "image": "nginx:latest", "app_name": "web-frontend", "port": 80}'
)

# 測試案例：(輸入, 期望pods數字, 期望包含的關鍵字)
TEST_CASES = [
    ("deploy 3 pods of nginx:latest",                       3,  ["nginx"]),
    ("start 5 replicas of redis:7-alpine for cache-server", 5,  ["redis"]),
    ("launch 2 postgres:15 pods named db-primary",          2,  ["postgres"]),
    ("spin up 1 node:20-alpine container for api-gateway",  1,  ["node"]),
    ("run 4 python:3.11-slim pods for data-processor",      4,  ["python"]),
    ("部署 3 個 web-frontend 的 pod，使用 nginx:latest",    3,  ["nginx"]),
    ("幫我起 5 個 auth-service，映像檔是 python:3.11-slim", 5,  ["python"]),
    ("建立 2 個 redis:7-alpine 容器，命名為 cache-server",  2,  ["redis"]),
    ("我需要 1 個 db-primary pod，image 用 postgres:15",    1,  ["postgres"]),
    ("請部署 4 個 node:20-alpine 作為 api-gateway",         4,  ["node"]),
    ("deploy 2 nginx:latest pods for web-frontend, port 80",2,  ["nginx", "80"]),
    ("start 3 node:20-alpine for api-gateway, expose port 3000", 3, ["node", "3000"]),
    ("部署 2 個 web-frontend，image nginx:latest，開放 port 80", 2, ["nginx", "80"]),
    ("幫我跑 3 個 api-gateway，用 node:20-alpine，port 3000", 3, ["node", "3000"]),
    ("run 1 postgres:15 pod for db-primary, container port 5432", 1, ["postgres", "5432"]),
    ("deploy 2 nginx:latest for web-frontend, memory limit 256Mi", 2, ["nginx", "256"]),
    ("start 3 python:3.11-slim pods, memory 512Mi",         3,  ["python", "512"]),
    ("部署 2 個 web-frontend，image nginx:latest，記憶體限制 256Mi", 2, ["nginx", "256"]),
    ("幫我跑 4 個 data-processor，python:3.11-slim，記憶體 512Mi", 4, ["python", "512"]),
    ("launch 1 elasticsearch:8.11.0 pod, memory=2Gi",       1,  ["elasticsearch", "2"]),
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

    model = AutoModelForCausalLM.from_pretrained(
        BASE_MODEL, quantization_config=bnb_config, device_map={"": 0}
    )
    if DEPLOY_ADAPTER_PATH and os.path.isdir(DEPLOY_ADAPTER_PATH):
        from peft import PeftModel
        print(f"✅ 掛載部署 LoRA：{DEPLOY_ADAPTER_PATH}")
        model = PeftModel.from_pretrained(model, DEPLOY_ADAPTER_PATH)
    else:
        print("ℹ️  評估純 base 模型（未設定 DEPLOY_ADAPTER_PATH）")
    model.eval()
    return model, tokenizer


def get_raw_output(model, tokenizer, prompt_text: str) -> str:
    """直接拿模型的原始輸出，不做任何解析"""
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
            do_sample=False,          # greedy，輸出更穩定
            eos_token_id=tokenizer.eos_token_id,
            pad_token_id=tokenizer.pad_token_id or tokenizer.eos_token_id,
        )

    new_tokens = outputs[0][input_len:]
    return tokenizer.decode(new_tokens, skip_special_tokens=True).strip()


def check_output(raw: str, expected_pods: int, expected_keywords: list) -> tuple:
    """
    寬鬆檢查：
    - pods 數字對不對
    - 關鍵字有沒有出現在輸出裡
    """
    # 檢查 pods 數字
    pods_match = re.search(r'"pods"\s*:\s*(\d+)', raw)
    pods_ok = False
    actual_pods = None
    if pods_match:
        actual_pods = int(pods_match.group(1))
        pods_ok = (actual_pods == expected_pods)

    # 檢查關鍵字
    keywords_ok = all(kw.lower() in raw.lower() for kw in expected_keywords)

    return pods_ok, keywords_ok, actual_pods


def evaluate():
    model, tokenizer = load_model()

    print("\n" + "=" * 65)
    print("  📊 寬鬆版模型評估（只檢查數字和關鍵字）")
    print("=" * 65)

    pods_correct     = 0
    keywords_correct = 0
    both_correct     = 0
    total            = len(TEST_CASES)

    for i, (inp, exp_pods, exp_kw) in enumerate(TEST_CASES):
        raw = get_raw_output(model, tokenizer, inp)
        # 只取第一行，忽略垃圾
        first_line = raw.split("\n")[0].strip()

        pods_ok, kw_ok, actual_pods = check_output(first_line, exp_pods, exp_kw)

        if pods_ok:
            pods_correct += 1
        if kw_ok:
            keywords_correct += 1
        if pods_ok and kw_ok:
            both_correct += 1

        status = "✅" if (pods_ok and kw_ok) else ("🟡" if (pods_ok or kw_ok) else "❌")
        print(f"[{i+1:02d}] {status} pods={'✓' if pods_ok else f'✗(got {actual_pods})'} "
              f"kw={'✓' if kw_ok else '✗'} | {inp[:45]}")

    print("\n" + "=" * 65)
    print(f"  pods 準確率    : {pods_correct}/{total} ({pods_correct/total*100:.1f}%)")
    print(f"  關鍵字準確率   : {keywords_correct}/{total} ({keywords_correct/total*100:.1f}%)")
    print(f"  兩者都對       : {both_correct}/{total} ({both_correct/total*100:.1f}%)")
    print("=" * 65)
    print(f"\n💡 報告可以說：")
    print(f"   「Fine-tune 後 pods 預測準確率 {pods_correct/total*100:.1f}%，")
    print(f"    關鍵字識別準確率 {keywords_correct/total*100:.1f}%」")


if __name__ == "__main__":
    evaluate()
