"""Healer 補強方案階段 5：定時執行預設帶 podFailurePolicy（gitops/manifest_writer.build_cronjob_doc，2026-11-25）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gitops import manifest_writer as mw

SPEC = {"app_name": "backup", "image": "alpine:3.20", "pods": 1, "workload": "cronjob", "schedule": "0 3 * * *",
        "command": ["sh", "-c", "echo hi"], "memory": "64Mi", "cpu": "50m"}


def test_pod_failure_policy_rules():
    js = mw.build_cronjob_doc(SPEC, "user-x")["spec"]["jobTemplate"]["spec"]
    rules = js["podFailurePolicy"]["rules"]
    fail = next(r for r in rules if r["action"] == "FailJob")
    assert fail["onExitCodes"] == {"containerName": "backup", "operator": "In", "values": [126, 127]}
    ignore = next(r for r in rules if r["action"] == "Ignore")
    assert ignore["onPodConditions"] == [{"type": "DisruptionTarget"}]
    # podFailurePolicy 只能配 restartPolicy: Never（API server 會擋 OnFailure）
    assert js["template"]["spec"]["restartPolicy"] == "Never"
    assert js["backoffLimit"] == 2


def test_container_name_matches_rule():
    doc = mw.build_cronjob_doc({**SPEC, "app_name": "Nightly_Report"}, "user-x")
    js = doc["spec"]["jobTemplate"]["spec"]
    assert js["podFailurePolicy"]["rules"][0]["onExitCodes"]["containerName"] == js["template"]["spec"]["containers"][0]["name"]
