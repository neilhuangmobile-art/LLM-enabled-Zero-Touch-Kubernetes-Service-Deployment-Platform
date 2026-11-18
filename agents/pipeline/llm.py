"""模型呼叫：Gemini（正規化）與本地 Qwen3.5-4B（core/pipeline_model_server.py，127.0.0.1:8766）。

每個呼叫都是「system prompt + 使用者內容 → 文字」，失敗回 None，由呼叫端決定要不要改用另一個。
測試時直接傳一個假的函式進 runner，不會碰網路。"""
from __future__ import annotations

import json
import os
import re
from typing import Callable, Optional

LLM = Callable[[str, str], Optional[str]]

# 2026-11-18：引擎跟主模型伺服器同一個開關（core/config.py 的 LLM_ENGINE）。
# llamacpp：直接打 llama-server（OpenAI 相容介面），不用啟動 core/pipeline_model_server.py。
from core.config import LLAMACPP_REASON_URL, LLM_ENGINE  # noqa: E402

ENGINE = LLM_ENGINE
# llamacpp 模式一律用 LLAMACPP_REASON_URL：舊設定留下的 PIPELINE_MODEL_URL（8766）在這個模式下不會有人啟動
LOCAL_URL = LLAMACPP_REASON_URL if ENGINE == "llamacpp" else os.environ.get("PIPELINE_MODEL_URL", "http://127.0.0.1:8766")
if ENGINE == "llamacpp" and os.environ.get("PIPELINE_MODEL_URL"):
    print(f"[pipeline] LLM_ENGINE=llamacpp：忽略 PIPELINE_MODEL_URL，改用 {LOCAL_URL}（LLAMACPP_REASON_URL）")
LOCAL_TIMEOUT = float(os.environ.get("PIPELINE_MODEL_TIMEOUT", "60"))


# 使用者內容裡假冒的特殊標記（<|im_start|> 等）先拆開再送進模型，見 core/special_tokens.py
from core.special_tokens import sanitize_special  # noqa: E402,F401


def local_llm(system: str, user: str, max_new_tokens: int = 768) -> Optional[str]:
    """本地模型伺服器。連不上或逾時回 None。user 會先拆開假冒的特殊標記（system 是程式寫的，不處理）。"""
    import requests
    user = sanitize_special(user)
    try:
        if ENGINE == "llamacpp":
            # 與 11-18 驗證用的送法完全相同（experiments/model_select/reason/run_pipeline_llamacpp.py）：
            # 關掉思考、貪婪解碼、關掉快取（開著時輸出不固定）
            r = requests.post(f"{LOCAL_URL}/v1/chat/completions", timeout=LOCAL_TIMEOUT, json={
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "max_tokens": max_new_tokens, "temperature": 0, "cache_prompt": False,
                "chat_template_kwargs": {"enable_thinking": False}})
        else:
            r = requests.post(f"{LOCAL_URL}/generate", timeout=LOCAL_TIMEOUT,
                              json={"system": system, "user": user, "max_new_tokens": max_new_tokens})
        if r.status_code != 200:
            print(f"[pipeline] 本地模型回應 {r.status_code}：{r.text[:200]}")
            return None
        j = r.json()
        return j["choices"][0]["message"]["content"] if ENGINE == "llamacpp" else j.get("text")
    except Exception as e:
        print(f"[pipeline] 本地模型連不上或回應格式不對（{LOCAL_URL}）：{e}")
        return None


def local_available() -> bool:
    import requests
    try:
        j = requests.get(f"{LOCAL_URL}/health", timeout=3).json()
        return j.get("status") == "ok" if ENGINE == "llamacpp" else j.get("loaded") is True
    except Exception:
        return False


def gemini_llm(system: str, user: str) -> Optional[str]:
    """Gemini（有 key 輪換、被截斷回 None）。沒裝套件或沒有 key 回 None。"""
    try:
        from core.gemini_client import _generate, is_available
        if not is_available():
            return None
        return _generate(system, user, "多代理正規化")
    except Exception as e:
        print(f"[pipeline] Gemini 失敗：{e}")
        return None


def extract_json(text: Optional[str]):
    """取第一個完整的 {...}（略過思考區塊與 markdown）。解析不了回 None。"""
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    start = text.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            c = text[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)
    return None
