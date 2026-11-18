"""意圖符合檢查（2026-11-17，手稿「agent_意圖檢查」第一點：中間 JSON 的填寫結果是否符合 user 的意圖）。

intent_check.py 找「講了卻沒寫進去」（缺漏）；這裡找「系統填的值和使用者講的矛盾」。
照手稿的流程：發現不符合 → 修正 → 最多 3 輪 → 還修不好就問使用者。

- 目前預設只檢查「要不要定時執行」（CHECK_FIELDS，見下方說明與 S40 實測）。
- 只檢查系統填的：基礎欄位、workload、expose 來源是 inferred／default 的。使用者講的不檢查。
  storage、schedule 這兩種額外欄位是模型對使用者句子的解讀（S19「定時去抓數據」被讀成定時執行），不論來源都檢查。
- 模型回報的 evidence 一定要是原句裡真的有的字，欄位要在清單內，否則不採用（跟 normalize.add_extra 同一個做法）。
- 建議值要過同一套檢查（schema.clean_value；映像檔要在知識表）才會直接換，換了在部署單上寫明「原本 X → 改成 Y」。
- 沒有合法建議值：清空交回空缺檢查重新推；重推出一樣的值就不再白跑，直接問使用者。
"""
from __future__ import annotations

import copy
import json
import time
from typing import Dict, List, Optional

from agents.pipeline import schema, software_rag
from agents.pipeline.llm import LLM, extract_json
from agents.pipeline.prompts import VERIFY_SYSTEM

MAX_ROUNDS = 3
FIELDS = ("image", "replicas", "port", "memory", "cpu", "workload", "expose", "storage", "schedule")
_CHECK_BASE = ("image", "replicas", "port", "memory", "cpu")   # app_name 只是名稱，不檢查
# 實際要檢查的項目（2026-11-17 使用者決定）：S40 實測（v6）全部項目都檢查時，整體分數沒進步、多問 15→20、
# 會來回改；唯一改對的是「定時執行誤判」（S19）。所以先只檢查 schedule。其他項目的程式保留，要開再加進來。
# 沒有排程的句子不會呼叫模型。
CHECK_FIELDS = ("schedule",)


def _live(spec: Dict, kind: str) -> List[Dict]:
    return [e for e in spec["extras"] if e["kind"] == kind and e["status"] not in ("dropped",)]


def _system_values(spec: Dict) -> Dict:
    """要給模型檢查的值（只有系統填的）。"""
    out = {}
    for f in _CHECK_BASE:
        b = spec["base"][f]
        if b["value"] is not None and b["source"] != "user":
            out[f] = b["value"]
    for k in ("workload", "expose"):
        if spec[k]["value"] is not None and spec[k]["source"] != "user":
            out[k] = spec[k]["value"]
    out["storage"] = bool(_live(spec, "storage"))
    sched = _live(spec, "schedule")
    if sched:
        v = sched[0]["value"]
        out["schedule"] = v.get("cron") if isinstance(v, dict) else v
    for f in spec.get("verify_skip", []):    # 使用者在問題卡上選了「維持」的，不再檢查
        out.pop(f, None)
    out = {k: v for k, v in out.items() if k in CHECK_FIELDS}
    if "schedule" in out and spec["workload"]["value"]:
        # 判斷排程對不對需要知道服務類型，當成參考資料一起給（不會被改，因為 workload 不在 CHECK_FIELDS）
        out["workload (context only)"] = spec["workload"]["value"]
    return out


def _parse(spec: Dict, raw, checked: Dict) -> List[Dict]:
    """模型輸出 → 合格的問題清單（evidence 要在原句、欄位要是這次檢查的）。"""
    probs = []
    for p in (raw or {}).get("problems") or [] if isinstance(raw, dict) else []:
        if not isinstance(p, dict):
            continue
        f = str(p.get("field") or "").strip().lower()
        ev = str(p.get("evidence") or "").strip()
        if f not in FIELDS or f not in checked:
            continue
        if not ev or ev not in spec["text"]:
            spec["trace"].append({"step": "verify", "ms": 0, "detail": f"evidence 不在原句，不採用：{f} {ev[:40]!r}"})
            continue
        if any(q["field"] == f for q in probs):
            continue
        probs.append({"field": f, "issue": str(p.get("issue") or "")[:200], "evidence": ev, "suggest": p.get("suggest")})
    return probs


