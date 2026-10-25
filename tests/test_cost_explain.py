"""
2026-10-25：畫面上的金額要附算式。測算式裡代入的數字跟實際算出的金額一致，
並確認所有地方用的是同一組單價（agents/cost_agent.py 開頭的常數）。
"""
import re

from agents import cost_agent, scenario, timeslot


def _manifest(replicas, cpu, mem):
    return {"spec": {"replicas": replicas, "template": {"spec": {"containers": [
        {"name": "app", "resources": {"requests": {"cpu": cpu, "memory": mem}}}]}}}}


def test_monthly_cost_how_matches_number():
    est = cost_agent._estimate_monthly_cost(_manifest(1, "100m", "128Mi"))
    assert est["estimated_usd"] == 4.05                       # 資源總覽截圖上的數字
    how = est["how"]
    assert "0.1 核" in how and "0.125 GiB" in how and "≈ 4.05 美元" in how
    cores, gib = 0.1, 0.125
    assert round(cores * cost_agent.CPU_USD_PER_HOUR * 730 + gib * cost_agent.MEM_USD_PER_GIB_HOUR * 730, 2) == 4.05


def test_replicas_multiply_into_totals():
    est = cost_agent._estimate_monthly_cost(_manifest(3, "250m", "512Mi"))
    assert "0.75 核" in est["how"] and "1.5 GiB" in est["how"] and f"≈ {est['estimated_usd']} 美元" in est["how"]


def test_timeslot_uses_same_prices_and_explains():
    c = timeslot.cost_per_replica_hour(100, 128 * 1024 ** 2)
    assert abs(c - (0.1 * cost_agent.CPU_USD_PER_HOUR + 0.125 * cost_agent.MEM_USD_PER_GIB_HOUR)) < 1e-9
    plan = {"app": "web", "base": 1, "windows": [{"start": "09:00", "end": "18:00", "replicas": 3, "days": [0, 1, 2, 3, 4]}]}
    sv = timeslot.savings(plan, c)
    assert f"≈ {sv['always_peak_usd']} 美元" in sv["how"] and f"≈ {sv['scheduled_usd']} 美元" in sv["how"]
    assert f"省下的 {sv['saved_usd']} 美元" in sv["how"]


def test_scenario_savings_explains_subtraction():
    r = scenario.plan({"total_users": 500, "duration_days": 3}, "nginx:1.27", "web")
    sv = r["savings"]
    assert sv and sv["how"]
    m = re.search(r"相減省下約 ([\d.]+) 美元", sv["how"])
    assert m and float(m.group(1)) == sv["total_usd"]
    assert f"開 {r['replicas']} 個副本" in sv["how"] and "3 天" in sv["how"]


def test_numbers_in_formula_reproduce_the_result():
    """使用者照畫面上的算式自己乘，要得到同一個數字（2026-10-25：原本單價四捨五入到 0.0056，乘出來 12.26 不是 12.15）。"""
    c = timeslot.cost_per_replica_hour(100, 128 * 1024 ** 2)
    plan = {"app": "web", "base": 1, "windows": [{"start": "09:00", "end": "18:00", "replicas": 3, "days": [0, 1, 2, 3, 4]}]}
    sv = timeslot.savings(plan, c)
    shown_c = float(re.search(r"每小時約 ([\d.]+) 美元", sv["how"]).group(1))
    assert round(shown_c * 3 * 730, 2) == sv["always_peak_usd"]
    r = scenario.plan({"total_users": 500, "duration_days": 3}, "nginx:1.27", "web")
    nums = re.search(r"約 ([\d.]+) 美元（把", r["savings"]["how"]), re.search(r"≈ ([\d.]+) 美元。沒考慮", r["savings"]["how"])
    assert round(float(nums[0].group(1)) - float(nums[1].group(1)), 2) == r["savings"]["total_usd"]
