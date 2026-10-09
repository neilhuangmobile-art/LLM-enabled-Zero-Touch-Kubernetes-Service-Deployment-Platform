"""
cluster/remote_ops.py
客戶叢集的 Healer 補救：把 healer.diagnose 的診斷結果，對應成客戶 repo 的 Git 操作。

2026-10-09 新增（客戶自帶叢集第 5 階段）。平台連不進客戶叢集，不能像本機那樣直接刪 Pod 或
patch Deployment，所有補救都變成 commit，由客戶叢集裡的 Flux 套用（約 30~60 秒生效）：

    analyze_logs（CrashLoopBackOff）→ restart_app：更新 restartedAt annotation，觸發滾動重啟
    fix_image（ImagePullBackOff）  → restore_previous_image：恢復最近一個不同的映像檔
    increase_memory（OOMKilled）   → set_memory_limit：記憶體上限加倍，最多 MAX_MEMORY
    其他（探針、依賴、權限、設定檔、port、人工檢查）→ 不自動改設定，只回傳診斷建議

這裡不 import Flask，repo 物件由呼叫端傳入（測試可以傳假的）。
"""
import re
from typing import Any, Dict, Optional

MAX_MEMORY_MI = 4096  # 加倍到 4Gi 為止：OOM 一直發生多半是程式本身的問題，不能無限加


def _to_mi(mem: Optional[str]) -> Optional[int]:
    m = re.match(r"^(\d+)(Ki|Mi|Gi|Ti)?$", (mem or "").strip())
    if not m:
        return None
    v, unit = int(m.group(1)), m.group(2) or ""
    return {"Ki": v // 1024, "Mi": v, "Gi": v * 1024, "Ti": v * 1024 * 1024, "": v // (1024 * 1024)}[unit]


def heal(repo, issue: Dict[str, Any], diagnosis: Dict[str, Any],
         current_memory: Optional[str] = None) -> Dict[str, Any]:
    """依診斷動作執行對應的 Git 操作。回傳格式對齊 healer.remediate.remediate()。"""
    action = diagnosis.get("action") or "manual_inspect"
    app = issue.get("app") or ""
    base = {"action": action, "pending_sync": False}
    if not app:
        return {**base, "ok": False,
                "message": "找不到這個 Pod 屬於哪個部署，無法自動修復，請參考診斷建議手動處理。"
                           }
    sync_note = "已送出修正，等你的叢集同步（約 30~60 秒）。"
    try:
        if action == "analyze_logs":
            res = repo.restart_app(app)
            return {**base, "ok": True, "pending_sync": True, "commit": res.get("commit"),
                    "message": f"已重新啟動 {app}。{sync_note} 如果是程式本身的錯誤，重啟後可能還會再壞，"
                               "請看上方的根因說明。"}
        if action == "fix_image":
            # 不用 revert_app（還原最近一次變更）：最近一次變更可能就是上一次的修復，重複觸發會
            # 讓設定在好壞之間來回跳（2026-10-09 實測跳了 5 次）。改成恢復「最近一個不同的映像檔」。
            res = repo.restore_previous_image(app)
            return {**base, "ok": True, "pending_sync": True, "commit": res.get("commit"),
                    "message": f"已把 {app} 的映像檔從 {res.get('previous_image')} 恢復成 "
                               f"{res.get('restored_image')}。{sync_note}"}
        if action == "increase_memory":
            cur = _to_mi(current_memory) or 128
            if cur >= MAX_MEMORY_MI:
                return {**base, "ok": False,
                        "message": f"{app} 的記憶體上限已經是 {cur}Mi，不再自動加大：一直記憶體不足多半是程式"
                                   "本身的問題（例如記憶體洩漏），請檢查程式或手動調整。"}
            new = min(cur * 2, MAX_MEMORY_MI)
            repo.set_memory_limit(app, f"{new}Mi")
            return {**base, "ok": True, "pending_sync": True,
                    "message": f"已把 {app} 的記憶體上限從 {cur}Mi 調到 {new}Mi。{sync_note}"}
    except Exception as e:  # TenantRepoError 的訊息已經是中英雙語
        return {**base, "ok": False, "message": str(e)}
    suggestion = diagnosis.get("suggestion") or diagnosis.get("root_cause") or ""
    return {**base, "ok": False, "manual": True,
            "message": "這類問題不適合自動修改設定，請參考診斷建議處理。"
                       + (f"建議：{suggestion}" if suggestion else "")}
