"""Healer 頁「自動修復紀錄」改從事件紀錄讀（2026-10-21）。

原本讀記憶體裡的 recent_actions，web_demo 重啟就清空，畫面說「還沒有自動修復過」；
而且那份清單所有帳號共用，會看到別人 namespace 的修復。
"""
import time

import pytest

from core import event_store


@pytest.fixture()
def web_demo(tmp_path, monkeypatch):
    wd = pytest.importorskip("web_demo")
    event_store.init(str(tmp_path / "heal.db"))
    yield wd
    event_store.init(str(tmp_path / "other.db"))


def test_history_survives_restart_and_is_newest_first(web_demo, tmp_path):
    path = str(tmp_path / "heal.db")
    event_store.record("user-a", "heal", "web", "ok",
                       {"pod": "web-1", "action": "increase_memory", "action_label": "調高記憶體上限",
                        "root_cause": "記憶體不足"}, ts=time.time() - 60)
    event_store.record("user-a", "heal", "api", "inspect",
                       {"pod": "api-1", "action": "create_config", "action_label": "檢查設定檔",
                        "root_cause": "缺少設定檔", "llm_raw_guess": "OOM Killed"})
    event_store.init(str(tmp_path / "x.db"))
    event_store.init(path)   # 模擬重開機：重新開啟同一個資料庫
    h = web_demo._heal_history("user-a")
    assert [x["pod"] for x in h] == ["api-1", "web-1"]
    assert h[0]["needs_user"] is True and h[0]["ok"] is None
    assert h[0]["llm_raw_guess"] == "OOM Killed"
    assert h[1]["ok"] is True and h[1]["action"] == "調高記憶體上限"
    assert h[1]["time"].endswith("Z")


def test_history_is_scoped_to_namespace(web_demo):
    event_store.record("user-other", "heal", "secret", "ok", {"pod": "secret-1"})
    event_store.record("user-a", "deploy", "web", "ok")   # 其他種類不算修復紀錄
    assert web_demo._heal_history("user-a") == []


def test_history_respects_limit(web_demo):
    for i in range(5):
        event_store.record("user-a", "heal", "web", "fail", {"pod": f"web-{i}"}, ts=time.time() - 100 + i)
    h = web_demo._heal_history("user-a", limit=3)
    assert [x["pod"] for x in h] == ["web-4", "web-3", "web-2"]
    assert all(x["ok"] is False for x in h)


def test_status_route_reads_own_namespace(web_demo, monkeypatch):
    event_store.record("user-other", "heal", "secret", "ok", {"pod": "secret-1"})
    event_store.record("user-me", "heal", "web", "ok", {"pod": "web-1", "action_label": "重新建立 Pod"})
    c = web_demo.app.test_client()
    with c.session_transaction() as s:
        s["username"] = "me"
    monkeypatch.setattr(web_demo, "_session_ns", lambda: "user-me")
    d = c.get("/api/healer/status").get_json()
    assert [a["pod"] for a in d["recent_actions"]] == ["web-1"]
