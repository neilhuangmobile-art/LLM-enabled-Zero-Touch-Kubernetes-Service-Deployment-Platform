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
        "processing": None,
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

    def test_actively_processing_an_issue_not_falsely_flagged_as_stuck(self, monkeypatch):
        """2026-10-03 使用者錄 demo 時實測到的真實 bug：根因診斷固定會打一次
        本地 LLM，實測一次 2-3 分鐘跑不掉，`last_scan` 要等整批 issue 都處理完
        才會更新——原本的 staleness 判斷只看 `last_scan` 多舊（> interval*3 =
        90 秒），導致 Healer 正在認真修東西的時候，使用者反而看到「自動監控
        似乎卡住了」的假警報，剛好是最不該報假警報的時刻。現在 `last_scan` 雖然
        已經很舊（10 分鐘），但只要 `processing` 顯示「最近才開始處理一個
        issue」，就不該被判定成卡住。"""
        monkeypatch.setattr(web_demo, "K8S_ENABLED", True)
        monkeypatch.setattr(web_demo, "_healer_bg_thread", _FakeThread(alive=True))
        web_demo._healer_bg_state["last_scan"] = (datetime.utcnow() - timedelta(minutes=10)).isoformat() + "Z"
        web_demo._healer_bg_state["started_at"] = (datetime.utcnow() - timedelta(minutes=20)).isoformat() + "Z"
        web_demo._healer_bg_state["interval"] = 30
        web_demo._healer_bg_state["processing"] = {
            "pod": "healer-demo-758dbf949-x6vz9", "namespace": "user-neilhuang-mobile",
            "reason": "OOMKilled", "since": (datetime.utcnow() - timedelta(minutes=1)).isoformat() + "Z",
        }
        result = web_demo._healer_bg_liveness()
        assert result["running"] is True
        assert result["message"] is None
        assert result["processing"]["pod"] == "healer-demo-758dbf949-x6vz9"

    def test_processing_but_stuck_too_long_still_flagged_stale(self, monkeypatch):
        """對照組：`processing` 存在，但 `since` 本身也已經久到不合理（超過
        interval*20 = 10 分鐘，涵蓋多個 issue 排隊處理的情況），還是要老實
        回報卡住，不能因為有 `processing` 欄位就無條件放行。"""
        monkeypatch.setattr(web_demo, "K8S_ENABLED", True)
        monkeypatch.setattr(web_demo, "_healer_bg_thread", _FakeThread(alive=True))
        web_demo._healer_bg_state["last_scan"] = (datetime.utcnow() - timedelta(minutes=30)).isoformat() + "Z"
        web_demo._healer_bg_state["started_at"] = (datetime.utcnow() - timedelta(minutes=40)).isoformat() + "Z"
        web_demo._healer_bg_state["interval"] = 30
        web_demo._healer_bg_state["processing"] = {
            "pod": "stuck-pod", "namespace": "default", "reason": "OOMKilled",
            "since": (datetime.utcnow() - timedelta(minutes=30)).isoformat() + "Z",
        }
        result = web_demo._healer_bg_liveness()
        assert result["running"] is False
        assert result["message"] is not None


class TestUtcIsoHelpers:
    """`_utc_now_iso()`／`_parse_utc_iso()`：2026-10-03 使用者實測發現 Healer
    頁面的時間軸顯示比實際時間晚 8 小時——根因是舊的
    `datetime.utcnow().isoformat()` 沒有時區標記，前端 JS 的 `new Date(...)`
    會把它當成「當地時間」解析，不是 UTC。"""

    def test_now_iso_ends_with_z(self):
        assert web_demo._utc_now_iso().endswith("Z")

    def test_parse_utc_iso_roundtrips_through_now_iso(self):
        s = web_demo._utc_now_iso()
        parsed = web_demo._parse_utc_iso(s)
        assert abs((datetime.utcnow() - parsed).total_seconds()) < 2

    def test_parse_utc_iso_also_accepts_strings_without_z(self):
        """舊資料（修法之前存進去、沒有 "Z" 的字串）也要能正常解析，不能一改
        格式就讓舊資料全部解析失敗。"""
        s = datetime.utcnow().isoformat()
        parsed = web_demo._parse_utc_iso(s)
        assert abs((datetime.utcnow() - parsed).total_seconds()) < 2
