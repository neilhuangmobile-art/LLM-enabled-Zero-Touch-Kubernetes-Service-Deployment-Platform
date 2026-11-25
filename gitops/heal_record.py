"""
gitops/heal_record.py
Healer 自動修復（含驗證失敗後的退回）寫進部署紀錄 repo（2026-11-25，使用者決定）。

原本本機修復只改叢集、不寫 GitOps（Healer 方案 P10）：部署紀錄 repo 跟叢集實際設定不一致，
「換回上一版」也看不到 Healer 改過什麼。現在修復成功 patch 之後，把叢集上這個服務的
映像檔、resources、探針寫回 `manifests/<namespace>/<app>/deployment.yaml` 並提交。

- 提交訊息用固定模板 `healer: <app> <動作代碼>`，不放模型或容器產生的文字。
- 檔案不存在（例如評估用的 namespace、還沒有部署紀錄的服務）就略過，不建立新檔。
- 名稱檢查與路徑範圍檢查沿用 gitops.rollback。
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional

import yaml

_FIELDS = ("image", "resources", "livenessProbe", "readinessProbe", "startupProbe")
# 階段 3.1「退回最近的更新」換掉整個 Pod 範本，環境變數、指令也可能跟著變；退回（rollback）同樣要整個寫回
_FULL_FIELDS = _FIELDS + ("env", "envFrom", "command", "args")
_FULL_ACTIONS = {"revert_recent_change", "rollback"}


def _live_containers(namespace: str, name: str, deploy=None, fields=_FIELDS) -> Dict[str, Dict]:
    """叢集上 Deployment 的容器設定，轉成和 YAML 一樣的 camelCase dict。"""
    from kubernetes import client as k8s_client
    api = k8s_client.ApiClient()
    if deploy is None:
        from healer.remediate import _load_k8s_config
        _load_k8s_config()
        deploy = k8s_client.AppsV1Api().read_namespaced_deployment(name, namespace)
    out = {}
    for c in deploy.spec.template.spec.containers or []:
        d = api.sanitize_for_serialization(c)
        out[c.name] = {k: d.get(k) for k in fields}
    return out


def apply_to_manifest(text: str, name: str, live: Dict[str, Dict], fields=_FIELDS) -> Optional[str]:
    """把 live 的容器設定套到 YAML 裡名稱是 name 的 Deployment；沒有變動回傳 None。"""
    docs = list(yaml.safe_load_all(text))
    changed = False
    for doc in docs:
        if not isinstance(doc, dict) or doc.get("kind") != "Deployment" or (doc.get("metadata") or {}).get("name") != name:
            continue
        for c in (((doc.get("spec") or {}).get("template") or {}).get("spec") or {}).get("containers") or []:
            new = live.get(c.get("name"))
            if not new:
                continue
            for k in fields:
                v = new.get(k)
                if v is None:
                    if k in c:
                        c.pop(k)
                        changed = True
                elif c.get(k) != v:
                    c[k] = v
                    changed = True
    if not changed:
        return None
    return yaml.safe_dump_all([d for d in docs if d is not None], allow_unicode=True, sort_keys=False,
                              default_flow_style=False, explicit_start=True)


def record_heal(namespace: str, name: str, action: str, repo_path: Optional[str] = None, deploy=None) -> Dict:
    """回傳 {"ok": bool, "sha": ..., "skipped": 原因, "error": ...}。"""
    from gitops.rollback import _manifest_dir_within, is_valid_k8s_name
    from gitops.manifest_writer import commit_paths, ensure_gitops_repo
    if not (is_valid_k8s_name(name) and is_valid_k8s_name(namespace)):
        return {"ok": False, "skipped": "名稱不合法"}
    if not repo_path:
        from core.config import GITOPS_REPO_PATH
        repo_path = GITOPS_REPO_PATH
    ensure_gitops_repo(repo_path)
    target = _manifest_dir_within(Path(repo_path), namespace, name)
    if target is None:
        return {"ok": False, "skipped": "路徑超出 manifests 範圍"}
    path = target / "deployment.yaml"
    if not path.is_file():
        return {"ok": False, "skipped": "這個服務沒有部署紀錄檔"}
    fields = _FULL_FIELDS if action in _FULL_ACTIONS else _FIELDS
    new_text = apply_to_manifest(path.read_text(encoding="utf-8"), name,
                                 _live_containers(namespace, name, deploy, fields), fields)
    if new_text is None:
        return {"ok": True, "skipped": "部署紀錄已經和叢集一致"}
    path.write_text(new_text, encoding="utf-8")
    safe_action = action if action.replace("_", "").isalnum() else "heal"
    sha, err = commit_paths(repo_path, [str(path)], f"healer: {name} {safe_action}")
    if err:
        return {"ok": False, "error": err}
    return {"ok": True, "sha": sha}
