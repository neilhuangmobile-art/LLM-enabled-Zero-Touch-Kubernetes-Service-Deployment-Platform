"""手機推播通知（core/push.py）：endpoint 允許清單、裝置上限、失效訂閱清除、通知內容、帳號隔離。"""
import pytest

from core import push

KEYS = {"p256dh": "B" + "A" * 86, "auth": "x" * 22}


def _sub(endpoint="https://fcm.googleapis.com/fcm/send/abc123"):
    return {"endpoint": endpoint, "keys": dict(KEYS)}


@pytest.fixture(autouse=True)
def _db(tmp_path):
    push.init(str(tmp_path / "push.db"))
    yield


@pytest.mark.parametrize("endpoint", [
    "https://fcm.googleapis.com/fcm/send/abc",
    "https://updates.push.services.mozilla.com/wpush/v2/abc",
    "https://web.push.apple.com/QGx",
    "https://wns2-par02p.notify.windows.com/w/?token=abc",
], ids=["fcm", "mozilla", "apple", "windows"])
def test_known_push_services_are_accepted(endpoint):
    assert push.validate_subscription(_sub(endpoint)) is None


@pytest.mark.parametrize("endpoint", [
    "http://fcm.googleapis.com/fcm/send/abc",            # 不是 https
    "https://127.0.0.1/steal",                            # 內網
    "https://169.254.169.254/latest/meta-data",           # 雲端中繼資料
    "https://evil.example.com/fcm.googleapis.com",        # 網域不在清單，只是路徑長得像
    "https://fcm.googleapis.com.evil.com/x",              # 後綴偽裝
    "https://fcm.googleapis.com:8443/x",                  # 非 443 port
    "https://user:pw@fcm.googleapis.com/x",               # 帶帳密
], ids=["http", "loopback", "metadata", "path-trick", "suffix-trick", "port", "userinfo"])
def test_endpoint_outside_allowlist_is_rejected(endpoint):
    """伺服器會主動連 endpoint，不能讓人填任意網址（SSRF）。"""
    assert push.validate_subscription(_sub(endpoint))
    assert push.add("alice", "user-alice", _sub(endpoint)) is not None
    assert push.for_namespace("user-alice") == []


def test_bad_keys_are_rejected():
    s = _sub()
    s["keys"]["auth"] = "<script>"
    assert push.validate_subscription(s)
    assert push.validate_subscription("not a dict")


def test_device_limit_per_user():
    for i in range(push.MAX_DEVICES_PER_USER):
        assert push.add("alice", "user-alice", _sub(f"https://fcm.googleapis.com/fcm/send/d{i}")) is None
    err = push.add("alice", "user-alice", _sub("https://fcm.googleapis.com/fcm/send/one-more"))
    assert err and "5" in err
    # 同一台裝置重新訂閱不算新的一台
    assert push.add("alice", "user-alice", _sub("https://fcm.googleapis.com/fcm/send/d0")) is None


def test_notifications_go_only_to_own_namespace():
    push.add("alice", "user-alice", _sub("https://fcm.googleapis.com/fcm/send/a"))
    push.add("bob", "user-bob", _sub("https://fcm.googleapis.com/fcm/send/b"))
    sent = []
    r = push.send_to_namespace("user-alice", {"title": "t"}, sender=lambda s, d: sent.append(s["endpoint"]))
    assert r["sent"] == 1 and sent == ["https://fcm.googleapis.com/fcm/send/a"]


def test_same_device_switching_account_moves_to_new_account():
    ep = "https://fcm.googleapis.com/fcm/send/shared"
    push.add("alice", "user-alice", _sub(ep))
    push.add("bob", "user-bob", _sub(ep))
    assert push.for_namespace("user-alice") == []
    assert [s["endpoint"] for s in push.for_namespace("user-bob")] == [ep]
    assert not push.remove("alice", ep)   # 不能刪別人的裝置


class _Gone(Exception):
    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.response = type("R", (), {"status_code": status})()


def test_expired_subscription_is_removed():
    push.add("alice", "user-alice", _sub())

    def gone(s, d):
        raise _Gone(410)
    assert push.send_to_namespace("user-alice", {"title": "t"}, sender=gone)["removed"] == 1
    assert push.for_namespace("user-alice") == []


def test_temporary_failure_keeps_subscription():
    push.add("alice", "user-alice", _sub())

    def down(s, d):
        raise _Gone(503)
    assert push.send_to_namespace("user-alice", {"title": "t"}, sender=down)["failed"] == 1
    assert len(push.for_namespace("user-alice")) == 1


def test_heal_message_is_short_and_has_no_raw_logs():
    m = push.heal_message("orders-api", "inspect", "無法連線到資料庫\nFATAL password=hunter2 " + "x" * 300,
                          "檢查相依服務", "orders-api")
    assert "需要你處理" in m["title"]
    assert "\n" not in m["body"] and len(m["body"]) <= push.MAX_TEXT
    assert m["url"] == "/?page=healer&pod=orders-api"
    ok = push.heal_message("web", "ok", "記憶體不足", "調高記憶體上限", "web")
    assert "已自動修復" in ok["title"] and "調高記憶體上限" in ok["body"]
    # Pod 名稱只留合法字元，不能拿來塞網址參數
    assert push.heal_message("a", "ok", "c", "l", "x&y=<b>")["url"] == "/?page=healer&pod=xyb"


def test_healer_processing_hidden_from_other_accounts(monkeypatch):
    """「正在修復 X」是全平台共用的一份狀態，別的帳號不能看到（2026-10-22 PWA 實測時發現）。"""
    web_demo = pytest.importorskip("web_demo")
    monkeypatch.setattr(web_demo, "_healer_bg_liveness", lambda: {
        "running": True, "processing": {"pod": "orders-api-1-2", "namespace": "user-alice", "reason": "Error"}})
    monkeypatch.setattr(web_demo, "_heal_history", lambda ns: [])
    c = web_demo.app.test_client()
    for who, ns, visible in (("bob", "user-bob", False), ("alice", "user-alice", True)):
        monkeypatch.setattr(web_demo, "_session_ns", lambda ns=ns: ns)
        with c.session_transaction() as s:
            s["username"] = who
        d = c.get("/api/healer/status").get_json()
        assert bool(d["processing"]) is visible


def test_heal_push_follows_record_dedupe(tmp_path, monkeypatch):
    """推播跟事件紀錄用同一個去重：同一個問題重複發生只通知一次，原因不同才再通知。"""
    web_demo = pytest.importorskip("web_demo")
    from core import event_store
    event_store.init(str(tmp_path / "ev.db"))
    sent = []
    monkeypatch.setattr(web_demo._push, "send_async", lambda ns, msg: sent.append((ns, msg)))
    monkeypatch.setattr(web_demo, "_real_heal_pod_unrecorded",
                        lambda p, n: {"ok": True, "action": "check_dependencies", "root_cause": "無法連線到資料庫"})
    for _ in range(3):
        web_demo._real_heal_pod("orders-api-5bb884c797-54fcm", "user-push")
    assert len(sent) == 1 and sent[0][0] == "user-push"
    assert sent[0][1]["url"] == "/?page=healer&pod=orders-api"
    monkeypatch.setattr(web_demo, "_real_heal_pod_unrecorded",
                        lambda p, n: {"ok": True, "action": "increase_memory", "root_cause": "記憶體不足"})
    web_demo._real_heal_pod("orders-api-5bb884c797-54fcm", "user-push")
    assert len(sent) == 2 and "已自動修復" in sent[1][1]["title"]
