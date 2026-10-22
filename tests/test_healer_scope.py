"""2026-10-22 帳號 neilhuang-mobile 實測 Healer 找到的兩個問題的回歸測試。"""
import importlib

from healer.diagnose import diagnose_issue

PROBE_EVENTS = [
    {"reason": "Unhealthy", "message": 'Liveness probe failed: Get "http://10.1.2.248:8080/": dial tcp 10.1.2.248:8080: connect: connection refused'},
    {"reason": "Killing", "message": "Container api failed liveness probe, will be restarted"},
    {"reason": "BackOff", "message": "Back-off restarting failed container api in pod demo-slow"},
]


def test_slow_start_is_probe_problem_not_dependency():
    """啟動要 40 秒、健康檢查 15 秒就放棄：事件裡探針連不上的 connection refused 不能被當成依賴服務連不上。"""
    r = diagnose_issue({"pod_name": "demo-slow-6ff4ff668b-62z5s", "reason": "CrashLoopBackOff",
                        "logs": "warming up cache...", "events": PROBE_EVENTS}, use_llm=False)
    assert r["action"] == "fix_probe", r


def test_dependency_error_in_logs_still_wins_over_probe():
    """應用程式自己的日誌寫連不上資料庫：就算同時有探針失敗，仍判成依賴服務問題。"""
    r = diagnose_issue({"pod_name": "orders-1-2", "reason": "CrashLoopBackOff",
                        "logs": "FATAL: could not connect to database server: connection refused",
                        "events": PROBE_EVENTS}, use_llm=False)
    assert r["action"] == "check_dependencies", r


def test_healer_namespace_scope(monkeypatch):
    """同一個叢集上兩個網站：各自只處理自己的 namespace，不會搶修同一個 Pod。"""
    import core.config as cfg
    monkeypatch.setenv("HEALER_ONLY_NAMESPACES", "user-showcase")
    monkeypatch.delenv("HEALER_SKIP_NAMESPACES", raising=False)
    demo = importlib.reload(cfg)
    assert demo.healer_owns_namespace("user-showcase") and not demo.healer_owns_namespace("user-neilhuang-mobile")
    monkeypatch.delenv("HEALER_ONLY_NAMESPACES")
    monkeypatch.setenv("HEALER_SKIP_NAMESPACES", "user-showcase")
    prod = importlib.reload(cfg)
    assert not prod.healer_owns_namespace("user-showcase") and prod.healer_owns_namespace("user-neilhuang-mobile")
    monkeypatch.delenv("HEALER_SKIP_NAMESPACES")
    assert importlib.reload(cfg).healer_owns_namespace("anything")   # 都沒設定：全部處理（原本的行為）
