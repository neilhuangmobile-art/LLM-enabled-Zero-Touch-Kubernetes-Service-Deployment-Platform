"""共用背景排程器 core/scheduler.py（2026-10-21）：分時擴縮、休眠與之後的行事曆共用同一條背景執行緒。"""
import pytest

from core import scheduler


@pytest.fixture(autouse=True)
def clean():
    for n in scheduler.tasks():
        scheduler.unregister(n)
    yield
    for n in scheduler.tasks():
        scheduler.unregister(n)


def test_interval_is_respected():
    calls = []
    scheduler.register("每次", lambda: calls.append("每次"), every_seconds=0)
    scheduler.register("每 30 秒", lambda: calls.append("30"), every_seconds=30)
    assert scheduler.run_due(now=1000) == ["每次", "每 30 秒"]
    assert scheduler.run_due(now=1010) == ["每次"]
    assert scheduler.run_due(now=1029) == ["每次"]
    assert scheduler.run_due(now=1030) == ["每次", "每 30 秒"]
    assert calls.count("30") == 2


def test_one_failing_task_does_not_stop_others():
    calls = []

    def boom():
        raise RuntimeError("壞掉")

    scheduler.register("壞的", boom)
    scheduler.register("好的", lambda: calls.append(1))
    assert scheduler.run_due(now=1) == ["好的"]
    assert calls == [1]
    assert scheduler.run_due(now=2) == ["好的"]     # 壞的下次照樣會再試，排程器沒有停


def test_k8s_tasks_skip_when_cluster_down_and_run_later():
    calls = []
    scheduler.register("要叢集", lambda: calls.append("k8s"), every_seconds=30, needs_k8s=True)
    scheduler.register("不用叢集", lambda: calls.append("free"), every_seconds=30, needs_k8s=False)
    assert scheduler.run_due(now=100, k8s_ready=False) == ["不用叢集"]
    # 叢集恢復：沒跑過的工作馬上補跑，不用再等 30 秒
    assert scheduler.run_due(now=110, k8s_ready=True) == ["要叢集"]


def test_register_same_name_replaces():
    calls = []
    scheduler.register("行事曆", lambda: calls.append("舊"))
    scheduler.register("行事曆", lambda: calls.append("新"))
    scheduler.run_due(now=1)
    assert calls == ["新"]
    assert scheduler.tasks() == ["行事曆"]


def test_web_demo_registers_autoscale_tasks():
    wd = pytest.importorskip("web_demo")
    wd._register_autoscale_tasks()
    assert set(scheduler.tasks()) == {"分時擴縮／休眠：喚醒", "分時擴縮：套用排程", "休眠：閒置判斷"}
