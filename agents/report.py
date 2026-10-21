"""
一句話產生分析報告（2026-10-10）：把 core/event_store 累積的紀錄整理成統計數字、每日趨勢、
系統觀察，以及（可選的）AI 解讀。

防幻覺的分工：
- 所有數字都由這個檔案從紀錄計算（硬事實），模型不產生任何數字。
- 「系統觀察」是固定規則依數字寫出的句子，每次結果一樣。
- 「AI 解讀」由 Gemini 根據統計結果寫；句子裡出現的每個數字都要能在統計結果裡找到，
  找不到的整句刪掉，並告訴使用者刪了幾句（近失事件可見化，跟 Healer 的做法一致）。
- 報告上三種內容分開標示，讀者分得出哪些是查到的、哪些是推論。
"""

import re
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional

MAX_DAYS = 180
DEFAULT_DAYS = 30

_PERIOD_RULES = [
    (re.compile(r"今天|今日|today|24\s*小時|一天"), 1),
    (re.compile(r"週報|周報|本週|本周|這週|這周|這禮拜|這星期|一週|一周|一星期|七天|weekly|this week|past week|last week"), 7),
    (re.compile(r"季|三個月|90\s*天|quarter"), 90),
    (re.compile(r"全部|所有紀錄|從頭|歷史以來|有史以來|all time|everything"), MAX_DAYS),
    (re.compile(r"月報|本月|這個月|這月|一個月|30\s*天|monthly|this month|past month|last month"), 30),
]


def parse_period(text: str) -> int:
    """從使用者的一句話判斷報告期間（天數）。講了「N 天」以 N 為準，最多 180 天。"""
    text = (text or "")[:300]
    m = re.search(r"(\d{1,3})\s*(?:天|日|days?)", text, re.IGNORECASE)
    if m:
        return max(1, min(MAX_DAYS, int(m.group(1))))
    for rx, days in _PERIOD_RULES:
        if rx.search(text):
            return days
    return DEFAULT_DAYS


def clean_issue(msg: str) -> str:
    """審查訊息拿掉程式內部前綴（[0]、[安全]、Deployment/x:、引號），跟前端 cleanIssue 同規則。"""
    s = str(msg or "")
    s = re.sub(r"^\s*(?:\[[^\]]{0,20}\]\s*)+", "", s)
    s = re.sub(r"^[A-Za-z]{1,30}/[\w.-]{1,80}:\s*", "", s)
    s = re.sub(r"'([^']{1,200})'", r"\1", s)
    return s.strip()[:160]


def _issue_key(msg: str) -> str:
    """把「容器 web 缺少 readinessProbe」跟「容器 api 缺少 readinessProbe」歸成同一類。"""
    s = clean_issue(msg)
    s = re.sub(r"^容器\s*[\w.-]{1,63}\s*[：:]?\s*", "", s)
    s = re.sub(r"[（(][^）)]{0,80}[）)]", "", s)
    return s.strip()


def blocker_category(b: str) -> str:
    b = str(b or "")
    if b.startswith("[安全]"):
        return "security"
    if b.startswith("[成本]"):
        return "cost"
    if b.startswith("[效能]"):
        return "perf"
    if "資源需求" in b or "容量" in b:
        return "capacity"
    return "other"


CATEGORY_LABEL = {"security": "安全", "cost": "成本", "perf": "效能", "capacity": "叢集容量", "other": "其他"}


