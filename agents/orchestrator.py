"""
agents/orchestrator.py
代理協調器 — 整合安全、成本、效能三個代理，對 K8s YAML 進行全面評估。

架構：
    orchestrator
    ├── security_agent  （安全掃描，可阻斷部署）
    ├── cost_agent      （資源成本分析）
    └── perf_agent      （效能與 HPA 建議）

工作流程：
    1. 接收 LLM 產生的 manifest（或用戶提供的 YAML）
    2. 並行執行三個代理的分析
    3. 整合結果，輸出最終決策（approve / warn / block）
    4. 可選：將結果儲存為 JSON 報告

研究報告依據：
    「補救代理在偵測到故障後生成修復程式碼，
     隨後將其交由「驗證代理」在沙盒環境中測試，
     最後才提交至 Git 倉庫」
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import time
import argparse
import concurrent.futures
from typing import Dict, Any, List, Optional

# ════════════════════════════════════════════════════════════════
# 決策閾值設定
# ════════════════════════════════════════════════════════════════

# 安全分數低於此值 → block（不允許部署）
SECURITY_BLOCK_THRESHOLD = 60

# 安全分數在此範圍 → warn（允許但警告）
SECURITY_WARN_THRESHOLD  = 80

# 成本問題 severity=critical → block
# 效能問題 severity=high 超過此數 → warn
PERF_HIGH_ISSUE_WARN_LIMIT = 2


# ════════════════════════════════════════════════════════════════
# 代理載入
# ════════════════════════════════════════════════════════════════

def _run_security(manifest: Dict) -> Dict:
    try:
        from agents.security_agent import scan
        return scan(manifest)
    except Exception as e:
        return {"agent": "security_agent", "ok": False, "score": 0,
                "issues": [], "patches": [], "summary": f"代理執行失敗：{e}"}

def _run_cost(manifest: Dict) -> Dict:
    try:
        from agents.cost_agent import analyze
        return analyze(manifest)
    except Exception as e:
        return {"agent": "cost_agent", "ok": True, "issues": [],
                "cost_estimate": {}, "summary": f"代理執行失敗：{e}"}

def _run_perf(manifest: Dict) -> Dict:
    try:
        from agents.perf_agent import analyze
        return analyze(manifest)
    except Exception as e:
        return {"agent": "perf_agent", "ok": True, "issues": [],
                "hpa_yaml": None, "summary": f"代理執行失敗：{e}"}


# ════════════════════════════════════════════════════════════════
# 決策邏輯
# ════════════════════════════════════════════════════════════════

def _make_decision(security: Dict, cost: Dict, perf: Dict) -> Dict:
    """
    根據三個代理的結果做出最終決策。

    回傳：{
        "decision": "approve" | "warn" | "block",
        "reason":   str,
        "blockers": [str],   # 阻斷原因
        "warnings": [str],   # 警告列表
    }
    """
    blockers: List[str] = []
    warnings: List[str] = []

    # ── 安全決策 ──────────────────────────────────────────────
    sec_score = security.get("score", 100)
    sec_issues = security.get("issues", [])

    critical_sec = [i for i in sec_issues if i.get("severity") == "critical"]
    if critical_sec:
        for i in critical_sec:
            blockers.append(f"[安全] {i['message']}")

    if sec_score < SECURITY_BLOCK_THRESHOLD:
        blockers.append(f"[安全] 安全分數過低（{sec_score}/100 < {SECURITY_BLOCK_THRESHOLD}）")
    elif sec_score < SECURITY_WARN_THRESHOLD:
        warnings.append(f"[安全] 安全分數偏低（{sec_score}/100），建議在部署前修復 HIGH 問題")

    # ── 成本決策 ──────────────────────────────────────────────
    cost_issues = cost.get("issues", [])
    critical_cost = [i for i in cost_issues if i.get("severity") == "critical"]
    for i in critical_cost:
        blockers.append(f"[成本] {i['message']}")

    high_cost = [i for i in cost_issues if i.get("severity") == "high"]
    for i in high_cost:
        warnings.append(f"[成本] {i['message']}")

    # ── 效能決策 ──────────────────────────────────────────────
    perf_issues = perf.get("issues", [])
    high_perf   = [i for i in perf_issues if i.get("severity") == "high"]
    if len(high_perf) > PERF_HIGH_ISSUE_WARN_LIMIT:
        warnings.append(
            f"[效能] {len(high_perf)} 個 HIGH 效能問題（如單副本、缺少健康探針）"
        )
    for i in high_perf[:PERF_HIGH_ISSUE_WARN_LIMIT]:
        warnings.append(f"[效能] {i['message']}")

    # ── 最終決策 ──────────────────────────────────────────────
    if blockers:
        decision = "block"
        reason   = f"因 {len(blockers)} 個阻斷性問題，部署被拒絕"
    elif warnings:
        decision = "warn"
        reason   = f"發現 {len(warnings)} 個警告，建議處理後再部署"
    else:
        decision = "approve"
        reason   = "所有代理檢查通過，可以部署"

    return {
        "decision": decision,
        "reason":   reason,
        "blockers": blockers,
        "warnings": warnings,
    }


# ════════════════════════════════════════════════════════════════
# 主要協調函數
# ════════════════════════════════════════════════════════════════

def orchestrate(manifest: Any,
                parallel: bool = True,
                save_report: bool = False,
                report_path: Optional[str] = None) -> Dict:
    """
    對 K8s manifest 執行全代理協調評估。

    參數：
        manifest:    dict 或 YAML 字串
        parallel:    是否並行執行三個代理（預設 True）
        save_report: 是否儲存 JSON 報告
        report_path: 報告儲存路徑（None = 自動命名）

    回傳：{
        "decision":  "approve" | "warn" | "block",
        "reason":    str,
        "blockers":  [str],
        "warnings":  [str],
        "agents": {
            "security": {...},
            "cost":     {...},
            "perf":     {...},
        },
        "elapsed_ms":  float,
        "report_path": str | None,
    }
    """
    t0 = time.time()

    # 解析 YAML 字串
    if isinstance(manifest, str):
        import yaml
        try:
            manifest = yaml.safe_load(manifest)
        except Exception as e:
            return {
                "decision":  "block",
                "reason":    f"YAML 解析失敗：{e}",
                "blockers":  [f"YAML 解析失敗：{e}"],
                "warnings":  [],
                "agents":    {},
                "elapsed_ms": 0,
                "report_path": None,
            }

    # 執行三個代理
    if parallel:
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            future_sec  = executor.submit(_run_security, manifest)
            future_cost = executor.submit(_run_cost,     manifest)
            future_perf = executor.submit(_run_perf,     manifest)
            sec_result  = future_sec.result()
            cost_result = future_cost.result()
            perf_result = future_perf.result()
    else:
        sec_result  = _run_security(manifest)
        cost_result = _run_cost(manifest)
        perf_result = _run_perf(manifest)

    # 整合決策
    decision = _make_decision(sec_result, cost_result, perf_result)
    elapsed  = (time.time() - t0) * 1000

    result = {
        **decision,
        "agents": {
            "security": sec_result,
            "cost":     cost_result,
            "perf":     perf_result,
        },
        "elapsed_ms":  round(elapsed, 1),
        "report_path": None,
    }

    # 儲存報告
    if save_report:
        if report_path is None:
            from core.config import REPORTS_DIR
            ts           = time.strftime("%Y%m%d_%H%M%S")
            app_name     = manifest.get("metadata", {}).get("name", "unknown")
            report_path  = os.path.join(REPORTS_DIR, f"agent_report_{app_name}_{ts}.json")

        os.makedirs(os.path.dirname(report_path), exist_ok=True)
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        result["report_path"] = report_path
        print(f"[Orchestrator] 報告已儲存：{report_path}")

    return result


def print_result(result: Dict) -> None:
    """友善列印協調結果。"""
    from core.config import ensure_utf8_output
    ensure_utf8_output()

    decision  = result["decision"]
    icons     = {"approve": "✓", "warn": "⚠", "block": "✗"}
    icon      = icons.get(decision, "?")
    elapsed   = result.get("elapsed_ms", 0)

    print(f"\n{'='*65}")
    print(f"{icon}  決策：{decision.upper()}  （耗時 {elapsed:.0f}ms）")
    print(f"   {result['reason']}")
    print(f"{'='*65}")

    if result.get("blockers"):
        print(f"\n阻斷原因（{len(result['blockers'])} 項）：")
        for b in result["blockers"]:
            print(f"  ✗ {b}")

    if result.get("warnings"):
        print(f"\n警告（{len(result['warnings'])} 項）：")
        for w in result["warnings"]:
            print(f"  ⚠ {w}")

    agents = result.get("agents", {})
    print(f"\n代理摘要：")
    for name, agent_result in agents.items():
        summary = agent_result.get("summary", "（無摘要）")
        print(f"  [{name:12s}] {summary}")

    if result.get("report_path"):
        print(f"\n完整報告：{result['report_path']}")

    # HPA 建議
    hpa = agents.get("perf", {}).get("hpa_yaml")
    if hpa and decision != "block":
        import yaml
        print(f"\n建議的 HPA 配置（可自行套用）：")
        print(yaml.dump(hpa, allow_unicode=True, default_flow_style=False))


# ════════════════════════════════════════════════════════════════
# CLI
# ════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    from core.config import ensure_utf8_output; ensure_utf8_output()
    parser = argparse.ArgumentParser(description="K8s 多代理協調評估")
    parser.add_argument("file",          nargs="?",           help="YAML 檔案路徑（不指定則使用範例）")
    parser.add_argument("--save-report", action="store_true", help="儲存 JSON 報告")
    parser.add_argument("--no-parallel", action="store_true", help="循序執行代理（預設並行）")
    parser.add_argument("--report-path", default=None,        help="報告儲存路徑")
    args = parser.parse_args()

    if args.file:
        import yaml
        with open(args.file, encoding="utf-8") as f:
            manifest = yaml.safe_load(f)
    else:
        # 範例：有安全問題 + 缺少 probe + 單副本
        manifest = {
            "apiVersion": "apps/v1",
            "kind":       "Deployment",
            "metadata":   {"name": "demo-app", "namespace": "default"},
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": {"app": "demo-app"}},
                "template": {
                    "metadata": {"labels": {"app": "demo-app"}},
                    "spec": {
                        "containers": [{
                            "name":  "app",
                            "image": "nginx:latest",
                            "securityContext": {"privileged": True},
                            "resources": {
                                "requests": {"memory": "128Mi", "cpu": "100m"},
                                "limits":   {"memory": "256Mi", "cpu": "500m"},
                            },
                        }]
                    }
                }
            }
        }

    result = orchestrate(
        manifest,
        parallel     = not args.no_parallel,
        save_report  = args.save_report,
        report_path  = args.report_path,
    )
    print_result(result)
