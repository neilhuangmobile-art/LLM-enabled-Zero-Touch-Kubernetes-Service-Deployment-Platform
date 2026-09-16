"""
gitops/argocd_sync.py
透過 Argo CD API 觸發應用程式同步，並查詢同步狀態。

功能：
  - 觸發 Argo CD Application sync（等同 argocd app sync <name>）
  - 輪詢等待同步完成（configurable timeout）
  - 取得 Application 健康狀態與同步狀態
  - Argo CD 不可用時優雅降級（提供等效 CLI 指令）

Argo CD API 文件：https://argo-cd.readthedocs.io/en/stable/developer-guide/api-docs/

研究報告依據：
    「Argo CD 作為 GitOps 控制器，持續比對 Git 倉庫的期望狀態
     與叢集的實際狀態，自動或手動觸發同步以消除差異」
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import time
import json
from typing import Optional

try:
    import requests as _requests
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False


# ══════════════════════════════════════════════════════════════════
# 設定
# ══════════════════════════════════════════════════════════════════

# 從環境變數讀取（也可在建立 ArgocdClient 時傳入）
_DEFAULT_SERVER   = os.environ.get("ARGOCD_SERVER",   "https://localhost:8080")
_DEFAULT_TOKEN    = os.environ.get("ARGOCD_TOKEN",    "")
_DEFAULT_INSECURE = os.environ.get("ARGOCD_INSECURE", "true").lower() == "true"


# ══════════════════════════════════════════════════════════════════
# 公開 API
# ══════════════════════════════════════════════════════════════════

class ArgocdClient:
    """
    Argo CD REST API 客戶端。

    使用方式：
        client = ArgocdClient(server="https://argocd.example.com", token="xxx")
        result = client.sync("my-app")
    """

    def __init__(
        self,
        server  : str  = _DEFAULT_SERVER,
        token   : str  = _DEFAULT_TOKEN,
        insecure: bool = _DEFAULT_INSECURE,
    ):
        self.server   = server.rstrip("/")
        self.token    = token
        self.insecure = insecure

    # ── headers ──────────────────────────────────────────────────

    @property
    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    # ── 同步操作 ─────────────────────────────────────────────────

    def sync(
        self,
        app_name : str,
        timeout  : int  = 120,
        revision : str  = "HEAD",
        prune    : bool = False,
    ) -> dict:
        """
        觸發 Argo CD Application 同步並等待完成。

        Args:
            app_name : Argo CD Application 名稱
            timeout  : 等待同步完成的最長秒數
            revision : 同步的 Git revision（預設 HEAD）
            prune    : 是否刪除 Git 中不存在的資源

        Returns:
            {
                "ok"           : bool,
                "app_name"     : str,
                "sync_status"  : str,   # "Synced" / "OutOfSync" / "Unknown"
                "health_status": str,   # "Healthy" / "Degraded" / "Progressing"
                "message"      : str,
            }
        """
        if not REQUESTS_AVAILABLE:
            return _no_requests_result(app_name)

        print(f"🔄 觸發 Argo CD 同步：{app_name} (revision={revision})")

        # 1. 發送 sync 請求
        try:
            resp = _requests.post(
                f"{self.server}/api/v1/applications/{app_name}/sync",
                headers=self._headers,
                json={"revision": revision, "prune": prune},
                verify=not self.insecure,
                timeout=10,
            )
        except Exception as e:
            return _connection_error_result(app_name, e)

        if resp.status_code not in (200, 201):
            return {
                "ok"           : False,
                "app_name"     : app_name,
                "sync_status"  : "Unknown",
                "health_status": "Unknown",
                "message"      : f"Sync 請求失敗 HTTP {resp.status_code}：{resp.text[:200]}",
            }

        # 2. 輪詢等待同步完成
        return self._wait_for_sync(app_name, timeout)

    def get_status(self, app_name: str) -> dict:
        """
        查詢 Application 目前的同步與健康狀態。
        """
        if not REQUESTS_AVAILABLE:
            return _no_requests_result(app_name)

        try:
            resp = _requests.get(
                f"{self.server}/api/v1/applications/{app_name}",
                headers=self._headers,
                verify=not self.insecure,
                timeout=10,
            )
        except Exception as e:
            return _connection_error_result(app_name, e)

        if resp.status_code != 200:
            return {
                "ok"           : False,
                "app_name"     : app_name,
                "sync_status"  : "Unknown",
                "health_status": "Unknown",
                "message"      : f"查詢失敗 HTTP {resp.status_code}",
            }

        data   = resp.json()
        status = data.get("status", {})

        return _parse_app_status(app_name, status)

    def list_apps(self) -> list:
        """列出所有 Argo CD Application。"""
        if not REQUESTS_AVAILABLE:
            return []
        try:
            resp = _requests.get(
                f"{self.server}/api/v1/applications",
                headers=self._headers,
                verify=not self.insecure,
                timeout=10,
            )
            if resp.status_code == 200:
                items = resp.json().get("items", []) or []
                return [
                    {
                        "name"  : app["metadata"]["name"],
                        "sync"  : app.get("status", {}).get("sync",   {}).get("status", "?"),
                        "health": app.get("status", {}).get("health", {}).get("status", "?"),
                    }
                    for app in items
                ]
        except Exception:
            pass
        return []

    # ── 內部：輪詢 ───────────────────────────────────────────────

    def _wait_for_sync(self, app_name: str, timeout: int) -> dict:
        """輪詢直到 sync_status == Synced 或 timeout。"""
        deadline = time.time() + timeout
        dots     = 0

        while time.time() < deadline:
            result = self.get_status(app_name)
            sync   = result.get("sync_status",   "Unknown")
            health = result.get("health_status", "Unknown")

            if sync == "Synced" and health in ("Healthy", "Progressing"):
                print(f"\n✅ 同步完成：{app_name} — {sync} / {health}")
                return {**result, "ok": True}

            if health == "Degraded":
                print(f"\n❌ 同步後應用不健康：{app_name} — {health}")
                return {**result, "ok": False,
                        "message": f"應用同步後狀態 Degraded，請檢查 Pod 狀態"}

            # 顯示進度
            dots = (dots + 1) % 4
            print(f"\r   等待同步{'.' * dots}   sync={sync} health={health}", end="", flush=True)
            time.sleep(5)

        print(f"\n⚠️  同步逾時（{timeout}s）")
        return {
            "ok"           : False,
            "app_name"     : app_name,
            "sync_status"  : "Unknown",
            "health_status": "Unknown",
            "message"      : f"等待同步超過 {timeout} 秒，請手動確認：argocd app get {app_name}",
        }


# ══════════════════════════════════════════════════════════════════
# 便利函式（不需要建立 client 物件）
# ══════════════════════════════════════════════════════════════════

def sync_app(
    app_name : str,
    server   : str = _DEFAULT_SERVER,
    token    : str = _DEFAULT_TOKEN,
    timeout  : int = 120,
) -> dict:
    """快速同步一個 Argo CD Application。"""
    return ArgocdClient(server=server, token=token).sync(app_name, timeout=timeout)


def get_app_status(
    app_name : str,
    server   : str = _DEFAULT_SERVER,
    token    : str = _DEFAULT_TOKEN,
) -> dict:
    """快速查詢 Application 狀態。"""
    return ArgocdClient(server=server, token=token).get_status(app_name)


# ══════════════════════════════════════════════════════════════════
# 工具函式
# ══════════════════════════════════════════════════════════════════

def _parse_app_status(app_name: str, status: dict) -> dict:
    sync_status   = status.get("sync",   {}).get("status",  "Unknown")
    health_status = status.get("health", {}).get("status",  "Unknown")
    message       = status.get("health", {}).get("message", "")

    return {
        "ok"           : sync_status == "Synced",
        "app_name"     : app_name,
        "sync_status"  : sync_status,
        "health_status": health_status,
        "message"      : message or f"{sync_status} / {health_status}",
    }


def _no_requests_result(app_name: str) -> dict:
    return {
        "ok"           : None,
        "app_name"     : app_name,
        "sync_status"  : "Unknown",
        "health_status": "Unknown",
        "message"      : (
            "requests 套件未安裝（pip install requests）。\n"
            f"請手動執行：argocd app sync {app_name}"
        ),
    }


def _connection_error_result(app_name: str, error: Exception) -> dict:
    return {
        "ok"           : False,
        "app_name"     : app_name,
        "sync_status"  : "Unknown",
        "health_status": "Unknown",
        "message"      : (
            f"無法連線 Argo CD：{error}\n"
            f"請確認 ARGOCD_SERVER 與 ARGOCD_TOKEN 環境變數，\n"
            f"或手動執行：argocd app sync {app_name}"
        ),
    }


# ══════════════════════════════════════════════════════════════════
# CLI 測試入口
# ══════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("=" * 55)
    print("  gitops/argocd_sync.py — Argo CD 同步測試")
    print("=" * 55)

    # 列出所有 Application
    client = ArgocdClient()
    apps   = client.list_apps()

    if apps:
        print(f"\n找到 {len(apps)} 個 Application：")
        for a in apps:
            icon = "✅" if a["sync"] == "Synced" else "⚠️ "
            print(f"  {icon} {a['name']} — sync={a['sync']} health={a['health']}")
    else:
        print("\n⚠️  無法連線 Argo CD 或沒有 Application")
        print("    設定環境變數後重試：")
        print("    export ARGOCD_SERVER=https://your-argocd-server")
        print("    export ARGOCD_TOKEN=your-api-token")
        print("\n    等效 CLI 指令：")
        print("    argocd app list")
        print("    argocd app sync <app-name>")
