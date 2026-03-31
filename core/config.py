"""
core/config.py
統一路徑與設定，所有模組都從這裡引用，不再各自硬編碼。
"""
import os

# ── 專案根目錄（無論從哪裡執行都能正確解析）──────────────────────
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ── 模型路徑 ────────────────────────────────────────────────────
BASE_MODEL   = "meta-llama/Llama-3.1-8B-Instruct"
ADAPTER_PATH = os.path.join(ROOT, "llama3_k8s_lora_results")

# ── 資料集 ───────────────────────────────────────────────────────
DATASET_DIR  = os.path.join(ROOT, "dataset")
DATASET_PATH = os.path.join(DATASET_DIR, "finetune_samples.jsonl")

# ── YAML 輸出 ────────────────────────────────────────────────────
YAML_DIR     = os.path.join(ROOT, "yamls", "deployments")

# ── 報告輸出 ─────────────────────────────────────────────────────
REPORTS_DIR          = os.path.join(ROOT, "reports")
EVAL_REPORT          = os.path.join(REPORTS_DIR, "eval_report.json")
EVAL_BASELINE_REPORT = os.path.join(REPORTS_DIR, "eval_baseline_report.json")
EVAL_HARD_REPORT     = os.path.join(REPORTS_DIR, "eval_hard_report.json")
EVAL_SPEED_REPORT    = os.path.join(REPORTS_DIR, "eval_speed_report.json")

# ── Model Server ─────────────────────────────────────────────────
MODEL_SERVER_HOST = "127.0.0.1"
MODEL_SERVER_PORT = 8765
MODEL_SERVER_URL  = f"http://{MODEL_SERVER_HOST}:{MODEL_SERVER_PORT}"

# ── 系統 Prompt（唯一定義來源）───────────────────────────────────
SYSTEM_PROMPT = (
    "You are an AI that converts Kubernetes deployment requests into JSON.\n"
    "ONLY output a valid JSON object. No explanation, no markdown, no extra text.\n"
    "Required fields: pods (integer), image (string), app_name (string)\n"
    "Optional fields: port (integer), memory (string, e.g. 256Mi)\n"
    'Example: {"pods": 3, "image": "nginx:latest", "app_name": "web-frontend", "port": 80}'
)

# ── 確保目錄存在（import 時自動建立）────────────────────────────
for _d in [DATASET_DIR, YAML_DIR, REPORTS_DIR]:
    os.makedirs(_d, exist_ok=True)


def ensure_utf8_output():
    """
    確保 stdout/stderr 以 UTF-8 輸出，解決 Windows cp950 無法顯示
    中文符號（✓ ✗ ⚠ 等）的問題。在所有 CLI 入口點呼叫一次即可。
    """
    import sys
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name)
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
