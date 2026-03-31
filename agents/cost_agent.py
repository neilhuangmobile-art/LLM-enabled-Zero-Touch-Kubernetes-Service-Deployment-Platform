"""
agents/cost_agent.py
成本代理 — 分析 K8s 資源配置，給出資源優化與成本節省建議。

職責：
    1. 評估 requests/limits 設定是否合理
    2. 根據應用程式類型估算最佳資源配置
    3. 偵測過度配置（over-provisioning）與配置不足（under-provisioning）
    4. 結合 Prometheus 指標給出動態建議（若可用）

研究報告依據：
    「AI 驅動的 FinOps 與 GPU 優化：AI 驅動的成本優化變得至關重要。
     平台可利用 LLM 分析歷史利用率數據，建議資源配額（Quotas）的動態調整」
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import re
from typing import Dict, List, Any, Optional, Tuple

AGENT_NAME = "cost_agent"

# ── 資源單位轉換 ─────────────────────────────────────────────────
_MEM_UNITS = {
    "Ki": 1024,
    "Mi": 1024 ** 2,
    "Gi": 1024 ** 3,
    "Ti": 1024 ** 4,
    "K":  1000,
    "M":  1000 ** 2,
    "G":  1000 ** 3,
}

def _parse_memory_bytes(mem_str: str) -> Optional[int]:
    """將 '256Mi', '1Gi' 等字串轉為 bytes。"""
    if not mem_str:
        return None
    m = re.match(r'^(\d+(?:\.\d+)?)\s*([A-Za-z]*)$', str(mem_str).strip())
    if not m:
        return None
    value, unit = float(m.group(1)), m.group(2)
    multiplier = _MEM_UNITS.get(unit, 1)
    return int(value * multiplier)


def _parse_cpu_millicores(cpu_str: str) -> Optional[int]:
    """將 '500m', '1', '0.5' 等字串轉為 millicores。"""
    if not cpu_str:
        return None
    s = str(cpu_str).strip()
    if s.endswith("m"):
        return int(float(s[:-1]))
    try:
        return int(float(s) * 1000)
    except ValueError:
        return None


def _bytes_to_mi(b: int) -> str:
    return f"{b // (1024**2)}Mi"

def _mc_to_str(mc: int) -> str:
    if mc >= 1000 and mc % 1000 == 0:
        return str(mc // 1000)
    return f"{mc}m"


# ── 應用程式類型識別 ─────────────────────────────────────────────

_APP_TYPE_PATTERNS = [
    ("web",       r"nginx|apache|httpd|frontend|web|flask|fastapi|express|react|vue"),
    ("java",      r"java|spring|tomcat|jvm|jboss|quarkus|micronaut"),
    ("database",  r"postgres|mysql|mariadb|mongodb|redis|cassandra|elasticsearch"),
    ("python",    r"python|django|celery|gunicorn|uvicorn"),
    ("llm",       r"llama|gpt|bert|transformer|inference|vllm|triton"),
    ("worker",    r"worker|consumer|processor|queue|kafka|rabbitmq"),
]

# 各類型的建議資源範圍（requests_cpu_mc, requests_mem_mi, limits_cpu_mc, limits_mem_mi）
_APP_PROFILES = {
    "web":      (100,  128, 500,  256),
    "java":     (250,  512, 1000, 1024),
    "database": (250,  256, 1000, 1024),
    "python":   (100,  128, 500,   256),
    "llm":      (2000, 8192, 4000, 16384),
    "worker":   (200,  256, 800,   512),
    "default":  (100,  128, 500,   256),
}


def _detect_app_type(image: str, app_name: str = "") -> str:
    """根據映像名稱和 app_name 推測應用程式類型。"""
    text = f"{image} {app_name}".lower()
    for app_type, pattern in _APP_TYPE_PATTERNS:
        if re.search(pattern, text):
            return app_type
    return "default"


# ════════════════════════════════════════════════════════════════
# 成本分析核心
# ════════════════════════════════════════════════════════════════

def _analyze_container_resources(container: Dict) -> List[Dict]:
    """分析單個容器的資源設定，回傳問題列表。"""
    issues   = []
    name     = container.get("name", "unknown")
    image    = container.get("image", "")
    res      = container.get("resources", {})
    requests = res.get("requests", {})
    limits   = res.get("limits",   {})
    app_type = _detect_app_type(image, name)
    profile  = _APP_PROFILES.get(app_type, _APP_PROFILES["default"])
    rec_req_cpu, rec_req_mem, rec_lim_cpu, rec_lim_mem = profile

    # 1. 沒有設定 requests/limits
    if not requests and not limits:
        issues.append({
            "type":     "missing_resources",
            "severity": "high",
            "message":  f"容器 '{name}' 未設定任何資源限制，可能耗盡節點資源",
            "recommendation": {
                "resources": {
                    "requests": {
                        "cpu":    _mc_to_str(rec_req_cpu),
                        "memory": _bytes_to_mi(rec_req_mem * 1024**2),
                    },
                    "limits": {
                        "cpu":    _mc_to_str(rec_lim_cpu),
                        "memory": _bytes_to_mi(rec_lim_mem * 1024**2),
                    },
                }
            },
            "app_type": app_type,
        })
        return issues

    # 2. 有 requests 沒有 limits（BestEffort → 容易被驅逐）
    if requests and not limits:
        issues.append({
            "type":     "no_limits",
            "severity": "medium",
            "message":  f"容器 '{name}' 設定了 requests 但缺少 limits，QoS 等級為 Burstable",
            "recommendation": {
                "resources": {
                    "limits": {
                        "cpu":    _mc_to_str(rec_lim_cpu),
                        "memory": _bytes_to_mi(rec_lim_mem * 1024**2),
                    }
                }
            },
        })

    # 3. 記憶體過度配置
    lim_mem_bytes = _parse_memory_bytes(limits.get("memory"))
    if lim_mem_bytes:
        lim_mem_mi = lim_mem_bytes // (1024**2)
        if lim_mem_mi > rec_lim_mem * 4:
            issues.append({
                "type":     "memory_over_provisioned",
                "severity": "low",
                "message":  (
                    f"容器 '{name}' 記憶體限制（{lim_mem_mi}Mi）"
                    f"遠超 {app_type} 類型建議值（{rec_lim_mem}Mi）"
                ),
                "recommendation": {
                    "resources": {"limits": {"memory": _bytes_to_mi(rec_lim_mem * 1024**2)}}
                },
            })

    # 4. 記憶體配置不足（OOMKilled 風險）
    if lim_mem_bytes:
        lim_mem_mi = lim_mem_bytes // (1024**2)
        if lim_mem_mi < rec_req_mem // 2:
            issues.append({
                "type":     "memory_under_provisioned",
                "severity": "high",
                "message":  (
                    f"容器 '{name}' 記憶體限制（{lim_mem_mi}Mi）"
                    f"低於 {app_type} 類型最低建議值（{rec_req_mem}Mi），有 OOMKilled 風險"
                ),
                "recommendation": {
                    "resources": {
                        "requests": {"memory": _bytes_to_mi(rec_req_mem * 1024**2)},
                        "limits":   {"memory": _bytes_to_mi(rec_lim_mem * 1024**2)},
                    }
                },
            })

    # 5. CPU 過度配置
    lim_cpu_mc = _parse_cpu_millicores(limits.get("cpu"))
    if lim_cpu_mc and lim_cpu_mc > rec_lim_cpu * 5:
        issues.append({
            "type":     "cpu_over_provisioned",
            "severity": "low",
            "message":  (
                f"容器 '{name}' CPU 限制（{lim_cpu_mc}m）"
                f"遠超 {app_type} 類型建議值（{rec_lim_cpu}m）"
            ),
            "recommendation": {
                "resources": {"limits": {"cpu": _mc_to_str(rec_lim_cpu)}}
            },
        })

    # 6. requests > limits（Kubernetes 不允許，但主動檢查）
    req_mem_bytes = _parse_memory_bytes(requests.get("memory"))
    if req_mem_bytes and lim_mem_bytes and req_mem_bytes > lim_mem_bytes:
        issues.append({
            "type":     "requests_exceed_limits",
            "severity": "critical",
            "message":  f"容器 '{name}' memory requests 超過 limits，Kubernetes 將拒絕此 Pod",
            "recommendation": None,
        })

    return issues


def _estimate_monthly_cost(manifest: Dict) -> Dict:
    """
    粗略估算月費（USD）。
    假設：c5.xlarge (4 vCPU / 8GiB) 約 $0.17/hr，以比例計算。
    """
    spec          = manifest.get("spec", {})
    replicas      = spec.get("replicas", 1)
    template_spec = spec.get("template", {}).get("spec", spec)
    containers    = template_spec.get("containers", [])

    total_cpu_mc  = 0
    total_mem_mi  = 0

    for c in containers:
        requests = c.get("resources", {}).get("requests", {})
        cpu_mc   = _parse_cpu_millicores(requests.get("cpu")) or 100
        mem_bytes = _parse_memory_bytes(requests.get("memory")) or (128 * 1024**2)
        total_cpu_mc  += cpu_mc
        total_mem_mi  += mem_bytes // (1024**2)

    # 按 requests 乘以 replicas
    monthly_cpu_core  = (total_cpu_mc * replicas) / 1000
    monthly_mem_gib   = (total_mem_mi * replicas) / 1024

    # 粗略估算（基於 AWS On-Demand 定價）
    cpu_cost = monthly_cpu_core  * 0.048 * 730   # ~$0.048/vCPU/hr
    mem_cost = monthly_mem_gib   * 0.006 * 730   # ~$0.006/GiB/hr
    total    = round(cpu_cost + mem_cost, 2)

    return {
        "replicas":       replicas,
        "cpu_cores":      round(monthly_cpu_core, 2),
        "memory_gib":     round(monthly_mem_gib, 2),
        "estimated_usd":  total,
        "note":           "粗估值（基於 AWS c5 On-Demand，僅供參考）",
    }


# ════════════════════════════════════════════════════════════════
# 主要掃描函數
# ════════════════════════════════════════════════════════════════

def analyze(manifest: Any) -> Dict:
    """
    分析 K8s manifest 的資源配置與成本效益。

    輸出：{
        "agent":    "cost_agent",
        "ok":       bool,
        "issues":   [issue_dict],
        "cost_estimate": {...},
        "summary":  str,
    }
    """
    if isinstance(manifest, str):
        import yaml
        try:
            manifest = yaml.safe_load(manifest)
        except Exception as e:
            return {"agent": AGENT_NAME, "ok": False, "issues": [],
                    "cost_estimate": {}, "summary": f"YAML 解析失敗：{e}"}

    spec          = manifest.get("spec", {})
    template_spec = spec.get("template", {}).get("spec", spec)
    containers    = (template_spec.get("containers", []) +
                     template_spec.get("initContainers", []))

    all_issues: List[Dict] = []
    for c in containers:
        all_issues.extend(_analyze_container_resources(c))

    cost = _estimate_monthly_cost(manifest)
    high_count     = sum(1 for i in all_issues if i["severity"] in ("critical", "high"))
    ok             = (high_count == 0)

    if not all_issues:
        summary = f"✓ 資源配置合理，月費估算：${cost['estimated_usd']} USD"
    else:
        summary = (
            f"{'✗' if not ok else '~'} 發現 {len(all_issues)} 個資源問題"
            f"（{high_count} 個需立即處理），月費估算：${cost['estimated_usd']} USD"
        )

    return {
        "agent":          AGENT_NAME,
        "ok":             ok,
        "issues":         all_issues,
        "cost_estimate":  cost,
        "summary":        summary,
    }


# ════════════════════════════════════════════════════════════════
# CLI
# ════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    from core.config import ensure_utf8_output; ensure_utf8_output()
    import argparse, yaml

    parser = argparse.ArgumentParser(description="K8s 資源成本分析")
    parser.add_argument("file", nargs="?", help="YAML 檔案路徑")
    args = parser.parse_args()

    if args.file:
        with open(args.file, encoding="utf-8") as f:
            manifest = yaml.safe_load(f)
    else:
        manifest = {
            "apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": "web-app"},
            "spec": {
                "replicas": 5,
                "template": {
                    "spec": {
                        "containers": [{
                            "name": "nginx", "image": "nginx:1.25",
                            "resources": {
                                "requests": {"memory": "64Mi",  "cpu": "50m"},
                                "limits":   {"memory": "32Mi",  "cpu": "100m"},  # requests > limits！
                            }
                        }]
                    }
                }
            }
        }

    result = analyze(manifest)
    print(f"\n{'='*60}")
    print(f"成本分析：{result['summary']}")
    print(f"{'='*60}")
    est = result["cost_estimate"]
    print(f"估算：{est.get('replicas')} 副本 × "
          f"{est.get('cpu_cores')} vCPU + {est.get('memory_gib')} GiB = "
          f"${est.get('estimated_usd')}/月")
    if result["issues"]:
        print(f"\n發現的問題（{len(result['issues'])} 項）：")
        for i in result["issues"]:
            print(f"  [{i['severity'].upper()}] {i['type']}: {i['message']}")
            if i.get("recommendation"):
                print(f"           建議：{json.dumps(i['recommendation'], ensure_ascii=False)}")
