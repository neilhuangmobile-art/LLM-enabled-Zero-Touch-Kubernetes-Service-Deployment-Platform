"""第二次實驗用的第三方模型（2026-11-07）：盲測的前置設定與評分都交給它，不由開發端（Claude）判斷。

模型：預設 gemini-pro-latest，可用環境變數 BENCH_JUDGE_MODEL 換。刻意跟出題用的模型（argon）
以及 ZeroTouch 翻譯層用的 gemini-3.5-flash-lite 不同。每次呼叫實際用到的模型名稱都會記錄。
"""
import json, os, re, time
from pathlib import Path

JUDGE_MODEL = os.environ.get("BENCH_JUDGE_MODEL", "gemini-pro-latest")
_client = None


def _get_client():
    global _client
    if _client is None:
        from dotenv import load_dotenv
        from google import genai
        load_dotenv(Path(__file__).resolve().parents[2] / ".env")
        keys = [k.strip() for k in (os.environ.get("GEMINI_API_KEYS") or os.environ.get("GEMINI_API_KEY") or "").split(",")
                if k.strip()]
        if not keys:
            raise RuntimeError("找不到 Gemini API key（.env 的 GEMINI_API_KEYS）")
        _client = genai.Client(api_key=keys[0])
    return _client


def ask_json(prompt: str, retries: int = 4):
    """送出提示，回傳解析好的 JSON；失敗回 None。溫度 0，讓同一份輸入盡量得到同一個判斷。"""
    from google.genai import types
    cfg = types.GenerateContentConfig(temperature=0, response_mime_type="application/json")
    for i in range(retries):
        try:
            resp = _get_client().models.generate_content(model=JUDGE_MODEL, contents=prompt, config=cfg)
            text = (resp.text or "").strip()
            m = re.search(r"\{.*\}", text, re.S)
            return json.loads(m.group(0) if m else text)
        except Exception as e:  # noqa: BLE001
            if i == retries - 1:
                print(f"[judge] 失敗：{type(e).__name__}: {str(e)[:120]}", flush=True)
                return None
            time.sleep(5 * (i + 1))
