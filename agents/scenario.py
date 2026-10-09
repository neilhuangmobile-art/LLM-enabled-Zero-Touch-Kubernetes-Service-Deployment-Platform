"""
情境解析部署：使用者描述「情境」（多少人用、用幾天、重不重要），換算成部署規格。

分工原則（防幻覺）：
- 數字（人數、天數）一律從原句用固定規則抽出來；Gemini 只補語意判斷（重要程度、
  要不要保存資料），它給的數字必須在原句裡找得到才採用。
- 副本數、資源、省錢金額全部由這個檔案的確定性規則計算，模型不決定任何數字。
- 每一個推算都附上理由，部署單上讓使用者看得到、改得掉。

換算用的經驗值集中在檔案開頭，口試或使用者要調整時只改這裡。
"""

import math
import re
from typing import Dict, List, Optional

from agents.cost_agent import _detect_app_type, _estimate_monthly_cost, recommended_resources

# ── 換算用的經驗值 ─────────────────────────────────────────────────
# 只知道總人數、不知道同時在線人數時，假設尖峰時段約兩成的人同時在用。
# 報名、投票這類活動的流量集中在開放後的短時間內，兩成是偏保守的估計。
PEAK_RATIO = 0.2
# 每個副本大約能同時服務多少人（依映像檔類型）。None 表示這類服務不能靠加副本擴充。
PER_REPLICA_USERS = {
    "web": 200,       # 靜態網頁伺服器，單一副本可以服務很多人
    "python": 100,    # 動態網頁程式
    "java": 150,
    "worker": 100,
    "default": 100,
    "llm": 5,         # 模型推論非常吃資源
    "database": None, # 資料庫多副本要另外設定複寫，不能直接加副本
}
HIGH_CRITICALITY_MIN = 2   # 重要的服務至少 2 個副本，一個壞了另一個還能撐住
MAX_SCENARIO_REPLICAS = 10 # 情境推算最多建議 10 個，再多就請使用者自己確認
MONTH_DAYS = 30

# ── 數字解析 ───────────────────────────────────────────────────────
_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "兩": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000, "萬": 10000, "万": 10000}
_NUM = r"(\d[\d,]*(?:\.\d+)?\s*[kK千百萬万]?|[零〇一二兩两三四五六七八九十百千萬万]+)"


def _to_int(tok: str) -> Optional[int]:
    """「500」「1,000」「1.5k」「5百」「兩百」「三十」「一萬二」都轉成整數。"""
    tok = (tok or "").strip().replace(",", "").replace(" ", "")
    if not tok:
        return None
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([kK千百萬万]?)", tok)
    if m:
        mult = {"": 1, "k": 1000, "K": 1000, "千": 1000, "百": 100, "萬": 10000, "万": 10000}[m.group(2)]
        return int(round(float(m.group(1)) * mult))
    total, section, num, last_unit = 0, 0, 0, 0
    for ch in tok:
        if ch in _CN_DIGITS:
            num = _CN_DIGITS[ch]
        elif ch in _CN_UNITS:
            unit = _CN_UNITS[ch]
            if unit == 10000:
                total += (section + num) * unit
                section, num = 0, 0
            else:
                section += (num or 1) * unit
                num = 0
            last_unit = unit
        else:
            return None
    # 口語省略最後的單位：「兩百五」是 250、「一萬二」是 12000
    if num and last_unit >= 100 and tok[-1] in _CN_DIGITS and tok[-2] in _CN_UNITS:
        num *= last_unit // 10
    return total + section + num or None


_PEOPLE = (r"(?:多|餘|左右)?\s*(?:個|位|名)?\s*(?:人|使用者|用戶|用户|同學|同学|學生|学生|會員|会员|"
           r"客人|訪客|访客|參加者|参加者|觀眾|观众|員工|员工|people|users?|visitors?|attendees?|"
           r"participants?|students?|members?)")
_USERS_RE = re.compile(_NUM + r"\s*" + _PEOPLE, re.IGNORECASE)
_CONCURRENT_HINT = re.compile(r"同時|同时|同一時間|一起上線|在線|在线|concurrent|simultaneous|at the same time|at once",
                              re.IGNORECASE)
_DUR_UNITS = [
    (r"(?:小時|小时|hours?|hrs?)", 1 / 24),
    (r"(?:天|日|days?)", 1),
    (r"(?:週|周|星期|禮拜|礼拜|weeks?)", 7),
    (r"(?:個月|个月|months?)", 30),
]
_DUR_RE = [(re.compile(_NUM + r"\s*" + u, re.IGNORECASE), f) for u, f in _DUR_UNITS]

_HIGH_RE = re.compile(r"很重要|非常重要|不能掛|不能挂|不可以掛|不能停|不能斷|不能当|不能當|正式上線|正式环境|正式環境|"
                      r"上線用|商業|商业|付費|付费|客戶在用|高可用|production|prod\b|critical|mission[- ]critical|"
                      r"must not go down|can't go down|high availability", re.IGNORECASE)
