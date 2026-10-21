"""2026-10-10 實測抓到的 Healer 誤判：拿不到真正的日誌、規則順序讓「反覆崩潰」蓋掉具體原因。"""
import pytest

from healer import pod_watcher
from healer.diagnose import diagnose_issue


class _FakeV1:
    def __init__(self, previous, current):
        self.previous, self.current = previous, current

    def read_namespaced_pod_log(self, name, namespace, container, tail_lines, previous):
        val = self.previous if previous else self.current
        if isinstance(val, Exception):
            raise val
        return val


@pytest.mark.parametrize("previous", [
    "unable to retrieve container logs for docker://6feab746075259f8",
    "",
    Exception('previous terminated container "x" in pod "y" not found'),
])
def test_logs_fall_back_to_current_container(monkeypatch, previous):
    if not pod_watcher.K8S_AVAILABLE:
        pytest.skip("kubernetes 套件沒裝")
    fake = _FakeV1(previous, "starting orders-api\nFATAL: cannot connect to database\n")
    monkeypatch.setattr(pod_watcher.k8s_client, "CoreV1Api", lambda: fake)
    assert "cannot connect to database" in pod_watcher._get_pod_logs("p", "ns", "c")


def test_logs_unavailable_does_not_leak_error_text(monkeypatch):
    if not pod_watcher.K8S_AVAILABLE:
        pytest.skip("kubernetes 套件沒裝")
    fake = _FakeV1("unable to retrieve container logs for docker://abc", Exception("boom: secret detail"))
    monkeypatch.setattr(pod_watcher.k8s_client, "CoreV1Api", lambda: fake)
    out = pod_watcher._get_pod_logs("p", "ns", "c")
    assert out == "[無法取得日誌]"


def test_specific_cause_wins_over_crashloop_symptom():
    """崩潰的 Pod 一定有「重啟中」事件；日誌寫了連不上資料庫，就要判成依賴問題，不是刪 Pod 重建。"""
    d = diagnose_issue({
        "pod_name": "orders-api-1", "namespace": "user-x", "container": "orders-api",
        "reason": "Error", "message": "", "restart_count": 5,
        "logs": "starting orders-api\nFATAL: cannot connect to database\n",
        "events": [{"reason": "BackOff", "message": "Back-off restarting failed container orders-api"}],
    })
    assert d["action"] == "check_dependencies"
    assert "資料庫" in d["root_cause"]   # 規則層與模型的寫法不同，但都要講到資料庫


def test_pure_crashloop_still_detected():
    d = diagnose_issue({
        "pod_name": "web-1", "namespace": "user-x", "container": "web", "reason": "CrashLoopBackOff",
        "message": "", "restart_count": 5, "logs": "",
        "events": [{"reason": "BackOff", "message": "Back-off restarting failed container web"}],
    })
    assert d["action"] == "analyze_logs"


@pytest.mark.parametrize("logs,keep", [
    ("FATAL: cannot connect to database\n", False),                    # 日誌沒提到 Redis，模型卻寫 Redis → 換掉
    ("Error: connect ECONNREFUSED redis:6379\n", True),               # 日誌真的有 Redis → 保留模型說法
])
def test_model_cannot_invent_tech_names(monkeypatch, logs, keep):
    """2026-10-21 全系統檢查：日誌只寫連不上資料庫，模型建議卻寫「確認 Redis 容器已正確執行」。"""
    import healer.diagnose as dg
    monkeypatch.setattr(dg, "_llm_analyze", lambda ctx: {
        "root_cause": "無法連線到資料庫", "suggestion": "確認 Redis 容器已正確執行且可訪問", "severity": "medium"})
    d = dg.diagnose_issue({"pod_name": "orders-api-1", "namespace": "u", "container": "c", "reason": "Error",
                           "message": "", "restart_count": 3, "logs": logs, "events": [], "image": "busybox:1.36"})
    assert d["action"] == "check_dependencies"
    assert ("Redis" in d["suggestion"]) is keep
    if not keep:
        assert "Redis" in (d.get("llm_raw_guess") or "")   # 被攔下的說法留存，近失事件可見化
