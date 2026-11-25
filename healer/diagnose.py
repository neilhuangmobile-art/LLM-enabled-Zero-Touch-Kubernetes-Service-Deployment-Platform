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
    # (正則, root_cause, action, severity, 自動)
    # 自動＝會改叢集的動作。2026-11-25（Healer 方案 0.4）：這類規則只比對 K8s 產生的結構化文字
    # （reason、message、exitCode、事件），不比對容器自己的 log——原本容器印出「OOMKilled」就會觸發調高記憶體。
    # 0.6：拿掉 `memory.*limit`（應用程式 log 常見的設定說明文字），只認 OOMKilled。
    (r"OOMKilled|oomkilling|out of memory",
     "記憶體不足（OOMKilled）",
     "increase_memory",
     "high", True),
    # 2026-11-25（Healer 階段 3.5）：JVM／Node 自己的 heap 用完跟容器被 OOMKilled 是兩回事，加容器記憶體沒用。
    # 排在 OOM 規則後面：容器真的被 OOMKilled 時仍然先比中上一條（那條只看結構化文字）。
    (r"java\.lang\.OutOfMemoryError|javascript heap out of memory|heap out of memory",
     "程式自己的記憶體（heap）用完了，不是容器的記憶體上限不夠",
     "tune_heap",
     "high", False),
    # 2026-11-25 診斷誤判修正：缺 ConfigMap／Secret 原本被判成映像檔問題——事件用空白串成一行，
    # `image.*not found` 會從「Container image ... already present」一路比到後面的「configmap ... not found」。
    # 事件改成一行一個、規則改用 [^\n]*，而且設定檔規則排在映像檔規則前面。
    (r"createcontainerconfigerror|configmap[^\n]*not found|secret[^\n]*not found|couldn't find key",
     "ConfigMap 或 Secret 不存在",
     "create_config",
     "high", False),
    # 指令找不到（exit 126／127）：映像檔裡沒有這個程式，或啟動指令打錯，換映像檔版本沒有幫助
    (r"executable file not found|exitcode 12[67]\b|command not found|exec format error",
     "啟動指令找不到或不能執行（映像檔裡沒有這個程式，或指令打錯）",
     "fix_command",
     "high", False),
    (r"imagepullbackoff|errimagepull|failed to pull image|manifest[^\n]*unknown|image[^\n]*not found",
     "映像拉取失敗，tag 不存在或倉庫無法存取",
     "fix_image",
     "high", True),
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
     "medium", False),
    # 0.6：單獨的「403」會把應用程式正常的 HTTP 403 存取紀錄判成權限問題，要搭配 forbidden／RBAC／serviceaccount
    (r"permission denied|unauthorized|forbidden|\brbac\b|serviceaccount",
     "權限不足，可能是 RBAC 或掛載路徑問題",
     "fix_permissions",
     "medium", False),
    (r"no such file",
     "找不到需要的檔案（可能是設定檔沒有掛進來）",
     "create_config",
     "high", False),
    (r"liveness.*probe|readiness.*probe|probe failed",
     "健康檢查探針失敗",
     "fix_probe",
     "medium", True),
    (r"port.*already in use|address already in use",
     "連接埠衝突",
     "fix_port_conflict",
     "medium", False),
    # 「反覆崩潰」只描述症狀、不是原因，排在最後：日誌裡有具體原因（例如連不上資料庫）時要先命中那一條。
    # 2026-10-10 實測：這條原本排第三，崩潰的 Pod 一定有「重啟中」的事件，所以永遠先命中這條，
    # 處理方式變成刪掉 Pod 重建；資料庫連不上時重建沒有用，Healer 每 30 秒重建一次，無限循環。
    # 一啟動就正常結束（exit 0）：這個映像檔不是常駐服務，重建也一樣會結束
    (r"exitcode 0\b",
     "程式一啟動就正常結束（exit 0），這個映像檔可能不是常駐的服務",
     "fix_command",
     "medium", False),
    (r"crashloopbackoff|back-off restarting|back off restarting",
     "容器反覆崩潰，需分析應用程式日誌",
     "analyze_logs",
     "high", True),
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
    r"oom|out of memory|image[^\n]*not found|manifest[^\n]*unknown|errimagepull|"
    r"executable file not found|heap out of memory|outofmemoryerror|"
    r"permission denied|unauthorized|403|"
    + _DEPENDENCY_RE + "|" +
    r"configmap[^\n]*not found|secret[^\n]*not found|no such file|"
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
    # 2026-11-25：事件一行一個（原本用空白串成一行，規則會跨事件比中，見 _RULE_PATTERNS 的說明）
    exit_line = " ".join(x for x in (
        f"exitcode {context.get('exit_code')}" if context.get("exit_code") is not None else "",
        f"lastreason {context.get('last_reason')}" if context.get("last_reason") else "") if x)
    event_lines = [f"{e.get('reason') or ''} {_event_text(e.get('message') or '')}" for e in events]
    combined_text = "\n".join([reason, message, exit_line, logs, *event_lines]).lower()
    # 2026-11-25（Healer 方案 0.4）：K8s 自己產生的文字（不含容器 log），會改叢集的動作只看這段
    struct_text = "\n".join([reason, message, exit_line, *event_lines]).lower()

    # ── 層 1：規則型快速分析 ─────────────────────────────────────
    rule_result = _rule_analyze(combined_text, reason, struct_text)
    if rule_result["confidence"] == "rule" and not use_llm:
        rule_result["pod_name"] = pod_name
        return _finish(rule_result, rule_result, struct_text)

    # ── 層 2：LLM 深度分析 ──────────────────────────────────────
    if use_llm:
        llm_result = _llm_analyze(context)
        if llm_result:
            llm_result["pod_name"] = pod_name
            # 2026-11-25（Healer 方案階段 4.3）：模型要引用一段證據原文，程式檢查真的出現在 log、事件或狀態裡；
            # 引用不到（或沒給）就不採用模型的敘述。舊版 model server 不回傳 evidence 欄位時略過這道檢查。
            if "evidence" in llm_result and not _evidence_found(llm_result.get("evidence"), context):
                guess = llm_result.get("root_cause")
                base = dict(rule_result) if rule_result["confidence"] == "rule" else {
                    **rule_result, "root_cause": "證據不足：模型的判斷在 log 和事件裡找不到根據，需要人工查看完整日誌",
                    "action": "manual_inspect", "suggestion": _action_to_suggestion("manual_inspect")}
                base.update({"pod_name": pod_name, "llm_raw_guess": guess, "evidence_rejected": True,
                             "history": llm_result.get("history"), "runbook": llm_result.get("runbook")})
                return _finish(base, rule_result, struct_text)
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
                elif rule_result["action"] == "fix_probe":
                    # 探針失敗的事件裡有 `connection refused`，模型常把它寫成「連線到 API 失敗／超時」，
                    # 跟實際狀況（應用程式還沒啟動完就被健康檢查判死）不符（2026-10-22 實測）。用規則層的文字。
                    if llm_result.get("root_cause") != rule_result["root_cause"]:
                        llm_result["llm_raw_guess"] = llm_result.get("root_cause")
                    llm_result["root_cause"] = rule_result["root_cause"]
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
            return _finish(llm_result, rule_result, struct_text)

    # ── 層 3：無法分析，回傳規則結果或未知 ──────────────────────
    rule_result["pod_name"] = pod_name
    return _finish(rule_result, rule_result, struct_text)


