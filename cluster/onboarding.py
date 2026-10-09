"""
cluster/onboarding.py
產生客戶要執行的 PowerShell 安裝腳本，以及套用到客戶叢集的安裝包（bundle.yaml）。

2026-10-09 新增（客戶自帶叢集第 3 階段）。安裝包內容（全部在客戶專屬 namespace 裡）：
    Namespace（Pod Security baseline：叢集本身拒絕特權容器、hostPath、hostNetwork）
    zt-flux-applier ServiceAccount + RoleBinding(admin，只限這個 namespace)
        → Flux 用它的身分套用設定（impersonation），就算平台被攻破，損害也只在這個 namespace
    zt-git-auth Secret（唯讀 deploy key + GitHub 官方 known_hosts）
    GitRepository / Kustomization（名稱 zt-apps，每 30 秒同步 ./apps）
    zt-reporter：唯讀回報器（ConfigMap 放原始碼、Secret 放回報 token、Deployment 跑官方 python 映像檔）
    zt-reporter-nodes-<namespace>：唯一的叢集層級權限，只能讀節點的 CPU/記憶體容量

用 Python dict → yaml.safe_dump 產生，不用字串拼接：私鑰、token 等內容不可能破壞 YAML 結構
或注入額外的資源。
"""
import hashlib
import os
import re
from typing import Dict

import yaml

FLUX_VERSION = "v2.9.6"
FLUX_INSTALL_URL = f"https://github.com/fluxcd/flux2/releases/download/{FLUX_VERSION}/install.yaml"
REPORTER_IMAGE = "python:3.12-slim"
REPORTER_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent", "reporter.py")
_NS_RE = re.compile(r"[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?")
_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{16,128}")


def _check(namespace: str, base_url: str):
    if not _NS_RE.fullmatch(namespace or ""):
        raise ValueError("invalid namespace")
    if not re.fullmatch(r"https?://[A-Za-z0-9.\-:\[\]]+", base_url or ""):
        raise ValueError("invalid base url")


def uninstall_commands(namespace: str) -> str:
    return (f"kubectl delete namespace {namespace}\n"
            f"kubectl delete clusterrolebinding,clusterrole zt-reporter-nodes-{namespace}")


