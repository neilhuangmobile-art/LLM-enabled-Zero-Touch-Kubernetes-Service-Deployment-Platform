"""2026-11-15 第二份測試：組員出的 S01～S40，測推理代理「補完整個設定」的正確率和速度。

用法（要用 C:\\zt-k8s\\.venv-qwen35）：
  python experiments/model_select/reason/run_reason.py --model Qwen/Qwen3.5-4B --quant 4bit --tag server-up
  python experiments/model_select/reason/run_reason.py --rescore results/<檔名>.jsonl

評分怎麼從組員的標準答案換算成程式檢查，寫在 CHECKS（題目原文的 extra／pass_if 是文字，程式看不懂，
所以由我換成結構化條件；有爭議的題目在報告裡列出）。"""
import argparse
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from prompt_reason import REASON_SYSTEM  # noqa: E402
from run_eval import extract_json  # noqa: E402

TESTSET = os.path.join(HERE, "testset_s.jsonl")
RESULTS = os.path.join(HERE, "results")

PUB, INT, NONE = "public", "internal", "none"
# 由組員的 extra / pass_if 換算：expose＝可接受的開放方式；storage＝True 要有、False 不能有、
# 數字＝至少幾 Gi；cron＝排程；secret＝要有資料庫密碼。沒列的項目不檢查。
CHECKS = {
    "S01": {"expose": [PUB]}, "S02": {"expose": [PUB]}, "S03": {"secret": True},
    "S04": {"expose": [PUB, INT]}, "S05": {"expose": [PUB, INT]}, "S06": {"expose": [PUB]},
    "S07": {"expose": [PUB]}, "S08": {"expose": [PUB, INT]}, "S09": {}, "S10": {"expose": [PUB]},
    "S11": {"expose": [PUB]}, "S12": {"expose": [PUB], "storage": True},
    "S13": {"cron": "0 3 * * *", "storage": True, "expose": [NONE]},
    "S14": {"expose": [INT]}, "S15": {"expose": [PUB], "storage": True}, "S16": {"expose": [NONE]},
    "S17": {"expose": [PUB], "storage": True}, "S18": {"expose": [INT], "storage": True, "secret": True},
    "S19": {"expose": [INT], "storage": True}, "S20": {"expose": [PUB], "storage": True},
    "S21": {"expose": [INT], "storage": True},
    "S22": {"cron": "0 0 * * 1", "storage": True, "expose": [NONE]},
    "S23": {"expose": [INT], "storage": False}, "S24": {"expose": [PUB], "storage": 10},
    "S25": {"expose": [PUB]}, "S26": {"expose": [INT], "storage": True, "secret": True},
    "S27": {"expose": [INT], "storage": True}, "S28": {"cron": "0 0 * * *", "expose": [NONE]},
    "S29": {"expose": [PUB], "storage": True}, "S30": {"expose": [PUB], "storage": True},
    "S31": {"cron": "0 * * * *", "storage": True, "expose": [NONE]},
    "S32": {"expose": [INT], "storage": True}, "S33": {"expose": [PUB], "storage": 20},
    "S34": {"expose": [PUB], "storage": True}, "S35": {"expose": [NONE], "storage": True},
    "S36": {"expose": [PUB], "storage": True},
    "S37": {}, "S38": {}, "S39": {}, "S40": {},
}
_ALIAS = {"mongodb": "mongo", "postgresql": "postgres", "grafana/grafana-oss": "grafana/grafana"}


