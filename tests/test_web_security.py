"""
core/web_security.py：上外網用的頻率限制。純邏輯、不需要 Flask，CI 能跑。
用假時鐘控制時間，不用真的 sleep。
"""
from core.web_security import RateLimiter, check_request, match_rule, too_many_message


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_rule_order_specific_before_general():
    assert match_rule("/api/chat", "POST")[0] == "chat"
    assert match_rule("/api/deploy/parse", "POST")[0] == "deploy"
    assert match_rule("/api/pods", "GET")[0] == "api"
    # 登入頁 GET 不限，只限送出表單的 POST
    assert match_rule("/auth/login", "GET") is None
    assert match_rule("/auth/login", "POST")[0] == "auth"
    assert match_rule("/", "GET") is None


def test_bucket_blocks_after_capacity_then_refills():
    clock = FakeClock()
    lim = RateLimiter(clock=clock)
    results = [check_request(lim, "/auth/login", "POST", "1.2.3.4")[0] for _ in range(11)]
    assert results[:10] == [True] * 10 and results[10] is False
    allowed, retry, rule = check_request(lim, "/auth/login", "POST", "1.2.3.4")
    assert not allowed and rule == "auth" and 0 < retry <= 6.1
    clock.t += 6.1  # 10/分鐘 → 每 6 秒回補一顆
    assert check_request(lim, "/auth/login", "POST", "1.2.3.4")[0] is True


def test_users_and_ips_have_separate_buckets():
    lim = RateLimiter(clock=FakeClock())
    for _ in range(20):
        assert check_request(lim, "/api/chat", "POST", "9.9.9.9", username="alice")[0]
    assert check_request(lim, "/api/chat", "POST", "9.9.9.9", username="alice")[0] is False
    # 兩人同時使用：bob 不受 alice 用光額度影響（即使同一個 IP，例如同一個通道）
    assert check_request(lim, "/api/chat", "POST", "9.9.9.9", username="bob")[0] is True


def test_chat_limit_does_not_consume_general_api_bucket():
    lim = RateLimiter(clock=FakeClock())
    for _ in range(20):
        check_request(lim, "/api/chat", "POST", "1.1.1.1", username="alice")
    assert check_request(lim, "/api/pods", "GET", "1.1.1.1", username="alice")[0] is True


def test_report_bucket_keyed_by_token_not_ip():
    lim = RateLimiter(clock=FakeClock())
    for _ in range(12):
        assert check_request(lim, "/agent/report", "POST", "5.5.5.5", token="tokA" * 8)[0]
    assert check_request(lim, "/agent/report", "POST", "5.5.5.5", token="tokA" * 8)[0] is False
    assert check_request(lim, "/agent/report", "POST", "5.5.5.5", token="tokB" * 8)[0] is True


def test_eviction_keeps_memory_bounded():
    lim = RateLimiter(clock=FakeClock(), max_keys=100)
    for i in range(500):
        check_request(lim, "/auth/login", "POST", f"10.0.{i // 256}.{i % 256}")
    assert len(lim._buckets) <= 100


def test_message_is_bilingual_and_rounds_up():
    msg = too_many_message(2.2)
    assert "3 秒" in msg and "3 seconds" in msg
