"""
core/web_security.py
網站上外網用的防護：頻率限制（token bucket）、路徑分類規則。

2026-10-09：平台要透過通道（Tailscale Funnel / Cloudflare Tunnel）對外開放給客戶使用，
原本的 Flask 開發伺服器沒有任何頻率限制——任何人狂送請求就能耗光 GPU（模型一次只能
處理一個請求）和 Gemini 免費額度。這裡是純邏輯、不 import Flask，CI 能直接測。

記憶體內計數：重啟 web_demo.py 會歸零，單一 process 部署（waitress 多執行緒共用一份）
足夠；未來真的多台/多 process 部署再換 Redis。
"""
import threading
import time
from typing import Dict, Optional, Tuple

# (規則名稱, 路徑前綴, 只限這些 HTTP 方法(None=全部), 容量, 每秒回補, 計數依據)
# 計數依據："user" = 已登入帳號（沒登入就退回 IP）；"ip" = 來源 IP；"token" = 回報器 token
RULES = (
    ("auth",    "/auth/",          ("POST",), 10, 10 / 60, "ip"),     # 登入/註冊：擋暴力破解
    ("onboard", "/onboard/",       None,      30, 30 / 60, "ip"),     # 安裝腳本/進度回報
    ("report",  "/agent/report",   None,      12, 12 / 60, "token"),  # 回報器每 15 秒一次，留餘裕
    ("chat",    "/api/chat",       None,      20, 20 / 60, "user"),   # 吃 GPU + Gemini
    ("intent",  "/api/intent",     None,      30, 30 / 60, "user"),
    ("deploy",  "/api/deploy",     ("POST",), 10, 10 / 60, "user"),   # 含 /api/deploy/parse
    ("api",     "/api/",           None,     120, 2.0,     "user"),   # 其他 API 的總上限
)


def match_rule(path: str, method: str) -> Optional[tuple]:
    """回傳第一條符合的規則；RULES 由具體到一般排列，所以 /api/chat 會先比到 chat 而不是 api。"""
    for rule in RULES:
        _, prefix, methods, *_ = rule
        if path.startswith(prefix) and (methods is None or method in methods):
            return rule
    return None


class RateLimiter:
    """Token bucket：每個 key 一桶，容量 capacity，每秒回補 refill 顆，每次請求消耗 1 顆。"""

    def __init__(self, clock=time.monotonic, max_keys: int = 50000):
        self._clock = clock
        self._lock = threading.Lock()
        self._buckets: Dict[str, Tuple[float, float]] = {}  # key -> (剩餘 token, 上次更新時間)
        self._max_keys = max_keys

    def hit(self, key: str, capacity: float, refill: float) -> Tuple[bool, float]:
        """消耗一顆。回傳 (是否允許, 若被擋要等幾秒)。"""
        now = self._clock()
        with self._lock:
            tokens, last = self._buckets.get(key, (capacity, now))
            tokens = min(capacity, tokens + (now - last) * refill)
            if tokens >= 1:
                self._buckets[key] = (tokens - 1, now)
                allowed, retry = True, 0.0
            else:
                self._buckets[key] = (tokens, now)
                allowed, retry = False, (1 - tokens) / refill if refill > 0 else 60.0
            if len(self._buckets) > self._max_keys:
                self._evict()
        return allowed, retry

    def _evict(self):
        # 被大量不同 IP 灌爆時，丟掉最久沒用的一半，避免記憶體無限長大
        items = sorted(self._buckets.items(), key=lambda kv: kv[1][1])
        for k, _ in items[: len(items) // 2]:
            del self._buckets[k]


def check_request(limiter: RateLimiter, path: str, method: str, ip: str,
                  username: Optional[str] = None, token: Optional[str] = None
                  ) -> Tuple[bool, float, Optional[str]]:
    """套用規則。回傳 (是否允許, 建議等待秒數, 命中的規則名稱)。沒有符合的規則一律放行。"""
    rule = match_rule(path, method)
    if rule is None:
        return True, 0.0, None
    name, _, _, capacity, refill, basis = rule
    if basis == "user" and username:
        who = f"u:{username}"
    elif basis == "token" and token:
        who = f"t:{token[:16]}"
    else:
        who = f"ip:{ip}"
    allowed, retry = limiter.hit(f"{name}|{who}", capacity, refill)
    return allowed, retry, name


def too_many_message(retry_after: float) -> str:
    secs = max(1, int(retry_after + 0.999))
    return (f"操作太頻繁了，請等 {secs} 秒後再試。 / "
            f"Too many requests — please wait {secs} seconds and try again.")
