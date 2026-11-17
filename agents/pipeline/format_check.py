"""格式修正（2026-11-17，手稿「agent_格式及負荷檢查」第一點）。

手稿：「若格式不符合，則嘗試修正，並在修正後重新檢查（只能針對格式進行修改）」。
原本 schema.set_field 遇到格式不合格的值直接丟掉；現在丟掉時會記在 spec["rejected"]，這裡在最後整合時試著修：
1. 先用固定規則（單位換算、去掉多餘的字），
2. 規則修不好再問本地模型（FORMAT_FIX_SYSTEM，只能修格式、不能改數量或意思），
3. 修完一律重新過 schema.clean_value；最多 3 輪，還是不合格就問使用者。
另外檢查「整份規格」層級的格式：服務名稱要字母開頭（Service 名稱規則）、排程間隔至少 5 分鐘。
負荷（資源夠不夠）不在這裡：照 11-11 的決定，超過負荷由網站的負荷檢查告訴使用者，不自動調低。
"""
from __future__ import annotations

import re
import time
from typing import Dict, Optional

from agents.pipeline import cron, schema
from agents.pipeline.llm import LLM, extract_json
from agents.pipeline.prompts import FORMAT_FIX_SYSTEM

MAX_ROUNDS = 3


def rule_fix(field: str, raw: str) -> Optional[str]:
    """固定規則的格式修正。修不出來回 None（結果還要再過 clean_value）。"""
    s = str(raw or "").strip()
    low = s.lower().replace(" ", "")
    if field == "memory":
        m = re.match(r"^(\d+(?:\.\d+)?)(gi|gib|gb|g|mi|mib|mb|m)?$", low)
        if not m or not m.group(2):
            return None                      # 沒有單位：不知道是 MB 還是 GB，不猜
        n, unit = float(m.group(1)), m.group(2)
        mi = n * 1024 if unit.startswith("g") else n
        if mi < 1 or abs(mi - round(mi)) > 1e-6:
            return None
        mi = int(round(mi))
        return f"{mi // 1024}Gi" if mi % 1024 == 0 else f"{mi}Mi"
    if field == "cpu":
        m = re.match(r"^(\d+(?:\.\d+)?)(m|millicores?|millicpu|cores?|cpus?|vcpus?|核)?$", low)
        if not m:
            return None
        n, unit = float(m.group(1)), (m.group(2) or "")
        mc = n if unit.startswith("m") else n * 1000      # m／millicores → 毫核；core／核／沒寫 → 核
        if mc < 1 or abs(mc - round(mc)) > 1e-6:
            return None
        mc = int(round(mc))
        return str(mc // 1000) if mc % 1000 == 0 else f"{mc}m"
    if field in ("port", "replicas"):
        m = re.match(r"^\D{0,12}?(\d{1,5})(?:/(?:tcp|udp))?\D{0,6}$", low)
        return m.group(1) if m else None
    if field == "app_name":
        t = re.sub(r"[^a-z0-9-]+", "-", s.lower()).strip("-")
        if t and not t[0].isalpha():
            t = "app-" + t
        return t[:59].strip("-") or None
    if field == "image":
        t = s.lower().split("@")[0]
        t = re.sub(r"\s+", ":", t.strip()) if re.match(r"^[a-z0-9._/-]+\s+[a-z0-9._-]+$", t.strip()) else t
        return t
    return None


def _model_fix(local: Optional[LLM], field: str, raw: str, spec: Dict) -> Optional[str]:
    if local is None:
        return None
    t0 = time.time()
    out = extract_json(local(FORMAT_FIX_SYSTEM, f"Setting: {field}\nCurrent value: {raw}"))
    v = out.get("value") if isinstance(out, dict) else None
    spec["trace"].append({"step": "format", "ms": int((time.time() - t0) * 1000), "detail": f"模型修 {field}：{raw!r} → {v!r}"})
    return None if v is None else str(v)


def _pending(spec: Dict):
    """還沒修好的：被丟掉的值，而且那一格現在是空的、或只是預設值／等級比原本低。"""
    seen, out = set(), []
    for r in spec.get("rejected", []):
        f = r["field"]
        if f in seen or f not in schema.BASE_FIELDS:
            continue
        cur = spec["base"][f]
        if cur["value"] is None or schema.SOURCE_RANK.get(cur["source"], 0) < schema.SOURCE_RANK.get(r["source"], 0):
            seen.add(f)
            out.append(r)
    return out


def _fixed(spec: Dict, field: str, raw: str, new) -> None:
    spec.setdefault("format_fixed", []).append({"field": field, "from": raw, "to": str(new)})
    schema.add_notice(spec, f"{field} 的寫法「{raw}」不符合格式，已經改成 {new}")


def run(spec: Dict, local: Optional[LLM]) -> None:
    t0 = time.time()
    # 1. 服務名稱一定要字母開頭（網站另外會建 <名稱>-svc 的 Service，名稱規則比較嚴）
    name = schema.value(spec, "app_name")
    if name and not name[0].isalpha():
        new = rule_fix("app_name", name)
        if new and schema.clean_value("app_name", new) == new:
            spec["base"]["app_name"] = {**spec["base"]["app_name"], "value": new}
            _fixed(spec, "app_name", name, new)
    # 2. 被丟掉的值：規則 → 模型 → 重新檢查，最多 3 輪
    for rnd in range(1, MAX_ROUNDS + 1):
        todo = _pending(spec)
        if not todo:
            break
        spec["attempts"]["format"] = rnd
        for r in todo:
            f, raw = r["field"], r["value"]
            cand = rule_fix(f, raw) if rnd == 1 else None
            if cand is None or schema.clean_value(f, cand) is None:
                cand = _model_fix(local, f, raw, spec)
            good = schema.clean_value(f, cand) if cand is not None else None
            if good is not None and schema.set_field(spec, f, good, r["source"], f"格式修正：{raw}"[:200]):
                _fixed(spec, f, raw, good)
            elif rnd == MAX_ROUNDS:
                cur = schema.value(spec, f)
                schema.add_question(spec, f"format:{f}",
                                    f"「{raw}」看不懂是哪一種 {f} 的寫法，請重新填寫"
                                    + (f"（不填就用 {cur}）" if cur is not None else ""),
                                    options=[{"value": str(cur), "label": f"用 {cur}"}] if cur is not None else None,
                                    recommended=str(cur) if cur is not None else None,
                                    reason="試著修正了 3 次還是不符合格式")
    # 3. 排程間隔：間隔太短是超過上限，不自動改，問使用者（沒有「幫我決定」）
    for e in spec["extras"]:
        if e["kind"] == "schedule" and e["status"] not in ("dropped",):
            c = e["value"].get("cron") if isinstance(e["value"], dict) else e["value"]
            if cron.too_frequent(str(c or "")):
                schema.add_question(spec, "schedule_interval",
                                    f"「{e['evidence'] or c}」比平台的上限更頻繁：定時執行最短每 {cron.MIN_INTERVAL_MINUTES} 分鐘一次。要怎麼處理？",
                                    options=[{"value": f"*/{cron.MIN_INTERVAL_MINUTES} * * * *",
                                              "label": f"改成每 {cron.MIN_INTERVAL_MINUTES} 分鐘"},
                                             {"value": "cancel", "label": "先不部署"}],
                                    recommended=None, reason="超過上限不替你決定")
    spec["trace"].append({"step": "format", "ms": int((time.time() - t0) * 1000),
                          "detail": f"修正 {len(spec.get('format_fixed', []))} 項；待問 {sum(q['field'].startswith(('format:', 'schedule_interval')) for q in spec['questions'])} 項"})


def resume_answer(spec: Dict, field: str, ans: str) -> None:
    if field == "schedule_interval":
        for e in spec["extras"]:
            if e["kind"] == "schedule" and e["status"] not in ("dropped",):
                e["value"] = {"cron": ans}
                e["status"] = "pending"
        return
    f = field[len("format:"):]
    good = schema.clean_value(f, ans) or schema.clean_value(f, rule_fix(f, ans))
    if good is not None:
        schema.set_field(spec, f, good, "user", "使用者回答")