def load_tests():
    with open(TESTSET, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def norm_image(v):
    if not isinstance(v, str) or not v.strip():
        return None
    s = v.strip().lower().split("@")[0]
    parts = s.split("/")
    if len(parts) > 1 and ("." in parts[0] or ":" in parts[0]):   # 去掉 registry（ghcr.io/…）
        parts = parts[1:]
    if parts and parts[0] == "library":
        parts = parts[1:]
    s = "/".join(parts)
    if ":" in s.split("/")[-1]:
        s = s.rsplit(":", 1)[0]
    return _ALIAS.get(s, s)


def to_mi(v):
    m = re.fullmatch(r"\s*([\d.]+)\s*(Ki|Mi|Gi|K|M|G)?i?B?\s*", str(v or ""), re.I)
    if not m:
        return None
    n, u = float(m.group(1)), (m.group(2) or "Mi").lower()
    return n * {"ki": 1 / 1024, "k": 1 / 1024, "mi": 1, "m": 1, "gi": 1024, "g": 1024}[u]


def to_milli(v):
    s = str(v if v is not None else "").strip()
    m = re.fullmatch(r"([\d.]+)\s*(m?)", s)
    if not m:
        return None
    return float(m.group(1)) * (1 if m.group(2) else 1000)


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def score_one(t, p):
    exp, chk = t["expected"], CHECKS[t["id"]]
    must_ask = bool(t["must_ask"])
    r = {"json_ok": isinstance(p, dict), "must_ask": must_ask, "checks": {}}
    if not isinstance(p, dict):
        r["pass"] = False
        return r
    asked = bool(p.get("questions"))
    r["asked"] = asked
    c = r["checks"]
    if must_ask:                      # 這 4 題只看有沒有反問
        c["ask"] = asked
        r["pass"] = asked
        return r
    acc = [_ALIAS.get(a, a) for a in exp["image"]["accept"]]
    c["image"] = norm_image(p.get("image")) in acc
    rep = _int(p.get("replicas"))
    c["replicas"] = rep is not None and exp["replicas"]["min"] <= rep <= exp["replicas"]["max"]
    port = _int(p.get("port"))
    c["port"] = (port is None) if not exp["port"]["accept"] else (port in exp["port"]["accept"])
    mem, cpu = to_mi(p.get("memory")), to_milli(p.get("cpu"))
    c["memory"] = mem is not None and to_mi(exp["memory"]["min"]) <= mem <= to_mi(exp["memory"]["max"])
    c["cpu"] = cpu is not None and to_milli(exp["cpu"]["min"]) <= cpu <= to_milli(exp["cpu"]["max"])
    wl = str(p.get("workload") or "").lower()
    if "cron" in chk:
        cron = " ".join(str(p.get("schedule") or "").split())
        c["cronjob"] = wl == "cronjob" and cron == chk["cron"]
    else:
        c["not_cronjob"] = wl != "cronjob"
    if "expose" in chk:
        c["expose"] = str(p.get("expose") or "").lower() in chk["expose"]
    st = p.get("storage")
    has_st = isinstance(st, dict) or (isinstance(st, str) and st.strip() not in ("", "null"))
    if "storage" in chk:
        want = chk["storage"]
        if want is False:
            c["storage"] = not has_st
        elif want is True:
            c["storage"] = has_st
        else:
            size = to_mi(st.get("size")) if isinstance(st, dict) else None
            c["storage"] = has_st and size is not None and size >= want * 1024
    if chk.get("secret"):
        c["secret"] = p.get("db_password") is True
    r["false_ask"] = asked
    r["pass"] = all(c.values())
    return r


def summarize(rows, tests):
    n = len(rows)
    out = {"n": n, "json_ok_rate": round(sum(r["score"]["json_ok"] for r in rows) / n, 3),
           "pass_rate": round(sum(r["score"]["pass"] for r in rows) / n, 3)}
    for d in ("簡單", "中等", "困難"):
        sub = [r for r in rows if tests[r["id"]]["difficulty"] == d]
        if sub:
            out[f"pass_{d}"] = f"{sum(r['score']['pass'] for r in sub)}/{len(sub)}"
    keys = {}
    for r in rows:
        for k, v in r["score"]["checks"].items():
            keys.setdefault(k, []).append(v)
    out["checks"] = {k: f"{sum(v)}/{len(v)}" for k, v in keys.items()}
    out["false_ask"] = sum(1 for r in rows if r["score"].get("false_ask"))
    lat = sorted(r["latency_s"] for r in rows)
    out["latency_p50_s"] = lat[n // 2]
    out["latency_p90_s"] = lat[min(n - 1, int(n * 0.9))]
    out["latency_max_s"] = lat[-1]
    return out


def gpu_state():
    try:
        q = subprocess.run(["nvidia-smi", "--query-gpu=temperature.gpu,clocks.sm,power.draw",
                            "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=10)
        t, clk, pw = [x.strip() for x in q.stdout.strip().split(",")]
        return {"temp_c": int(t), "sm_mhz": int(clk), "power_w": float(pw)}
    except Exception:
        return None


def make_hf(model_id, quant, thinking):
    import torch
    from transformers import AutoTokenizer, AutoModelForCausalLM
    kw = {"device_map": "cuda:0"}
    if quant == "4bit":
        from transformers import BitsAndBytesConfig
        kw["quantization_config"] = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4",
                                                        bnb_4bit_compute_dtype=torch.bfloat16)
    else:
        kw["dtype"] = torch.bfloat16
    tok = AutoTokenizer.from_pretrained(model_id)
    torch.cuda.reset_peak_memory_stats()
    model = AutoModelForCausalLM.from_pretrained(model_id, **kw).eval()
    print(f"[載入] {model_id} {quant} {torch.cuda.memory_allocated() / 1024 ** 3:.2f} GB", flush=True)

    def run(text):
        msgs = [{"role": "system", "content": REASON_SYSTEM}, {"role": "user", "content": text}]
        prompt = tok.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True, enable_thinking=thinking)
        ids = tok(prompt, return_tensors="pt").to(model.device)
        with torch.no_grad():
            out = model.generate(**ids, max_new_tokens=3072 if thinking else 768, do_sample=False,
                                 pad_token_id=tok.pad_token_id or tok.eos_token_id)
        return tok.decode(out[0][ids["input_ids"].shape[1]:], skip_special_tokens=True)

    def warmup():                     # 第一次呼叫要編譯 kernel（約 48 秒），不算進成績
        run("幫我開一個 nginx")
    return run, warmup, lambda: round(torch.cuda.max_memory_allocated() / 1024 ** 3, 2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model")
    ap.add_argument("--quant", choices=["bf16", "4bit"], default="4bit")
    ap.add_argument("--thinking", choices=["on", "off"], default="off")
    ap.add_argument("--tag", default="")
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
        print(json.dumps(summarize(rows, tests), ensure_ascii=False, indent=1))
        for r in rows:
            bad = [k for k, v in r["score"]["checks"].items() if not v]
            if bad or not r["score"]["json_ok"]:
                print(r["id"], "錯：", bad or "JSON 解析失敗")
        return

    thinking = a.thinking == "on"
    run, warmup, peak = make_hf(a.model, a.quant, thinking)
    print("[暖機]", gpu_state(), flush=True)
    warmup()
    os.makedirs(RESULTS, exist_ok=True)
    tag = f"{a.model.split('/')[-1]}_{a.quant}_think-{a.thinking}" + (f"_{a.tag}" if a.tag else "")
    path = os.path.join(RESULTS, f"{tag}.jsonl")
    items = list(tests.values())[: a.limit] if a.limit else list(tests.values())
    rows = []
    with open(path, "w", encoding="utf-8") as f:
        for t in items:
            g0 = gpu_state()
            t0 = time.time()
            try:
                raw = run(t["text"])
            except Exception as e:
                raw = f"__ERROR__ {e}"
            lat = round(time.time() - t0, 2)
            row = {"id": t["id"], "raw": raw, "latency_s": lat, "out_chars": len(raw),
                   "gpu_before": g0, "gpu_after": gpu_state(),
                   "score": score_one(t, extract_json(raw))}
            rows.append(row)
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()
            bad = [k for k, v in row["score"]["checks"].items() if not v]
            print(f"{t['id']} {lat:6.2f}s pass={int(row['score']['pass'])} 錯={bad} gpu={row['gpu_after']}", flush=True)
        s = summarize(rows, tests)
        s.update({"model": a.model, "quant": a.quant, "thinking": a.thinking, "tag": a.tag, "peak_vram_gb": peak()})
        f.write(json.dumps({"summary": s}, ensure_ascii=False) + "\n")
    print(json.dumps(s, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
