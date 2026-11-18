"""
RAG 資料產生（2026-11-15）：用 Gemini 產生 C2 情境範例與開發集。

  python scripts/rag/gen_data.py examples   # 依軟體目錄的用途，每個軟體改寫 4 句新手說法 → rag/knowledge/examples.jsonl
  python scripts/rag/gen_data.py dev        # 開發集 200 題（10 批 × 20，每批換身分與情境）→ docs/bench_blind/dev_scenario.jsonl

模型用 gemini-pro-latest（與系統輸入正規化用的 flash-lite 不同）。開發集可以給開發端閱讀，
但不能放進知識庫；盲測集（blind_scenario.jsonl）不由這支程式產生，也不會被讀取。
"""
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODEL = os.environ.get("RAG_GEN_MODEL", "gemini-pro-latest")
_client = None


def _get_client():
    global _client
    if _client is None:
        from dotenv import load_dotenv
        from google import genai
        load_dotenv(ROOT / ".env")
        keys = [k.strip() for k in (os.environ.get("GEMINI_API_KEYS") or os.environ.get("GEMINI_API_KEY") or "").split(",") if k.strip()]
        if not keys:
            raise RuntimeError("找不到 Gemini API key（.env 的 GEMINI_API_KEYS）")
        _client = genai.Client(api_key=keys[0])
    return _client


def ask_list(prompt: str, temperature: float = 1.0, retries: int = 4):
    """回傳 JSON 陣列；失敗回 []。"""
    from google.genai import types
    cfg = types.GenerateContentConfig(temperature=temperature, response_mime_type="application/json",
                                      max_output_tokens=32768)
    for i in range(retries):
        try:
            resp = _get_client().models.generate_content(model=MODEL, contents=prompt, config=cfg)
            text = (resp.text or "").strip()
            m = re.search(r"\[.*\]", text, re.S)
            data = json.loads(m.group(0) if m else text)
            if isinstance(data, list):
                return data
        except Exception as e:  # noqa: BLE001
            print(f"  [gemini] 第 {i + 1} 次失敗：{type(e).__name__}: {str(e)[:120]}", flush=True)
            time.sleep(8 * (i + 1))
    return []


NEED_CODES = ["persistent_storage", "scheduled_job", "background_no_expose", "custom_command", "credentials",
              "env_config", "external_port", "extra_ports", "scale_by_users", "high_availability", "short_lived",
              "resource_hint", "multi_service"]

EX_PROMPT = """你要替一個「用一句話部署服務到 Kubernetes」的系統產生訓練用的檢索範例。
下面是幾個軟體與它們的用途。請替「每一個軟體」寫 4 句完全不懂技術的新手可能會說的話，要求：
- 繁體中文（台灣用語），不可出現英文、軟體名稱、映像檔、連接埠、記憶體等技術詞；只描述情境與用途。
- 4 句的說法、長短、語氣都要不同（有的很短、有的囉嗦、有的帶情緒、有的夾雜無關的話）。
- 可以順帶暗示一些額外需求（例如資料不能不見、每天固定時間執行、不用讓人連、要設密碼、大概多少人用）。

軟體清單：
{items}

需求代碼只能從這些選：{needs}

只輸出 JSON 陣列，每個元素：
{{"text": "新手說的話", "image_id": "軟體 id", "needs": ["暗示到的需求代碼"], "users_concurrent": 同時在線人數或 null}}"""

DEV_PERSONAS = [
    ("大學生、社團幹部", "社團活動、課堂作業、比賽"),
    ("研究生、實驗室助理", "實驗室、論文資料、課程助教"),
    ("小店老闆、攤商", "小店、菜單、訂位、會員"),
    ("自由工作者、接案者", "作品集、客戶專案、接案工具"),
    ("家長、長輩、家庭用途", "家庭照片、家庭記帳、社區活動"),
    ("公司新進職員", "部門內部工具、報表、文件"),
    ("公司工程師新人", "監控、測試環境、內部服務"),
    ("學校老師、行政人員", "班級、招生、校內活動"),
    ("活動或展覽主辦人", "展覽、研討會、報名、投票"),
    ("混合以上身分", "前面較少出現的服務類型"),
]

