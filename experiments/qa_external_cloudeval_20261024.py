"""
qa_external_cloudeval_20261024.py
外部基準測試：用第三方的 CloudEval-YAML（NeurIPS 2023 研討會；Hugging Face ai4cloud/CloudEval-YAML，MIT 授權）
評估這個平台的部署解析，回應「自己出題自己考」的質疑。

範圍（如實說明）：
- CloudEval 原本評的是「生成完整 YAML」，這個平台只做「一句話 → 單一服務的部署規格」，所以只取標準答案是
  「單一容器 Deployment」的題目（20 題），比對平台解析出的欄位，不是完整的 CloudEval 分數。
- 比對欄位：映像檔、副本數、容器 port、名稱。標準答案裡標了 `# *` 的欄位代表任何值都算對，不計分；
  標準答案沒寫的欄位也不計分。
- 每題跑兩次：英文題目（question_simplified）與 CloudEval 附的中文翻譯（question_translated，簡體）。

執行前提：web_demo.py（127.0.0.1:5050）與 core/model_server.py 都在跑。會註冊一個測試帳號，跑完由人工刪除。
輸出：docs/qa_external_cloudeval_20261024_results.jsonl、docs/qa_external_cloudeval_20261024_summary.json
"""
import json
import os
import re
import sys
import time

import requests
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from core.config import ensure_utf8_output
ensure_utf8_output()

BASE = "http://127.0.0.1:5050"
USER, PASS = "qa-cloudeval-20261024", "qacloudeval20261024"
OUT = os.path.join(ROOT, "docs", "qa_external_cloudeval_20261024_results.jsonl")
SUMMARY = os.path.join(ROOT, "docs", "qa_external_cloudeval_20261024_summary.json")
FIELDS = ("image", "replicas", "container_port", "name")


_SECRET_LIKE_RE = re.compile(r"((?:password|passwd|secret|token|api[_-]?key)\s*:\s*)(\S+)", re.IGNORECASE)


def mask_secrets(text: str) -> str:
    """題目裡有些附了 Kubernetes Secret 的範例值（公開資料集的示範內容，不是我們的金鑰）。
    存結果前遮蔽，避免 gitleaks 擋下 commit，也避免看起來像金鑰的字串進版控。"""
    return _SECRET_LIKE_RE.sub(lambda m: m.group(1) + "<已遮蔽>", text or "")


def load_dataset():
    from huggingface_hub import snapshot_download
    import pyarrow.parquet as pq
    p = snapshot_download("ai4cloud/CloudEval-YAML", repo_type="dataset", cache_dir=os.path.join(ROOT, "data", "hf_cache"),
                          allow_patterns=["*.parquet"])
    rows = []
    for d, _, fs in os.walk(p):
        for f in sorted(fs):
            if f.endswith(".parquet"):
                rows += pq.read_table(os.path.join(d, f)).to_pylist()
    return rows


def _wild(line_text: str) -> bool:
    return bool(re.search(r"#\s*\*", line_text or ""))


def expected_fields(ref: str):
    """標準答案 → 要比對的欄位（標 # * 的不算）。不是單一容器 Deployment 回 None。"""
    try:
        docs = [d for d in yaml.safe_load_all(ref) if isinstance(d, dict)]
    except Exception:
        return None
    deps = [d for d in docs if d.get("kind") == "Deployment"]
    if len(deps) != 1:
        return None
    spec = deps[0].get("spec") or {}
    cs = ((spec.get("template") or {}).get("spec") or {}).get("containers") or []
    if len(cs) != 1:
        return None
    lines = ref.splitlines()

    def line_of(pattern):
        return next((l for l in lines if re.search(pattern, l)), "")
    exp = {}
    c = cs[0]
    if c.get("image") and not _wild(line_of(r"^\s*-?\s*image\s*:")):
        exp["image"] = str(c["image"]).strip()
    if "replicas" in spec and not _wild(line_of(r"^\s*replicas\s*:")):
        exp["replicas"] = int(spec["replicas"])
    ports = c.get("ports") or []
    if ports and ports[0].get("containerPort") and not _wild(line_of(r"containerPort\s*:")):
        exp["container_port"] = int(ports[0]["containerPort"])
    name = (deps[0].get("metadata") or {}).get("name")
    if name and not _wild(line_of(r"^\s*name\s*:\s*" + re.escape(str(name)))):
        exp["name"] = str(name)
    return exp


