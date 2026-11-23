"""2026-11-18 情境部署最佳化的固定評估：用正式路徑（agents.pipeline.llm.local_llm、agree 規則、欄位 RAG）跑開發題組，
不送 Gemini（正規化交給本地模型），所以不花錢、使用者資料不外流。

題組分工（避免調過頭）：
- scenario：docs/bench_blind/dev_scenario.jsonl（200 題）＝調整用，所有改動先看這組
- claude：docs/bench_blind/dev_claude.jsonl（300 題）＝驗證用，只用來確認改動，不拿來調參數
盲測題組不在這裡，最後只跑一次且要先問使用者。

  python experiments/model_select/reason/run_pipeline_dev.py --set scenario --tag baseline
  PIPELINE_ASK_POLICY=all python ... --tag ask_all          # 對照用：恢復原本的反問方式
  python ... --compare scenario baseline new               # 逐題配對 McNemar
結果：results/dev_<set>_<tag>.jsonl（逐題，含 passed 與判定依據）與 summary_dev_<set>_<tag>.json
"""
import argparse
import collections
import io
import json
import os
import sys
import time
import contextlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from run_pipeline_eval import ROOT, runner, to_pred  # noqa: E402
from run_pipeline_claude import score as _strict_score  # noqa: E402
from run_reason import _ALIAS, norm_image  # noqa: E402
from agents.pipeline import llm as pllm  # noqa: E402


# 最後一段是這些通用字時，名稱要看前一段（nodebb/docker＝nodebb、vaultwarden/server＝vaultwarden）
_GENERIC_LAST = {"docker", "server", "core", "cli", "app", "web", "community", "stable"}


def _base(img):
    """映像檔比對用的名稱：去掉 tag、取最後一段（gitea/gitea 與 gitea 視為同一個）。
    2026-11-19：最後一段是 docker、server 這類通用字時改取前一段（原本 nodebb/docker 被當成 docker 而判錯）。"""
    n = norm_image(img)
    if not n:
        return None
    n = n.split(":")[0]
    parts = _ALIAS.get(n, n).split("/")
    last = parts[-1]
    if last in _GENERIC_LAST and len(parts) > 1:
        last = parts[-2]
    return _ALIAS.get(last, last)


def score(t, p):
    """組員的評分要求映像檔名稱完全相同，開發題組的答案多半只寫短名（gitea、grafana），
    會把 gitea/gitea 這種正確答案算錯。2026-11-18 改成比對最後一段名稱，其他判定不變。"""
    s = _strict_score(t, p)
    if s["kind"] == "answer":
        acc = {_base(a) for a in t["expected"]["image"]["accept"]}
        s["checks"]["image"] = _base(p.get("image")) in acc
        s["passed"] = all(s["checks"].values()) and not s["asked"]
        s["rule"] += "；映像檔比對最後一段名稱（2026-11-18）"
    return s

SETS = {"scenario": "dev_scenario.jsonl", "claude": "dev_claude.jsonl",
        "blind": "blind_scenario.jsonl",   # 盲測（2026-11-20 使用者同意跑一次；跑完不拿來調整）
        # 2026-11-23 獨立驗證題組：另開、不知情的 Claude 出題，使用者抽查後鎖定；只跑不調、只看統計
        "holdout": "holdout_20261123.jsonl", "holdout2": "holdout2_20261124.jsonl",
        # 2026-11-23：柏碩 11-18 打包的題組（zt_testsets_20261118.zip）。s40＝組員原本的 S01～S40（repo 裡已有同內容）；
        # perturb＝dev_scenario／dev_claude 各題的錯字、極短、囉嗦、中英夾雜改寫，標準答案沿用原題
        "s40": "../../experiments/model_select/reason/testset_s.jsonl", "perturb": "dev_perturb.jsonl"}


def load_tasks(set_: str) -> list:
    """讀題組。perturb 只有 id／kind／text：標準答案從原題（dev_scenario、dev_claude）帶入，id 改成 D001_typo 避免重複。"""
    def read(name):
        return [json.loads(x) for x in open(os.path.normpath(os.path.join(ROOT, "docs", "bench_blind", name)), encoding="utf-8")
                if x.strip().startswith("{")]
    tasks = read(SETS[set_])
    if set_ == "perturb":
        base = {t["id"]: t for t in read(SETS["scenario"]) + read(SETS["claude"])}
        tasks = [dict(base[t["id"]], id=f"{t['id']}_{t['kind']}", base_id=t["id"], kind=t["kind"], text=t["text"])
                 for t in tasks if t["id"] in base]
    return tasks


def _source(field: str) -> str:
    """反問來自哪個步驟（統計用）。"""
    for p in ("model:", "extra:", "verify:", "format:"):
        if field.startswith(p):
            return p[:-1]
    return field