DEV_PROMPT = """我在做研究實驗：測試一個「用一句話把服務部署到 Kubernetes」的系統能否從使用者描述推理出正確的部署設定。
請扮演出題者，寫 20 題「完全不懂 Kubernetes、Docker 的新手」可能會說的話。
本批使用者身分：{persona}；情境重點：{scenario}。

【規則】
1. 全部繁體中文（台灣用語），題目文字不可出現英文單字或縮寫。
2. 每題都是情境描述；不可直接說出映像檔、副本數、連接埠、記憶體、處理器等設定值；最多 2 題可順口提到軟體的中文俗稱。
3. 關鍵資訊（軟體類型、人數、要不要保存資料、要不要定時、要不要對外、密碼）分散在句首、句中、句尾、拆在多句或只用暗示；數字寫法多樣（兩三百人、快一千、十幾個、半天）。
4. 問法多樣：流水帳、很短、夾雜閒聊或抱怨、前後矛盾、帶情緒、用錯名詞、一次要兩件事、錯字或注音文、不合理要求、問句形式；每種至少 1 題。
5. 難易：簡單 5、中等 9、困難 6（困難至少 2 題是資訊不足、正確做法是反問）。
6. 服務類型分散（網站、資料庫、檔案分享、維基、程式碼託管、論壇、部落格、定時工作、背景程式、快取、訊息佇列、監控、表單），至少 8 種。
7. 正確答案必須是真實存在的 Docker 映像檔；數字合理。
8. 不可與下列已出過的題目相似：{avoid}

只輸出 JSON 陣列，20 個元素，每個：
{{"id":"D###","difficulty":"簡單|中等|困難","persona":"","scenario":"","style":"","info_position":"","text":"使用者說的話",
 "expected":{{"image":{{"accept":["可接受的映像檔名稱，不含版本"],"must_ask":false}},"app_name":{{"hint":"","must_ask":false}},
 "replicas":{{"min":1,"max":3}},"port":{{"accept":[80]}},"memory":{{"min":"128Mi","max":"1Gi"}},"cpu":{{"min":"100m","max":"1000m"}}}},
 "extra":[{{"need":"需求代碼，只能用：{needs}","detail":"具體內容","why":"原句哪裡暗示"}}],
 "must_ask":["必須反問的事項，沒有就空陣列"],"traps":"","pass_if":""}}"""


def gen_examples():
    cat = json.load(open(ROOT / "rag/knowledge/catalog.json", encoding="utf-8"))["entries"]
    out = ROOT / "rag/knowledge/examples.jsonl"
    rows = []
    for i in range(0, len(cat), 6):
        batch = cat[i:i + 6]
        items = "\n".join(f"- id={e['id']}｜類型={e['category']}｜用途：{'、'.join(e['scenarios'])}" for e in batch)
        got = ask_list(EX_PROMPT.format(items=items, needs=", ".join(NEED_CODES)))
        ids = {e["id"] for e in batch}
        ok = [r for r in got if isinstance(r, dict) and r.get("image_id") in ids and r.get("text")]
        for r in ok:
            r["needs"] = [n for n in (r.get("needs") or []) if n in NEED_CODES]
            r["source"] = f"gemini:{MODEL}"
        rows += ok
        print(f"  範例批次 {i // 6 + 1}：{len(ok)} 句", flush=True)
    out.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
    print(f"情境範例 {len(rows)} 句 → {out}")


def gen_dev():
    out = ROOT / "docs/bench_blind/dev_scenario.jsonl"
    rows = []
    for b, (persona, scenario) in enumerate(DEV_PERSONAS):
        avoid = "；".join(r["text"][:30] for r in rows[-40:]) or "（無）"
        got = ask_list(DEV_PROMPT.format(persona=persona, scenario=scenario, avoid=avoid, needs=", ".join(NEED_CODES)))
        ok = [r for r in got if isinstance(r, dict) and r.get("text") and isinstance(r.get("expected"), dict)]
        for j, r in enumerate(ok):
            r["id"] = f"D{len(rows) + j + 1:03d}"
            r["source"] = f"gemini:{MODEL}"
        rows += ok
        print(f"  開發集批次 {b + 1}/{len(DEV_PERSONAS)}：{len(ok)} 題（累計 {len(rows)}）", flush=True)
        out.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
    print(f"開發集 {len(rows)} 題 → {out}")


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "examples"
    {"examples": gen_examples, "dev": gen_dev}[what]()
