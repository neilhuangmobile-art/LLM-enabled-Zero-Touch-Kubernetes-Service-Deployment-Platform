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
    pvc_yaml     = build_pvc_yaml(llm_result, namespace)
    pvc_path     = manifest_dir / "pvc.yaml"

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
    if pvc_yaml:
        pvc_path.write_text(pvc_yaml, encoding="utf-8")
        written.append(str(pvc_path))

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

# 名稱像密碼、金鑰的環境變數（2026-10-10，docker-compose 匯入）：值不寫進部署設定檔與 Git，
# 改存在 Kubernetes Secret「<服務>-secrets」，設定檔只寫「去哪個 Secret 拿」。
_SECRET_ENV_RE = re.compile(r"PASS|SECRET|TOKEN|KEY|CREDENTIAL|PRIVATE", re.IGNORECASE)
# 2026-10-24：值裡帶帳號密碼的網址或私鑰本體也算（跟 agents/compose_import._SECRET_VALUE_RE 同一條規則）
_SECRET_VALUE_RE = re.compile(r"[a-z][a-z0-9+.-]{0,20}://[^/\s:@]+:[^/\s@]+@|-----BEGIN [A-Z ]*PRIVATE KEY-----", re.IGNORECASE)


def container_ports(cport: int, extras=None) -> list:
    """容器要開的 port：主 port，加上 extra_ports 的容器 port（2026-10-24，多 port）。"""
    out = [int(cport)]
    for e in extras or []:
        if int(e["container"]) not in out:
            out.append(int(e["container"]))
    return out


def extra_service_ports(existing: list, extras=None) -> list:
    """extra_ports 對應的 Service port：每個額外 port 開「容器 port」（內部互連）與「對外 port」（兩者不同時），
    已經有的 port 號碼不重複開。名稱用 c<容器 port>／e<對外 port>（K8s 規定多個 port 要有不同名稱）。"""
    used = {p["port"] for p in existing}
    out = []
    for e in extras or []:
        for name, num in ((f"c{int(e['container'])}", int(e["container"])), (f"e{int(e['port'])}", int(e["port"]))):
            if num not in used:
                used.add(num)
                out.append({"name": name, "protocol": "TCP", "port": num, "targetPort": int(e["container"])})
    return out


def split_secret_env(env: dict, force=None):
    """回傳 (一般環境變數, 密碼類環境變數)。force：一定要當密碼的名稱（例如 K8s YAML 裡本來就放在
    Secret 的值，名稱叫 DATABASE_URL 也一樣不能寫進 Git）。"""
    force = set(force or [])
    plain, secret = {}, {}
    for k, v in (env or {}).items():
        (secret if (str(k) in force or _SECRET_ENV_RE.search(str(k)) or _SECRET_VALUE_RE.search(str(v))) else plain)[str(k)] = str(v)
    return plain, secret


def secret_name(app_name: str) -> str:
    return f"{_sanitize_name(app_name)}-secrets"


def env_entries(app_name: str, env: dict, force=None) -> list:
    """Deployment 的 env 欄位：一般的直接寫值，密碼類改成從 Secret 讀。"""
    plain, secret = split_secret_env(env, force)
    out = [{"name": k, "value": v} for k, v in plain.items()]
    out += [{"name": k, "valueFrom": {"secretKeyRef": {"name": secret_name(app_name), "key": k}}} for k in secret]
    return out


def build_secret_yaml(llm_result: dict, namespace: str) -> Optional[str]:
    """客戶叢集用：平台連不進客戶叢集，Secret 只能放進客戶自己的私有 repo 由 Flux 套用。"""
    _, secret = split_secret_env(llm_result.get("env") or {}, llm_result.get("secret_keys"))
    if not secret:
        return None
    doc = {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
           "metadata": {"name": secret_name(llm_result.get("app_name", "app")), "namespace": namespace},
           "stringData": secret}
    return "---\n" + yaml.dump(doc, allow_unicode=True, default_flow_style=False)


def pvc_name(app_name: str, vol_name: str) -> str:
    return f"{_sanitize_name(app_name)}-{vol_name}"[:63].rstrip("-")


def pod_volumes(app_name: str, volumes: list):
    """回傳 (Pod 的 volumes, 容器的 volumeMounts)。保存空間用 PVC，暫存空間用 emptyDir。"""
    vols, mounts = [], []
    for v in volumes or []:
        if v.get("kind") == "pvc":
            vols.append({"name": v["name"], "persistentVolumeClaim": {"claimName": pvc_name(app_name, v["name"])}})
        else:
            vols.append({"name": v["name"], "emptyDir": {}})
        m = {"name": v["name"], "mountPath": v["mount"]}
        if v.get("read_only"):
            m["readOnly"] = True
        mounts.append(m)
    return vols, mounts


def build_pvc_docs(llm_result: dict, namespace: str) -> list:
    """保存空間（PVC）的設定，一個 volume 一份。標上 zerotouch/app，刪除服務時用它找。"""
    app = _sanitize_name(llm_result.get("app_name", "app"))
    return [{"apiVersion": "v1", "kind": "PersistentVolumeClaim",
             "metadata": {"name": pvc_name(app, v["name"]), "namespace": namespace,
                          "labels": {"app": app, "zerotouch/app": app}},
             "spec": {"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": v.get("size") or "1Gi"}}}}
            for v in (llm_result.get("volumes") or []) if v.get("kind") == "pvc"]