def _clean_suggest(field: str, v):
    """建議值合法才回傳（不合法回 None）。"""
    if v is None:
        return None
    if field in schema.BASE_FIELDS:
        c = schema.clean_value(field, v)
        if field == "image" and c:
            c = software_rag.full_name(c)
            if not software_rag.is_known(c):
                return None
        return c
    if field == "workload":
        s = str(v).strip().lower()
        return s if s in schema.WORKLOADS else None
    if field == "expose":
        s = str(v).strip().lower()
        return s if s in schema.EXPOSES else None
    if field in ("storage", "schedule"):
        s = str(v).strip().lower()
        if s in ("false", "no", "none", "null", "0", "不需要", "不用"):
            return False
        if field == "storage" and s in ("true", "yes", "1"):
            return True
        return None
    return None


def _current(spec: Dict, field: str):
    if field in schema.BASE_FIELDS:
        return spec["base"][field]["value"]
    if field in ("workload", "expose"):
        return spec[field]["value"]
    if field == "storage":
        return bool(_live(spec, "storage"))
    sched = _live(spec, "schedule")
    return bool(sched)


def _drop(spec: Dict, kind: str, why: str) -> None:
    for e in _live(spec, kind):
        e["status"], e["note"] = "dropped", why


def _apply(spec: Dict, p: Dict, value, source: str = "inferred") -> None:
    """把修正寫進 spec（value 已經過 _clean_suggest）。"""
    f = p["field"]
    if f in schema.BASE_FIELDS:
        spec["base"][f] = {"value": value, "source": source, "evidence": f"意圖檢查：{p['evidence']}"[:200]}
    elif f in ("workload", "expose"):
        spec[f] = {"value": value, "source": source}
        if f == "workload" and value != "cronjob":
            _drop(spec, "schedule", "意圖檢查：不是定時執行")
    elif f == "storage":
        if value is False:
            _drop(spec, "storage", "意圖檢查：不需要保存資料")
        elif value is True and not _live(spec, "storage"):
            spec["extras"].append({"kind": "storage", "desc": "保存資料（意圖檢查建議）", "value": {"size": None},
                                   "evidence": p["evidence"], "source": "inferred", "status": "pending", "note": ""})
    elif f == "schedule" and value is False:
        _drop(spec, "schedule", "意圖檢查：不是要這個程式定時執行")
        if spec["workload"]["value"] == "cronjob" and spec["workload"]["source"] != "user":
            spec["workload"] = {"value": "service", "source": "inferred"}


def _show(v) -> str:
    if v is True:
        return "要"
    if v is False:
        return "不要"
    return str(v)


def _ask(spec: Dict, p: Dict, suggest) -> None:
    cur = _current(spec, p["field"])
    opts = ([{"value": json.dumps(suggest, ensure_ascii=False), "label": f"改成 {_show(suggest)}"}] if suggest is not None else [])
    opts.append({"value": "keep", "label": f"維持 {_show(cur)}"})
    schema.add_question(spec, f"verify:{p['field']}",
                        f"你說「{p['evidence']}」，但系統原本設定的 {p['field']} 是 {_show(cur)}，可能不符合：{p['issue']}。要怎麼處理？",
                        options=opts, recommended=json.dumps(suggest, ensure_ascii=False) if suggest is not None else None,
                        reason="檢查了 3 次還是不確定，交給你決定")


def _restore(spec: Dict, snap: Dict, field: str) -> None:
    """把這個欄位恢復成檢查前的樣子，撤掉這次對它的修改紀錄與提醒，之後不再檢查。
    workload 和 schedule 會互相影響（改 workload 時會丟掉排程），一起恢復。"""
    group = {"workload", "schedule"} if field in ("workload", "schedule") else {field}
    for f in group:
        if f in schema.BASE_FIELDS:
            spec["base"][f] = copy.deepcopy(snap["base"][f])
        elif f in ("workload", "expose"):
            spec[f] = copy.deepcopy(snap[f])
        else:
            spec["extras"] = [e for e in spec["extras"] if e["kind"] != f] + \
                             [copy.deepcopy(e) for e in snap["extras"] if e["kind"] == f]
        spec["verified"] = [v for v in spec["verified"] if v["field"] != f]
        spec["notices"] = [n for n in spec["notices"] if not n.startswith(f"檢查後把 {f} ")]
        spec.setdefault("verify_skip", []).append(f)
    spec["trace"].append({"step": "verify", "ms": 0, "detail": f"{field} 被來回修改，判定不確定，恢復成原本的值"})


