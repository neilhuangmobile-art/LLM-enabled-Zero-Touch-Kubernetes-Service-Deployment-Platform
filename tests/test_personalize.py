"""個人化部署建議（2026-10-23）：agents/personalize.py。純邏輯，不需要 flask；最後一項驗證產出的 style_priors.json 格式。"""
import json
import os

import pytest

from agents.personalize import suggest, nearest_style, load_priors, PRIORS_PATH

PRIORS = {
    "feature_mean": [1.5, 0.5, 0.5, 0.5], "feature_std": [0.7, 0.5, 0.5, 0.5],
    "styles": [
        {"id": "experimental", "label": "實驗型", "share": 0.4, "replicas_median": 1, "mem_mib_median": 128, "cpu_m_median": 100,
         "center": [-0.7, -1.0, -1.0, -0.8]},
        {"id": "middle", "label": "中間型", "share": 0.35, "replicas_median": 2, "mem_mib_median": 256, "cpu_m_median": 200,
         "center": [0.0, 0.4, 0.3, 0.6]},
        {"id": "production", "label": "正式環境型", "share": 0.25, "replicas_median": 3, "mem_mib_median": 512, "cpu_m_median": 500,
         "center": [1.0, 1.0, 1.0, 1.0]},
    ]}


def test_no_history_no_suggestion():
    r = suggest([], PRIORS)
    assert not r["ok"]


def test_cold_start_uses_nearest_style():
    hist = [{"pods": 3, "memory": "512Mi", "cpu": "500m", "image": "nginx:1.27"}]
    assert nearest_style(hist, PRIORS)["id"] == "production"
    r = suggest(hist, PRIORS)
    assert r["ok"] and r["style"] == "正式環境型" and r["memory"] == "512Mi" and r["cpu"] == "500m" and r["pods"] is None   # 冷啟動不建議副本數
    assert "正式環境型" in r["reason"]
    hist2 = [{"pods": 1, "image": "nginx:latest"}]
    assert nearest_style(hist2, PRIORS)["id"] == "experimental"


def test_three_deploys_use_own_habit():
    # 2026-10-25 改：部署 3 次以上直接用自己最近 5 次的中位數（原本跟公開風格加權混合，10 次後仍只有習慣的七成）
    few = [{"pods": 3, "memory": "1Gi", "cpu": "500m", "image": "app:1"}] * 3
    many = [{"pods": 3, "memory": "1Gi", "cpu": "500m", "image": "app:1"}] * 45
    a, b = suggest(few, PRIORS), suggest(many, PRIORS)
    assert (a["memory"], a["cpu"], a["pods"]) == ("1Gi", "500m", 3) == (b["memory"], b["cpu"], b["pods"])
    assert "過去 45 次" in b["reason"] and "大多開 3 個副本" in b["reason"]


def test_follows_habit_change_with_recent_deploys():
    # 新的在前：最近 3 次改用 2Gi，之前 10 次都是 256Mi → 跟著最近的習慣
    hist = [{"pods": 4, "memory": "2Gi", "cpu": "1000m", "image": "r:7"}] * 3 + [{"pods": 2, "memory": "256Mi", "cpu": "250m", "image": "r:7"}] * 10
    r = suggest(hist, PRIORS)
    assert r["memory"] == "2Gi" and r["pods"] == 4


def test_one_as_article_is_not_a_replica_count():
    hist = [{"pods": 3, "image": "app:1"}] * 5
    assert suggest(hist, PRIORS, "幫我部署一個 nginx")["pods"] == 3
    assert suggest(hist, PRIORS, "部署一個副本的 nginx")["pods"] is None
    assert suggest(hist, PRIORS, "deploy one pod of nginx")["pods"] is None


def test_constant_probe_feature_does_not_force_production():
    # 實際的 style_priors.json：平台每次都加健康檢查，原本人人都被分成正式環境型
    real = load_priors()
    if not real:
        pytest.skip("沒有 style_priors.json")
    cheap = [{"pods": 1, "memory": "128Mi", "cpu": "100m", "image": "nginx:latest"}] * 2
    assert nearest_style(cheap, real)["id"] != "production"


def test_explicit_pods_in_sentence_not_overridden():
    hist = [{"pods": 4, "image": "app:1"}] * 5
    assert suggest(hist, PRIORS, "部署 2 個 nginx")["pods"] is None
    assert suggest(hist, PRIORS, "部署 nginx")["pods"] is not None
    # 2026-10-24：中文數字、英文數字詞也算講了副本數（「開兩個」原本認不得，會被習慣值蓋掉）
    for text in ("cache 服務開兩個，memory 給大一點 1Gi", "幫我部署十個 nginx", "redis 開三個副本", "deploy three pods of nginx"):
        assert suggest(hist, PRIORS, text)["pods"] is None, text


def test_rounding_looks_human():
    r = suggest([{"pods": 2, "memory": "300Mi", "cpu": "0.23", "image": "x:1"}] * 10, PRIORS)
    assert r["memory"].endswith(("Mi", "Gi")) and int(r["memory"].rstrip("MiG")) % 64 == 0 or r["memory"].endswith("Gi")
    assert int(r["cpu"][:-1]) % 50 == 0


def test_works_without_priors_file():
    r = suggest([{"pods": 2, "memory": "256Mi", "image": "x:1"}] * 4, None)
    assert r["ok"] and r["memory"] == "256Mi" and r["style"] is None


def test_generated_priors_file_format():
    if not os.path.exists(PRIORS_PATH):
        pytest.skip("還沒產生 style_priors.json")
    p = load_priors()
    assert p["deployments"] >= 1000 and len(p["styles"]) == 3 and len(p["feature_mean"]) == 4
    assert abs(sum(s["share"] for s in p["styles"]) - 1) < 0.01
    assert {s["id"] for s in p["styles"]} == {"experimental", "middle", "production"}
    text = json.dumps(p, ensure_ascii=False)
    assert "apiVersion" not in text and "kind:" not in text          # 只有統計數字，沒有檔案內容
