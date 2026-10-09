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
