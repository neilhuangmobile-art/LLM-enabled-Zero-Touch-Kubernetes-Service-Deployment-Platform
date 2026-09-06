"""
core/model_server.py
常駐 Model Server：啟動一次，模型永遠在記憶體。
其他腳本透過 HTTP API 呼叫，不需要每次重新載入模型。

2026-09-06 起採「小模型雙軌」：
    - 部署模型（/infer、/chat）：Qwen2.5-3B-Instruct，4-bit 量化跑 GPU（約 2.2GB）
    - 監控模型（/diagnose）    ：Qwen2.5-1.5B-Instruct，跑 CPU，顯卡全留給部署模型
    兩顆各一把 lock 序列化自己的 generate()；prompt 一律走 tokenizer 的 chat template
    （Qwen ChatML），不再手寫 "### User" 角色標記（那個寫法會被使用者輸入偽造）。

啟動方式：
    python core/model_server.py

API 端點：
    POST /infer     {"prompt": "...", "examples": [{"input","output"}]} → {"result": {...}}
    POST /chat      {"message": "...", "history": [...]}                → {"reply": "..."}
    POST /diagnose  {"context": {...}}                                  → {"diagnosis": {...}}
    GET  /health    → {"status": "ok", "deploy_loaded": bool, "monitor_loaded": bool}
    GET  /unload    → 卸載模型，釋放記憶體
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 必須在 import transformers/peft（連帶拉進 huggingface_hub）之前執行：huggingface_hub
# 會在被 import 的當下就讀取 HF_HOME 等環境變數決定快取路徑常數，事後才設定 os.environ
# 不會生效。2026-08-07：HF_HOME 之前指到的 NTFS 磁碟局部損毀過，教訓是快取路徑相關的
# 環境變數必須在這裡最先載入，不能只依賴後面才 import 的 core.config 順便觸發。
from core.config import (
    BASE_MODEL, DEPLOY_ADAPTER_PATH, MONITOR_MODEL, MONITOR_DEVICE,
    SYSTEM_PROMPT, MODEL_SERVER_HOST, MODEL_SERVER_PORT, HF_TOKEN,
)

# Force UTF-8 stdout/stderr so Chinese/emoji print won't crash on cp950 terminals
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import json
import re
import threading
import torch
import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
from transformers import AutoTokenizer, AutoModelForCausalLM, BitsAndBytesConfig

# 選用：把小模型偶爾漏出的簡體字轉成繁體（台灣用語）。未安裝 opencc 時原樣輸出。
try:
    from opencc import OpenCC
    _s2tw = OpenCC("s2twp")
except Exception:
    _s2tw = None


def _to_tw(text: str) -> str:
    if _s2tw and text and any("一" <= c <= "鿿" for c in text):
        try:
            return _s2tw.convert(text)
        except Exception:
            return text
    return text


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        _load()
    except Exception as e:
        print(f"[Model Server] 模型載入失敗，server 仍啟動，端點將回傳 503：{e}", file=sys.stderr)
    yield


app = FastAPI(title="K8s LLM Model Server", lifespan=lifespan)

# ── 全域模型狀態 ─────────────────────────────────────────────────
_deploy_model  = None
_deploy_tok    = None
_monitor_model = None
_monitor_tok   = None
_gpu_available = torch.cuda.is_available()

# 部署模型 /infer、/chat 共用一個物件；監控模型獨立。各自一把鎖序列化自己的 generate()。
_deploy_lock  = threading.Lock()
_monitor_lock = threading.Lock()


class InferRequest(BaseModel):
    prompt: str
    max_new_tokens: int = 200
    temperature: float = 0.3
    # RAG few-shot：每筆 {"input": "...", "output": {...}}，會以 user/assistant 對話對
    # 插在真正的使用者請求之前，引導模型的輸出格式。
    examples: List[Dict[str, Any]] = []


class ChatRequest(BaseModel):
    message: str
    history: List[Dict[str, str]] = []
    max_new_tokens: int = 512
    temperature: float = 0.5


class DiagnoseRequest(BaseModel):
    context: Dict[str, Any]
    max_new_tokens: int = 320
    temperature: float = 0.2


class ClassifyRequest(BaseModel):
    message: str
    max_new_tokens: int = 120
    temperature: float = 0.0


CHAT_SYSTEM = (
    "You are ZeroTouch K8s Assistant, a concise Kubernetes and cloud infrastructure assistant. "
    "The user may be a junior engineer who is new to Kubernetes: answer in plain, simple language, "
    "avoid unnecessary jargon, and briefly explain any technical term you must use. "
    "You can answer normal casual conversation, explain Kubernetes concepts, and help troubleshoot. "
    "Do not output JSON unless the user asks for JSON. "
    "If a '[參考知識]' / reference section is included in the user's message, use it only as silent "
    "background context — never quote, list, or repeat the example commands from it. "
    "Answer only the user's actual request, in a few sentences. Keep answers short. "
    "LANGUAGE RULE: detect the language of the user's own message and reply in that exact language. "
    "If it is Chinese, reply ONLY in Traditional Chinese (Taiwan, zh-TW) — every character must be "
    "Traditional; never output a Simplified character (e.g. write 內存/檢查/優化/資源, not 内存/检查/优化/资源). "
    "If it is English, reply ONLY in English. Do not switch languages mid-reply."
)

_DIAGNOSE_ACTIONS = {
    "fix_image", "increase_memory", "check_dependencies", "fix_permissions",
    "create_config", "fix_probe", "fix_port_conflict", "analyze_logs", "manual_inspect",
}

DIAGNOSE_SYSTEM = (
    "You are a Kubernetes SRE expert. You are given a pod failure context (error reason, recent "
    "logs, events). Respond with ONLY a JSON object, no explanation, no markdown, no code fence.\n"
    '{"root_cause": "<one sentence, Traditional Chinese (zh-TW)>", '
    '"severity": "high|medium|low", '
    '"action": "<EXACTLY ONE of: fix_image, increase_memory, check_dependencies, fix_permissions, '
    'create_config, fix_probe, fix_port_conflict, analyze_logs, manual_inspect>", '
    '"suggestion": "<concrete fix steps, Traditional Chinese (zh-TW)>"}\n'
    "The 'action' value must be one of those exact English snake_case codes, nothing else."
)


# ── 意圖分類（Chat 分派用；規則比對不到時才呼叫）──────────────────
_INTENT_ACTIONS = {
    "deploy", "scale", "update_image", "rollback", "delete",
    "list_pods", "list_deployments", "healer_scan", "healer_fix",
    "healer_auto_fix", "gitops_log", "cluster_metrics", "qa",
}
# 每個 action 允許的 args（其餘一律丟棄，避免模型亂塞）
_INTENT_ARG_KEYS = {
    "deploy": {"app_name", "image", "pods", "port", "memory", "cpu"},
    "scale": {"name", "replicas"},
    "update_image": {"name", "image"},
    "rollback": {"name"},
    "delete": {"name"},
    "healer_fix": {"pod_name"},
}
_INTENT_INT_KEYS = {"pods", "port", "replicas"}
_INTENT_DESTRUCTIVE = {"scale", "update_image", "rollback", "delete",
                       "healer_fix", "healer_auto_fix"}

INTENT_SYSTEM = (
    "You are an intent router for a Kubernetes deployment platform. Given ONE user message, "
    "respond with ONLY a JSON object, no markdown, no code fence:\n"
    '{"action":"<code>","args":{...},"confidence":<0.0-1.0>}\n'
    "Action codes (choose EXACTLY one):\n"
    "  deploy            {app_name?, image?, pods?, port?, memory?}\n"
    "  scale             {name, replicas}\n"
    "  update_image      {name, image}\n"
    "  rollback          {name}\n"
    "  delete            {name}\n"
    "  list_pods         {}\n"
    "  list_deployments  {}\n"
    "  healer_scan       {}\n"
    "  healer_fix        {pod_name}\n"
    "  healer_auto_fix   {}\n"
    "  gitops_log        {}\n"
    "  cluster_metrics   {}\n"
    "  qa                {}   (questions, chit-chat, concept explanations, anything else)\n"
    "Rules: if the message only ASKS how to do something (no imperative command), use qa. "
    "If unsure between a destructive action and qa, choose qa with low confidence. "
    "Never invent a deployment name, pod name, or image that is not present in the message."
)

INTENT_FEWSHOT = [
    ("list pods", '{"action":"list_pods","args":{},"confidence":0.99}'),
    ("顯示所有部署", '{"action":"list_deployments","args":{},"confidence":0.98}'),
    ("scale web to 3", '{"action":"scale","args":{"name":"web","replicas":3},"confidence":0.98}'),
    ("幫我把 web-frontend 擴到 5 個", '{"action":"scale","args":{"name":"web-frontend","replicas":5},"confidence":0.95}'),
    ("deploy 3 nginx pods for shop port 80", '{"action":"deploy","args":{"pods":3,"image":"nginx:latest","app_name":"shop","port":80},"confidence":0.97}'),
    ("部署一個 redis 給 cache-service", '{"action":"deploy","args":{"image":"redis:latest","app_name":"cache-service"},"confidence":0.9}'),
    ("roll back the api deployment", '{"action":"rollback","args":{"name":"api"},"confidence":0.9}'),
    ("delete web-frontend", '{"action":"delete","args":{"name":"web-frontend"},"confidence":0.95}'),
    ("把 api-gateway 的 image 換成 node:20", '{"action":"update_image","args":{"name":"api-gateway","image":"node:20"},"confidence":0.92}'),
    ("how do I scale a deployment in kubernetes?", '{"action":"qa","args":{},"confidence":0.9}'),
    ("掃描壞掉的 pod", '{"action":"healer_scan","args":{},"confidence":0.9}'),
    ("叢集現在健康嗎", '{"action":"cluster_metrics","args":{},"confidence":0.7}'),
    ("看一下部署歷史", '{"action":"gitops_log","args":{},"confidence":0.9}'),
]


def _clean_intent(parsed: dict) -> dict:
    """把模型輸出正規化成 {action, args, confidence}，action 不在 enum 就退回 qa。"""
    action = str((parsed or {}).get("action", "")).strip()
    if action not in _INTENT_ACTIONS:
        return {"action": "qa", "args": {}, "confidence": 0.0}
    raw_args = (parsed or {}).get("args") or {}
    if not isinstance(raw_args, dict):
        raw_args = {}
    allowed = _INTENT_ARG_KEYS.get(action, set())
    args = {}
    for k, v in raw_args.items():
        if k not in allowed:
            continue
        if k in _INTENT_INT_KEYS:
            try:
                v = int(v)
            except (TypeError, ValueError):
                continue
        args[k] = v
    try:
        confidence = float((parsed or {}).get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    # 破壞性操作：信心不足或必要 arg 缺 → 交給呼叫端當 qa / clarify 處理
    if action in _INTENT_DESTRUCTIVE:
        required = _INTENT_ARG_KEYS.get(action, set())
        if confidence < 0.75 or not required.issubset(args.keys()):
            return {"action": "clarify", "args": {"guess": action, **args},
                    "confidence": confidence}
    return {"action": action, "args": args, "confidence": confidence}


# ── 模型載入 ─────────────────────────────────────────────────────
def _load():
    global _deploy_model, _deploy_tok, _monitor_model, _monitor_tok
    if _deploy_model is not None and _monitor_model is not None:
        return

    if _deploy_model is None:
        print(f"[Model Server] 載入部署模型：{BASE_MODEL}（{'GPU 4-bit' if _gpu_available else 'CPU'}）")
        torch.cuda.empty_cache() if _gpu_available else None
        _deploy_tok = AutoTokenizer.from_pretrained(BASE_MODEL, token=HF_TOKEN)
        if _deploy_tok.pad_token is None:
            _deploy_tok.pad_token = _deploy_tok.eos_token

        if _gpu_available:
            bnb_config = BitsAndBytesConfig(
                load_in_4bit=True,
                bnb_4bit_quant_type="nf4",
                bnb_4bit_compute_dtype=torch.float16,
            )
            _deploy_model = AutoModelForCausalLM.from_pretrained(
                BASE_MODEL, token=HF_TOKEN,
                quantization_config=bnb_config, device_map={"": 0},
            )
        else:
            _deploy_model = AutoModelForCausalLM.from_pretrained(
                BASE_MODEL, token=HF_TOKEN, torch_dtype=torch.float32,
                device_map="cpu", low_cpu_mem_usage=True,
            )

        if DEPLOY_ADAPTER_PATH and os.path.isdir(DEPLOY_ADAPTER_PATH):
            from peft import PeftModel
            print(f"[Model Server] 掛載部署 LoRA：{DEPLOY_ADAPTER_PATH}")
            _deploy_model = PeftModel.from_pretrained(_deploy_model, DEPLOY_ADAPTER_PATH)

        _deploy_model.config.use_cache = True
        _deploy_model.eval()

    if _monitor_model is None:
        print(f"[Model Server] 載入監控模型：{MONITOR_MODEL}（{MONITOR_DEVICE}）")
        _monitor_tok = AutoTokenizer.from_pretrained(MONITOR_MODEL, token=HF_TOKEN)
        if _monitor_tok.pad_token is None:
            _monitor_tok.pad_token = _monitor_tok.eos_token
        _monitor_model = AutoModelForCausalLM.from_pretrained(
            MONITOR_MODEL, token=HF_TOKEN,
            torch_dtype=torch.float32 if MONITOR_DEVICE == "cpu" else torch.float16,
            low_cpu_mem_usage=True,
        ).to(MONITOR_DEVICE)
        _monitor_model.config.use_cache = True
        _monitor_model.eval()

    print(f"[Model Server] 模型就緒，監聽 {MODEL_SERVER_HOST}:{MODEL_SERVER_PORT}")


# ── 共用生成 ─────────────────────────────────────────────────────
def _generate(model, tok, messages: List[Dict[str, str]], lock: threading.Lock,
              max_new_tokens: int, temperature: float) -> str:
    """以 chat template 組 prompt 並生成，回傳新產生的文字（不含 prompt）。"""
    input_ids = tok.apply_chat_template(
        messages, add_generation_prompt=True, return_tensors="pt",
    ).to(model.device)
    attention_mask = torch.ones_like(input_ids)
    input_len = input_ids.shape[1]

    gen_kwargs = dict(
        max_new_tokens=max_new_tokens,
        do_sample=temperature > 0,
        top_p=0.9,
        repetition_penalty=1.15,
        eos_token_id=tok.eos_token_id,
        pad_token_id=tok.pad_token_id or tok.eos_token_id,
    )
    if temperature > 0:
        gen_kwargs["temperature"] = temperature

    with lock, torch.no_grad():
        out = model.generate(input_ids=input_ids, attention_mask=attention_mask, **gen_kwargs)
    return tok.decode(out[0][input_len:], skip_special_tokens=True).strip()


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


def _deploy_messages(prompt: str, examples: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for ex in examples[:4]:
        ex_in = str(ex.get("input", "")).strip()
        ex_out = ex.get("output")
        if not ex_in or ex_out is None:
            continue
        if not isinstance(ex_out, str):
            ex_out = json.dumps(ex_out, ensure_ascii=False)
        messages.append({"role": "user", "content": ex_in})
        messages.append({"role": "assistant", "content": ex_out})
    messages.append({"role": "user", "content": prompt})
    return messages


# ── API 端點 ─────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {
        "status": "ok",
        "deploy_loaded": _deploy_model is not None,
        "monitor_loaded": _monitor_model is not None,
        # 向後相容舊呼叫端（web_demo / llama_client 早期只看 model_loaded）
        "model_loaded": _deploy_model is not None,
    }


@app.get("/unload")
def unload():
    global _deploy_model, _deploy_tok, _monitor_model, _monitor_tok
    _deploy_model = _deploy_tok = _monitor_model = _monitor_tok = None
    if _gpu_available:
        torch.cuda.empty_cache()
    return {"status": "unloaded"}


@app.post("/infer")
def infer(req: InferRequest):
    if _deploy_model is None:
        raise HTTPException(status_code=503, detail="部署模型尚未載入")

    messages = _deploy_messages(req.prompt, req.examples)
    generated = _generate(_deploy_model, _deploy_tok, messages, _deploy_lock,
                          req.max_new_tokens, req.temperature)

    result = _parse_output(generated)
    if result and _validate(result):
        result.setdefault("image", "nginx:latest")
        result.setdefault("app_name", "auto-app")
        img = str(result["image"]).strip()
        if img and ":" not in img and "/" not in img:
            result["image"] = f"{img}:latest"
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
        return {"result": result}

    return {"result": {"error": "解析失敗", "raw": generated[:200]}}


@app.post("/chat")
def chat(req: ChatRequest):
    if _deploy_model is None:
        raise HTTPException(status_code=503, detail="部署模型尚未載入")

    messages = [{"role": "system", "content": CHAT_SYSTEM}]
    for item in (req.history or [])[-8:]:
        role = "user" if item.get("role") == "user" else "assistant"
        content = str(item.get("content", ""))[:1200]
        if content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": req.message})

    reply = _generate(_deploy_model, _deploy_tok, messages, _deploy_lock,
                      req.max_new_tokens, req.temperature)
    return {"reply": _to_tw(reply) or "我沒有產生有效回覆，請再問一次。"}


@app.post("/diagnose")
def diagnose(req: DiagnoseRequest):
    if _monitor_model is None:
        raise HTTPException(status_code=503, detail="監控模型尚未載入")

    ctx = req.context or {}
    events = ctx.get("events", []) or []
    event_str = "; ".join(
        f"{e.get('reason', '?')}: {str(e.get('message', ''))[:120]}" for e in events[:5]
    )
    user_content = (
        f"Pod: {ctx.get('pod_name', 'unknown')}\n"
        f"Container: {ctx.get('container', '')}\n"
        f"Error State: {ctx.get('reason', 'Unknown')}\n"
        f"Restart Count: {ctx.get('restart_count', 0)}\n"
        f"Recent Logs:\n{str(ctx.get('logs', ''))[:800]}\n"
        f"Events: {event_str}"
    )
    messages = [
        {"role": "system", "content": DIAGNOSE_SYSTEM},
        {"role": "user", "content": user_content},
    ]

    generated = _generate(_monitor_model, _monitor_tok, messages, _monitor_lock,
                          req.max_new_tokens, req.temperature)
    parsed = _parse_output(generated) or {}
    action = str(parsed.get("action", "")).strip()
    if action not in _DIAGNOSE_ACTIONS:
        action = "manual_inspect"
    severity = str(parsed.get("severity", "")).strip().lower()
    if severity not in ("high", "medium", "low"):
        severity = "unknown"
    return {
        "diagnosis": {
            "root_cause": _to_tw(parsed.get("root_cause", "")),
            "severity": severity,
            "action": action,
            "suggestion": _to_tw(parsed.get("suggestion", "")),
            "raw": generated[:400],
        }
    }


@app.post("/classify")
def classify(req: ClassifyRequest):
    """把一句使用者訊息分類成 {action, args, confidence}，給 Chat 分派用。
    規則比對不到才會打到這裡；用部署模型（3B），嚴格 JSON、固定 enum。"""
    if _deploy_model is None:
        raise HTTPException(status_code=503, detail="部署模型尚未載入")

    messages = [{"role": "system", "content": INTENT_SYSTEM}]
    for ex_in, ex_out in INTENT_FEWSHOT:
        messages.append({"role": "user", "content": ex_in})
        messages.append({"role": "assistant", "content": ex_out})
    messages.append({"role": "user", "content": str(req.message)[:600]})

    generated = _generate(_deploy_model, _deploy_tok, messages, _deploy_lock,
                          req.max_new_tokens, req.temperature)
    parsed = _parse_output(generated) or {}
    result = _clean_intent(parsed)
    result["raw"] = generated[:200]
    return {"result": result}


if __name__ == "__main__":
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as _s:
        if _s.connect_ex((MODEL_SERVER_HOST, MODEL_SERVER_PORT)) == 0:
            print(f"[Model Server] Port {MODEL_SERVER_PORT} 已被占用，server 已在執行中，直接退出。")
            sys.exit(0)
    uvicorn.run(app, host=MODEL_SERVER_HOST, port=MODEL_SERVER_PORT, log_level="warning")
