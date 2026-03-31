"""
0_touch_generate_pods.py
AI Kubernetes 部署助手主程式
執行：python 0_touch_generate_pods.py（任意目錄皆可）
"""
import sys
import os
# 讓任意目錄都能正確找到專案模組
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core.config import ensure_utf8_output
ensure_utf8_output()

import time
import yaml
from kubernetes import client, config
from llama_client import ask_llama, save_gold_sample
from core.config import YAML_DIR

# ==============================
# 設定
# ==============================
DEFAULT_IMAGE = "nginx:latest"
APP_DEFAULT   = "auto-app"
DEFAULT_PORT  = 80
NS            = "default"
SAVE_DIR      = YAML_DIR   # 由 core/config.py 統一管理，不再硬編碼



# ==============================
# K8s 物件建構
# ==============================
def build_deploy(app, img, replicas, port=80, memory=None):
    resources = None
    if memory:
        resources = client.V1ResourceRequirements(
            requests={"memory": memory, "cpu": "100m"},
            limits={"memory": memory, "cpu": "500m"},
        )

    container = client.V1Container(
        name=app,
        image=img,
        ports=[client.V1ContainerPort(container_port=port)],
        resources=resources,
    )
    template = client.V1PodTemplateSpec(
        metadata=client.V1ObjectMeta(labels={"app": app}),
        spec=client.V1PodSpec(containers=[container])
    )
    spec = client.V1DeploymentSpec(
        replicas=replicas,
        selector=client.V1LabelSelector(match_labels={"app": app}),
        template=template
    )
    return client.V1Deployment(
        api_version="apps/v1",
        kind="Deployment",
        metadata=client.V1ObjectMeta(name=app),
        spec=spec
    )


def build_svc(app, port=80):
    spec = client.V1ServiceSpec(
        selector={"app": app},
        ports=[client.V1ServicePort(port=port, target_port=port)],
        type="LoadBalancer"
    )
    return client.V1Service(
        api_version="v1",
        kind="Service",
        metadata=client.V1ObjectMeta(name=f"{app}-svc"),
        spec=spec
    )


def _build_manifest_dict(app, img, pods, port=80, memory=None):
    """建立純 dict manifest，供代理評估使用（不依賴 kubernetes client 物件）。"""
    mem_req = memory or "128Mi"
    mem_lim = memory or "256Mi"
    container = {
        "name": app, "image": img, "ports": [{"containerPort": port}],
        "resources": {
            "requests": {"memory": mem_req, "cpu": "100m"},
            "limits":   {"memory": mem_lim, "cpu": "500m"},
        },
    }
    return {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata":   {"name": app, "namespace": NS},
        "spec": {
            "replicas": pods,
            "selector": {"matchLabels": {"app": app}},
            "template": {
                "metadata": {"labels": {"app": app}},
                "spec":     {"containers": [container]},
            },
        },
    }


def save_yaml(app, deployment, service):
    os.makedirs(SAVE_DIR, exist_ok=True)
    path = os.path.join(SAVE_DIR, f"{app}.yaml")
    with open(path, "w", encoding="utf-8") as f:
        f.write(yaml.dump(deployment.to_dict()))
        f.write("---\n")
        f.write(yaml.dump(service.to_dict()))
    print(f"[YAML] generated -> {path}")
    return path