MAX_TEXT = 300


def _finish(result: dict, rule_result: dict, struct_text: str) -> dict:
    """2026-11-25（Healer 方案階段 4.5）：模型寫的文字限長；另外保留規則層的原因（rule_root_cause），
    推播只用這個固定文字，不放模型原文。最後一樣經過結構化證據檢查。"""
    for k in ("root_cause", "suggestion", "llm_raw_guess"):
        if isinstance(result.get(k), str) and len(result[k]) > MAX_TEXT:
            result[k] = result[k][:MAX_TEXT] + "…"
    result["rule_root_cause"] = rule_result.get("root_cause") if rule_result.get("confidence") == "rule" else None
    return _enforce_structured(result, struct_text)


def _norm(text: str) -> str:
    """忽略大小寫、空白、引號和冒號（送給模型的事件格式是「reason: message」，原始資料是分開的兩個欄位）。"""
    return " ".join(re.sub(r"[`\"':]", " ", str(text or "").lower()).split())


def _evidence_found(evidence, context: dict) -> bool:
    """模型引用的證據（至少 8 個字）要在 log、事件、狀態訊息裡找得到（不分大小寫、忽略空白和引號）。"""
    # 送給模型的 log 去重後會加上「(x3)」次數標記（healer/logsift.sift），比對原文前拿掉
    ev = _norm(re.sub(r"\s*\(x\d+\)\s*$", "", str(evidence or "")))
    if len(ev) < 8:
        return False
    hay = _norm("\n".join([str(context.get("logs") or ""), str(context.get("message") or ""),
                           str(context.get("reason") or ""),
                           *(f"{e.get('reason') or ''} {e.get('message') or ''}" for e in context.get("events") or [])]))
    return ev in hay


def _enforce_structured(result: dict, struct_text: str) -> dict:
    """2026-11-25（Healer 方案 0.4）：會改叢集的動作（不管是規則還是模型給的）一定要有 K8s 結構化證據，
    否則降成「人工檢查」，原因文字保留。HEALER_GUARD=0 時不檢查（對照實驗用）。"""
    from healer import guard
    action = result.get("action")
    result["struct_excerpt"] = (struct_text or "")[:500]   # 補救時判斷 Docker Hub 限流（429）用
    if guard.enabled() and action in guard.AUTO_ACTIONS and not guard.supports(action, struct_text):
        result["blocked_action"] = action
        result["action"] = "manual_inspect"
        result["auto_blocked"] = "只有容器 log 提到這個問題，K8s 的狀態和事件沒有證據，不自動修改設定"
    return result


# ══════════════════════════════════════════════════════════════════
# 內部：規則型分析
# ══════════════════════════════════════════════════════════════════

