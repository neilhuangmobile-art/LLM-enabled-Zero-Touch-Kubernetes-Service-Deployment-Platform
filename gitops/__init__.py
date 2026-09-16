# gitops/__init__.py
# GitOps 模組 - 對外介面

from gitops.manifest_writer import write_manifest
from gitops.argocd_sync     import sync_app, get_app_status, ArgocdClient
from gitops.rollback        import rollback, get_rollback_history


def deploy(
    llm_result : dict,
    namespace  : str  = "default",
    repo_path  : str  = ".",
    sync       : bool = True,
    dry_run    : bool = False,
) -> dict:
    """
    完整 GitOps 部署流程：寫入 manifest → git commit → Argo CD sync。

    Args:
        llm_result : ask_llama() 回傳的 dict
        namespace  : 目標 K8s namespace
        repo_path  : Git 倉庫根目錄
        sync       : True = 寫入後觸發 Argo CD 同步
        dry_run    : True = 只顯示操作，不實際執行

    Returns:
        {
            "ok"         : bool,
            "manifest"   : dict,   # write_manifest 結果
            "sync"       : dict,   # sync_app 結果（若 sync=True）
            "message"    : str,
        }
    """
    app_name = llm_result.get("app_name", "app")

    # 1. 寫入 manifest 並 commit
    manifest_result = write_manifest(
        llm_result,
        repo_path=repo_path,
        namespace=namespace,
        dry_run=dry_run,
        commit=True,
    )

    if not manifest_result["ok"]:
        return {
            "ok"      : False,
            "manifest": manifest_result,
            "sync"    : None,
            "message" : f"manifest 寫入失敗：{manifest_result['message']}",
        }

    # 2. Argo CD 同步
    sync_result = None
    if sync and not dry_run:
        sync_result = sync_app(app_name)
    elif sync and dry_run:
        sync_result = {"ok": True, "message": f"[dry-run] 將同步 Argo CD app: {app_name}"}

    overall_ok = manifest_result["ok"] and (
        sync_result is None or sync_result.get("ok") is not False
    )

    return {
        "ok"      : overall_ok,
        "manifest": manifest_result,
        "sync"    : sync_result,
        "message" : (f"部署 {app_name} 完成" if overall_ok
                     else f"部署 {app_name} 部分失敗"),
    }
