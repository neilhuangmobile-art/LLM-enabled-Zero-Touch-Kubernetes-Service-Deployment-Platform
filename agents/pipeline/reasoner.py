"""推理代理：
1. fill_gaps：本地模型依原句推出空著的欄位（source=inferred），也會提出「非問不可」的問題。
2. apply_extras：把額外欄位對應到部署設定（固定規則，不靠模型）。對應不到的明白標成不支援或變成問題，不默默丟掉。"""
from __future__ import annotations

import os
import re
import time
from typing import Dict, Optional

from agents.pipeline import cron as cron_util
from agents.pipeline import defaults, schema
from agents.pipeline.llm import LLM, extract_json
from agents.pipeline.prompts import REASON_SIZING_TIERS, REASON_SYSTEM, REPLICAS_RULE

_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,100}$")


# ── 1. 推理補空缺 ────────────────────────────────────────────────────
def fill_gaps(spec: Dict, local: Optional[LLM]) -> Dict:
    """呼叫一次推理模型。回傳模型輸出（解析失敗回 {}），並把結果寫進 spec。"""
    if local is None:
        return {}
    t0 = time.time()
    # 2026-11-17：只送原句（和選型實測時的輸入一樣）。原本附上「已決定的欄位」，實測 S38、S39 附上後模型就不提問、
    # 直接猜軟體（不附時兩題都會問）。使用者講過的欄位由來源等級保護（set_field 不讓 inferred 蓋 user），不需要靠 prompt。
    out = extract_json(local(reason_system(), _reason_input(spec)))
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
            # 2026-11-18：只有「用哪個軟體」這種不問就做不出來的才問。其餘推理代理自己加的追問（網頁還是 App、
            # 通知方式、舊電腦型號……）改成部署單上的提示，先用預設值，使用者看得到也能改。
            # Claude 題組實測：262 題裡 159 題不該問卻問，其中 145 個是這種追問（CLAM／Clarify When Necessary：
            # 只在歧義會改變結果時才問）。PIPELINE_ASK_POLICY=all 恢復原本做法（對照用）。
            if field != "image" and schema.ask_policy() != "all":
                schema.add_notice(spec, f"系統先用預設值處理，部署前請確認：{str(q['question'])[:200]}")
                continue
            schema.add_question(spec, field, str(q["question"])[:300],
                                recommended=str(q.get("recommended") or "")[:200] or None,
                                reason="推理代理認為這一項不能替你決定")
    spec["trace"].append({"step": "reason", "ms": ms, "detail": f"補了 {', '.join(filled) or '無'}；{out.get('reason', '')}"[:300]})
    return out


def _reason_input(spec: Dict) -> str:
    """推理代理的輸入：原句；PIPELINE_REASON_CANDS=1 時再附上用原句查到的軟體候選（2026-11-18）。
    原本推理代理決定軟體時看不到 RAG，RAG 只在它決定之後才參與（Claude 題組小樣本：RAG 找到正確軟體、
    流程卻沒採用的佔選錯的三成）。做法參考 Gorilla 的檢索感知：候選標明可能不對，不合適就照原本方式決定或反問。
    使用者自己講了軟體時不附（不需要選）。"""
    text = spec["text"]
    import os
    if os.environ.get("PIPELINE_REASON_CANDS", "0").strip() not in ("1", "true", "on") or schema.value(spec, "image"):
        return text
    from agents.pipeline import software_rag
    cands = software_rag.candidates(text, k=5)
    if not cands:
        return text
    lines = "\n".join(f"- {c['image']}: {str(c.get('desc', ''))[:140]}" for c in cands)
    return (f"{text}\n\n[Software found in the platform catalog for this request - may be wrong; use one only if it really "
            f"fits what the user wants. If none fits, choose as you normally would. If it is the user's own program "
            f"or the purpose is unclear, keep image null and ask.]\n{lines}")


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
            if kind == "other":
                # 2026-11-24：other 多半是把需求本身、使用人數、背景重講一次（開發題組 419 題出現 817 則「沒辦法自動處理」），
                # 不一則一則警告，迴圈後合併成一行參考；final＝回答問題卡後 runner.resume 不要把它改回待處理
                e["final"] = True
                continue
        if e["status"] == "unsupported":
            schema.add_notice(spec, f"「{e['evidence'] or e['desc']}」：{e['note']}")
    _other_notice(spec)
    _need_cron_command(spec, image)


OTHER_NOTICE = "這些描述沒有對應到特定設定，只當參考："


def _other_notice(spec: Dict) -> None:
    """kind=other 的額外描述合併成一行提醒（最多列 3 則）。重跑時先拿掉舊的那行，不會重複。"""
    spec["notices"] = [n for n in spec["notices"] if not n.startswith(OTHER_NOTICE)]
    items = [str(e["evidence"] or e["desc"])[:30] for e in spec["extras"]
             if e["kind"] == "other" and e["status"] == "unsupported" and e.get("final")]
    if items:
        more = f" 等 {len(items)} 則" if len(items) > 3 else ""
        schema.add_notice(spec, OTHER_NOTICE + "、".join(f"「{x}」" for x in items[:3]) + more)


CRON_EXAMPLE_COMMAND = ("sh", "-c", "echo 請把這行換成要定時執行的指令")


