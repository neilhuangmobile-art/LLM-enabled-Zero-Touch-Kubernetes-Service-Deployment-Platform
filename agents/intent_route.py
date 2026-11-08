"""
意圖判斷的結果查核（2026-11-08，第二次對照實驗之後）。

第二次實驗的盲測找到的根因：部署意圖靠規則加 3B 分類器判斷，3B 的提示詞把 qa 當成「其他所有情況」、
沒把握就選 qa，所以新手的說法（「幫我弄一個網站」「set up a blog」「背景跑的小程式」）被判成問答，
系統只回一段教學，後面的部署流程完全沒有執行到。也因為 3B 不知道使用者有哪些服務，
「把那個 nginx 網站退回上一版」對不到名稱，只能反問。

改成：規則比不到時先問 Gemini（core/gemini_client.gemini_route_intent），附上使用者目前的服務清單；
Gemini 的回答只當建議，這裡用固定規則查核，不合格的交回 3B 或改成反問。
- 動作必須在允許的清單裡。
- 擴縮、換版本、回滾、刪除的服務名稱，必須是使用者真的有的服務，或原句裡出現過的名稱。
- 副本數必須是原句裡出現過的數字；映像檔的版本必須在原句出現過。
- 這些操作在畫面上一律還要使用者按確認才執行（前端 startDestructiveFlow），這裡只決定要帶出哪一張確認卡。

只看傳進來的參數，不碰 K8s、不呼叫模型，方便測試。
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

ALLOWED_ACTIONS = {
    "deploy", "scale", "update_image", "rollback", "delete", "list_pods", "list_deployments",
    "describe_pod", "pod_health", "describe_deployment", "healer_scan", "healer_fix", "healer_auto_fix",
    "gitops_log", "cluster_metrics", "qa",
}
# 要指定一個服務的操作（跟 core/model_server 的 _INTENT_DESTRUCTIVE 一致，另外加上只讀的單一服務查詢）
SERVICE_ACTIONS = {"scale", "update_image", "rollback", "delete", "describe_deployment"}
DESTRUCTIVE = {"scale", "update_image", "rollback", "delete", "healer_fix", "healer_auto_fix"}
REQUIRED = {"scale": ("name", "replicas"), "update_image": ("name", "image"), "rollback": ("name",),
            "delete": ("name",), "describe_deployment": ("name",), "describe_pod": ("name",),
            "pod_health": ("name",), "healer_fix": ("pod_name",)}

_CN_NUM = {"一": 1, "兩": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _numbers_in(text: str) -> set:
    nums = {int(x) for x in re.findall(r"\d+", text or "")}
    nums |= {v for k, v in _CN_NUM.items() if k in (text or "")}
    return nums


def _in_text(value: str, text: str) -> bool:
    v = (value or "").strip().lower()
    return bool(v) and re.search(r"(?<![a-z0-9-])" + re.escape(v) + r"(?![a-z0-9-])", (text or "").lower()) is not None


def _image_ok(image: str, text: str, current_image: str) -> bool:
    """映像檔要有根據：名稱在原句出現過或跟這個服務現在的映像檔同名；有寫版本的話，版本要在原句出現過。"""
    image = (image or "").strip().lower()
    if not image:
        return False
    base, _, tag = image.rpartition(":") if ":" in image.split("/")[-1] else (image, "", "")
    base_short = base.split("/")[-1]
    cur_short = (current_image or "").lower().split(":")[0].split("/")[-1]
    if not (_in_text(base_short, text) or (cur_short and base_short == cur_short)):
        return False
    if tag and tag != "latest":
        return tag in (text or "").lower()
    if tag == "latest":
        return re.search(r"latest|最新", text or "", re.I) is not None
    return True


def _clarify(action: str, args: dict, confidence: float, reasons: List[str], ungrounded: List[str]) -> dict:
    return {"action": "clarify", "args": {"guess": action, **args}, "confidence": confidence,
            "clarify_reasons": reasons, "ungrounded_values": ungrounded, "source": "gemini"}


def validate_routed_intent(result: Optional[dict], message: str, services: List[Dict]) -> Optional[dict]:
    """查核 Gemini 的意圖判斷。回傳可以交給前端的 {action, args, confidence, source}；
    格式不對、動作不認得時回 None（呼叫端改用 3B 分類器）。"""
    if not isinstance(result, dict):
        return None
    action = str(result.get("action") or "").strip()
    if action not in ALLOWED_ACTIONS:
        return None
    raw_args = result.get("args")
    raw_args = raw_args if isinstance(raw_args, dict) else {}
    try:
        confidence = max(0.0, min(1.0, float(result.get("confidence", 0.0))))
    except (TypeError, ValueError):
        confidence = 0.0
    if action in ("deploy", "qa") or action not in REQUIRED and action not in DESTRUCTIVE:
        # 部署：前端只把原句交給 /api/deploy/parse 重新解析，不用模型給的欄位
        return {"action": action, "args": {}, "confidence": confidence, "source": "gemini"}

    args: dict = {}
    for k in ("name", "pod_name", "image"):
        if raw_args.get(k):
            args[k] = str(raw_args[k]).strip()
    if raw_args.get("replicas") is not None:
        try:
            args["replicas"] = int(raw_args["replicas"])
        except (TypeError, ValueError):
            pass

    names = {str(s.get("name") or "").lower(): s for s in services or [] if s.get("name")}
    reasons, ungrounded = [], []
    missing = [k for k in REQUIRED.get(action, ()) if k not in args]
    if missing:
        reasons.append("missing_args")
    name = args.get("name", "")
    svc = names.get(name.lower()) if name else None
    if name and action in SERVICE_ACTIONS and svc is None and not _in_text(name, message):
        ungrounded.append(name)
    if name and action in ("describe_pod", "pod_health") and svc is None and not _in_text(name, message):
        ungrounded.append(name)
    if args.get("pod_name") and not _in_text(args["pod_name"], message):
        ungrounded.append(args["pod_name"])
    if "replicas" in args and args["replicas"] not in _numbers_in(message):
        ungrounded.append(str(args["replicas"]))
    if action == "update_image" and args.get("image") and not _image_ok(args["image"], message, (svc or {}).get("image", "")):
        ungrounded.append(args["image"])
    if ungrounded:
        reasons.append("ungrounded_args")
    if action in DESTRUCTIVE and confidence < 0.75:
        reasons.append("low_confidence")
    if reasons:
        return _clarify(action, args, confidence, reasons, ungrounded)
    if svc is not None:
        args["name"] = svc["name"]   # 用叢集裡真正的名稱（大小寫）
    return {"action": action, "args": args, "confidence": confidence, "source": "gemini"}


def services_brief(services: List[Dict], limit: int = 30) -> str:
    """給 Gemini 看的服務清單（一行一個）。名稱與映像檔是使用者自己的資料，當成資料不是指令。"""
    rows = []
    for s in (services or [])[:limit]:
        rows.append(f"- name={s.get('name')} image={s.get('image')} replicas={s.get('replicas')}")
    return "\n".join(rows) if rows else "(none)"
