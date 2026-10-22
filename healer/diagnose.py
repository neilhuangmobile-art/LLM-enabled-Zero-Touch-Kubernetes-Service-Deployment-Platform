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

# 依賴服務連不上的各種寫法（2026-10-10 補：原本只認 connection refused，
# 「FATAL: cannot connect to database」這類常見寫法完全比對不到）
_DEPENDENCY_RE = (r"ECONNREFUSED|connection refused|dial tcp.*refused|"
                  r"(?:cannot|can't|could not|couldn't|unable to|failed to) connect|"
                  r"connection (?:timed out|reset)|no route to host|ENOTFOUND|"
                  r"name or service not known|temporary failure in name resolution|"
                  r"連不上|無法連線")

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
    # 2026-10-07 近失事件可見化實測時發現的真實 bug：K8s 事件的 reason 欄位是
    # "BackOff"、message 是「Back-off restarting failed container」，不是這裡原本
    # 只比對的 "CrashLoopBackOff" 字面字串——Pod 被抓到的那一瞬間如果容器狀態剛好
    # 不是 waiting（例如正好在兩次重啟中間、terminated.reason 只是 "Completed"），
    # reason 欄位就不會是 "CrashLoopBackOff"，完全靠事件文字撐住比對，原本的正則
    # 抓不到，導致規則層判定 confidence 不是 "rule"，連帶讓 _has_concrete_signal()
    # 那一整道幻覈防護被繞過（這道防護只在 confidence=="rule" 時才會跑）。
    (_DEPENDENCY_RE,
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
    # 「反覆崩潰」只描述症狀、不是原因，排在最後：日誌裡有具體原因（例如連不上資料庫）時要先命中那一條。
    # 2026-10-10 實測：這條原本排第三，崩潰的 Pod 一定有「重啟中」的事件，所以永遠先命中這條，
    # 處理方式變成刪掉 Pod 重建；資料庫連不上時重建沒有用，Healer 每 30 秒重建一次，無限循環。
    (r"crashloopbackoff|back-off restarting|back off restarting",
     "容器反覆崩潰，需分析應用程式日誌",
     "analyze_logs",
     "high"),
]


# 2026-10-06 kubectl exec 真實故障測試實測發現：純 CrashLoopBackOff（例如反覆
# `nginx -s stop` 讓 process 自己正常退出，日誌是空的、事件只有通用的
# "Back-off restarting failed container"）這種情境下，監控小模型（1.5B）仍會
# 自己編出一個聽起來具體但完全沒有證據支持的根因（實測案例：編出「容器資源不足
# 導致 OOM Killed」）。DIAGNOSE_SYSTEM 裡已經有「證據不足要老實說」的規則，但
# 1.5B 這個尺寸的指令遵循度不夠可靠（同一類問題見 docs/security_review.md 第 9
# 節）。這裡加一道不依賴模型是否遵守規則的保險：合併文字裡要真的含有具體錯誤
# 關鍵字家族之一，才信任模型給的 root_cause，否則一律蓋成老實版本。
_CONCRETE_SIGNAL_RE = re.compile(
    r"oom|out of memory|image.*not found|manifest.*unknown|errimagepull|"
    r"permission denied|unauthorized|403|"
    + _DEPENDENCY_RE + "|" +
    r"configmap.*not found|secret.*not found|no such file|"
    r"liveness.*probe|readiness.*probe|probe failed|"
    r"port.*already in use|address already in use",
    re.IGNORECASE,
)


# 模型敘述裡常被憑空加上的技術名稱；出現時必須在日誌、事件、映像檔或 Pod 名稱裡找得到
_TECH_TERMS = {
    "redis": r"redis", "mysql": r"mysql|mariadb", "postgres": r"postgres|psql|pg_", "mongodb": r"mongo",
    "kafka": r"kafka", "rabbitmq": r"rabbit|amqp", "elasticsearch": r"elastic", "nginx": r"nginx",
    "apache": r"apache|httpd", "java": r"java|jvm", "node.js": r"node", "python": r"python",
    "docker": r"docker", "dns": r"dns|resolve|name or service", "ssl": r"ssl|tls|certificate|x509",
}


def _unsupported_tech(text: str, evidence: str) -> list:
    low = (text or "").lower()
    out = []
    for term, pattern in _TECH_TERMS.items():
        if re.search(re.escape(term).replace(r"\.", r"\.?"), low) and not re.search(pattern, evidence):
            out.append(term)
    return out


_PROBE_FAIL_RE = re.compile(r"(liveness|readiness|startup) probe failed|failed (liveness|readiness|startup) probe", re.I)


def _event_text(message: str) -> str:
    """健康檢查失敗的事件只留下「哪種探針失敗」。K8s 會把探針連不上的細節（`dial tcp ...: connection refused`）
    寫進事件，原樣保留會被「依賴服務連不上」那條規則搶先比中（2026-10-22 實測：啟動要 40 秒的服務被判成
    連不上 API、叫使用者檢查相依服務）。應用程式自己日誌裡的連線錯誤不受影響，照樣優先比中依賴服務。"""
    m = _PROBE_FAIL_RE.search(message or "")
    return f"{(m.group(1) or m.group(2)).lower()} probe failed" if m else (message or "")


