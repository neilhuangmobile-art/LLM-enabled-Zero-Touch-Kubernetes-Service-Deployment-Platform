"""
qa_sweep_20261007_deploy_parse.py
補測：qa_sweep_20261007_large.py 的 deploy_simple/deploy_ambiguous/deploy_edge 這
55 題，原本送 /api/intent，但那個端點對 deploy 動作只回 {action:"deploy", args:{}}
（規則層只判斷「這是不是部署請求」，不解析實際內容），完全沒有真的測到
llama_client.py 的 _deterministic_deploy_parse()（副本數邊界值/中文數字詞/多服務
偵測這些這次 session 修過的邏輯）。這支腳本改送正確的端點 /api/deploy/parse 重跑
同樣這 55 題。
"""
import os
import sys
import json
import time
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core.config import ensure_utf8_output
ensure_utf8_output()

import qa_sweep_20261007_large as base

BASE = base.BASE
OUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "docs", "qa_sweep_20261007_deploy_parse_results.jsonl")

cases = [(cat, msg) for cat, msg, ep in base.CASES
         if cat in ("deploy_simple", "deploy_ambiguous", "deploy_edge")]
print(f"重測 {len(cases)} 題（走 /api/deploy/parse）")

s = base.login()
with open(OUT_PATH, "w", encoding="utf-8") as f:
    for i, (cat, msg) in enumerate(cases):
        t0 = time.time()
        try:
            r = s.post(f"{BASE}/api/deploy/parse", json={"input": msg}, timeout=60)
            status = r.status_code
            try:
                out = r.json()
            except Exception:
                out = {"_raw": r.text[:500]}
        except Exception as e:
            status = None
            out = {"_error": str(e)}
        latency_ms = round((time.time() - t0) * 1000, 1)
        row = {"i": i, "category": cat, "input": msg, "status": status,
               "output": out, "latency_ms": latency_ms}
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
        f.flush()
        print(f"[{i+1}/{len(cases)}] ({cat}, {latency_ms:7.0f}ms) {msg[:50]!r} -> status={status}")

print(f"\n完成，結果存於：{OUT_PATH}")
