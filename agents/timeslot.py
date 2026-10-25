"""
分時擴縮（2026-10-11）：把「shop-web 白天 3 個、晚上 1 個」這種一句話，轉成固定格式的時段表，
再算出任何時間點該開幾個副本、跟「整天開滿」比起來每月省多少。

全部是確定性規則，不經過模型：時段和副本數都是使用者講的，平台只負責換算。看不懂的句子
回傳錯誤訊息和範例，請使用者換個說法，不會自己猜時間。

時段表格式：
    {"app": "shop-web",
     "windows": [{"start": "08:00", "end": "20:00", "replicas": 3, "days": [0,1,2,3,4,5,6]}],
     "base": 1}
- days 用 Python 的 weekday()：0 是週一、6 是週日。
- start > end 代表跨過午夜（例如 22:00 到 06:00），屬於 start 那一天的時段。
- start == end 代表一整天。
- 不在任何時段裡的時間用 base；base 是 None 時，時段必須涵蓋一整週。
"""

import re
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

TZ = timezone(timedelta(hours=8))   # 台灣時間；存檔與比較都用這個時區
ALL_DAYS = [0, 1, 2, 3, 4, 5, 6]
WEEKDAYS = [0, 1, 2, 3, 4]
WEEKEND = [5, 6]
MAX_WINDOWS = 6

# 沒講具體幾點時，口語時段對應的時間
_NAMED_RANGES = [
    (r"上班時間|辦公時間|工作時間", ("09:00", "18:00")),
    (r"白天|日間", ("08:00", "20:00")),
    (r"晚上|夜間|晚間|夜裡|下班(?:後|時間)?", ("20:00", "08:00")),
    (r"深夜|半夜|凌晨", ("00:00", "06:00")),
    (r"早上|上午", ("06:00", "12:00")),
    (r"下午", ("12:00", "18:00")),
]
_BASE_WORDS = r"其他時間|其餘時間|其它時間|其他時段|其餘|其他|其它|剩下的時間|平常|預設"
_REPLICAS_RE = re.compile(r"(\d{1,3})\s*(?:個|副本|份|台|pods?\b|replicas?\b)", re.I)
_PERIOD = r"(早上|上午|中午|下午|晚上|傍晚|凌晨|半夜|深夜)?"
_CLOCK = r"(\d{1,2})(?:\s*[:：]\s*(\d{2})|\s*點\s*(半|\d{1,2}\s*分)?)"
_RANGE_RE = re.compile(_PERIOD + r"\s*" + _CLOCK + r"\s*(?:到|至|-|~|～|—)\s*" + _PERIOD + r"\s*" + _CLOCK)
_NAME_RE = re.compile(r"(?<![\w.-])([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)(?![\w.-])")
_NOT_NAMES = {"pod", "pods", "replica", "replicas", "am", "pm"}


def _to_minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _fmt(minutes: int) -> str:
    minutes %= 1440
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _clock(period: Optional[str], hour: str, mm: Optional[str], half: Optional[str]) -> Optional[str]:
    h = int(hour)
    if half:
        m = 30 if "半" in half else int(re.sub(r"\D", "", half) or 0)
    else:
        m = int(mm) if mm else 0
    if h > 24 or m > 59:
        return None
    if period in ("下午", "晚上", "傍晚") and h < 12:
        h += 12
    elif period in ("凌晨", "半夜", "深夜") and h == 12:
        h = 0
    elif period == "中午" and h < 6:
        h += 12
    return _fmt(h * 60 + m)


def _days_of(clause: str) -> Optional[List[int]]:
    if re.search(r"平日|工作日|週間|周間|上班日|週一到週五|周一到周五|星期一到星期五", clause):
        return WEEKDAYS
    if re.search(r"週末|周末|假日|六日|星期六日|週六日|周六日", clause):
        return WEEKEND
    return None


def _app_name(text: str) -> Optional[str]:
    for m in _NAME_RE.finditer(text.lower()):
        name = m.group(1)
        if name in _NOT_NAMES or name.isdigit() or re.fullmatch(r"\d+(?:pods?|replicas?)?", name):
            continue
        return name
    return None


