"""從歷史流量學時段（2026-10-23）：agents/timeslot_learn.py 與 autoscale_store 的每小時流量。純邏輯，不需要 flask。"""
from datetime import datetime, timedelta

from agents.timeslot import TZ, validate_plan, describe
from agents.timeslot_learn import learn_schedule

START = datetime(2026, 10, 5, 0, 0, tzinfo=TZ)        # 週一
PROBE = 300                                          # 每個 Pod 每小時健康檢查的封包數


def _samples(days, busy_fn, pods=2, busy_pkts=5000):
    out = []
    for h in range(days * 24):
        t = START + timedelta(hours=h)
        extra = busy_pkts if busy_fn(t) else 0
        out.append((t.timestamp(), PROBE * pods + extra, pods))
    return out


def test_weekday_daytime_busy():
    s = _samples(14, lambda t: t.weekday() < 5 and 9 <= t.hour < 18)
    r = learn_schedule("shop", s, current_replicas=3, baseline_per_pod=PROBE)
    assert r["ok"], r
    assert r["plan"]["windows"] == [{"start": "09:00", "end": "18:00", "replicas": 3, "days": [0, 1, 2, 3, 4]}]
    assert r["plan"]["base"] == 1 and validate_plan(r["plan"], 50) is None
    assert "14 天" in r["explain"] and "套用" in r["explain"]


def test_same_pattern_every_day_merges_days_and_short_gap_filled():
    # 每天 8–12、13–20 忙，中午空 1 小時 → 併成 8–20
    s = _samples(10, lambda t: 8 <= t.hour < 12 or 13 <= t.hour < 20)
    r = learn_schedule("web", s, current_replicas=1, baseline_per_pod=PROBE)
    assert r["ok"]
    assert r["plan"]["windows"] == [{"start": "08:00", "end": "20:00", "replicas": 2, "days": [0, 1, 2, 3, 4, 5, 6]}]


def test_overnight_busy_crosses_midnight():
    s = _samples(8, lambda t: t.hour >= 22 or t.hour < 3)
    r = learn_schedule("batch", s, current_replicas=4, baseline_per_pod=PROBE)
    assert r["ok"] and r["plan"]["windows"][0]["start"] == "22:00" and r["plan"]["windows"][0]["end"] == "03:00"
    assert describe(r["plan"])        # 確認卡的時段表做得出來


def test_flat_traffic_is_not_suggested():
    r = learn_schedule("idle", _samples(9, lambda t: False), current_replicas=2, baseline_per_pod=PROBE)
    assert not r["ok"] and r["reason"] == "flat" and "休眠" in r["error"]
    r = learn_schedule("hot", _samples(9, lambda t: True), current_replicas=2, baseline_per_pod=PROBE)
    assert not r["ok"] and r["reason"] == "flat" and "一直都很高" in r["error"]


def test_not_enough_data_says_how_many_days_left():
    r = learn_schedule("new", _samples(3, lambda t: 9 <= t.hour < 18), current_replicas=2, baseline_per_pod=PROBE)
    assert not r["ok"] and r["reason"] == "not_enough" and r["need"] == 4 and "還要 4 天" in r["error"]


def test_health_check_traffic_alone_is_not_busy_with_more_pods():
    # 5 個 Pod 的健康檢查流量比 1 個 Pod 多，但扣掉基準後都不算忙
    s = [(START.timestamp() + h * 3600, PROBE * (5 if h % 24 < 12 else 1), 5 if h % 24 < 12 else 1) for h in range(8 * 24)]
    r = learn_schedule("probe-only", s, current_replicas=2, baseline_per_pod=PROBE)
    assert not r["ok"] and r["reason"] == "flat"


def test_traffic_store_isolation_retention_and_forget(tmp_path):
    import time
    from core import autoscale_store as st
    st.init(str(tmp_path / "t.db"))
    now = int(time.time()) // 3600 * 3600
    st.add_traffic([("user-a", "web", now, 100, 2), ("user-a", "web", now, 150, 2),      # 同一小時重寫 → 取代
                    ("user-b", "web", now, 999, 1), ("user-a", "web", now - 70 * 86400, 1, 1)])
    assert st.traffic_for("user-a", "web") == [(now, 150.0, 2)]                          # 70 天前的被清掉、b 的看不到
    assert st.has_traffic_hour(now)
    st.forget_app("user-a", "web")
    assert st.traffic_for("user-a", "web") == []
    assert st.meta_get("traffic_backfilled") is None
    st.meta_set("traffic_backfilled", "1")
    assert st.meta_get("traffic_backfilled") == "1"