def build_bundle(namespace: str, owner: str, base_url: str, repo_ssh_url: str,
                 deploy_key: Dict[str, str], report_token: str) -> str:
    _check(namespace, base_url)
    with open(REPORTER_SRC, encoding="utf-8") as f:
        reporter_src = f.read()
    labels = {"app.kubernetes.io/managed-by": "zerotouch", "zerotouch/owner-hash": namespace[-16:]}
    ssh_url = repo_ssh_url
    if ssh_url.startswith("git@github.com:"):
        ssh_url = "ssh://git@github.com/" + ssh_url[len("git@github.com:"):]
    nodes_role = f"zt-reporter-nodes-{namespace}"
    # 回報器程式碼或 token 一變，Pod 範本的這個雜湊就跟著變，Kubernetes 會自動滾動重啟回報器。
    # 2026-10-09 實測：沒有這個的話，客戶重跑安裝指令後 ConfigMap/Secret 雖然更新了，但跑著的
    # 回報器還是舊程式碼、舊 token → 被平台拒絕（401）→ 退避成 5 分鐘一次，看起來像叢集離線。
    rollout_hash = hashlib.sha256((reporter_src + "|" + report_token).encode()).hexdigest()[:16]

    docs = [
        {"apiVersion": "v1", "kind": "Namespace",
         "metadata": {"name": namespace, "labels": {
             **labels,
             "pod-security.kubernetes.io/enforce": "baseline",
             "pod-security.kubernetes.io/enforce-version": "latest",
             "pod-security.kubernetes.io/warn": "restricted"}}},
        # ── Flux 套用設定時使用的身分：只有這個 namespace 的 admin ──
        {"apiVersion": "v1", "kind": "ServiceAccount",
         "metadata": {"name": "zt-flux-applier", "namespace": namespace, "labels": labels}},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding",
         "metadata": {"name": "zt-flux-applier", "namespace": namespace, "labels": labels},
         "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": "admin"},
         "subjects": [{"kind": "ServiceAccount", "name": "zt-flux-applier", "namespace": namespace}]},
        {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
         "metadata": {"name": "zt-git-auth", "namespace": namespace, "labels": labels},
         "stringData": {"identity": deploy_key["private_key"], "known_hosts": deploy_key["known_hosts"]}},
        {"apiVersion": "source.toolkit.fluxcd.io/v1", "kind": "GitRepository",
         "metadata": {"name": "zt-apps", "namespace": namespace, "labels": labels},
         "spec": {"interval": "30s", "url": ssh_url, "ref": {"branch": "main"},
                  "secretRef": {"name": "zt-git-auth"}}},
        {"apiVersion": "kustomize.toolkit.fluxcd.io/v1", "kind": "Kustomization",
         "metadata": {"name": "zt-apps", "namespace": namespace, "labels": labels},
         "spec": {"interval": "30s", "path": "./apps", "prune": True, "wait": False,
                  "targetNamespace": namespace, "serviceAccountName": "zt-flux-applier",
                  "sourceRef": {"kind": "GitRepository", "name": "zt-apps"}, "timeout": "2m"}},
        # ── 唯讀回報器 ──
        {"apiVersion": "v1", "kind": "ServiceAccount",
         "metadata": {"name": "zt-reporter", "namespace": namespace, "labels": labels}},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role",
         "metadata": {"name": "zt-reporter-readonly", "namespace": namespace, "labels": labels},
         "rules": [
             {"apiGroups": [""], "resources": ["pods", "pods/log", "events", "services"],
              "verbs": ["get", "list"]},
             {"apiGroups": ["apps"], "resources": ["deployments", "replicasets"], "verbs": ["get", "list"]},
             {"apiGroups": [""], "resources": ["configmaps"], "resourceNames": ["zerotouch-info"],
              "verbs": ["get"]},
             {"apiGroups": ["source.toolkit.fluxcd.io"], "resources": ["gitrepositories"],
              "verbs": ["get", "list"]},
             {"apiGroups": ["kustomize.toolkit.fluxcd.io"], "resources": ["kustomizations"],
              "verbs": ["get", "list"]},
             {"apiGroups": ["metrics.k8s.io"], "resources": ["pods"], "verbs": ["get", "list"]}]},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding",
         "metadata": {"name": "zt-reporter-readonly", "namespace": namespace, "labels": labels},
         "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": "zt-reporter-readonly"},
         "subjects": [{"kind": "ServiceAccount", "name": "zt-reporter", "namespace": namespace}]},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRole",
         "metadata": {"name": nodes_role, "labels": labels},
         "rules": [{"apiGroups": [""], "resources": ["nodes"], "verbs": ["get", "list"]}]},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRoleBinding",
         "metadata": {"name": nodes_role, "labels": labels},
         "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": nodes_role},
         "subjects": [{"kind": "ServiceAccount", "name": "zt-reporter", "namespace": namespace}]},
        {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
         "metadata": {"name": "zt-reporter-token", "namespace": namespace, "labels": labels},
         "stringData": {"token": report_token}},
        {"apiVersion": "v1", "kind": "ConfigMap",
         "metadata": {"name": "zt-reporter-src", "namespace": namespace, "labels": labels},
         "data": {"reporter.py": reporter_src}},
        {"apiVersion": "apps/v1", "kind": "Deployment",
         "metadata": {"name": "zt-reporter", "namespace": namespace, "labels": {**labels, "app": "zt-reporter"}},
         "spec": {"replicas": 1, "selector": {"matchLabels": {"app": "zt-reporter"}},
                  "template": {
                      "metadata": {"labels": {**labels, "app": "zt-reporter"},
                                   "annotations": {"zerotouch/reporter-rollout": rollout_hash}},
                      "spec": {
                          "serviceAccountName": "zt-reporter",
                          "securityContext": {"runAsNonRoot": True, "runAsUser": 65534, "runAsGroup": 65534,
                                              "seccompProfile": {"type": "RuntimeDefault"}},
                          "containers": [{
                              "name": "reporter", "image": REPORTER_IMAGE,
                              "command": ["python", "-u", "-B", "/app/reporter.py"],
                              "env": [{"name": "ZT_NAMESPACE", "value": namespace},
                                      {"name": "ZT_REPORT_URL", "value": base_url + "/agent/report"},
                                      {"name": "ZT_INTERVAL", "value": "15"},
                                      {"name": "PYTHONDONTWRITEBYTECODE", "value": "1"}],
                              "resources": {"requests": {"cpu": "10m", "memory": "32Mi"},
                                            "limits": {"cpu": "100m", "memory": "96Mi"}},
                              "securityContext": {"allowPrivilegeEscalation": False,
                                                  "readOnlyRootFilesystem": True,
                                                  "capabilities": {"drop": ["ALL"]}},
                              "volumeMounts": [{"name": "src", "mountPath": "/app", "readOnly": True},
                                               {"name": "token", "mountPath": "/etc/zt", "readOnly": True}]}],
                          "volumes": [{"name": "src", "configMap": {"name": "zt-reporter-src"}},
                                      {"name": "token", "secret": {"secretName": "zt-reporter-token"}}]}}}},
    ]
    header = (f"# ZeroTouch 安裝包：{owner} 的叢集設定（namespace {namespace}）\n"
              f"# 解除安裝：\n# " + uninstall_commands(namespace).replace("\n", "\n# ") + "\n")
    return header + yaml.safe_dump_all(docs, allow_unicode=True, sort_keys=False)