def run(a):
    if a.gemini:
        # 正式環境的路徑（Gemini 正規化）會花錢：一定要有上限，超過就停用 Gemini（core/gemini_client._budget_exceeded）
        if not a.max_gemini_calls:
            sys.exit("--gemini 一定要搭配 --max-gemini-calls（花費上限），避免把預付額度用光")
        os.environ["GEMINI_MAX_CALLS"] = str(a.max_gemini_calls)
        from core import gemini_client
        if not gemini_client.is_available():
            sys.exit("Gemini 沒有可用的 key，停止")
    if not pllm.local_available():
        sys.exit(f"本地模型 {pllm.LOCAL_URL} 沒有回應，停止（不跑沒有模型的評估）")
    fails = {"n": 0, "total": 0}

    def llm(system, user, max_new_tokens=768):
        out = pllm.local_llm(system, user, max_new_tokens)
        fails["total"] += 1
        fails["n"] += out is None
        return out

    tasks = load_tasks(a.set)
    tasks = tasks[: a.limit] if a.limit else tasks
    path = os.path.join(HERE, "results", f"dev_{a.set}_{a.tag}.jsonl")
    gem, gstat = _gemini_fn(a)
    rows = []
    with open(path, "w", encoding="utf-8") as f:
        for t in tasks:
            t0 = time.time()
            with contextlib.redirect_stdout(io.StringIO()):
                spec = runner.run(t["text"], gem, llm)
            pred = to_pred(spec)
            s = score(t, pred)
            row = {"id": t["id"], "difficulty": t.get("difficulty"), "input": t["text"], "pred": pred, "score": s,
                   "passed": s["passed"], "latency_ms": int((time.time() - t0) * 1000),
                   "questions": [{"field": q["field"], "q": q["question"][:120]} for q in spec["questions"]],
                   "notices": spec.get("notices", [])[:8],
                   # 選軟體的信心度（每個候選的 P(Yes)），離線掃描門檻用
                   "choose_scores": spec.get("choose_scores"), "choose_reason": spec.get("choose_reason"),
                   # 每個欄位是誰填的（user／inferred／default），算各欄位的信心度用
                   "sources": {f: spec["base"][f]["source"] for f in spec["base"]},
                   # 2026-11-22：軟體講清楚了沒（來源 gemini／local、判斷、本地機率）與正規化用了哪個模型
                   "clarity": {k: v for k, v in (spec.get("clarity") or {}).items() if k != "text"} or None,
                   "normalize_backend": spec["backend"].get("normalize")}
            rows.append(row)
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()
            print(f"{t['id']} {row['latency_ms'] / 1000:5.1f}s pass={int(row['passed'])} q={[q['field'] for q in row['questions']]}", flush=True)
    summ = summarize(rows)
    summ.update({"set": a.set, "tag": a.tag, "ask_policy": os.environ.get("PIPELINE_ASK_POLICY", "essential"),
                 "select_rule": os.environ.get("PIPELINE_SELECT_RULE", "agree"), "rag_backend": os.environ.get("RAG_BACKEND", "field"),
                 "select_mode": os.environ.get("PIPELINE_SELECT_MODE", "select"), "choose_style": os.environ.get("PIPELINE_CHOOSE_STYLE", "confidence"),
                 "choose_threshold": os.environ.get("PIPELINE_CHOOSE_THRESHOLD", "0.8"),
                 "llm_calls": fails["total"], "llm_failures": fails["n"], "gemini": bool(a.gemini),
                 "clarity_mode": os.environ.get("PIPELINE_CLARITY", "off"),
                 "clarity_bounds": [os.environ.get("PIPELINE_CLARITY_LO", "0.1"), os.environ.get("PIPELINE_CLARITY_HI", "0.9")],
                 "cron_command": os.environ.get("PIPELINE_CRON_COMMAND", "example"),
                 "clarity_sources": dict(collections.Counter((r["clarity"] or {}).get("source", "無") for r in rows)),
                 "normalize_backends": dict(collections.Counter(str(r["normalize_backend"]) for r in rows)),
                 "gemini_cache": gstat})
    if a.gemini:
        from core import gemini_client
        summ["gemini_usage"] = gemini_client.usage()   # 呼叫次數與 token 數，換算實際花費用
    json.dump(summ, open(os.path.join(HERE, "results", f"summary_dev_{a.set}_{a.tag}.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(json.dumps(summ, ensure_ascii=False, indent=1))


def _gemini_fn(a):
    """正規化用的 Gemini 函式（2026-11-22）。--gemini-cache 時把回應存檔，同一份 prompt＋原句第二次直接讀檔：
    比較「開／關」時兩次用同一份 Gemini 結果，只花一次的呼叫。只給 --gemini-cache 不給 --gemini＝只讀檔、不呼叫。"""
    stat = {"hits": 0, "calls": 0, "misses_without_gemini": 0} if a.gemini_cache else None
    if not a.gemini_cache:
        return (pllm.gemini_llm if a.gemini else None), stat
    import hashlib
    cache = {}
    if os.path.exists(a.gemini_cache):
        for line in open(a.gemini_cache, encoding="utf-8"):
            if line.strip():
                j = json.loads(line)
                cache[j["key"]] = j["out"]

    def g(system, user):
        key = hashlib.sha256((system + "\0" + user).encode("utf-8")).hexdigest()
        if key in cache:
            stat["hits"] += 1
            return cache[key]
        if not a.gemini:
            stat["misses_without_gemini"] += 1
            return None
        stat["calls"] += 1
        out = pllm.gemini_llm(system, user)
        if out is not None:
            cache[key] = out
            with open(a.gemini_cache, "a", encoding="utf-8") as fc:
                fc.write(json.dumps({"key": key, "out": out}, ensure_ascii=False) + "\n")
        return out
    return g, stat


def summarize(rows):
    ans = [r for r in rows if r["score"]["kind"] == "answer"]
    ask = [r for r in rows if r["score"]["kind"] == "must_ask"]
    lat = sorted(r["latency_ms"] for r in rows)
    src = collections.Counter(_source(q["field"]) for r in ans if r["score"]["asked"] for q in r["questions"])
    return {"n": len(rows), "n_answer": len(ans), "n_must_ask": len(ask),
            "pass_answer": sum(r["passed"] for r in ans), "pass_rate_answer": round(sum(r["passed"] for r in ans) / max(1, len(ans)), 3),
            "image_ok": sum(r["score"]["checks"]["image"] for r in ans),
            "false_ask": sum(r["score"]["asked"] for r in ans), "false_ask_sources": dict(src),
            "must_ask_recall": round(sum(r["passed"] for r in ask) / max(1, len(ask)), 3),
            "latency_p50_ms": lat[len(lat) // 2] if lat else None, "latency_p90_ms": lat[int(0.9 * (len(lat) - 1))] if lat else None}


def compare(set_, a, b):
    """逐題配對：答題（需直接回答）用 passed、反問題用 passed，McNemar 精確檢定。"""
    from scipy.stats import binomtest
    A = {json.loads(l)["id"]: json.loads(l) for l in open(os.path.join(HERE, "results", f"dev_{set_}_{a}.jsonl"), encoding="utf-8")}
    B = {json.loads(l)["id"]: json.loads(l) for l in open(os.path.join(HERE, "results", f"dev_{set_}_{b}.jsonl"), encoding="utf-8")}
    ids = [i for i in A if i in B]
    for kind in ("answer", "must_ask"):
        ks = [i for i in ids if A[i]["score"]["kind"] == kind]
        x = sum(B[i]["passed"] and not A[i]["passed"] for i in ks)
        y = sum(A[i]["passed"] and not B[i]["passed"] for i in ks)
        p = binomtest(min(x, y), x + y, 0.5).pvalue if x + y else 1.0
        print(f"{kind}：{a} {sum(A[i]['passed'] for i in ks)}/{len(ks)} → {b} {sum(B[i]['passed'] for i in ks)}/{len(ks)}；"
              f"只有 {b} 對 {x}、只有 {a} 對 {y}，McNemar p={p:.4f}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", choices=list(SETS), default="scenario")
    ap.add_argument("--tag")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--compare", nargs=2)
    ap.add_argument("--gemini", action="store_true", help="正規化走 Gemini（正式路徑，會花錢）")
    ap.add_argument("--max-gemini-calls", type=int, help="Gemini 呼叫上限（--gemini 必填）")
    ap.add_argument("--gemini-cache", help="Gemini 回應存檔的路徑（同一句話第二次直接讀檔；不加 --gemini 時只讀不呼叫）")
    ap.add_argument("--rescore", help="用目前的評分重算某個 tag 的結果檔（不重跑模型）")
    a = ap.parse_args()
    if a.rescore:
        p = os.path.join(HERE, "results", f"dev_{a.set}_{a.rescore}.jsonl")
        tasks = {t["id"]: t for t in load_tasks(a.set)}
        rows = [json.loads(x) for x in open(p, encoding="utf-8")]
        for r in rows:
            r["score"] = score(tasks[r["id"]], r["pred"])
            r["passed"] = r["score"]["passed"]
        with open(p, "w", encoding="utf-8") as f:
            f.writelines(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
        sp = os.path.join(HERE, "results", f"summary_dev_{a.set}_{a.rescore}.json")
        summ = json.load(open(sp, encoding="utf-8"))
        summ.update(summarize(rows))
        json.dump(summ, open(sp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(json.dumps(summarize(rows), ensure_ascii=False))
    elif a.compare:
        compare(a.set, *a.compare)
    else:
        run(a)
