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


def test_probe_root_cause_uses_rule_text(monkeypatch):
    """模型把探針失敗寫成「連線到 API 網址超時」：處置對但原因錯，改用規則層的文字，模型原句留在 llm_raw_guess。"""
    import healer.diagnose as dg
    monkeypatch.setattr(dg, "_llm_analyze", lambda ctx: {"root_cause": "連線到 API 網址超時", "suggestion": "檢查 API",
                                                         "action": "check_dependencies", "severity": "medium"})
    r = dg.diagnose_issue({"pod_name": "demo-slow-1-2", "reason": "CrashLoopBackOff",
                           "logs": "warming up cache...", "events": PROBE_EVENTS}, use_llm=True)
    assert r["action"] == "fix_probe"
    assert r["root_cause"] == "健康檢查探針失敗" and r["llm_raw_guess"] == "連線到 API 網址超時"


def test_dependency_error_in_logs_still_wins_over_probe():
    """應用程式自己的日誌寫連不上資料庫：就算同時有探針失敗，仍判成依賴服務問題。"""
    r = diagnose_issue({"pod_name": "orders-1-2", "reason": "CrashLoopBackOff",
                        "logs": "FATAL: could not connect to database server: connection refused",
                        "events": PROBE_EVENTS}, use_llm=False)
    assert r["action"] == "check_dependencies", r


def test_heal_dedupe_ignores_model_wording(tmp_path, monkeypatch):
    """去重不比對模型寫的原因文字：同一個問題換個說法不能再通知一次；真的修了一次（新的 Pod）就要再記。"""
    import pytest
    web_demo = pytest.importorskip("web_demo")
    from core import event_store
    event_store.init(str(tmp_path / "dedupe.db"))
    sent = []
    monkeypatch.setattr(web_demo._push, "send_async", lambda ns, msg: sent.append(msg))
    wordings = iter(["無法連線到 Postgres 網站", "連線到 Postgres 頻繁失敗", "無法連線到 Postgres 頻道"])
    monkeypatch.setattr(web_demo, "_real_heal_pod_unrecorded",
                        lambda p, n: {"ok": True, "action": "check_dependencies", "root_cause": next(wordings)})
    for _ in range(3):
        web_demo._real_heal_pod("demo-orders-68c676bf58-42kt6", "user-dedupe2")
    assert len(sent) == 1   # 三種說法，同一個問題，只通知一次

    monkeypatch.setattr(web_demo, "_real_heal_pod_unrecorded",
                        lambda p, n: {"ok": True, "action": "increase_memory", "root_cause": "記憶體不足"})
    web_demo._real_heal_pod("demo-oom-1-aaaaa", "user-dedupe2")
    web_demo._real_heal_pod("demo-oom-1-aaaaa", "user-dedupe2")   # 網站重啟後重新檢查同一個 Pod：不重複
    web_demo._real_heal_pod("demo-oom-2-bbbbb", "user-dedupe2")   # 調高後的新 Pod 又記憶體不足：是新的一次修復
    assert len(sent) == 3
    assert len(event_store.query("user-dedupe2", kinds=["heal"])) == 3


def test_healer_namespace_scope(monkeypatch):
    """同一個叢集上兩個網站：各自只處理自己的 namespace，不會搶修同一個 Pod。"""
    # 兩個值都明確設定（空字串也算設定），重新載入時 .env 才不會補上開發者本機的設定
    import core.config as cfg
    monkeypatch.setenv("HEALER_ONLY_NAMESPACES", "user-showcase")
    monkeypatch.setenv("HEALER_SKIP_NAMESPACES", "")
    demo = importlib.reload(cfg)
    assert demo.healer_owns_namespace("user-showcase") and not demo.healer_owns_namespace("user-neilhuang-mobile")
    monkeypatch.setenv("HEALER_ONLY_NAMESPACES", "")
    monkeypatch.setenv("HEALER_SKIP_NAMESPACES", "user-showcase")
    prod = importlib.reload(cfg)
    assert not prod.healer_owns_namespace("user-showcase") and prod.healer_owns_namespace("user-neilhuang-mobile")
    monkeypatch.setenv("HEALER_SKIP_NAMESPACES", "")
    assert importlib.reload(cfg).healer_owns_namespace("anything")   # 都沒設定：全部處理（原本的行為）
    monkeypatch.undo()
    importlib.reload(cfg)


def test_scan_skips_sleep_waker_pods(monkeypatch):
    """2026-10-26 全系統測試：Healer 曾對休眠用的喚醒器執行「檢查相依服務」。喚醒器由休眠模組管理，掃描時要跳過。"""
    from types import SimpleNamespace as NS
    from healer import pod_watcher as pw
    pods = [NS(metadata=NS(name="zt-waker-web-abc", labels={"zerotouch/waker": "web"})),
            NS(metadata=NS(name="web-abc", labels={"app": "web"}))]
    fake_v1 = NS(list_namespaced_pod=lambda ns: NS(items=pods))
    monkeypatch.setattr(pw, "K8S_AVAILABLE", True)
    monkeypatch.setattr(pw, "_load_k8s_config", lambda: None)
    monkeypatch.setattr(pw, "k8s_client", NS(CoreV1Api=lambda: fake_v1), raising=False)
    monkeypatch.setattr(pw, "_check_pod", lambda pod: {"pod_name": pod.metadata.name})
    monkeypatch.setattr(pw, "_print_issue", lambda issue: None)
    assert [i["pod_name"] for i in pw.scan_once("user-x")] == ["web-abc"]