def build_install_script(base_url: str, token: str, namespace: str) -> str:
    """PowerShell 安裝腳本。整段包在一個函式裡：失敗時 return 而不是 exit，
    不會把使用者的 PowerShell 視窗關掉。每一步都回報進度，讓網頁知道卡在哪裡。"""
    _check(namespace, base_url)
    if not _TOKEN_RE.fullmatch(token or ""):
        raise ValueError("invalid token")
    return _PS_TEMPLATE.replace("{{BASE}}", base_url).replace("{{TOKEN}}", token) \
        .replace("{{NS}}", namespace).replace("{{FLUX_URL}}", FLUX_INSTALL_URL) \
        .replace("{{FLUX_VERSION}}", FLUX_VERSION)


_PS_TEMPLATE = r'''# ZeroTouch 叢集連線安裝腳本（namespace {{NS}}）
# 這個腳本會：檢查 kubectl 與叢集 → 安裝 Flux {{FLUX_VERSION}}（若尚未安裝）→ 套用平台設定 → 等回報程式啟動
# 每一步的結果會回報給 ZeroTouch 網站，讓網頁告訴你下一步怎麼做。
function Install-ZeroTouch {
    $ErrorActionPreference = 'Continue'
    $ZtBase = '{{BASE}}'; $ZtToken = '{{TOKEN}}'; $ZtNs = '{{NS}}'
    function Send-ZtProgress([string]$Step, [bool]$Ok, [string]$Detail) {
        try {
            if ($Detail.Length -gt 400) { $Detail = $Detail.Substring($Detail.Length - 400) }
            $body = @{ step = $Step; ok = $Ok; detail = $Detail } | ConvertTo-Json -Compress
            Invoke-RestMethod -Method Post -Uri "$ZtBase/onboard/$ZtToken/progress" -TimeoutSec 10 `
                -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($body)) | Out-Null
        } catch { }
    }
    function Show-ZtFail([string]$Step, [string]$Msg, [string]$Detail) {
        Write-Host ""
        Write-Host "X $Msg" -ForegroundColor Red
        if ($Detail) { Write-Host $Detail -ForegroundColor DarkGray }
        Send-ZtProgress $Step $false $Detail
        Write-Host "請回到 ZeroTouch 網頁按「繼續」，會告訴你怎麼處理。" -ForegroundColor Yellow
    }
    # PowerShell 5.1 會把 kubectl 的錯誤輸出包成 NativeCommandError（一堆紅字 At line…），用 "$_" 只取訊息本身
    function Invoke-Kubectl { $out = & kubectl @args 2>&1 | ForEach-Object { "$_" } | Out-String; return @{ code = $LASTEXITCODE; out = $out.Trim() } }

    Write-Host "== ZeroTouch 叢集連線 ==" -ForegroundColor Cyan
    Send-ZtProgress 'start' $true ''

    Write-Host "[1/5] 檢查 kubectl ..."
    if (-not (Get-Command kubectl -ErrorAction SilentlyContinue)) {
        Show-ZtFail 'kubectl' '找不到 kubectl。請安裝並開啟 Docker Desktop。' ''
        return
    }
    Send-ZtProgress 'kubectl' $true ''

    Write-Host "[2/5] 檢查 Kubernetes 叢集 ..."
    $r = Invoke-Kubectl cluster-info --request-timeout=8s
    if ($r.code -ne 0) {
        Show-ZtFail 'cluster' '連不上 Kubernetes 叢集。請在 Docker Desktop 開啟 Kubernetes。' $r.out
        return
    }
    $ctx = (Invoke-Kubectl config current-context).out
    Write-Host "    使用叢集：$ctx"
    Send-ZtProgress 'cluster' $true "context=$ctx"

    Write-Host "[3/5] 安裝 Flux（叢集原本有就略過）..."
    $has = Invoke-Kubectl -n flux-system get deployment source-controller kustomize-controller --request-timeout=8s
    if ($has.code -eq 0) {
        Write-Host "    已經有 Flux，略過安裝（不會更動你現有的 Flux 版本）"
        Send-ZtProgress 'flux' $true 'already installed'
    } else {
        $r = Invoke-Kubectl apply -f '{{FLUX_URL}}'
        if ($r.code -ne 0) { Show-ZtFail 'flux' '安裝 Flux 失敗，請重新執行同一行指令。' $r.out; return }
        Send-ZtProgress 'flux' $true ''
    }
    Write-Host "    等待 Flux 啟動：要下載映像檔，網路慢時最多可能要 10 分鐘，請不要關閉視窗…"
    $r = Invoke-Kubectl wait --for=condition=established --timeout=120s crd/gitrepositories.source.toolkit.fluxcd.io crd/kustomizations.kustomize.toolkit.fluxcd.io
    if ($r.code -eq 0) { $r = Invoke-Kubectl -n flux-system rollout status deployment/source-controller --timeout=600s }
    if ($r.code -eq 0) { $r = Invoke-Kubectl -n flux-system rollout status deployment/kustomize-controller --timeout=600s }
    if ($r.code -ne 0) {
        $pods = (Invoke-Kubectl -n flux-system get pods --no-headers).out
        Show-ZtFail 'crds' 'Flux 沒有在 10 分鐘內啟動，多半是映像檔下載太慢。請重新啟動 Docker Desktop 後重新執行同一行指令。' "$($r.out)`n$pods"
        return
    }
    Send-ZtProgress 'crds' $true ''

    Write-Host "[4/5] 套用 ZeroTouch 設定到 namespace $ZtNs ..."
    $r = Invoke-Kubectl apply -f "$ZtBase/onboard/$ZtToken/bundle.yaml"
    if ($r.code -ne 0) { Show-ZtFail 'bundle' '套用平台設定失敗（指令可能已過期）。' $r.out; return }
    Send-ZtProgress 'bundle' $true ''

    Write-Host "[5/5] 等待狀態回報程式啟動（第一次要下載映像檔，約 1~3 分鐘）..."
    $r = Invoke-Kubectl -n $ZtNs rollout status deployment/zt-reporter --timeout=600s
    if ($r.code -ne 0) { Show-ZtFail 'reporter' '回報程式還沒啟動完成。' $r.out; return }
    Send-ZtProgress 'done' $true ''

    Write-Host ""
    Write-Host "完成！請回到 ZeroTouch 網頁按「繼續」。" -ForegroundColor Green
}
Install-ZeroTouch
'''
