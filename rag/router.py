"""
RAG 路由（2026-11-15）：依「最終判定的動作」與「要補的欄位」決定查哪些資料庫，或不查。

依據：規劃稿第六節的路由表（Adaptive-RAG、Lightweight Query Routing 的做法）。
動作名稱沿用 web_demo 的意圖代碼；規則層共有 30 種動作，這裡只列需要檢索的，其餘一律不查。
"""
from typing import List, Optional

NO_RETRIEVAL = {
    "scale", "update_image", "rollback", "delete", "list_pods", "list_deployments", "describe_pod",
    "pod_health", "describe_deployment", "gitops_log", "cluster_metrics", "healer_scan", "healer_fix",
    "healer_auto_fix", "chitchat",
}


def route(action: str, field: Optional[str] = None, base_complete: bool = False,
          has_extra: bool = False, scenario: bool = False) -> List[str]:
    """回傳要查的資料庫名稱（field_retriever 的 collection）；空清單＝不查。"""
    if action == "deploy":
        if base_complete and not has_extra and not scenario:
            return []                                   # 快路徑
        cols = []
        if field is None or field in {"image", "port", "volume", "env", "memory", "cpu", "replicas"}:
            cols += ["catalog", "examples"]
        if has_extra or scenario or field in (None, "extra"):
            cols += ["requirements"]
        cols.append("lessons")
        return cols
    if action in ("troubleshoot", "healer_diagnose"):
        return ["troubleshoot", "knowledge"]
    if action in ("qa", "concept"):
        return ["knowledge"]
    if action in NO_RETRIEVAL or action.startswith(("as_", "schedule", "weekly_", "personalize")):
        return []
    return ["knowledge"]
