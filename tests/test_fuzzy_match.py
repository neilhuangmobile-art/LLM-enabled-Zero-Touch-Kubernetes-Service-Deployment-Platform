"""
測試 core/fuzzy_match.py 的模糊資源名稱比對邏輯。純函式，不需要 K8s 連線、不需要
model server，比照 tests/test_cost_agent.py 的測試風格（純邏輯輸入輸出比對）。

這是 Chat「指代消解」跟「模糊比對真實資源名稱」兩個功能共用的核心比對邏輯——
判斷錯了會導致破壞性操作（scale/delete/rollback）被套用到錯誤的資源，比純問答
答錯更嚴重，值得優先覆蓋測試。pick_confident_match() 的門檻邏輯尤其重要：分數
不夠高或前兩名分數太接近時，必須回 None 逼呼叫端反問使用者，不能自己猜一個。
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.fuzzy_match import fuzzy_match_resource, pick_confident_match


class TestFuzzyMatchResource:
    def test_exact_match_scores_highest(self):
        results = fuzzy_match_resource("web-frontend", ["web-frontend", "my-cache"])
        assert results[0]["name"] == "web-frontend"
        assert results[0]["score"] == 1.0
        assert results[0]["reason"] == "exact"

    def test_exact_match_is_case_insensitive(self):
        results = fuzzy_match_resource("Web-Frontend", ["web-frontend"])
        assert results[0]["score"] == 1.0
        assert results[0]["reason"] == "exact"

    def test_prefix_match_finds_full_pod_name_from_deployment_name(self):
        # 真實情境：使用者講 deployment 名稱，K8s 裡實際的 pod 名稱後面帶亂數後綴
        results = fuzzy_match_resource("web-frontend", ["web-frontend-7d8f9-xk2pq", "my-cache-abc12"])
        assert results[0]["name"] == "web-frontend-7d8f9-xk2pq"
        assert results[0]["reason"] == "prefix"

    def test_typo_is_detected_via_similarity(self):
        results = fuzzy_match_resource("web-fronted", ["web-frontend", "my-cache", "auto-app"])
        assert results[0]["name"] == "web-frontend"
        assert results[0]["reason"] == "typo"
        assert results[0]["score"] >= 0.72

    def test_short_query_does_not_prefix_match_everything(self):
        # "w" 太短，不該靠 startswith 就命中一大堆不相關的候選
        results = fuzzy_match_resource("w", ["web-frontend", "worker-service"])
        assert not any(r["reason"] == "prefix" for r in results)

    def test_category_alias_matches_when_no_direct_string_similarity(self):
        # "快取服務" 跟任何候選名稱字面上都不像，只能靠中文暱稱 → 類別比對
        results = fuzzy_match_resource("快取服務", ["my-cache", "web-frontend"])
        names = [r["name"] for r in results]
        assert "my-cache" in names
        assert all(r["reason"] == "category" for r in results if r["name"] == "my-cache")
        assert "web-frontend" not in names  # 不該被誤判成同一類別

    def test_category_match_score_is_below_confident_threshold(self):
        # 類別比對是最不精確的一層，分數要低到不會被 pick_confident_match() 誤採用
        results = fuzzy_match_resource("快取服務", ["my-cache"])
        assert results[0]["score"] < 0.85

    def test_no_candidates_returns_empty_list(self):
        assert fuzzy_match_resource("anything", []) == []

    def test_empty_query_returns_empty_list(self):
        assert fuzzy_match_resource("", ["web-frontend"]) == []

    def test_no_similarity_and_no_category_hint_returns_empty_list(self):
        results = fuzzy_match_resource("xyzzy123nonsense", ["web-frontend", "my-cache"])
        assert results == []

    def test_results_sorted_descending_by_score(self):
        results = fuzzy_match_resource("web-fronted", ["web-frontend", "web-front-x"])
        scores = [r["score"] for r in results]
        assert scores == sorted(scores, reverse=True)


class TestPickConfidentMatch:
    def test_single_high_score_is_confident(self):
        matches = [{"name": "web-frontend", "score": 1.0, "reason": "exact"}]
        result = pick_confident_match(matches)
        assert result is not None
        assert result["name"] == "web-frontend"

    def test_low_score_is_not_confident(self):
        matches = [{"name": "web-frontend", "score": 0.6, "reason": "category"}]
        assert pick_confident_match(matches) is None

    def test_close_top_two_scores_are_ambiguous(self):
        matches = [
            {"name": "web-frontend", "score": 0.88, "reason": "typo"},
            {"name": "web-front-x", "score": 0.86, "reason": "typo"},
        ]
        assert pick_confident_match(matches) is None

    def test_exact_match_wins_even_with_a_close_runner_up(self):
        # 真實回歸案例（2026-09-16 端對端手動測試發現）：K8s 的 Pod 名稱慣例是
        # 「Deployment 名稱 + hash 後綴」，查詢 Deployment 自己的名稱時，Deployment
        # 是 exact（1.0），它自己的 Pod 是 prefix（0.9），差距只有 0.1 < 0.15 的
        # 門檻——不能因此判成「不夠確定」，使用者打的字串跟一個真實資源完全相符，
        # 這不是真的有歧義。
        matches = [
            {"name": "cache-service", "score": 1.0, "reason": "exact"},
            {"name": "cache-service-7fb8db84-fzqhh", "score": 0.9, "reason": "prefix"},
            {"name": "cache-service-7fb8db84-m96tv", "score": 0.9, "reason": "prefix"},
        ]
        result = pick_confident_match(matches)
        assert result is not None
        assert result["name"] == "cache-service"

    def test_clear_score_gap_is_confident(self):
        matches = [
            {"name": "web-frontend", "score": 0.95, "reason": "typo"},
            {"name": "my-cache", "score": 0.55, "reason": "category"},
        ]
        result = pick_confident_match(matches)
        assert result is not None
        assert result["name"] == "web-frontend"

    def test_empty_matches_returns_none(self):
        assert pick_confident_match([]) is None

    def test_custom_thresholds_are_respected(self):
        matches = [
            {"name": "a", "score": 0.7, "reason": "typo"},
            {"name": "b", "score": 0.5, "reason": "typo"},
        ]
        assert pick_confident_match(matches) is None  # 預設門檻 0.85 擋下
        result = pick_confident_match(matches, min_score=0.6, min_gap=0.1)
        assert result is not None
        assert result["name"] == "a"
