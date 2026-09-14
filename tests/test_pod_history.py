"""
測試 2026-09-15 新增的 Pod 時間序列記錄（Healer 視覺化的趨勢圖用）：
`_pod_light_snapshot()`（純函式，給假的 K8s Pod 物件）跟 `_get_pod_history()`
（讀取 `_pod_history` 這個記憶體內的儲存，用 monkeypatch 塞測試資料，不用真的
連 K8s 或跑背景執行緒）。

標記 integration：import web_demo.py 會連帶 import flask/torch。
"""
import sys
import os
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = pytest.mark.integration

web_demo = pytest.importorskip("web_demo")


def _fake_container_status(ready, restart_count):
    cs = MagicMock()
    cs.ready = ready
    cs.restart_count = restart_count
    return cs


def _fake_pod(phase, container_statuses):
    pod = MagicMock()
    pod.status.phase = phase
    pod.status.container_statuses = container_statuses
    return pod


class TestPodLightSnapshot:
    def test_healthy_pod_snapshot(self):
        pod = _fake_pod("Running", [_fake_container_status(True, 0)])
        snap = web_demo._pod_light_snapshot(pod)
        assert snap["phase"] == "Running"
        assert snap["restarts"] == 0
        assert snap["ready"] == 1
        assert snap["total"] == 1
        assert "t" in snap

    def test_crashlooping_pod_snapshot_sums_restarts_across_containers(self):
        pod = _fake_pod("Running", [
            _fake_container_status(False, 5),
            _fake_container_status(True, 0),
        ])
        snap = web_demo._pod_light_snapshot(pod)
        assert snap["restarts"] == 5
        assert snap["ready"] == 1
        assert snap["total"] == 2

    def test_no_container_statuses_does_not_crash(self):
        pod = _fake_pod("Pending", None)
        snap = web_demo._pod_light_snapshot(pod)
        assert snap["restarts"] == 0
        assert snap["total"] == 0

    def test_missing_phase_defaults_to_unknown(self):
        pod = _fake_pod(None, [])
        snap = web_demo._pod_light_snapshot(pod)
        assert snap["phase"] == "Unknown"


class TestGetPodHistory:
    @pytest.fixture(autouse=True)
    def reset_history(self, monkeypatch):
        monkeypatch.setattr(web_demo, "_pod_history", {})
        yield

    def test_returns_empty_list_for_unknown_pod(self):
        assert web_demo._get_pod_history("user-alice", "nonexistent-pod") == []

    def test_returns_stored_samples_for_known_pod(self):
        web_demo._pod_history["user-alice/web-abc123"] = [
            {"t": "2026-09-15T00:00:00", "phase": "Running", "restarts": 0, "ready": 1, "total": 1},
            {"t": "2026-09-15T00:00:30", "phase": "Running", "restarts": 1, "ready": 1, "total": 1},
        ]
        history = web_demo._get_pod_history("user-alice", "web-abc123")
        assert len(history) == 2
        assert history[-1]["restarts"] == 1

    def test_namespace_isolation_does_not_leak_other_users_history(self):
        web_demo._pod_history["user-alice/web-abc123"] = [{"t": "x", "phase": "Running", "restarts": 0, "ready": 1, "total": 1}]
        # 同名的 Pod 出現在另一個帳號的 namespace 下，不該互相看到彼此的歷史
        assert web_demo._get_pod_history("user-bob", "web-abc123") == []

    def test_returns_a_copy_not_the_live_list(self):
        web_demo._pod_history["user-alice/web-abc123"] = [{"t": "x", "phase": "Running", "restarts": 0, "ready": 1, "total": 1}]
        history = web_demo._get_pod_history("user-alice", "web-abc123")
        history.append({"t": "y", "phase": "Running", "restarts": 99, "ready": 1, "total": 1})
        # 呼叫端修改回傳值不該汙染原始儲存
        assert len(web_demo._pod_history["user-alice/web-abc123"]) == 1