_LOW_RE = re.compile(r"測試|测试|試試|试试|玩玩|練習|练习|實驗|实验|自己用|隨便|随便|\bdemo\b|\btest(?:ing)?\b|"
                     r"just trying|playground|for fun", re.IGNORECASE)
_PERSIST_RE = re.compile(r"報名|报名|註冊|注册|訂單|订单|投票|留言|會員資料|会员资料|填表|表單|表单|問卷|问卷|"
                         r"存資料|存资料|保存|紀錄|记录|上傳|上传|sign[- ]?up|registration|orders?|votes?|"
                         r"database|store data|save data|form", re.IGNORECASE)


def _numbers_in(text: str) -> set:
    """原句裡出現的所有數字（含中文數字、天數換算），用來核對 Gemini 給的數字有沒有根據。"""
    out = set()
    for m in re.finditer(_NUM, text or ""):
        n = _to_int(m.group(1))
        if n is not None:
            out.update({n, n * 7, n * 30})
    return out


def extract_by_rules(text: str) -> Dict:
    """用固定規則抽出情境屬性；抽不到的欄位是 None。"""
    text = text or ""
    total = concurrent = None
    for m in _USERS_RE.finditer(text):
        n = _to_int(m.group(1))
        if not n:
            continue
        around = text[max(0, m.start() - 8): m.end() + 6]
        if _CONCURRENT_HINT.search(around):
            concurrent = concurrent or n
        else:
            total = total or n

    duration = None
    for rx, factor in _DUR_RE:
        m = rx.search(text)
        if m:
            n = _to_int(m.group(1))
            if n:
                duration = round(n * factor, 2)
                break

    criticality = "high" if _HIGH_RE.search(text) else ("low" if _LOW_RE.search(text) else None)
    persistence = True if _PERSIST_RE.search(text) else None
    return {"total_users": total, "concurrent_users": concurrent, "duration_days": duration,
            "criticality": criticality, "needs_persistence": persistence}


def merge_model_attrs(text: str, rules: Dict, model: Optional[Dict]) -> Dict:
    """規則抽到的優先；模型只補空缺，而且數字要在原句裡找得到才採用。"""
    out = dict(rules)
    out["sources"] = {k: "rule" for k, v in rules.items() if v is not None}
    if not isinstance(model, dict):
        return out
    nums = _numbers_in(text)
    for key in ("total_users", "concurrent_users", "duration_days"):
        if out.get(key) is not None:
            continue
        try:
            v = float(model.get(key))
        except (TypeError, ValueError):
            continue
        if v <= 0:
            continue
        # 小於一天的時數（例如 0.5 天）不好核對，直接只接受整數且原句出現過的數字
        if key == "duration_days":
            ok = round(v) in nums or round(v * 24) in nums
        else:
            ok = int(v) in nums
        if ok:
            out[key] = round(v, 2) if key == "duration_days" else int(v)
            out["sources"][key] = "model"
    if out.get("criticality") is None and model.get("criticality") in ("high", "normal", "low"):
        out["criticality"] = model["criticality"]
        out["sources"]["criticality"] = "model"
    if out.get("needs_persistence") is None and isinstance(model.get("needs_persistence"), bool):
        out["needs_persistence"] = model["needs_persistence"]
        out["sources"]["needs_persistence"] = "model"
    return out


_EXPLICIT_REPLICA_RES = [
    re.compile(_NUM + r"\s*(?:個|个|份)?\s*(?:副本|實例|实例|pods?\b|replicas?\b|instances?\b)", re.IGNORECASE),
    re.compile(r"(?:副本|replicas?|pods?)\s*(?:數|数)?\s*(?:=|:|：|為|为|是|設|设|開|开)?\s*" + _NUM, re.IGNORECASE),
    # 「起兩個 redis」：兩個以上 + 映像檔名稱才算；「一個 nginx」多半只是「一個服務」的意思
    re.compile(_NUM + r"\s*(?:個|个|份)\s*(?=[A-Za-z])"),
]


def explicit_replicas(text: str) -> Optional[int]:
    """使用者有沒有明講副本數。比部署解析器的判斷嚴格：「一個報名網站」的「一個」不算。"""
    for i, rx in enumerate(_EXPLICIT_REPLICA_RES):
        m = rx.search(text or "")
        if m:
            n = _to_int(m.group(1))
            if n and (i < 2 or n >= 2):
                return n
    return None


def has_scenario(attrs: Dict) -> bool:
    return any(attrs.get(k) is not None for k in
               ("total_users", "concurrent_users", "duration_days", "criticality"))


# ── 換算 ───────────────────────────────────────────────────────────
def _monthly_usd(replicas: int, res: Dict) -> float:
    """沿用成本代理的計價公式（依 requests 計算），不另外發明一套。"""
    manifest = {"spec": {"replicas": replicas, "template": {"spec": {"containers": [
        {"name": "app", "resources": res}]}}}}
    return _estimate_monthly_cost(manifest)["estimated_usd"]


