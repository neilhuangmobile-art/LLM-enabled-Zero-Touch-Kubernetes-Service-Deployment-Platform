"""
enrich_dataset.py
=================
將三個原始資料集補上所有欄位後輸出到 dataset/ 目錄。

欄位補充策略：
  is_k8s      → 規則（trash_talk = False，其他 = True）
  language    → 規則（zh_tw 檔 = zh-tw，EN 檔 = en）
  complexity  → 規則（prompt 長度 + category 關鍵字）
  namespace   → 規則（從 prompt 文字抽取，抓不到給 default）
  output      → model server（POST /infer，Qwen2.5-3B）

使用方式（在伺服器上執行）：
  python enrich_dataset.py [--dry-run] [--limit 100] [--skip-output]

  --dry-run     只跑前 5 筆，印出結果，不寫檔
  --limit N     只處理前 N 筆（測試用）
  --skip-output 跳過 LLaMA 呼叫，output 欄位填 null（離線時用）
"""

import json
import re
import sys
import os
import time
import argparse
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path
from typing import Optional

# ── 路徑設定 ──────────────────────────────────────────────────────
BASE_DIR    = Path(__file__).parent
DATASET_DIR = BASE_DIR / "dataset"
DATASET_DIR.mkdir(exist_ok=True)

EN_K8S_FILE   = BASE_DIR / "k8s_prompts_20000.jsonl"          # 放在同目錄
EN_TRASH_FILE = BASE_DIR / "trash_talk_prompts_5000.jsonl"
ZH_K8S_FILE   = BASE_DIR / "k8s_prompts_20000_zh_tw.jsonl"

OUT_EN_K8S    = DATASET_DIR / "k8s_en_enriched.jsonl"
OUT_EN_TRASH  = DATASET_DIR / "k8s_trash_enriched.jsonl"
OUT_ZH_K8S    = DATASET_DIR / "k8s_zh_enriched.jsonl"
OUT_COMBINED  = DATASET_DIR / "k8s_all_enriched.jsonl"

MODEL_SERVER  = "http://127.0.0.1:8765"

# ── 複雜度判斷規則 ─────────────────────────────────────────────────
COMPLEX_KEYWORDS = [
    "helm", "ingress", "tls", "rbac", "statefulset", "pvc",
    "persistent", "autoscal", "hpa", "vpa", "keda", "multi",
    "secret", "configmap", "affinity", "toleration", "initcontainer",
    "sidecar", "istio", "service mesh", "cronjob", "batch",
    "多應用", "多個應用", "blue.green", "藍綠", "canary",
]
SIMPLE_KEYWORDS = [
    "deploy", "run", "start", "create", "launch",
    "部署", "建立", "啟動", "跑",
]

def infer_complexity(prompt: str, category: str = "") -> str:
    p = prompt.lower()
    cat = category.lower()
    score = 0
    for kw in COMPLEX_KEYWORDS:
        if kw in p or kw in cat:
            score += 1
    if len(prompt) > 600:
        score += 2
    elif len(prompt) > 300:
        score += 1
    if score >= 3:
        return "complex"
    elif score >= 1:
        return "medium"
    return "simple"

# ── Namespace 抽取 ─────────────────────────────────────────────────
NS_PATTERN = re.compile(
    r"namespace\s*[:\s=「」\"'【】]*\s*([a-zA-Z0-9\-_]+)", re.IGNORECASE
)
ZH_NS_MAP = {
    "預設": "default", "正式": "production", "prod": "production",
    "dev": "dev", "staging": "staging", "開發": "dev",
    "測試": "staging", "監控": "monitoring",
}

def extract_namespace(prompt: str) -> str:
    m = NS_PATTERN.search(prompt)
    if m:
        ns = m.group(1).strip().lower()
        return ZH_NS_MAP.get(ns, ns)
    for zh, en in ZH_NS_MAP.items():
        if zh in prompt:
            return en
    return "default"

# ── LLaMA 呼叫 ────────────────────────────────────────────────────
SYSTEM_PROMPT = (
    "You are an AI that extracts Kubernetes deployment parameters from a request.\n"
    "Output ONLY a valid JSON object. No explanation, no markdown.\n"
    "Required: app_name (string), image (string), pods (integer 1-50)\n"
    "Optional: port (integer), memory (string e.g. 512Mi or 4Gi), namespace (string)\n"
    'If you cannot determine a value, use: image="nginx:latest", pods=1, app_name="auto-app"\n'
    'Example: {"app_name":"web","image":"nginx:latest","pods":3,"port":80,"memory":"512Mi","namespace":"default"}'
)

