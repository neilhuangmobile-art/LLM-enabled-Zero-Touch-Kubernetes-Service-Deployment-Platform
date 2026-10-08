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
    """2026-10-03 改成用推斷出來的 app／Deployment 名稱當 key，不是用 Pod 名稱
    本身——這樣同一個服務的 Pod 被 Healer 修復重建、名稱換了，歷史還是能接得
    起來，使用者才看得出「這個服務曾經壞過、後來被修好」的完整過程，不是每次
    重建都從空的重新開始。測試用的 Pod 名稱改成真實 K8s 格式
    （`<deploy>-<replicaset hash>-<pod hash>`，兩段隨機後綴），不是以前那種
    只有一個連字號的簡化假名字。"""

    @pytest.fixture(autouse=True)
    def reset_history(self, monkeypatch):
        monkeypatch.setattr(web_demo, "_pod_history", {})
        yield

    def test_returns_empty_list_for_unknown_pod(self):
        assert web_demo._get_pod_history("user-alice", "nonexistent-pod") == []

    def test_returns_stored_samples_for_known_pod(self):
        web_demo._pod_history["user-alice/web"] = [
            {"t": "2026-09-15T00:00:00", "phase": "Running", "restarts": 0, "ready": 1, "total": 1},
            {"t": "2026-09-15T00:00:30", "phase": "Running", "restarts": 1, "ready": 1, "total": 1},
        ]
        history = web_demo._get_pod_history("user-alice", "web-7d6b8c9f4-xk2pq")
        assert len(history) == 2
        assert history[-1]["restarts"] == 1

    def test_pod_recreated_with_new_name_still_finds_same_app_history(self):
        """核心行為：同一個 app 的 Pod 被 Healer 修復重建、拿到全新名稱，查詢
        結果要是同一份累積歷史，不是空的重新開始。"""
        web_demo._pod_history["user-alice/web"] = [
            {"t": "2026-09-15T00:00:00", "phase": "Running", "restarts": 0, "ready": 1, "total": 1},
        ]
        before = web_demo._get_pod_history("user-alice", "web-7d6b8c9f4-xk2pq")
        after = web_demo._get_pod_history("user-alice", "web-9f8e7d6c5-ab1cd")  # Healer 重建後的新名稱
        assert before == after
        assert len(after) == 1

    def test_namespace_isolation_does_not_leak_other_users_history(self):
        web_demo._pod_history["user-alice/web"] = [{"t": "x", "phase": "Running", "restarts": 0, "ready": 1, "total": 1}]
        # 同名的 Pod 出現在另一個帳號的 namespace 下，不該互相看到彼此的歷史
        assert web_demo._get_pod_history("user-bob", "web-7d6b8c9f4-xk2pq") == []

    def test_returns_a_copy_not_the_live_list(self):
        web_demo._pod_history["user-alice/web"] = [{"t": "x", "phase": "Running", "restarts": 0, "ready": 1, "total": 1}]
        history = web_demo._get_pod_history("user-alice", "web-7d6b8c9f4-xk2pq")
        history.append({"t": "y", "phase": "Running", "restarts": 99, "ready": 1, "total": 1})
        # 呼叫端修改回傳值不該汙染原始儲存
        assert len(web_demo._pod_history["user-alice/web"]) == 1


class TestAppKeyForPodName:
    """`_app_key_for_pod_name()` 把真實 K8s Pod 名稱（兩段隨機後綴）還原成
    穩定的 Deployment／app 名稱，連字號本身是 app 名稱一部分的情況也要正確
    處理（例如 "web-frontend-123" 這種 app 名稱裡也有連字號的）。"""

    def test_standard_three_segment_pod_name(self):
        assert web_demo._app_key_for_pod_name("healer-demo-758dbf949-x6vz9") == "healer-demo"

    def test_app_name_itself_contains_hyphens(self):
        assert web_demo._app_key_for_pod_name("web-frontend-123-774c969d47-llktd") == "web-frontend-123"

    def test_falls_back_to_original_name_when_no_hyphens(self):
        assert web_demo._app_key_for_pod_name("standalonepod") == "standalonepod"