def plan(attrs: Dict, image: str, app_name: str = "", explicit_replicas: Optional[int] = None,
         memory: str = None, cpu: str = None) -> Dict:
    """把情境屬性換算成副本數，附上推算理由與省錢估算。

    explicit_replicas：使用者句子裡明講的副本數；有講就照使用者的，這裡只提供建議。
    """
    app_type = _detect_app_type(image or "", app_name or "")
    per = PER_REPLICA_USERS.get(app_type, PER_REPLICA_USERS["default"])
    crit = attrs.get("criticality")
    total, conc, days = attrs.get("total_users"), attrs.get("concurrent_users"), attrs.get("duration_days")
    reasons: List[str] = []
    notes: List[str] = []

    if conc is None and total:
        conc = max(1, math.ceil(total * PEAK_RATIO))
        reasons.append(f"約 {total} 人使用，尖峰時段估計約兩成同時在線，也就是 {conc} 人")
    elif conc:
        reasons.append(f"同時在線約 {conc} 人")

    replicas = 1
    if per is None:
        reasons.append("資料庫類服務不能直接加副本分擔流量，維持 1 個副本")
        if crit == "high":
            notes.append("資料庫要做到不中斷，需要另外設定主從複寫，這個平台目前不會自動設定")
    else:
        if conc:
            replicas = max(1, math.ceil(conc / per))
            reasons.append(f"這類服務每個副本約可服務 {per} 人同時使用，所以需要 {replicas} 個副本")
        if crit == "high" and replicas < HIGH_CRITICALITY_MIN:
            replicas = HIGH_CRITICALITY_MIN
            reasons.append(f"你提到這個服務很重要，至少開 {HIGH_CRITICALITY_MIN} 個副本，一個出問題另一個還能撐住")
        elif crit == "low" and not conc:
            reasons.append("測試用途，1 個副本就夠")
        if replicas > MAX_SCENARIO_REPLICAS:
            notes.append(f"推算需要 {replicas} 個副本，超過建議上限，先開 {MAX_SCENARIO_REPLICAS} 個，上線後再依實際流量調整")
            replicas = MAX_SCENARIO_REPLICAS

    applied = explicit_replicas is None
    if not applied:
        if explicit_replicas != replicas:
            notes.append(f"依情境推算建議 {replicas} 個副本，你指定了 {explicit_replicas} 個，照你指定的部署")
        replicas = explicit_replicas

    if attrs.get("needs_persistence"):
        notes.append("你的服務看起來需要保存資料，例如報名或投票紀錄。目前部署的容器重新啟動後資料不會保留，"
                     "資料請存到另外的資料庫服務")
    if days and days < MONTH_DAYS:
        d = int(days) if float(days).is_integer() else days
        notes.append(f"這個服務只需要用 {d} 天，活動結束後記得跟我說「刪除 {app_name or '這個服務'}」，"
                     "不用的服務繼續開著會一直佔用資源")

    # ── 省錢估算：每一項都寫清楚跟什麼比 ──
    res = recommended_resources(image, app_name, memory, cpu)
    rec_month = _monthly_usd(replicas, res)
    # 比較基準：沒考慮情境時的作法＝照總人數開副本、整個月開著。
    # 兩項省下的金額加起來剛好是「基準月費 − 照情境推算的費用」，不會重複計算。
    comparisons = []
    baseline = rec_month
    if total and attrs.get("concurrent_users") is None and per:
        naive = min(MAX_SCENARIO_REPLICAS, max(1, math.ceil(total / per)))
        if naive > replicas:
            baseline = _monthly_usd(naive, res)
            comparisons.append({"label": f"如果把 {total} 人都當成同時在線，要開 {naive} 個副本",
                                "usd": round(baseline - rec_month, 2)})
    span = min(days, MONTH_DAYS) / MONTH_DAYS if days else 1
    if days and days < MONTH_DAYS:
        saved = rec_month * (1 - span)
        comparisons.append({"label": f"用完 {int(days) if float(days).is_integer() else days} 天就刪除，不放著一整個月",
                            "usd": round(saved, 2)})
    savings = None
    if comparisons:
        savings = {
            "items": comparisons,
            "total_usd": round(baseline - rec_month * span, 2),
            "recommended_monthly_usd": round(rec_month, 2),
            "baseline_usd": round(baseline, 2),
            "scenario_usd": round(rec_month * span, 2),
            "basis": "依公有雲（AWS）牌價估算每月費用。放在你自己的電腦或學校叢集不另外收費，省下的是電腦資源",
        }

    return {
        "applies": True,
        "attrs": {k: attrs.get(k) for k in ("total_users", "concurrent_users", "duration_days",
                                            "criticality", "needs_persistence")},
        "sources": attrs.get("sources", {}),
        "app_type": app_type,
        "replicas": replicas,
        "replicas_applied": applied,
        "reasons": reasons,
        "notes": notes,
        "savings": savings,
    }