def parse_schedule(text: str, app: str = None) -> Dict:
    """一句話 → {"ok": True, "plan": {...}} 或 {"ok": False, "error": "..."}。"""
    text = (text or "").strip()[:300]
    app = app or _app_name(text)
    if not app:
        return {"ok": False, "error": "要調整哪一個服務？請把服務名稱一起講，例如「shop-web 白天 3 個、晚上 1 個」。"}
    # 服務名稱本身可能含數字（例如 web2），先拿掉再找時間和副本數
    rest = re.sub(r"(?<![\w.-])" + re.escape(app) + r"(?![\w.-])", " ", text, flags=re.I)
    clauses = [c.strip() for c in re.split(r"[，,、；;。\n]|然後|另外|但是?", rest) if c.strip()]
    windows, base = [], None
    for clause in clauses:
        rm = _REPLICAS_RE.search(clause)
        if not rm:
            continue
        n = int(rm.group(1))
        days = _days_of(clause)
        rg = _RANGE_RE.search(clause)
        if rg:
            p1, h1, m1, x1, p2, h2, m2, x2 = rg.groups()
            start = _clock(p1, h1, m1, x1)
            end = _clock(p2 or (p1 if p1 in ("下午", "晚上") and int(h2) > int(h1) else None), h2, m2, x2)
            if not start or not end:
                return {"ok": False, "error": f"「{clause}」裡的時間看不懂，請用像「8 點到 22 點」或「08:00-22:00」的寫法。"}
            windows.append({"start": start, "end": end, "replicas": n, "days": days or ALL_DAYS})
            continue
        named = next((rng for pat, rng in _NAMED_RANGES if re.search(pat, clause)), None)
        if named:
            windows.append({"start": named[0], "end": named[1], "replicas": n, "days": days or ALL_DAYS})
        elif re.search(_BASE_WORDS, clause):
            base = n
        elif days:
            windows.append({"start": "00:00", "end": "00:00", "replicas": n, "days": days})
        elif base is None and windows:
            base = n          # 「白天 3 個，1 個」：最後一個沒講時間的當成其他時間
        else:
            return {"ok": False, "error": f"「{clause}」沒有講是哪個時段。可以說「白天」「晚上」「8 點到 22 點」「平日」「週末」或「其他時間」。"}
    if not windows:
        return {"ok": False, "error": "沒有找到時段。例如：「shop-web 白天 3 個、晚上 1 個」或「shop-web 9 點到 18 點 3 個，其他時間 1 個」。"}
    plan = {"app": app, "windows": windows[:MAX_WINDOWS], "base": base}
    err = validate_plan(plan)
    if err:
        return {"ok": False, "error": err}
    return {"ok": True, "plan": plan}


def _covers(w: Dict, weekday: int, minute: int) -> bool:
    s, e, days = _to_minutes(w["start"]), _to_minutes(w["end"]), w.get("days") or ALL_DAYS
    if s == e:
        return weekday in days
    if s < e:
        return weekday in days and s <= minute < e
    return (minute >= s and weekday in days) or (minute < e and (weekday - 1) % 7 in days)


def replicas_at(plan: Dict, when: datetime) -> Optional[int]:
    """某個時間點該開幾個；沒有任何時段涵蓋、也沒有 base 時回 None。"""
    local = when.astimezone(TZ)
    wd, minute = local.weekday(), local.hour * 60 + local.minute
    for w in plan.get("windows") or []:
        if _covers(w, wd, minute):
            return int(w["replicas"])
    return plan.get("base")


def _week_samples(plan: Dict, step: int = 15):
    """一週每 step 分鐘取樣一次：產生 (weekday, minute, replicas)。"""
    for wd in range(7):
        for minute in range(0, 1440, step):
            n = None
            for w in plan.get("windows") or []:
                if _covers(w, wd, minute):
                    n = int(w["replicas"])
                    break
            yield wd, minute, (n if n is not None else plan.get("base"))


