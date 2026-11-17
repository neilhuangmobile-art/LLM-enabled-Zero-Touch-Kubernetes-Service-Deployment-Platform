"""2026-11-11 候選模型實測：同一份 prompt（prompt.py）跑 testset.jsonl，評分後寫進 results/。

用法：
  # 本地模型（要用 C:\\zt-k8s\\.venv-qwen35，transformers 5.x）
  python experiments/model_select/run_eval.py --backend hf --model Qwen/Qwen3.5-4B --quant bf16
  python experiments/model_select/run_eval.py --backend hf --model Qwen/Qwen3.5-9B --quant 4bit
  # Gemini（用專案原本的 venv，會照每分鐘 5 次的限制放慢）
  python experiments/model_select/run_eval.py --backend gemini
  # 只重新評分已經存在的結果
  python experiments/model_select/run_eval.py --rescore results/<檔名>.jsonl
"""
import argparse
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from prompt import NORMALIZE_SYSTEM  # noqa: E402

TESTSET = os.path.join(HERE, "testset.jsonl")
RESULTS = os.path.join(HERE, "results")

_ALIASES = {"mongodb": "mongo", "postgresql": "postgres", "postgre": "postgres"}


def load_tests():
    with open(TESTSET, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def extract_json(text):
    """取第一個完整的 {...}（略過思考區塊）。"""
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    start = text.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            c = text[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)
    return None


# ── 評分 ─────────────────────────────────────────────
def _norm_image(v):
    s = str(v or "").strip().lower()
    for p in ("docker.io/library/", "docker.io/", "library/"):
        if s.startswith(p):
            s = s[len(p):]
    repo, tag = (s.rsplit(":", 1) + [""])[:2] if ":" in s.split("/")[-1] else (s, "")
    repo = repo.split("/")[-1]
    return _ALIASES.get(repo, repo), tag


def _image_ok(gold, pred):
    if not pred:
        return False
    g_repo, g_tag = _norm_image(gold)
    p_repo, p_tag = _norm_image(pred)
    if g_repo != p_repo:
        return False
    if not g_tag:
        return p_tag in ("", "latest", "management")
    return p_tag == g_tag


def _int(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _mem(v):
    s = str(v or "").strip().replace(" ", "")
    m = re.fullmatch(r"(\d+)(Mi|Gi|M|G|MB|GB)", s, re.I)
    if not m:
        return s
    n, u = m.groups()
    return n + ("Gi" if u.lower().startswith("g") else "Mi")


def _empty(v):
    return v is None or (isinstance(v, str) and v.strip().lower() in ("", "null", "none"))


def score_one(test, parsed):
    gold_base, gold_extras = test["gold"]["base"], test["gold"]["extras"]
    out = {"json_ok": isinstance(parsed, dict), "base_hit": 0, "base_total": 0, "base_invented": 0,
           "extra_hit": 0, "extra_total": len(gold_extras), "extra_missed": [], "extra_unexpected": 0,
           "evidence_ok": 0, "evidence_total": 0, "errors": []}
    if not out["json_ok"]:
        out["base_total"] = sum(1 for v in gold_base.values() if v != "1?")
        out["extra_missed"] = [e["kind"] for e in gold_extras]
        return out
    base = parsed.get("base") if isinstance(parsed.get("base"), dict) else {}
    extras = parsed.get("extras") if isinstance(parsed.get("extras"), list) else []
    for field in ("app_name", "image", "replicas", "port", "memory", "cpu"):
        pred, gold = base.get(field), gold_base.get(field)
        if gold == "1?":                       # 「一個 X」：留空或 1 都算對，不計入分母
            if not _empty(pred) and _int(pred) != 1:
                out["base_invented"] += 1
                out["errors"].append(f"{field}: 原句沒講副本數，卻填 {pred!r}")
            continue
        if gold is None:
            # 2026-11-12 組員指正：沒講的欄位（包含服務名稱）由「空缺檢查」補並標來源，正規化一律留空
            if not _empty(pred):
                out["base_invented"] += 1
                out["errors"].append(f"{field}: 原句沒講，卻填 {pred!r}")
            continue
        out["base_total"] += 1
        if field == "image":
            ok = _image_ok(gold, pred)
        elif field in ("replicas", "port"):
            ok = _int(pred) == int(gold)
        elif field == "memory":
            ok = _mem(pred) == _mem(gold)
        else:
            ok = str(pred or "").strip().lower() == str(gold).strip().lower()
        out["base_hit"] += ok
        if not ok:
            out["errors"].append(f"{field}: 應為 {gold!r}，得到 {pred!r}")
    used = set()
    for g in gold_extras:
        hit = None
        for i, e in enumerate(extras):
            if i in used or not isinstance(e, dict) or e.get("kind") != g["kind"]:
                continue
            val = json.dumps(e.get("value"), ensure_ascii=False)
            if "value" in g and g["value"] not in val:
                continue
            if "contains" in g and g["contains"] not in val:
                continue
            if "size" in g and g["size"].replace("Gi", "") not in val:
                continue
            if "cron" in g and g["cron"].replace(" ", "") not in val.replace(" ", ""):
                continue
            hit = i
            break
        if hit is None:
            out["extra_missed"].append(g["kind"])
            out["errors"].append(f"額外欄位漏掉或值不對：{g}")
        else:
            used.add(hit)
            out["extra_hit"] += 1
    out["extra_unexpected"] = sum(1 for i, e in enumerate(extras) if i not in used)
    for e in extras:
        if isinstance(e, dict) and e.get("evidence"):
            out["evidence_total"] += 1
            out["evidence_ok"] += str(e["evidence"]).strip() in test["text"]
    return out


def summarize(rows):
    n = len(rows)
    s = {k: sum(r["score"][k] for r in rows) for k in
         ("base_hit", "base_total", "base_invented", "extra_hit", "extra_total", "extra_unexpected", "evidence_ok", "evidence_total")}
    lat = sorted(r["latency_s"] for r in rows)
    return {
        "n": n,
        "json_ok_rate": round(sum(r["score"]["json_ok"] for r in rows) / n, 3),
        "base_acc": round(s["base_hit"] / max(1, s["base_total"]), 3),
        "base_invented": s["base_invented"],
        "extra_recall": round(s["extra_hit"] / max(1, s["extra_total"]), 3),
        "extra_missed_rate": round(1 - s["extra_hit"] / max(1, s["extra_total"]), 3),
        "extra_unexpected": s["extra_unexpected"],
        "evidence_exact_rate": round(s["evidence_ok"] / max(1, s["evidence_total"]), 3),
        "latency_p50_s": lat[n // 2], "latency_p90_s": lat[min(n - 1, int(n * 0.9))], "latency_max_s": lat[-1],
    }


# ── 執行 ─────────────────────────────────────────────
def make_hf(model_id, quant, thinking):
    import torch
    from transformers import AutoTokenizer
    kw = {"device_map": "cuda:0"}
    if quant == "4bit":
        from transformers import BitsAndBytesConfig
        kw["quantization_config"] = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4",
                                                        bnb_4bit_compute_dtype=torch.bfloat16)
    else:
        kw["dtype"] = torch.bfloat16
    tok = AutoTokenizer.from_pretrained(model_id)
    torch.cuda.reset_peak_memory_stats()
    try:
        from transformers import AutoModelForCausalLM
        model = AutoModelForCausalLM.from_pretrained(model_id, **kw)
    except Exception as e:
        print(f"[載入] AutoModelForCausalLM 失敗，改用 ImageTextToText：{e}")
        from transformers import AutoModelForImageTextToText
        model = AutoModelForImageTextToText.from_pretrained(model_id, **kw)
    model.eval()
    load_gb = torch.cuda.memory_allocated() / 1024 ** 3
    print(f"[載入] {model_id} {quant} 權重佔用 {load_gb:.2f} GB（{type(model).__name__}）")

    def run(text):
        msgs = [{"role": "system", "content": NORMALIZE_SYSTEM}, {"role": "user", "content": text}]
        extra = {} if thinking is None else {"enable_thinking": thinking}
        prompt = tok.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True, **extra)
        ids = tok(prompt, return_tensors="pt").to(model.device)
        with torch.no_grad():
            out = model.generate(**ids, max_new_tokens=2048 if thinking else 512, do_sample=False,
                                 pad_token_id=tok.pad_token_id or tok.eos_token_id)
        return tok.decode(out[0][ids["input_ids"].shape[1]:], skip_special_tokens=True)

    meta = {"load_gb": round(load_gb, 2), "class": type(model).__name__}
    return run, meta, lambda: round(torch.cuda.max_memory_allocated() / 1024 ** 3, 2)


def make_gemini():
    root = os.path.dirname(os.path.dirname(HERE))
    sys.path.insert(0, root)
    from core.gemini_client import _generate, is_available
    if not is_available():
        raise SystemExit("Gemini 不可用（沒有 key 或沒裝 google-genai）")
    last = [0.0]

    def run(text):
        wait = 13 - (time.time() - last[0])          # 免費層每把 key 每分鐘 5 次
        if wait > 0:
            time.sleep(wait)
        last[0] = time.time()
        return _generate(NORMALIZE_SYSTEM, text, "模型實測") or ""
    return run, {"model": os.environ.get("GEMINI_NORMALIZE_MODEL", "gemini-3.5-flash-lite")}, lambda: None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", choices=["hf", "gemini"])
    ap.add_argument("--model")
    ap.add_argument("--quant", choices=["bf16", "4bit"], default="bf16")
    ap.add_argument("--thinking", choices=["on", "off", "none"], default="off",
                    help="none = 模板不支援 enable_thinking（例如 Qwen2.5）")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--rescore")
    a = ap.parse_args()

    tests = {t["id"]: t for t in load_tests()}
    if a.rescore:
        with open(a.rescore, encoding="utf-8") as f:
            rows = [json.loads(line) for line in f if line.strip()]
        rows = [r for r in rows if "id" in r]
        for r in rows:
            r["score"] = score_one(tests[r["id"]], extract_json(r["raw"]))
        print(json.dumps(summarize(rows), ensure_ascii=False, indent=1))
        return

    thinking = {"on": True, "off": False, "none": None}[a.thinking]
    if a.backend == "hf":
        run, meta, peak = make_hf(a.model, a.quant, thinking)
        tag = f"{a.model.split('/')[-1]}_{a.quant}_think-{a.thinking}"
    else:
        run, meta, peak = make_gemini()
        tag = "gemini"
    os.makedirs(RESULTS, exist_ok=True)
    path = os.path.join(RESULTS, f"{tag}.jsonl")
    rows = []
    items = list(tests.values())[: a.limit] if a.limit else list(tests.values())
    with open(path, "w", encoding="utf-8") as f:
        for t in items:
            t0 = time.time()
            try:
                raw = run(t["text"])
            except Exception as e:
                raw = f"__ERROR__ {e}"
            lat = round(time.time() - t0, 2)
            row = {"id": t["id"], "raw": raw, "latency_s": lat, "score": score_one(t, extract_json(raw))}
            rows.append(row)
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()
            sc = row["score"]
            print(f"{t['id']:8} {lat:6.2f}s json={int(sc['json_ok'])} base={sc['base_hit']}/{sc['base_total']} "
                  f"extra={sc['extra_hit']}/{sc['extra_total']} 亂填={sc['base_invented']}", flush=True)
        summary = summarize(rows)
        summary.update({"backend": a.backend, "model": a.model, "quant": a.quant, "thinking": a.thinking,
                        "peak_vram_gb": peak(), **meta})
        f.write(json.dumps({"summary": summary}, ensure_ascii=False) + "\n")
    print(json.dumps(summary, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
