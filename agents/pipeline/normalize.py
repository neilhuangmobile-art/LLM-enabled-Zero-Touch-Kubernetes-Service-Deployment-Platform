"""第 1 步：把使用者原句整理成中間 JSON（只寫使用者講過的）。

Gemini 優先，失敗（沒有 key、額度用完、輸出不是 JSON）改用本地模型跑同一份 prompt。
模型寫進基礎欄位的值要在原句找得到根據，才算 source=user；找不到的丟掉，交給空缺檢查重新補
（11-11 實測 Gemini 會把軟體名稱抄進 app_name，那不算使用者講的）。"""
from __future__ import annotations

import os
import re
import time
from typing import Dict, Optional

from agents.pipeline import schema
from agents.pipeline.llm import LLM, extract_json
from agents.pipeline.prompts import CLARITY_ADDON, NORMALIZE_SYSTEM


def clarity_mode() -> str:
    """PIPELINE_CLARITY（2026-11-22，反問改善）：
    off（預設）＝和原本完全一樣；on＝依「軟體講清楚了沒」決定要不要問軟體；
    log＝Gemini 一樣會判斷並記錄，但不影響結果（評估時用同一份 Gemini 回應比較開與關）。"""
    import os
    v = os.environ.get("PIPELINE_CLARITY", "off").strip().lower()
    return v if v in ("on", "log") else "off"


def apply_clarity(spec: Dict, c) -> None:
    """把 Gemini 的 clarity 存進 spec；矛盾或做不到的地方在 on 模式加到部署單提醒。格式不對就當作沒有（改用本地判斷）。"""
    # prompt 的欄位叫 kind_clear（2026-11-22 第二版；第一版叫 software_clear，Gemini 誤解成「使用者有沒有講出產品名」）
    clear = c.get("kind_clear", c.get("software_clear")) if isinstance(c, dict) else None
    if not isinstance(clear, bool):
        spec["trace"].append({"step": "clarity", "ms": 0, "detail": "Gemini 沒有回清不清楚的判斷"})
        return
    notes = [str(n).strip()[:200] for n in (c.get("notes") or []) if str(n).strip()][:5] \
        if isinstance(c.get("notes"), list) else []
    spec["clarity"] = {"source": "gemini", "software_clear": clear,
                       "category": str(c.get("category") or "")[:80] or None,
                       "suggest": str(c.get("suggest") or "").strip()[:120] or None, "notes": notes}
    if clarity_mode() == "on":
        for n in notes:
            schema.add_notice(spec, f"系統注意到：{n}")
    spec["trace"].append({"step": "clarity", "ms": 0,
                          "detail": f"Gemini：{'清楚' if clear else '沒講清楚'}要哪一類軟體（{spec['clarity']['category'] or '不知道'}）"})


def _second_check(spec: Dict, gemini: Optional[LLM]) -> None:
    """PIPELINE_CLARITY_CHECK=gemini2（2026-11-23，預設關）：Gemini 正規化時說「講清楚了」，再用獨立的短 prompt 問一次。
    結果存在 clarity["check_clear"]，由 gap_check 決定要不要問。holdout 該問沒問的 7 題有 6 題是 Gemini 說講清楚了。
    第二次沒回應或格式不對：不存，維持第一次的判斷。"""
    c = spec.get("clarity")
    if gemini is None or not c or c.get("software_clear") is not True \
            or os.environ.get("PIPELINE_CLARITY_CHECK", "").strip().lower() != "gemini2":
        return
    from agents.pipeline.prompts import CLARITY_CHECK_SYSTEM
    out = extract_json(gemini(CLARITY_CHECK_SYSTEM, spec["text"]))
    ok = out.get("kind_clear") if isinstance(out, dict) else None
    if not isinstance(ok, bool):
        spec["trace"].append({"step": "clarity", "ms": 0, "detail": "第二次判斷沒有回應，維持第一次的判斷"})
        return
    c["check_clear"] = ok
    kinds = [str(k)[:40] for k in out.get("kinds") or []][:5] if isinstance(out.get("kinds"), list) else []
    c["check_kinds"] = kinds
    spec["trace"].append({"step": "clarity", "ms": 0,
                          "detail": f"第二次判斷：{'清楚' if ok else '沒講清楚'}（可能的種類：{'、'.join(kinds) or '無'}）"})


EXTRA_KINDS = ("env", "secret", "storage", "command", "schedule", "no_expose", "extra_port", "other")

_CN_NUM = {1: "一壹單", 2: "二兩两貳", 3: "三參", 4: "四肆", 5: "五伍", 6: "六陸", 7: "七柒", 8: "八捌", 9: "九玖",
           10: "十拾"}
_EN_NUM = {1: "one|single|a single", 2: "two|couple", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven",
           8: "eight", 9: "nine", 10: "ten"}


def _number_in_text(n: int, text: str) -> bool:
    if re.search(r"(?<!\d)" + str(n) + r"(?!\d)", text):
        return True
    if n in _CN_NUM and re.search("[" + _CN_NUM[n] + "]", text):
        return True
    return n in _EN_NUM and re.search(r"\b(?:" + _EN_NUM[n] + r")\b", text, re.I) is not None


