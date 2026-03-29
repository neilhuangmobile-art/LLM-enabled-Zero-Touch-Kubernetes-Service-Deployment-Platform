"""
llama_client.py
Llama 3.1 推理模組 + 高品質標註資料收集
"""
import torch
import json
import os
import re
from typing import Optional
from datetime import datetime
from transformers import AutoTokenizer, AutoModelForCausalLM, BitsAndBytesConfig
from peft import PeftModel

BASE_MODEL   = "meta-llama/Llama-3.1-8B-Instruct"
ADAPTER_PATH = r"D:\k8s_new\llama3_k8s_lora_results"
DATASET_PATH = r"D:\k8s_new\dataset\finetune_samples.jsonl"

_model, _tokenizer = None, None

SYSTEM_PROMPT = (
    "You are an AI that converts Kubernetes deployment requests into JSON.\n"
    "ONLY output a valid JSON object. No explanation, no markdown, no extra text.\n"
    "Required fields: pods (integer), image (string), app_name (string)\n"
    "Optional fields: port (integer), memory (string, e.g. 256Mi)\n"
    'Example: {"pods": 3, "image": "nginx:latest", "app_name": "web-frontend", "port": 80}'
)


def _load_model_once():
    global _model, _tokenizer
    if _model is not None:
        return _model, _tokenizer

    print("🧹 正在清理顯存並載入 Llama 3.1 GPU 專家模型 (4-bit 量化)...")
    torch.cuda.empty_cache()

    bnb_config = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_compute_dtype=torch.float16,
    )

    _tokenizer = AutoTokenizer.from_pretrained(BASE_MODEL)
    _tokenizer.pad_token = _tokenizer.eos_token

    base = AutoModelForCausalLM.from_pretrained(
        BASE_MODEL,
        quantization_config=bnb_config,
        device_map={"": 0}
    )
    base.config.use_cache = True

    if os.path.exists(ADAPTER_PATH):
        print(f"✅ 偵測到微調權重，正在合併：{ADAPTER_PATH}")
        _model = PeftModel.from_pretrained(base, ADAPTER_PATH)
    else:
        print("⚠️  未找到 LoRA 權重，使用原始基礎模型")
        _model = base

    _model.eval()
    return _model, _tokenizer


def _extract_first_json(text: str) -> Optional[str]:
    """
    從字串中精確抽出第一個完整的 JSON 物件。
    做法：找到第一個 { 之後，逐字追蹤括號深度，
    遇到配對的 } 就停，完全不依賴 regex 的貪婪匹配。
    """
    start = text.find("{")
    if start == -1:
        return None

    depth     = 0
    in_string = False
    escape    = False

    for i in range(start, len(text)):
        ch = text[i]
        if escape:
            escape = False
            continue
        if ch == "\\" and in_string:
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]

    return None


def _fix_nulls(text: str) -> str:
    """把 JSON 裡的 null 值換成字串 NULL，讓 json.loads 能正常解析"""
    return re.sub(r'(?<=:)\s*null\b', ' "NULL"', text)


def _parse(text: str) -> Optional[dict]:
    """
    從任意字串中解析出 K8s JSON，四層策略：
    1. 抽出第一個完整 JSON → 修 null → 解析
    2. json_repair
    3. Regex 手動萃取
    """
    # 先抽出第一個完整 JSON 區塊
    snippet = _extract_first_json(text)
    if snippet:
        # 修 null 再解析
        fixed = _fix_nulls(snippet)
        try:
            result = json.loads(fixed)
            if isinstance(result, dict):
                return {k: v for k, v in result.items() if v != "NULL"}
        except Exception:
            pass

    # 策略 2：json_repair
    try:
        from json_repair import repair_json
        target = snippet or text
        result = json.loads(repair_json(_fix_nulls(target)))
        if isinstance(result, dict):
            print("[DEBUG] json_repair 修復成功")
            return {k: v for k, v in result.items() if v != "NULL"}
    except Exception:
        pass

    # 策略 3：Regex 手動萃取（最後防線）
    src = snippet or text
    pods_m  = re.search(r'"(?:pods|replicas)"\s*:\s*(\d+)', src)
    image_m = re.search(r'"image"\s*:\s*"([^"]+)"',         src)
    app_m   = re.search(r'"app_name"\s*:\s*"([^"]+)"',      src)
    port_m  = re.search(r'"port"\s*:\s*(\d+)',              src)
    mem_m   = re.search(r'"memory"\s*:\s*"([^"]+)"',        src)

    if pods_m:
        print("[DEBUG] 使用 Regex 萃取")
        result = {
            "pods"    : int(pods_m.group(1)),
            "image"   : image_m.group(1) if image_m else "nginx:latest",
            "app_name": app_m.group(1)   if app_m   else "auto-app",
        }
        if port_m:
            result["port"] = int(port_m.group(1))
        if mem_m and mem_m.group(1) != "NULL":
            result["memory"] = mem_m.group(1)
        return result

    return None


