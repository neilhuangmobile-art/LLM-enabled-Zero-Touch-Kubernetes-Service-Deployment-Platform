"""
重建程式碼知識圖譜（graphify），輸出到 graphify-out/。

用法：
    python scripts/graphify_rebuild.py           # 完整重建
    python scripts/graphify_rebuild.py --check   # 只檢查現有的圖有沒有異常，不重建

為什麼要有這支腳本（2026-11-07）：
- 直接跑 graphify 會把 YAML、JSON 也當成程式碼，每個欄位都變成一個節點，第一次建圖
  12,519 個節點有 9,497 個是 YAML。排除清單寫在專案根目錄的 .graphifyignore，
  graphify 每次掃描都會讀，不會因為刪掉 graphify-out/ 就失效。
- 群組（community）名稱依「這個群組主要來自哪個檔案」決定（FILE_LABELS），不是照群組編號，
  程式改動後重新分群，名稱也會跟著正確的檔案走。
- 建完會做防呆檢查（check_graph）：又出現 YAML/JSON 節點、節點數暴增、或連線指向專案內
  不存在的節點，就報錯並且不覆蓋原本的圖。

不需要 API key、不呼叫 AI，只做程式碼的語法分析（AST），通常 1 分鐘內完成。
graphify 沒裝的話：uv tool install graphifyy
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "graphify-out"

# 節點數上限：目前約 3,000 個，超過這個數字代表又有資料檔被掃進來
MAX_NODES = 6000
# 圖裡只該有這些檔案類型的節點（.graphifyignore 已排除其他類型，這裡是第二道檢查；
# 用白名單而不是黑名單，新出現的檔案類型也擋得住）。"" 是沒有副檔名的 git hook，
# 以及 graphify 替外部套件建的節點。
ALLOWED_SUFFIXES = {".py", ".js", ".ps1", ""}

# 檔案 → 群組名稱。一個群組取「成員最多的非測試檔」來命名；
# 新增模組時在這裡補一行，沒列到的會用該檔案裡連線最多的函式命名。
FILE_LABELS = {
    "web_demo.py": "Web 主程式",
    "llama_client.py": "LLM 推理入口",
    "core/model_server.py": "Model Server",
    "core/gemini_client.py": "Gemini 翻譯層",
    "core/claude_client.py": "Claude 翻譯層",
    "core/config.py": "設定",
    "core/hardware.py": "硬體偵測",
    "core/workspace_store.py": "對話與專案儲存",
    "core/schedule_store.py": "預約任務",
    "core/push.py": "手機推播",
    "core/sleep_mode.py": "閒置休眠與喚醒",
    "core/autoscale_store.py": "分時擴縮設定",
    "core/event_store.py": "事件紀錄",
    "core/mobile_page.py": "手機監控頁",
    "agents/orchestrator.py": "三方代理審查",
    "agents/security_agent.py": "安全代理",
    "agents/cost_agent.py": "成本代理",
    "agents/perf_agent.py": "效能代理",
    "agents/report.py": "分析報告",
    "agents/personalize.py": "個人化部署建議",
    "agents/scenario.py": "情境判斷",
    "agents/timeslot.py": "分時時段解析",
    "agents/timeslot_learn.py": "建議時段學習",
    "agents/compose_import.py": "Compose 匯入",
    "agents/k8s_yaml_import.py": "K8s YAML 匯入",
    "guardian/yaml_validator.py": "YAML 驗證",
    "guardian/dry_run.py": "Dry-run 驗證",
    "gitops/manifest_writer.py": "Manifest 產生",
    "gitops/tenant_repo.py": "客戶部署 repo",
    "gitops/argocd_sync.py": "GitOps 同步",
    "gitops/rollback.py": "GitOps 回滾",
    "healer/diagnose.py": "Healer 診斷",
    "healer/remediate.py": "Healer 補救",
    "healer/pod_watcher.py": "Healer 監控",
    "cluster/remote_state.py": "客戶叢集回報狀態",
    "cluster/remote_view.py": "客戶叢集讀取",
    "cluster/remote_ops.py": "客戶叢集補救",
    "cluster/onboarding.py": "客戶叢集安裝包",
    "cluster/agent/reporter.py": "客戶叢集回報器",
    "rag/retriever.py": "RAG 檢索",
    "rag/build_index.py": "RAG 建索引",
    "rag/kb_manager.py": "RAG 知識庫管理",
    "rag/vector_store.py": "RAG 向量庫",
    "rag/eval_retrieval.py": "RAG 檢索評估",
    "observability/prometheus_client.py": "Prometheus 監控",
    "0_touch_generate_pods.py": "CLI 部署入口",
    "scripts/bench/common.py": "對照實驗腳本",
    "scripts/build_style_priors.py": "部署風格統計",
    "core/schedule_time.py": "預約時間解析",
    "core/web_security.py": "網站安全防護",
    "rag/reranker.py": "RAG 重排序",
    "enrich_dataset.py": "訓練資料擴充",
    "generate_finetune_data.py": "微調資料產生",
    "training/fetch_hf_dataset.py": "下載 HF 資料集",
    "research_benchmark.py": "研究基準測試",
    "static/push-client.js": "推播前端",
    "scripts/start_services.ps1": "服務啟動腳本",
    "scripts/install_autostart.ps1": "開機自動啟動",
    "scripts/git-hooks/pre-commit": "Git 提交前檢查",
    "scripts/graphify_rebuild.py": "知識圖譜重建",
}

# 群組的核心函式如果是這些（或是檔名），拿來區分同名群組沒有意義，改用編號
_GENERIC_HUBS = {"parametrize", "fixture", "env", "Any", "datetime", "pathlib", "pytest"}


# ── 找到裝了 graphify 的 Python ─────────────────────────────────────────────
def _ensure_graphify() -> None:
    try:
        import graphify  # noqa: F401
        return
    except ImportError:
        pass
    if os.environ.get("_GRAPHIFY_REEXEC"):
        sys.exit("找不到 graphify。請先執行：uv tool install graphifyy")
    try:
        tool_dir = subprocess.run(["uv", "tool", "dir"], capture_output=True, text=True,
                                  check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        sys.exit("找不到 graphify，也找不到 uv。請先安裝 uv，再執行：uv tool install graphifyy")
    sub = "Scripts/python.exe" if os.name == "nt" else "bin/python"
    py = Path(tool_dir) / "graphifyy" / sub
    if not py.exists():
        sys.exit("找不到 graphify。請先執行：uv tool install graphifyy")
    env = dict(os.environ, _GRAPHIFY_REEXEC="1")
    sys.exit(subprocess.run([str(py), __file__, *sys.argv[1:]], env=env).returncode)


# ── 防呆檢查 ───────────────────────────────────────────────────────────────
def _project_prefixes(nodes: list[dict]) -> set[str]:
    """專案內節點 id 的前綴（檔案路徑轉成的 id），用來分辨「外部套件」與「專案內缺漏」。"""
    prefixes = set()
    for n in nodes:
        sf = str(n.get("source_file") or "")
        if sf:
            prefixes.add(Path(sf).with_suffix("").as_posix().replace("/", "_").lower())
    return prefixes


def check_graph(nodes: list[dict], edges: list[dict]) -> list[str]:
    """回傳問題清單；空清單代表圖是正常的。"""
    problems = []
    bad = Counter(s for s in (Path(str(n.get("source_file") or "")).suffix.lower() for n in nodes)
                  if s not in ALLOWED_SUFFIXES)
    if bad:
        problems.append(f"圖裡出現非程式碼的節點 {dict(bad)}，請檢查 .graphifyignore 是否被改掉或刪掉")
    if len(nodes) > MAX_NODES:
        problems.append(f"節點數 {len(nodes)} 超過上限 {MAX_NODES}，可能有新的資料檔被掃進來")
    ids = {n["id"] for n in nodes}
    prefixes = _project_prefixes(nodes)
    internal_missing = Counter()
    for e in edges:
        for end in (e["source"], e["target"]):
            if end not in ids and any(end.lower().startswith(p + "_") for p in prefixes):
                internal_missing[end] += 1
    if internal_missing:
        top = ", ".join(f"{k}×{v}" for k, v in internal_missing.most_common(5))
        problems.append(f"有 {sum(internal_missing.values())} 條連線指向專案內不存在的節點：{top}")
    return problems


def _edge_summary(nodes: list[dict], edges: list[dict]) -> str:
    ids = {n["id"] for n in nodes}
    external = sum(1 for e in edges if e["source"] not in ids or e["target"] not in ids)
    loops = sum(1 for e in edges if e["source"] == e["target"])
    return (f"指向外部套件（json、os、pytest 等，不畫進圖是正確的）：{external} 條；"
            f"遞迴呼叫自己：{loops} 條")


# ── 群組命名 ───────────────────────────────────────────────────────────────
def file_display_name(path: str) -> str:
    """檔案路徑 → 群組名稱。"""
    if path in FILE_LABELS:
        return FILE_LABELS[path]
    p = Path(path)
    if p.name == "__init__.py":
        return f"{p.parent.as_posix() or '專案'} 套件"
    if path.startswith("tests/"):
        return "測試：" + p.stem.removeprefix("test_")
    if p.stem.startswith("qa_"):
        return "QA 腳本：" + p.stem.removeprefix("qa_")
    if p.stem.startswith("eval_"):
        return "模型評估：" + p.stem.removeprefix("eval_")
    return p.stem


def _main_file(G, members: list[str]) -> str:
    """群組的代表檔案：測試佔六成以上就用測試檔，否則用成員最多的正式程式檔。"""
    all_files, prod = Counter(), Counter()
    for m in members:
        sf = Path(str(G.nodes[m].get("source_file") or "")).as_posix()
        if sf and sf != ".":
            all_files[sf] += 1
            if not sf.startswith("tests/"):
                prod[sf] += 1
    total = sum(all_files.values())
    tests = total - sum(prod.values())
    pool = prod if prod and tests < total * 0.6 else all_files
    if not pool:
        return ""
    # 同票時不選 __init__.py（它通常只是匯出別的模組）
    return min(pool, key=lambda f: (-pool[f], Path(f).name == "__init__.py", f))


def label_communities(G, communities: dict[int, list[str]]) -> dict[int, str]:
    from graphify.cluster import label_communities_by_hub
    hub = label_communities_by_hub(G, communities)
    labels: dict[int, str] = {}
    used: Counter = Counter()
    for cid, members in sorted(communities.items(), key=lambda kv: -len(kv[1])):
        main = _main_file(G, members)
        base = file_display_name(main) if main else str(hub.get(cid, f"群組 {cid}"))
        used[base] += 1
        name = base
        if used[base] > 1:
            # 同一個檔案拆成好幾群（例如 web_demo.py），用群組的核心函式區分
            h = str(hub.get(cid, "")).removesuffix("()").lstrip(".")
            generic = (not h or h in _GENERIC_HUBS or Path(h).suffix in (".py", ".js")
                       or h == Path(main).stem)
            name = f"{base}（{used[base]}）" if generic else f"{base}：{h}"
        labels[cid] = name
    return labels


# ── 主流程 ─────────────────────────────────────────────────────────────────
def rebuild() -> int:
    from graphify.analyze import god_nodes, suggest_questions, surprising_connections
    from graphify.build import build_from_json
    from graphify.cli import _stamped_manifest_files
    from graphify.cluster import cluster, score_all
    from graphify.detect import detect, save_manifest
    from graphify.export import to_json
    from graphify.extract import collect_files, extract
    from graphify.report import generate

    os.chdir(ROOT)
    OUT.mkdir(exist_ok=True)
    det = detect(Path("."))
    code_files = []
    for f in det["files"].get("code", []):
        code_files.extend(collect_files(Path(f)) if Path(f).is_dir() else [Path(f)])
    print(f"程式碼檔案：{len(code_files)} 個（其他類型不納入）")
    ex = extract(code_files, cache_root=Path("."), root=Path("."))
    ex.setdefault("hyperedges", [])
    ex["input_tokens"] = ex["output_tokens"] = 0

    problems = check_graph(ex["nodes"], ex["edges"])
    if problems:
        print("\n圖譜異常，沒有覆蓋原本的圖：")
        for p in problems:
            print("  - " + p)
        return 1

    G = build_from_json(ex, root=".", directed=False)
    communities = cluster(G)
    cohesion = score_all(G, communities)
    labels = label_communities(G, communities)
    gods = god_nodes(G)
    surprises = surprising_connections(G, communities)
    questions = suggest_questions(G, communities, labels)

    # 完整重建本來就可能比舊圖小（刪了程式碼），所以 force=True；異常已經在上面擋掉
    to_json(G, communities, str(OUT / "graph.json"), force=True, community_labels=labels)
    report = generate(G, communities, cohesion, labels, gods, surprises, det,
                      {"input": 0, "output": 0}, ".", suggested_questions=questions)
    (OUT / "GRAPH_REPORT.md").write_text(report, encoding="utf-8")
    (OUT / ".graphify_labels.json").write_text(
        json.dumps({str(k): v for k, v in labels.items()}, ensure_ascii=False), encoding="utf-8")
    (OUT / ".graphify_python").write_text(sys.executable, encoding="utf-8")
    (OUT / ".graphify_root").write_text(str(ROOT), encoding="utf-8")
    corpus = det.get("all_files") or det["files"]
    save_manifest(_stamped_manifest_files(corpus, ex, Path("."), partial_source_files=set()),
                  root=".", scan_corpus={f for fl in corpus.values() for f in fl})
    # 舊版留下的排除設定會跟 .graphifyignore 打架，統一用 .graphifyignore
    (OUT / ".graphify_build.json").unlink(missing_ok=True)
    # graphify update 留下的「待套用名稱」，名稱已經由這支腳本重算，留著會在下次 update 時蓋回去
    (OUT / ".graphify_labels.pending.json").unlink(missing_ok=True)

    graphify_cmd = Path(sys.executable).parent / ("graphify.exe" if os.name == "nt" else "graphify")
    html = subprocess.run([str(graphify_cmd), "export", "html"], cwd=ROOT,
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    if html.returncode != 0:
        print("網頁圖產生失敗：" + (html.stderr or html.stdout)[-500:])
        return 1

    print(f"\n完成：{G.number_of_nodes()} 個節點、{G.number_of_edges()} 條連線、{len(communities)} 個群組")
    print(_edge_summary(ex["nodes"], ex["edges"]))
    print(f"輸出在 {OUT}（graph.html 用瀏覽器開）")
    return 0


def check_only() -> int:
    path = OUT / "graph.json"
    if not path.exists():
        print("還沒有圖譜，請執行：python scripts/graphify_rebuild.py")
        return 1
    g = json.loads(path.read_text(encoding="utf-8"))
    nodes, edges = g["nodes"], g.get("links") or g.get("edges") or []
    problems = check_graph(nodes, edges)
    # graphify update 會把有變動的群組改用英文函式名稱命名，這裡用同一套規則重算一次比對
    from graphify.build import build_from_json
    G = build_from_json(g, directed=bool(g.get("directed", False)))
    communities: dict[int, list[str]] = {}
    for n in nodes:
        if n.get("community") is not None:
            communities.setdefault(int(n["community"]), []).append(n["id"])
    expected = label_communities(G, communities)
    renamed = {n.get("community") for n in nodes
               if n.get("community") is not None
               and n.get("community_name") != expected.get(int(n["community"]))}
    if renamed:
        problems.append(f"{len(renamed)} 個群組的名稱不是依檔案命名的（通常是用了 graphify update），請重建")
    if problems:
        print("圖譜異常：")
        for p in problems:
            print("  - " + p)
        print("修正方式：python scripts/graphify_rebuild.py")
        return 1
    print(f"圖譜正常：{len(nodes)} 個節點、{len(edges)} 條連線")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    _ensure_graphify()
    sys.exit(check_only() if "--check" in sys.argv else rebuild())