def build_pvc_yaml(llm_result: dict, namespace: str) -> Optional[str]:
    docs = build_pvc_docs(llm_result, namespace)
    if not docs:
        return None
    return "".join("---\n" + yaml.dump(d, allow_unicode=True, default_flow_style=False) for d in docs)


def _build_deployment_yaml(llm_result: dict, namespace: str) -> str:
    """從 LLM 結果生成 Deployment YAML 字串。"""
    app_name  = _sanitize_name(llm_result.get("app_name", "app"))
    image     = llm_result.get("image",    "nginx:latest")
    replicas  = int(llm_result.get("replicas", llm_result.get("pods", 1)))
    port      = int(llm_result.get("port", 80))
    # 資源設定跟本機部署、部署前審查共用同一套規則（agents/cost_agent.recommended_resources）
    from agents.cost_agent import recommended_resources, recommended_env, recommended_probes, container_port
    cport     = container_port(image, port, llm_result.get("container_port"))   # 容器實際監聽的 port，見 container_port 說明
    resources = recommended_resources(image, app_name, llm_result.get("memory"), llm_result.get("cpu"))
    # 平台自動加的（例如 nginx 依 CPU 上限調整 worker 數）先放，使用者自己指定的同名變數優先
    env_vars  = {**recommended_env(image), **(llm_result.get("env") or {})}
    _vols, _mounts = pod_volumes(app_name, llm_result.get("volumes"))
    _extra = {}
    if llm_result.get("command"):
        _extra["command"] = list(llm_result["command"])
    if llm_result.get("args"):
        _extra["args"] = list(llm_result["args"])
    if _mounts:
        _extra["volumeMounts"] = _mounts

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
                        "ports": [{"containerPort": c} for c in container_ports(cport, llm_result.get("extra_ports"))],
                        "resources": resources,
                        "securityContext": {"allowPrivilegeEscalation": False},
                        **recommended_probes(image, cport, llm_result.get("command")),
                        **_extra,
                        "env": env_entries(app_name, env_vars, llm_result.get("secret_keys")),
                    }],
                    **({"volumes": _vols} if _vols else {}),
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
    from agents.cost_agent import container_port
    cport    = container_port(llm_result.get("image", ""), port, llm_result.get("container_port"))
    # 對外 port 跟容器 port 不同時兩個都開：compose 的服務彼此用容器 port 連（web:80），對外用 port（8080）
    ports = [{"name": "app", "protocol": "TCP", "port": cport, "targetPort": cport}]
    if port != cport:
        ports.append({"name": "external", "protocol": "TCP", "port": port, "targetPort": cport})
    ports += extra_service_ports(ports, llm_result.get("extra_ports"))

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
            "ports"   : ports,
            "type"    : "ClusterIP",
        },
    }

    return "---\n" + yaml.dump(doc, allow_unicode=True, default_flow_style=False)


# ══════════════════════════════════════════════════════════════════
# Git 操作
# ══════════════════════════════════════════════════════════════════

def commit_paths(repo_path: str, paths: list, message: str):
    """只提交指定的檔案或目錄，回傳 (commit SHA, 錯誤訊息)。

    2026-10-23 全系統檢查發現：原本用 GitPython 的 index.commit()，它會自己執行 pre-commit hook
    （gitleaks 檢查密碼外洩），在 Windows 上呼叫到的 bash 是 WSL 的，這台電腦的 WSL 沒有 bash，
    於是每一次 commit 都失敗、檔案留在暫存區——10/9 換電腦裝上 hook 之後，本機模式的部署與回滾
    全都沒有留下 Git 紀錄（部署本身成功，所以沒人發現）。改用 git 指令：hook 由 Git for Windows
    自己的 sh 執行，密碼檢查照樣有效。另外：
    - `git commit -- <路徑>` 只提交這次的檔案，不會把使用者暫存區裡其他東西一起帶進去
      （index.commit() 會提交整個暫存區）。
    - 提交失敗就把這些檔案移出暫存區，不留半套的狀態。
    """
    import subprocess

    def git(*args):
        return subprocess.run(["git", "-C", str(repo_path), *args], capture_output=True,
                              encoding="utf-8", errors="replace", timeout=60)

    rels = [str(p) for p in paths]
    add = git("add", "--", *rels)
    if add.returncode != 0:
        return None, (add.stderr or add.stdout).strip()[:300]
    commit = git("commit", "-q", "-m", message, "--", *rels)
    if commit.returncode != 0:
        git("reset", "-q", "--", *rels)
        return None, (commit.stderr or commit.stdout).strip()[:300] or "git commit 失敗"
    sha = git("rev-parse", "HEAD").stdout.strip()
    return sha, None


def _git_commit(repo_path: str, files: list, app_name: str, namespace: str) -> Optional[str]:
    """
    將指定檔案提交。返回 commit SHA，失敗則回傳 None。
    """
    timestamp = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    msg = f"gitops: deploy {app_name} to {namespace} [{timestamp}]"
    sha, err = commit_paths(repo_path, [os.path.abspath(f) for f in files], msg)
    if sha:
        print(f"✅ git commit: {sha[:8]} — {msg}")
    else:
        print(f"⚠️  git commit 失敗：{err}")
    return sha


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
