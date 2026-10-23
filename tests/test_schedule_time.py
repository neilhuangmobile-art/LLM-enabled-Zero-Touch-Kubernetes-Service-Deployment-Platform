"""預約時間解析（core/schedule_time.py）：固定「現在」= 2026-10-23（週五）10:30 台北時間。"""
from datetime import datetime

import pytest

from core.schedule_time import TW, parse_when

NOW = datetime(2026, 10, 23, 10, 30, tzinfo=TW)

CASES = [
    ("明天早上9點部署2個nginx", "2026-10-24（週六）09:00", "部署2個nginx"),
    ("10月25日下午3點部署 2 個 nginx", "2026-10-25（週日）15:00", "部署 2 個 nginx"),
    ("3分鐘後部署1個nginx叫sched-test", "2026-10-23（週五）10:33", "部署1個nginx叫sched-test"),
    ("預約下週一晚上8點半把web擴到3個", "2026-10-26（週一）20:30", "把web擴到3個"),
    ("後天刪掉 sched-test", "2026-10-25（週日）09:00", "刪掉 sched-test"),
    ("下午兩點十分換 web 的映像檔成 nginx:1.28", "2026-10-23（週五）14:10", "換 web 的映像檔成 nginx:1.28"),
    ("2026-11-02 14:00 部署 redis", "2026-11-02（週一）14:00", "部署 redis"),
    ("11/3 部署 redis", "2026-11-03（週二）09:00", "部署 redis"),
    ("3天後下午2點刪除報名網站", "2026-10-26（週一）14:00", "刪除報名網站"),
    ("週日中午12點部署 redis", "2026-10-25（週日）12:00", "部署 redis"),
    ("早上8點部署 redis", "2026-10-24（週六）08:00", "部署 redis"),     # 今天 8 點已過 → 明天
    ("半小時後擴到 2 個", "2026-10-23（週五）11:00", "擴到 2 個"),
    ("凌晨兩點刪掉 demo", "2026-10-24（週六）02:00", "刪掉 demo"),
    ("1月5日部署 redis", "2027-01-05（週二）09:00", "部署 redis"),       # 今年已過 → 明年
]


@pytest.mark.parametrize("text,display,rest", CASES, ids=[c[0][:12] for c in CASES])
def test_parse(text, display, rest):
    r = parse_when(text, NOW)
    assert r["found"] and r["display"] == display and r["rest"] == rest


@pytest.mark.parametrize("text", ["部署 2 個 nginx", "部署 nginx:1.27 開 8080 port", "把 web 擴到 3 個", "web-1 有沒有壞",
                                  "部署 nginx 給我快一點", "記憶體多一點，部署 redis"])
def test_no_time_is_not_guessed(text):
    """沒有講時間的句子不能被讀出時間（映像檔版本號、port、副本數都不是時間）。"""
    assert parse_when(text, NOW)["found"] is False


def test_hint_without_time_asks_user_to_pick():
    r = parse_when("預約部署 nginx", NOW)
    assert r["found"] is False and r["hint"] is True


@pytest.mark.parametrize("text", ["每天早上9點擴到3個", "每週一刪掉 demo", "每小時重啟一次"])
def test_recurring_is_rejected(text):
    """重複排程跟隊友的分時擴縮重疊，這裡不支援，要明確回報。"""
    assert parse_when(text, NOW).get("recurring") is True
