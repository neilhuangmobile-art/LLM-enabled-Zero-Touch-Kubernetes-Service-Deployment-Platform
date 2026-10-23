"""
從歷史流量學時段（2026-10-23）：把每小時收到的封包數整理成「建議的分時擴縮時段」。

全部是固定規則，不經過 AI；結果只是建議，使用者要在確認卡按「套用」才會生效（跟手動設定同一張卡）。
輸出跟 agents/timeslot.parse_schedule 同一種格式，套用前一樣要通過 validate_plan。

判斷方式（沿用休眠判斷的門檻，見 core/sleep_mode.decide_idle）：
- 健康檢查本身每小時就有固定的封包數（基準），所以不能用「流量是 0」判斷閒。
- 分「平日」「週末」兩組，算每個小時的中位數；中位數超過「基準 × 1.02 + 60 個封包（約 8 次請求）」就算忙。
- 連續的忙碌小時合併成時段，中間短於 2 小時的空檔併進去，避免時段太零碎。
"""

from datetime import datetime
from statistics import median
from typing import Dict, List, Optional, Sequence, Tuple

from agents.timeslot import TZ, WEEKDAYS, WEEKEND, ALL_DAYS, MAX_WINDOWS, validate_plan

MIN_DAYS = 7
GAP_HOURS = 2           # 忙碌時段之間短於這麼多小時的空檔併進去
BUSY_RATIO = 1.02
BUSY_MARGIN = 60        # 約 8 次 HTTP 請求


def _local_hour(ts: float) -> Tuple[bool, int]:
    d = datetime.fromtimestamp(ts, TZ)
    return d.weekday() >= 5, d.hour


def _fill_gaps(busy: List[bool]) -> List[bool]:
    """一天 24 小時（首尾相接）：兩段忙碌之間短於 GAP_HOURS 的空檔視為忙碌。"""
    if not any(busy) or all(busy):
        return list(busy)
    out = list(busy)
    start = next(i for i in range(24) if busy[i])
    i = 0
    while i < 24:
        h = (start + i) % 24
        if not out[h]:
            j = i
            while j < 24 and not busy[(start + j) % 24]:
                j += 1
            if j - i < GAP_HOURS:
                for k in range(i, j):
                    out[(start + k) % 24] = True
            i = j
        else:
            i += 1
    return out


def _runs(busy: List[bool]) -> List[Tuple[int, int]]:
    """連續的忙碌小時 → [(開始小時, 結束小時)]，可以跨午夜；整天都忙回 [(0, 0)]。"""
    if all(busy):
        return [(0, 0)]
    if not any(busy):
        return []
    start = next(i for i in range(24) if not busy[i])   # 從一個閒的小時開始掃，跨午夜的時段才不會被切成兩段
    runs, i = [], 0
    while i < 24:
        h = (start + i) % 24
        if busy[h]:
            j = i
            while j < 24 and busy[(start + j) % 24]:
                j += 1
            runs.append((h, (start + j) % 24))
            i = j
        else:
            i += 1
    return sorted(runs)


def _hhmm(h: int) -> str:
    return f"{h:02d}:00"


