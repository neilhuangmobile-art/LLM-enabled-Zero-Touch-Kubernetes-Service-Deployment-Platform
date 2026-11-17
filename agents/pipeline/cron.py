"""定時執行（CronJob）的排程小工具（2026-11-17）：展開 5 欄 cron、最短間隔、下次執行時間、中文說明。

只支援平台會產生的寫法：數字、*、*/n、a-b、a-b/n、逗號列表（與 reasoner.valid_cron 一致）。
時區一律台灣時間（CronJob 設 timeZone: Asia/Taipei）。不另外裝 croniter 之類的套件。
"""
from __future__ import annotations

import datetime as _dt
from typing import List, Optional, Set

MIN_INTERVAL_MINUTES = 5          # 使用者 11-17 決定：間隔至少 5 分鐘
MAX_RUN_SECONDS = 3600            # 每次最多跑 1 小時
TZ_NAME = "Asia/Taipei"
_TZ = _dt.timezone(_dt.timedelta(hours=8))     # 台灣沒有日光節約，固定 +8
_RANGES = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]   # 分 時 日 月 星期（0＝週日，7 也當週日）
_WEEK = "日一二三四五六"


def _expand(field: str, lo: int, hi: int) -> Optional[Set[int]]:
    out: Set[int] = set()
    for part in field.split(","):
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            if not s.isdigit() or int(s) < 1:
                return None
            step = int(s)
        if part == "*":
            a, b = lo, hi
        elif "-" in part:
            x, y = part.split("-", 1)
            if not (x.isdigit() and y.isdigit()):
                return None
            a, b = int(x), int(y)
        elif part.isdigit():
            a = b = int(part)
            if step > 1:
                b = hi
        else:
            return None
        if hi == 6 and b == 7:      # 星期的 7 也是週日
            b = 6
            out.add(0)
        if a < lo or b > hi or a > b:
            return None
        out.update(range(a, b + 1, step))
    return out or None


def parse(cron: str) -> Optional[List[Set[int]]]:
    parts = str(cron or "").split()
    if len(parts) != 5:
        return None
    sets = [_expand(p, lo, hi) for p, (lo, hi) in zip(parts, _RANGES)]
    return None if any(s is None for s in sets) else sets


def min_interval_minutes(cron: str) -> Optional[int]:
    """兩次執行之間最短幾分鐘（只看分鐘與小時，日期限制只會讓間隔更長）。格式不對回 None。"""
    sets = parse(cron)
    if sets is None:
        return None
    mins, hours = sorted(sets[0]), sorted(sets[1])
    times = sorted(h * 60 + m for h in hours for m in mins)
    if len(times) == 1:
        return 24 * 60
    gaps = [b - a for a, b in zip(times, times[1:])] + [times[0] + 24 * 60 - times[-1]]
    return min(gaps)


def too_frequent(cron: str) -> bool:
    n = min_interval_minutes(cron)
    return n is not None and n < MIN_INTERVAL_MINUTES


def next_run(cron: str, now: Optional[_dt.datetime] = None) -> Optional[_dt.datetime]:
    """台灣時間的下一次執行時間（最多往後找 366 天）。"""
    sets = parse(cron)
    if sets is None:
        return None
    mins, hours, days, months, wdays = sets
    parts = str(cron).split()
    day_any, wday_any = parts[2] == "*", parts[4] == "*"
    t = (now or _dt.datetime.now(_TZ)).astimezone(_TZ).replace(second=0, microsecond=0) + _dt.timedelta(minutes=1)
    end = t + _dt.timedelta(days=366)
    while t < end:
        if t.month not in months:
            t = (t.replace(day=1, hour=0, minute=0) + _dt.timedelta(days=32)).replace(day=1)
            continue
        wd = (t.weekday() + 1) % 7               # Python 週一＝0 → cron 週日＝0
        # cron 規則：日與星期都有限制時，任一個符合就算
        day_ok = (t.day in days and wd in wdays) if (day_any or wday_any) else (t.day in days or wd in wdays)
        if not day_ok:
            t = t.replace(hour=0, minute=0) + _dt.timedelta(days=1)
            continue
        if t.hour not in hours:
            t = t.replace(minute=0) + _dt.timedelta(hours=1)
            continue
        if t.minute in mins:
            return t
        t += _dt.timedelta(minutes=1)
    return None


def describe_zh(cron: str) -> str:
    """常見寫法翻成中文（台灣時間）；其他的回「排程 <cron>（台灣時間）」。"""
    parts = str(cron or "").split()
    if parse(cron) is None:
        return f"排程 {cron}"
    m, h, d, mo, w = parts
    if m.startswith("*/") and h == "*" and d == mo == w == "*":
        return f"每 {m[2:]} 分鐘"
    if m.isdigit() and h == "*" and d == mo == w == "*":
        return f"每小時的第 {int(m)} 分"
    if m.isdigit() and h.startswith("*/") and d == mo == w == "*":
        return f"每 {h[2:]} 小時（第 {int(m)} 分）"
    if m.isdigit() and h.isdigit():
        hhmm = f"{int(h):02d}:{int(m):02d}"
        if d == mo == w == "*":
            return f"每天 {hhmm}（台灣時間）"
        if d == mo == "*" and w.isdigit():
            return f"每週{_WEEK[int(w) % 7]} {hhmm}（台灣時間）"
        if d.isdigit() and mo == w == "*":
            return f"每月 {int(d)} 號 {hhmm}（台灣時間）"
    return f"排程 {cron}（台灣時間）"


def runs_per_month(cron: str) -> Optional[float]:
    """一個月（30 天）大約跑幾次，給成本估算用。格式不對回 None。"""
    sets = parse(cron)
    if sets is None:
        return None
    mins, hours, days, months, wdays = sets
    parts = str(cron).split()
    per_day = len(mins) * len(hours)
    day_frac = 1.0
    if parts[2] != "*" and parts[4] != "*":
        day_frac = min(1.0, len(days) / 31 + len(wdays) / 7)
    elif parts[2] != "*":
        day_frac = len(days) / 31
    elif parts[4] != "*":
        day_frac = len(wdays) / 7
    return per_day * 30 * day_frac * (len(months) / 12)
