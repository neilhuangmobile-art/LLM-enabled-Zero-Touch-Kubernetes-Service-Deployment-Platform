"""從 K8s 物件取出 Pod 範本（2026-11-17，支援定時執行 CronJob）。

審查代理和 guardian 原本都寫 spec.template.spec，CronJob 的容器在 spec.jobTemplate.spec.template.spec，
結果是 CronJob 的容器完全沒被檢查（特權、hostNetwork、映像檔黑名單都略過）。統一用這個函式取。
"""
from __future__ import annotations

from typing import Dict


def pod_template_spec(manifest: Dict) -> Dict:
    manifest = manifest or {}
    kind = manifest.get("kind", "")
    spec = manifest.get("spec", {}) or {}
    if kind == "Pod":
        return spec
    if kind == "CronJob":
        return (((spec.get("jobTemplate") or {}).get("spec") or {}).get("template") or {}).get("spec") or {}
    return spec.get("template", {}).get("spec", spec)