def validate_plan(plan: Dict, max_replicas: int = 50) -> Optional[str]:
    """回傳錯誤訊息（白話）或 None。前端送回來的時段表也用這個再檢查一次。"""
    if not isinstance(plan, dict) or not plan.get("app"):
        return "缺少服務名稱。"
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", str(plan["app"])):
        return "服務名稱的格式不對。"
    ws = plan.get("windows")
    if not isinstance(ws, list) or not ws or len(ws) > MAX_WINDOWS:
        return f"時段要有 1 到 {MAX_WINDOWS} 個。"
    for w in ws:
        if not isinstance(w, dict):
            return "時段格式不對。"
        for k in ("start", "end"):
            if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(w.get(k, ""))):
                return "時間要寫成 00:00 到 23:59。"
        days = w.get("days")
        if not isinstance(days, list) or not days or any(d not in ALL_DAYS for d in days):
            return "星期的設定不對。"
        if not isinstance(w.get("replicas"), int) or not 1 <= w["replicas"] <= max_replicas:
            return f"每個時段的副本數要在 1 到 {max_replicas} 之間（要完全停掉請用休眠）。"
    base = plan.get("base")
    if base is not None and (not isinstance(base, int) or not 1 <= base <= max_replicas):
        return f"其他時間的副本數要在 1 到 {max_replicas} 之間。"
    # 同一個時間點被兩個時段蓋到、而且數字不同：使用者講的話互相矛盾，請他講清楚
    for wd, minute, _ in _week_samples(plan):
        hits = {int(w["replicas"]) for w in ws if _covers(w, wd, minute)}
        if len(hits) > 1:
            return (f"時段重疊了：{_DAY_NAMES[wd]} {_fmt(minute)} 同時落在兩個時段裡，副本數不一樣。"
                    "請把時段講清楚，例如「9 點到 18 點 3 個，其他時間 1 個」。")
        if not hits and base is None:
            return f"{_DAY_NAMES[wd]} {_fmt(minute)} 沒有被任何時段涵蓋。其他時間要開幾個？例如加上「其他時間 1 個」。"
    return None


_DAY_NAMES = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]


def describe_days(days: List[int]) -> str:
    days = sorted(days or ALL_DAYS)
    if days == ALL_DAYS:
        return "每天"
    if days == WEEKDAYS:
        return "平日（週一到週五）"
    if days == WEEKEND:
        return "週末"
    return "、".join(_DAY_NAMES[d] for d in days)


def describe(plan: Dict) -> List[Dict]:
    """給畫面用的時段表：[{when, replicas}]，跨午夜的寫成「20:00 到隔天 08:00」。"""
    rows = []
    for w in plan.get("windows") or []:
        s, e = w["start"], w["end"]
        if s == e:
            span = "整天"
        elif _to_minutes(s) > _to_minutes(e):
            span = f"{s} 到隔天 {e}"
        else:
            span = f"{s} 到 {e}"
        rows.append({"when": f"{describe_days(w.get('days'))} {span}", "replicas": int(w["replicas"])})
    if plan.get("base") is not None:
        rows.append({"when": "其他時間", "replicas": int(plan["base"])})
    return rows


def savings(plan: Dict, cost_per_replica_hour: float) -> Dict:
    """跟「整天都開最多那個數字」比。cost_per_replica_hour 由呼叫端依服務的資源需求算好。"""
    samples = [n for _, _, n in _week_samples(plan)]
    peak = max(samples)
    replica_hours_week = sum(samples) * 15 / 60
    peak_hours_week = peak * 168
    month = 730 / 168
    scheduled = round(replica_hours_week * month * cost_per_replica_hour, 2)
    always = round(peak_hours_week * month * cost_per_replica_hour, 2)
    return {"peak": peak, "avg_replicas": round(sum(samples) / len(samples), 2),
            "scheduled_usd": scheduled, "always_peak_usd": always,
            "saved_usd": round(always - scheduled, 2),
            "saved_pct": round((1 - replica_hours_week / peak_hours_week) * 100) if peak_hours_week else 0}


def cost_per_replica_hour(cpu_mc: int, mem_bytes: int) -> float:
    """跟 agents/cost_agent._estimate_monthly_cost 同一組牌價（$0.048/vCPU/hr、$0.006/GiB/hr）。"""
    return (cpu_mc or 100) / 1000 * 0.048 + (mem_bytes or 128 * 1024 ** 2) / 1024 ** 3 * 0.006


def next_change(plan: Dict, now: datetime) -> Optional[Tuple[datetime, int]]:
    """下一次副本數會變的時間點與新的數字（一週內找不到就回 None）。畫面用。"""
    cur = replicas_at(plan, now)
    t = now.astimezone(TZ).replace(second=0, microsecond=0)
    for _ in range(7 * 24 * 60):
        t += timedelta(minutes=1)
        n = replicas_at(plan, t)
        if n != cur:
            return t, n
    return None
