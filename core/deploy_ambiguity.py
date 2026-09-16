"""
core/deploy_ambiguity.py
判斷一份已經解析好的部署 spec 是不是「用預設值蒙混過去」，而不是真的問出使用者要
什麼——部署缺欄位時，決定該用預設值猜還是該反問使用者。

刻意獨立成一個不需要 torch/flask 的小模組：llama_client.py 頂層無條件 import torch，
如果把這個純函式寫在 llama_client.py 裡面，任何想測試它的測試檔都會連帶被拖進 torch，
變成要標 @pytest.mark.integration 才能跑——這個函式本身是純邏輯（字串/字典輸入輸出），
應該跟 agents/cost_agent.py 那些純函式一樣可以直接進 CI（pytest -m "not integration"）。
llama_client.py 的 ask_llama() 會 import 這裡的函式來用。
"""
from typing import Optional


def deploy_ambiguity_check(raw_input: str, spec: dict) -> dict:
    """
    回傳 {"ambiguous": bool, "reason": str, "hint": str|None}：

      - ambiguous=True：image/app_name 完全沒有任何線索（使用者的話裡連
        agents/cost_agent 認得的技術關鍵字都沒有），這種情況下 image 欄位是
        套用寫死的 nginx:latest 預設值硬猜出來的，不是真的解析出使用者要什麼。
        呼叫端應該先反問，不要直接把這份猜出來的 spec 送進部署審查流程。
      - hint：不擋流程的提醒（例如「這類服務通常建議至少 2 個副本」），有值時
        呼叫端可以顯示在確認卡片上，不需要因此中斷流程。

    只檢查 image/app_name 這種「猜錯會讓使用者以為系統聽懂了、實際上部署了完全
    不相干服務」的欄位；port/memory/cpu 維持既有預設值邏輯不變——這些欄位猜錯的
    代價低（部署確認卡片上本來就能直接改，不會誤導使用者到跑錯 image 的程度）。
    """
    empty = {"ambiguous": False, "reason": "", "hint": None}
    if not isinstance(spec, dict) or "error" in spec:
        return empty

    image = str(spec.get("image", "") or "").strip()
    app_name = str(spec.get("app_name", "") or "").strip()

    has_hint = _has_service_type_hint(raw_input)
    is_default_image = (not image) or image.startswith("nginx")
    is_default_app_name = (not app_name) or app_name == "auto-app"

    if not has_hint and is_default_image and is_default_app_name:
        return {
            "ambiguous": True,
            "reason": (
                "看不出想部署什麼服務，image 是用預設值猜的，不是從輸入解析出來的。 / "
                "Couldn't tell what service to deploy — the image was a guessed default, "
                "not something parsed from the request."
            ),
            "hint": None,
        }

    return {"ambiguous": False, "reason": "", "hint": _replica_hint(image, app_name, spec.get("pods"))}


def _has_service_type_hint(raw_input: str) -> bool:
    """使用者原始輸入裡有沒有任何服務類型線索（技術關鍵字，例如 nginx/redis/postgres）。
    延遲 import agents.cost_agent，避免這個模組被 import 時就強制拉進整個 agents 套件；
    agents 不可用時保守回傳 False（沒有線索），讓呼叫端走反問，不要冒然當作有線索。
    """
    try:
        from agents.cost_agent import _detect_app_type
    except Exception:
        return False
    return _detect_app_type(raw_input, raw_input) != "default"


def _replica_hint(image: str, app_name: str, pods) -> Optional[str]:
    """副本數是預設值、但這個服務類型通常建議跑多副本時，回傳一句不擋流程的提醒。
    比對不到、agents 模組不可用時安靜回 None，不影響主流程。"""
    try:
        from agents.perf_agent import _HPA_PROFILES, _detect_app_type
        app_type = _detect_app_type(image, app_name)
        profile = _HPA_PROFILES.get(app_type)
        pods_int = int(pods) if pods is not None else 1
    except Exception:
        return None
    if profile and profile.get("min", 1) >= 2 and pods_int < profile["min"]:
        return (
            f"{app_type} 類型的服務通常建議至少 {profile['min']} 個副本以確保高可用"
            f"（目前是 {pods_int} 個）。 / "
            f"{app_type}-type services are usually recommended to run at least "
            f"{profile['min']} replicas for high availability (currently {pods_int})."
        )
    return None
