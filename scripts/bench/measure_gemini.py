"""量 ZeroTouch 每次請求呼叫 Gemini 用掉多少 token（網站本身沒有記錄用量）

做法：用實驗 A、D 的同一批輸入，直接呼叫平台裡的同一組函式，攔截 SDK 回傳的 usage_metadata。
部署路徑假設「正規化＋情境擷取」兩個呼叫都會發生（實際上情境擷取只在像情境描述時才呼叫），所以是上限估計。
問答路徑：惡意誘導判斷＋聊天正規化，同樣當上限。
用法：python scripts/bench/measure_gemini.py
"""
import json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import gemini_client as g  # noqa: E402
from bench_deploy import TASKS  # noqa: E402
from bench_qa import QS  # noqa: E402

usage = []
_orig = g._client_for


def _wrap(key):
    c = _orig(key)
    if getattr(c.models, "_bench_wrapped", False):
        return c
    real = c.models.generate_content

    def gen(*a, **kw):
        r = real(*a, **kw)
        u = getattr(r, "usage_metadata", None)
        usage.append({"model": kw.get("model"), "in": getattr(u, "prompt_token_count", 0) or 0,
                      "out": (getattr(u, "candidates_token_count", 0) or 0) + (getattr(u, "thoughts_token_count", 0) or 0)})
        return r
    c.models.generate_content = gen
    c.models._bench_wrapped = True
    return c


g._client_for = _wrap

rows = []
for tid, cat, text, _ in TASKS:
    n0 = len(usage)
    g.gemini_normalize_deploy_request(text)
    g.gemini_extract_scenario(text)
    u = usage[n0:]
    rows.append({"path": "deploy", "id": tid, "calls": len(u), "in": sum(x["in"] for x in u), "out": sum(x["out"] for x in u),
                 "model": u[0]["model"] if u else None})
for qid, q, _ in QS:
    n0 = len(usage)
    g.classify_malicious_intent(q)
    g.gemini_normalize_chat_message(q, [])
    u = usage[n0:]
    rows.append({"path": "chat", "id": qid, "calls": len(u), "in": sum(x["in"] for x in u), "out": sum(x["out"] for x in u),
                 "model": u[0]["model"] if u else None})
out = ROOT / "docs" / "bench_20261106" / "gemini_usage.jsonl"
out.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
for p in ("deploy", "chat"):
    rs = [r for r in rows if r["path"] == p]
    print(p, "平均呼叫", sum(r["calls"] for r in rs) / len(rs), "平均輸入 token", sum(r["in"] for r in rs) / len(rs),
          "平均輸出 token", sum(r["out"] for r in rs) / len(rs), "模型", rs[0]["model"])
