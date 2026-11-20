"""部署單每個欄位的信心度（2026-11-18）。

信心度不是模型自己說的機率，而是用有標準答案的開發題組統計出來的「實測正確率」：
- 使用者自己講的欄位：100%
- 映像檔（系統選的）：依選軟體時的模型分數 P(Yes) 分組，各組的實測正確率（模型原始分數偏高，不能直接顯示）
- 其他系統填的欄位（副本數、port、記憶體、CPU）：依「是推理代理推的、還是程式預設值」分組的實測正確率
- 名稱（app_name）沒有對錯，不顯示

統計表在 calibration.json，由 experiments/model_select/reason/calibrate_choose.py 從評估結果產生；
換模型、提示詞、軟體目錄後要重算。沒有統計表時只用下面的映像檔分組（不編造其他欄位的數字）。
"""
from __future__ import annotations

import json
import os
from typing import Dict, Optional

_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "calibration.json")

# 映像檔分組的預設值（dev_scenario 139 題，2026-11-18）：(分數下限, 實測正確率, 題數)
# 最低一組只量過「被採用」的分數（採用門檻 0.8 以上）；低於 0.8 沒有實測，不顯示數字
BINS = ((0.98, 0.86, 29), (0.95, 0.67, 48), (0.8, 0.55, 62))
SOURCE = "dev_scenario 139 題（2026-11-18，Qwen3.5-4B Q8）"

_table: Optional[Dict] = None


def _valid_row(row) -> bool:
    return (isinstance(row, (list, tuple)) and len(row) == 2 and isinstance(row[0], (int, float))
            and 0 <= row[0] <= 1 and isinstance(row[1], int) and row[1] > 0)


def _validate(t: Dict) -> Optional[str]:
    """統計表格式不對就整份停用（回傳原因），不要讓一列壞資料把部署單整個弄成 500。"""
    if not isinstance(t, dict):
        return "最外層不是物件"
    for b in t.get("image_bins") or []:
        if not (isinstance(b, (list, tuple)) and len(b) == 3 and _valid_row(b[1:])):
            return f"image_bins 有一列格式不對：{b}"
    for f, d in (t.get("fields") or {}).items():
        if not isinstance(d, dict):
            return f"fields.{f} 不是物件"
        for src, row in d.items():
            if not _valid_row(row):
                return f"fields.{f}.{src} 格式不對：{row}"
    return None


def _load() -> Dict:
    """讀 calibration.json。找不到或格式不對都會印出原因：信心度數字只剩映像檔的內建分組，其他欄位不顯示。"""
    global _table
    if _table is None:
        try:
            with open(_PATH, encoding="utf-8") as f:
                t = json.load(f)
            why = _validate(t)
            if why:
                print(f"[calibration] {_PATH} {why}，整份停用：部署單只顯示映像檔的內建信心度，其他欄位不顯示數字")
                t = {}
        except FileNotFoundError:
            print(f"[calibration] 找不到 {_PATH}：部署單只顯示映像檔的內建信心度，其他欄位不顯示數字"
                  "（執行 experiments/model_select/reason/calibrate_choose.py 產生）")
            t = {}
        except Exception as e:  # noqa: BLE001
            print(f"[calibration] 讀不到信心度統計表（{e}），整份停用；修好檔案後要重啟網站")
            t = {}
        _table = t
    return _table or {}


def level(pct: int) -> str:
    """顏色等級：綠（可靠）≥85、黃（建議確認）≥65、紅（請確認）。"""
    return "hi" if pct >= 85 else "mid" if pct >= 65 else "lo"


# 系統填的值最高只顯示 95%：100% 留給使用者自己講的；而且統計樣本有限（例如 CPU 在 Claude 題組範圍寬、197 題全對，
# 在 Gemini 題組卻常錯），不宣稱完全可靠
SYSTEM_MAX_PCT = 95


def _pack(acc: float, n: int, source: str) -> Dict:
    pct = min(SYSTEM_MAX_PCT, int(round(acc * 100)))
    return {"pct": pct, "level": level(pct), "n": n, "source": source}


def image_confidence(p: Optional[float]) -> Optional[Dict]:
    if p is None:
        return None
    t = _load()
    # 檔案裡的分組要完整（三組）才用，出處標檔案的；否則全部用內建分組，出處標內建的（不混用、不標錯出處）
    file_bins = t.get("image_bins") or []
    bins, source = ([tuple(b) for b in file_bins], t.get("source") or SOURCE) if len(file_bins) == len(BINS) else (BINS, SOURCE)
    for low, acc, n in bins:
        if p >= low:
            out = _pack(acc, int(n), source)
            out["raw"] = round(float(p), 4)
            return out
    return None


def field_confidence(field: str, source: Optional[str], p: Optional[float] = None) -> Optional[Dict]:
    """field：image／replicas／port／memory／cpu；source：user／inferred／default。算不出來回 None（不顯示）。"""
    if source == "user":
        return {"pct": 100, "level": "hi", "n": 0, "source": "使用者指定"}
    if field == "image":
        return image_confidence(p)
    t = _load()
    row = (t.get("fields") or {}).get(field, {}).get(source or "")
    if not row:
        return None
    acc, n = row
    return _pack(acc, n, t.get("source") or SOURCE)


# 舊介面（問題卡的選項標籤用）
def reliability(p: Optional[float]) -> Optional[Dict]:
    c = image_confidence(p)
    if not c:
        return None
    return {"level": {"hi": "高", "mid": "中", "lo": "低"}[c["level"]], "accuracy": c["pct"] / 100, "n": c["n"],
            "raw": c["raw"], "source": c["source"], "pct": c["pct"]}
