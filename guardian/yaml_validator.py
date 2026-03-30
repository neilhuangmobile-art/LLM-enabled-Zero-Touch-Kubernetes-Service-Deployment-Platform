"""
guardian/yaml_validator.py
YAML 格式檢查 + 安全性靜態分析

在 kubectl dry-run 之前，先用純 Python 做輕量的安全掃描：
  1. Schema 合法性（apiVersion/kind/metadata 是否存在）
  2. 安全規則（對應 policy_rules.yaml 的規則）
  3. 資源配置合理性（replicas 上限、memory 格式）

研究報告依據：
    「策略即程式碼（PaC）作為終極護欄 — Kyverno 和 OPA
     在 AI 生成的請求進入叢集前對其進行攔截並驗證」
     → 本模組為 Kyverno/OPA 的輕量 Python 替代，用於本地開發階段
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import re
import yaml
from pathlib import Path
from typing import Union

# policy_rules.yaml 與本模組同目錄
_POLICY_FILE = Path(__file__).parent / "policy_rules.yaml"


# ══════════════════════════════════════════════════════════════════
# 公開 API
# ══════════════════════════════════════════════════════════════════

def validate_yaml(
    manifest: Union[str, dict, list],
    policy_file: str = None,
) -> dict:
    """
    執行完整的 YAML 驗證（格式 + 安全性）。

    Returns:
        {
            "ok"      : bool,
            "errors"  : list[str],   # 阻斷性問題（必須修復）
            "warnings": list[str],   # 建議性問題（可忽略但要注意）
            "checked" : int,         # 掃描到的資源數量
        }
    """
    errors   = []
    warnings = []

    # 1. 轉成文件列表
    docs = _to_docs(manifest)
    if docs is None:
        return {"ok": False, "errors": ["YAML 解析失敗"], "warnings": [], "checked": 0}

    # 2. 載入策略規則
    policy = _load_policy(policy_file or str(_POLICY_FILE))

    # 3. 逐一掃描每個 K8s 資源
    for i, doc in enumerate(docs):
        if not isinstance(doc, dict) or not doc:
            continue

        doc_id = f"[{i}] {doc.get('kind', '?')}/{doc.get('metadata', {}).get('name', '?')}"

        # Schema 檢查
        schema_errs = _check_schema(doc)
        errors.extend(f"{doc_id}: {e}" for e in schema_errs)

        # 安全策略檢查
        sec_errs, sec_warns = _check_security(doc, policy)
        errors.extend(f"{doc_id}: {e}" for e in sec_errs)
        warnings.extend(f"{doc_id}: {w}" for w in sec_warns)

        # 資源合理性
        res_errs, res_warns = _check_resources(doc, policy)
        errors.extend(f"{doc_id}: {e}" for e in res_errs)
        warnings.extend(f"{doc_id}: {w}" for w in res_warns)

    return {
        "ok"      : len(errors) == 0,
        "errors"  : errors,
        "warnings": warnings,
        "checked" : len([d for d in docs if isinstance(d, dict) and d]),
    }


def validate_from_llm_result(llm_result: dict) -> dict:
    """直接接受 ask_llama() 輸出進行驗證（便利包裝）。"""
    if "error" in llm_result:
        return {"ok": False, "errors": [f"LLM 輸出有錯誤：{llm_result['error']}"],
                "warnings": [], "checked": 0}
    try:
        from guardian.dry_run import _build_k8s_manifests
        manifests = _build_k8s_manifests(llm_result)
        return validate_yaml(manifests)
    except Exception as e:
        return {"ok": False, "errors": [str(e)], "warnings": [], "checked": 0}


# ══════════════════════════════════════════════════════════════════
# 內部：策略規則載入
# ══════════════════════════════════════════════════════════════════

def _load_policy(path: str) -> dict:
    """載入 policy_rules.yaml。檔案不存在時回傳預設空規則。"""
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
            return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except yaml.YAMLError:
        return {}


# ══════════════════════════════════════════════════════════════════
# 內部：檢查函式
# ══════════════════════════════════════════════════════════════════

def _check_schema(doc: dict) -> list:
    """必要欄位存在性檢查。"""
    errors = []
    required = ["apiVersion", "kind", "metadata"]
    for field in required:
        if field not in doc:
            errors.append(f"缺少必要欄位 '{field}'")

    meta = doc.get("metadata", {})
    if isinstance(meta, dict) and "name" not in meta:
        errors.append("metadata.name 不能為空")

    return errors


def _check_security(doc: dict, policy: dict) -> tuple:
    """
    安全性規則檢查，對應 policy_rules.yaml 中的 security 區塊。
    回傳 (errors, warnings)。
    """
    errors   = []
    warnings = []
    kind     = doc.get("kind", "")

    if kind not in ("Deployment", "DaemonSet", "StatefulSet", "Pod", "Job"):
        return errors, warnings

    # 取得所有 container spec
    containers = _get_containers(doc)

    sec_policy = policy.get("security", {})

    for c in containers:
        name = c.get("name", "?")
        sc   = c.get("securityContext", {}) or {}

        # ── 禁止特權容器 ────────────────────────────────────────
        if sec_policy.get("deny_privileged", True):
            if sc.get("privileged") is True:
                errors.append(
                    f"容器 '{name}' 使用了 privileged:true — 禁止特權容器"
                )

        # ── 禁止 root 執行 ──────────────────────────────────────
        if sec_policy.get("deny_run_as_root", False):
            run_as_user = sc.get("runAsUser")
            if run_as_user == 0:
                warnings.append(
                    f"容器 '{name}' 以 root（runAsUser:0）執行，建議改用非 root 使用者"
                )

        # ── 禁止 hostNetwork ────────────────────────────────────
        if sec_policy.get("deny_host_network", True):
            pod_spec = _get_pod_spec(doc)
            if pod_spec and pod_spec.get("hostNetwork") is True:
                errors.append("Pod 使用了 hostNetwork:true — 可能暴露主機網路")

        # ── 禁止 hostPID ────────────────────────────────────────
        if sec_policy.get("deny_host_pid", True):
            pod_spec = _get_pod_spec(doc)
            if pod_spec and pod_spec.get("hostPID") is True:
                errors.append("Pod 使用了 hostPID:true — 可存取主機 PID 命名空間")

        # ── 禁止 allowPrivilegeEscalation ───────────────────────
        if sec_policy.get("deny_privilege_escalation", False):
            if sc.get("allowPrivilegeEscalation") is True:
                warnings.append(
                    f"容器 '{name}' 允許提權（allowPrivilegeEscalation:true）"
                )

        # ── latest tag 警告 ─────────────────────────────────────
        if sec_policy.get("warn_latest_tag", True):
            image = c.get("image", "")
            if image.endswith(":latest") or ":" not in image:
                warnings.append(
                    f"容器 '{name}' 使用了 'latest' 或無版本 tag（{image}）"
                    "，生產環境建議使用固定版本"
                )

    return errors, warnings


def _check_resources(doc: dict, policy: dict) -> tuple:
    """資源配置合理性檢查。"""
    errors   = []
    warnings = []
    kind     = doc.get("kind", "")

    res_policy = policy.get("resources", {})

    # ── Deployment replicas 上限 ────────────────────────────────
    if kind == "Deployment":
        spec     = doc.get("spec", {}) or {}
        replicas = spec.get("replicas")
        if replicas is not None:
            max_r = res_policy.get("max_replicas", 50)
            if replicas > max_r:
                errors.append(
                    f"replicas={replicas} 超過允許上限 {max_r}"
                )
            if replicas < 1:
                errors.append(f"replicas={replicas} 不能小於 1")

    # ── 容器 memory 格式 ────────────────────────────────────────
    containers = _get_containers(doc)
    for c in containers:
        name    = c.get("name", "?")
        res     = c.get("resources", {}) or {}
        for scope in ("limits", "requests"):
            mem = (res.get(scope) or {}).get("memory")
            if mem and not re.match(r"^\d+(Ki|Mi|Gi|Ti|Pi|Ei|k|M|G|T|P|E)$", str(mem)):
                errors.append(
                    f"容器 '{name}' 的 {scope}.memory 格式錯誤：'{mem}'"
                    "（範例：256Mi, 1Gi）"
                )

        # ── 未設資源限制警告 ────────────────────────────────────
        if res_policy.get("warn_no_limits", True):
            if not res.get("limits"):
                warnings.append(
                    f"容器 '{name}' 未設定 resources.limits，可能導致資源耗盡"
                )

    return errors, warnings


# ══════════════════════════════════════════════════════════════════
# 內部：YAML 解析輔助
# ══════════════════════════════════════════════════════════════════

def _to_docs(manifest: Union[str, dict, list]) -> list:
    """將各種格式的輸入統一轉成 list[dict]。"""
    try:
        if isinstance(manifest, str):
            return list(yaml.safe_load_all(manifest))
        if isinstance(manifest, dict):
            return [manifest]
        if isinstance(manifest, list):
            return manifest
    except yaml.YAMLError:
        return None
    return None


def _get_pod_spec(doc: dict) -> dict:
    """從 Deployment/DaemonSet 等取得 pod spec。"""
    kind = doc.get("kind", "")
    if kind == "Pod":
        return doc.get("spec", {})
    return doc.get("spec", {}).get("template", {}).get("spec", {})


def _get_containers(doc: dict) -> list:
    """取得 spec 下所有 containers（含 initContainers）。"""
    pod_spec = _get_pod_spec(doc)
    if not pod_spec:
        return []
    return (
        (pod_spec.get("containers") or []) +
        (pod_spec.get("initContainers") or [])
    )


# ══════════════════════════════════════════════════════════════════
# 命令列直接執行（測試用）
# ══════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("=" * 55)
    print("  guardian/yaml_validator.py — YAML 安全性驗證")
    print("=" * 55)

    test_cases = [
        {
            "name": "安全的部署",
            "yaml": """
