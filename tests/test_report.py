"""事件紀錄（core/event_store.py）與分析報告（agents/report.py）的純邏輯測試。"""
import time

import pytest

from agents.report import build_report, parse_period, validate_narrative, _issue_key
from core import event_store


@pytest.fixture()
def store(tmp_path):
    event_store.init(str(tmp_path / "events.db"))
    yield event_store
    event_store.init(str(tmp_path / "other.db"))


def test_query_is_scoped_to_namespace(store):
    store.record("user-a", "deploy", "web", "ok", {"image": "nginx"})
    store.record("user-b", "deploy", "secret-app", "ok", {"image": "redis"})
    a = store.query("user-a")
    assert [e["app"] for e in a] == ["web"]
    assert store.query("") == []                       # 沒有 namespace 就什麼都不回
    assert not store.record("", "deploy", "x", "ok")    # 也不能寫沒有 namespace 的紀錄
    assert not store.record("user-a", "nonsense", "x")  # 不認識的種類不收


def test_retention_cleanup(tmp_path):
    path = str(tmp_path / "old.db")
    event_store.init(path)
    event_store.record("user-a", "deploy", "old", "ok", ts=time.time() - (event_store.RETENTION_DAYS + 5) * 86400)
    event_store.record("user-a", "deploy", "new", "ok")
    event_store.init(str(tmp_path / "x.db"))
    event_store.init(path)   # 重新開啟時清掉過期紀錄
    assert [e["app"] for e in event_store.query("user-a")] == ["new"]


def _ev(kind, app=None, outcome="ok", data=None, ago_days=1.0, now=None):
    return {"ts": (now or time.time()) - ago_days * 86400, "kind": kind, "app": app, "outcome": outcome,
            "data": data or {}}


def test_report_numbers():
    now = time.time()
    events = [
        _ev("deploy", "web", "ok", {"image": "nginx", "pods": 2}, 3, now),
        _ev("deploy", "api", "fail", {"error": "timeout"}, 2, now),
        _ev("review", "big", "block", {"blockers": ["[成本] 每月費用過高", "[安全] 使用特權容器"], "monthly_usd": 120.5}, 2, now),
        _ev("review", "web", "warn", {"issues": ["[0] Deployment/web: 容器 web 缺少 readinessProbe"]}, 3, now),
        _ev("review", "api", "warn", {"issues": ["[0] Deployment/api: 容器 api 缺少 readinessProbe"]}, 2, now),
        _ev("heal", "web", "ok", {"root_cause": "記憶體不足", "action": "increase_memory"}, 1, now),
        _ev("scale", "web", "ok", {"replicas": 3}, 1, now),
        _ev("deploy", "ancient", "ok", {}, 60, now),   # 期間外
    ]
    f = build_report(events, 30, now=now, live={"services": 2, "unhealthy": 1})
    assert f["deploys"] == {"total": 2, "ok": 1, "failed": 1, "apps": 1, "top_apps": [("web", 1)]}
    assert f["reviews"]["block"] == 1 and f["reviews"]["warn"] == 2
    assert f["reviews"]["block_by_category"] == {"成本": 1, "安全": 1} or \
        set(f["reviews"]["block_by_category"]) == {"成本", "安全"}
    assert f["reviews"]["blocked_cost_usd"] == 120.5
    # 不同容器的同一種提醒要歸成同一類
    assert f["reviews"]["top_warnings"][0][1] == 2
    assert f["heals"]["total"] == 1 and f["heals"]["top_causes"] == [("記憶體不足", 1)]
    assert f["ops"]["scale"] == {"total": 1, "ok": 1}
    assert f["incidents"]["days_without_incident"] == 1
    assert sum(d["deploys"] for d in f["series"]) == 1
    texts = " ".join(o["text"] for o in f["observations"])
    assert "擋下 1 次" in texts and "1 個 Pod 狀態不正常" in texts


def test_empty_report_says_so():
    f = build_report([], 7, first_record_ts=None)
    assert not f["has_data"]
    assert "沒有任何紀錄" in f["observations"][0]["text"]


def test_issue_key_groups_containers():
    assert _issue_key("[0] Deployment/a: 容器 a 缺少 livenessProbe") == _issue_key("容器 b-2 缺少 livenessProbe")


def test_narrative_drops_sentences_with_unknown_numbers():
    f = build_report([_ev("deploy", "web", "ok", {}, 1), _ev("review", "x", "block", {"blockers": ["[安全] x"]}, 1)], 30)
    text = ("這段期間部署成功 1 次。\n審查擋下 1 次有風險的部署。\n下個月預計可以省下 350 美元。\n"
            "整體來說服務很穩定。")
    out = validate_narrative(text, f)
    assert "下個月預計可以省下 350 美元。" not in out["sentences"]
    assert out["dropped"] == 1
    assert len(out["sentences"]) == 3


def test_narrative_allows_percentages_from_known_numbers():
    f = build_report([_ev("deploy", "a", "ok", {}, 1), _ev("deploy", "b", "fail", {}, 1)], 30)
    out = validate_narrative("兩次部署有 50% 失敗。", f)
    assert out["dropped"] == 0


@pytest.mark.parametrize("text,days", [
    ("給我這個月的分析報告", 30), ("週報", 7), ("今天的報告", 1), ("過去 14 天的報表", 14),
    ("這一季的報告", 90), ("全部的紀錄做成報告", 180), ("報告", 30), ("999 天的報告", 180),
])
def test_parse_period(text, days):
    assert parse_period(text) == days


def test_web_report_route_is_scoped_and_records_events(monkeypatch, tmp_path):
    web_demo = pytest.importorskip("web_demo")
    event_store.init(str(tmp_path / "route.db"))
    event_store.record("user-other", "deploy", "not-mine", "ok")
    c = web_demo.app.test_client()
    with c.session_transaction() as s:
        s["username"] = "reporter"
    monkeypatch.setattr(web_demo, "K8S_ENABLED", False, raising=False)
    monkeypatch.setattr(web_demo, "_session_ns", lambda: "user-reporter")
    monkeypatch.setattr(web_demo, "_report_live", lambda ns: {})
    r = c.get("/api/report?q=週報&ai=0")
    assert r.status_code == 200
    d = r.get_json()
    assert d["period"]["days"] == 7 and d["namespace"] == "user-reporter"
    assert all(e.get("app") != "not-mine" for e in d["recent"])
    # 操作完會自動記一筆（K8s 沒連線時擴縮失敗，也要記成失敗）
    c.post("/api/scale", json={"name": "web", "replicas": 2})
    rows = event_store.query("user-reporter", kinds=["scale"])
    assert rows and rows[-1]["outcome"] == "fail" and rows[-1]["data"]["replicas"] == 2
    assert not event_store.query("user-other", kinds=["scale"])
