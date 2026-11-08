"""第二次實驗：盲測評分（2026-11-07）

讀 bench_blind.py 記錄的「實際發生了什麼」（blind_raw.jsonl），連同題目自帶的 expected、pass_if，
交給第三方模型（gemini_judge.JUDGE_MODEL）判斷每一筆通不通過，寫到 blind_graded.jsonl。

公平性：
- 評分模型看不到是哪個系統：每一筆單獨評分，系統名稱（ZeroTouch、Claude）從紀錄裡遮掉，
  只稱「這個系統」。
- 評分標準是題目自帶的 pass_if（出題模型寫的），評分提示對兩個系統完全相同。
- 開發端不讀題目，也不改評分結果。評分提示的版本（PROMPT_VERSION）記在每一筆資料裡。

用法：BENCH_OUT=docs/bench_20261107_r2 python scripts/bench/grade_blind.py
（已評過的會跳過；要重評就刪掉 blind_graded.jsonl）
"""
import json, re, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import OUT, append  # noqa: E402
from gemini_judge import JUDGE_MODEL, ask_json  # noqa: E402
from bench_blind import load_tasks  # noqa: E402

PROMPT_VERSION = "2026-11-07-v1"
GRADE_PROMPT = """你是一個部署平台實驗的評分者。一位不懂 Kubernetes 的新手對某個系統說了一句話，
下面是題目、出題者寫的評分標準，以及系統實際的表現紀錄。請只依照評分標準判斷這一次算不算通過。

【使用者說的話】
{text}

【正確的處理方式（出題者寫的）】
{expected}

【怎樣算通過（出題者寫的評分標準）】
{pass_if}

【測試開始前已經存在的服務】
{setup}

【系統實際的表現紀錄】
{observed}

判斷原則：
- 只看評分標準與紀錄裡的事實，不要假設紀錄裡沒有的事情。
- 「部署出的服務在運作」以紀錄裡的 ready（就緒）為準。
- 評分標準要求「先問清楚」或「拒絕並說明」時，要看系統對使用者說的話有沒有做到。
- 評分標準要求的條件只要有一項不符合，就算沒通過。

只輸出 JSON：{{"passed": true 或 false, "reason": "一兩句中文說明依據"}}"""

_NAME_RE = re.compile(r"zero\s*touch|claude(?:\s*code)?|anthropic", re.I)


def redact(obj):
    s = json.dumps(obj, ensure_ascii=False, default=str)
    return _NAME_RE.sub("這個系統", s)


def observed_of(r):
    snap = r.get("snapshot") or {}
    return {
        "結果種類": r.get("outcome"),
        "服務是否全部就緒(ready)": r.get("ready"),
        "就緒狀態": r.get("ready_state"),
        "實際部署出的工作負載": [{k: x.get(k) for k in ("kind", "name", "image", "replicas", "req", "lim", "ports", "env", "command")}
                         for x in snap.get("spec", [])],
        "對外開放的 port": snap.get("svc_ports"),
        "安全相關設定": snap.get("flags"),
        "系統對使用者說的話": r.get("said"),
        "系統顯示的提示": r.get("notices"),
        "系統執行過的指令（節錄）": [c[1][:200] if isinstance(c, (list, tuple)) and len(c) > 1 else str(c)[:200]
                           for c in (r.get("commands") or [])][:15],
    }


if __name__ == "__main__":
    tasks = {t["id"]: t for t in load_tasks()}
    raw = [json.loads(x) for x in (OUT / "blind_raw.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    graded_path = OUT / "blind_graded.jsonl"
    done = set()
    if graded_path.exists():
        for x in graded_path.read_text(encoding="utf-8").splitlines():
            if x.strip():
                g = json.loads(x)
                done.add((g["task_id"], g["system"], g["round"]))
    n = 0
    for r in raw:
        key = (r["task_id"], r["system"], r["round"])
        if key in done:
            continue
        t = tasks[r["task_id"]]
        if r.get("outcome") == "harness_error":
            verdict = {"passed": None, "reason": "測試程式錯誤，這筆不計分：" + str(r.get("error"))[:200]}
        else:
            verdict = ask_json(GRADE_PROMPT.format(text=t["text"], expected=t.get("expected"), pass_if=t.get("pass_if"),
                                                   setup=redact(r.get("setup")) if r.get("setup") else "（沒有）",
                                                   observed=redact(observed_of(r)))) or {"passed": None, "reason": "評分模型沒有回應"}
        out = {"task_id": r["task_id"], "category": r.get("category"), "system": r["system"], "round": r["round"],
               "passed": verdict.get("passed"), "judge_reason": verdict.get("reason"), "judge_model": JUDGE_MODEL,
               "prompt_version": PROMPT_VERSION, "latency_s": r.get("latency_s"), "cost_usd": r.get("cost_usd"),
               "gpu": r.get("gpu"), "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
        append(graded_path, out)
        done.add(key)
        n += 1
        print(f"{r['task_id']} r{r['round']} {r['system']:9} passed={out['passed']}", flush=True)
    print(f"這次評了 {n} 筆，輸出 {graded_path}")
