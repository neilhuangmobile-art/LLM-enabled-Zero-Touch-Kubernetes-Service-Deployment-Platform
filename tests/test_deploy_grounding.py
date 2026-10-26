"""
tests/test_deploy_grounding.py
測 llama_client._ground_deploy_fields()——2026-10-25 實測：使用者說「redis 7.2」，小模型輸出
redis:7.2-alpine，原句沒有的 -alpine 一路進到規格確認卡。部署解析原本只檢查副本數範圍，
這裡補上映像檔與名稱的查證（意圖分類那條路早就有同樣的查證）。
"""
import pytest

llama_client = pytest.importorskip("llama_client")
ground = llama_client._ground_deploy_fields


def test_invented_tag_suffix_falls_back_to_stated_version():
    r = ground({"pods": 3, "image": "redis:7.2-alpine", "app_name": "shop-cache"},
               "我的購物網站需要一個快取，用 redis 7.2，大概要三份，叫 shop-cache")
    assert r["image"] == "redis:7.2" and r["app_name"] == "shop-cache" and r["_grounding"]


def test_invented_tag_without_stated_version_becomes_latest_and_name_falls_back():
    r = ground({"pods": 3, "image": "redis:7-alpine", "app_name": "cache-server"}, "部署三個 redis")
    assert r["image"] == "redis:latest" and r["app_name"] == "redis"


def test_image_not_mentioned_asks_user_instead_of_guessing():
    r = ground({"pods": 1, "image": "redis:latest", "app_name": "auto-app"}, "幫我部署一個快取服務")
    assert "error" in r and "redis:latest" in r["error"]


@pytest.mark.parametrize("result,text", [
    ({"pods": 2, "image": "postgres:16", "app_name": "db"}, "幫我開兩個 PostgreSQL 16 資料庫，叫 db"),
    ({"pods": 2, "image": "nginx:1.27", "app_name": "web-test"}, "deploy 2 nginx:1.27 pods for web-test, port 80"),
    ({"pods": 1, "image": "ghcr.io/acme/api:2.1", "app_name": "my-api"}, "部署 ghcr.io/acme/api:2.1 叫 My_Api"),
    ({"pods": 1, "image": "httpd:2.4", "app_name": "auto-app"}, "部署一個 Apache 2.4 網頁伺服器"),
    ({"pods": 1, "image": "golang:latest", "app_name": "api"}, "幫我生一個 api 服務，go 寫的"),
])
def test_grounded_values_are_untouched(result, text):
    assert ground(dict(result), text) == result


def test_default_image_is_cleared_when_not_mentioned_2026_10_26():
    # 原句沒提到映像檔：解析器補的預設 nginx 要清空，讓網站請使用者補，不能悄悄部署 nginx
    r = ground({"pods": 1, "image": "nginx:latest", "app_name": "web-frontend"}, "部署一個網站")
    assert r["image"] == "" and r["app_name"] == "auto-app"
    r = ground({"pods": 1, "image": "nginx:latest", "app_name": "auto-app"}, "幫我部署一個快取服務")
    assert r["image"] == ""
    # 有講 nginx 就照常保留
    assert ground({"pods": 2, "image": "nginx:latest", "app_name": "nginx"}, "部署兩個 nginx")["image"] == "nginx:latest"


def test_errors_pass_through():
    err = {"error": "解析失敗"}
    assert ground(err, "隨便") is err


@pytest.mark.parametrize("text,image,pods", [
    ("幫我部署快取，用 redis 7.2，三個副本，叫 shop-cache", "redis:7.2", 3),
    ("部署 2 個 nginx 1.27", "nginx:1.27", 2),
    ("部署 nginx 3 個", "nginx:latest", 3),          # 3 是數量不是版本
    ("部署 2 個 redis:7.2 名稱 cache", "redis:7.2", 2),
])
def test_deterministic_keeps_space_separated_version_2026_10_25(text, image, pods):
    r = llama_client._deterministic_deploy_parse(text)
    assert r["image"] == image and r["pods"] == pods


@pytest.mark.parametrize("text,n", [("大概要三份", 3), ("部署 3 份 redis", 3), ("兩份 nginx", 2), ("200 份人", None)])
def test_fen_is_a_replica_unit_2026_10_25(text, n):
    assert llama_client._extract_pods_count(text) == n


def test_alias_needs_whole_word():
    r = ground({"pods": 1, "image": "golang:latest", "app_name": "auto-app"}, "部署一個 google 搜尋服務")
    assert "error" in r