def _day_key(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def build_report(events: List[Dict], days: int, now: float = None, live: Dict = None,
                 first_record_ts: Optional[float] = None) -> Dict:
    """把紀錄整理成報告用的硬事實。events 必須已經限定在使用者自己的 namespace。"""
    now = now or time.time()
    days = max(1, min(MAX_DAYS, int(days or DEFAULT_DAYS)))
    since = now - days * 86400
    ev = [e for e in events if e.get("ts", 0) >= since]
    by_kind = defaultdict(list)
    for e in ev:
        by_kind[e["kind"]].append(e)

    # ── 部署 ──
    deploys = by_kind["deploy"]
    dep_ok = [e for e in deploys if e.get("outcome") == "ok"]
    dep_apps = Counter(e.get("app") for e in dep_ok if e.get("app"))

    # ── 審查把關 ──
    reviews = by_kind["review"]
    decisions = Counter(e.get("outcome") or "unknown" for e in reviews)
    blocked = [e for e in reviews if e.get("outcome") == "block"]
    block_cats = Counter()
    block_reasons = Counter()
    for e in blocked:
        bl = (e.get("data") or {}).get("blockers") or []
        cats = {blocker_category(b) for b in bl} or {"other"}
        for c in cats:
            block_cats[c] += 1
        for b in bl[:3]:
            block_reasons[_issue_key(b)] += 1
    # 因為成本或叢集容量被擋下的部署，原本估計每月要花多少（依公有雲牌價）
    blocked_cost_usd = round(sum(float((e.get("data") or {}).get("monthly_usd") or 0) for e in blocked
                                 if {blocker_category(b) for b in (e.get("data") or {}).get("blockers") or []}
                                 & {"cost", "capacity"}), 2)
    warn_issues = Counter()
    for e in reviews:
        for msg in (e.get("data") or {}).get("issues") or []:
            k = _issue_key(msg)
            if k:
                warn_issues[k] += 1

    # ── 其他操作 ──
    ops = {k: {"total": len(by_kind[k]), "ok": sum(1 for e in by_kind[k] if e.get("outcome") == "ok")}
           for k in ("scale", "update", "delete", "rollback")}

    # ── Healer ──
    heals = [e for e in by_kind["heal"] if e.get("outcome") != "none"]
    heal_ok = sum(1 for e in heals if e.get("outcome") == "ok")
    # inspect：Healer 只查出原因、列出資訊，沒有實際改動，需要使用者處理
    heal_inspect = sum(1 for e in heals if e.get("outcome") == "inspect")
    causes = Counter(((e.get("data") or {}).get("root_cause") or "未判定") for e in heals)
    heal_apps = Counter(e.get("app") for e in heals if e.get("app"))

    # ── 快照（Pod 數量、異常、重啟）──
    snaps = by_kind["snapshot"]
    snap_daily: Dict[str, Dict] = {}
    for e in snaps:
        d = e.get("data") or {}
        k = _day_key(e["ts"])
        cur = snap_daily.setdefault(k, {"pods": 0, "unhealthy": 0, "restarts": 0})
        for f in cur:
            cur[f] = max(cur[f], int(d.get(f) or 0))

    # ── 事故：最近一次需要 Healer 處理、或快照看到異常 Pod 的時間 ──
    incident_ts = [e["ts"] for e in heals]
    incident_ts += [e["ts"] for e in snaps if int((e.get("data") or {}).get("unhealthy") or 0) > 0]
    last_incident = max(incident_ts) if incident_ts else None
    start_ref = max(since, first_record_ts) if first_record_ts else since
    if last_incident:
        days_clean = int((now - last_incident) // 86400)
    elif first_record_ts:
        days_clean = int((now - start_ref) // 86400)
    else:
        days_clean = None

    # ── 每日趨勢 ──
    series = []
    start_day = datetime.fromtimestamp(since).date()
    for i in range(days + 1):
        day = start_day + timedelta(days=i)
        if day > datetime.fromtimestamp(now).date():
            break
        k = day.strftime("%Y-%m-%d")
        series.append({"date": k, "deploys": 0, "blocks": 0, "heals": 0, "ops": 0,
                       "pods": snap_daily.get(k, {}).get("pods"),
                       "unhealthy": snap_daily.get(k, {}).get("unhealthy"),
                       "restarts": snap_daily.get(k, {}).get("restarts")})
    idx = {s["date"]: s for s in series}
    for e in dep_ok:
        if _day_key(e["ts"]) in idx:
            idx[_day_key(e["ts"])]["deploys"] += 1
    for e in blocked:
        if _day_key(e["ts"]) in idx:
            idx[_day_key(e["ts"])]["blocks"] += 1
    for e in heals:
        if _day_key(e["ts"]) in idx:
            idx[_day_key(e["ts"])]["heals"] += 1
    for k in ("scale", "update", "delete", "rollback"):
        for e in by_kind[k]:
            if _day_key(e["ts"]) in idx:
                idx[_day_key(e["ts"])]["ops"] += 1

    recent = [{"ts": e["ts"], "kind": e["kind"], "app": e.get("app"), "outcome": e.get("outcome"),
               "detail": _event_detail(e)} for e in reversed(ev) if e["kind"] != "snapshot"][:30]

    facts = {
        "period": {"days": days, "start": _day_key(since), "end": _day_key(now),
                   "records_since": _day_key(first_record_ts) if first_record_ts else None},
        "deploys": {"total": len(deploys), "ok": len(dep_ok), "failed": len(deploys) - len(dep_ok),
                    "apps": len(dep_apps), "top_apps": dep_apps.most_common(5)},
        "reviews": {"total": len(reviews), "approve": decisions.get("approve", 0),
                    "warn": decisions.get("warn", 0), "block": decisions.get("block", 0),
                    "block_by_category": {CATEGORY_LABEL[c]: n for c, n in block_cats.most_common()},
                    "top_block_reasons": block_reasons.most_common(5),
                    "blocked_cost_usd": blocked_cost_usd,
                    "top_warnings": warn_issues.most_common(5)},
        "ops": ops,
        "heals": {"total": len(heals), "ok": heal_ok, "needs_you": heal_inspect,
                  "failed": len(heals) - heal_ok - heal_inspect,
                  "top_causes": causes.most_common(5), "top_apps": heal_apps.most_common(5)},
        "incidents": {"days_without_incident": days_clean,
                      "last_incident": _day_key(last_incident) if last_incident else None},
        "series": series,
        "recent": recent,
        "live": live or {},
        "has_data": bool(ev),
    }
    facts["observations"] = observations(facts)
    return facts


_KIND_LABEL = {"deploy": "部署", "review": "審查", "scale": "擴縮", "update": "換映像檔",
               "delete": "刪除", "rollback": "回滾", "heal": "自動修復", "sleep": "休眠", "wake": "喚醒"}


def _event_detail(e: Dict) -> str:
    d = e.get("data") or {}
    k = e["kind"]
    if k == "review":
        lab = {"approve": "通過", "warn": "通過但有提醒", "block": "擋下"}.get(e.get("outcome"), e.get("outcome") or "")
        first = clean_issue(((d.get("blockers") or [None])[0]) or "") if e.get("outcome") == "block" else ""
        # 時間軸只放第一句，完整原因在部署單上看過了
        first = re.split(r"[：:。]", first, maxsplit=1)[0][:60]
        return f"審查{lab}" + (f"：{first}" if first else "")
    if k == "deploy":
        return (f"部署 {d.get('image') or ''}，{d.get('pods') or '?'} 個副本"
                if e.get("outcome") == "ok" else f"部署失敗：{clean_issue(d.get('error') or '')}")
    if k == "scale":
        lead = "依排程把副本調成" if d.get("source") == "schedule" else "副本調成"
        return f"{lead} {d.get('replicas')} 個" + ("" if e.get("outcome") == "ok" else "（失敗）")
    if k == "sleep":
        return (f"太久沒人使用，進入休眠（原本 {d.get('replicas')} 個副本）" if e.get("outcome") == "ok"
                else f"想進入休眠但沒有完成：{clean_issue(d.get('error') or '')[:60]}")
    if k == "wake":
        how = {"traffic": "有人連線，自動喚醒", "manual": "手動喚醒"}.get(d.get("source"), "喚醒")
        return f"{how}，恢復 {d.get('replicas')} 個副本" + ("" if e.get("outcome") == "ok" else f"（失敗：{clean_issue(d.get('error') or '')[:60]}）")
    if k == "update":
        return f"映像檔換成 {d.get('image')}" + ("" if e.get("outcome") == "ok" else "（失敗）")
    if k == "heal":
        tail = {"ok": "", "inspect": "（需要你處理）"}.get(e.get("outcome"), "（沒有修好）")
        return f"{d.get('root_cause') or '未判定原因'}，處理方式：{d.get('action_label') or d.get('action') or ''}{tail}"
    return _KIND_LABEL.get(k, k) + ("" if e.get("outcome") == "ok" else "（失敗）")


def observations(f: Dict) -> List[Dict]:
    """固定規則寫出的觀察，每句都直接對應上面的數字。level：good / warn / info。"""
    out = []
    rv, dp, hl, inc, live = f["reviews"], f["deploys"], f["heals"], f["incidents"], f.get("live") or {}
    if not f["has_data"]:
        since = f["period"].get("records_since")
        out.append({"level": "info", "text": ("這段期間沒有任何紀錄。" +
                    (f"紀錄從 {since} 開始累積，可以把期間拉長再看一次。" if since else
                     "平台從現在開始累積紀錄，部署或修復之後就會出現在報告裡。"))})
    if rv["block"]:
        cats = "、".join(f"{k} {v} 次" for k, v in rv["block_by_category"].items())
        out.append({"level": "good", "text": f"部署前審查擋下 {rv['block']} 次有問題的部署（{cats}），這些設定沒有進到叢集。"})
    if rv["blocked_cost_usd"]:
        out.append({"level": "good", "text": f"因為成本或叢集容量被擋下的部署，如果上線估計每月要 ${rv['blocked_cost_usd']}（依公有雲牌價）。"})
    if dp["total"]:
        if dp["failed"] and dp["failed"] / dp["total"] >= 0.2:
            out.append({"level": "warn", "text": f"{dp['total']} 次部署有 {dp['failed']} 次失敗，比例偏高，可以先看最近的失敗原因。"})
        else:
            out.append({"level": "info", "text": f"共部署 {dp['ok']} 次、涵蓋 {dp['apps']} 個服務。"})
    if hl["total"]:
        cause, n = hl["top_causes"][0]
        parts = [f"自動修好 {hl['ok']} 次"]
        if hl["needs_you"]:
            parts.append(f"{hl['needs_you']} 次只能查出原因、需要你處理")
        if hl["failed"]:
            parts.append(f"{hl['failed']} 次修復失敗")
        out.append({"level": "info" if not (hl["failed"] or hl["needs_you"]) else "warn",
                    "text": f"Healer 處理了 {hl['total']} 次狀況：{'、'.join(parts)}。最常見的原因是「{cause}」（{n} 次）。"})
        for app, n in hl["top_apps"]:
            if n >= 3:
                out.append({"level": "warn", "text": f"{app} 在這段期間被修復了 {n} 次，同一個服務反覆出狀況，建議檢查它的設定或換一個穩定版本的映像檔。"})
    if rv["top_warnings"]:
        msg, n = rv["top_warnings"][0]
        if n >= 2:
            out.append({"level": "info", "text": f"最常出現的審查提醒是「{msg}」，出現 {n} 次。"})
    if inc["days_without_incident"] is not None and inc["days_without_incident"] >= 1:
        out.append({"level": "good", "text": f"已經連續 {inc['days_without_incident']} 天沒有需要修復的狀況。"})
    if live.get("unhealthy"):
        out.append({"level": "warn", "text": f"現在有 {live['unhealthy']} 個 Pod 狀態不正常，可以說「掃描有問題的 Pod」讓 Healer 看看。"})
    return out


# ── AI 解讀的數字核對 ──────────────────────────────────────────────
def _collect_numbers(obj, out: set):
    if isinstance(obj, bool):
        return
    if isinstance(obj, (int, float)):
        out.add(round(float(obj), 2))
        out.add(float(round(obj)))
    elif isinstance(obj, str):
        for m in re.finditer(r"\d+(?:\.\d+)?", obj):
            out.add(round(float(m.group(0)), 2))
    elif isinstance(obj, dict):
        for v in obj.values():
            _collect_numbers(v, out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _collect_numbers(v, out)


def validate_narrative(text: str, facts: Dict) -> Dict:
    """只留下數字全部查得到的句子。回傳 {"sentences": [...], "dropped": n}。"""
    allowed: set = set()
    _collect_numbers({k: v for k, v in facts.items() if k != "observations"}, allowed)
    # 百分比：允許由兩個查得到的數字算出來的比例（四捨五入到整數）
    nums = [n for n in allowed if 0 < n < 100000]
    pcts = set()
    small = sorted(nums)[:200]
    for a in small:
        for b in small:
            if b and a <= b:
                pcts.add(float(round(a / b * 100)))
    kept, dropped = [], 0
    parts = re.split(r"(?<=[。！？!?])|\n+", text or "")
    for s in parts:
        s = re.sub(r"^\s*(?:[-*•]|\d+[.)、])\s*", "", s).strip()
        if len(s) < 4:
            continue
        found = [round(float(x), 2) for x in re.findall(r"\d+(?:\.\d+)?", s)]
        ok = all(n in allowed or (("%" in s or "％" in s or "百分之" in s) and float(round(n)) in pcts)
                 for n in found)
        if ok:
            kept.append(s[:200])
        else:
            dropped += 1
    return {"sentences": kept[:6], "dropped": dropped}


def facts_for_model(f: Dict) -> Dict:
    """給模型看的精簡版：不含逐日序列和逐筆紀錄，減少它亂引用的空間。"""
    return {k: f[k] for k in ("period", "deploys", "reviews", "ops", "heals", "incidents", "live")}
