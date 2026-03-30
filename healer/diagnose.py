"""
healer/diagnose.py
LLM 根因分析 — 讀取 Pod 日誌與事件，請 LLM 診斷問題並給出建議。

輸入：pod_watcher.py 收集的 context dict（含日誌、事件、錯誤狀態）
輸出：結構化診斷結果（根因、嚴重度、補救建議）

研究報告依據：
    「與傳統基於規則的自動化系統相比，LLM 代理展現出更強的
     情境理解與根因分析能力。代理能夠分析運行時日誌與集群事件，
     識別如 CrashLoopBackOff、鏡像遺失或 Ingress 配置錯誤等問題
     的深層原因」
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import re

# 規則型快速分析（不需要 LLM，速度快）
_RULE_PATTERNS = [
    # (正則, root_cause, action, severity)
    (r"OOMKilled|out of memory|memory.*limit",
     "記憶體不足（OOMKilled）",
     "increase_memory",
     "high"),
    (r"ImagePullBackOff|ErrImagePull|image.*not found|manifest.*unknown",
     "映像拉取失敗，tag 不存在或倉庫無法存取",
     "fix_image",
     "high"),
    (r"CrashLoopBackOff",
     "容器反覆崩潰，需分析應用程式日誌",
     "analyze_logs",
     "high"),
    (r"ECONNREFUSED|connection refused|dial tcp.*refused",
     "依賴服務無法連線（資料庫/API 未啟動）",
     "check_dependencies",
     "medium"),
    (r"permission denied|unauthorized|403",
     "權限不足，可能是 RBAC 或掛載路徑問題",
     "fix_permissions",
     "medium"),
    (r"configmap.*not found|secret.*not found|no such file",
     "ConfigMap 或 Secret 不存在",
     "create_config",
     "high"),
    (r"liveness.*probe|readiness.*probe|probe failed",
     "健康檢查探針失敗",
     "fix_probe",
     "medium"),
    (r"port.*already in use|address already in use",
     "連接埠衝突",
     "fix_port_conflict",
     "medium"),
]


# ══════════════════════════════════════════════════════════════════
# 公開 API
# ══════════════════════════════════════════════════════════════════

def diagnose_issue(context: dict, use_llm: bool = True) -> dict:
    """
    分析 Pod 異常的根本原因。

    優先順序：
      1. 規則型快速分析（毫秒級，不需 LLM）
      2. LLM 深度分析（需要 model_server 在線）

    Args:
        context  : pod_watcher._trigger_heal() 傳入的 context dict
                   需包含 reason, logs, events, pod_name 等欄位
        use_llm  : False = 只用規則分析（測試用）

    Returns:
        {
            "pod_name"    : str,
            "reason"      : str,   # 原始 K8s 錯誤狀態
            "root_cause"  : str,   # 分析出的根本原因
            "severity"    : str,   # "high" / "medium" / "low"
            "action"      : str,   # 建議的補救動作代碼
            "suggestion"  : str,   # 人類可讀的補救建議
            "confidence"  : str,   # "rule" / "llm" / "unknown"
            "raw_llm"     : str,   # LLM 原始輸出（除錯用）
        }
    """
    pod_name = context.get("pod_name", "unknown")
    reason   = context.get("reason",   "Unknown")
    logs     = context.get("logs",     "")
    events   = context.get("events",   [])
    message  = context.get("message",  "")

    # 合併所有文字供分析
    combined_text = "\n".join([
        reason, message, logs,
        " ".join(e.get("message", "") for e in events),
    ]).lower()

    # ── 層 1：規則型快速分析 ─────────────────────────────────────
    rule_result = _rule_analyze(combined_text, reason)
    if rule_result["confidence"] == "rule" and not use_llm:
        rule_result["pod_name"] = pod_name
        return rule_result

    # ── 層 2：LLM 深度分析 ──────────────────────────────────────
    if use_llm:
        llm_result = _llm_analyze(context)
        if llm_result:
            llm_result["pod_name"] = pod_name
            # 如果規則分析有結果，補充 severity
            if rule_result["severity"] != "unknown":
                llm_result.setdefault("severity", rule_result["severity"])
            return llm_result

    # ── 層 3：無法分析，回傳規則結果或未知 ──────────────────────
    rule_result["pod_name"] = pod_name
    return rule_result


# ══════════════════════════════════════════════════════════════════
# 內部：規則型分析
# ══════════════════════════════════════════════════════════════════

def _rule_analyze(text: str, reason: str) -> dict:
    """用正規表示式快速比對已知錯誤模式。"""
    for pattern, root_cause, action, severity in _RULE_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return {
                "reason"    : reason,
                "root_cause": root_cause,
                "severity"  : severity,
                "action"    : action,
                "suggestion": _action_to_suggestion(action),
                "confidence": "rule",
                "raw_llm"   : "",
            }
    return {
        "reason"    : reason,
        "root_cause": f"無法從規則比對識別原因（{reason}）",
        "severity"  : "unknown",
        "action"    : "manual_inspect",
        "suggestion": "請手動執行 kubectl logs 和 kubectl describe pod 進行排查",
        "confidence": "unknown",
        "raw_llm"   : "",
    }


def _action_to_suggestion(action: str) -> str:
    _map = {
        "increase_memory"   : "增加 resources.limits.memory（建議至少翻倍），"
                              "例如從 256Mi 調整到 512Mi",
        "fix_image"         : "確認映像 tag 是否存在，"
                              "執行 docker pull <image> 驗證，"
                              "或檢查倉庫的 imagePullSecrets",
        "analyze_logs"      : "執行 kubectl logs <pod> --previous 查看崩潰前日誌，"
                              "尋找 Fatal/Panic/Exception 等錯誤",
        "check_dependencies": "確認依賴的資料庫/API 服務是否正常運行，"
                              "檢查對應 Service 和 Endpoints",
        "fix_permissions"   : "檢查 ServiceAccount RBAC 設定，"
                              "或確認掛載的 Volume 路徑權限",
        "create_config"     : "確認 ConfigMap/Secret 是否存在於同一 namespace，"
                              "執行 kubectl get configmap,secret -n <namespace>",
        "fix_probe"         : "調整 livenessProbe/readinessProbe 的 initialDelaySeconds，"
                              "給應用程式更多啟動時間",
        "fix_port_conflict" : "確認 containerPort 設定正確，"
                              "且沒有其他 Process 使用相同連接埠",
        "manual_inspect"    : "請手動執行 kubectl logs 和 kubectl describe pod 進行排查",
    }
    return _map.get(action, "請參考 kubectl describe pod 的輸出進行排查")


# ══════════════════════════════════════════════════════════════════
# 內部：LLM 分析
# ══════════════════════════════════════════════════════════════════

def _llm_analyze(context: dict) -> dict:
    """
    呼叫 LLM（透過 llama_client）進行深度根因分析。
    LLM 無法使用時靜默回傳 None。
    """
    try:
        from llama_client import ask_llama
    except ImportError:
        return None

    pod_name  = context.get("pod_name", "unknown")
    reason    = context.get("reason",   "Unknown")
    logs      = (context.get("logs", "") or "")[:800]   # 限制長度
    events    = context.get("events", [])
    container = context.get("container", "")
    restarts  = context.get("restart_count", 0)

    event_str = "; ".join(
        f"{e.get('reason','?')}: {e.get('message','')[:100]}"
        for e in events[:5]
    )

    prompt = f"""You are a Kubernetes SRE expert. Analyze this pod failure and respond in JSON only.

