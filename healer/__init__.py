# healer/__init__.py
# Phase 6 自癒模組 - 對外介面

from healer.pod_watcher import scan_once, watch_forever
from healer.diagnose   import diagnose_issue
from healer.remediate  import remediate


def heal_once(namespace: str = "default", dry_run: bool = False) -> list:
    """
    掃描一次並自動補救所有異常 Pod。

    Args:
        namespace : 要掃描的 namespace
        dry_run   : True = 只記錄，不實際修改 K8s 資源

    Returns:
        list of {"issue", "diagnosis", "remediation"} dict
    """
    issues  = scan_once(namespace, auto_heal=False)
    results = []

    for issue in issues:
        from healer.pod_watcher import _get_pod_logs, _get_pod_events
        logs   = _get_pod_logs(issue["pod_name"], issue["namespace"], issue.get("container", ""))
        events = _get_pod_events(issue["pod_name"], issue["namespace"])
        context = {**issue, "logs": logs, "events": events}

        diagnosis    = diagnose_issue(context)
        remediation  = remediate(issue, diagnosis, dry_run=dry_run)

        results.append({
            "issue"      : issue,
            "diagnosis"  : diagnosis,
            "remediation": remediation,
        })

    return results
