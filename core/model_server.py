"""
core/model_server.py
常駐 Model Server：啟動一次，模型永遠在記憶體。
其他腳本透過 HTTP API 呼叫，不需要每次重新載入模型。

啟動方式：
    python core/model_server.py

API 端點：
    POST /infer   {"prompt": "..."} → {"result": {...}}
    GET  /health  → {"status": "ok", "model_loaded": true}
    GET  /unload  → 卸載模型，釋放 VRAM
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Force UTF-8 stdout/stderr so Chinese/emoji print won't crash on cp950 terminals
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import json
import re
import torch
import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional
from transformers import AutoTokenizer, AutoModelForCausalLM, BitsAndBytesConfig
from peft import PeftModel

from core.config import BASE_MODEL, ADAPTER_PATH, SYSTEM_PROMPT, MODEL_SERVER_HOST, MODEL_SERVER_PORT

@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        _load()
    except Exception as e:
        print(f"[Model Server] 模型載入失敗，server 仍啟動，/infer 將回傳 503：{e}", file=sys.stderr)
    yield

app = FastAPI(title="K8s LLM Model Server", lifespan=lifespan)

# ── 全域模型狀態 ─────────────────────────────────────────────────
_model     = None
_tokenizer = None
_device    = "cuda" if torch.cuda.is_available() else "cpu"


class InferRequest(BaseModel):
    prompt: str
    max_new_tokens: int = 128
    temperature: float  = 0.3


# ── 模型載入 ─────────────────────────────────────────────────────
def _load():
    global _model, _tokenizer
    if _model is not None:
        return

    print("[Model Server] 正在載入模型（只需這一次）...")
    torch.cuda.empty_cache()

    _tokenizer = AutoTokenizer.from_pretrained(BASE_MODEL)
    _tokenizer.pad_token = _tokenizer.eos_token

    if _device == "cuda":
        bnb_config = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_compute_dtype=torch.float16,
        )
        base = AutoModelForCausalLM.from_pretrained(
            BASE_MODEL,
            quantization_config=bnb_config,
            device_map={"": 0},
        )
    else:
        print("[Model Server] 未偵測到 GPU，使用 CPU（速度較慢）")
        base = AutoModelForCausalLM.from_pretrained(
            BASE_MODEL,
            dtype=torch.float16,
            device_map="cpu",
            low_cpu_mem_usage=True,
        )
    base.config.use_cache = True

    if os.path.exists(ADAPTER_PATH):
        print(f"[Model Server] 合併 LoRA 權重：{ADAPTER_PATH}")
        _model = PeftModel.from_pretrained(base, ADAPTER_PATH)
    else:
        print("[Model Server] 未找到 LoRA 權重，使用原始基礎模型")
        _model = base

    _model.eval()
    print(f"[Model Server] 模型就緒，監聽 {MODEL_SERVER_HOST}:{MODEL_SERVER_PORT}")


# ── 輔助函式（與 llama_client.py 相同邏輯）────────────────────────
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


def _parse_output(raw: str) -> Optional[dict]:
    snippet = _extract_first_json(raw)
    if not snippet:
        return None
    fixed = re.sub(r':\s*null\b', ': "NULL"', snippet)
    try:
        result = json.loads(fixed)
        if isinstance(result, dict):
            return {k: v for k, v in result.items() if v != "NULL"}
    except Exception:
        pass
    # json_repair fallback
    try:
        from json_repair import repair_json
        result = json.loads(repair_json(fixed))
        if isinstance(result, dict):
            return {k: v for k, v in result.items() if v != "NULL"}
    except Exception:
        pass
    return None


def _validate(result: dict) -> bool:
    if not result or "error" in result:
        return False
    try:
        return 1 <= int(result.get("pods", 0)) <= 100
    except (ValueError, TypeError):
        return False


# ── API 端點 ─────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {"status": "ok", "model_loaded": _model is not None}


@app.get("/unload")
def unload():
    global _model, _tokenizer
    _model = None
    _tokenizer = None
    torch.cuda.empty_cache()
    return {"status": "unloaded"}


@app.post("/infer")
def infer(req: InferRequest):
    if _model is None:
        raise HTTPException(status_code=503, detail="模型尚未載入")

    full_prompt = (
        f"### System\n{SYSTEM_PROMPT}\n\n"
        f"### User\n{req.prompt}\n"
        "### Assistant\n{"
    )

    inputs    = _tokenizer(full_prompt, return_tensors="pt").to(_device)
    input_len = inputs["input_ids"].shape[1]

    with torch.no_grad():
        outputs = _model.generate(
            **inputs,
            max_new_tokens=req.max_new_tokens,
            temperature=req.temperature,
            do_sample=True,
            top_p=0.9,
            repetition_penalty=1.2,
            eos_token_id=_tokenizer.eos_token_id,
            pad_token_id=_tokenizer.eos_token_id,
        )

    new_tokens = outputs[0][input_len:]
    generated  = _tokenizer.decode(new_tokens, skip_special_tokens=True).strip()
    if not generated.startswith("{"):
        generated = "{" + generated

    result = _parse_output(generated)

    if result and _validate(result):
        result.setdefault("image",    "nginx:latest")
        result.setdefault("app_name", "auto-app")
        result["pods"] = int(result["pods"])
        if "port" in result:
            try:
                p = int(result["port"])
                result["port"] = p if 1 <= p <= 65535 else None
                if result["port"] is None:
                    del result["port"]
            except (ValueError, TypeError):
                del result["port"]
        if "memory" in result:
            if not re.match(r"^\d+(Mi|Gi|Ki|M|G)$", str(result["memory"])):
                del result["memory"]
        return {"result": result}

    return {"result": {"error": "解析失敗", "raw": generated[:200]}}


if __name__ == "__main__":
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as _s:
        if _s.connect_ex((MODEL_SERVER_HOST, MODEL_SERVER_PORT)) == 0:
            print(f"[Model Server] Port {MODEL_SERVER_PORT} 已被占用，server 已在執行中，直接退出。")
            sys.exit(0)
    uvicorn.run(app, host=MODEL_SERVER_HOST, port=MODEL_SERVER_PORT, log_level="warning")