def run(spec: Dict, local: Optional[LLM], refill=None) -> None:
    """refill：清空欄位後重新推的函式（gap_check.run）；測試可以傳 None。"""
    if local is None:
        return
    spec.setdefault("verified", [])
    snap = copy.deepcopy({"base": spec["base"], "workload": spec["workload"], "expose": spec["expose"],
                          "extras": spec["extras"]})
    changed = set()
    for rnd in range(1, MAX_ROUNDS + 1):
        checked = _system_values(spec)
        if not checked:
            break
        t0 = time.time()
        user = f"User request: {spec['text']}\nValues chosen by the system: {json.dumps(checked, ensure_ascii=False)}"
        raw = local(VERIFY_SYSTEM, user)
        if raw is None:
            # 模型沒有回應：不能記成「沒有矛盾」（2026-11-18，web_demo 會在部署單上提示有檢查沒完成）
            spec["trace"].append({"step": "verify", "ms": int((time.time() - t0) * 1000),
                                  "detail": f"第 {rnd} 輪：本地模型沒有回應，這輪檢查略過"})
            break
        probs = _parse(spec, extract_json(raw), checked)
        spec["attempts"]["verify"] = rnd
        spec["trace"].append({"step": "verify", "ms": int((time.time() - t0) * 1000),
                              "detail": f"第 {rnd} 輪：{'；'.join(p['field'] + '：' + p['issue'] for p in probs) or '沒有矛盾'}"[:300]})
        if not probs:
            break
        cleared, n_before = [], len(spec["verified"])
        for p in probs:
            sug = _clean_suggest(p["field"], p["suggest"])
            before = _current(spec, p["field"])
            if sug is not None and sug == before:      # 建議的就是現在的值：不算矛盾
                continue
            if p["field"] in changed:
                # 11-17 S40 實測：同一欄位被改過又被要求改（1→2→1、要→不要→要），代表模型自己也不確定。
                # 恢復成檢查前的值、不再檢查，也不拿去問使用者（避免把模型的猶豫變成使用者的負擔）
                _restore(spec, snap, p["field"])
                continue
            if rnd == MAX_ROUNDS:
                _ask(spec, p, sug)
                continue
            if sug is not None:
                _apply(spec, p, sug)
                changed.add(p["field"])
                spec["verified"].append({"field": p["field"], "from": _show(before), "to": _show(sug), "issue": p["issue"]})
                schema.add_notice(spec, f"檢查後把 {p['field']} 從 {_show(before)} 改成 {_show(sug)}：{p['issue']}")
            elif p["field"] in schema.BASE_FIELDS:
                spec["base"][p["field"]] = {"value": None, "source": None, "evidence": None}
                cleared.append((p, before))
            else:
                _ask(spec, p, None)
        if cleared and refill is not None:
            refill(spec)
            for p, before in cleared:
                after = _current(spec, p["field"])
                if after is None or after == before:   # 重推還是一樣（或推不出來）：不再白跑，問使用者
                    if after is None and before is not None:
                        spec["base"][p["field"]] = {"value": before, "source": "inferred", "evidence": None}
                    if not any(q["field"] == p["field"] for q in spec["questions"]):
                        _ask(spec, p, None)
                else:
                    changed.add(p["field"])
                    spec["verified"].append({"field": p["field"], "from": _show(before), "to": _show(after), "issue": p["issue"]})
                    schema.add_notice(spec, f"檢查後把 {p['field']} 從 {_show(before)} 改成 {_show(after)}：{p['issue']}")
        if spec["questions"] or len(spec["verified"]) == n_before and not cleared:
            break   # 有問題要問、或這一輪什麼都沒改（再問模型也一樣）


def resume_answer(spec: Dict, field: str, ans: str) -> None:
    """問題卡回答：keep＝維持並當成使用者確認過（以後不再檢查）；其他＝採用該值（來源 user）。"""
    f = field[len("verify:"):]
    spec.setdefault("verify_skip", []).append(f)
    if ans == "keep":
        if f in schema.BASE_FIELDS and spec["base"][f]["value"] is not None:
            spec["base"][f]["source"] = "user"
        elif f in ("workload", "expose") and spec[f]["value"] is not None:
            spec[f]["source"] = "user"
        return
    try:
        val = json.loads(ans)
    except (TypeError, ValueError):
        val = ans
    sug = _clean_suggest(f, val)
    if sug is None:
        return
    _apply(spec, {"field": f, "evidence": "使用者回答"}, sug, source="user" if f in schema.BASE_FIELDS + ("workload", "expose") else "inferred")
