"""推理代理：
1. fill_gaps：本地模型依原句推出空著的欄位（source=inferred），也會提出「非問不可」的問題。
2. apply_extras：把額外欄位對應到部署設定（固定規則，不靠模型）。對應不到的明白標成不支援或變成問題，不默默丟掉。"""
from __future__ import annotations

import re
import time
from typing import Dict, Optional

from agents.pipeline import cron as cron_util
from agents.pipeline import defaults, schema
from agents.pipeline.llm import LLM, extract_json
from agents.pipeline.prompts import REASON_SYSTEM

_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,100}$")


# ── 1. 推理補空缺 ────────────────────────────────────────────────────
def fill_gaps(spec: Dict, local: Optional[LLM]) -> Dict:
    """呼叫一次推理模型。回傳模型輸出（解析失敗回 {}），並把結果寫進 spec。"""
    if local is None:
        return {}
    t0 = time.time()
    # 2026-11-17：只送原句（和選型實測時的輸入一樣）。原本附上「已決定的欄位」，實測 S38、S39 附上後模型就不提問、
    # 直接猜軟體（不附時兩題都會問）。使用者講過的欄位由來源等級保護（set_field 不讓 inferred 蓋 user），不需要靠 prompt。
    out = extract_json(local(REASON_SYSTEM, spec["text"]))
    ms = int((time.time() - t0) * 1000)
    if not isinstance(out, dict):
        spec["trace"].append({"step": "reason", "ms": ms, "detail": "輸出解析失敗"})
        return {}
    filled = [f for f in schema.empty_fields(spec) if schema.set_field(spec, f, out.get(f), "inferred")]
    schema.set_choice(spec, "workload", out.get("workload"), "inferred")
    schema.set_choice(spec, "expose", out.get("expose"), "inferred")
    _inferred_extras(spec, out)
    for q in out.get("questions") or []:
        if isinstance(q, dict) and str(q.get("question") or "").strip():
            field = "image" if schema.value(spec, "image") is None and not out.get("image") else f"model:{len(spec['questions'])}"
            schema.add_question(spec, field, str(q["question"])[:300],
                                recommended=str(q.get("recommended") or "")[:200] or None,
                                reason="推理代理認為這一項不能替你決定")
    spec["trace"].append({"step": "reason", "ms": ms, "detail": f"補了 {', '.join(filled) or '無'}；{out.get('reason', '')}"[:300]})
    return out


def _has_extra(spec: Dict, kind: str) -> bool:
    return any(e["kind"] == kind and e["status"] != "dropped" for e in spec["extras"])


def _inferred_extras(spec: Dict, out: Dict) -> None:
    """推理代理推出、但使用者沒明講的設定（例如部落格要保存上傳的檔案）。標成 inferred，部署單上會寫「系統建議」。"""
    st = out.get("storage")
    if isinstance(st, dict) and not _has_extra(spec, "storage"):
        spec["extras"].append({"kind": "storage", "desc": "保存資料（系統建議）", "value": {"size": st.get("size")},
                               "evidence": "", "source": "inferred", "status": "pending", "note": ""})
    cron = valid_cron(out.get("schedule"))
    if out.get("workload") == "cronjob" and cron:
        sched = [e for e in spec["extras"] if e["kind"] == "schedule" and e["status"] != "dropped"]
        if not sched:
            spec["extras"].append({"kind": "schedule", "desc": "定時執行", "value": {"cron": cron},
                                   "evidence": "", "source": "inferred", "status": "pending", "note": ""})
        else:
            # 使用者講了要定時，但正規化寫出來的排程格式不對（例如 "3 00 * * *"、「每 5 分鐘」）：用推理代理的排程
            cur = sched[0]["value"].get("cron") if isinstance(sched[0]["value"], dict) else sched[0]["value"]
            if not valid_cron(cur):
                sched[0]["value"] = {"cron": cron}


_CRON_FIELD = r"(\*|\*/\d{1,2}|\d{1,2}(-\d{1,2})?(,\d{1,2})*)"
_CRON_RE = re.compile(r"^" + r"\s+".join([_CRON_FIELD] * 5) + r"$")


def valid_cron(v) -> Optional[str]:
    """五欄位 cron，分鐘 0～59、小時 0～23；格式不對回 None。"""
    if isinstance(v, dict):
        v = v.get("cron")
    s = " ".join(str(v or "").split())
    if not _CRON_RE.match(s):
        return None
    minute, hour = s.split()[:2]
    for f, hi in ((minute, 59), (hour, 23)):
        if f.isdigit() and int(f) > hi:
            return None
    return s


# ── 2. 額外欄位 → 部署設定 ───────────────────────────────────────────
def _size_gi(v) -> Optional[int]:
    m = re.match(r"^\s*(\d+)\s*(g|gi|gb|gib)?\s*$", str(v or ""), re.I)
    return int(m.group(1)) if m else None