def _validate(result: dict) -> bool:
    if not result or not isinstance(result, dict):
        return False
    if "error" in result:
        return False
    try:
        pods_int = int(result.get("pods", 0))
        return 1 <= pods_int <= 100
    except (ValueError, TypeError):
        return False


def ask_llama(prompt_text: str) -> dict:
    try:
        model, tokenizer = _load_model_once()

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
                max_new_tokens=128,
                temperature=0.3,
                do_sample=True,
                top_p=0.9,
                repetition_penalty=1.2,
                eos_token_id=tokenizer.eos_token_id,
                pad_token_id=tokenizer.eos_token_id,
            )

        new_tokens = outputs[0][input_len:]
        generated  = tokenizer.decode(new_tokens, skip_special_tokens=True).strip()

        # 補回預填的 {
        if not generated.startswith("{"):
            generated = "{" + generated

        print(f"[DEBUG] 模型原始輸出：{generated}")

        # 直接抽出第一個完整 JSON，垃圾內容完全無視
        snippet = _extract_first_json(generated)
        if not snippet:
            raise ValueError(f"找不到完整 JSON，原始輸出：{generated}")

        # 修 null 再解析
        snippet = re.sub(r':\s*null\b', ': "NULL"', snippet)
        print(f"[DEBUG] 抽出 JSON：{snippet}")

        result = _parse(snippet)

        if result and _validate(result):
            result.setdefault("image",    "nginx:latest")
            result.setdefault("app_name", "auto-app")
            result["pods"] = int(result["pods"])

            if "port" in result:
                try:
                    p = int(result["port"])
                    if 1 <= p <= 65535:
                        result["port"] = p
                    else:
                        del result["port"]
                except (ValueError, TypeError):
                    del result["port"]

            if "memory" in result:
                if not re.match(r"^\d+(Mi|Gi|Ki|M|G)$", str(result["memory"])):
                    del result["memory"]

            return result

        raise ValueError(f"驗證失敗，解析結果：{result}")

    except Exception as e:
        return {"error": "解析失敗", "raw": str(e)}


def save_gold_sample(user_input: str, corrected_json: dict):
    if "error" in corrected_json:
        print("⚠️  有 error，跳過存檔")
        return

    try:
        pods = int(corrected_json.get("pods", 0))
        assert 1 <= pods <= 100
    except Exception:
        print("⚠️  pods 異常，跳過存檔")
        return

    user_input = user_input.strip()
    if user_input.isdigit():
        user_input = f"deploy {user_input} pods"

    dataset_dir = os.path.dirname(DATASET_PATH)
    if dataset_dir and not os.path.exists(dataset_dir):
        os.makedirs(dataset_dir)

    sample = {
        "input" : user_input,
        "output": {
            "pods"    : pods,
            "image"   : corrected_json.get("image",    "nginx:latest"),
            "app_name": corrected_json.get("app_name", "auto-app"),
            **( {"port":   corrected_json["port"]}   if "port"   in corrected_json else {} ),
            **( {"memory": corrected_json["memory"]} if "memory" in corrected_json else {} ),
        },
        "timestamp": datetime.utcnow().isoformat(),
    }

    with open(DATASET_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(sample, ensure_ascii=False) + "\n")

    print(f"📝 已存入：{sample['input']} → {sample['output']}")
