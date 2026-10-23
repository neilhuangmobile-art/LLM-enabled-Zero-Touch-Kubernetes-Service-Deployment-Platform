"""
從公開的 Kubernetes 部署設定統計出幾種典型「部署風格」（2026-10-23，個人化用）。

資料來源：Hugging Face `substratusai/the-stack-yaml-k8s`——從 BigCode 的 The Stack（GitHub 公開程式碼）
篩出的真實 Kubernetes YAML（約 27 萬個檔案）。授權標示為 other，依各原始 repo 的授權；
這個腳本**只產出統計數字**（比例、中位數），不保存、不轉發任何檔案內容。

產出：agents/style_priors.json（幾 KB，進版控）。網站平常只讀這個 JSON，不需要 pyarrow／sklearn。
這是一次性的分析，換資料或想重算時再跑：

    C:\\zt-k8s\\.venv\\Scripts\\python.exe scripts\\build_style_priors.py

做法（固定規則，不用 AI，見 docs/handoff/claude-memory/2026-10-07-personalization-decision.md）：
- 只取 kind: Deployment，每個取第一個容器：副本數、有沒有設資源（requests 或 limits）、記憶體與 CPU、
  有沒有健康檢查、映像檔有沒有釘版本（不是 latest 也不是沒寫 tag）。
- 特徵標準化後用 k-means（k=3、固定亂數種子）分群，依各群特徵命名，記比例與中位數。
"""
import json
import math
import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
OUT = os.path.join(ROOT, "agents", "style_priors.json")   # 不能放 agents/data/：.gitignore 的 data/ 會把它排除
CACHE = os.path.join(ROOT, "data", "hf_cache")          # data/ 不進版控
REPO = "substratusai/the-stack-yaml-k8s"

MEM_UNITS = {"ki": 1 / 1024, "mi": 1, "gi": 1024, "ti": 1024 * 1024, "k": 1000 / 1024 ** 2, "m": 1000 ** 2 / 1024 ** 2,
             "g": 1000 ** 3 / 1024 ** 2, "": 1 / 1024 ** 2}


def mem_mib(v):
    m = re.fullmatch(r"\s*([0-9.]+)\s*([KkMmGgTt]i?)?\s*", str(v or ""))
    if not m:
        return None
    unit = (m.group(2) or "").lower()
    try:
        return float(m.group(1)) * MEM_UNITS.get(unit, 0) or None
    except ValueError:
        return None


def cpu_m(v):
    s = str(v or "").strip()
    try:
        return float(s[:-1]) if s.endswith("m") else float(s) * 1000 if s else None
    except ValueError:
        return None


def features(doc):
    """一個 Deployment → 特徵 dict；不合格回 None。"""
    if not isinstance(doc, dict) or doc.get("kind") != "Deployment":
        return None
    spec = doc.get("spec") or {}
    cs = (((spec.get("template") or {}).get("spec") or {}).get("containers")) or []
    if not isinstance(cs, list) or not cs or not isinstance(cs[0], dict):
        return None
    c = cs[0]
    try:
        replicas = int(spec.get("replicas", 1))
    except (TypeError, ValueError):
        return None
    if not 0 <= replicas <= 100:
        return None
    res = c.get("resources") or {}
    req, lim = (res.get("requests") or {}) if isinstance(res, dict) else {}, (res.get("limits") or {}) if isinstance(res, dict) else {}
    req = req if isinstance(req, dict) else {}
    lim = lim if isinstance(lim, dict) else {}
    image = str(c.get("image") or "")
    tag = image.rsplit(":", 1)[1] if ":" in image.rsplit("/", 1)[-1] else ""
    return {"replicas": replicas,
            "has_resources": bool(req or lim),
            "mem_mib": mem_mib(req.get("memory") or lim.get("memory")),
            "cpu_m": cpu_m(req.get("cpu") or lim.get("cpu")),
            "has_probe": bool(c.get("readinessProbe") or c.get("livenessProbe")),
            "pinned": bool(tag) and tag != "latest" and "{" not in image}


