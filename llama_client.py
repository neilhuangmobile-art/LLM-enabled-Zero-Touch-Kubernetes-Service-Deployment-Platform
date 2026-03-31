"""
llama_client.py
Llama 3.1 推理模組 + 高品質標註資料收集

使用優先順序：
1. 優先連線 core/model_server.py（常駐 HTTP server，不需重載模型）
   - 若 server 尚未啟動，自動在背景啟動它（首次需等待 1~2 分鐘）
   - 之後每次呼叫都是毫秒級，不需重載模型
2. Server 啟動失敗時，fallback 到本地直接載入
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core.config import ensure_utf8_output
ensure_utf8_output()

import torch
import json
import re
import time
import subprocess
from typing import Optional
from datetime import datetime, timezone

# 從 config 引用，不再硬編碼路徑
from core.config import (
    BASE_MODEL, ADAPTER_PATH, DATASET_PATH,
    SYSTEM_PROMPT, MODEL_SERVER_URL,
)

_model, _tokenizer = None, None
_server_launch_attempted = False  # 避免重複嘗試啟動


# ══════════════════════════════════════════════════════════════════
# RAG 知識增強（選用，索引不存在時自動跳過）
# ══════════════════════════════════════════════════════════════════
def _try_augment_with_rag(prompt_text: str) -> str:
    """
    嘗試以 K8s 知識庫增強 prompt，防止 LLM 幻覺。
    若索引未建立或 rag 模組不可用，靜默回傳原始 prompt。
    """
    try:
        from rag.retriever import augment_prompt
        augmented = augment_prompt(prompt_text, top_k=2, max_context_chars=500)
        if augmented != prompt_text:
            print("[RAG] 知識增強已注入")
        return augmented
    except Exception:
        return prompt_text  # 靜默降級，不影響主流程


# ══════════════════════════════════════════════════════════════════
# 自動啟動 Model Server（核心改善：不再需要手動啟動）
# ══════════════════════════════════════════════════════════════════
def _is_server_alive() -> bool:
    """檢查 Model Server 是否已在運行。"""
    try:
        import urllib.request
        urllib.request.urlopen(f"{MODEL_SERVER_URL}/health", timeout=2)
        return True
    except Exception:
        return False


def _auto_start_server() -> bool:
    """
    若 Model Server 尚未啟動，自動在背景啟動並等待就緒。
    解決「每次重跑程式碼都要重新載入模型」的問題：
      - 第一次：啟動 server，等待模型載入（1~2 分鐘）
      - 之後每次：server 已在記憶體，呼叫幾乎是即時的
    """
    global _server_launch_attempted

    if _is_server_alive():
        return True  # 已在跑，直接用

    if _server_launch_attempted:
        return False  # 已嘗試啟動過，不重複

    _server_launch_attempted = True

    server_py = os.path.join(os.path.dirname(os.path.abspath(__file__)), "core", "model_server.py")
    if not os.path.exists(server_py):
        print("[Model Server] 找不到 core/model_server.py，改用本地載入")
        return False

    print("=" * 55)
    print("[Model Server] 未啟動，正在自動後台啟動...")
    print("   首次啟動需要載入模型（約 1~2 分鐘）")
    print("   之後每次執行都幾乎是即時的，不需重載")
    print("=" * 55)

    log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "model_server.log")
    with open(log_path, "w", encoding="utf-8") as log_f:
        subprocess.Popen(
            [sys.executable, server_py],
            stdout=log_f,
            stderr=log_f,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
        )

    # 輪詢等待就緒（最多 3 分鐘）
    print("   等待模型載入", end="", flush=True)
    for i in range(180):
        time.sleep(1)
        if _is_server_alive():
            print(f"\n[Model Server] 就緒！（{i + 1} 秒）")
            return True
        if i % 15 == 14:
            print(".", end="", flush=True)

    print("\n[Model Server] 啟動超時，改用本地載入（較慢）")
    return False


# ══════════════════════════════════════════════════════════════════
# HTTP Client（優先使用 Model Server）
# ══════════════════════════════════════════════════════════════════
def _try_server(prompt_text: str) -> Optional[dict]:
    """嘗試呼叫常駐 Model Server。未啟動時回傳 None（觸發 fallback）。"""
    try:
        import urllib.request
        body = json.dumps({"prompt": prompt_text}).encode()
        req  = urllib.request.Request(
            f"{MODEL_SERVER_URL}/infer",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=300) as resp:
            data = json.loads(resp.read())
            return data.get("result")
    except Exception:
        return None  # server 未啟動，靜默 fallback


# ══════════════════════════════════════════════════════════════════
# 本地直接載入（Fallback）
# ══════════════════════════════════════════════════════════════════
def _load_model_once():
    global _model, _tokenizer
    if _model is not None:
        return _model, _tokenizer

    from transformers import AutoTokenizer, AutoModelForCausalLM, BitsAndBytesConfig
    from peft import PeftModel

    print("[Local] 正在清理顯存並載入 Llama 3.1 GPU 專家模型 (4-bit 量化)...")
    print("[Local] 提示：執行 'python core/model_server.py' 可避免每次重載模型")
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
        device_map={"": 0},
    )
    base.config.use_cache = True

    if os.path.exists(ADAPTER_PATH):
        print(f"[Local] 偵測到微調權重，正在合併：{ADAPTER_PATH}")
        _model = PeftModel.from_pretrained(base, ADAPTER_PATH)
    else:
        print("[Local] 未找到 LoRA 權重，使用原始基礎模型")
        _model = base

    _model.eval()
    return _model, _tokenizer


# ══════════════════════════════════════════════════════════════════
# 解析輔助
# ══════════════════════════════════════════════════════════════════
def _extract_first_json(text: str) -> Optional[str]:
    start = text.find("{")
    if start == -1:
        return None
    depth, in_string, escape = 0, False, False
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
    return re.sub(r'(?<=:)\s*null\b', ' "NULL"', text)


def _parse(text: str) -> Optional[dict]:
    snippet = _extract_first_json(text)
    if snippet:
        fixed = _fix_nulls(snippet)
        try:
            result = json.loads(fixed)
            if isinstance(result, dict):
                return {k: v for k, v in result.items() if v != "NULL"}
        except Exception:
            pass

    try:
        from json_repair import repair_json
        target = snippet or text
        result = json.loads(repair_json(_fix_nulls(target)))
        if isinstance(result, dict):
            print("[DEBUG] json_repair 修復成功")
            return {k: v for k, v in result.items() if v != "NULL"}
    except Exception:
        pass

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
        return 1 <= int(result.get("pods", 0)) <= 100
    except (ValueError, TypeError):
        return False


def _local_infer(prompt_text: str) -> dict:
    """本地直接推論（fallback 路徑）。"""
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
    if not generated.startswith("{"):
        generated = "{" + generated

    print(f"[DEBUG] 模型原始輸出：{generated}")

    snippet = _extract_first_json(generated)
    if not snippet:
        raise ValueError(f"找不到完整 JSON，原始輸出：{generated}")

    snippet = re.sub(r':\s*null\b', ': "NULL"', snippet)
    result  = _parse(snippet)

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


# ══════════════════════════════════════════════════════════════════
# 公開 API
# ══════════════════════════════════════════════════════════════════
def ask_llama(prompt_text: str) -> dict:
    """
    呼叫 LLM 推論。
    1. 自動確保 Model Server 在跑（首次啟動需等待，之後即時）
    2. 透過 HTTP 呼叫 Model Server（快）
    3. Server 無法使用時 fallback 到本地載入
    """
    try:
        # RAG 知識增強（索引不存在時自動跳過）
        enhanced = _try_augment_with_rag(prompt_text)

        # 自動啟動 Model Server（已在跑則直接跳過）
        _auto_start_server()

        # 嘗試 HTTP server
        server_result = _try_server(enhanced)
        if server_result is not None:
            return server_result

        # Fallback：本地直接載入
        return _local_infer(enhanced)

    except Exception as e:
        return {"error": "解析失敗", "raw": str(e)}


def save_gold_sample(user_input: str, corrected_json: dict):
    if "error" in corrected_json:
        print("[Dataset] 有 error，跳過存檔")
        return

    try:
        pods = int(corrected_json.get("pods", 0))
        assert 1 <= pods <= 100
    except Exception:
        print("[Dataset] pods 異常，跳過存檔")
        return

    user_input = user_input.strip()
    if user_input.isdigit():
        user_input = f"deploy {user_input} pods"

    sample = {
        "input" : user_input,
        "output": {
            "pods"    : pods,
            "image"   : corrected_json.get("image",    "nginx:latest"),
            "app_name": corrected_json.get("app_name", "auto-app"),
            **( {"port":   corrected_json["port"]}   if "port"   in corrected_json else {} ),
            **( {"memory": corrected_json["memory"]} if "memory" in corrected_json else {} ),
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    with open(DATASET_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(sample, ensure_ascii=False) + "\n")

    print(f"[Dataset] 已存入：{sample['input']} → {sample['output']}")
