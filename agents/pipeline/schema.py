"""中間 JSON 的格式、欄位檢查和來源等級。

每個基礎欄位存成 {"value", "source", "evidence"}：
- source：user（使用者講的）> inferred（推理代理推的）> default（程式預設值）
- 後面的步驟不能覆蓋來源等級比自己高的值（set_field 統一把關）。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

BASE_FIELDS = ("image", "app_name", "replicas", "port", "memory", "cpu")
SOURCE_RANK = {"default": 1, "inferred": 2, "user": 3}
WORKLOADS = ("service", "worker", "cronjob")
EXPOSES = ("public", "internal", "none")
MAX_REPLICAS = 20          # 部署單上單一服務的上限；更多的交給負荷檢查問使用者

_IMAGE_RE = re.compile(r"^[a-z0-9]([a-z0-9._/-]{0,200}[a-z0-9])?(:[A-Za-z0-9._-]{1,128})?$")
_DNS_RE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,40}[a-z0-9])?$")
_MEM_RE = re.compile(r"^([1-9]\d{0,5})(Mi|Gi)$")
_CPU_RE = re.compile(r"^([1-9]\d{0,5})m$|^([1-9]\d?)(\.\d{1,3})?$")


def new_spec(text: str) -> Dict[str, Any]:
    return {
        "text": text,
        "purpose": "",
        "base": {f: {"value": None, "source": None, "evidence": None} for f in BASE_FIELDS},
        "workload": {"value": None, "source": None},
        "expose": {"value": None, "source": None},
        "extras": [],        # [{kind, desc, value, evidence, source, status, note}]
        "manifest": {},      # 額外欄位對應出來的部署設定：env、secret_keys、volumes、command、extra_ports
        "questions": [],     # [{id, field, question, options:[{value,label}], recommended, reason}]
        "notices": [],       # 要明白告訴使用者的事（不支援、被調整）
        "trace": [],         # [{step, ms, detail}]
        "attempts": {"intent": 0, "gap": 0, "verify": 0},
        "verified": [],      # 意圖符合檢查改過的：[{field, from, to, issue}]
        "backend": {},       # 每一步實際用了哪個模型
    }


# ── 欄位值的格式檢查（不合格就當作沒填） ──────────────────────────────
def clean_value(field: str, v: Any) -> Any:
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    if field == "image":
        s = str(v).strip().split("@")[0]
        if ":" in s.split("/")[-1]:                  # 名稱轉小寫，tag 保留原樣（例如 3-Management 很少見但合法）
            name, tag = s.rsplit(":", 1)
            s = f"{name.lower()}:{tag}"
        else:
            s = s.lower()
        return s if _IMAGE_RE.match(s) else None
    if field == "app_name":
        s = re.sub(r"[^a-z0-9-]+", "-", str(v).strip().lower()).strip("-")[:42].strip("-")
        return s if _DNS_RE.match(s) else None
    if field in ("replicas", "port"):
        try:
            n = int(v)
        except (TypeError, ValueError):
            return None
        if field == "replicas":
            return n if 1 <= n <= MAX_REPLICAS else None
        return n if 1 <= n <= 65535 else None
    if field == "memory":
        s = str(v).strip().replace(" ", "")
        m = re.match(r"^(\d+)(Mi|Gi|M|G|MB|GB|MiB|GiB)$", s, re.I)
        if not m:
            return None
        unit = "Gi" if m.group(2).lower().startswith("g") else "Mi"
        s = f"{int(m.group(1))}{unit}"
        return s if _MEM_RE.match(s) else None
    if field == "cpu":
        s = str(v).strip().replace(" ", "")
        return s if _CPU_RE.match(s) else None
    return v


def set_field(spec: Dict, field: str, value: Any, source: str, evidence: Optional[str] = None) -> bool:
    """寫入基礎欄位。值不合格、或現有值的來源等級比這次高，就不寫。回傳有沒有寫進去。"""
    v = clean_value(field, value)
    if v is None:
        # 格式不合格（不是空值）：記下來給最後的格式修正（format_check）試著修，不要無聲丟掉
        if value is not None and not (isinstance(value, str) and not value.strip()):
            spec.setdefault("rejected", []).append({"field": field, "value": str(value)[:100], "source": source})
        return False
    cur = spec["base"][field]
    if cur["value"] is not None and SOURCE_RANK.get(cur["source"], 0) > SOURCE_RANK[source]:
        return False
    spec["base"][field] = {"value": v, "source": source, "evidence": evidence}
    return True


def set_choice(spec: Dict, key: str, value: Any, source: str) -> bool:
    """workload／expose 這類有固定選項的欄位，規則同 set_field。"""
    allowed = WORKLOADS if key == "workload" else EXPOSES
    v = str(value or "").strip().lower()
    if v not in allowed:
        return False
    cur = spec[key]
    if cur["value"] is not None and SOURCE_RANK.get(cur["source"], 0) > SOURCE_RANK[source]:
        return False
    spec[key] = {"value": v, "source": source}
    return True


def empty_fields(spec: Dict) -> List[str]:
    return [f for f in BASE_FIELDS if spec["base"][f]["value"] is None]


def value(spec: Dict, field: str) -> Any:
    return spec["base"][field]["value"]


def add_question(spec: Dict, field: str, question: str, options: Optional[List[Dict]] = None,
                 recommended: Any = None, reason: str = "") -> None:
    """同一個欄位只問一次。options 的最後一定有「幫我決定」（採用推薦值）。"""
    if any(q["field"] == field for q in spec["questions"]):
        return
    opts = list(options or [])
    if recommended is not None and not any(o.get("value") == "__auto__" for o in opts):
        opts.append({"value": "__auto__", "label": "幫我決定"})
    spec["questions"].append({"id": f"q{len(spec['questions']) + 1}", "field": field, "question": question,
                              "options": opts, "recommended": recommended, "reason": reason})


def add_notice(spec: Dict, text: str) -> None:
    if text and text not in spec["notices"]:
        spec["notices"].append(text)
