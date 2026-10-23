"""預約任務：儲存規則、到期只提醒不執行、帳號隔離、唯讀 session、意圖判斷。"""
import json
import time

import pytest

from core import schedule_store as st

NOW = 1_800_000_000.0


@pytest.fixture(autouse=True)
def _db(tmp_path):
    st.init(str(tmp_path / "schedule.db"))
    yield


def _mk(user="alice", kind="scale", args=None, run_at=NOW + 3600):
    return st.create(user, f"user-{user}", kind, args or {"name": "web", "replicas": 3}, "把 web 調成 3 個副本", run_at, now=NOW)


def test_time_limits():
    with pytest.raises(st.ScheduleError):
        _mk(run_at=NOW + 60)                      # 不到 2 分鐘
    with pytest.raises(st.ScheduleError):
        _mk(run_at=NOW + 91 * 86400)              # 超過 90 天
    with pytest.raises(st.ScheduleError):
        st.create("alice", "user-alice", "rollback", {}, "x", NOW + 3600, now=NOW)   # 不支援的種類


def test_active_limit_per_user():
    for _ in range(st.MAX_ACTIVE):
        _mk()
    with pytest.raises(st.ScheduleError):
        _mk()
    _mk(user="bob")   # 別人不受影響


def test_due_only_reminds_once_and_never_executes():
    t = _mk(run_at=NOW + 600)
    assert st.collect_due(now=NOW + 300) == []
    due = st.collect_due(now=NOW + 601)
    assert [d["id"] for d in due] == [t["id"]]
    assert st.collect_due(now=NOW + 700) == []            # 只提醒一次
    assert st.get("alice", t["id"])["status"] == "due"    # 狀態是「等你確認」，不是執行過


def test_snooze_hides_until_later():
    t = _mk(run_at=NOW + 600)
    st.collect_due(now=NOW + 601)
    st.snooze("alice", t["id"], now=NOW + 610)
    assert st.due_for("alice", now=NOW + 620) == []
    assert [x["id"] for x in st.due_for("alice", now=NOW + 610 + st.SNOOZE + 1)] == [t["id"]]


def test_start_claims_once():
    """兩個分頁同時按「確定執行」：只有一個搶得到。"""
    t = _mk(run_at=NOW + 600)
    st.collect_due(now=NOW + 601)
    assert st.start("alice", t["id"]) is not None
    assert st.start("alice", t["id"]) is None
    assert st.finish("alice", t["id"], "done", "已部署")["status"] == "done"
    assert st.cancel("alice", t["id"]) is None           # 完成後不能取消


def test_expire_and_stuck_running():
    a = _mk(run_at=NOW + 600)
    b = _mk(run_at=NOW + 600)
    st.collect_due(now=NOW + 601)
    st.start("alice", b["id"], now=NOW + 700)
    st.collect_due(now=NOW + 700 + st.RUNNING_TIMEOUT + 1)
    assert st.get("alice", b["id"])["status"] == "failed"         # 按了執行卻沒回報結果
    st.collect_due(now=NOW + 600 + st.DUE_EXPIRE + 1)
    assert st.get("alice", a["id"])["status"] == "expired"        # 等太久


def test_reschedule_and_isolation():
    t = _mk()
    assert st.get("bob", t["id"]) is None
    assert st.cancel("bob", t["id"]) is None
    assert st.reschedule("bob", t["id"], time.time() + 7200) is None
    r = st.reschedule("alice", t["id"], time.time() + 7200)
    assert r and r["status"] == "scheduled"
    with pytest.raises(st.ScheduleError):
        st.reschedule("alice", t["id"], time.time() + 10)


def test_notes():
    st.set_note("alice", "2026-10-23", "記得跟組員對齊分時擴縮")
    assert st.notes_between("alice", "2026-10-01", "2026-10-31") == {"2026-10-23": "記得跟組員對齊分時擴縮"}
    assert st.notes_between("bob", "2026-10-01", "2026-10-31") == {}
    st.set_note("alice", "2026-10-23", "")
    assert st.notes_between("alice", "2026-10-01", "2026-10-31") == {}


# ── 路由（需要 web_demo；CI 沒裝 Flask 時只跳過這些，上面的純邏輯照樣跑）──
@pytest.fixture
def wd():
    return pytest.importorskip("web_demo")


@pytest.fixture
def client(monkeypatch, wd):
    monkeypatch.setattr(wd, "_session_ns", lambda: "user-alice")
    c = wd.app.test_client()
    with c.session_transaction() as s:
        s["username"] = "alice"
    return c


def _post(c, url, body):
    return c.post(url, data=json.dumps(body), content_type="application/json")


def test_route_create_scale_and_list(client):
    r = _post(client, "/api/schedule", {"kind": "scale", "name": "web", "replicas": 3, "run_at": time.time() + 3600})
    assert r.status_code == 200 and r.get_json()["task"]["summary"] == "把 web 調成 3 個副本"
    assert len(client.get("/api/schedule?status=active").get_json()["tasks"]) == 1


@pytest.mark.parametrize("body", [
    {"kind": "scale", "name": "Bad Name", "replicas": 3},
    {"kind": "scale", "name": "web", "replicas": 0},
    {"kind": "update_image", "name": "web", "image": ""},
    {"kind": "rollback", "name": "web"},
    {"kind": "delete", "name": "web", "run_at": "soon"},
], ids=["bad-name", "zero-replicas", "no-image", "rollback", "bad-time"])
def test_route_rejects_bad_input(client, body):
    body = dict(body)
    body.setdefault("run_at", time.time() + 3600)
    r = _post(client, "/api/schedule", body)
    assert r.status_code == 400 and r.get_json()["error"]


def test_route_deploy_blocked_by_review_cannot_be_scheduled(client, monkeypatch, wd):
    monkeypatch.setattr(wd, "_prepare_deploy", lambda text, spec: (
        {"app_name": "x"}, {}, {"decision": "block", "blockers": ["[成本] 200 個副本超過上限"]}, None))
    r = _post(client, "/api/schedule", {"kind": "deploy", "spec": {"app_name": "x", "image": "nginx", "pods": 200, "port": 80},
                                        "run_at": time.time() + 3600})
    assert r.status_code == 400 and "審查沒有通過" in r.get_json()["error"]


def test_route_other_users_task_not_found(client):
    t = st.create("bob", "user-bob", "delete", {"name": "web"}, "刪除服務 web", time.time() + 3600)
    assert _post(client, f"/api/schedule/{t['id']}/cancel", {}).status_code == 404


def test_calendar_and_readonly(client):
    _post(client, "/api/calendar/note", {"day": "2026-10-23", "text": "測試備註"})
    d = client.get("/api/calendar?month=2026-10").get_json()
    assert d["days"]["2026-10-23"]["note"] == "測試備註"
    with client.session_transaction() as s:
        s["readonly"] = True
    assert client.get("/api/calendar?month=2026-10").status_code == 200
    for url in ("/api/schedule", "/api/calendar/note", "/api/schedule/1/cancel", "/api/schedule/when"):
        assert _post(client, url, {}).status_code == 403, url
    assert client.get("/api/schedule").status_code == 403


@pytest.mark.parametrize("text,action", [
    ("我有哪些預約？", "list_schedule"), ("下週要做什麼", "list_schedule"), ("列出預約", "list_schedule"),
    ("取消明天的部署預約", "cancel_schedule"), ("把 sched-test 的預約改到 5 點", "reschedule"),
], ids=["list-q", "next-week", "list", "cancel", "reschedule"])
def test_schedule_intents(wd, text, action):
    assert wd._rule_intent(text)["action"] == action
