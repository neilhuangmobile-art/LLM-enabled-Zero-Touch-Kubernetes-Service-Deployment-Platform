"""
healer/pod_watcher.py
監聽 Kubernetes Pod 事件，偵測異常狀態並觸發 LLM 自癒流程。

監聽的異常狀態：
  - CrashLoopBackOff  — 容器反覆崩潰
  - OOMKilled         — 記憶體不足被殺
  - ImagePullBackOff  — 映像拉取失敗
  - ErrImagePull      — 映像不存在
  - Error             — 一般錯誤狀態

用法：
    # 一次性掃描（適合 cron job）
    python healer/pod_watcher.py --once

    # 持續監聽（適合背景服務）
    python healer/pod_watcher.py --watch

研究報告依據：
    「自主監控與代理化 SRE 自癒系統 —
     代理能夠分析運行時日誌與集群事件，
     識別如 CrashLoopBackOff、鏡像遺失或 Ingress 配置錯誤等問題」
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import time
import json
import argparse
from datetime import datetime
from typing import Optional

# Kubernetes client（需安裝 kubernetes 套件）
try:
    from kubernetes import client as k8s_client, config as k8s_config, watch
    K8S_AVAILABLE = True
except ImportError:
    K8S_AVAILABLE = False


# ── 需要觸發自癒的異常狀態 ────────────────────────────────────────
WATCH_REASONS = {
    "CrashLoopBackOff" : "容器反覆崩潰，可能是應用程式 bug 或設定錯誤",
    "OOMKilled"        : "記憶體不足，Pod 被系統強制終止",
    "ImagePullBackOff" : "映像拉取失敗，可能是 tag 錯誤或倉庫無法存取",
    "ErrImagePull"     : "映像不存在或無權限拉取",
    "Error"            : "容器以非零狀態碼退出",
    "RunContainerError": "容器無法啟動（可能是 securityContext 問題）",
    "CreateContainerConfigError": "容器設定錯誤（如 ConfigMap/Secret 不存在）",
}


# ══════════════════════════════════════════════════════════════════
# 公開 API
# ══════════════════════════════════════════════════════════════════

def scan_once(namespace: str = "default", auto_heal: bool = False) -> list:
    """
    掃描一次指定 namespace 內的所有異常 Pod。

    Args:
        namespace : 要掃描的 namespace（"" = 全部 namespace）
        auto_heal : True = 偵測到問題自動觸發 diagnose + remediate

    Returns:
        list of PodIssue dict
    """
    if not K8S_AVAILABLE:
        print("⚠️  kubernetes 套件未安裝，請執行：pip install kubernetes")
        return _mock_scan()

    _load_k8s_config()
    v1   = k8s_client.CoreV1Api()
    issues = []

    try:
        if namespace:
            pods = v1.list_namespaced_pod(namespace)
        else:
            pods = v1.list_pod_for_all_namespaces()
    except Exception as e:
        print(f"❌ 無法連線 K8s：{e}")
        return []

    for pod in pods.items:
        issue = _check_pod(pod)
        if issue:
            issues.append(issue)
            _print_issue(issue)
            if auto_heal:
                _trigger_heal(issue)

    if not issues:
        print(f"✅ namespace '{namespace}' 內所有 Pod 狀態正常")

    return issues


def watch_forever(namespace: str = "default", auto_heal: bool = True, interval: int = 30):
    """
    持續監聽 Pod 事件（每 interval 秒掃描一次）。

    Args:
        namespace : 要監聽的 namespace
        auto_heal : 是否自動觸發自癒
        interval  : 掃描間隔（秒）
    """
    print(f"👁️  開始持續監聽 namespace='{namespace}'，間隔={interval}s")
    print("   Ctrl+C 停止監聽\n")

    seen_issues = set()  # 避免重複處理同一個 Pod 的問題

    try:
        while True:
            issues = scan_once(namespace, auto_heal=False)

            for issue in issues:
                key = f"{issue['namespace']}/{issue['pod_name']}/{issue['reason']}"
                if key not in seen_issues:
                    seen_issues.add(key)
                    _print_issue(issue)
                    if auto_heal:
                        _trigger_heal(issue)

            # 清除已恢復的 Pod 記錄
            active_keys = {
                f"{i['namespace']}/{i['pod_name']}/{i['reason']}"
                for i in issues
            }
            seen_issues &= active_keys

            time.sleep(interval)

    except KeyboardInterrupt:
        print("\n⏹  監聽已停止")


# ══════════════════════════════════════════════════════════════════
# 內部：Pod 狀態檢查
# ══════════════════════════════════════════════════════════════════

def _check_pod(pod) -> Optional[dict]:
    """
    檢查單一 Pod 是否有異常，有則回傳 issue dict，正常回傳 None。
    """
    pod_name  = pod.metadata.name
    namespace = pod.metadata.namespace
    phase     = pod.status.phase or "Unknown"

    if not pod.status.container_statuses:
        return None

    for cs in pod.status.container_statuses:
        state = cs.state

        # ── 檢查 waiting 狀態（CrashLoopBackOff, ImagePullBackOff 等）
        if state.waiting and state.waiting.reason in WATCH_REASONS:
            return {
                "pod_name"    : pod_name,
                "namespace"   : namespace,
                "container"   : cs.name,
                "reason"      : state.waiting.reason,
                "message"     : state.waiting.message or "",
                "restart_count": cs.restart_count,
                "phase"       : phase,
                "detected_at" : datetime.utcnow().isoformat(),
                "description" : WATCH_REASONS[state.waiting.reason],
            }

        # ── 檢查 terminated 狀態（OOMKilled, Error 等）
        if state.terminated and state.terminated.reason in WATCH_REASONS:
            return {
                "pod_name"    : pod_name,
                "namespace"   : namespace,
                "container"   : cs.name,
                "reason"      : state.terminated.reason,
                "message"     : state.terminated.message or "",
                "restart_count": cs.restart_count,
                "exit_code"   : state.terminated.exit_code,
                "phase"       : phase,
                "detected_at" : datetime.utcnow().isoformat(),
                "description" : WATCH_REASONS.get(state.terminated.reason, ""),
            }

    return None


def _get_pod_logs(pod_name: str, namespace: str, container: str,
                  tail_lines: int = 50) -> str:
    """取得 Pod 最近的日誌。"""
    if not K8S_AVAILABLE:
        return "[模擬日誌] Error: connection refused\nPanic: nil pointer dereference"
    try:
        v1 = k8s_client.CoreV1Api()
        return v1.read_namespaced_pod_log(
            name=pod_name,
            namespace=namespace,
            container=container,
            tail_lines=tail_lines,
            previous=True,   # 取崩潰前的日誌
        )
    except Exception as e:
        try:
            # 取當前日誌
            v1 = k8s_client.CoreV1Api()
            return v1.read_namespaced_pod_log(
                name=pod_name,
                namespace=namespace,
                container=container,
                tail_lines=tail_lines,
            )
        except Exception:
            return f"[無法取得日誌：{e}]"


def _get_pod_events(pod_name: str, namespace: str) -> list:
    """取得 Pod 相關的 K8s Events。"""
    if not K8S_AVAILABLE:
        return []
    try:
        v1      = k8s_client.CoreV1Api()
        events  = v1.list_namespaced_event(
            namespace=namespace,
            field_selector=f"involvedObject.name={pod_name}",
        )
        return [
            {
                "reason" : e.reason,
                "message": e.message,
                "count"  : e.count,
                "type"   : e.type,
            }
            for e in events.items
        ]
    except Exception:
        return []


# ══════════════════════════════════════════════════════════════════
# 內部：自癒觸發
# ══════════════════════════════════════════════════════════════════

def _trigger_heal(issue: dict):
    """
    偵測到問題後，收集上下文並呼叫 diagnose + remediate。
    """
    print(f"\n🔧 觸發自癒流程：{issue['pod_name']} ({issue['reason']})")

    # 1. 收集日誌與事件
    logs   = _get_pod_logs(issue["pod_name"], issue["namespace"], issue["container"])
    events = _get_pod_events(issue["pod_name"], issue["namespace"])

    # 2. 組裝上下文
    context = {
        **issue,
        "logs"  : logs,
        "events": events,
    }

    # 3. 呼叫診斷模組
    try:
        from healer.diagnose import diagnose_issue
        diagnosis = diagnose_issue(context)
        print(f"   🔍 診斷結果：{diagnosis.get('root_cause', '未知')}")

        # 4. 呼叫補救模組
        from healer.remediate import remediate
        result = remediate(issue, diagnosis)
        print(f"   {'✅' if result['ok'] else '⚠️ '} 補救：{result['action']}")

    except ImportError as e:
        print(f"   ⚠️  模組尚未完成：{e}")


# ══════════════════════════════════════════════════════════════════
# 內部：工具函式
# ══════════════════════════════════════════════════════════════════

def _load_k8s_config():
    """嘗試載入 K8s 設定（先試叢集內，再試本地 kubeconfig）。"""
    try:
        k8s_config.load_incluster_config()
    except Exception:
        try:
            k8s_config.load_kube_config()
        except Exception as e:
            raise RuntimeError(f"無法載入 K8s 設定：{e}")


def _print_issue(issue: dict):
    print(f"⚠️  [{issue['detected_at'][:19]}] "
          f"{issue['namespace']}/{issue['pod_name']} "
          f"→ {issue['reason']} "
          f"（重啟次數：{issue.get('restart_count', 0)}）")
    print(f"   {issue['description']}")


def _mock_scan() -> list:
    """kubernetes 套件不可用時回傳模擬資料（用於測試）。"""
    print("ℹ️  使用模擬模式（kubernetes 套件未安裝）")
    return [
        {
            "pod_name"    : "web-frontend-abc12",
            "namespace"   : "default",
            "container"   : "web-frontend",
            "reason"      : "CrashLoopBackOff",
            "message"     : "back-off 5m0s restarting failed container",
            "restart_count": 7,
            "phase"       : "Running",
            "detected_at" : datetime.utcnow().isoformat(),
            "description" : WATCH_REASONS["CrashLoopBackOff"],
            "logs"        : "Error: ECONNREFUSED - Connection refused\nFatal: cannot connect to database",
            "events"      : [{"reason": "BackOff", "message": "Back-off restarting failed container",
                               "count": 12, "type": "Warning"}],
        }
    ]


# ══════════════════════════════════════════════════════════════════
# CLI 入口
# ══════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="K8s Pod 異常監聽器")
    parser.add_argument("--namespace", "-n", default="default",
                        help="監聽的 namespace（預設：default，空字串=全部）")
    parser.add_argument("--watch",  action="store_true", help="持續監聽模式")
    parser.add_argument("--once",   action="store_true", help="掃描一次後退出")
    parser.add_argument("--heal",   action="store_true", help="偵測到問題自動觸發自癒")
    parser.add_argument("--interval", type=int, default=30, help="監聽間隔秒數（預設 30）")
    args = parser.parse_args()

    print("=" * 55)
    print("  healer/pod_watcher.py — Pod 異常監聽器")
    print("=" * 55)

    if args.watch:
        watch_forever(args.namespace, auto_heal=args.heal, interval=args.interval)
    else:
        # 預設執行一次掃描
        issues = scan_once(args.namespace, auto_heal=args.heal)
        print(f"\n掃描完成，共發現 {len(issues)} 個異常 Pod")
        if issues:
            print(json.dumps(issues, ensure_ascii=False, indent=2, default=str))
