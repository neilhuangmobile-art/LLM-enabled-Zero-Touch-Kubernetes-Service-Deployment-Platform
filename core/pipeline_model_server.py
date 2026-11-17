"""多代理人流程的本地模型伺服器（2026-11-16）：Qwen3.5-4B 4-bit，只聽 127.0.0.1:8766。

要用 C:\\zt-k8s\\.venv-qwen35 執行（需要 transformers 5.x；專案 venv 是 4.57.6，載不了 Qwen3.5）：
  set HF_HUB_OFFLINE=1
  C:\\zt-k8s\\.venv-qwen35\\Scripts\\python.exe core\\pipeline_model_server.py

只用標準函式庫的 http.server（測試 venv 沒有 FastAPI）。一次只跑一個生成（鎖），
現有 8765 的模型伺服器（3B、1.5B）不受影響。

GET  /health   → {"loaded": bool, "model": ..., "vram_gb": ...}
POST /generate {"system": str, "user": str, "max_new_tokens": int} → {"text": str, "ms": int}
"""
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL_ID = os.environ.get("PIPELINE_MODEL_ID", "Qwen/Qwen3.5-4B")
HOST, PORT = "127.0.0.1", int(os.environ.get("PIPELINE_MODEL_PORT", "8766"))
MAX_BODY = 64 * 1024            # 請求上限：原句加 JSON 不會超過這個大小
MAX_NEW_TOKENS = 1024

_state = {"model": None, "tok": None, "error": None}
_lock = threading.Lock()


def _load():
    try:
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
        q = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4", bnb_4bit_compute_dtype=torch.bfloat16)
        tok = AutoTokenizer.from_pretrained(MODEL_ID)
        model = AutoModelForCausalLM.from_pretrained(MODEL_ID, device_map="cuda:0", quantization_config=q).eval()
        _state.update(model=model, tok=tok)
        _generate("You reply with JSON.", "{}", 8)        # 第一次呼叫要編譯 kernel（約 48 秒），啟動時先做掉
        print(f"[pipeline-model] {MODEL_ID} 4-bit 載入完成，{torch.cuda.memory_allocated() / 1024 ** 3:.2f} GB", flush=True)
    except Exception as e:
        _state["error"] = str(e)
        print(f"[pipeline-model] 載入失敗：{e}", file=sys.stderr, flush=True)


def _generate(system: str, user: str, max_new_tokens: int) -> str:
    import torch
    tok, model = _state["tok"], _state["model"]
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    prompt = tok.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True, enable_thinking=False)
    ids = tok(prompt, return_tensors="pt").to(model.device)
    with _lock, torch.no_grad():
        out = model.generate(**ids, max_new_tokens=max_new_tokens, do_sample=False,
                             pad_token_id=tok.pad_token_id or tok.eos_token_id)
    return tok.decode(out[0][ids["input_ids"].shape[1]:], skip_special_tokens=True)


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path != "/health":
            return self._send(404, {"error": "not found"})
        vram = None
        try:
            import torch
            vram = round(torch.cuda.memory_allocated() / 1024 ** 3, 2)
        except Exception:
            pass
        self._send(200, {"loaded": _state["model"] is not None, "model": MODEL_ID, "error": _state["error"], "vram_gb": vram})

    def do_POST(self):
        if self.path != "/generate":
            return self._send(404, {"error": "not found"})
        if _state["model"] is None:
            return self._send(503, {"error": "model not loaded"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > MAX_BODY:
                return self._send(413, {"error": "body too large or empty"})
            req = json.loads(self.rfile.read(n).decode("utf-8"))
            system, user = str(req.get("system") or ""), str(req.get("user") or "")
            mnt = max(1, min(int(req.get("max_new_tokens") or 768), MAX_NEW_TOKENS))
        except (ValueError, TypeError):
            return self._send(400, {"error": "bad request"})
        t0 = time.time()
        try:
            text = _generate(system, user, mnt)
        except Exception as e:
            return self._send(500, {"error": str(e)[:300]})
        self._send(200, {"text": text, "ms": int((time.time() - t0) * 1000)})

    def log_message(self, fmt, *args):     # 不把請求內容（使用者原句）寫進紀錄
        pass


if __name__ == "__main__":
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    threading.Thread(target=_load, daemon=True).start()
    print(f"[pipeline-model] 聽 http://{HOST}:{PORT}（載入中）", flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