Pod: {pod_name}
Container: {container}
Error State: {reason}
Restart Count: {restarts}
Recent Logs:
{logs}
Events: {event_str}

Respond ONLY with this JSON (no explanation):
{{
  "root_cause": "<one sentence root cause in Chinese>",
  "severity": "<high|medium|low>",
  "action": "<fix_image|increase_memory|check_dependencies|fix_permissions|create_config|fix_probe|manual_inspect>",
  "suggestion": "<concrete fix steps in Chinese>"
}}"""

    try:
        raw = ask_llama(prompt)

        # ask_llama 對這個 prompt 可能直接回 dict 或有 error
        if isinstance(raw, dict) and "error" not in raw:
            # 嘗試從回傳的 dict 取需要的欄位
            return {
                "reason"    : reason,
                "root_cause": raw.get("root_cause", "LLM 分析完成但無法取得根因"),
                "severity"  : raw.get("severity", "unknown"),
                "action"    : raw.get("action", "manual_inspect"),
                "suggestion": raw.get("suggestion", ""),
                "confidence": "llm",
                "raw_llm"   : json.dumps(raw, ensure_ascii=False),
            }
    except Exception:
        pass

    return None


# ══════════════════════════════════════════════════════════════════
# CLI 測試入口
# ══════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("=" * 55)
    print("  healer/diagnose.py — LLM 根因分析測試")
    print("=" * 55)

    test_cases = [
        {
            "name": "OOMKilled",
            "context": {
                "pod_name"    : "api-server-xyz",
                "namespace"   : "default",
                "container"   : "api-server",
                "reason"      : "OOMKilled",
                "message"     : "",
                "restart_count": 3,
                "logs"        : "fatal error: runtime: out of memory\ngoroutine 1 [running]",
                "events"      : [{"reason": "OOMKilling", "message": "Memory limit reached", "count": 3, "type": "Warning"}],
            },
        },
        {
            "name": "ImagePullBackOff",
            "context": {
                "pod_name"    : "worker-abc",
                "namespace"   : "production",
                "container"   : "worker",
                "reason"      : "ImagePullBackOff",
                "message"     : "Back-off pulling image myapp:v2.0.1",
                "restart_count": 0,
                "logs"        : "",
                "events"      : [{"reason": "Failed", "message": "Failed to pull image: manifest unknown", "count": 5, "type": "Warning"}],
            },
        },
        {
            "name": "資料庫連線失敗",
            "context": {
                "pod_name"    : "backend-pod-111",
                "namespace"   : "default",
                "container"   : "backend",
                "reason"      : "CrashLoopBackOff",
                "message"     : "",
                "restart_count": 10,
                "logs"        : "Error: connect ECONNREFUSED 10.0.0.5:5432\nFailed to connect to database postgres",
                "events"      : [],
            },
        },
    ]

    for tc in test_cases:
        print(f"\n▶ {tc['name']}")
        result = diagnose_issue(tc["context"], use_llm=False)  # 測試時不呼叫 LLM
        print(f"  根因：{result['root_cause']}")
        print(f"  嚴重度：{result['severity']}  |  補救動作：{result['action']}")
        print(f"  建議：{result['suggestion']}")
        print(f"  信心來源：{result['confidence']}")
