"""分時擴縮與休眠：需要 web_demo（Flask）的測試——Chat 規則、API、Service 改接、排程套用。

2026-10-21 從 tests/test_autoscale.py 拆出來：CI 刻意不裝 flask，原本 importorskip 寫在檔案中間，
會連前面不需要 flask 的純邏輯測試一起跳過。純邏輯的留在 test_autoscale.py，CI 一定會跑。
"""
from datetime import datetime
from types import SimpleNamespace as NS

import pytest

web_demo = pytest.importorskip("web_demo")

from agents.timeslot import parse_schedule, TZ  # noqa: E402
from core import sleep_mode as sm  # noqa: E402


def _t(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=TZ)   # 2026-10-12 是週一


# ── 網站：規則、API、Service 改接 ──


@pytest.mark.parametrize("msg,action,args", [
    ("shop-web 白天 3 個、晚上 1 個", "as_schedule", {}),
    ("喚醒 shop-web", "as_wake", {"name": "shop-web"}),
    ("shop-web 不要休眠", "as_exempt_on", {"name": "shop-web"}),
    ("shop-web 可以休眠", "as_exempt_off", {"name": "shop-web"}),
    ("取消 shop-web 的排程", "as_cancel", {"name": "shop-web"}),
    ("閒置 3 天就休眠", "as_idle_days", {"days": 3}),
    ("休眠設定", "as_show", {}),
    ("把 web 擴到 3 個", "scale", {"name": "web", "replicas": 3}),     # 原本的擴縮不受影響
])
def test_chat_rules(msg, action, args):
    hit = web_demo._rule_intent(msg)
    assert hit and hit["action"] == action and hit["args"] == args


@pytest.mark.parametrize("msg", ["休眠模式是什麼？", "shop-web 可以休眠嗎", "幫我部署 nginx 白天 3 個"])
def test_chat_rules_skip_questions_and_deploys(msg):
    hit = web_demo._rule_intent(msg)
    assert not hit or not hit["action"].startswith("as_")


def _client(ns="user-as-test"):
    c = web_demo.app.test_client()
    with c.session_transaction() as s:
        s["username"] = "as-test"
    return c


def test_api_refuses_customer_cluster(monkeypatch):
    monkeypatch.setattr(web_demo, "_session_ns", lambda: "zt-someone-0123456789abcdef")
    monkeypatch.setattr(web_demo, "_is_remote_ns", lambda ns: True)
    r = _client().post("/api/autoscale/parse", json={"text": "web 白天 3 個、晚上 1 個"})
    assert r.status_code == 400 and "只支援平台自己的叢集" in r.get_json()["error"]


def test_api_schedule_rejects_tampered_plan(monkeypatch):
    monkeypatch.setattr(web_demo, "K8S_ENABLED", True, raising=False)
    monkeypatch.setattr(web_demo, "_session_ns", lambda: "user-as-test")
    bad = {"app": "web", "base": 1, "windows": [{"start": "08:00", "end": "20:00", "replicas": 500, "days": [0]}]}
    r = _client().post("/api/autoscale/schedule", json={"plan": bad})
    assert r.status_code == 400 and "副本數" in r.get_json()["error"]


def test_point_services_replaces_selector_and_restores(monkeypatch):
    """selector 是 map，patch 會合併：舊的 key 一定要設成 None，不然 Service 會同時要求兩組標籤、誰都選不到。"""
    patches = []

    class _Core:
        def patch_namespaced_service(self, name, ns, body, **kw):
            patches.append(body)

    monkeypatch.setattr(web_demo.k8s_client, "CoreV1Api", lambda *a: _Core())
    svc = NS(metadata=NS(name="web-svc", annotations={}), spec=NS(selector={"app": "web"}))
    web_demo._as_point_services("user-a", [svc], to_waker_of="web")
    assert patches[-1]["spec"]["selector"] == {"app": None, sm.LABEL_WAKER: "web"}
    redirected = NS(metadata=NS(name="web-svc", annotations={sm.ANN_ORIG_SELECTOR: '{"app": "web"}'}),
                    spec=NS(selector={sm.LABEL_WAKER: "web"}))
    web_demo._as_point_services("user-a", [redirected])
    assert patches[-1]["spec"]["selector"] == {sm.LABEL_WAKER: None, "app": "web"}
    assert patches[-1]["metadata"]["annotations"] == {sm.ANN_ORIG_SELECTOR: None}


class _FakeApps:
    def __init__(self, replicas, ann=None):
        self.dep = NS(metadata=NS(name="web", annotations=dict(ann or {}), labels={}),
                      spec=NS(replicas=replicas))
        self.scaled, self.patched = [], []

    def read_namespaced_deployment(self, name, ns, **kw):
        return self.dep

    def patch_namespaced_deployment_scale(self, name, ns, body, **kw):
        self.scaled.append(body["spec"]["replicas"])
        self.dep.spec.replicas = body["spec"]["replicas"]

    def patch_namespaced_deployment(self, name, ns, body, **kw):
        self.patched.append(body)