def apply_extras(spec: Dict) -> None:
    man = spec["manifest"]
    image = schema.value(spec, "image") or ""
    for e in spec["extras"]:
        if e["status"] != "pending":
            continue
        kind, val = e["kind"], e["value"]
        if kind == "secret":
            from agents.deploy_fill import db_required_env
            _env, keys = db_required_env(image, man.get("env"))
            if keys and isinstance(val, str) and val.strip():
                man.setdefault("env", {})
                for k in keys:
                    man["env"][k] = val.strip()[:200]
                man["secret_keys"] = sorted(set(man.get("secret_keys", [])) | set(keys))
                e["status"] = "applied"
            else:
                e["status"] = "unsupported"
                e["note"] = "不知道這個軟體的密碼要放在哪個設定，請在部署單的環境變數自己填"
        elif kind == "storage":
            _apply_storage(spec, e, image)
        elif kind == "command":
            if isinstance(val, list) and val:
                man["command"] = [str(x)[:1000] for x in val[:50]]
            elif isinstance(val, str) and val.strip():
                man["command"] = ["sh", "-c", val.strip()[:1000]]
            else:
                e["status"], e["note"] = "unsupported", "看不出要執行的指令"
                continue
            e["status"] = "applied"
        elif kind == "schedule":
            # 2026-11-17 支援定時執行（CronJob）。間隔太短（< 5 分鐘）先保持 pending，由 format_check 問使用者
            c = valid_cron(val)
            if not c:
                e["status"], e["note"] = "unsupported", "看不懂要在什麼時間執行，請在部署單上填排程"
            elif cron_util.too_frequent(c):
                e["value"] = {"cron": c}
                continue
            else:
                e["value"] = {"cron": c}
                man["schedule"] = c
                spec["workload"] = {"value": "cronjob", "source": "user" if e["source"] == "user" else "inferred"}
                spec["expose"] = {"value": "internal", "source": "default"}    # 跑完就結束的程式沒有人會連
                e["status"] = "applied"
        elif kind == "no_expose":
            schema.set_choice(spec, "expose", "internal", "user")
            e["status"] = "applied"
        elif kind == "extra_port":
            n = defaults.name(image)
            if n in defaults.MGMT_PORTS:
                p = defaults.MGMT_PORTS[n][0]
                man["extra_ports"] = [{"port": p, "container": p}]
                new_image = defaults.with_mgmt_tag(image)
                if new_image != image:
                    spec["base"]["image"] = {**spec["base"]["image"], "value": new_image}
                    schema.add_notice(spec, f"管理網頁要用 {new_image} 這個版本的映像檔，已經幫你換掉")
                e["status"] = "applied"
            else:
                e["status"], e["note"] = "unsupported", "不知道這個軟體的管理網頁在哪個 port"
        elif kind == "env":
            if isinstance(val, dict) and val and all(_ENV_NAME_RE.match(str(k)) for k in val):
                man.setdefault("env", {}).update({str(k): str(v)[:4096] for k, v in list(val.items())[:20]})
                e["status"] = "applied"
            else:
                e["status"], e["note"] = "unsupported", "不知道這個設定對應哪個環境變數"
        else:
            e["status"], e["note"] = "unsupported", "這部分目前沒辦法自動處理"
        if e["status"] == "unsupported":
            schema.add_notice(spec, f"「{e['evidence'] or e['desc']}」：{e['note']}")
    _need_cron_command(spec, image)


def _need_cron_command(spec: Dict, image: str) -> None:
    """定時執行一定要有要跑的指令：程式執行環境（python、node、busybox…）沒有指令就什麼都不會做，
    也不能像背景程式那樣先放空殼（跑完就結束）。使用者 11-17 決定在問題卡上問。"""
    if spec["workload"]["value"] != "cronjob" or spec["manifest"].get("command"):
        return
    if image and defaults.name(image) not in defaults.RUNTIME_IMAGES:
        return      # 軟體本身就有預設要做的事（例如資料庫備份工具），不硬問
    schema.add_question(spec, "cron_command",
                        "每次定時執行時，要跑什麼指令？例如：python /app/backup.py，或 echo hello",
                        recommended=None, reason="定時執行的程式跑完就結束，沒有指令就什麼都不會做")


def _apply_storage(spec: Dict, e: Dict, image: str) -> None:
    man = spec["manifest"]
    path = defaults.data_path(image)
    if not path:
        e["status"], e["note"] = "unsupported", f"不知道 {image or '這個軟體'} 的資料存在哪個資料夾"
        return
    val = e["value"] if isinstance(e["value"], dict) else {}
    want = _size_gi(val.get("size"))
    size = f"{min(want, defaults.MAX_STORAGE_GI)}Gi" if want else defaults.DEFAULT_STORAGE
    # 只有使用者自己講的大小（evidence 裡有那個數字）超過上限才問；推理代理自己估的大小直接用上限內的值
    user_said = e["source"] == "user" and want is not None and re.search(r"(?<!\d)" + str(want) + r"(?!\d)", e["evidence"] or "")
    if want and want > defaults.MAX_STORAGE_GI and user_said:
        schema.add_question(spec, "storage_size",
                            f"你需要約 {want}GB 的保存空間，但目前每個服務最多只能給 {defaults.MAX_STORAGE_GI}GB。要怎麼處理？",
                            options=[{"value": f"{defaults.MAX_STORAGE_GI}Gi", "label": f"先用 {defaults.MAX_STORAGE_GI}GB"},
                                     {"value": "cancel", "label": "先不部署"}],
                            recommended=None, reason="超過上限不自動調低")
    vol = {"name": "data", "mount": path, "kind": "pvc", "size": size, "read_only": False}
    man["volumes"] = [vol]
    extra_env = defaults.DATA_ENV.get(defaults.name(image))
    if extra_env:
        man.setdefault("env", {}).update(extra_env)
    if defaults.name(image) in defaults.RUNTIME_IMAGES:
        schema.add_notice(spec, f"保存空間掛在 {path}，你的程式要把資料寫在這個資料夾才會保留")
    e["status"] = "applied"
