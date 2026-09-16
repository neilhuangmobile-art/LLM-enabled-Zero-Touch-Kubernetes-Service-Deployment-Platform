"""
core/config.py
統一路徑與設定，所有模組都從這裡引用，不再各自硬編碼。
"""
import os

# ── 專案根目錄（無論從哪裡執行都能正確解析）──────────────────────
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_dotenv(path: str = os.path.join(ROOT, ".env")) -> None:
    """Minimal .env loader to avoid requiring python-dotenv at runtime."""
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


_load_dotenv()

# Hugging Face uses HUGGINGFACE_HUB_TOKEN/HF_TOKEN depending on library version.
HF_TOKEN = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_HUB_TOKEN")
if HF_TOKEN:
    os.environ.setdefault("HF_TOKEN", HF_TOKEN)
    os.environ.setdefault("HUGGINGFACE_HUB_TOKEN", HF_TOKEN)

# ── 模型路徑 ────────────────────────────────────────────────────
# 2026-09-06：本機只有 6GB VRAM，跑不動 Llama-3.1-8B。改用小模型雙軌：
#   - 部署（/infer、/chat）：Qwen2.5-3B-Instruct，4-bit 量化跑 GPU（約 2.2GB）
#   - 監控（/diagnose）：Qwen2.5-1.5B-Instruct，跑 CPU，顯卡全留給部署模型
# 8B LoRA（llama3_k8s_lora_results）架構維度對不上 Qwen，直接棄用；部署改走
# 「Gemini 正規化 → RAG few-shot 範例 → 3B 生成」，不再微調。
BASE_MODEL     = os.environ.get("BASE_MODEL", "Qwen/Qwen2.5-3B-Instruct")
MONITOR_MODEL  = os.environ.get("MONITOR_MODEL", "Qwen/Qwen2.5-1.5B-Instruct")
MONITOR_DEVICE = os.environ.get("MONITOR_DEVICE", "cpu")

# 部署模型的 LoRA adapter；預設空字串 = 不掛 adapter（純 base + RAG few-shot）。
# 之後若要在 Qwen2.5-3B 上重訓小 LoRA，把權重路徑填進 DEPLOY_ADAPTER_PATH 即可。
DEPLOY_ADAPTER_PATH = os.environ.get("DEPLOY_ADAPTER_PATH", "")

# 舊名稱保留：仍有 eval_*.py 等離線工具引用 ADAPTER_PATH（那些工具還在跑 8B，不在主流程）。
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
MODEL_SERVER_HOST = os.environ.get("MODEL_SERVER_HOST", "127.0.0.1")
MODEL_SERVER_PORT = int(os.environ.get("MODEL_SERVER_PORT", "8765"))
MODEL_SERVER_URL  = f"http://{MODEL_SERVER_HOST}:{MODEL_SERVER_PORT}"

# ── 系統 Prompt（唯一定義來源）───────────────────────────────────
SYSTEM_PROMPT = (
    "You are an AI that converts Kubernetes deployment requests into JSON.\n"
    "ONLY output a valid JSON object. No explanation, no markdown, no extra text.\n"
    "Required fields: pods (integer), image (string), app_name (string)\n"
    "Optional fields: port (integer), memory (string, e.g. 256Mi), cpu (string, e.g. 500m or 2)\n"
    "If example input/output pairs are provided, follow their exact field names and JSON shape.\n"
    "Only include a field when the request actually states it; never invent values the user did not give.\n"
    "If the request states per-node capacity (e.g. \"each node has 4 cpu and 8Gi memory\"), ALSO include: "
    "total_cpu (string, cpu per pod * pods), total_memory (string, memory per pod * pods), "
    "cpu_bound_nodes (integer, ceil(total_cpu / node cpu capacity)), "
    "memory_bound_nodes (integer, ceil(total_memory / node memory capacity)), "
    "node_count (integer, max(cpu_bound_nodes, memory_bound_nodes)) — compute these step by step in that order.\n"
    'Example: {"pods": 3, "image": "nginx:latest", "app_name": "web-frontend", "port": 80, "cpu": "500m"}'
)

# ── Node 容量（用來估算部署需要幾個 node，非量測值，可依實際叢集調整）──
NODE_CAPACITY = {"cpu": "4", "memory": "8Gi"}

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
