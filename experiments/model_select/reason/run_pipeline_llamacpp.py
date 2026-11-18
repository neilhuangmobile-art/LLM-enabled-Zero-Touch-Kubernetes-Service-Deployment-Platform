"""2026-11-18 同 run_pipeline_eval.py（組員 S01～S40、同一套評分），只把本地模型換成 llama.cpp 的 llama-server。

目的：AMD 顯卡不能用 bitsandbytes 4-bit、transformers 跑 Qwen3.5 也沒有加速核心（一次推理 11～16 秒），
測 llama.cpp（Vulkan／CUDA／CPU）跑同一個 Qwen3.5-4B 的品質與速度。基準是組員在 NVIDIA 4-bit 跑的 v8
（docs/bench_record/pipeline_s40/pipeline_local_v8.jsonl）。

  python experiments/model_select/reason/run_pipeline_llamacpp.py --url http://127.0.0.1:8781 --tag vulkan_q8
"""
import argparse
import json
import os
import sys
import time

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from run_pipeline_eval import ROOT, load_tests, runner, score_one, summarize, to_pred  # noqa: E402,F401

_stats = {"calls": 0, "ms": 0.0, "tokens": 0}


def make_llm(url, timeout=120):
    def llm(system, user, max_new_tokens=768):
        t0 = time.time()
        try:
            r = requests.post(f"{url}/v1/chat/completions", timeout=timeout, json={
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "max_tokens": max_new_tokens, "temperature": 0, "cache_prompt": False,   # 開著快取時輸出不固定（11-18 實測）
                "chat_template_kwargs": {"enable_thinking": False}})
            r.raise_for_status()
            j = r.json()
        except Exception as e:  # noqa: BLE001
            print(f"  [llama] 失敗：{e}", flush=True)
            return None
        _stats["calls"] += 1
        _stats["ms"] += (time.time() - t0) * 1000
        _stats["tokens"] += (j.get("usage") or {}).get("completion_tokens", 0)
        return j["choices"][0]["message"]["content"]
    return llm


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--tag", required=True)
    ap.add_argument("--limit", type=int)
    a = ap.parse_args()
    llm = make_llm(a.url)
    tests = {t["id"]: t for t in load_tests()}
    items = list(tests.values())[: a.limit] if a.limit else list(tests.values())
    path = os.path.join(HERE, "results", f"pipeline_llamacpp_{a.tag}.jsonl")
    rows = []
    with open(path, "w", encoding="utf-8") as f:
        for t in items:
            t0 = time.time()
            spec = runner.run(t["text"], None, llm)
            pred = to_pred(spec)
            row = {"id": t["id"], "latency_s": round(spec.get("total_ms", 0) / 1000, 2), "wall_s": round(time.time() - t0, 2),
                   "spec": runner._public(spec), "pred": pred, "raw": json.dumps(pred, ensure_ascii=False),
                   "score": score_one(t, pred)}
            rows.append(row)
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()
            bad = [k for k, v in row["score"]["checks"].items() if not v]
            print(f"{t['id']} {row['latency_s']:6.2f}s pass={int(row['score']['pass'])} 錯={bad} q={len(spec['questions'])}", flush=True)
        s = summarize(rows, tests)
        s.update({"engine": "llama.cpp", "tag": a.tag, "rag_backend": os.environ.get("RAG_BACKEND", "field"),
                  "select_rule": os.environ.get("PIPELINE_SELECT_RULE", "agree"),
                  "llm_calls": _stats["calls"], "llm_ms_per_call": round(_stats["ms"] / max(1, _stats["calls"])),
                  "tok_per_s": round(_stats["tokens"] / max(1e-9, _stats["ms"] / 1000), 1)})
        f.write(json.dumps({"summary": s}, ensure_ascii=False) + "\n")
    print(json.dumps(s, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
