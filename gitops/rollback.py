"""
gitops/rollback.py
GitOps 回滾執行器 — 找出上一個穩定的 Git commit 並同步到叢集。

策略：
  1. Git 歷史回滾  — 找出指定 app 上一個成功部署的 commit，
                      hard-reset 該 app 的 manifest 目錄並重新提交
  2. Argo CD 回滾  — 呼叫 Argo CD API rollback（回到上一個 revision）
  3. kubectl 回滾  — kubectl rollout undo deployment/<name>（最後手段）

研究報告依據：
    「自動回滾是 GitOps 零接觸部署的關鍵安全網，
     當新版本導致健康狀態惡化時，代理應能自主觸發回滾
     而無需人工介入」
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import re
import subprocess
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

try:
    import git as gitpython
    GIT_AVAILABLE = True
except ImportError:
    GIT_AVAILABLE = False


# K8s 資源名稱（DNS-1123 label）：小寫英數字與連字號、開頭結尾不能是連字號、最長 63。
# 2026-10-08 安全審查：app_name 直接拼進 `manifests/<namespace>/<app_name>` 路徑後
# 交給 kubectl apply，原本完全沒驗證，傳 `../user-別人/app` 就能讓路徑指到其他帳號的
# manifest 目錄（當時剛好被 manifest 寫死的 namespace 欄位擋下，不是設計過的防線）。
# 合法的 K8s 名稱本來就不可能含 `/` 或 `.`，用白名單一次排除所有穿越寫法。
_K8S_NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")


def is_valid_k8s_name(name: str) -> bool:
    return bool(name) and bool(_K8S_NAME_RE.match(name))


def _invalid_target_result(app_name: str, namespace: str) -> Optional[dict]:
    """app_name / namespace 不是合法 K8s 名稱就回傳失敗結果，合法回傳 None。"""
    if not is_valid_k8s_name(app_name):
        return {"ok": False, "strategy": "none",
                "message": f"應用程式名稱不合法：{app_name!r}（只能用小寫英數字與連字號）",
                "details": {}, "timestamp": datetime.utcnow().isoformat()}
    if not is_valid_k8s_name(namespace):
        return {"ok": False, "strategy": "none",
                "message": f"namespace 不合法：{namespace!r}",
                "details": {}, "timestamp": datetime.utcnow().isoformat()}
    return None


def _manifest_dir_within(repo_root: Path, namespace: str, app_name: str) -> Optional[Path]:
    """回傳 `<repo>/manifests/<namespace>/<app_name>`，確認解析後確實落在
    `<repo>/manifests/<namespace>/` 底下（第二層防線，名稱驗證之外再擋一次）；
    不在範圍內回傳 None。"""
    base = (repo_root / "manifests" / namespace).resolve()
    target = (base / app_name).resolve()
    if target.parent != base:
        return None
    return target


# ══════════════════════════════════════════════════════════════════
# 公開 API
# ══════════════════════════════════════════════════════════════════

def rollback(
    app_name  : str,
    namespace : str  = "default",
    repo_path : str  = ".",
    strategy  : str  = "auto",
    dry_run   : bool = False,
) -> dict:
    """
    對指定應用程式執行回滾。

    Args:
        app_name  : 應用程式名稱（對應 Deployment 與 Argo CD App 名稱）
        namespace : K8s namespace
        repo_path : Git 倉庫根目錄
        strategy  : "auto" | "git" | "argocd" | "kubectl"
                    auto = 依序嘗試 argocd → git → kubectl
        dry_run   : True = 只顯示將執行的操作

    Returns:
        {
            "ok"        : bool,
            "strategy"  : str,   # 實際使用的回滾策略
            "message"   : str,
            "details"   : dict,
            "timestamp" : str,
        }
    """
    invalid = _invalid_target_result(app_name, namespace)
    if invalid:
        return invalid

    print(f"\n[rollback] 觸發回滾：{namespace}/{app_name}（策略：{strategy}）"
          + ("  [dry-run]" if dry_run else ""))

    if strategy == "auto":
        # 依序嘗試，第一個「真的成功」的回傳；ok=None（策略不可用，例如 Argo CD
        # 沒部署）或 ok=False（該策略執行但失敗）都要繼續試下一個，不能提早停下
        # ——這裡曾經寫成 `is not False`，把 None 也當「停下來」，導致 Argo CD
        # 回報「不可用」時整個 auto 流程直接結束，git/kubectl 兩個實際上會成功
        # 的策略永遠輪不到。
        for s in ("argocd", "git", "kubectl"):
            result = _run_strategy(s, app_name, namespace, repo_path, dry_run)
            if result["ok"] is True:
                result["strategy"] = s
                result["timestamp"] = datetime.utcnow().isoformat()
                _print_result(result)
                return result

        return {
            "ok"       : False,
            "strategy" : "auto",
            "message"  : "所有回滾策略均失敗，請手動執行 kubectl rollout undo",
            "details"  : {},
            "timestamp": datetime.utcnow().isoformat(),
        }

    result = _run_strategy(strategy, app_name, namespace, repo_path, dry_run)
    result["strategy"]  = strategy
    result["timestamp"] = datetime.utcnow().isoformat()
    _print_result(result)
    return result


def get_rollback_history(
    app_name  : str,
    namespace : str = "default",
    repo_path : str = ".",
    limit     : int = 5,
) -> list:
    """
    取得指定 app 的近期部署歷史（從 Git log 讀取）。

    Returns:
        list of {"sha", "timestamp", "message", "author"}
    """
    if not (is_valid_k8s_name(app_name) and is_valid_k8s_name(namespace)):
        return []
    manifest_dir = Path(repo_path) / "manifests" / namespace / app_name

    if not GIT_AVAILABLE:
        return _kubectl_rollout_history(app_name, namespace)

    try:
        repo   = gitpython.Repo(repo_path, search_parent_directories=True)
        rel    = str(manifest_dir.relative_to(Path(repo.working_dir)))
        commits = list(repo.iter_commits(paths=rel, max_count=limit))

        return [
            {
                "sha"      : c.hexsha[:8],
                "timestamp": datetime.fromtimestamp(c.committed_date).isoformat(),
                "message"  : c.message.strip(),
                "author"   : c.author.name,
            }
            for c in commits
        ]
    except Exception as e:
        print(f"[warn] 無法取得 Git 歷史：{e}")
        return _kubectl_rollout_history(app_name, namespace)


# ══════════════════════════════════════════════════════════════════
# 各策略實作
# ══════════════════════════════════════════════════════════════════

def _run_strategy(
    strategy : str,
    app_name : str,
    namespace: str,
    repo_path: str,
    dry_run  : bool,
) -> dict:
    handlers = {
        "argocd" : _rollback_argocd,
        "git"    : _rollback_git,
        "kubectl": _rollback_kubectl,
    }
    handler = handlers.get(strategy)
    if not handler:
        return {"ok": False, "message": f"未知策略：{strategy}", "details": {}}

    try:
        return handler(app_name, namespace, repo_path, dry_run)
    except Exception as e:
        return {"ok": False, "message": f"{strategy} 回滾時發生錯誤：{e}", "details": {}}


def _rollback_argocd(
    app_name : str,
    namespace: str,
    repo_path: str,
    dry_run  : bool,
) -> dict:
    """透過 Argo CD API 回滾到上一個 revision。"""
    try:
        from gitops.argocd_sync import ArgocdClient
        client = ArgocdClient()

        # 先取得目前狀態
        status = client.get_status(app_name)
        if status.get("ok") is False and "無法連線" in status.get("message", ""):
            return {"ok": None, "message": "Argo CD 不可用", "details": {}}

        try:
            import requests
            from gitops.argocd_sync import _DEFAULT_SERVER, _DEFAULT_TOKEN, _DEFAULT_INSECURE
        except ImportError:
            return {"ok": None, "message": "Argo CD 回滾需要 requests 套件", "details": {}}

        # Argo CD Application 名稱不分帳號，只用 app_name 呼叫 rollback 的話，填別的
        # 帳號的 app 名稱就能回滾別人的服務（2026-10-08 安全審查）——先確認這個
        # Application 的部署目的地 namespace 就是呼叫者自己的 namespace，查不到也拒絕。
        try:
            app_resp = requests.get(
                f"{_DEFAULT_SERVER}/api/v1/applications/{app_name}",
                headers={"Authorization": f"Bearer {_DEFAULT_TOKEN}"},
                verify=not _DEFAULT_INSECURE,
                timeout=10,
            )
            dest_ns = (app_resp.json().get("spec", {}).get("destination", {}).get("namespace")
                       if app_resp.status_code == 200 else None)
        except Exception:
            dest_ns = None
        if dest_ns != namespace:
            return {"ok": False,
                    "message": f"拒絕 Argo CD 回滾：{app_name} 的部署目的地不是 {namespace}",
                    "details": {}}

        if dry_run:
            return {
                "ok"     : True,
                "message": f"[dry-run] 將呼叫 Argo CD rollback API 回滾 {app_name}",
                "details": {},
            }

        # 呼叫 rollback（revision=-1 = 上一個）
        try:
            resp = requests.post(
                f"{_DEFAULT_SERVER}/api/v1/applications/{app_name}/rollback",
                headers={"Authorization": f"Bearer {_DEFAULT_TOKEN}",
                         "Content-Type": "application/json"},
                json={"id": 0},   # id=0 表示回到上一個
                verify=not _DEFAULT_INSECURE,
                timeout=10,
            )
            if resp.status_code in (200, 201):
                return {
                    "ok"     : True,
                    "message": f"Argo CD 已觸發 {app_name} 回滾",
                    "details": {},
                }
        except Exception:
            pass

        return {"ok": None, "message": "Argo CD rollback API 無回應", "details": {}}

    except ImportError:
        return {"ok": None, "message": "argocd_sync 模組不可用", "details": {}}


def _rollback_git(
    app_name : str,
    namespace: str,
    repo_path: str,
    dry_run  : bool,
) -> dict:
    """
    Git 歷史回滾：
    找出 manifest 目錄在倒數第二個 commit 的狀態，
    還原檔案並新增一個 "revert" commit。
    """
    if not GIT_AVAILABLE:
        return {"ok": None, "message": "GitPython 未安裝", "details": {}}

    try:
        repo   = gitpython.Repo(repo_path, search_parent_directories=True)
        repo_root = Path(repo.working_dir).resolve()
        manifest_dir = _manifest_dir_within(repo_root, namespace, app_name)
        if manifest_dir is None:
            return {"ok": False, "message": f"拒絕回滾：路徑超出 manifests/{namespace}/ 範圍", "details": {}}
        rel    = str(manifest_dir.relative_to(repo_root))
        commits = list(repo.iter_commits(paths=rel, max_count=2))

        if len(commits) < 2:
            return {
                "ok"     : False,
                "message": f"沒有足夠的 commit 歷史可以回滾（找到 {len(commits)} 個）",
                "details": {},
            }

        current_sha  = commits[0].hexsha[:8]
        previous_sha = commits[1].hexsha[:8]
        previous_commit = commits[1]

        if dry_run:
            return {
                "ok"     : True,
                "message": f"[dry-run] 將從 {current_sha} 回滾到 {previous_sha}",
                "details": {"from": current_sha, "to": previous_sha},
            }

        # 從上一個 commit 取出檔案——git 物件內部的路徑永遠是用 `/` 分隔（跟
        # 平台無關），但 `rel` 是從 Path.relative_to() 轉成字串，Windows 上會是
        # `manifests\user-rbselftest\rb-selftest` 這種反斜線形式，兩者直接拿去
        # `startswith()` 比對在 Windows 上永遠對不上、迴圈一次都不會進去，實測
        # 發現這台機器上「還原檔案」這一步從沒真的動過任何一個檔案——比對前先
        # 統一轉成 `/` 分隔。
        # `.traverse()` 連目錄本身（Tree 物件）也會一併列出，不只是檔案
        # （Blob）——`rel_posix` 剛好等於目錄路徑本身，`startswith()` 連目錄
        # 那筆也會比對到，試圖把它當檔案寫入就會撞到「路徑其實是目錄」的
        # Permission denied，所以要額外濾掉非 blob 的項目。
        # 比對要以「目錄」為單位：只寫 startswith(rel_posix) 的話，回滾 `app` 會連
        # `app123/` 底下的檔案也一起覆寫（2026-10-08 安全審查時發現）。
        rel_posix = rel.replace("\\", "/")
        for item in previous_commit.tree.traverse():
            if hasattr(item, "path") and item.path.startswith(rel_posix + "/") and item.type == "blob":
                blob_path = Path(repo.working_dir) / item.path
                blob_path.parent.mkdir(parents=True, exist_ok=True)
                blob_path.write_bytes(item.data_stream.read())

        # 把還原後的 manifest 真的套用到叢集——只改 git 歷史不會讓任何一個
        # Pod 真的換版本，這一步才是「回滾」真正發生的地方
        apply_proc = subprocess.run(
            ["kubectl", "apply", "-f", str(Path(repo.working_dir) / rel), "-n", namespace],
            capture_output=True, text=True, timeout=30,
        )
        if apply_proc.returncode != 0:
            return {
                "ok"     : False,
                "message": f"manifest 已還原但套用到叢集失敗：{apply_proc.stderr.strip()[:300]}",
                "details": {"from": current_sha, "to": previous_sha},
            }

        # 提交還原
        repo.index.add([rel])
        timestamp = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        msg = f"gitops: revert {app_name} to {previous_sha} [{timestamp}]"
        new_commit = repo.index.commit(msg)

        return {
            "ok"     : True,
            "message": f"已回滾 {app_name}：{current_sha} → {previous_sha}，新 commit {new_commit.hexsha[:8]}",
            "details": {
                "from_sha"  : current_sha,
                "to_sha"    : previous_sha,
                "new_commit": new_commit.hexsha[:8],
            },
        }

    except Exception as e:
        return {"ok": False, "message": f"Git 回滾失敗：{e}", "details": {}}


def _rollback_kubectl(
    app_name : str,
    namespace: str,
    repo_path: str,
    dry_run  : bool,
) -> dict:
    """使用 kubectl rollout undo 回滾 Deployment。"""
    cmd = ["kubectl", "rollout", "undo",
           f"deployment/{app_name}", "-n", namespace]

    if dry_run:
        return {
            "ok"     : True,
            "message": f"[dry-run] 將執行：{' '.join(cmd)}",
            "details": {"cmd": cmd},
        }

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if proc.returncode == 0:
            return {
                "ok"     : True,
                "message": f"kubectl rollout undo 成功：{proc.stdout.strip()}",
                "details": {"stdout": proc.stdout.strip()},
            }
        return {
            "ok"     : False,
            "message": f"kubectl rollout undo 失敗：{proc.stderr.strip()}",
            "details": {"stderr": proc.stderr.strip()},
        }
    except FileNotFoundError:
        return {"ok": None, "message": "kubectl 未安裝", "details": {}}
    except subprocess.TimeoutExpired:
        return {"ok": False, "message": "kubectl rollout undo 逾時（30s）", "details": {}}


def _kubectl_rollout_history(app_name: str, namespace: str) -> list:
    """從 kubectl rollout history 取得歷史（GitPython 不可用時的備案）。"""
    try:
        proc = subprocess.run(
            ["kubectl", "rollout", "history",
             f"deployment/{app_name}", "-n", namespace],
            capture_output=True, text=True, timeout=10,
        )
        if proc.returncode == 0:
            lines = [l for l in proc.stdout.strip().split("\n") if l.strip()]
            return [{"sha": "kubectl", "message": l, "timestamp": "", "author": ""}
                    for l in lines[1:]]  # 跳過 header
    except Exception:
        pass
    return []


# ══════════════════════════════════════════════════════════════════
# 工具函式
# ══════════════════════════════════════════════════════════════════

def _print_result(result: dict):
    icon = "[OK]" if result["ok"] else ("[WARN]" if result["ok"] is None else "[FAIL]")
    print(f"   {icon} [{result['strategy']}] {result['message']}")


# ══════════════════════════════════════════════════════════════════
# CLI 測試入口
# ══════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import argparse

    print("=" * 55)
    print("  gitops/rollback.py — 回滾執行器")
    print("=" * 55)

    parser = argparse.ArgumentParser(description="GitOps 回滾工具")
    parser.add_argument("app_name",  help="應用程式名稱")
    parser.add_argument("--namespace", "-n", default="default")
    parser.add_argument("--strategy", choices=["auto","argocd","git","kubectl"], default="auto")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--history", action="store_true", help="只顯示歷史，不回滾")
    args = parser.parse_args()

    if args.history:
        history = get_rollback_history(args.app_name, args.namespace)
        print(f"\n  {args.app_name} 部署歷史：")
        for h in history:
            print(f"  [{h['sha']}] {h['timestamp']}  {h['message'][:60]}")
    else:
        result = rollback(
            args.app_name,
            namespace=args.namespace,
            strategy=args.strategy,
            dry_run=args.dry_run,
        )
        print(f"\n最終結果：{'成功' if result['ok'] else '失敗'}")
        print(f"訊息：{result['message']}")