apiVersion: apps/v1
kind: Deployment
metadata:
  name: web-frontend
spec:
  replicas: 3
  selector:
    matchLabels:
      app: web-frontend
  template:
    metadata:
      labels:
        app: web-frontend
    spec:
      containers:
      - name: web-frontend
        image: nginx:1.25-alpine
        resources:
          limits:
            memory: 256Mi
""",
        },
        {
            "name": "危險的部署（特權容器 + hostNetwork）",
            "yaml": """
apiVersion: apps/v1
kind: Deployment
metadata:
  name: bad-app
spec:
  replicas: 200
  selector:
    matchLabels:
      app: bad-app
  template:
    metadata:
      labels:
        app: bad-app
    spec:
      hostNetwork: true
      containers:
      - name: bad-app
        image: alpine:latest
        securityContext:
          privileged: true
""",
        },
    ]

    for tc in test_cases:
        print(f"\n▶ {tc['name']}")
        result = validate_yaml(tc["yaml"])
        status = "✅ 通過" if result["ok"] else "❌ 失敗"
        print(f"  狀態：{status}（掃描 {result['checked']} 個資源）")
        for e in result["errors"]:
            print(f"  ❌ 錯誤：{e}")
        for w in result["warnings"]:
            print(f"  ⚠️  警告：{w}")
