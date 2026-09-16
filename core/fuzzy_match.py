"""
core/fuzzy_match.py
資源名稱模糊比對：把使用者打錯字、只講暱稱/類別的名稱，對應到叢集裡真正存在的
Pod/Deployment 名稱。Chat 的「指代消解」跟「模糊比對真實資源名稱」兩個功能共用
這裡的邏輯。

刻意不依賴 flask/torch（純 stdlib + 選用性 import agents.cost_agent），可以獨立
在 CI 跑 pytest -m "not integration"，不用裝重依賴。也刻意不引入向量化語意搜尋或
新套件——比對用 stdlib difflib 就夠，符合專案「加依賴要謹慎」的慣例（見
requirements.txt 開頭註解）。

比對優先序（分數遞減）：
    1. 完全相符（大小寫不分）
    2. 前綴相符（沿用 web_demo.py _resolve_pods() 現有邏輯）
    3. difflib 字串相似度（抓打字錯誤，例如 web-fronted vs web-frontend）
    4. 類別比對（「那個 web 的」「快取服務」這種類別描述，重用
       agents/cost_agent.py 的 _detect_app_type()，另外補一份中文暱稱表）

呼叫端絕對不要自己再挑一個結果執行——用 pick_confident_match() 判斷夠不夠確定，
不夠確定要把整份候選清單攤開讓使用者選，不要猜。破壞性操作（scale/delete/
rollback）選錯資源的後果比純問答答錯嚴重得多。
"""
import difflib
from typing import Dict, List, Optional

# 中英暱稱 → 類別代碼對照表。這裡的類別代碼刻意跟 agents/cost_agent.py 的
# _detect_app_type() 回傳值（web/java/database/python/llm/worker/default）對齊，
# 但不是同一份規則表：cost_agent 那份只收錄技術關鍵字（postgres/mysql/redis...），
# 這裡額外補使用者實際會講的中文暱稱、以及候選資源名稱裡常見但技術關鍵字表
# 沒收錄的字（例如「cache」——candidate 常常叫 "my-cache" 而不是含 "redis" 字面）。
_CATEGORY_ALIASES: Dict[str, str] = {
    "前端": "web", "網頁": "web", "web": "web", "frontend": "web",
    "快取": "database", "cache": "database", "redis": "database",
    "資料庫": "database", "db": "database", "database": "database",
    "postgres": "database", "mysql": "database", "mongo": "database",
    "訊息佇列": "worker", "queue": "worker", "kafka": "worker", "rabbitmq": "worker",
    "排程": "worker", "scheduler": "worker", "worker": "worker",
}

_TYPO_MIN_RATIO = 0.72
_PREFIX_MIN_QUERY_LEN = 3  # 短於這個長度不做前綴比對，避免「w」這種字命中一大堆不相關候選


def _keyword_category(text: str) -> Optional[str]:
    """純字串層級的關鍵字類別判斷：text 裡有沒有出現任一別名關鍵字。找不到回 None
    （代表沒有任何類別線索，不是「比對過但沒中」的意思，呼叫端要能區分這兩種情況）。
    """
    low = (text or "").lower()
    for alias, category in _CATEGORY_ALIASES.items():
        if alias in low:
            return category
    return None


def _guess_category(text: str) -> Optional[str]:
    """猜測一段文字在講哪個服務類別。先查中文暱稱表，查不到才交給
    agents.cost_agent._detect_app_type()（那份只認技術關鍵字，是後備）。
    agents 模組不可用時（理論上不會，但避免硬相依）靜默跳過，回 None。
    """
    category = _keyword_category(text)
    if category:
        return category
    try:
        from agents.cost_agent import _detect_app_type
        guess = _detect_app_type(text, text)
        return guess if guess != "default" else None
    except Exception:
        return None


def fuzzy_match_resource(query: str, candidates: List[str]) -> List[dict]:
    """把 query（可能是打錯字的名稱、暱稱、或類別描述）跟真實存在的 candidates
    比對，回傳依信心分數（0~1）由高到低排序的候選清單：
        [{"name": str, "score": float, "reason": "exact"|"prefix"|"typo"|"category"}, ...]

    完全比對不到任何東西（含類別線索都沒有）時回傳空清單，不是拋例外。
    """
    query = (query or "").strip()
    if not query or not candidates:
        return []

    query_low = query.lower()
    results: Dict[str, dict] = {}

    for cand in candidates:
        if not cand:
            continue
        cand_low = cand.lower()
        if cand_low == query_low:
            results[cand] = {"name": cand, "score": 1.0, "reason": "exact"}
            continue
        if len(query_low) >= _PREFIX_MIN_QUERY_LEN and cand_low.startswith(query_low):
            results[cand] = {"name": cand, "score": 0.9, "reason": "prefix"}
            continue
        ratio = difflib.SequenceMatcher(None, query_low, cand_low).ratio()
        if ratio >= _TYPO_MIN_RATIO:
            results[cand] = {"name": cand, "score": round(ratio, 3), "reason": "typo"}

    # 前三層（完全/前綴/打字錯誤）都比對不到任何東西，才試類別比對——這是最不精確
    # 的一層，故意分數壓低（0.55，遠低於 pick_confident_match() 預設的 0.85 門檻），
    # 通常只會用來湊出候選清單給使用者選，不會被當成「夠確定可以自動採用」。
    if not results:
        category = _guess_category(query)
        if category:
            for cand in candidates:
                if not cand:
                    continue
                cand_category = _keyword_category(cand)
                if cand_category is None:
                    try:
                        from agents.cost_agent import _detect_app_type
                        cand_category = _detect_app_type(cand, cand)
                    except Exception:
                        cand_category = None
                if cand_category == category:
                    results[cand] = {"name": cand, "score": 0.55, "reason": "category"}

    return sorted(results.values(), key=lambda r: -r["score"])


def pick_confident_match(matches: List[dict], min_score: float = 0.85,
                          min_gap: float = 0.15) -> Optional[dict]:
    """從 fuzzy_match_resource() 的結果判斷能不能安全自動採用最高分那個。

    回傳 None 代表不夠確定：可能是分數本身不夠高，也可能是前兩名分數太接近
    （真的分不出使用者講的是哪一個）。呼叫端這時應該把 matches 整份拿去讓使用者
    選，不要自己再猜一個去執行——尤其是破壞性操作。

    完全相符（reason="exact"）一律直接採用，不跟第二名比分數差距：K8s 的 Pod
    名稱慣例是「Deployment 名稱 + hash 後綴」，只要候選名單同時有一個 Deployment
    跟它自己的 Pod（例如 "cache-service" 跟 "cache-service-7fb8db84-fzqhh"），
    對 "cache-service" 做查詢時，Deployment 本身是 exact（1.0）、它的 Pod 是
    prefix（0.9），差距只有 0.1，會被下面的門檻誤判成「不夠確定」——但使用者
    打的字串跟某個真實資源名稱完全相符，這不是真的有歧義，只是剛好有相關資源
    共享名稱前綴。這是 2026-09-16 端對端手動測試時實際發現的案例，不是假設性的。
    """
    if not matches:
        return None
    top = matches[0]
    if top["reason"] == "exact":
        return top
    if top["score"] < min_score:
        return None
    if len(matches) > 1 and (top["score"] - matches[1]["score"]) < min_gap:
        return None
    return top