def _need_cron_command(spec: Dict, image: str) -> None:
    """定時執行一定要有要跑的指令：程式執行環境（python、node、busybox…）沒有指令就什麼都不會做，
    也不能像背景程式那樣先放空殼（跑完就結束）。使用者 11-17 決定在問題卡上問。"""
    if spec["workload"]["value"] != "cronjob" or spec["manifest"].get("command"):
        return
    if image and defaults.name(image) not in defaults.RUNTIME_IMAGES:
        return      # 軟體本身就有預設要做的事（例如資料庫備份工具），不硬問
    # 2026-11-22 使用者決定：預設改成先放範例指令＋部署單提醒（跟背景程式「保持運作的空殼」同一種做法），不出問題卡。
    # 開發題組這類題目的標準答案都是不用問（16 題被算成多問）。PIPELINE_CRON_COMMAND=ask 恢復成問問題卡（對照用）
    if os.environ.get("PIPELINE_CRON_COMMAND", "example").strip().lower() != "ask":
        spec["manifest"]["command"] = list(CRON_EXAMPLE_COMMAND)
        schema.add_notice(spec, "你沒有說每次定時執行要跑什麼指令，先放一個範例指令（目前只會印一行字、不會做事），"
                                "請改成你的程式，例如：python /app/backup.py")
        return
    schema.add_question(spec, "cron_command",
                        "每次定時執行時，要跑什麼指令？例如：python /app/backup.py，或 echo hello",
                        recommended=None, reason="定時執行的程式跑完就結束，沒有指令就什麼都不會做")


# 2026-11-24：保存空間碰到上限時的提醒（開頭固定，評估程式用它判斷有沒有告知使用者）
STORAGE_LIMIT_NOTICE = "保存空間上限："
# 描述暗示資料量可能很大（影片、大量照片、備份、以 GB/TB 計）：只加提醒，不改大小
_BIG_DATA_HINT = re.compile(r"影片|影音|錄影|video|照片|相片|photo|備份|backup|\d+\s*(?:G|GB|T|TB)\b|很大|大檔|大量", re.I)


# 2026-11-25：靜態網頁伺服器不掛保存空間。資料夾就是網頁本身（/usr/share/nginx/html、htdocs），
# 掛上一塊空的保存空間會蓋掉使用者打包好的網頁 → 403、容器一直重啟（使用者實測：看到「報名」就推論要保存資料）
STATIC_WEB_IMAGES = {"nginx", "httpd"}
STATIC_WEB_STORAGE_NOTICE = ("這次沒有掛保存空間：nginx／httpd 會直接提供資料夾裡的網頁，掛上空的保存空間會蓋掉你打包好的網頁"
                             "（打開會出現 403）。網頁請打包進映像檔；如果需要讓人上傳或保存資料（例如報名資料），"
                             "請改用表單、部落格或檔案分享這類軟體")


def _apply_storage(spec: Dict, e: Dict, image: str) -> None:
    man = spec["manifest"]
    if defaults.name(image) in STATIC_WEB_IMAGES:
        # dropped＋final：回答問題卡後也不會重新掛上；評估的 to_pred 也不會算成有保存空間
        e["status"], e["note"], e["final"] = "dropped", "靜態網頁伺服器不掛保存空間", True
        schema.add_notice(spec, STATIC_WEB_STORAGE_NOTICE)
        return
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
    elif want and want > defaults.MAX_STORAGE_GI:
        # 系統自己估的大小超過上限：照樣調到上限，但一定要告訴使用者（原本默默調低）
        schema.add_notice(spec, f"{STORAGE_LIMIT_NOTICE}系統估計你可能需要約 {want}GB，但目前每個服務最多 "
                                f"{defaults.MAX_STORAGE_GI}GB，這次先給 {size}；資料超過時會寫不進去，請先清理或改用外部空間")
    elif _BIG_DATA_HINT.search(spec.get("text") or ""):
        schema.add_notice(spec, f"{STORAGE_LIMIT_NOTICE}這次給 {size}；目前每個服務最多 {defaults.MAX_STORAGE_GI}GB，"
                                f"如果影片、照片或備份這類資料會超過，請在部署單調大，或先清理舊資料")
    vol = {"name": "data", "mount": path, "kind": "pvc", "size": size, "read_only": False}
    man["volumes"] = [vol]
    extra_env = defaults.DATA_ENV.get(defaults.name(image))
    if extra_env:
        man.setdefault("env", {}).update(extra_env)
    if defaults.name(image) in defaults.RUNTIME_IMAGES:
        schema.add_notice(spec, f"保存空間掛在 {path}，你的程式要把資料寫在這個資料夾才會保留")
    e["status"] = "applied"


def reason_system() -> str:
    """推理代理的系統提示詞；PIPELINE_REPLICAS_RULE=0 時不加副本數補充規則（對照實驗用）。"""
    # PIPELINE_REASON_SIZING=tiers（2026-11-23，實驗）：拿掉範本裡會被照抄的 250m／256Mi，改成依負載選刻度
    base = REASON_SIZING_TIERS if os.environ.get("PIPELINE_REASON_SIZING", "").strip().lower() == "tiers" else REASON_SYSTEM
    if os.environ.get("PIPELINE_REPLICAS_RULE", "1") == "0":
        return base
    return base + REPLICAS_RULE

