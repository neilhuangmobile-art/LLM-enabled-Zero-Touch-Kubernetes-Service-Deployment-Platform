"""
測試 core/deploy_ambiguity.py：部署缺欄位時，判斷該用預設值猜還是該反問使用者。
純函式，不需要 K8s 連線、不需要 model server、不需要 torch（跟 llama_client.py
分開成獨立模組正是為了這點，見該檔案開頭說明）。

判斷錯了的後果：ambiguous 該擋卻沒擋 → 使用者以為系統聽懂了，實際上部署了完全
不相干的 nginx；不該擋卻擋住 → 每次部署都要多問一輪，體驗變差。兩個方向都要覆蓋。
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.deploy_ambiguity import deploy_ambiguity_check


class TestDeployAmbiguityCheck:
    def test_no_service_hint_and_default_image_is_ambiguous(self):
        result = deploy_ambiguity_check(
            "幫我起一個服務",
            {"pods": 1, "image": "nginx:latest", "app_name": "auto-app", "port": 80},
        )
        assert result["ambiguous"] is True
        assert result["reason"]

    def test_explicit_image_keyword_is_not_ambiguous_even_with_default_app_name(self):
        result = deploy_ambiguity_check(
            "deploy 3 redis pods",
            {"pods": 3, "image": "redis:latest", "app_name": "auto-app", "port": 80},
        )
        assert result["ambiguous"] is False

    def test_explicit_app_name_is_not_ambiguous_even_with_default_image(self):
        # 使用者講清楚了 app_name（代表這不是隨便亂猜的請求），即使 image 剛好落在
        # 預設值，也不該被判成「完全沒線索」。
        result = deploy_ambiguity_check(
            "幫我部署一個叫 my-checkout-service 的東西",
            {"pods": 1, "image": "nginx:latest", "app_name": "my-checkout-service", "port": 80},
        )
        assert result["ambiguous"] is False

    def test_error_spec_short_circuits_to_not_ambiguous(self):
        result = deploy_ambiguity_check("...", {"error": "解析失敗"})
        assert result["ambiguous"] is False

    def test_non_dict_spec_short_circuits_to_not_ambiguous(self):
        result = deploy_ambiguity_check("...", None)
        assert result["ambiguous"] is False

    def test_web_keyword_gives_replica_hint_when_pods_defaulted_to_one(self):
        result = deploy_ambiguity_check(
            "deploy 1 nginx pod for web-frontend",
            {"pods": 1, "image": "nginx:latest", "app_name": "web-frontend", "port": 80},
        )
        assert result["ambiguous"] is False
        assert result["hint"] is not None
        assert "副本" in result["hint"]

    def test_no_hint_when_pods_already_meets_recommended_minimum(self):
        result = deploy_ambiguity_check(
            "deploy 3 nginx pods for web-frontend",
            {"pods": 3, "image": "nginx:latest", "app_name": "web-frontend", "port": 80},
        )
        assert result["hint"] is None

    def test_database_type_with_single_pod_gives_no_hint(self):
        # agents/perf_agent 的 database profile min=1，單副本本來就符合建議，不該提醒
        result = deploy_ambiguity_check(
            "deploy 1 postgres pod for db-primary",
            {"pods": 1, "image": "postgres:15", "app_name": "db-primary", "port": 5432},
        )
        assert result["hint"] is None
