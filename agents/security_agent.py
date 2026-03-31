"""
agents/security_agent.py
安全代理 — 專門負責檢查 K8s YAML 的安全設定，輸出安全評分與修復建議。

職責：
    1. 呼叫 guardian/yaml_validator.py 進行靜態安全掃描
    2. 檢查 RBAC、NetworkPolicy、ImagePull 等高風險設定
    3. 產出結構化安全報告（score、severity、issues、patches）

研究報告依據：
    「從「單一任務代理」向「多代理系統」演進是 2026 年的重要趨勢。
     在這種架構下，專門的代理（如「安全代理」、「成本代理」、「效能代理」）
     協作管理集群」
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import re
from typing import Dict, List, Any, Optional

# ────────────────────────────────────────────────────────────────
AGENT_NAME = "security_agent"

# 安全議題的嚴重等級定義
SEVERITY_SCORE = {
    "critical": 40,
    "high":     20,
    "medium":   10,
    "low":       5,
    "info":      0,
}

# 安全規則（補充 yaml_validator 沒有的更細緻規則）
_EXTRA_RULES = [
    {
        "id":       "SEC-001",
        "name":     "privileged 容器",
        "path":     "spec.containers[].securityContext.privileged",
        "bad_val":  True,
        "severity": "critical",
        "message":  "privileged=true 讓容器獲得幾乎完整的主機存取權，極度危險",
        "patch":    {"securityContext": {"privileged": False}},
    },
    {
        "id":       "SEC-002",
        "name":     "hostNetwork 啟用",
        "path":     "spec.hostNetwork",
        "bad_val":  True,
        "severity": "critical",
        "message":  "hostNetwork=true 允許容器繞過 K8s 網路策略，直接存取節點網路",
        "patch":    {"hostNetwork": False},
    },
    {
        "id":       "SEC-003",
        "name":     "hostPID 啟用",
        "path":     "spec.hostPID",
        "bad_val":  True,
        "severity": "critical",
        "message":  "hostPID=true 允許容器看到主機上所有 process，可能造成資訊洩漏",
        "patch":    {"hostPID": False},
    },
    {
        "id":       "SEC-004",
        "name":     "以 root 執行",
        "path":     "spec.containers[].securityContext.runAsUser",
        "bad_val":  0,
        "severity": "high",
        "message":  "以 root (uid=0) 執行增加容器逃逸風險，建議使用非特權使用者",
        "patch":    {"securityContext": {"runAsNonRoot": True, "runAsUser": 1000}},
    },
    {
        "id":       "SEC-005",
        "name":     "allowPrivilegeEscalation",
        "path":     "spec.containers[].securityContext.allowPrivilegeEscalation",
        "bad_val":  True,
        "severity": "high",
        "message":  "allowPrivilegeEscalation=true 允許容器取得比父 process 更高的權限",
        "patch":    {"securityContext": {"allowPrivilegeEscalation": False}},
    },
    {
        "id":       "SEC-006",
        "name":     "使用 latest tag",
        "path":     "spec.containers[].image",
        "check":    "latest_tag",
        "severity": "medium",
        "message":  "使用 :latest tag 導致部署不可重現，且無法有效進行映像安全掃描",
        "patch":    None,  # 需人工指定版本
    },
    {
        "id":       "SEC-007",
        "name":     "缺少 readOnlyRootFilesystem",
        "path":     "spec.containers[].securityContext.readOnlyRootFilesystem",
        "missing":  True,
        "severity": "low",
        "message":  "建議設定 readOnlyRootFilesystem=true，防止容器修改自身映像",
        "patch":    {"securityContext": {"readOnlyRootFilesystem": True}},
    },
    {
        "id":       "SEC-008",
        "name":     "缺少資源限制",
        "path":     "spec.containers[].resources.limits",
        "missing":  True,
        "severity": "medium",
        "message":  "未設定 resources.limits 可能導致單一容器佔用全部節點資源（資源耗盡攻擊）",
        "patch":    {
            "resources": {
                "requests": {"memory": "128Mi", "cpu": "100m"},
                "limits":   {"memory": "256Mi", "cpu": "500m"},
            }
        },
    },
]


# ════════════════════════════════════════════════════════════════
# 輔助函數
# ════════════════════════════════════════════════════════════════

def _get_containers(manifest: Dict) -> List[Dict]:
    """從 manifest 取得所有 containers（含 initContainers）。"""
    spec = manifest.get("spec", {})
    template_spec = spec.get("template", {}).get("spec", spec)
    containers = template_spec.get("containers", [])
    init_containers = template_spec.get("initContainers", [])
    return containers + init_containers


def _check_latest_tag(image: str) -> bool:
    """檢查映像是否使用 latest tag 或無 tag。"""
    if not image:
        return False
    # 無 tag 或 :latest
    if ":" not in image:
        return True
    tag = image.split(":")[-1]
    return tag == "latest" or tag == ""


def _check_rule_against_manifest(rule: Dict, manifest: Dict) -> Optional[Dict]:
    """
    對 manifest 執行單條規則檢查。
    回傳：found issue dict 或 None
    """
    containers = _get_containers(manifest)
    spec       = manifest.get("spec", {})
    template_spec = spec.get("template", {}).get("spec", spec)

    # 容器層級規則
    if "containers[]" in rule.get("path", ""):
        for c in containers:
            name = c.get("name", "unknown")
            ctx  = c.get("securityContext", {})

            # latest tag 檢查
            if rule.get("check") == "latest_tag":
                image = c.get("image", "")
                if _check_latest_tag(image):
                    return {
                        "rule_id":  rule["id"],
                        "name":     rule["name"],
                        "severity": rule["severity"],
                        "message":  f"容器 '{name}'：{rule['message']}（image={image}）",
                        "patch":    rule["patch"],
                    }

            # missing 欄位檢查
            elif rule.get("missing"):
                if "resources.limits" in rule["path"]:
                    limits = c.get("resources", {}).get("limits")
                    if not limits:
                        return {
                            "rule_id":  rule["id"],
                            "name":     rule["name"],
                            "severity": rule["severity"],
                            "message":  f"容器 '{name}'：{rule['message']}",
                            "patch":    rule["patch"],
                        }
                elif "readOnlyRootFilesystem" in rule["path"]:
                    if "readOnlyRootFilesystem" not in ctx:
                        return {
                            "rule_id":  rule["id"],
                            "name":     rule["name"],
                            "severity": rule["severity"],
                            "message":  f"容器 '{name}'：{rule['message']}",
                            "patch":    rule["patch"],
                        }

            # bad_val 比對
            elif "bad_val" in rule:
                if "privileged" in rule["path"]:
                    if ctx.get("privileged") == rule["bad_val"]:
                        return {
                            "rule_id":  rule["id"],
                            "name":     rule["name"],
                            "severity": rule["severity"],
                            "message":  f"容器 '{name}'：{rule['message']}",
                            "patch":    rule["patch"],
                        }
                elif "runAsUser" in rule["path"]:
                    if ctx.get("runAsUser") == rule["bad_val"]:
                        return {
                            "rule_id":  rule["id"],
                            "name":     rule["name"],
                            "severity": rule["severity"],
                            "message":  f"容器 '{name}'：{rule['message']}",
                            "patch":    rule["patch"],
                        }
                elif "allowPrivilegeEscalation" in rule["path"]:
                    if ctx.get("allowPrivilegeEscalation") == rule["bad_val"]:
                        return {
                            "rule_id":  rule["id"],
                            "name":     rule["name"],
                            "severity": rule["severity"],
                            "message":  f"容器 '{name}'：{rule['message']}",
                            "patch":    rule["patch"],
                        }

    # Pod 層級規則（hostNetwork、hostPID）
    else:
        if "hostNetwork" in rule["path"]:
            if template_spec.get("hostNetwork") == rule["bad_val"]:
                return {
                    "rule_id":  rule["id"],
                    "name":     rule["name"],
                    "severity": rule["severity"],
                    "message":  rule["message"],
                    "patch":    rule["patch"],
                }
        elif "hostPID" in rule["path"]:
            if template_spec.get("hostPID") == rule["bad_val"]:
                return {
                    "rule_id":  rule["id"],
                    "name":     rule["name"],
                    "severity": rule["severity"],
                    "message":  rule["message"],
                    "patch":    rule["patch"],
                }

    return None


# ════════════════════════════════════════════════════════════════
# 主要安全掃描函數
# ════════════════════════════════════════════════════════════════

def scan(manifest: Any) -> Dict:
    """
    對 K8s manifest 進行完整安全掃描。

    輸入：manifest（dict 或 YAML 字串）
    輸出：{
        "agent":    "security_agent",
        "ok":       bool,           # True = 無 critical/high 問題
        "score":    int,            # 100 = 滿分，0 = 非常危險
        "issues":   [issue_dict],   # 發現的問題列表
        "patches":  [patch_dict],   # 建議的修補
        "summary":  str,            # 人類可讀摘要
    }
    """
    # 解析輸入
    if isinstance(manifest, str):
        import yaml
        try:
            manifest = yaml.safe_load(manifest)
        except Exception as e:
            return _error_result(f"YAML 解析失敗：{e}")

    if not isinstance(manifest, dict):
        return _error_result("manifest 必須是 dict 或 YAML 字串")

    # 先呼叫 guardian/yaml_validator 做基礎掃描
    issues: List[Dict] = []
    try:
        from guardian.yaml_validator import validate_yaml
        base_result = validate_yaml(manifest)
        for err in base_result.get("errors", []):
            issues.append({
                "rule_id":  "GUARD-ERR",
                "name":     "基礎驗證錯誤",
                "severity": "critical",
                "message":  err,
                "patch":    None,
            })
        for warn in base_result.get("warnings", []):
            issues.append({
                "rule_id":  "GUARD-WARN",
                "name":     "基礎驗證警告",
                "severity": "medium",
                "message":  warn,
                "patch":    None,
            })
    except ImportError:
        pass

    # 執行進階安全規則
    for rule in _EXTRA_RULES:
        issue = _check_rule_against_manifest(rule, manifest)
        if issue:
            # 避免重複（guardian 可能已回報相同問題）
            if not any(i["rule_id"] == issue["rule_id"] for i in issues):
                issues.append(issue)

    # 計算安全分數
    deduction = sum(SEVERITY_SCORE.get(i["severity"], 0) for i in issues)
    score     = max(0, 100 - deduction)

    # 判斷是否通過
    critical_count = sum(1 for i in issues if i["severity"] == "critical")
    high_count     = sum(1 for i in issues if i["severity"] == "high")
    ok = (critical_count == 0 and high_count == 0)

    # 整理修補建議
    patches = [
        {"rule_id": i["rule_id"], "name": i["name"], "patch": i["patch"]}
        for i in issues if i.get("patch")
    ]

    # 產生摘要
    summary_parts = []
    if not issues:
        summary_parts.append("✓ 安全掃描通過，未發現問題")
    else:
        if critical_count:
            summary_parts.append(f"✗ {critical_count} 個 CRITICAL 問題（必須修復）")
        if high_count:
            summary_parts.append(f"⚠ {high_count} 個 HIGH 問題（強烈建議修復）")
        medium_count = sum(1 for i in issues if i["severity"] == "medium")
        low_count    = sum(1 for i in issues if i["severity"] in ("low", "info"))
        if medium_count:
            summary_parts.append(f"~ {medium_count} 個 MEDIUM 問題")
        if low_count:
            summary_parts.append(f"  {low_count} 個 LOW 問題")

    summary = "，".join(summary_parts) + f"（安全分數：{score}/100）"

    return {
        "agent":   AGENT_NAME,
        "ok":      ok,
        "score":   score,
        "issues":  issues,
        "patches": patches,
        "summary": summary,
    }


def _error_result(msg: str) -> Dict:
    return {
        "agent":   AGENT_NAME,
        "ok":      False,
        "score":   0,
        "issues":  [{"rule_id": "PARSE-ERR", "name": "解析錯誤",
                     "severity": "critical", "message": msg, "patch": None}],
        "patches": [],
        "summary": f"✗ 解析錯誤：{msg}",
    }


# ════════════════════════════════════════════════════════════════
# CLI
# ════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    from core.config import ensure_utf8_output; ensure_utf8_output()
    import argparse
    import yaml

    parser = argparse.ArgumentParser(description="K8s YAML 安全掃描")
    parser.add_argument("file", nargs="?", help="YAML 檔案路徑（不指定則使用範例）")
    args = parser.parse_args()

    if args.file:
        with open(args.file, encoding="utf-8") as f:
            manifest = yaml.safe_load(f)
    else:
        # 內建測試範例
        manifest = {
            "apiVersion": "apps/v1",
            "kind":       "Deployment",
            "metadata":   {"name": "test-app"},
            "spec": {
                "replicas": 2,
                "selector": {"matchLabels": {"app": "test"}},
                "template": {
                    "metadata": {"labels": {"app": "test"}},
                    "spec": {
                        "hostNetwork": True,  # 危險！
                        "containers": [{
                            "name":  "app",
                            "image": "nginx:latest",  # 有問題
                            "securityContext": {
                                "privileged": True,   # 危險！
                                "runAsUser": 0,       # 危險！
                            },
                        }]
                    }
                }
            }
        }

    result = scan(manifest)
    print(f"\n{'='*60}")
    print(f"安全掃描結果：{result['summary']}")
    print(f"{'='*60}")
    if result["issues"]:
        print("\n發現的問題：")
        for i, issue in enumerate(result["issues"], 1):
            sev = issue["severity"].upper()
            print(f"  [{sev}] {issue['rule_id']}: {issue['message']}")
    if result["patches"]:
        print(f"\n建議的修補（{len(result['patches'])} 項）：")
        for p in result["patches"]:
            print(f"  {p['rule_id']}: {json.dumps(p['patch'], ensure_ascii=False)}")