def test_schedule_only_acts_when_target_changes(monkeypatch, tmp_path):
    from core import autoscale_store as st
    st.init(str(tmp_path / "s.db"))
    apps = _FakeApps(replicas=1)
    monkeypatch.setattr(web_demo.k8s_client, "AppsV1Api", lambda *a: apps)
    monkeypatch.setattr(web_demo, "_check_scale_risk", lambda *a, **k: (False, None))
    plan = parse_schedule("web 白天 3 個、晚上 1 個")["plan"]
    st.save_schedule("user-a", plan)
    assert web_demo._as_apply_schedule("user-a", "web", plan, None, _t(2026, 10, 12, 10)) == 3
    assert apps.scaled == [3]
    apps.dep.spec.replicas = 5   # 使用者在白天手動調成 5 個
    assert web_demo._as_apply_schedule("user-a", "web", plan, 3, _t(2026, 10, 12, 15)) is None
    assert apps.dep.spec.replicas == 5                       # 手動調整保留到下一個時段
    assert web_demo._as_apply_schedule("user-a", "web", plan, 3, _t(2026, 10, 12, 21)) == 1
    assert apps.scaled == [3, 1]


def test_schedule_while_sleeping_only_updates_wake_target(monkeypatch, tmp_path):
    from core import autoscale_store as st
    st.init(str(tmp_path / "s2.db"))
    apps = _FakeApps(replicas=0, ann={sm.ANN_SLEEP_REPLICAS: "1"})
    monkeypatch.setattr(web_demo.k8s_client, "AppsV1Api", lambda *a: apps)
    plan = parse_schedule("web 白天 3 個、晚上 1 個")["plan"]
    st.save_schedule("user-a", plan)
    assert web_demo._as_apply_schedule("user-a", "web", plan, None, _t(2026, 10, 12, 10)) == 3
    assert apps.scaled == []                                 # 不會把休眠中的服務叫醒
    assert apps.patched[-1]["metadata"]["annotations"] == {sm.ANN_SLEEP_REPLICAS: "3"}


def test_waker_and_sleep_state_hidden_from_lists(monkeypatch):
    """喚醒器不是使用者的服務，服務列表與 Pod 列表都不能出現；休眠中的服務要標出來。"""
    from datetime import timezone
    ts = datetime(2026, 10, 11, tzinfo=timezone.utc)

    def dep(name, labels=None, ann=None, replicas=1):
        return NS(metadata=NS(name=name, labels=labels or {}, annotations=ann or {}, creation_timestamp=ts),
                  spec=NS(replicas=replicas, selector=NS(match_labels=None),
                          template=NS(spec=NS(containers=[NS(image="nginx:1.27")]))),
                  status=NS(ready_replicas=0, conditions=None))

    class _Apps:
        def list_namespaced_deployment(self, ns):
            return NS(items=[dep("web", ann={sm.ANN_SLEEP_REPLICAS: "2"}, replicas=0),
                             dep("zt-waker-web", labels={sm.LABEL_WAKER: "web"})])

    monkeypatch.setattr(web_demo, "K8S_ENABLED", True, raising=False)
    monkeypatch.setattr(web_demo.k8s_client, "AppsV1Api", lambda *a: _Apps())
    monkeypatch.setattr(web_demo.k8s_client, "CoreV1Api", lambda *a: None)
    rows = web_demo._local_k8s_get_deployments("user-a")
    assert [r["name"] for r in rows] == ["web"]
    assert rows[0]["sleeping"] is True and rows[0]["sleep_replicas"] == 2


@pytest.mark.parametrize("msg,expected", [
    ("晚上 8 點把 web 擴到 3 個", "scale"),           # 立即擴縮，不是設定時段
    ("把 web 在 10 點擴到 5 個", None),
    ("web 凌晨 3 點掛掉了 2 個 pod", None),           # 在描述問題
])
def test_schedule_rule_does_not_steal_other_sentences(msg, expected):
    """2026-10-21：分時擴縮規則排除「擴、縮、調、掛掉、壞、當機、錯誤」，這 3 句回到原本的判斷。"""
    assert (web_demo._rule_intent(msg) or {}).get("action") == expected


def test_idle_days_api_explains_limit(monkeypatch):
    monkeypatch.setattr(web_demo, "_as_guard", lambda: ("user-as-test", None))
    r = _client().post("/api/autoscale/settings", json={"idle_days": 30})
    assert r.status_code == 400 and "10 天" in r.get_json()["error"]


