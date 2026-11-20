"""第 2 步：意圖檢查②——使用者講了、但中間 JSON 還沒寫進去的要求。

本地模型拿原句和目前的 JSON 比對，列出漏掉的項目；每一項的 evidence 要真的出現在原句，
才交給推理代理加進額外欄位。最多 3 輪；第 3 輪之後還有新的漏項，就變成問題問使用者。"""
from __future__ import annotations

import json
import time
from typing import Dict, Optional

from agents.pipeline import schema
from agents.pipeline.llm import LLM, extract_json
from agents.pipeline.normalize import add_extra
from agents.pipeline.prompts import INTENT_SYSTEM

MAX_ROUNDS = 3


def _summary(spec: Dict) -> str:
    """給模型看的目前狀態（不含 trace 之類的內部欄位）。"""
    return json.dumps({
        "base": {f: spec["base"][f]["value"] for f in schema.BASE_FIELDS},
        "extras": [{"kind": e["kind"], "desc": e["desc"], "evidence": e["evidence"]} for e in spec["extras"]],
    }, ensure_ascii=False)


def run(spec: Dict, local: Optional[LLM]) -> None:
    if local is None:
        spec["trace"].append({"step": "intent", "ms": 0, "detail": "沒有本地模型，跳過"})
        return
    leftover = []
    for rnd in range(1, MAX_ROUNDS + 1):
        spec["attempts"]["intent"] = rnd
        t0 = time.time()
        raw = local(INTENT_SYSTEM, f"User text:\n{spec['text']}\n\nExtracted JSON:\n{_summary(spec)}")
        data = extract_json(raw)
        items = data.get("missing") if isinstance(data, dict) and isinstance(data.get("missing"), list) else None
        ms = int((time.time() - t0) * 1000)
        if items is None:
            spec["trace"].append({"step": "intent", "ms": ms, "detail": f"第 {rnd} 輪輸出解析失敗"})
            continue
        added = [it for it in items if add_extra(spec, it, source="user")]
        spec["trace"].append({"step": "intent", "ms": ms, "detail": f"第 {rnd} 輪找到 {len(items)} 項，新增 {len(added)} 項"})
        if not added:          # 沒有新的漏項（或模型列的都已經寫進去了）就結束
            return
        leftover = added
    # 3 輪都還在找到新東西：最後一輪加的項目請使用者確認，避免無止境地加下去
    for it in leftover:
        # 2026-11-18：不再拿句子片段反問（「你提到『記得加密碼』，我不確定理解得對不對」），直接採用原本的推薦值
        # 「照我說的做」，部署單上寫明，使用者可以拿掉。PIPELINE_ASK_POLICY=all 恢復原本做法（對照用）。
        if schema.ask_policy() != "all":
            ev = str(it.get("evidence") or "").strip()
            if ev:   # 部署單上有「已套用／注意到但沒處理」清單，這裡只提醒使用者去確認，不說可以在卡片上拿掉（沒有那個按鈕）
                schema.add_notice(spec, f"你提到「{ev[:60]}」，系統已盡量照你說的設定，請確認部署單是否符合")
            continue
        schema.add_question(spec, f"extra:{it.get('evidence', '')[:30]}",
                            f"你提到「{it.get('evidence', '')}」，我不確定理解得對不對，這部分要怎麼處理？",
                            options=[{"value": "keep", "label": "照我說的做"}, {"value": "drop", "label": "不用了"}],
                            recommended="keep", reason="意圖檢查 3 輪後仍有新的漏項")
