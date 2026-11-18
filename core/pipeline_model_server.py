"""多代理人流程的本地模型伺服器（2026-11-16）：Qwen3.5-4B，只聽 127.0.0.1:8766。

精度（2026-11-18 起兩種顯卡都能跑）：環境變數 PIPELINE_PRECISION=auto|4bit|bf16|fp16。
auto＝NVIDIA 且裝了 bitsandbytes 用 4-bit（約 3GB 顯存），否則（AMD／ROCm、沒有 bitsandbytes）用 bf16（約 9GB）。
載入前先看剩餘顯存，不夠就不載入，/health 回傳原因與解法，網站照原本規則退回舊解析並告知使用者。

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

_state = {"model": None, "tok": None, "error": None, "precision": None}
_lock = threading.Lock()


# Qwen3.5-4B 權重約 4.2B 參數：bf16 約 8.5GB、4-bit 約 3GB，另外留生成用的空間
NEED_GB = {"4bit": 4.0, "bf16": 10.0, "fp16": 10.0}


def plan_precision(want: str, backend, has_bnb: bool, bf16_ok: bool = True) -> tuple:
    """回傳 (精度, 說明)。純函式，測試不需要 torch。"""
    want = (want or "auto").strip().lower()
    half = "bf16" if bf16_ok else "fp16"
    can_4bit = backend == "cuda" and has_bnb
    if want in ("bf16", "fp16"):
        return (want if (want == "fp16" or bf16_ok) else "fp16"), "依 PIPELINE_PRECISION"
    if want == "4bit" and not can_4bit:
        return half, "要求 4bit，但這台不支援 bitsandbytes（AMD 或沒有安裝），改用 " + half
    if want == "4bit" or (want == "auto" and can_4bit):
        return "4bit", "NVIDIA 且有 bitsandbytes"
    return half, ("AMD（ROCm）" if backend == "rocm" else "沒有 bitsandbytes") + "，用 " + half


def _load():
    try:
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer
        if not torch.cuda.is_available():
            raise RuntimeError("沒有偵測到 GPU：多代理人流程的模型需要顯示卡（NVIDIA 或 AMD）")
        backend = "rocm" if getattr(torch.version, "hip", None) else "cuda"
        try:
            import bitsandbytes  # noqa: F401
            has_bnb = backend == "cuda"
        except Exception:
            has_bnb = False
        prec, why = plan_precision(os.environ.get("PIPELINE_PRECISION", "auto"), backend, has_bnb,
                                   bool(torch.cuda.is_bf16_supported()))
        free = torch.cuda.mem_get_info()[0] / 1024 ** 3
        print(f"[pipeline-model] {torch.cuda.get_device_name(0)}（{backend}），{why}；剩餘顯存 {free:.1f} GB", flush=True)
        if free < NEED_GB[prec]:
            raise RuntimeError(
                f"顯存不夠：{prec} 需要約 {NEED_GB[prec]:.0f} GB，目前只剩 {free:.1f} GB。"
                + ("AMD 顯卡不能用 4-bit，請先關掉主模型伺服器（8765），或在 .env 設 MONITOR_DEVICE=cpu 讓監控模型改用 CPU 後重開 8765，再啟動這個伺服器。"
                   if prec != "4bit" else "請先關掉其他使用顯卡的程式。"))
        tok = AutoTokenizer.from_pretrained(MODEL_ID)
        if prec == "4bit":
            from transformers import BitsAndBytesConfig
            q = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4", bnb_4bit_compute_dtype=torch.bfloat16)
            model = AutoModelForCausalLM.from_pretrained(MODEL_ID, device_map="cuda:0", quantization_config=q).eval()
        else:
            dtype = torch.bfloat16 if prec == "bf16" else torch.float16
            model = AutoModelForCausalLM.from_pretrained(MODEL_ID, device_map="cuda:0", dtype=dtype).eval()
        _state.update(model=model, tok=tok, precision=prec)
        _generate("You reply with JSON.", "{}", 8)        # 第一次呼叫要編譯 kernel（約 48 秒），啟動時先做掉
        print(f"[pipeline-model] {MODEL_ID} {prec} 載入完成，{torch.cuda.memory_allocated() / 1024 ** 3:.2f} GB", flush=True)
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
        self._send(200, {"loaded": _state["model"] is not None, "model": MODEL_ID, "error": _state["error"], "vram_gb": vram,
                          "precision": _state["precision"]})

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
    # 讀專案 .env（HF_HOME 等）；core/config 只 import os，要在 transformers 之前（見 CLAUDE.md 的 import 順序陷阱）
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import core.config  # noqa: F401,E402
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    threading.Thread(target=_load, daemon=True).start()
    print(f"[pipeline-model] 聽 http://{HOST}:{PORT}（載入中）", flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
