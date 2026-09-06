"""
gitops/manifest_writer.py
將 LLM 生成的部署結構寫入 YAML 檔案，存入本地 Git 倉庫。

功能：
  - 從 ask_llama() 回傳的 dict 生成標準 K8s YAML（Deployment + Service）
  - 自動建立目錄結構：manifests/<namespace>/<app_name>/
  - 使用 GitPython 提交變更（commit message 含時間戳與 app 名稱）
  - 支援 dry_run 模式（只寫檔不 commit）

研究報告依據：
    「GitOps 工作流程確保所有部署操作均通過 Git 進行版本控制，
     提供完整的審計軌跡與回滾能力」
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

# GitPython（選用）
try:
    import git as gitpython
    GIT_AVAILABLE = True
except ImportError:
    GIT_AVAILABLE = False


# ══════════════════════════════════════════════════════════════════
# 公開 API
# ══════════════════════════════════════════════════════════════════

def write_manifest(
    llm_result    : dict,
    repo_path     : str = ".",
    namespace     : str = "default",
    dry_run       : bool = False,
    commit        : bool = True,
) -> dict:
    """
    將 LLM 生成結果寫入 Git 倉庫的 YAML 檔案。

    Args:
        llm_result : ask_llama() 回傳的 dict
        repo_path  : Git 倉庫根目錄（預設當前目錄）
        namespace  : 目標 K8s namespace
        dry_run    : True = 只顯示將寫入的內容，不實際寫檔
        commit     : True = 寫檔後自動 git commit（需 GIT_AVAILABLE）

    Returns:
        {
            "ok"          : bool,
            "files"       : list[str],   # 寫入的檔案路徑
            "commit_sha"  : str | None,  # git commit hash
            "message"     : str,
        }
    """
    app_name = llm_result.get("app_name", "app")
    app_name = _sanitize_name(app_name)

    # 建立 manifests/<namespace>/<app_name>/ 目錄
    manifest_dir = Path(repo_path) / "manifests" / namespace / app_name
    if not dry_run:
        manifest_dir.mkdir(parents=True, exist_ok=True)

    # 生成 YAML 文件
    deploy_yaml  = _build_deployment_yaml(llm_result, namespace)
    service_yaml = _build_service_yaml(llm_result, namespace)

    deploy_path  = manifest_dir / "deployment.yaml"
    service_path = manifest_dir / "service.yaml"

    written = []

    if dry_run:
        print(f"\n[dry-run] 將寫入：{deploy_path}")
        print(deploy_yaml)
        if service_yaml:
            print(f"\n[dry-run] 將寫入：{service_path}")
            print(service_yaml)
        return {"ok": True, "files": [], "commit_sha": None, "message": "dry-run 完成"}

    # 實際寫檔
    deploy_path.write_text(deploy_yaml, encoding="utf-8")
    written.append(str(deploy_path))

    if service_yaml:
        service_path.write_text(service_yaml, encoding="utf-8")
        written.append(str(service_path))

    print(f"📝 已寫入 {len(written)} 個檔案：")
    for f in written:
        print(f"   {f}")

    # Git commit
    commit_sha = None
    if commit and GIT_AVAILABLE:
        commit_sha = _git_commit(repo_path, written, app_name, namespace)
    elif commit and not GIT_AVAILABLE:
        print("⚠️  GitPython 未安裝（pip install gitpython），跳過 commit")

    return {
        "ok"        : True,
        "files"     : written,
        "commit_sha": commit_sha,
        "message"   : f"已寫入 {app_name} 的 manifests" +
                      (f"，commit: {commit_sha[:8]}" if commit_sha else ""),
    }


# ══════════════════════════════════════════════════════════════════
# YAML 生成
# ══════════════════════════════════════════════════════════════════

def _build_deployment_yaml(llm_result: dict, namespace: str) -> str:
    """從 LLM 結果生成 Deployment YAML 字串。"""
    app_name  = _sanitize_name(llm_result.get("app_name", "app"))
    image     = llm_result.get("image",    "nginx:latest")
    replicas  = int(llm_result.get("replicas", llm_result.get("pods", 1)))
    port      = int(llm_result.get("port", 80))
    cpu_req   = llm_result.get("cpu",    "100m")
    mem_req   = llm_result.get("memory", "128Mi")
    env_vars  = llm_result.get("env",    {})

    doc = {
        "apiVersion": "apps/v1",
        "kind"      : "Deployment",
        "metadata"  : {
            "name"     : app_name,
            "namespace": namespace,
            "labels"   : {"app": app_name},
            "annotations": {
                "gitops.k8s-platform/generated-at": datetime.utcnow().isoformat(),
                "gitops.k8s-platform/source"      : "llm-generated",
            },
        },
        "spec": {
            "replicas": replicas,
            "selector": {"matchLabels": {"app": app_name}},
            "template": {
                "metadata": {"labels": {"app": app_name}},
                "spec": {
                    "containers": [{
                        "name" : app_name,
                        "image": image,
                        "ports": [{"containerPort": port}],
                        "resources": {
                            "requests": {"cpu": cpu_req,    "memory": mem_req},
                            "limits"  : {"cpu": cpu_req,    "memory": mem_req},
                        },
                        "env": [
                            {"name": k, "value": str(v)}
                            for k, v in env_vars.items()
                        ],
                    }],
                },
            },
        },
    }

    return "---\n" + yaml.dump(doc, allow_unicode=True, default_flow_style=False)


def _build_service_yaml(llm_result: dict, namespace: str) -> Optional[str]:
    """生成 Service YAML；若 LLM 結果沒有 port 資訊則回傳 None。"""
    port = llm_result.get("port")
    if not port:
        return None

    app_name = _sanitize_name(llm_result.get("app_name", "app"))
    port     = int(port)

    doc = {
        "apiVersion": "v1",
        "kind"      : "Service",
        "metadata"  : {
            "name"     : app_name,
            "namespace": namespace,
            "labels"   : {"app": app_name},
        },
        "spec": {
            "selector": {"app": app_name},
            "ports"   : [{"protocol": "TCP", "port": port, "targetPort": port}],
            "type"    : "ClusterIP",
        },
    }

    return "---\n" + yaml.dump(doc, allow_unicode=True, default_flow_style=False)


# ══════════════════════════════════════════════════════════════════
# Git 操作
# ══════════════════════════════════════════════════════════════════

def _git_commit(repo_path: str, files: list, app_name: str, namespace: str) -> Optional[str]:
    """
    將指定檔案加入暫存區並提交。
    返回 commit SHA（前 8 碼），失敗則回傳 None。
    """
    try:
        repo = gitpython.Repo(repo_path, search_parent_directories=True)
        repo.index.add([os.path.abspath(f) for f in files])

        timestamp  = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        msg        = f"gitops: deploy {app_name} to {namespace} [{timestamp}]"
        commit_obj = repo.index.commit(msg)
        sha        = commit_obj.hexsha

        print(f"✅ git commit: {sha[:8]} — {msg}")
        return sha

    except gitpython.InvalidGitRepositoryError:
        print("⚠️  當前目錄不是 Git 倉庫，跳過 commit")
        return None
    except Exception as e:
        print(f"⚠️  git commit 失敗：{e}")
        return None


# ══════════════════════════════════════════════════════════════════
# 工具函式
# ══════════════════════════════════════════════════════════════════

def _sanitize_name(name: str) -> str:
    """確保名稱符合 K8s DNS label 規範（小寫、只含英數字與 -）。"""
    name = name.lower().strip()
    name = re.sub(r"[^a-z0-9-]", "-", name)
    name = re.sub(r"-+", "-", name).strip("-")
    return name[:63] or "app"


# ══════════════════════════════════════════════════════════════════
# CLI 測試入口
# ══════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("=" * 55)
    print("  gitops/manifest_writer.py — Manifest 寫入測試")
    print("=" * 55)

    sample = {
        "app_name": "my-web-app",
        "image"   : "nginx:1.25",
        "replicas": 2,
        "port"    : 80,
        "cpu"     : "200m",
        "memory"  : "256Mi",
        "env"     : {"ENV": "production", "LOG_LEVEL": "info"},
    }

    result = write_manifest(sample, repo_path=".", namespace="default", dry_run=True)
    print(f"\n結果：{result['message']}")
