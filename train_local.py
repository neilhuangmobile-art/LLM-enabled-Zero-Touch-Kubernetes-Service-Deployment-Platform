"""
train_local.py
Llama 3.1-8B LoRA Fine-tuning 腳本
執行：python train_local.py
"""
import os
import json
import torch
from datasets import load_dataset
from transformers import (
    AutoTokenizer,
    AutoModelForCausalLM,
    TrainingArguments,
    BitsAndBytesConfig,
)
from peft import LoraConfig
from trl import SFTTrainer

BASE_MODEL   = "meta-llama/Llama-3.1-8B-Instruct"
DATASET_PATH = r"D:\k8s_new\dataset\finetune_samples.jsonl"
OUTPUT_DIR   = r"D:\k8s_new\llama3_k8s_lora_results"

SYSTEM_PROMPT = (
    "You are an AI that converts Kubernetes deployment requests into JSON.\n"
    "ONLY output a valid JSON object. No explanation, no markdown, no extra text.\n"
    "Required fields: pods (integer), image (string), app_name (string)\n"
    "Optional fields: port (integer), memory (string, e.g. 256Mi)\n"
    'Example: {"pods": 3, "image": "nginx:latest", "app_name": "web-frontend", "port": 80}'
)

# tokenizer 需要在 formatting 時使用，先宣告為 None
_tokenizer_ref = None


def formatting_prompts_func(examples):
    global _tokenizer_ref
    output_texts = []
    for i in range(len(examples["input"])):
        user_input = examples["input"][i]
        raw_output = examples["output"][i]

        if isinstance(raw_output, dict):
            output_json = json.dumps(raw_output, ensure_ascii=False)
        else:
            output_json = str(raw_output)

        # EOS token 讓模型學會輸出完 JSON 就停
        # 這樣模型不會再產生 ### User... 垃圾字串
        eos = _tokenizer_ref.eos_token if _tokenizer_ref else ""

        text = (
            f"### System\n{SYSTEM_PROMPT}\n\n"
            f"### User\n{user_input}\n"
            f"### Assistant\n"
            f"{output_json}{eos}"
        )
        output_texts.append(text)
    return output_texts


def _quick_test(model, tokenizer):
    test_cases = [
        "deploy 3 pods of nginx:latest",
        "幫我起 5 個 api-gateway，映像檔是 node:20-alpine",
        "launch 2 postgres:15 pods named db-primary, port 5432",
        "部署 4 個 data-processor，python:3.11-slim，記憶體 512Mi",
        "spin up 1 redis:7-alpine pod for cache-server",
        "建立 7 個 web-frontend，image nginx:latest，開放 port 80",
        "run 10 golang:1.21-alpine pods for scheduler",
        "起 8 個 auth-service，python:3.11-slim",
    ]

    print("\n🧪 訓練後快速驗證：")
    model.eval()
    passed = 0
    for test_input in test_cases:
        prompt = (
            f"### System\n{SYSTEM_PROMPT}\n\n"
            f"### User\n{test_input}\n"
            "### Assistant\n{"
        )
        inputs    = tokenizer(prompt, return_tensors="pt").to("cuda")
        input_len = inputs["input_ids"].shape[1]

        with torch.no_grad():
            outputs = model.generate(
                **inputs,
                max_new_tokens=80,
                temperature=0.1,
                do_sample=True,
                pad_token_id=tokenizer.eos_token_id,
            )

        new_tokens = outputs[0][input_len:]
        result     = tokenizer.decode(new_tokens, skip_special_tokens=True).strip()
        if not result.startswith("{"):
            result = "{" + result
        first_line = result.split("\n")[0].strip()

        try:
            import json as _json, re
            fixed = re.sub(r':\s*null\b', ': "NULL"', first_line)
            parsed = _json.loads(fixed)
            parsed = {k: v for k, v in parsed.items() if v != "NULL"}
            ok = "pods" in parsed and "image" in parsed
        except Exception:
            ok = False

        status = "✅" if ok else "❌"
        if ok:
            passed += 1
        print(f"  {status} {test_input[:45]}")
        if ok:
            print(f"     → {parsed}")
        else:
            print(f"     → 原始輸出: {first_line[:60]}")
        print()

    print(f"  驗證結果：{passed}/{len(test_cases)} 通過")


