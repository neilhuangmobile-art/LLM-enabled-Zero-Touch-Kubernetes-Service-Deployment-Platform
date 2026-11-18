"""
Claude 出題（2026-11-15，PROMPTx-RAG 計畫步驟 1）

  python scripts/rag/gen_claude_set.py questions   # 300 題 → docs/bench_blind/dev_claude.jsonl
  python scripts/rag/gen_claude_set.py perturb     # Gemini 與 Claude 兩組題目各 4 種擾動 → docs/bench_blind/dev_perturb.jsonl

目的：11/15 發現「Gemini 寫的知識庫配 Gemini 出的題」分數會被寫法相近墊高，所以另用 Claude 出一組，
兩組互換測試（PROMPTx-PE 的跨領域測試）；擾動版本對應論文式 6 的穩健性（取最差表現）。

出題規格與 Gemini 開發集相同（沿用 gen_data.py 的 DEV_PROMPT 與 DEV_PERSONAS），只換出題模型。
claude 在空的暫存資料夾執行、不開任何工具，讀不到專案與知識庫。兩個輸出檔都是可閱讀的開發用題目，
不是盲測集；防汙染測試（tests/test_field_retriever.py）會檢查它們與知識庫沒有重疊。
中斷後重跑會接續（已完成的批次不重做）。
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "rag"))
from gen_data import DEV_PERSONAS, DEV_PROMPT, NEED_CODES  # noqa: E402

CLAUDE_CMD = os.environ.get("CLAUDE_CMD", "claude.cmd" if os.name == "nt" else "claude")
OUT_Q = ROOT / "docs" / "bench_blind" / "dev_claude.jsonl"
OUT_P = ROOT / "docs" / "bench_blind" / "dev_perturb.jsonl"
DEV_GEMINI = ROOT / "docs" / "bench_blind" / "dev_scenario.jsonl"
LOG = ROOT / "data" / "claude_gen_usage.jsonl"
NO_TOOLS = "Bash,PowerShell,Read,Write,Edit,Glob,Grep,WebFetch,WebSearch,NotebookEdit,Agent,Skill"


def ask_claude(prompt: str, timeout: int = 900):
    """送一次 claude -p，回傳解析出的 JSON 陣列；失敗回 []。用量記到 data/claude_gen_usage.jsonl。"""
    with tempfile.TemporaryDirectory(prefix="zt_claude_gen_") as wd:
        cmd = [CLAUDE_CMD, "-p", "--output-format", "json", "--no-session-persistence", "--strict-mcp-config",
               "--setting-sources", "local", "--disallowedTools", NO_TOOLS]
        t0 = time.time()
        try:
            p = subprocess.run(cmd, cwd=wd, input=prompt, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=timeout)
            out = p.stdout
        except subprocess.TimeoutExpired:
            print("  [claude] 逾時", flush=True)
            return []
    try:
        res = json.loads(out)
    except Exception:  # noqa: BLE001
        print(f"  [claude] 輸出不是 JSON：{out[:200]}", flush=True)
        return []
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "seconds": round(time.time() - t0, 1),
                            "cost_usd": res.get("total_cost_usd"), "usage": res.get("usage"),
                            "model": ",".join((res.get("modelUsage") or {}).keys())}, ensure_ascii=False) + "\n")
    text = res.get("result") or ""
    m = re.search(r"\[.*\]", text, re.S)
    try:
        data = json.loads(m.group(0) if m else text)
        return data if isinstance(data, list) else []
    except Exception as e:  # noqa: BLE001
        dump = LOG.parent / f"claude_gen_fail_{time.strftime('%H%M%S')}.txt"
        dump.write_text(text, encoding="utf-8")
        print(f"  [claude] 陣列解析失敗（{type(e).__name__}: {str(e)[:80]}），原始輸出存在 {dump}", flush=True)
        return []


def _load(path):
    return [json.loads(x) for x in open(path, encoding="utf-8") if x.strip().startswith("{")] if path.exists() else []


def gen_questions(batches: int = 15):
    have = _load(OUT_Q)
    done_batches = {q.get("_batch") for q in have}
    for b in range(batches):
        if b in done_batches:
            continue
        persona, scen = DEV_PERSONAS[b % len(DEV_PERSONAS)]
        avoid = "；".join(q["text"][:30] for q in have[-40:]) or "（無）"
        prompt = ("你是研究實驗的出題者。只輸出 JSON 陣列，不要任何其他文字，也不要用程式碼區塊。\n\n"
                  + DEV_PROMPT.format(persona=persona, scenario=scen, avoid=avoid, needs="、".join(NEED_CODES)))
        for attempt in range(2):
            out = [q for q in ask_claude(prompt) if isinstance(q, dict) and q.get("text") and q.get("expected")]
            if len(out) >= 15:
                break
            print(f"  第 {b + 1} 批只有 {len(out)} 題，重試", flush=True)
        for q in out[:20]:
            q["id"] = f"C{len(have) + 1:03d}"
            q["_batch"] = b
            q["source"] = "claude"
            have.append(q)
        with open(OUT_Q, "w", encoding="utf-8") as f:
            for q in have:
                f.write(json.dumps(q, ensure_ascii=False) + "\n")
        print(f"  第 {b + 1}/{batches} 批（{persona}）：累計 {len(have)} 題", flush=True)


PERTURB_PROMPT = """你在替一個「用一句話部署服務到 Kubernetes」的系統做穩健性測試。下面每一題是新手說的話。
請替每一題各寫 4 種「意思完全相同」的改寫，要求：
- typo：保留原意，加入 2～4 個常見錯字、同音字或注音文（例如「的/得」「在/再」「ㄉ」）。
- short：壓縮成 15 字以內最精簡的說法，保留所有關鍵需求（人數、要不要保存資料、定時、對外等）。
- verbose：加上與需求無關的閒聊或抱怨，總長度約原句 2 倍，關鍵資訊打散到不同位置。
- mixed：中英夾雜（例如 deploy、database、server、backup 等一般英文詞），但不可寫出原句沒提到的軟體名稱。
不可新增或刪除任何需求，不可寫出原句沒有的軟體名稱、版本、數字。繁體中文（台灣用語）。

題目：
{items}

只輸出 JSON 陣列，每題一個元素：{{"id":"原題號","typo":"","short":"","verbose":"","mixed":""}}"""


def gen_perturb(batch_size: int = 25):
    qs = _load(DEV_GEMINI) + _load(OUT_Q)
    have = {(r["id"], r["kind"]) for r in _load(OUT_P)}
    todo = [q for q in qs if (q["id"], "typo") not in have]
    for i in range(0, len(todo), batch_size):
        part = todo[i:i + batch_size]
        items = "\n".join(f"- {q['id']}：{q['text']}" for q in part)
        out = ask_claude(PERTURB_PROMPT.format(items=items))
        ids = {q["id"] for q in part}
        with open(OUT_P, "a", encoding="utf-8") as f:
            for o in out:
                if not isinstance(o, dict) or o.get("id") not in ids:
                    continue
                for kind in ("typo", "short", "verbose", "mixed"):
                    if o.get(kind):
                        f.write(json.dumps({"id": o["id"], "kind": kind, "text": o[kind]}, ensure_ascii=False) + "\n")
        print(f"  擾動：{min(i + batch_size, len(todo))}/{len(todo)}", flush=True)


if __name__ == "__main__":
    {"questions": gen_questions, "perturb": gen_perturb}[sys.argv[1]]()
