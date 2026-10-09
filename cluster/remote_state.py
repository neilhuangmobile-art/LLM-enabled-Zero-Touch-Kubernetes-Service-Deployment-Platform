"""
cluster/remote_state.py
客戶叢集的連線狀態：同意紀錄、一次性安裝 token、安裝進度、回報器 token、最新回報、
以及「連線檢查頁該顯示哪一種狀況」的判斷。

2026-10-09 新增（客戶自帶叢集第 3 階段）。設計重點：
- token 只存 SHA-256 雜湊，原文只在產生當下回傳一次（資料檔外洩也拿不到可用 token）
- 回報器送來的內容一律當成不可信輸入：限制大小、筆數、字串長度，只留白名單欄位
- connection_status() 是純函式，連線檢查頁的六種狀況都能單元測試
- 持久化到 data/clusters.json（原子寫入）；最新一份回報只放記憶體（量大、重啟後等下一次回報即可）
"""
import hashlib
import json
import os
import secrets
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

ONBOARD_TOKEN_TTL = 30 * 60      # 安裝指令有效 30 分鐘
REPORT_FRESH_SEC = 60            # 60 秒內有回報 = 線上（回報器每 15 秒一次）
REPORT_OFFLINE_SEC = 120         # 超過 2 分鐘沒回報 = 離線
NO_REPORT_GRACE_SEC = 90         # 安裝完成後等回報器第一次回報的寬限
MAX_REPORT_BYTES = 256 * 1024
CONSENT_VERSION = "2026-10-09.2"  # .2：補上輸入文字會送到 Gemini 的說明

# 安裝腳本回報的步驟名稱（install.ps1 與這裡要一致）
STEPS = ("start", "kubectl", "cluster", "flux", "crds", "bundle", "reporter", "done")


