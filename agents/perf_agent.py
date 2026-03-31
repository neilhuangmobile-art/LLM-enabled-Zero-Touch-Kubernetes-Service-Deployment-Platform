"""
agents/perf_agent.py
效能代理 — 分析 K8s 部署配置，給出 HPA、副本數與效能優化建議。

職責：
    1. 根據應用程式類型建議 HPA 設定
    2. 評估副本數是否滿足高可用需求
    3. 建議探針（probe）設定
    4. 偵測效能瓶頸設定（如缺少反親和性、無 PDB）

研究報告依據：
    「效能代理（HPA 建議）：AI 驅動的 FinOps 包括預測性容量預熱，
     平台可利用 LLM 分析歷史利用率數據，建議資源配額的動態調整」
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
from typing import Dict, List, Any, Optional

AGENT_NAME = "perf_agent"

# ── HPA 建議配置 ─────────────────────────────────────────────────
_HPA_PROFILES = {
    "web":      {"cpu_target": 70, "mem_target": 80, "min": 2, "max": 10},
    "java":     {"cpu_target": 65, "mem_target": 75, "min": 2, "max": 8},
    "database": {"cpu_target": 80, "mem_target": 85, "min": 1, "max": 3},
    "python":   {"cpu_target": 70, "mem_target": 80, "min": 2, "max": 10},
    "llm":      {"cpu_target": 80, "mem_target": 90, "min": 1, "max": 4},
    "worker":   {"cpu_target": 75, "mem_target": 80, "min": 2, "max": 20},
    "default":  {"cpu_target": 70, "mem_target": 80, "min": 2, "max": 10},
}

import re

_APP_TYPE_PATTERNS = [
    ("web",      r"nginx|apache|frontend|web|flask|fastapi|express"),
    ("java",     r"java|spring|tomcat|jvm"),
    ("database", r"postgres|mysql|redis|mongo|elasticsearch"),
    ("python",   r"python|django|celery|gunicorn"),
    ("llm",      r"llama|gpt|bert|inference|vllm"),
    ("worker",   r"worker|consumer|queue|kafka"),
]

def _detect_app_type(image: str, app_name: str = "") -> str:
    text = f"{image} {app_name}".lower()
    for t, pattern in _APP_TYPE_PATTERNS:
        if re.search(pattern, text):
            return t
    return "default"


# ════════════════════════════════════════════════════════════════
# 各項效能分析
# ════════════════════════════════════════════════════════════════

def _check_replicas(spec: Dict) -> List[Dict]:
    """檢查副本數設定。"""
    issues = []
    replicas = spec.get("replicas", 1)

    if replicas < 2:
        issues.append({
            "type":     "single_replica",
            "severity": "high",
            "message":  f"replicas={replicas}，單副本部署無法高可用（節點重啟即停機）",
            "recommendation": {
                "spec": {"replicas": 2},
                "tip":  "生產環境至少 2 個副本，搭配 PodDisruptionBudget 保護",
            },
        })
    elif replicas > 20:
        issues.append({
            "type":     "high_replica_count",
            "severity": "info",
            "message":  f"replicas={replicas}，副本數較多，建議改用 HPA 動態管理",
            "recommendation": {
                "tip": "考慮設定 HPA，根據 CPU/Memory 自動調整副本數",
            },
        })
    return issues


def _check_strategy(spec: Dict) -> List[Dict]:
    """檢查部署策略。"""
    issues = []
    strategy = spec.get("strategy", {})
    s_type   = strategy.get("type", "RollingUpdate")

    if s_type == "Recreate":
        issues.append({
            "type":     "recreate_strategy",
            "severity": "medium",
            "message":  "使用 Recreate 策略會造成部署期間服務中斷",
            "recommendation": {
                "spec": {
                    "strategy": {
                        "type": "RollingUpdate",
                        "rollingUpdate": {
                            "maxSurge": 1,
                            "maxUnavailable": 0,
                        }
                    }
                }
            },
        })
    elif s_type == "RollingUpdate":
        rolling = strategy.get("rollingUpdate", {})
        max_unavail = rolling.get("maxUnavailable")
        if max_unavail is None:
            issues.append({
                "type":     "missing_rolling_update_config",
                "severity": "low",
                "message":  "未明確設定 rollingUpdate 參數，建議明確指定 maxSurge 和 maxUnavailable",
                "recommendation": {
                    "spec": {
                        "strategy": {
                            "type": "RollingUpdate",
                            "rollingUpdate": {
                                "maxSurge": 1,
                                "maxUnavailable": 0,
                            }
                        }
                    }
                },
            })

    return issues


def _check_probes(containers: List[Dict]) -> List[Dict]:
    """檢查健康探針設定。"""
    issues = []

    for c in containers:
        name            = c.get("name", "unknown")
        has_readiness   = "readinessProbe" in c
        has_liveness    = "livenessProbe" in c
        has_startup     = "startupProbe" in c
        liveness        = c.get("livenessProbe", {})
        image           = c.get("image", "")
        app_type        = _detect_app_type(image, name)

        if not has_readiness:
            issues.append({
                "type":     "missing_readiness_probe",
                "severity": "high",
                "message":  (
                    f"容器 '{name}' 缺少 readinessProbe，"
                    "流量可能在應用程式就緒前就被路由進來"
                ),
                "recommendation": {
                    "readinessProbe": {
                        "httpGet": {"path": "/health", "port": 8080},
                        "initialDelaySeconds": 10,
                        "periodSeconds": 5,
                        "failureThreshold": 3,
                    }
                },
            })

        if not has_liveness:
            issues.append({
                "type":     "missing_liveness_probe",
                "severity": "medium",
                "message":  f"容器 '{name}' 缺少 livenessProbe，死鎖或假死的容器不會被自動重啟",
                "recommendation": {
                    "livenessProbe": {
                        "httpGet": {"path": "/health", "port": 8080},
                        "initialDelaySeconds": 30,
                        "periodSeconds": 10,
                        "failureThreshold": 3,
                    }
                },
            })

        # Java 應用通常需要更長的 initialDelay
        if has_liveness and app_type == "java":
            init_delay = liveness.get("initialDelaySeconds", 0)
            if init_delay < 60:
                issues.append({
                    "type":     "java_liveness_too_aggressive",
                    "severity": "medium",
                    "message":  (
                        f"容器 '{name}'（Java 應用）livenessProbe.initialDelaySeconds={init_delay}，"
                        "JVM 啟動可能需要 60-120 秒，過短會導致容器被誤殺"
                    ),
                    "recommendation": {
                        "livenessProbe": {
                            "initialDelaySeconds": 120,
                            "startupProbe": {
                                "httpGet": {"path": "/health", "port": 8080},
                                "failureThreshold": 30,
                                "periodSeconds": 10,
                            }
                        }
                    },
                })

    return issues


def _check_anti_affinity(template_spec: Dict, app_name: str) -> List[Dict]:
    """檢查 Pod 反親和性設定（確保副本分散到不同節點）。"""
    issues = []
    affinity = template_spec.get("affinity", {})
    pod_anti_affinity = affinity.get("podAntiAffinity", {})

    if not pod_anti_affinity:
        issues.append({
            "type":     "missing_anti_affinity",
            "severity": "low",
            "message":  "未設定 podAntiAffinity，多個副本可能調度到同一節點，節點故障時全部停機",
            "recommendation": {
                "affinity": {
                    "podAntiAffinity": {
                        "preferredDuringSchedulingIgnoredDuringExecution": [{
                            "weight": 100,
                            "podAffinityTerm": {
                                "labelSelector": {
                                    "matchExpressions": [{
                                        "key":      "app",
                                        "operator": "In",
                                        "values":   [app_name],
                                    }]
                                },
                                "topologyKey": "kubernetes.io/hostname",
                            }
                        }]
                    }
                }
            },
        })

    return issues


def _generate_hpa_yaml(app_name: str, app_type: str, namespace: str = "default") -> Dict:
    """產生 HPA YAML 設定。"""
    profile = _HPA_PROFILES.get(app_type, _HPA_PROFILES["default"])
    return {
        "apiVersion": "autoscaling/v2",
        "kind":       "HorizontalPodAutoscaler",
        "metadata": {
            "name":      f"{app_name}-hpa",
            "namespace": namespace,
        },
        "spec": {
            "scaleTargetRef": {
                "apiVersion": "apps/v1",
                "kind":       "Deployment",
                "name":       app_name,
            },
            "minReplicas": profile["min"],
            "maxReplicas": profile["max"],
            "metrics": [
                {
                    "type": "Resource",
                    "resource": {
                        "name": "cpu",
                        "target": {
                            "type":               "Utilization",
                            "averageUtilization": profile["cpu_target"],
                        }
                    }
                },
                {
                    "type": "Resource",
                    "resource": {
                        "name": "memory",
                        "target": {
                            "type":               "Utilization",
                            "averageUtilization": profile["mem_target"],
                        }
                    }
                },
            ],
        }
    }


# ════════════════════════════════════════════════════════════════
# 主要分析函數
# ════════════════════════════════════════════════════════════════

def analyze(manifest: Any) -> Dict:
    """
    分析 K8s manifest 的效能配置，給出優化建議。

    輸出：{
        "agent":    "perf_agent",
        "ok":       bool,
        "issues":   [issue_dict],
        "hpa_yaml": dict,          # 建議的 HPA 配置
        "summary":  str,
    }
    """
    if isinstance(manifest, str):
        import yaml
        try:
            manifest = yaml.safe_load(manifest)
        except Exception as e:
            return {"agent": AGENT_NAME, "ok": False, "issues": [],
                    "hpa_yaml": None, "summary": f"YAML 解析失敗：{e}"}

    spec          = manifest.get("spec", {})
    app_name      = manifest.get("metadata", {}).get("name", "app")
    namespace     = manifest.get("metadata", {}).get("namespace", "default")
    template_spec = spec.get("template", {}).get("spec", spec)
    containers    = template_spec.get("containers", [])

    # 推測應用程式類型
    first_image = containers[0].get("image", "") if containers else ""
    app_type    = _detect_app_type(first_image, app_name)

    all_issues: List[Dict] = []
    all_issues.extend(_check_replicas(spec))
    all_issues.extend(_check_strategy(spec))
    all_issues.extend(_check_probes(containers))
    all_issues.extend(_check_anti_affinity(template_spec, app_name))

    high_count = sum(1 for i in all_issues if i["severity"] in ("critical", "high"))
    ok         = (high_count == 0)

    # 產生 HPA 建議
    hpa_yaml = _generate_hpa_yaml(app_name, app_type, namespace)

    if not all_issues:
        summary = f"✓ 效能配置良好（{app_type} 類型），建議搭配 HPA 動態擴展"
    else:
        summary = (
            f"{'✗' if not ok else '~'} 發現 {len(all_issues)} 個效能問題"
            f"（{high_count} 個需立即處理）"
        )

    return {
        "agent":    AGENT_NAME,
        "ok":       ok,
        "issues":   all_issues,
        "hpa_yaml": hpa_yaml,
        "app_type": app_type,
        "summary":  summary,
    }


# ════════════════════════════════════════════════════════════════
# CLI
# ════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    from core.config import ensure_utf8_output; ensure_utf8_output()
    import argparse, yaml as yaml_lib

    parser = argparse.ArgumentParser(description="K8s 效能分析")
    parser.add_argument("file", nargs="?", help="YAML 檔案路徑")
    args = parser.parse_args()

    if args.file:
        with open(args.file, encoding="utf-8") as f:
            manifest = yaml_lib.safe_load(f)
    else:
        manifest = {
            "apiVersion": "apps/v1", "kind": "Deployment",
            "metadata":   {"name": "nginx-app", "namespace": "default"},
            "spec": {
                "replicas": 1,  # 單副本！
                "template": {
                    "metadata": {"labels": {"app": "nginx-app"}},
                    "spec": {
                        "containers": [{
                            "name":  "nginx",
                            "image": "nginx:1.25",
                            # 沒有 probe！
                        }]
                    }
                }
            }
        }

    result = analyze(manifest)
    print(f"\n{'='*60}")
    print(f"效能分析（{result.get('app_type', '')} 類型）：{result['summary']}")
    print(f"{'='*60}")
    if result["issues"]:
        print(f"\n發現的問題（{len(result['issues'])} 項）：")
        for i in result["issues"]:
            print(f"  [{i['severity'].upper()}] {i['type']}: {i['message']}")
    if result.get("hpa_yaml"):
        print(f"\n建議的 HPA 配置：")
        print(yaml_lib.dump(result["hpa_yaml"], allow_unicode=True, default_flow_style=False))