def learn_schedule(app: str, samples: Sequence[Tuple[float, float, int]], current_replicas: int,
                   baseline_per_pod: Optional[float] = None, max_replicas: int = 50,
                   min_days: int = MIN_DAYS) -> Dict:
    """samples：[(整點的 UTC 秒, 那一小時收到的封包數, 那時候的 Pod 數)]。

    回傳 {"ok": True, "plan": {...}, "explain": "...", "days": n}
      或 {"ok": False, "reason": "not_enough", "error": "...", "days": n, "need": m}
      或 {"ok": False, "reason": "flat"|"fragmented", "error": "...", "days": n}
    """
    samples = [(float(t), float(p or 0), max(1, int(n or 1))) for t, p, n in samples if p is not None]
    days = round(len({int(t // 3600) for t, _, _ in samples}) / 24, 1)
    if days < min_days:
        need = max(1, int(min_days - days + 0.999))
        return {"ok": False, "reason": "not_enough", "days": days, "need": need,
                "error": f"{app} 的流量紀錄目前累積了約 {days:g} 天，至少要 {min_days} 天才能看出規律，大約還要 {need} 天。"
                         "平台每小時會自動記錄，不用另外設定。"}

    # 基準：健康檢查本身的流量。抓不到時用全部小時「每個 Pod」流量的最低四分位
    per_pod = sorted(p / n for _, p, n in samples)
    base = baseline_per_pod if baseline_per_pod is not None else per_pod[len(per_pod) // 4]

    groups: Dict[bool, Dict[int, List[float]]] = {False: {}, True: {}}
    totals: Dict[bool, Dict[int, List[float]]] = {False: {}, True: {}}
    for t, p, n in samples:
        weekend, hour = _local_hour(t)
        groups[weekend].setdefault(hour, []).append(p - base * n * BUSY_RATIO)   # 扣掉健康檢查之後多出來的
        totals[weekend].setdefault(hour, []).append(p)
    busy: Dict[bool, List[bool]] = {}
    for weekend in (False, True):
        hours = groups[weekend]
        if not hours:
            busy[weekend] = [False] * 24
            continue
        busy[weekend] = _fill_gaps([bool(hours.get(h)) and median(hours[h]) > BUSY_MARGIN for h in range(24)])

    all_busy = [b for w in (False, True) if groups[w] for b in busy[w]]
    if not any(all_busy) or all(all_busy):
        return {"ok": False, "reason": "flat", "days": days,
                "error": f"過去 {days:g} 天 {app} 的流量" + ("一直都很高" if all_busy and all(all_busy) else "一直都很低")
                         + "，看不出明顯的尖峰和離峰，不建議分時擴縮。"
                         + ("" if all_busy and all(all_busy) else "如果長期沒人用，休眠會比較省。")}

    peak = max(2, min(int(current_replicas or 0), max_replicas)) if current_replicas else 2
    wd_runs, we_runs = _runs(busy[False]), _runs(busy[True])
    windows = []
    if wd_runs == we_runs:
        windows = [{"start": _hhmm(a), "end": _hhmm(b), "replicas": peak, "days": list(ALL_DAYS)} for a, b in wd_runs]
    else:
        windows = ([{"start": _hhmm(a), "end": _hhmm(b), "replicas": peak, "days": list(WEEKDAYS)} for a, b in wd_runs]
                   + [{"start": _hhmm(a), "end": _hhmm(b), "replicas": peak, "days": list(WEEKEND)} for a, b in we_runs])
    if len(windows) > MAX_WINDOWS:
        return {"ok": False, "reason": "fragmented", "days": days,
                "error": f"過去 {days:g} 天 {app} 的忙碌時段太零碎（{len(windows)} 段），排成時段不會比較省，建議維持現在的副本數。"}
    plan = {"app": app, "windows": windows, "base": 1}
    err = validate_plan(plan, max_replicas)
    if err:
        return {"ok": False, "reason": "invalid", "days": days, "error": f"整理出來的時段有問題：{err}"}

    def avg(weekend_busy: bool) -> float:
        vals = [median(totals[w][h]) for w in (False, True) for h in totals[w] if busy[w][h] == weekend_busy]
        return sum(vals) / len(vals) if vals else 0.0
    hi, lo = avg(True), avg(False)
    ratio = f"，忙碌時段的流量大約是其他時間的 {hi / lo:.0f} 倍" if lo > 0 and hi / lo >= 1.5 else ""
    return {"ok": True, "plan": plan, "days": days,
            "explain": f"根據過去 {days:g} 天的流量紀錄{ratio}。建議忙碌時段開 {peak} 個、其他時間 1 個；"
                       "這只是建議，確認時段表沒問題再按「套用」。"}