def _has_concrete_signal(combined_text: str) -> bool:
    return bool(_CONCRETE_SIGNAL_RE.search(combined_text))


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

    # 合併所有文字供分析。2026-10-07 修 bug：原本只合併 events 的 message，沒有
    # 合併 reason（K8s 事件的 reason 欄位是獨立的簡短代碼，例如 "BackOff"、
    # "OOMKilling"，跟 message 是分開的兩個欄位，只看 message 會漏掉只出現在
    # reason 裡的訊號）。
    combined_text = "\n".join([
        reason, message, logs,
        " ".join(e.get("reason", "") for e in events),
        " ".join(_event_text(e.get("message", "")) for e in events),
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
            if rule_result["confidence"] == "rule":
                # 規則層已經比中已知錯誤模式：補救動作(action)/嚴重度以規則為準（比較可靠，
                # 且 remediate.py 靠 action 分派）。root_cause/suggestion 預設採用小模型的敘述
                # （通常比規則層的制式文字更具體），但只有在合併文字裡真的有具體證據時才信任——
                # 規則層命中的可能只是像 CrashLoopBackOff 這種泛用模式本身不含根因，這時若模型
                # 自己編出一個更具體但毫無根據的原因，寧可老實說證據不足，不要讓幻覈的文字騙到
                # 使用者（2026-10-06 kubectl exec 實測案例）。
                llm_result["action"]   = rule_result["action"]
                llm_result["severity"] = rule_result["severity"]
                if not _has_concrete_signal(combined_text):
                    # 2026-10-07：蓋掉之前先把模型原本的版本存進 llm_raw_guess，不要整個
                    # 丟棄——近失事件可見化需要讓使用者看得到「模型原本想講什麼、被系統
                    # 攔下來了」，不是只呈現最終安全版本（見 docs/security_review.md）。
                    llm_result["llm_raw_guess"] = llm_result.get("root_cause")
                    llm_result["root_cause"] = (
                        "證據不足，只能確定是重複重啟/異常終止，實際原因需要人工查看完整日誌"
                    )
                    llm_result["suggestion"] = rule_result["suggestion"]
                else:
                    # 有具體證據時採用模型的敘述，但模型提到的技術名稱必須在證據裡找得到。
                    # 2026-10-21 全系統檢查：日誌只寫「cannot connect to database」，模型的建議卻寫
                    # 「確認 Redis 容器已正確執行」，Redis 是它自己加的。
                    evidence = combined_text + " " + str(context.get("image", "")).lower() + " " + pod_name.lower()
                    for key in ("root_cause", "suggestion"):
                        bad = _unsupported_tech(llm_result.get(key) or "", evidence)
                        if bad:
                            llm_result.setdefault("llm_raw_guess", llm_result.get(key))
                            llm_result[key] = rule_result[key]
                    if not llm_result.get("suggestion"):
                        llm_result["suggestion"] = rule_result["suggestion"]
            elif rule_result["severity"] != "unknown":
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
        "suggestion": "自動判斷不出原因。請看 Pod 詳情裡的「最近的 log」和「最近事件」，通常會寫出錯在哪裡。",
        "confidence": "unknown",
        "raw_llm"   : "",
    }


def _action_to_suggestion(action: str) -> str:
    # 2026-10-09：建議都寫成平台裡就做得到的事，不叫使用者開終端機打 kubectl（零接觸定位）
    _map = {
        "increase_memory"   : "把記憶體上限調高，建議至少加倍，例如 256Mi 改成 512Mi。可以讓 Healer 自動處理。",
        "fix_image"         : "映像檔的名稱或版本可能打錯，或倉庫需要登入權限。請到服務詳情把映像檔改成正確的名稱。",
        "analyze_logs"      : "看 Pod 詳情裡的「最近的 log」，找 error、fatal、exception 這類字，通常就是當掉的原因。",
        "check_dependencies": "它依賴的資料庫或其他服務可能沒在執行。請到「我的服務」確認那些服務都是健康的。",
        "fix_permissions"   : "權限不足（服務帳號或掛載的資料夾），需要調整部署設定，請聯絡平台管理員。",
        "create_config"     : "缺少需要的設定檔或密碼（ConfigMap、Secret），請確認部署時有一起建立。",
        "fix_probe"         : "健康檢查太早開始，應用程式還沒啟動完就被判定失敗，可以把啟動等待時間加長。",
        "fix_port_conflict" : "Port 可能設錯，或被其他程式占用。請到服務詳情確認 Port。",
        "manual_inspect"    : "自動判斷不出原因。請看 Pod 詳情裡的「最近的 log」和「最近事件」，通常會寫出錯在哪裡。",
    }
    return _map.get(action, "請看 Pod 詳情裡的「最近的 log」和「最近事件」，通常會寫出錯在哪裡。")


# ══════════════════════════════════════════════════════════════════
# 內部：LLM 分析
# ══════════════════════════════════════════════════════════════════

def _llm_analyze(context: dict) -> dict:
    """
    呼叫監控小模型（Model Server 的 /diagnose，Qwen2.5-1.5B 跑 CPU）做深度根因分析。
    模型不可用時靜默回傳 None（上層 fallback 回規則層結果）。
    """
    try:
        from llama_client import diagnose_with_llm
    except ImportError:
        return None

    reason = context.get("reason", "Unknown")
    try:
        diag = diagnose_with_llm(context)
    except Exception:
        diag = None

    if not isinstance(diag, dict) or not diag.get("root_cause"):
        return None

    return {
        "reason"    : reason,
        "root_cause": diag.get("root_cause", "LLM 分析完成但無法取得根因"),
        "severity"  : diag.get("severity", "unknown"),
        "action"    : diag.get("action", "manual_inspect"),
        "suggestion": diag.get("suggestion", ""),
        "confidence": "llm",
        "raw_llm"   : diag.get("raw", ""),
    }


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