def call_llama(prompt_text: str, timeout: int = 60) -> Optional[dict]:
    """呼叫 model server 的 /infer，回傳解析好的 output dict，失敗回傳 None。

    2026-09-07：換 Qwen 雙軌後 model server 沒有 /generate，改打 /infer。
    /infer 直接回傳「已解析、已驗證」的 dict（{result: {...}}），不需要再自己抽 JSON。
    """
    body = json.dumps({"prompt": prompt_text[:500]}).encode("utf-8")
    req  = urllib.request.Request(
        f"{MODEL_SERVER}/infer",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        result = data.get("result")
        if isinstance(result, dict) and "error" not in result:
            return _validate_output(result)
        # 舊格式相容 / 意外回了純文字
        raw = result if isinstance(result, str) else data.get("response", data.get("text", ""))
        return _parse_output(raw) if raw else None
    except Exception as e:
        print(f"  [infer ERR] {e}", file=sys.stderr)
        return None

def _parse_output(raw: str) -> Optional[dict]:
    """從 LLaMA 原始回應中抽取 JSON"""
    # 嘗試直接 parse
    for attempt in [raw, raw.strip()]:
        try:
            obj = json.loads(attempt)
            return _validate_output(obj)
        except Exception:
            pass
    # 找第一個 { ... }
    m = re.search(r"\{[^{}]+\}", raw, re.DOTALL)
    if m:
        try:
            obj = json.loads(m.group())
            return _validate_output(obj)
        except Exception:
            pass
    # regex fallback
    pods_m  = re.search(r'"(?:pods|replicas)"\s*:\s*(\d+)', raw)
    image_m = re.search(r'"image"\s*:\s*"([^"]+)"', raw)
    app_m   = re.search(r'"app_name"\s*:\s*"([^"]+)"', raw)
    port_m  = re.search(r'"port"\s*:\s*(\d+)', raw)
    mem_m   = re.search(r'"memory"\s*:\s*"([^"]+)"', raw)
    ns_m    = re.search(r'"namespace"\s*:\s*"([^"]+)"', raw)
    if pods_m or image_m or app_m:
        result: dict = {
            "app_name":  app_m.group(1)  if app_m   else "auto-app",
            "image":     image_m.group(1) if image_m else "nginx:latest",
            "pods":      int(pods_m.group(1)) if pods_m else 1,
        }
        if port_m: result["port"] = int(port_m.group(1))
        if mem_m:  result["memory"] = mem_m.group(1)
        if ns_m:   result["namespace"] = ns_m.group(1)
        return result
    return None

def _validate_output(obj: dict) -> Optional[dict]:
    """確保必要欄位存在，修正型別"""
    if not isinstance(obj, dict):
        return None
    result = {
        "app_name":  str(obj.get("app_name", "auto-app")),
        "image":     str(obj.get("image", "nginx:latest")),
        "pods":      max(1, min(100, int(obj.get("pods", obj.get("replicas", 1))))),
    }
    if "port" in obj:
        try: result["port"] = int(obj["port"])
        except: pass
    if "memory" in obj:
        result["memory"] = str(obj["memory"])
    if "namespace" in obj:
        result["namespace"] = str(obj["namespace"])
    return result

# ── 主要處理函式 ──────────────────────────────────────────────────

def enrich_record(record: dict, is_k8s: bool, language: str, skip_output: bool) -> dict:
    """補全所有欄位，回傳新的 record"""
    # 取得 prompt 文字
    prompt = record.get("prompt") or record.get("prompt_zh_tw", "")
    category = record.get("category", "")

    # ── 規則欄位 ──
    enriched = {
        **record,
        "is_k8s":     is_k8s,
        "language":   language,
        "complexity": infer_complexity(prompt, category) if is_k8s else "n/a",
        "namespace":  extract_namespace(prompt) if is_k8s else None,
    }

    # ── output 欄位 ──
    if not is_k8s:
        # trash talk → output 固定為 null，is_k8s=False 就是負樣本
        enriched["output"] = None
    elif skip_output:
        enriched["output"] = None
    else:
        out = call_llama(prompt)
        enriched["output"] = out  # 可能是 dict 或 None

    return enriched


def process_file(
    input_path: Path,
    output_path: Path,
    is_k8s: bool,
    language: str,
    skip_output: bool,
    limit: Optional[int],
    dry_run: bool,
):
    if not input_path.exists():
        print(f"[SKIP] 找不到檔案：{input_path}")
        return 0, 0

    print(f"\n{'='*60}")
    print(f"處理：{input_path.name}")
    print(f"  is_k8s={is_k8s}  language={language}  limit={limit}")
    print(f"{'='*60}")

    success = 0
    fail    = 0
    lines   = []

    with open(input_path, "r", encoding="utf-8") as f:
        for i, line in enumerate(f):
            if limit and i >= limit:
                break
            line = line.strip()
            if not line:
                continue

            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                print(f"  [WARN] 第 {i+1} 行 JSON 解析失敗，跳過")
                fail += 1
                continue

            enriched = enrich_record(record, is_k8s, language, skip_output)

            if dry_run:
                print(f"\n--- 第 {i+1} 筆 ---")
                print(json.dumps(enriched, ensure_ascii=False, indent=2)[:600])
                if i >= 4:
                    print("(dry-run 只顯示前 5 筆)")
                    break
                continue

            lines.append(json.dumps(enriched, ensure_ascii=False))

            # 進度顯示
            if (i + 1) % 100 == 0:
                output_ok = sum(1 for l in lines if json.loads(l).get("output") is not None)
                print(f"  [{i+1:5d}] output 成功率 {output_ok}/{len(lines)} "
                      f"({output_ok/len(lines)*100:.1f}%)")

            success += 1

    if dry_run:
        return 0, 0

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print(f"\n  完成：{success} 筆 | 失敗：{fail} 筆 → {output_path.name}")
    return success, fail


def merge_outputs():
    """合併三個輸出檔成 combined"""
    files = [OUT_EN_K8S, OUT_EN_TRASH, OUT_ZH_K8S]
    total = 0
    with open(OUT_COMBINED, "w", encoding="utf-8") as out:
        for f in files:
            if not f.exists():
                continue
            with open(f, encoding="utf-8") as inp:
                for line in inp:
                    if line.strip():
                        out.write(line)
                        total += 1
    print(f"\n合併完成：{total} 筆 → {OUT_COMBINED.name}")


# ── CLI ──────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Enrich K8s dataset with all fields")
    parser.add_argument("--dry-run",     action="store_true", help="只顯示前5筆，不寫檔")
    parser.add_argument("--limit",       type=int, default=None, help="每個檔案最多處理幾筆")
    parser.add_argument("--skip-output", action="store_true", help="跳過 LLaMA，output=null")
    parser.add_argument("--file",        choices=["en", "trash", "zh", "all"], default="all",
                        help="只處理特定檔案（預設 all）")
    args = parser.parse_args()

    if args.dry_run:
        args.limit = 5

    # 確認 model server 是否在線
    if not args.skip_output and not args.dry_run:
        try:
            urllib.request.urlopen(f"{MODEL_SERVER}/health", timeout=3)
            print(f"[OK] Model server 在線：{MODEL_SERVER}")
        except Exception:
            print(f"[WARN] Model server 不在線（{MODEL_SERVER}），output 將為 null")
            print("       加上 --skip-output 跳過 LLaMA 呼叫，或先啟動 model server")
            args.skip_output = True

    t_start = time.time()
    total_ok = 0

    if args.file in ("en", "all"):
        ok, _ = process_file(
            EN_K8S_FILE, OUT_EN_K8S,
            is_k8s=True, language="en",
            skip_output=args.skip_output,
            limit=args.limit, dry_run=args.dry_run,
        )
        total_ok += ok

    if args.file in ("trash", "all"):
        ok, _ = process_file(
            EN_TRASH_FILE, OUT_EN_TRASH,
            is_k8s=False, language="en",
            skip_output=True,          # trash 不需要 LLaMA
            limit=args.limit, dry_run=args.dry_run,
        )
        total_ok += ok

    if args.file in ("zh", "all"):
        ok, _ = process_file(
            ZH_K8S_FILE, OUT_ZH_K8S,
            is_k8s=True, language="zh-tw",
            skip_output=args.skip_output,
            limit=args.limit, dry_run=args.dry_run,
        )
        total_ok += ok

    if not args.dry_run and args.file == "all":
        merge_outputs()

    elapsed = time.time() - t_start
    print(f"\n總耗時：{elapsed:.1f}s | 總筆數：{total_ok}")


if __name__ == "__main__":
    main()