def test_cluster_timeout_is_not_reported_as_not_found(monkeypatch):
    """叢集沒回應時要說叢集的問題，不能說「找不到這個服務」。"""
    class _Slow:
        def read_namespaced_deployment(self, *a, **k):
            raise TimeoutError("read timed out")

    monkeypatch.setattr(web_demo, "_as_guard", lambda: ("user-as-test", None))
    monkeypatch.setattr(web_demo.k8s_client, "AppsV1Api", lambda *a: _Slow())
    r = _client().post("/api/autoscale/wake", json={"name": "web"})
    assert r.status_code == 503 and "沒有回應" in r.get_json()["error"]


# ── 2026-10-23：照柏碩的接手說明對齊（單一背景迴圈、推播、共用 /api/scale 的檢查、行事曆、手機唯讀）──
def test_bg_tick_runs_schedule_due_every_interval_and_autoscale_every_tick(monkeypatch):
    calls = []
    monkeypatch.setattr(web_demo._sched, "collect_due", lambda: calls.append("due") or [])
    monkeypatch.setattr(web_demo, "_as_run_due", lambda now=None: calls.append("as"))
    last = web_demo._bg_tick(1000, 0.0, 30)
    last = web_demo._bg_tick(1010, last, 30)
    last = web_demo._bg_tick(1030, last, 30)
    assert calls == ["due", "as", "as", "due", "as"]


def test_as_run_due_intervals_and_skips_without_k8s(monkeypatch):
    calls = []
    for n in ("_as_tick_wake", "_as_tick_schedules", "_as_tick_idle"):
        monkeypatch.setattr(web_demo, n, (lambda name: (lambda: calls.append(name)))(n))
    monkeypatch.setattr(web_demo, "_as_last_run", {"schedules": 0.0, "idle": 0.0})
    monkeypatch.setattr(web_demo, "K8S_ENABLED", False)
    web_demo._as_run_due(1000)
    assert calls == []
    monkeypatch.setattr(web_demo, "K8S_ENABLED", True)
    web_demo._as_run_due(1000)
    web_demo._as_run_due(1010)
    assert calls == ["_as_tick_wake", "_as_tick_schedules", "_as_tick_idle", "_as_tick_wake"]


def test_scale_check_shared_rules(monkeypatch):
    monkeypatch.setattr(web_demo, "_check_scale_risk", lambda name, n, ns: (n > 8, "叢集空間不夠"))
    assert web_demo._scale_check("web", 0, "user-a")[2] == 400                  # 使用者不能到 0
    assert web_demo._scale_check("web", 0, "user-a", allow_zero=True)[0]        # 平台內部的休眠可以
    assert web_demo._scale_check("web", 9, "user-a")[:2] == (False, "叢集空間不夠")
    assert web_demo._scale_check("web", 3, "user-a")[0]


def test_api_scale_still_rejects_zero(monkeypatch):
    monkeypatch.setattr(web_demo, "_session_ns", lambda: "user-as-test")
    monkeypatch.setattr(web_demo, "_is_remote_ns", lambda ns: False)
    monkeypatch.setattr(web_demo, "K8S_ENABLED", True)
    r = _client().post("/api/scale", json={"name": "web", "replicas": 0})
    assert r.status_code == 400 and "1 到" in r.get_json()["error"]


def test_scheduled_scale_pushes_only_when_automatic(monkeypatch, tmp_path):
    from core import autoscale_store as st
    st.init(str(tmp_path / "push.db"))
    sent = []
    monkeypatch.setattr(web_demo._push, "send_async", lambda ns, msg: sent.append((ns, msg)))
    monkeypatch.setattr(web_demo, "_check_scale_risk", lambda *a, **k: (False, None))
    apps = _FakeApps(replicas=1)
    monkeypatch.setattr(web_demo.k8s_client, "AppsV1Api", lambda *a: apps)
    plan = parse_schedule("web 白天 3 個、晚上 1 個")["plan"]
    web_demo._as_apply_schedule("user-a", "web", plan, None, _t(2026, 10, 12, 10))                 # 使用者按套用：不推播
    assert sent == []
    web_demo._as_apply_schedule("user-a", "web", plan, 3, _t(2026, 10, 12, 21), notify=True)       # 背景自動：推播
    assert sent and sent[-1][0] == "user-a" and "3 個調成 1 個" in sent[-1][1]["body"]
    assert sent[-1][1]["url"] == "/m#services" and sent[-1][1]["tag"] == "autoscale-web"


def test_calendar_and_readonly_include_autoscale():
    assert web_demo._CAL_EVENT_LABEL["sleep"] == "休眠" and web_demo._CAL_EVENT_LABEL["wake"] == "喚醒"
    assert any(m == "GET" and rx.match("/api/autoscale") for m, rx in web_demo._READONLY_API)
    assert not any(m != "GET" and rx.match("/api/autoscale/schedule") for m, rx in web_demo._READONLY_API)
