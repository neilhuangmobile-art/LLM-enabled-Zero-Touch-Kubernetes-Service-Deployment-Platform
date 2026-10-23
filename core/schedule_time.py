"""
預約時間解析（2026-10-23）：從一句話裡讀出「什麼時候」，並把時間那段拿掉，剩下的照常交給意圖判斷。

只用規則，不呼叫模型：時間讀錯的後果是在錯的時間提醒部署，寧可讀不出來讓使用者在確認卡上自己選，
也不要猜。一律以台北時間（UTC+8，沒有日光節約）理解，回傳 UTC 時間戳記給儲存用。

能讀的說法：
  今天／明天／後天／大後天、N 分鐘後／N 小時後／N 天後、下週X／這週X、X月X日、YYYY-MM-DD、M/D，
  搭配 早上／上午／中午／下午／傍晚／晚上／凌晨 與 H點、H點半、H點M分、H:MM。
只有日期沒有時間 → 早上 9 點；只有時間沒有日期 → 今天，已經過了就是明天。
"""

import re
from datetime import datetime, timedelta, timezone

TW = timezone(timedelta(hours=8))
WEEKDAY_ZH = "一二三四五六日"

_CN = {"零": 0, "〇": 0, "一": 1, "二": 2, "兩": 2, "两": 2, "三": 3, "四": 4, "五": 5,
       "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_NUM = r"(?:\d{1,4}|[零〇一二兩两三四五六七八九十]{1,3})"

RECURRING_RE = re.compile(r"每(天|日|週|周|星期|禮拜|個?月|個?小時|隔)")
HINT_RE = re.compile(r"預約|排程|排定|定時|到時候|屆時")


def cn_num(s: str) -> int:
    """阿拉伯數字或簡單的中文數字（到九十九）。"""
    if s.isdigit():
        return int(s)
    if s == "十":
        return 10
    if "十" in s:
        a, _, b = s.partition("十")
        return (_CN.get(a, 1) if a else 1) * 10 + (_CN.get(b, 0) if b else 0)
    return _CN.get(s, -1) if len(s) == 1 else -1


def _wd(ch: str) -> int:
    return 6 if ch in "日天" else WEEKDAY_ZH.index(ch)


_REL_RE = re.compile(rf"({_NUM}|半)\s*(?:個)?\s*(分鐘|分|小時|鐘頭|天)\s*(?:後|之後|以後)")
_DATE_PATTERNS = [
    ("ymd", re.compile(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?:日|號)?")),
    ("md", re.compile(rf"({_NUM})\s*月\s*({_NUM})\s*(?:日|號)")),
    ("md_slash", re.compile(r"(?<![\d:])(\d{1,2})/(\d{1,2})(?![\d/])")),
    ("nextwd", re.compile(r"下(?:個)?(?:週|周|星期|禮拜)([一二三四五六日天])")),
    ("thiswd", re.compile(r"(?:這|本)?(?:週|周|星期|禮拜)([一二三四五六日天])")),
    ("rel_day", re.compile(r"大後天|後天|明天|今天|明日|今日")),
]
_PERIOD = r"(早上|上午|早|中午|下午|午後|傍晚|晚上|今晚|凌晨|半夜)"
_TIME_RE = re.compile(
    rf"(?:{_PERIOD}\s*)?(?:(\d{{1,2}})\s*[:：]\s*(\d{{2}})|({_NUM})\s*點\s*(?:(半)|({_NUM})\s*分?)?)")
_PERIOD_ONLY_RE = re.compile(_PERIOD)


def fmt_local(dt: datetime) -> str:
    """2026-10-25（週日）15:00"""
    d = dt.astimezone(TW)
    return f"{d:%Y-%m-%d}（週{WEEKDAY_ZH[d.weekday()]}）{d:%H:%M}"


def _strip(text: str, spans) -> str:
    out, last = [], 0
    for a, b in sorted(spans):
        if a < last:
            continue
        out.append(text[last:a])
        last = b
    out.append(text[last:])
    s = "".join(out)
    s = HINT_RE.sub("", s)
    s = re.sub(r"^(幫我|請|麻煩)?\s*(在|於)\s*", r"\1", s.strip())
    s = re.sub(r"\s{2,}", " ", s).strip(" ，,、。的在於")
    return s


def parse_when(text: str, now: datetime = None) -> dict:
    """回傳：
      {"found": False}                         沒有時間的說法
      {"found": False, "hint": True}           有「預約」之類的字，但讀不出時間
      {"found": True, "recurring": True, ...}  重複排程（這個功能不支援）
      {"found": True, "ts": <UTC 秒>, "display": "...", "rest": "去掉時間後的句子", "matched": "..."}
    """
    text = text or ""
    now = (now or datetime.now(timezone.utc)).astimezone(TW)
    m = RECURRING_RE.search(text)
    if m:
        return {"found": True, "recurring": True, "matched": m.group(0)}
    spans = []

    # 相對時間：「3 小時後」直接算完，不再看日期與時刻
    m = _REL_RE.search(text)
    if m:
        n = 0.5 if m.group(1) == "半" else cn_num(m.group(1))
        if n > 0:
            unit = m.group(2)
            delta = (timedelta(minutes=n) if unit in ("分鐘", "分") else
                     timedelta(hours=n) if unit in ("小時", "鐘頭") else timedelta(days=n))
            when = now + delta
            # 「3 天後下午 2 點」：天數之後有講時刻就用那個時刻；沒講就是現在這個時刻的 3 天後
            tm = _TIME_RE.search(text, m.end()) if unit == "天" else None
            hm = _hm(tm) if tm else None
            if hm:
                when = when.replace(hour=hm[0], minute=hm[1])
                spans.append(tm.span())
            spans.append(m.span())
            return _result(text, when.replace(second=0, microsecond=0), spans)

    day = None
    for kind, rx in _DATE_PATTERNS:
        m = rx.search(text)
        if not m:
            continue
        d = _date_from(kind, m, now)
        if d is not None:
            day = d
            spans.append(m.span())
            break

    hm = None
    for tm in _TIME_RE.finditer(text):
        if any(a <= tm.start() < b for a, b in spans):
            continue
        # 「快一點」「多一點資源」：中文數字加「點」，前面沒有日期也沒有早上／下午，不能當成時間
        if tm.group(4) and not tm.group(4).isdigit() and not tm.group(1) and day is None:
            continue
        hm = _hm(tm)
        if hm:
            spans.append(tm.span())
            break
    if hm is None:
        pm = _PERIOD_ONLY_RE.search(text)
        if pm and day is not None:
            hm = {"早上": (9, 0), "上午": (9, 0), "早": (9, 0), "中午": (12, 0), "下午": (15, 0), "午後": (15, 0),
                  "傍晚": (18, 0), "晚上": (20, 0), "今晚": (20, 0), "凌晨": (2, 0), "半夜": (0, 0)}[pm.group(1)]
            spans.append(pm.span())

    if day is None and hm is None:
        return {"found": False, "hint": bool(HINT_RE.search(text))}
    if day is None:
        cand = now.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
        when = cand if cand > now else cand + timedelta(days=1)
    else:
        h, mi = hm or (9, 0)
        when = datetime(day.year, day.month, day.day, h, mi, tzinfo=TW)
    return _result(text, when, spans)


def _date_from(kind, m, now):
    today = now.date()
    try:
        if kind == "ymd":
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date()
        if kind in ("md", "md_slash"):
            mo, d = cn_num(m.group(1)), cn_num(m.group(2))
            if not (1 <= mo <= 12 and 1 <= d <= 31):
                return None
            cand = datetime(today.year, mo, d).date()
            return cand if cand >= today else datetime(today.year + 1, mo, d).date()
        if kind == "rel_day":
            return today + timedelta(days={"今天": 0, "今日": 0, "明天": 1, "明日": 1, "後天": 2, "大後天": 3}[m.group(0)])
        if kind == "nextwd":
            # 下週X：下一個「週」裡的那一天（週一開始）
            start_next = today + timedelta(days=7 - today.weekday())
            return start_next + timedelta(days=_wd(m.group(1)))
        if kind == "thiswd":
            target = today + timedelta(days=_wd(m.group(1)) - today.weekday())
            return target if target >= today else target + timedelta(days=7)
    except ValueError:
        return None
    return None


def _hm(tm):
    period = tm.group(1)
    if tm.group(2) is not None:
        h, mi = int(tm.group(2)), int(tm.group(3))
    else:
        h = cn_num(tm.group(4))
        mi = 30 if tm.group(5) else (cn_num(tm.group(6)) if tm.group(6) else 0)
    if h < 0 or mi < 0 or mi > 59 or h > 24:
        return None
    if period in ("下午", "午後", "傍晚", "晚上", "今晚") and h < 12:
        h += 12
    elif period == "中午" and h < 6:
        h += 12
    elif period in ("凌晨", "半夜") and h == 12:
        h = 0
    if h == 24:
        h = 0
    return h, mi


def _result(text, when, spans):
    return {"found": True, "ts": when.astimezone(timezone.utc).timestamp(), "display": fmt_local(when),
            "rest": _strip(text, spans), "matched": "".join(text[a:b] for a, b in sorted(spans))}
