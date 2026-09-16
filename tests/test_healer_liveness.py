"""
測試 web_demo.py 的 _healer_bg_liveness()：2026-09-14 修正「/api/healer/status 的
running 只在啟動那一刻設一次，之後執行緒死掉也永遠回報 True」這個違反 AGENT_RULES.md
「判斷即時、不用舊值唬弄」原則的 bug。改成即時查 Thread.is_alive() + 比對 last_scan
時間戳有沒有久到不正常（執行緒卡住不會被 is_alive() 抓到，只能靠時間戳判斷）。

標記 integration：import web_demo.py 會連帶 import torch。
"""
import sys
import os
import threading
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = pytest.mark.integration

# collection 階段就會 import，@pytest.mark.integration 擋不住 CI 沒裝 flask/torch
# 時直接炸掉整個 pytest run（見 test_model_server_injection.py 的說明，實測驗證過）。
web_demo = pytest.importorskip("web_demo")


class _FakeThread:
    def __init__(self, alive):
        self._alive = alive

    def is_alive(self):
        return self._alive


@pytest.fixture(autouse=True)
def reset_state(monkeypatch):
    monkeypatch.setattr(web_demo, "_healer_bg_state", {
        "last_scan": None, "recent_actions": [], "started_at": None, "interval": 30,
    })
    yield


class TestHealerLiveness:
    def test_thread_dead_reports_not_running_with_actionable_message(self, monkeypatch):
        monkeypatch.setattr(web_demo, "K8S_ENABLED", True)
        monkeypatch.setattr(web_demo, "_healer_bg_thread", _FakeThread(alive=False))
        result = web_demo._healer_bg_liveness()
        assert result["running"] is False
        assert result["thread_alive"] is False
        assert result["message"] is not None
        assert "重新啟動" in result["message"] or "restart" in result["message"].lower()

    def test_thread_alive_and_fresh_scan_reports_running(self, monkeypatch):
        monkeypatch.setattr(web_demo, "K8S_ENABLED", True)
        monkeypatch.setattr(web_demo, "_healer_bg_thread", _FakeThread(alive=True))
        web_demo._healer_bg_state["last_scan"] = datetime.utcnow().isoformat()
        web_demo._healer_bg_state["started_at"] = (datetime.utcnow() - timedelta(seconds=60)).isoformat()
        result = web_demo._healer_bg_liveness()
        assert result["running"] is True
        assert result["message"] is None

    def test_thread_alive_but_scan_very_stale_reports_stuck(self, monkeypatch):
        # 這是 is_alive() 抓不到的情境：執行緒技術上還活著（例如卡在一個沒有逾時的
        # K8s API 呼叫），但已經很久沒有前進、沒有新的掃描紀錄。
        monkeypatch.setattr(web_demo, "K8S_ENABLED", True)
        monkeypatch.setattr(web_demo, "_healer_bg_thread", _FakeThread(alive=True))
        web_demo._healer_bg_state["last_scan"] = (datetime.utcnow() - timedelta(minutes=10)).isoformat()
        web_demo._healer_bg_state["started_at"] = (datetime.utcnow() - timedelta(minutes=20)).isoformat()
        web_demo._healer_bg_state["interval"] = 30
        result = web_demo._healer_bg_liveness()
        assert result["running"] is False
        assert result["thread_alive"] is True  # 執行緒本身沒死，只是卡住
        assert result["message"] is not None

    def test_just_started_within_grace_period_not_flagged_stale(self, monkeypatch):
        # 剛啟動、第一次掃描（30 秒後）還沒發生前，不該被誤判成「卡住」。
        monkeypatch.setattr(web_demo, "K8S_ENABLED", True)
        monkeypatch.setattr(web_demo, "_healer_bg_thread", _FakeThread(alive=True))
        web_demo._healer_bg_state["last_scan"] = None
        web_demo._healer_bg_state["started_at"] = datetime.utcnow().isoformat()
        web_demo._healer_bg_state["interval"] = 30
        result = web_demo._healer_bg_liveness()
        assert result["running"] is True

    def test_k8s_disabled_reports_not_running_without_alarming_message(self, monkeypatch):
        monkeypatch.setattr(web_demo, "K8S_ENABLED", False)
        monkeypatch.setattr(web_demo, "_healer_bg_thread", None)
        result = web_demo._healer_bg_liveness()
        assert result["running"] is False
        assert result["message"] is None