def wait_ready(api, app, replicas, timeout=120):
    print("[Deploy] Waiting for Pods to be ready...")
    for _ in range(timeout // 2):
        try:
            dep   = api.read_namespaced_deployment_status(app, NS)
            ready = dep.status.ready_replicas or 0
            print(f"  Ready {ready}/{replicas}", end="\r")
            if ready == replicas:
                print(f"\n[Deploy] Deployment Ready ({ready}/{replicas})")
                return True
        except Exception:
            pass
        time.sleep(2)
    print(f"\n⚠️  等待逾時，目前狀態可能尚未完全就緒")
    return False


# ==============================
# AI 解析成功判斷
# ==============================
def _is_ai_success(resp: dict) -> bool:
    if not resp or "error" in resp:
        return False
    pods_val = resp.get("pods")
    if pods_val is None:
        return False
    try:
        return 1 <= int(pods_val) <= 100
    except (ValueError, TypeError):
        return False


# ==============================
# 主流程
# ==============================
def main():
    config.load_kube_config()
    api  = client.AppsV1Api()
    core = client.CoreV1Api()

    print("=" * 55)
    print("  Zero-Touch K8s Deployment  (LLaMA-3 LoRA)")
    print("=" * 55)

    user_query = input("\n請輸入部署指令（中英文皆可）: ").strip()
    if not user_query:
        print("[Error] 輸入不能為空")
        return

    print("\n[AI] 推論中...")
    resp = ask_llama(user_query)

    print(f"\n[AI] 解析結果：")
    for k, v in resp.items():
        print(f"   {k:10s}: {v}")

    if _is_ai_success(resp):
        pods   = int(resp["pods"])
        app    = resp.get("app_name", APP_DEFAULT)
        img    = resp.get("image",    DEFAULT_IMAGE)
        port   = int(resp.get("port",   DEFAULT_PORT))
        memory = resp.get("memory",  None)

        print(f"\n[AI] 解析成功")
        print(f"   app    : {app}")
        print(f"   image  : {img}")
        print(f"   pods   : {pods}")
        print(f"   port   : {port}")
        if memory:
            print(f"   memory : {memory}")

        final_json = {"pods": pods, "image": img, "app_name": app, "port": port}
        if memory:
            final_json["memory"] = memory

    else:
        print("\n[AI] 解析失敗 → 啟動人工標註模式")
        print(f"   原始輸入：「{user_query}」")

        while True:
            try:
                pods = int(input("請手動輸入 Pod 數量: ").strip())
                if 1 <= pods <= 100:
                    break
                print("請輸入 1~100 之間的整數")
            except ValueError:
                print("格式錯誤，請輸入數字")

        final_json = {
            "pods"    : pods,
            "image"   : DEFAULT_IMAGE,
            "app_name": APP_DEFAULT,
            "port"    : DEFAULT_PORT,
        }
        app    = APP_DEFAULT
        img    = DEFAULT_IMAGE
        port   = DEFAULT_PORT
        memory = None

    # 存入標註庫（只呼叫一次）
    save_gold_sample(user_query, final_json)

    # ── 多代理評估（安全 / 成本 / 效能）────────────────────────────
    print("\n[Agents] 代理評估中...")
    try:
        from agents.orchestrator import orchestrate, print_result
        manifest_dict = _build_manifest_dict(app, img, pods, port, memory)
        orch_result   = orchestrate(manifest_dict, save_report=True)
        print_result(orch_result)

        if orch_result["decision"] == "block":
            print("\n[Block] 部署已被代理阻斷，請修復上述問題後再試")
            return
        if orch_result["decision"] == "warn":
            ans = input("\n[Warn] 有警告，仍要繼續部署？(y/N): ").strip().lower()
            if ans != "y":
                print("已取消部署")
                return
    except ImportError:
        print("   （agents 模組未載入，跳過代理評估）")

    # 執行 K8s 部署
    print(f"\n[Deploy] 開始部署 {app} ...")
    deploy = build_deploy(app, img, pods, port=port, memory=memory)
    svc    = build_svc(app, port=port)
    save_yaml(app, deploy, svc)

    # GitOps：將 YAML 寫入 Git 倉庫（可選，無 gitpython 或非 git 目錄時自動跳過）
    try:
        from gitops.manifest_writer import write_manifest
        gitops_result = write_manifest(final_json, repo_path=".", dry_run=False, commit=True)
        if gitops_result.get("committed"):
            print(f"[GitOps] 已 commit：{gitops_result.get('commit_hash', '')[:8]}")
        else:
            print(f"[GitOps] YAML 已寫入：{gitops_result.get('manifest_path', '')}")
    except Exception as _ge:
        print(f"[GitOps] 跳過（{_ge}）")

    try:
        api.replace_namespaced_deployment(app, NS, deploy)
        print(f"[Deploy] 更新現有 Deployment：{app}")
    except Exception:
        api.create_namespaced_deployment(NS, deploy)
        print(f"[Deploy] 建立新 Deployment：{app}")

    try:
        core.replace_namespaced_service(f"{app}-svc", NS, svc)
        print(f"[Deploy] 更新現有 Service：{app}-svc")
    except Exception:
        core.create_namespaced_service(NS, svc)
        print(f"[Deploy] 建立新 Service：{app}-svc")

    wait_ready(api, app, pods)

    # 狀態輸出
    print("\n" + "=" * 55)
    print("  Kubernetes Cluster Status")
    print("=" * 55)

    pods_list = core.list_namespaced_pod(NS, label_selector=f"app={app}")
    print(f"\nPods ({len(pods_list.items)}):")
    for p in pods_list.items:
        phase = p.status.phase or "Unknown"
        status = "Ready" if phase == "Running" else "Pending"
        print(f"  [{status}] {p.metadata.name} | {phase}")

    svc_info = core.read_namespaced_service(f"{app}-svc", NS)
    print(f"\nService:")
    print(f"   Name      : {svc_info.metadata.name}")
    print(f"   ClusterIP : {svc_info.spec.cluster_ip}")
    print(f"   Port      : {svc_info.spec.ports[0].port}")

    dep_info = api.read_namespaced_deployment(app, NS)
    ready    = dep_info.status.ready_replicas or 0
    print(f"\nDeployment:")
    print(f"   {dep_info.metadata.name} | Ready {ready}/{pods}")

    print(f"\n[Done] Gold sample saved to training dataset.\n")


if __name__ == "__main__":
    main()