def grounded(field: str, value, text: str) -> bool:
    """這個值在原句裡有沒有根據。"""
    low = text.lower()
    if field == "image":
        from agents.cost_agent import _image_name
        name = _image_name(str(value))
        aliases = {"mongo": ("mongo",), "postgres": ("postgre",), "httpd": ("httpd", "apache")}.get(name, (name,))
        return any(a in low for a in aliases)
    if field == "app_name":
        return str(value).lower() in low
    if field in ("replicas", "port"):
        try:
            return _number_in_text(int(value), text)
        except (TypeError, ValueError):
            return False
    if field in ("memory", "cpu"):
        m = re.match(r"(\d+)", str(value))
        return bool(m) and m.group(1) in text
    return False


def apply_normalized(spec: Dict, data: Dict) -> None:
    """把正規化的輸出寫進 spec（沒根據的值丟掉並記錄）。"""
    text = spec["text"]
    if not isinstance(data, dict):
        return
    spec["purpose"] = str(data.get("purpose") or "")[:200]
    base = data.get("base") if isinstance(data.get("base"), dict) else {}
    dropped = []
    for f in schema.BASE_FIELDS:
        v = base.get(f)
        if v is None or v == "":
            continue
        if grounded(f, v, text):
            schema.set_field(spec, f, v, "user", evidence=None)
        else:
            dropped.append(f"{f}={v}")
    if dropped:
        spec["trace"].append({"step": "normalize", "ms": 0, "detail": "原句找不到根據，丟掉：" + "、".join(dropped)})
    for e in data.get("extras") or []:
        add_extra(spec, e, source="user")


# 2026-11-16 S01～S40 實測：本地模型會把「讓客人掃 QR code 可以看」標成不對外、「為期三天」標成定時執行，
# 而且這兩種會蓋過推理代理的判斷。所以這兩種一定要在 evidence 裡看到明確的字眼才採用。
_CUE = {
    "no_expose": re.compile(
        r"不(?:要|用|需要|必|能|可以|讓|准)?.{0,8}(?:連|開放|暴露|公開|對外|外網|畫面|網頁)|別.{0,4}(?:開|暴露|連)|"
        r"內網|內部(?:系統|網路|使用|專用)|只給.{0,12}(?:自己|內部|後端|程式)|"
        r"nobody|no one|internal|not\b.{0,25}(?:access|connect|public|expose|reach)|never\b.{0,25}(?:connect|incoming|access)",
        re.I),
    "schedule": re.compile(
        r"每(?:天|日|晚|週|周|星期|禮拜|個?小時|分鐘|隔|\s*\d+\s*(?:分|小時|秒|天))|半夜|凌晨|定時|排程|"
        r"every\b|daily|nightly|hourly|weekly|cron|midnight", re.I),
}


def add_extra(spec: Dict, e: Dict, source: str) -> bool:
    """加一個額外欄位。evidence 要真的出現在原句；同一段 evidence 已經記過的不重複加。"""
    if not isinstance(e, dict):
        return False
    kind = str(e.get("kind") or "other").strip().lower()
    kind = kind if kind in EXTRA_KINDS else "other"
    ev = str(e.get("evidence") or "").strip()
    if not ev or ev not in spec["text"]:
        spec["trace"].append({"step": "extras", "ms": 0, "detail": f"evidence 不在原句，略過：{kind} {ev[:40]!r}"})
        return False
    # 「不能讓觀眾連不上」「千萬不能掛掉連不上」是要穩定，不是不對外（雙重否定）
    if kind in _CUE and (not _CUE[kind].search(ev) or (kind == "no_expose" and re.search(r"連不上|連不到|斷掉|掛掉", ev))):
        spec["trace"].append({"step": "extras", "ms": 0, "detail": f"evidence 沒有明確字眼，略過：{kind} {ev[:40]!r}"})
        return False
    for old in spec["extras"]:
        if old["kind"] == kind and (old["evidence"] in ev or ev in old["evidence"]):
            return False
    spec["extras"].append({"kind": kind, "desc": str(e.get("desc") or "")[:200], "value": e.get("value"),
                           "evidence": ev, "source": source, "status": "pending", "note": ""})
    return True


def normalize(spec: Dict, gemini: Optional[LLM], local: Optional[LLM]) -> None:
    t0 = time.time()
    data, used = None, None
    for name, fn in (("gemini", gemini), ("local", local)):
        if fn is None:
            continue
        # 2026-11-22：PIPELINE_CLARITY 開著時，Gemini 順便判斷「使用者有沒有講清楚要哪一類軟體」（方案 E，不多一次呼叫）。
        # 本地正規化照舊用原本的 prompt
        system = NORMALIZE_SYSTEM + CLARITY_ADDON if name == "gemini" and clarity_mode() != "off" else NORMALIZE_SYSTEM
        raw = fn(system, spec["text"])
        data = extract_json(raw)
        if isinstance(data, dict):
            used = name
            break
    spec["backend"]["normalize"] = used
    if used is None:
        spec["trace"].append({"step": "normalize", "ms": int((time.time() - t0) * 1000), "detail": "兩個模型都失敗"})
        return
    apply_normalized(spec, data)
    if used == "gemini" and clarity_mode() != "off":
        apply_clarity(spec, data.get("clarity"))
        _second_check(spec, gemini)
    spec["trace"].append({"step": "normalize", "ms": int((time.time() - t0) * 1000), "detail": f"用 {used}"})
