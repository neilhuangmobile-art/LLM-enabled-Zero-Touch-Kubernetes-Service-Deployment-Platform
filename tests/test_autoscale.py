"""分時擴縮與休眠模式（2026-10-11）：時段解析、省錢換算、閒置判斷、喚醒器設定、設定儲存、Chat 規則與 API。"""
from datetime import datetime
from types import SimpleNamespace as NS

import pytest

from agents.timeslot import (parse_schedule, replicas_at, validate_plan, savings, describe, next_change, TZ,
                             cost_per_replica_hour)
from core import sleep_mode as sm


def _t(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=TZ)   # 2026-10-12 是週一


# ── 時段解析 ──
@pytest.mark.parametrize("text,app,rows", [
    ("shop-web 白天 3 個、晚上 1 個", "shop-web", [("每天 08:00 到 20:00", 3), ("每天 20:00 到隔天 08:00", 1)]),
    ("shop-web 早上 8 點到晚上 10 點開 3 個，其他時間 1 個", "shop-web", [("每天 08:00 到 22:00", 3), ("其他時間", 1)]),
    ("把 api2 平日 9:00-18:00 開 5 個，其他時間 1 個", "api2", [("平日（週一到週五） 09:00 到 18:00", 5), ("其他時間", 1)]),
    ("shop-web 平日 2 個，週末 1 個", "shop-web", [("平日（週一到週五） 整天", 2), ("週末 整天", 1)]),
    ("shop-web 晚上 10 點到早上 6 點 1 個，其他 4 個", "shop-web", [("每天 22:00 到隔天 06:00", 1), ("其他時間", 4)]),
    ("shop-web 下午1點到5點 4 個，其餘 2 個", "shop-web", [("每天 13:00 到 17:00", 4), ("其他時間", 2)]),
    ("shop-web 8點半到17點 3 pods, 其他時間 1 pod", "shop-web", [("每天 08:30 到 17:00", 3), ("其他時間", 1)]),
])
def test_parse_schedule(text, app, rows):
    r = parse_schedule(text)
    assert r["ok"], r
    assert r["plan"]["app"] == app
    assert [(x["when"], x["replicas"]) for x in describe(r["plan"])] == rows


@pytest.mark.parametrize("text,needle", [
    ("白天 3 個晚上 1 個", "哪一個服務"),
    ("shop-web 白天 3 個", "其他時間要開幾個"),                          # 晚上沒有涵蓋到
    ("shop-web 9點到18點 3個，12點到13點 5個，其他時間 1個", "時段重疊"),   # 互相矛盾
    ("shop-web 開 3 個", "沒有講是哪個時段"),
    ("shop-web 白天 0 個，晚上 1 個", "1 到"),                            # 要停掉請用休眠
])
def test_parse_schedule_errors(text, needle):
    r = parse_schedule(text)
    assert not r["ok"] and needle in r["error"]


def test_replicas_at_overnight_and_weekdays():
    plan = parse_schedule("web 平日 9:00-18:00 5 個，晚上 10 點到早上 6 點 1 個，其他時間 2 個")["plan"]
    assert replicas_at(plan, _t(2026, 10, 12, 10)) == 5      # 週一上班時間
    assert replicas_at(plan, _t(2026, 10, 17, 10)) == 2      # 週六同一時間：不是平日
    assert replicas_at(plan, _t(2026, 10, 12, 23)) == 1      # 跨午夜的時段
    assert replicas_at(plan, _t(2026, 10, 13, 3)) == 1
    assert replicas_at(plan, _t(2026, 10, 13, 7)) == 2
    t, n = next_change(plan, _t(2026, 10, 12, 23, 30))
    assert (t.hour, t.minute, n) == (6, 0, 2)


def test_validate_rejects_tampered_plan():
    ok = {"app": "web", "windows": [{"start": "08:00", "end": "20:00", "replicas": 3, "days": [0, 1, 2, 3, 4, 5, 6]}], "base": 1}
    assert validate_plan(ok) is None
    assert "副本數" in validate_plan({**ok, "windows": [{**ok["windows"][0], "replicas": 999}]})
    assert "時間" in validate_plan({**ok, "windows": [{**ok["windows"][0], "start": "25:00"}]})
    assert "星期" in validate_plan({**ok, "windows": [{**ok["windows"][0], "days": [9]}]})
    assert "格式" in validate_plan({**ok, "app": "../etc"})
    assert validate_plan({**ok, "windows": []})


def test_savings_against_always_peak():
    plan = parse_schedule("web 白天 3 個、晚上 1 個")["plan"]
    sv = savings(plan, cost_per_replica_hour(100, 128 * 1024 ** 2))
    # 白天 12 小時 3 個、晚上 12 小時 1 個 → 平均 2 個，比整天 3 個省三分之一
    assert sv["peak"] == 3 and sv["avg_replicas"] == 2.0 and sv["saved_pct"] == 33
    assert sv["saved_usd"] == round(sv["always_peak_usd"] - sv["scheduled_usd"], 2) > 0


# ── 閒置判斷 ──
def test_expected_probe_traffic_matches_measurement():
    # 2026-10-11 實測：nginx（HTTP 每 5 秒＋每 10 秒）每分鐘 110 到 117 個封包；redis（TCP 每 10 秒）每分鐘 17 到 20 個
    nginx = sm.expected_probe_packets_per_hour([{"kind": "http", "period": 5}, {"kind": "http", "period": 10}]) / 60
    redis = sm.expected_probe_packets_per_hour([{"kind": "tcp", "period": 10}]) / 60
    assert 105 <= nginx <= 125 and 17 <= redis <= 25
    assert sm.expected_probe_packets_per_hour([{"kind": "exec", "period": 10}]) == 0