def norm_image(s: str) -> str:
    s = str(s or "").strip().strip('"\'').lower()
    s = re.sub(r"^(docker\.io/)?(library/)?", "", s)
    return s if ":" in s.rsplit("/", 1)[-1] else s + ":latest"


def judge(exp: dict, parsed: dict) -> dict:
    got = {"image": parsed.get("image"), "replicas": parsed.get("pods"),
           "container_port": parsed.get("container_port") or parsed.get("port"), "name": parsed.get("app_name")}
    res = {}
    for k, v in exp.items():
        g = got.get(k)
        if k == "image":
            res[k] = norm_image(g) == norm_image(v)
        elif k in ("replicas", "container_port"):
            try:
                res[k] = int(g) == int(v)
            except (TypeError, ValueError):
                res[k] = False
        else:
            res[k] = str(g or "").lower() == str(v).lower()
    return res, got


def login():
    s = requests.Session()
    s.post(f"{BASE}/auth/register", data={"username": USER, "password": PASS, "confirm": PASS})
    if s.get(f"{BASE}/api/status").status_code != 200:
        s.post(f"{BASE}/auth/login", data={"username": USER, "password": PASS})
    if s.get(f"{BASE}/api/status").status_code != 200:
        raise SystemExit("登入失敗")
    return s


def parse(s, text):
    waited = 0
    for _ in range(20):
        t0 = time.time()
        r = s.post(f"{BASE}/api/deploy/parse", json={"input": text}, timeout=180)
        ms = round((time.time() - t0) * 1000, 1)
        if r.status_code == 429:
            w = max(1, int(r.headers.get("Retry-After") or 6))
            waited += w
            time.sleep(w)
            continue
        try:
            out = r.json()
        except Exception:
            out = {"_raw": r.text[:300]}
        return r.status_code, out, ms, waited
    return 429, {}, 0, waited


def main():
    rows = load_dataset()
    cases = [(r, expected_fields(r["reference_code"])) for r in rows]
    cases = [(r, e) for r, e in cases if e]
    print(f"CloudEval-YAML 共 {len(rows)} 題，單一容器 Deployment {len(cases)} 題")
    s = login()
    out = []
    with open(OUT, "w", encoding="utf-8") as f:
        for r, exp in cases:
            for lang, q in (("en", r["question_simplified"]), ("zh", r["question_translated"])):
                if not q:
                    continue
                st, o, ms, waited = parse(s, q)
                parsed = o.get("parsed") or {}
                if isinstance(parsed.get("output"), dict):
                    parsed = {**parsed, **parsed["output"]}
                res, got = judge(exp, parsed)
                row = {"problem_key": r["problem_key"], "lang": lang, "input": mask_secrets(q), "expected": exp, "got": got,
                       "field_correct": res, "all_correct": all(res.values()) if res else None,
                       "status": st, "error": o.get("error"), "latency_ms": ms, "rate_limit_wait_s": waited}
                out.append(row)
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
                f.flush()
                print(f"[{r['problem_key']} {lang}] {'OK ' if row['all_correct'] else 'NG '} {res} {ms:.0f}ms")
    summ = {"dataset": "ai4cloud/CloudEval-YAML (MIT)", "run_at": time.strftime("%Y-%m-%d %H:%M"),
            "problems": len(cases), "by_lang": {}}
    for lang in ("en", "zh"):
        rs = [x for x in out if x["lang"] == lang]
        fields = {}
        for k in FIELDS:
            judged = [x["field_correct"][k] for x in rs if k in x["field_correct"]]
            fields[k] = {"judged": len(judged), "correct": sum(judged)}
        lat = sorted(x["latency_ms"] for x in rs)
        summ["by_lang"][lang] = {"n": len(rs), "all_correct": sum(1 for x in rs if x["all_correct"]), "fields": fields,
                                 "latency_median_ms": lat[len(lat) // 2] if lat else None}
    json.dump(summ, open(SUMMARY, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(json.dumps(summ, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