def _h(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _clip(v: Any, n: int) -> str:
    s = "" if v is None else str(v)
    return s if len(s) <= n else s[:n] + "…"


# ══════════════════════════════════════════════════════════════════
# 回報內容驗證（回報器 → 平台）
# ══════════════════════════════════════════════════════════════════

class ReportError(ValueError):
    pass


def _cond(obj: Any) -> Dict[str, Any]:
    """Flux 物件的 Ready condition → {ready, reason, message}；格式不對就視為未就緒。"""
    if not isinstance(obj, dict):
        return {"ready": False, "reason": "Missing", "message": ""}
    return {"ready": obj.get("ready") is True, "reason": _clip(obj.get("reason"), 80),
            "message": _clip(obj.get("message"), 500), "revision": _clip(obj.get("revision"), 80)}


def clean_report(raw: Any) -> Dict[str, Any]:
    """只保留白名單欄位、截斷字串、限制筆數。格式錯誤丟 ReportError。"""
    if not isinstance(raw, dict):
        raise ReportError("report must be an object")
    out: Dict[str, Any] = {
        "agent_version": _clip(raw.get("agent_version"), 20),
        "namespace": _clip(raw.get("namespace"), 63),
        "errors": [_clip(e, 300) for e in (raw.get("errors") or [])[:20]],
        "flux": {"source": _cond((raw.get("flux") or {}).get("source")),
                 "kustomization": _cond((raw.get("flux") or {}).get("kustomization"))},
        "deployments": [], "pods": [], "events": [], "nodes": [], "metrics": None,
        "platform_configmap": bool(raw.get("platform_configmap")),
        "services": [],
    }
    for d in (raw.get("deployments") or [])[:200]:
        if isinstance(d, dict):
            out["deployments"].append({
                "name": _clip(d.get("name"), 63), "replicas": int(d.get("replicas") or 0),
                "ready": int(d.get("ready") or 0), "available": int(d.get("available") or 0),
                "image": _clip(d.get("image"), 200),
                "memory_limit": _clip(d.get("memory_limit"), 20),
                "created": _clip(d.get("created"), 40), "updated": _clip(d.get("updated"), 40),
                "conditions": [_clip(c, 200) for c in (d.get("conditions") or [])[:5]]})
    for p in (raw.get("pods") or [])[:300]:
        if not isinstance(p, dict):
            continue
        containers = []
        for c in (p.get("containers") or [])[:10]:
            if isinstance(c, dict):
                containers.append({
                    "name": _clip(c.get("name"), 63), "ready": bool(c.get("ready")),
                    "restart_count": int(c.get("restart_count") or 0),
                    "state": _clip(c.get("state"), 20), "reason": _clip(c.get("reason"), 60),
                    "last_reason": _clip(c.get("last_reason"), 60),
                    "message": _clip(c.get("message"), 300), "image": _clip(c.get("image"), 200)})
        logs = p.get("logs")
        specs = []
        for c in (p.get("spec_containers") or [])[:10]:
            if isinstance(c, dict):
                specs.append({
                    "name": _clip(c.get("name"), 63), "image": _clip(c.get("image"), 200),
                    "ports": [int(x) for x in (c.get("ports") or [])[:10] if str(x).isdigit()],
                    "requests": {k: _clip(v, 20) for k, v in (c.get("requests") or {}).items() if k in ("cpu", "memory")},
                    "limits": {k: _clip(v, 20) for k, v in (c.get("limits") or {}).items() if k in ("cpu", "memory")}})
        out["pods"].append({
            "ip": _clip(p.get("ip"), 45), "spec_containers": specs,
            "name": _clip(p.get("name"), 253), "app": _clip(p.get("app"), 63),
            "phase": _clip(p.get("phase"), 20), "node": _clip(p.get("node"), 63),
            "created": _clip(p.get("created"), 40), "containers": containers,
            # log 是容器自己印的，最不可信：只留最後 50 行、8KB
            "logs": _clip("\n".join(str(logs).splitlines()[-50:]), 8000) if logs else ""})
    for e in (raw.get("events") or [])[:200]:
        if isinstance(e, dict):
            out["events"].append({
                "object": _clip(e.get("object"), 253), "type": _clip(e.get("type"), 20),
                "reason": _clip(e.get("reason"), 60), "message": _clip(e.get("message"), 300),
                "count": int(e.get("count") or 1), "last": _clip(e.get("last"), 40)})
    for s in (raw.get("services") or [])[:100]:
        if isinstance(s, dict):
            out["services"].append({"name": _clip(s.get("name"), 63), "type": _clip(s.get("type"), 20),
                                    "app": _clip(s.get("app"), 63),
                                    "ports": [int(x) for x in (s.get("ports") or [])[:10] if str(x).isdigit()]})
    for n in (raw.get("nodes") or [])[:50]:
        if isinstance(n, dict):
            out["nodes"].append({"name": _clip(n.get("name"), 63), "cpu": _clip(n.get("cpu"), 20),
                                 "memory": _clip(n.get("memory"), 20)})
    m = raw.get("metrics")
    if isinstance(m, dict):
        out["metrics"] = {_clip(k, 253): {"cpu": _clip(v.get("cpu"), 20), "memory": _clip(v.get("memory"), 20)}
                          for k, v in list(m.items())[:300] if isinstance(v, dict)}
    return out


# ══════════════════════════════════════════════════════════════════
# 連線檢查頁的狀況判斷（純函式）
# ══════════════════════════════════════════════════════════════════

# 狀態代碼 → 給使用者看的標題與下一步（{ns} 會換成客戶的 namespace）
STATUS_TEXT = {
    "not_started": ("還沒收到安裝腳本的任何訊息",
                    "請回到上一頁複製指令，貼到 PowerShell 執行，不是「命令提示字元」。開啟方式：按 Windows 鍵，"
                    "輸入 PowerShell，按 Enter。"),
    "no_kubectl": ("你的電腦找不到 kubectl",
                   "請安裝並開啟 Docker Desktop（它內建 kubectl），開啟後重新執行同一行指令。"),
    "cluster_unreachable": ("連不上你的 Kubernetes 叢集",
                            "打開 Docker Desktop，按右上角的齒輪進入 Settings，在 Kubernetes 頁籤勾選 Enable Kubernetes，"
                            "按 Apply & restart。等左下角變成綠燈後，重新執行指令。"),
    "flux_failed": ("安裝或啟動 Flux 時失敗",
                    "通常是網路慢、映像檔下載卡住。請在 Docker Desktop 右下角的圖示按右鍵選 Restart，等它變綠燈後，"
                    "按「返回」取得新的指令再執行一次，重複執行是安全的。下方灰色區塊是詳細狀態。"),
    "bundle_failed": ("套用平台設定時失敗",
                      "安裝指令可能已經過期（有效 30 分鐘），請按「返回」，再按「同意並繼續」取得新指令後重新執行。"),
    "installing": ("安裝進行中",
                   "請等 PowerShell 視窗跑完，第一次大約 2 到 5 分鐘，會下載 Flux 和回報程式。這個頁面會自動更新。"),
    "reporter_not_ready": ("狀態回報程式還沒啟動",
                           "第一次要下載映像檔，可能要 1 到 3 分鐘，請稍候再按「重新檢查」。還是不行的話，請在 PowerShell "
                           "執行 kubectl get pods -n {ns} 看狀態。"),
    "no_report": ("安裝完成，但還沒收到你的叢集回報",
                  "你的叢集可能連不到外網。請確認電腦能上網，並在 PowerShell 執行 kubectl logs -n {ns} "
                  "deploy/zt-reporter 查看錯誤。"),
    "git_not_ready": ("你的叢集拉不到部署設定",
                      "這通常是平台這端的 GitHub 設定問題。請按「返回」重新產生指令再執行一次，還是失敗請聯絡管理員。"),
    "sync_pending": ("連線成功，正在第一次同步",
                     "大約 30 秒，請稍候再按「重新檢查」。"),
    "awaiting_report": ("正在等你的叢集回報狀態",
                        "平台剛重新啟動，你的叢集會在 15 秒內回報，不用做任何事。"),
    "offline": ("你的叢集目前離線",
                "請確認電腦開著、Docker Desktop 正在執行。"),
    "connected": ("連線成功", "可以開始使用了。"),
}

_STEP_FAIL_STATUS = {"start": "not_started", "kubectl": "no_kubectl", "cluster": "cluster_unreachable",
                     "flux": "flux_failed", "crds": "flux_failed", "bundle": "bundle_failed",
                     "reporter": "reporter_not_ready"}


def connection_status(progress: List[dict], report: Optional[dict], report_at: Optional[float],
                      now: float, ever_connected: bool = False, platform_started: Optional[float] = None
                      ) -> Dict[str, Any]:
    """依安裝進度與最新回報判斷狀況。回傳 {code, ok, detail}。"""
    fresh = report is not None and report_at is not None and now - report_at <= REPORT_FRESH_SEC
    if report is not None and report_at is not None:
        if now - report_at > REPORT_OFFLINE_SEC:
            return {"code": "offline", "ok": False, "detail": ""}
        if fresh:
            src, ks = report["flux"]["source"], report["flux"]["kustomization"]
            if not src["ready"]:
                return {"code": "git_not_ready", "ok": False, "detail": src["message"]}
            if not ks["ready"] or not report.get("platform_configmap"):
                return {"code": "sync_pending", "ok": False, "detail": ks["message"]}
            return {"code": "connected", "ok": True, "detail": ""}
    if ever_connected and report is None:
        # 平台重啟後還沒收到第一份回報：不要叫已經裝好的人重裝；剛啟動的一分鐘內也不要顯示
        # 「離線」嚇人（2026-10-09 實測：重啟後前 15 秒每位客戶都會先看到紅色離線橫幅）
        if platform_started is not None and now - platform_started < REPORT_FRESH_SEC:
            return {"code": "awaiting_report", "ok": False, "detail": ""}
        return {"code": "offline", "ok": False, "detail": ""}
    if not progress:
        return {"code": "not_started", "ok": False, "detail": ""}
    last = progress[-1]
    if not last.get("ok"):
        return {"code": _STEP_FAIL_STATUS.get(str(last.get("step")), "flux_failed"), "ok": False,
                "detail": _clip(last.get("detail"), 300)}
    if last.get("step") == "done":
        waited = now - float(last.get("at") or now)
        return {"code": "no_report" if waited > NO_REPORT_GRACE_SEC else "reporter_not_ready",
                "ok": False, "detail": ""}
    # 最後一步成功但還沒到 done：腳本還在跑（例如等 Flux 啟動），不是回報程式有問題
    return {"code": "installing", "ok": False, "detail": ""}


def status_text(code: str, namespace: str) -> Tuple[str, str]:
    title, howto = STATUS_TEXT.get(code, STATUS_TEXT["not_started"])
    return title, howto.replace("{ns}", namespace)


# ══════════════════════════════════════════════════════════════════
# 狀態儲存
# ══════════════════════════════════════════════════════════════════

class ClusterRegistry:
    def __init__(self, path: Optional[str] = None, clock=time.time):
        self.path = path
        self.clock = clock
        self._lock = threading.RLock()
        self._data: Dict[str, dict] = {}
        self._reports: Dict[str, Tuple[float, dict]] = {}
        self.started_at = clock()
        if path and os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    self._data = json.load(f)
            except Exception:
                self._data = {}

    def _save(self):
        if not self.path:
            return
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def _user(self, username: str) -> dict:
        return self._data.setdefault(username, {"onboard_tokens": [], "progress": []})

    # ── 同意書 ──
    def record_consent(self, username: str, version: str = CONSENT_VERSION):
        with self._lock:
            self._user(username)["consent"] = {"version": version, "at": self.clock()}
            self._save()

    def has_consent(self, username: str, version: str = CONSENT_VERSION) -> bool:
        c = self._data.get(username, {}).get("consent")
        return bool(c and c.get("version") == version)

    # ── 一次性安裝 token ──
    def issue_onboard_token(self, username: str) -> str:
        """作廢舊的，產生新的。原文只回傳這一次。"""
        raw = secrets.token_urlsafe(24)
        with self._lock:
            u = self._user(username)
            u["onboard_tokens"] = [{"hash": _h(raw), "expires": self.clock() + ONBOARD_TOKEN_TTL}]
            u["progress"] = []
            self._save()
        return raw

    def claim_bundle(self, raw: str) -> Optional[str]:
        """安裝包（含 deploy key 與回報 token）只能下載一次：第一次成功領取就標記，之後同一個
        token 再來一律拒絕，避免指令外流後被重放取得金鑰。token 本身仍可用來回報安裝進度。"""
        if not raw:
            return None
        hv, now = _h(raw), self.clock()
        with self._lock:
            for name, u in self._data.items():
                for t in u.get("onboard_tokens", []):
                    if secrets.compare_digest(t["hash"], hv) and t["expires"] > now:
                        if t.get("bundle_claimed"):
                            return None
                        t["bundle_claimed"] = now
                        self._save()
                        return name
        return None

    def revoke_onboard_tokens(self, username: str):
        with self._lock:
            self._user(username)["onboard_tokens"] = []
            self._save()

    def user_for_onboard_token(self, raw: str) -> Optional[str]:
        if not raw:
            return None
        hv, now = _h(raw), self.clock()
        with self._lock:
            for name, u in self._data.items():
                for t in u.get("onboard_tokens", []):
                    if secrets.compare_digest(t["hash"], hv) and t["expires"] > now:
                        return name
        return None

    # ── 安裝進度 ──
    def record_progress(self, username: str, step: str, ok: bool, detail: str = ""):
        if step not in STEPS:
            return
        with self._lock:
            u = self._user(username)
            u["progress"] = (u.get("progress") or [])[-30:] + [
                {"step": step, "ok": bool(ok), "detail": _clip(detail, 500), "at": self.clock()}]
            self._save()

    # ── 回報器 token ──
    def issue_report_token(self, username: str) -> str:
        raw = secrets.token_urlsafe(32)
        with self._lock:
            self._user(username)["report_token"] = _h(raw)
            self._save()
        return raw

    def user_for_report_token(self, raw: str) -> Optional[str]:
        if not raw:
            return None
        hv = _h(raw)
        with self._lock:
            for name, u in self._data.items():
                t = u.get("report_token")
                if t and secrets.compare_digest(t, hv):
                    return name
        return None

    # ── 回報 ──
    def store_report(self, username: str, report: dict):
        now = self.clock()
        with self._lock:
            self._reports[username] = (now, report)
            st = connection_status([], report, now, now)
            u = self._user(username)
            if st["ok"] and not u.get("connected_at"):
                u["connected_at"] = now
                self._save()

    def latest_report(self, username: str) -> Tuple[Optional[float], Optional[dict]]:
        at, rep = self._reports.get(username, (None, None))
        return at, rep

    def ever_connected(self, username: str) -> bool:
        return bool(self._data.get(username, {}).get("connected_at"))

    def status(self, username: str) -> Dict[str, Any]:
        at, rep = self.latest_report(username)
        prog = self._data.get(username, {}).get("progress") or []
        st = connection_status(prog, rep, at, self.clock(), self.ever_connected(username), self.started_at)
        st["ever_connected"] = self.ever_connected(username)
        st["last_report_at"] = at
        return st

    def forget(self, username: str):
        """帳號被封鎖/刪除時清掉所有連線資料（token 立即失效）。"""
        with self._lock:
            self._data.pop(username, None)
            self._reports.pop(username, None)
            self._save()