def test_decide_idle():
    now, day, pod = 10_000_000.0, 86400, "web-abc12-xyz12"
    exp = sm.expected_probe_packets_per_hour([{"kind": "http", "period": 5}, {"kind": "http", "period": 10}])
    base = exp * 0.95                                                # 實際背景量跟估算值差 5%
    args = dict(window_seconds=5 * day, expected_per_hour=exp, bucket_seconds=3600)

    def idle(active_days, busiest, typical, history_days=9):
        return sm.decide_idle(now, now - active_days * day, busiest_by_pod={pod: busiest},
                              typical_by_pod={pod: typical}, history_since=now - history_days * day, **args)[0]

    assert idle(6, base * 1.01, base) is True                       # 只有健康檢查（每小時的數字很穩定）
    assert idle(2, base * 1.01, base) is False                      # 剛部署、剛喚醒不到 5 天
    assert idle(6, base * 1.01, base, history_days=3) is False      # 監控資料不夠 5 天：寧可不休眠
    assert idle(6, base * 1.02 + 80, base) is False                 # 有一小時多了 10 次左右的請求
    assert idle(6, exp * 1.6 + 100, exp * 1.6 + 100) is False       # 一直有穩定的流量（例如監控程式定期連）
    assert sm.decide_idle(now, now - 6 * day, busiest_by_pod={}, typical_by_pod={},
                          history_since=now - 9 * day, **args)[0] is False   # 查不到流量資料


def test_decide_idle_minute_buckets_tolerate_sampling_noise():
    # 2026-10-11 實測閒置 nginx（測試模式一分鐘一格）：最多 121、中位數 106
    now, pod = 10_000_000.0, "web-abc12-xyz12"
    exp = sm.expected_probe_packets_per_hour([{"kind": "http", "period": 5}, {"kind": "http", "period": 10}])
    kw = dict(window_seconds=600, expected_per_hour=exp, bucket_seconds=60, history_since=now - 3600)
    assert sm.decide_idle(now, now - 900, busiest_by_pod={pod: 121}, typical_by_pod={pod: 106}, **kw)[0] is True
    assert sm.decide_idle(now, now - 900, busiest_by_pod={pod: 106 + 80}, typical_by_pod={pod: 106}, **kw)[0] is False


def test_pod_belongs_to():
    assert sm.pod_belongs_to("web", "web-7d9f8b6c5d-x2k9p")
    assert not sm.pod_belongs_to("web", "web-api-7d9f8b6c5d-x2k9p")   # web 跟 web-api 不能互相算到
    assert not sm.pod_belongs_to("web", "zt-waker-web-7d9f8b6c5d-x2k9p")


def test_traffic_query_cannot_be_injected():
    for q in sm.traffic_queries('user-a"}) or vector(1', 432000, 3600).values():
        assert '"})' not in q and 'namespace="user-aorvector1"' in q


def test_waker_is_locked_down():
    m = sm.waker_manifest("shop-web", "user-a", [80, 8080, 80])
    pod = m["spec"]["template"]["spec"]
    c = pod["containers"][0]
    assert pod["automountServiceAccountToken"] is False
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert c["securityContext"] == {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                    "capabilities": {"drop": ["ALL"]}}
    assert c["resources"]["limits"] == {"cpu": "100m", "memory": "64Mi"}
    assert {p["containerPort"] for p in c["ports"]} == {80, 8080}
    assert m["spec"]["selector"]["matchLabels"] == {sm.LABEL_WAKER: "shop-web"}
    assert sm.WAKE_MARK in sm.WAKER_SCRIPT and "127.0.0.1" in sm.WAKER_SCRIPT


# ── 設定儲存（多租戶隔離） ──
def test_store_first_use_without_init_does_not_deadlock(tmp_path, monkeypatch):
    """沒有先呼叫 init() 就直接讀寫（網站實際的用法）。2026-10-11 用一般 Lock 時這裡會永遠卡住。"""
    import threading
    from core import autoscale_store as st
    monkeypatch.setenv("ZT_AUTOSCALE_DB", str(tmp_path / "fresh.db"))
    monkeypatch.setattr(st, "_conn", None)
    done = []
    th = threading.Thread(target=lambda: done.append(st.get_schedules("user-a")), daemon=True)
    th.start()
    th.join(5)
    assert done == [[]], "第一次使用就卡住了"



def test_store_isolation_and_forget(tmp_path):
    from core import autoscale_store as st
    st.init(str(tmp_path / "a.db"))
    plan = parse_schedule("web 白天 3 個、晚上 1 個")["plan"]
    st.save_schedule("user-a", plan)
    st.set_exempt("user-a", "web", True)
    assert [s["app"] for s in st.get_schedules("user-a")] == ["web"]
    assert st.get_schedules("user-b") == [] and st.get_settings("user-b")["exempt"] == []
    assert st.get_settings("user-a")["idle_days"] == st.DEFAULT_IDLE_DAYS
    assert st.set_idle_days("user-a", 999) == st.MAX_IDLE_DAYS
    st.forget_app("user-a", "web")
    assert st.get_schedules("user-a") == [] and st.get_settings("user-a")["exempt"] == []


# ── 網站：規則、API、Service 改接 ──
web_demo = pytest.importorskip("web_demo")


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
        def patch_namespaced_service(self, name, ns, body):
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

    def read_namespaced_deployment(self, name, ns):
        return self.dep

    def patch_namespaced_deployment_scale(self, name, ns, body):
        self.scaled.append(body["spec"]["replicas"])
        self.dep.spec.replicas = body["spec"]["replicas"]

    def patch_namespaced_deployment(self, name, ns, body):
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
