"""
guardian/dry_run.py
kubectl dry-run 自動驗證層

在 AI 生成的 YAML 清單真正套用到叢集之前，先透過 kubectl 驗證：
  - client 模式：只做語法與 schema 檢查（不需連線叢集）
  - server 模式：完整驗證（需要可用的 K8s 叢集）

用法：
    from guardian.dry_run import validate_manifest

    result = validate_manifest(yaml_string)
    if result["ok"]:
        # 安全，可以 apply
    else:
        print(result["errors"])

研究報告依據：
    「自動化驗證流：在合併 PR 前，透過 CI 管道運行
     kubectl apply --dry-run、YAML 語法分析（Linter）和安全掃描」
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import subprocess
import tempfile
import yaml
from typing import Union


# ══════════════════════════════════════════════════════════════════
# 公開 API
# ══════════════════════════════════════════════════════════════════

def validate_manifest(
    manifest: Union[str, dict],
    mode: str = "client",
    namespace: str = "default",
) -> dict:
    """
    對 K8s manifest 執行 kubectl dry-run 驗證。

    Args:
        manifest : YAML 字串 或 Python dict（單個資源或多文件列表）
        mode     : "client"（僅語法）或 "server"（完整叢集驗證）
        namespace: 目標 namespace

    Returns:
        {
            "ok"      : bool,           # True = 驗證通過
            "mode"    : str,            # 使用的驗證模式
            "errors"  : list[str],      # 錯誤訊息列表
            "warnings": list[str],      # 警告訊息列表
            "stdout"  : str,            # kubectl 原始輸出
        }
    """
    yaml_text = _to_yaml_string(manifest)
    if yaml_text is None:
        return _fail(["manifest 轉 YAML 失敗，請確認輸入格式"], mode)

    # 先做基本的 YAML 語法解析（比 kubectl 快，且不需要叢集）
    parse_errors = _yaml_parse_check(yaml_text)
    if parse_errors:
        return _fail(parse_errors, mode)

    # 呼叫 kubectl dry-run
    return _kubectl_dry_run(yaml_text, mode, namespace)


def validate_from_llm_result(llm_result: dict, mode: str = "client") -> dict:
    """
    直接接受 llama_client.ask_llama() 的輸出，
    自動組裝成 Deployment + Service 清單並驗證。

    Args:
        llm_result: ask_llama() 回傳的 dict
                    需包含 pods, image, app_name（port, memory 可選）
        mode      : "client" 或 "server"
    """
    if "error" in llm_result:
        return _fail([f"LLM 輸出有錯誤：{llm_result['error']}"], mode)

    try:
        manifests = _build_k8s_manifests(llm_result)
    except KeyError as e:
        return _fail([f"LLM 輸出缺少必要欄位：{e}"], mode)

    yaml_text = "---\n".join(yaml.dump(m, allow_unicode=True) for m in manifests)
    return validate_manifest(yaml_text, mode)


# ══════════════════════════════════════════════════════════════════
# 內部工具函式
# ══════════════════════════════════════════════════════════════════

def _to_yaml_string(manifest: Union[str, dict]) -> str:
    """將 dict 或 YAML 字串統一轉成 YAML 字串。"""
    if isinstance(manifest, str):
        return manifest
    if isinstance(manifest, dict):
        return yaml.dump(manifest, allow_unicode=True)
    if isinstance(manifest, list):
        return "---\n".join(yaml.dump(m, allow_unicode=True) for m in manifest)
    return None


def _yaml_parse_check(yaml_text: str) -> list:
    """嘗試解析 YAML，回傳錯誤列表。空列表 = 語法正確。"""
    errors = []
    try:
        docs = list(yaml.safe_load_all(yaml_text))
        if not docs or all(d is None for d in docs):
            errors.append("YAML 解析結果為空，請確認 manifest 內容")
    except yaml.YAMLError as e:
        errors.append(f"YAML 語法錯誤：{e}")
    return errors


def _kubectl_dry_run(yaml_text: str, mode: str, namespace: str) -> dict:
    """將 YAML 寫入暫存檔，呼叫 kubectl apply --dry-run。"""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", delete=False, encoding="utf-8"
    ) as tf:
        tf.write(yaml_text)
        tmp_path = tf.name

    try:
        cmd = [
            "kubectl", "apply",
            f"--dry-run={mode}",
            f"--namespace={namespace}",
            "-f", tmp_path,
        ]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )

        stdout   = result.stdout.strip()
        stderr   = result.stderr.strip()
        ok       = (result.returncode == 0)
        errors   = []
        warnings = []

        if not ok:
            # 解析 kubectl 的錯誤輸出
            for line in stderr.splitlines():
                line = line.strip()
                if not line:
                    continue
                if "Warning" in line or "warning" in line:
                    warnings.append(line)
                else:
                    errors.append(line)
            # 如果 stderr 為空但 returncode 不是 0
            if not errors:
                errors.append(f"kubectl 回傳非零狀態碼：{result.returncode}")
        else:
            # 成功時 stderr 可能有 warning
            for line in stderr.splitlines():
                if line.strip():
                    warnings.append(line.strip())

        return {
            "ok"      : ok,
            "mode"    : mode,
            "errors"  : errors,
            "warnings": warnings,
            "stdout"  : stdout,
        }

    except FileNotFoundError:
        # kubectl 未安裝或不在 PATH
        return {
            "ok"      : None,   # None = 無法判斷（kubectl 不可用）
            "mode"    : mode,
            "errors"  : ["kubectl 未安裝或不在系統 PATH，無法執行 dry-run"],
            "warnings": ["建議安裝 kubectl 以啟用完整驗證"],
            "stdout"  : "",
        }
    except subprocess.TimeoutExpired:
        return _fail(["kubectl dry-run 超時（30 秒），請檢查叢集連線"], mode)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _build_k8s_manifests(llm_result: dict) -> list:
    """
    將 ask_llama() 輸出組裝為 Deployment + Service 的 manifest 列表。
    這是目前 0_touch_generate_pods.py 的邏輯，集中在此供驗證層使用。
    """
    app_name = llm_result["app_name"]
    image    = llm_result["image"]
    pods     = int(llm_result["pods"])
    port     = llm_result.get("port")
    memory   = llm_result.get("memory")

    # ── Deployment ──────────────────────────────────────────────
    container = {
        "name" : app_name,
        "image": image,
    }
    if port:
        container["ports"] = [{"containerPort": int(port)}]
    if memory:
        container["resources"] = {
            "limits"  : {"memory": memory},
            "requests": {"memory": memory},
        }

    deployment = {
        "apiVersion": "apps/v1",
        "kind"      : "Deployment",
        "metadata"  : {"name": app_name, "labels": {"app": app_name}},
        "spec"      : {
            "replicas": pods,
            "selector": {"matchLabels": {"app": app_name}},
            "template": {
                "metadata": {"labels": {"app": app_name}},
                "spec"    : {"containers": [container]},
            },
        },
    }

    manifests = [deployment]

    # ── Service（只在有 port 時才建立）──────────────────────────
    if port:
        service = {
            "apiVersion": "v1",
            "kind"      : "Service",
            "metadata"  : {"name": f"{app_name}-svc"},
            "spec"      : {
                "selector": {"app": app_name},
                "ports"   : [{"port": int(port), "targetPort": int(port)}],
                "type"    : "ClusterIP",
            },
        }
        manifests.append(service)

    return manifests


def _fail(errors: list, mode: str) -> dict:
    return {
        "ok"      : False,
        "mode"    : mode,
        "errors"  : errors,
        "warnings": [],
        "stdout"  : "",
    }


# ══════════════════════════════════════════════════════════════════
# 命令列直接執行（測試用）
# ══════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("=" * 55)
    print("  guardian/dry_run.py — kubectl dry-run 驗證工具")
    print("=" * 55)

    # 測試用：模擬 ask_llama() 輸出
    test_cases = [
        {
            "name"  : "正常部署（帶 port 和 memory）",
            "input" : {"pods": 3, "image": "nginx:latest",
                       "app_name": "web-frontend", "port": 80, "memory": "256Mi"},
        },
        {
            "name"  : "最小部署",
            "input" : {"pods": 1, "image": "redis:7-alpine", "app_name": "cache"},
        },
        {
            "name"  : "錯誤輸出（LLM 解析失敗）",
            "input" : {"error": "解析失敗", "raw": "..."},
        },
    ]

    for tc in test_cases:
        print(f"\n▶ {tc['name']}")
        result = validate_from_llm_result(tc["input"], mode="client")
        status = "✅ 通過" if result["ok"] else ("⚠️  kubectl 不可用" if result["ok"] is None else "❌ 失敗")
        print(f"  狀態：{status}")
        if result["errors"]:
            for e in result["errors"]:
                print(f"  錯誤：{e}")
        if result["warnings"]:
            for w in result["warnings"]:
                print(f"  警告：{w}")
        if result["stdout"]:
            print(f"  kubectl: {result['stdout']}")