def main():
    global _tokenizer_ref

    print(f"📦 載入資料集：{DATASET_PATH}")
    if not os.path.exists(DATASET_PATH):
        print("❌ 找不到訓練資料！請先執行 generate_finetune_data.py")
        return

    dataset = load_dataset("json", data_files=DATASET_PATH, split="train")
    print(f"✅ 共 {len(dataset)} 筆訓練資料")

    print("\n📋 資料預覽（前3筆）：")
    for i in range(min(3, len(dataset))):
        print(f"  input : {dataset[i]['input']}")
        print(f"  output: {dataset[i]['output']}")
        print()

    if len(dataset) < 50:
        print(f"⚠️  警告：只有 {len(dataset)} 筆資料")
        ans = input("是否仍要繼續訓練？(y/n): ").strip().lower()
        if ans != "y":
            return

    print(f"\n🤖 載入基礎模型：{BASE_MODEL}")
    tokenizer = AutoTokenizer.from_pretrained(BASE_MODEL)
    tokenizer.pad_token    = tokenizer.eos_token
    tokenizer.padding_side = "right"
    _tokenizer_ref = tokenizer  # 讓 formatting_prompts_func 可以用

    bnb_config = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_compute_dtype=torch.float16,
    )
    model = AutoModelForCausalLM.from_pretrained(
        BASE_MODEL,
        quantization_config=bnb_config,
        device_map={"": 0}
    )
    model.config.use_cache = False

    lora_config = LoraConfig(
        r=16,
        lora_alpha=32,
        target_modules=[
            "q_proj", "k_proj", "v_proj", "o_proj",
            "gate_proj", "up_proj", "down_proj",
        ],
        lora_dropout=0.05,
        bias="none",
        task_type="CAUSAL_LM",
    )

    num_samples     = len(dataset)
    batch_size      = 2
    grad_accum      = 4
    effective_batch = batch_size * grad_accum
    steps_per_epoch = max(1, num_samples // effective_batch)
    total_epochs    = 2
    total_steps     = steps_per_epoch * total_epochs

    print(f"\n⚙️  訓練規劃：")
    print(f"   樣本數         : {num_samples}")
    print(f"   有效 batch size: {effective_batch}")
    print(f"   每 epoch steps : {steps_per_epoch}")
    print(f"   訓練 epochs    : {total_epochs}")
    print(f"   總 steps       : {total_steps}")
    print(f"   EOS token 已啟用：模型學會輸出後停止 ✅")

    training_args = TrainingArguments(
        output_dir=OUTPUT_DIR,
        per_device_train_batch_size=batch_size,
        gradient_accumulation_steps=grad_accum,
        learning_rate=2e-4,
        max_steps=total_steps,
        fp16=True,
        logging_steps=max(1, steps_per_epoch // 2),
        save_strategy="no",
        report_to="none",
        gradient_checkpointing=True,
        gradient_checkpointing_kwargs={"use_reentrant": False},
        warmup_steps=max(1, total_steps // 10),
        lr_scheduler_type="cosine",
        weight_decay=0.01,
    )

    trainer = SFTTrainer(
        model=model,
        train_dataset=dataset,
        peft_config=lora_config,
        formatting_func=formatting_prompts_func,
        max_seq_length=256,
        args=training_args,
    )

    print("\n🚀 開始 Fine-tuning...")
    trainer.train()

    print(f"\n💾 儲存 LoRA 權重到：{OUTPUT_DIR}")
    trainer.model.save_pretrained(OUTPUT_DIR)
    tokenizer.save_pretrained(OUTPUT_DIR)

    _quick_test(model, tokenizer)

    print("\n✅ 訓練完成！")
    print("   接下來：python eval_model.py 確認準確率")
    print("           python web_demo.py 啟動 Demo")


if __name__ == "__main__":
    main()