def load_rows():
    from huggingface_hub import snapshot_download
    import pyarrow.parquet as pq
    path = snapshot_download(REPO, repo_type="dataset", cache_dir=CACHE, allow_patterns=["*.parquet"])
    files = [os.path.join(dp, f) for dp, _, fs in os.walk(path) for f in fs if f.endswith(".parquet")]
    for f in sorted(files):
        for batch in pq.ParquetFile(f).iter_batches(columns=["content"], batch_size=2000):
            for text in batch.column(0).to_pylist():
                yield text


def median(xs):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def main():
    import yaml
    import numpy as np
    from sklearn.cluster import KMeans
    t0 = time.time()
    files = docs_seen = 0
    rows = []
    for text in load_rows():
        files += 1
        if "Deployment" not in (text or ""):
            continue
        try:
            for doc in yaml.safe_load_all(text):
                docs_seen += 1
                f = features(doc)
                if f:
                    rows.append(f)
        except Exception:
            continue    # Helm 範本（{{ }}）等不是合法 YAML 的檔案略過
    print(f"讀了 {files} 個檔案、{docs_seen} 份 YAML 文件，可用的 Deployment {len(rows)} 個（{time.time() - t0:.0f} 秒）")
    if len(rows) < 1000:
        raise SystemExit("可用的 Deployment 太少，不產生結果。")

    X = np.array([[math.log2(1 + r["replicas"]), float(r["has_resources"]), float(r["has_probe"]), float(r["pinned"])] for r in rows])
    mu, sd = X.mean(axis=0), X.std(axis=0) + 1e-9
    km = KMeans(n_clusters=3, n_init=10, random_state=42).fit((X - mu) / sd)
    groups = []
    for k in range(3):
        g = [r for r, lab in zip(rows, km.labels_) if lab == k]
        res_rate = sum(r["has_resources"] for r in g) / len(g)
        probe_rate = sum(r["has_probe"] for r in g) / len(g)
        groups.append({
            "share": round(len(g) / len(rows), 3),
            "count": len(g),
            "replicas_median": median([r["replicas"] for r in g]),
            "mem_mib_median": median([r["mem_mib"] for r in g]),
            "cpu_m_median": median([r["cpu_m"] for r in g]),
            "resources_rate": round(res_rate, 3),
            "probe_rate": round(probe_rate, 3),
            "pinned_rate": round(sum(r["pinned"] for r in g) / len(g), 3),
            "center": [round(float(v), 4) for v in km.cluster_centers_[k]],
        })
    # 依特徵命名：設資源與健康檢查比例最高的是正式環境型，最低的是實驗型，其餘中間型
    order = sorted(range(3), key=lambda i: groups[i]["resources_rate"] + groups[i]["probe_rate"])
    names = {order[0]: ("experimental", "實驗型：多半單副本、沒設資源上限"), order[1]: ("middle", "中間型"),
             order[2]: ("production", "正式環境型：有設資源與健康檢查")}
    for i, g in enumerate(groups):
        g["id"], g["label"] = names[i]
    out = {"source": f"https://huggingface.co/datasets/{REPO}",
           "license_note": "依各原始 repo 授權；本檔只含統計數字，不含任何檔案內容",
           "generated_at": time.strftime("%Y-%m-%d"),
           "files_read": files, "deployments": len(rows),
           "feature_order": ["log2(1+replicas)", "has_resources", "has_probe", "pinned_tag"],
           "feature_mean": [round(float(v), 4) for v in mu], "feature_std": [round(float(v), 4) for v in sd],
           "styles": sorted(groups, key=lambda g: g["id"])}
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(json.dumps({k: v for k, v in out.items() if k != "styles"}, ensure_ascii=False))
    for g in out["styles"]:
        print(g["label"], {k: g[k] for k in ("share", "replicas_median", "mem_mib_median", "cpu_m_median", "resources_rate", "probe_rate", "pinned_rate")})


if __name__ == "__main__":
    main()
