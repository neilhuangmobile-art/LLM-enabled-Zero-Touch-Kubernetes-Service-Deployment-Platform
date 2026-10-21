"""2026-10-21 全系統檢查抓到的三個問題的回歸測試。"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pytest

web_demo = pytest.importorskip("web_demo")


class _FakeV1:
    def __init__(self, previous, current):
        self.previous, self.current = previous, current

    def read_namespaced_pod_log(self, name, namespace, container, tail_lines, previous, _request_timeout):
        return self.previous if previous else self.current


def test_pod_detail_logs_skip_unavailable_message(monkeypatch):
    """Pod 詳情的日誌：舊容器被清掉時回的「unable to retrieve…」不能當成日誌（Healer 那份 10/10 修了，這份漏掉）。"""
    fake = _FakeV1("unable to retrieve container logs for docker://abc", "FATAL: cannot connect to database\n")
    monkeypatch.setattr(web_demo, "K8S_ENABLED", True, raising=False)
    monkeypatch.setattr(web_demo.k8s_client, "CoreV1Api", lambda *a: fake)
    assert "cannot connect to database" in web_demo._pod_log_tail("p", "ns", "c")


def test_same_heal_is_recorded_once_per_window(tmp_path, monkeypatch):
    """同一個服務、同一種處理、同一個原因，6 小時內只記一次（網站重啟會重新檢查還沒解決的問題）。"""
    from core import event_store
    event_store.init(str(tmp_path / "heal.db"))
    calls = {"n": 0}

    def fake_heal(pod, ns):
        calls["n"] += 1
        return {"ok": True, "action": "check_dependencies", "root_cause": "無法連線到資料庫"}

    monkeypatch.setattr(web_demo, "_real_heal_pod_unrecorded", fake_heal)
    for _ in range(4):
        web_demo._real_heal_pod("orders-api-5bb884c797-54fcm", "user-dedupe")
    assert calls["n"] == 4   # Healer 每次照樣檢查
    rows = event_store.query("user-dedupe", kinds=["heal"])
    assert len(rows) == 1    # 但只記一次
    # 原因不同就要記
    monkeypatch.setattr(web_demo, "_real_heal_pod_unrecorded",
                        lambda p, n: {"ok": True, "action": "increase_memory", "root_cause": "記憶體不足"})
    web_demo._real_heal_pod("orders-api-5bb884c797-54fcm", "user-dedupe")
    assert len(event_store.query("user-dedupe", kinds=["heal"])) == 2


def test_last_restart_time_is_reported():
    now = datetime.now(timezone.utc)
    term = lambda mins: NS(terminated=NS(finished_at=now - timedelta(minutes=mins)))
    pod = NS(status=NS(container_statuses=[NS(last_state=term(300)), NS(last_state=term(5)), NS(last_state=None)]))
    got = datetime.fromisoformat(web_demo._last_restart_iso(pod))
    assert abs((now - got).total_seconds() - 300) < 5
    assert web_demo._last_restart_iso(NS(status=NS(container_statuses=[]))) is None