def _rule_analyze(text: str, reason: str, struct_text: str = None) -> dict:
    """用正規表示式快速比對已知錯誤模式。struct_text：K8s 自己產生的文字（不含 log）；
    有給而且 HEALER_GUARD 開著時，自動規則只比對它（2026-11-25，Healer 方案 0.4）。"""
    from healer import guard
    strict = struct_text is not None and guard.enabled()
    for pattern, root_cause, action, severity, auto in _RULE_PATTERNS:
        if re.search(pattern, struct_text if (auto and strict) else text, re.IGNORECASE):
            return {
                "reason"    : reason,
                "root_cause": root_cause + _exit_code_note(struct_text if struct_text is not None else text, action),
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


def _exit_code_note(text: str, action: str) -> str:
    """結束碼的意思（Healer 方案階段 2 的結束碼分類）。137 不一定是記憶體不足。"""
    m = re.search(r"exitcode (-?\d+)", text or "")
    if not m:
        return ""
    code = int(m.group(1))
    if code == 137 and action != "increase_memory" and "oomkill" not in (text or ""):
        return "。結束碼 137 是被系統強制結束，但沒有 OOMKilled 紀錄，比較可能是健康檢查失敗被重啟或被驅逐，不一定是記憶體不足"
    if code in (1, 2) and action in ("analyze_logs", "manual_inspect", "check_dependencies"):
        return f"。結束碼 {code} 是程式自己回報錯誤結束，原因通常寫在 log 最後幾行"
    return ""


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
        "fix_command"       : "啟動指令可能打錯，或這個映像檔不是常駐服務（跑完就結束）。請確認映像檔和啟動指令；只是要定時跑一次的工作，請改用「定時執行」。",
        "tune_heap"         : "Java 請調整 -Xmx（或 JAVA_TOOL_OPTIONS），Node.js 請加 --max-old-space-size；只加容器的記憶體上限沒有用。也可能是程式有記憶體洩漏。",
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
    # 2026-11-25（Healer 方案階段 4）：送給模型的內容先整理——log 去重＋挑重點、附一篇處理手冊摘要、
    # 同一服務過去的修法（只有結構化欄位）。規則層與證據檢查仍然用原始 log。
    ctx = dict(context)
    query = " ".join([str(reason), str(context.get("message") or ""),
                      *(str(e.get("reason") or "") for e in context.get("events") or [])])
    try:
        from healer import logsift
        ctx["logs"] = logsift.sift(str(context.get("logs") or ""), query)
        book = logsift.pick_runbook(query + " " + ctx["logs"][-400:])
        if book:
            ctx["runbook"] = book["summary"]
    except Exception:  # noqa: BLE001
        book = None
    history = context.get("history") or past_fixes(context.get("namespace"), context.get("deployment"))
    if history:
        ctx["history"] = history
    try:
        diag = diagnose_with_llm(ctx)
    except Exception:
        diag = None

    if not isinstance(diag, dict) or not diag.get("root_cause"):
        return None

    out = {
        "reason"    : reason,
        "root_cause": diag.get("root_cause", "LLM 分析完成但無法取得根因"),
        "severity"  : diag.get("severity", "unknown"),
        "action"    : diag.get("action", "manual_inspect"),
        "suggestion": diag.get("suggestion", ""),
        "confidence": "llm",
        "raw_llm"   : diag.get("raw", ""),
        "history"   : history or None,
        "runbook"   : book["name"] if book else None,
    }
    if "evidence" in diag:
        out["evidence"] = diag.get("evidence") or ""
    return out


_PAST_LABELS = {"increase_memory": "調高記憶體", "fix_image": "退回映像檔", "fix_probe": "調整健康檢查",
                "analyze_logs": "重建 Pod", "revert_recent_change": "退回剛才的更新", "rollback": "退回修復"}
_PAST_RESULT = {"ok": "驗證通過", "fail": "沒有修好", "stopped": "已停手", "pending": "等待驗證"}


def past_fixes(namespace, app, records=None, limit: int = 3) -> str:
    """2026-11-25（Healer 方案階段 4.4）：同一個 namespace、同一個服務最近的自動修復，整理成一行給模型參考。
    只用結構化欄位（動作代碼、驗證結果），不放 log 和模型原文（避免被當成可以持久化的注入）。"""
    if not namespace or not app:
        return ""
    if records is None:
        try:
            import time as _t
            from core import event_store
            records = event_store.query(namespace, since=_t.time() - 7 * 86400, kinds=["heal"])
        except Exception:  # noqa: BLE001
            return ""
    items = []
    for r in records or []:
        d = r.get("data") or {}
        if r.get("app") != app or d.get("action") not in _PAST_LABELS:
            continue
        v = d.get("verify")
        if v == "pending":
            continue   # 動手當下那筆；之後會有一筆驗證結果
        res = _PAST_RESULT.get(v) if v else ("已處理（沒有驗證）" if r.get("outcome") == "ok" else "失敗")
        items.append(f"{_PAST_LABELS[d['action']]}→{res}")
    return ("上次：" + "；".join(items[-limit:])) if items else ""


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
