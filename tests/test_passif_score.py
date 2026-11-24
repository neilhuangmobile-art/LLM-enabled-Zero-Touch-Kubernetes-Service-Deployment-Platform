"""照題目 pass_if 評分（experiments/model_select/reason/passif_score.py，2026-11-24）。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "experiments", "model_select", "reason"))
from passif_score import score_passif  # noqa: E402


def _t(pass_if, extra=()):
    return {"pass_if": pass_if, "extra": [{"need": n} for n in extra]}


def test_storage_size_and_negated_expose():
    t = _t("映像檔屬於 accept 清單、副本數為 1、有至少 10Gi 保存空間、沒有對外開放")
    small = {"storage": {"size": "1Gi"}, "expose": "internal"}
    assert not score_passif(t, small, True)["passed"]
    big = {"storage": {"size": "5Gi", "limit_notice": True}, "expose": "internal"}   # 10Gi 超過平台上限：給到上限並告知
    assert score_passif(t, big, True)["passed"]
    assert not score_passif(t, dict(big, expose="public"), True)["passed"]   # 「沒有對外開放」不能被當成「有對外開放」


def test_expose_split_by_and():
    t = _t("映像檔合理、有配置保存資料的空間與對外開放")
    r = score_passif(t, {"storage": {"size": "1Gi"}, "expose": "internal"}, True)
    assert r["checks"] == {"有配置保存資料的空間": True, "對外開放": False}


def test_cron_patterns():
    pred = lambda s: {"workload": "cronjob", "schedule": s}   # noqa: E731
    assert score_passif(_t("建立 CronJob 且排程為每天 23:00"), pred("0 23 * * *"), True)["passed"]
    assert not score_passif(_t("建立 CronJob 且排程為每天 23:00"), pred("0 11 * * *"), True)["passed"]
    assert score_passif(_t("建立 CronJob 且排程為每週一早上 6～10 點之間"), pred("0 9 * * 1"), True)["passed"]
    assert score_passif(_t("設定為每 6 小時執行的 CronJob"), pred("0 */6 * * *"), True)["passed"]
    assert not score_passif(_t("設定為每週定時執行（CronJob）"), {"workload": "service"}, True)["passed"]


def test_multi_service_and_backup_never_pass():
    assert not score_passif(_t("有搭配資料庫"), {}, True)["passed"]
    t = _t("有保存空間、有每天 04:00 的備份 CronJob", extra=["另一個 CronJob 備份，排程 0 4 * * *"])
    assert not score_passif(t, {"workload": "cronjob", "schedule": "0 4 * * *", "storage": {"size": "5Gi"}}, True)["passed"]


def test_password_from_secret_or_db_image():
    t = _t("有設定登入密碼")
    assert not score_passif(t, {}, True, {})["passed"]
    assert score_passif(t, {}, True, {"secret_keys": ["ADMIN_PASSWORD"]})["passed"]
    assert score_passif(t, {"db_password": True}, True)["passed"]


def test_base_failure_stays_failed_and_unknown_reported():
    assert not score_passif(_t("有對外開放"), {"expose": "public"}, False)["passed"]
    assert score_passif(_t("某個看不懂的條件"), {}, True)["unknown"] == ["某個看不懂的條件"]


def test_must_ask_strict(tmp_path, monkeypatch):
    import json
    import passif_score as ps
    f = tmp_path / "j.json"
    f.write_text(json.dumps({"x:S1": {"passed": True, "asked": ["要部署什麼？"]}}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(ps, "JUDGMENTS", str(f))
    q = [{"field": "image", "q": "要部署什麼？"}]
    assert ps.score_must_ask("x", "S1", {"image": "postgres:15"}, q)["passed"] is False   # 已部署就不算問到
    assert ps.score_must_ask("x", "S1", {"image": None}, [])["passed"] is False
    assert ps.score_must_ask("x", "S1", {"image": None}, q)["passed"] is True
    changed = ps.score_must_ask("x", "S1", {"image": None}, [{"q": "別的問句"}])
    assert changed["passed"] is None and changed["judged"] is False                      # 問句變了：待重新判斷
