"""照題目的 pass_if 評分（2026-11-24）。

原本的評分（run_pipeline_claude.score）只看映像檔、副本數、port、記憶體、CPU 五格，題目的 extra 與 pass_if
（保存空間、定時排程、對不對外、密碼、搭配資料庫）完全沒檢查，分數比題目作者的「通過」寬很多。
這裡把 pass_if 拆成一條一條，逐條判斷；五格的部分沿用原本的評分結果。

判斷規則（寫死、不看題目內容調整）：
- 多服務（「搭配資料庫」「prometheus 與 grafana」「另一個備份 CronJob」）：系統一次只部署一個服務 → 一律算沒做到
- 密碼：資料庫映像檔（系統會自動產生密碼）或 manifest 有 Secret／名稱含 PASSWORD 的環境變數
- 無法歸類的條件記在 unknown，不算對也不算錯，由呼叫端回報
"""
from __future__ import annotations

import re
from typing import Dict, List

from agents.pipeline.defaults import MAX_STORAGE_GI

_BASIC = re.compile(r"accept|副本|^port|映像檔|資源|記憶體|合理|輕量|干擾|全部達成|wordpress 80|ghost 2368")
_MULTI = re.compile(r"搭配資料庫|與 ?grafana|grafana 且|prometheus 與|備份 CronJob")
_STORAGE = re.compile(r"空間|儲存|PVC")
_NO_EXPOSE = re.compile(r"不對外|內部網路|內網|ClusterIP|無開放|沒有開放|無對外|未對外|沒有建立.*Service|沒有對外")
_EXPOSE = re.compile(r"對外開放")
_CRON = re.compile(r"CronJob|定時|排程")
_PASSWORD = re.compile(r"密碼|帳密")


def _gi(size) -> float:
    m = re.match(r"([\d.]+)\s*(Gi|Mi|Ti)?", str(size or ""))
    if not m:
        return 0.0
    v = float(m.group(1))
    return {"Mi": v / 1024, "Ti": v * 1024}.get(m.group(2), v)


def _cron_ok(clause: str, pred: Dict) -> bool:
    sch = pred.get("schedule") or ""
    if pred.get("workload") != "cronjob" or not sch:
        return False
    f = sch.split()
    if len(f) != 5:
        return False
    if m := re.search(r"每 ?(\d+) ?小時", clause):
        return f[1] == f"*/{m.group(1)}" and f[2:] == ["*", "*", "*"]
    if m := re.search(r"每 ?(\d+) ?分鐘", clause):
        return f[0] == f"*/{m.group(1)}"
    hour = None
    if m := re.search(r"(\d+)[～~-](\d+) ?點", clause):
        lo, hi = int(m.group(1)), int(m.group(2))
        hour_ok = f[1].isdigit() and lo <= int(f[1]) <= hi
    elif m := re.search(r"(\d+):00|(\d+) ?點", clause):
        hour = int(m.group(1) or m.group(2))
        if "晚上" in clause and hour < 12:
            hour += 12
        hour_ok = f[1] == str(hour) and f[0] == "0"
    else:
        hour_ok = True
    if "每週一" in clause:
        return hour_ok and f[4] in ("1", "MON", "mon") and f[2] == "*"
    if "每週" in clause:
        return hour_ok and f[4] != "*" and f[2] == "*"
    if re.search(r"每天|每日", clause):
        return hour_ok and f[2:] == ["*", "*", "*"]
    return hour_ok


def _password_ok(pred: Dict, man: Dict) -> bool:
    if pred.get("db_password"):
        return True
    if man.get("secret_keys"):
        return True
    return any("PASS" in str(k).upper() for k in (man.get("env_keys") or []))


def score_passif(task: Dict, pred: Dict, base_passed: bool, man: Dict | None = None) -> Dict:
    """回傳 {"passed", "checks": {條件: bool}, "unknown": [條件]}；只用在不該問的題。"""
    man = man or {}
    checks: Dict[str, bool] = {}
    unknown: List[str] = []
    extra_needs = " ".join(e.get("need", "") for e in task.get("extra") or [])
    for raw in re.split(r"[、，,；;]|與(?=對外|密碼)", task.get("pass_if", "")):
        c = raw.strip().rstrip("）)").strip()
        if not c or _BASIC.search(c):
            continue
        if _MULTI.search(c) or (c.startswith("有每天") and "另一個 CronJob" in extra_needs):
            checks[c] = False
        elif _CRON.search(c):
            checks[c] = _cron_ok(c, pred)
        elif _STORAGE.search(c):
            st = pred.get("storage")
            m = re.search(r"至少 ?(\d+) ?Gi", c)
            need = int(m.group(1)) if m else 0
            if need > MAX_STORAGE_GI:
                # 2026-11-24：題目要的超過平台上限（每個服務最多 5Gi），一定給不到。
                # 有問使用者（storage_size 問題卡），或給到上限並在部署單告知，就算正確處理
                asked = any(q.get("field") == "storage_size" for q in pred.get("questions") or [])
                at_max = bool(st) and _gi(st.get("size")) >= MAX_STORAGE_GI and bool(st.get("limit_notice"))
                checks[c] = asked or at_max
            else:
                checks[c] = bool(st) and _gi(st.get("size")) >= need
        elif _NO_EXPOSE.search(c):
            checks[c] = pred.get("expose") != "public"
        elif _EXPOSE.search(c):
            checks[c] = pred.get("expose") == "public"
        elif _PASSWORD.search(c):
            checks[c] = _password_ok(pred, man)
        else:
            unknown.append(c)
    # S31 這類「資料庫＋另一個備份排程」：pass_if 的備份條件可能寫成「有每天 04:00 的備份 CronJob」
    if "另一個 CronJob" in extra_needs and not any(_MULTI.search(k) or k.startswith("有每天") for k in checks):
        checks["另開備份排程（一次只部署一個服務）"] = False
    return {"passed": bool(base_passed) and all(checks.values()), "checks": checks, "unknown": unknown}


# ---- 該問的題（2026-11-24）----
# 原本只要「有任何問題卡」就算問到：k8s40 S38 部署了 postgres:15、只問了名稱格式，也被算成通過。
# 嚴格版分兩層：
#   1. 自動：沒有部署（image 是空的）而且有問題卡，才有資格算通過
#   2. 人工：問題有沒有涵蓋 pass_if 點名的項目，比較主觀，記在 must_ask_judgments.json（附理由與當時的問句原文）；
#      重跑後問句變了就標「待重新判斷」，不沿用舊判定
import json as _json
import os as _os

JUDGMENTS = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "must_ask_judgments.json")


def _load_judgments() -> Dict:
    try:
        return _json.load(open(JUDGMENTS, encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def score_must_ask(set_: str, task_id: str, pred: Dict, questions: List[Dict]) -> Dict:
    asked = [q.get("q") or q.get("question") or "" for q in questions]
    if not asked or pred.get("image"):
        why = "沒有反問" if not asked else f"已經部署 {pred.get('image')}"
        return {"passed": False, "auto_fail": why, "judged": True}
    j = _load_judgments().get(f"{set_}:{task_id}")
    if not j or j.get("asked") != asked:
        return {"passed": None, "judged": False, "note": "待人工判斷（沒有判定，或系統問的句子變了）"}
    return {"passed": bool(j["passed"]), "judged": True, "reason": j.get("reason", "")}
