"""
個人化部署建議（2026-10-23，柏碩清單第 3 項；決策見 docs/handoff/claude-memory/2026-10-07-personalization-decision.md）。

全部是固定規則與統計，不經過 AI、不新增模型：
1. 冷啟動：使用者部署少於 3 次時，找他目前的部署跟公開語料統計出的哪一種典型風格最像（標準化後的歐氏距離，
   風格來自 scripts/build_style_priors.py 產出的 agents/style_priors.json），用那一類常見的設定當建議。
2. 部署 3 次以上：直接用他自己最近 5 次部署的中位數（2026-10-25 改）。原本是「風格值 ×（1 − w）＋ 個人中位數 × w」、
   w = n ／（n ＋ 5）、取全部紀錄的中位數；模擬實測（docs/sim_personalize_20261025.md）每次都開 1Gi 的人，部署 10 次後
   只被建議 704Mi，改用 2Gi 之後連續 5 次仍被建議 192Mi。公開語料三種風格的數字又幾乎一樣，混進去只會把建議拉回預設值。
3. 只建議副本數、每個副本的記憶體、CPU；只用來預填部署單，使用者看得到、可以改，之後照常審查。
"""
import json
import math
import os
import re
from statistics import median
from typing import Dict, List, Optional

PRIORS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "style_priors.json")
MIN_PERSONAL = 3
RECENT = 5          # 部署 3 次以上時，看最近幾次的習慣（換習慣後幾次就跟上）
# 「部署一個 nginx」的「一個」只是量詞，不算講了副本數（2026-10-25 模擬實測：原本這種句子永遠不會得到副本數建議）；
# 「一個副本」「one pod」這種明確講到副本的才算。
_ONE_RE = re.compile(r"^(?:1|一|one)$", re.I)
_REPLICA_UNIT_RE = re.compile(r"pods?|replicas?|副本|instances?", re.I)


def _states_pods(text: str) -> bool:
    for m in re.finditer(r"(\d+|[一二兩三四五六七八九十百]+|one|two|three|four|five|six|seven|eight|nine|ten)\s*(個\s*副本|個|份|台|pods?|replicas?|副本|instances?)",
                         text or "", re.I):
        if _ONE_RE.match(m.group(1)) and not _REPLICA_UNIT_RE.search(m.group(2)):
            continue
        return True
    return False


_MEM_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(Ki|Mi|Gi|K|M|G)?\s*$")
_MEM_MIB = {"Ki": 1 / 1024, "Mi": 1, "Gi": 1024, "K": 1000 / 1024 ** 2, "M": 1000 ** 2 / 1024 ** 2, "G": 1000 ** 3 / 1024 ** 2, None: 1 / 1024 ** 2}

_priors_cache: Dict[str, Dict] = {}


def load_priors(path: str = None) -> Optional[Dict]:
    path = path or PRIORS_PATH
    if path not in _priors_cache:
        try:
            with open(path, encoding="utf-8") as f:
                _priors_cache[path] = json.load(f)
        except (OSError, ValueError):
            return None
    return _priors_cache[path]


def mem_mib(v) -> Optional[float]:
    m = _MEM_RE.match(str(v or ""))
    return float(m.group(1)) * _MEM_MIB[m.group(2)] if m else None


def cpu_m(v) -> Optional[float]:
    s = str(v or "").strip()
    try:
        return float(s[:-1]) if s.endswith("m") else float(s) * 1000 if s else None
    except ValueError:
        return None


def _fmt_mem(mib: float) -> str:
    mib = max(64, int(round(mib / 64.0)) * 64)          # 取 64Mi 的倍數，看起來像人會寫的數字
    return f"{mib // 1024}Gi" if mib % 1024 == 0 else f"{mib}Mi"


def _fmt_cpu(m: float) -> str:
    return f"{max(50, int(round(m / 50.0)) * 50)}m"


def _pinned(image: str) -> bool:
    last = str(image or "").rsplit("/", 1)[-1]
    return ":" in last and not last.endswith(":latest")


def nearest_style(history: List[Dict], priors: Dict) -> Optional[Dict]:
    """使用者的部署 → 最像的風格。平台部署一律有健康檢查，所以這一項固定是 1。"""
    if not history or not priors or not priors.get("styles"):
        return None
    pods = median([max(0, int(h.get("pods") or 1)) for h in history])
    has_res = sum(1 for h in history if h.get("memory") or h.get("cpu")) / len(history)
    pinned = sum(1 for h in history if _pinned(h.get("image"))) / len(history)
    x = [math.log2(1 + pods), has_res, 1.0, pinned]
    mu, sd = priors["feature_mean"], priors["feature_std"]
    z = [(a - b) / (c or 1) for a, b, c in zip(x, mu, sd)]
    # 第 3 個特徵「有健康檢查」在平台上固定是 1，對分辨使用者沒有資訊，卻會把每個人都拉向「正式環境型」
    # （2026-10-25 模擬實測：只用 latest、最低配置的使用者也被分成正式環境型），算距離時不計入
    keep = [0, 1, 3]
    return min(priors["styles"], key=lambda s: sum((z[i] - s["center"][i]) ** 2 for i in keep))


def suggest(history: List[Dict], priors: Optional[Dict], text: str = "") -> Dict:
    """history：自己最近的成功部署 [{pods, memory, cpu, image}]（新的在前或後都可以）。
    回傳 {"ok": True, "pods": n|None, "memory": "256Mi"|None, "cpu": "100m"|None, "reason": "...", "style": "..."}，
    或 {"ok": False, "reason": "..."}。句子裡講了副本數就不建議副本數。"""
    history = [h for h in (history or []) if isinstance(h, dict)][:50]
    if not history:
        return {"ok": False, "reason": "還沒有部署紀錄，沒有可以參考的習慣。"}
    style = nearest_style(history, priors)
    n = len(history)
    personal = n >= MIN_PERSONAL
    recent = history[:RECENT]          # 呼叫端傳入時新的在前

    def pick(style_v, vals):
        vals = [v for v in vals if v is not None]
        if personal and vals:
            return median(vals)
        return style_v if style_v is not None else (median(vals) if vals else None)

    pods = pick(style.get("replicas_median") if style else None, [h.get("pods") for h in recent]) if personal else None
    mem = pick(style.get("mem_mib_median") if style else None, [mem_mib(h.get("memory")) for h in (recent if personal else history)])
    cpu = pick(style.get("cpu_m_median") if style else None, [cpu_m(h.get("cpu")) for h in (recent if personal else history)])
    pods_n = max(1, int(round(pods))) if pods is not None else None
    out = {"ok": True, "n": n, "style": style["label"] if style else None,
           # 副本數只在有 3 次以上自己的紀錄時才建議：公開語料三種風格的副本數中位數都是 1（2026-10-23 實際統計），冷啟動建議沒有意義
           "pods": None if _states_pods(text) else pods_n,
           "memory": _fmt_mem(mem) if mem else None, "cpu": _fmt_cpu(cpu) if cpu else None}
    if personal:
        basis = f"依你過去 {n} 次部署的習慣" if n <= RECENT else f"依你過去 {n} 次部署中最近 {RECENT} 次的習慣"
        out["reason"] = f"{basis}（大多開 {pods_n} 個副本）" if pods_n else basis
    elif style:
        out["reason"] = f"你的部署紀錄還不到 {MIN_PERSONAL} 次，先用公開專案裡「{style['label']}」常見的設定"
    else:
        out["reason"] = f"依你過去 {n} 次部署"
    if not (out["pods"] or out["memory"] or out["cpu"]):
        return {"ok": False, "reason": "沒有可以建議的欄位。"}
    return out
