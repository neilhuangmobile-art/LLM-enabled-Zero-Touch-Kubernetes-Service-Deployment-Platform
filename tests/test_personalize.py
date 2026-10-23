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


def test_weight_moves_toward_personal_with_more_history():
    few = [{"pods": 1, "memory": "1Gi", "image": "app:1"}] * 3
    many = [{"pods": 1, "memory": "1Gi", "image": "app:1"}] * 45
    a, b = suggest(few, PRIORS), suggest(many, PRIORS)
    assert a["ok"] and b["ok"]
    from agents.personalize import mem_mib
    # 次數越多越接近他自己的 1Gi
    assert abs(mem_mib(b["memory"]) - 1024) < abs(mem_mib(a["memory"]) - 1024)
    assert "過去 45 次" in b["reason"]


def test_explicit_pods_in_sentence_not_overridden():
    hist = [{"pods": 4, "image": "app:1"}] * 5
    assert suggest(hist, PRIORS, "部署 2 個 nginx")["pods"] is None
    assert suggest(hist, PRIORS, "部署 nginx")["pods"] is not None


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
