"""
0_touch_generate_pods.py
AI Kubernetes 部署助手主程式
執行：python 0_touch_generate_pods.py
"""
import sys
import os
import time
import yaml
from kubernetes import client, config
from llama_client import ask_llama, save_gold_sample

# ==============================
# 設定
# ==============================
DEFAULT_IMAGE = "nginx:latest"
APP_DEFAULT   = "auto-app"
DEFAULT_PORT  = 80
NS            = "default"
SAVE_DIR     = r"D:\k8s_new"



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


def save_yaml(app, deployment, service):
    if not os.path.exists(SAVE_DIR):
        os.makedirs(SAVE_DIR)
    path = os.path.join(SAVE_DIR, f"{app}.yaml")
    with open(path, "w", encoding="utf-8") as f:
        f.write(yaml.dump(deployment.to_dict()))
        f.write("---\n")
        f.write(yaml.dump(service.to_dict()))
    print(f"✅ YAML generated -> {path}")
    return path


def wait_ready(api, app, replicas, timeout=120):
    print("⏳ Waiting for Pods to be ready...")
    for _ in range(timeout // 2):
        try:
            dep   = api.read_namespaced_deployment_status(app, NS)
            ready = dep.status.ready_replicas or 0
            print(f"  Ready {ready}/{replicas}", end="\r")
            if ready == replicas:
                print(f"\n✅ Deployment Ready ({ready}/{replicas})")
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
    print("  🤖 Zero-Touch K8s Deployment  (LLaMA-3 LoRA)")
    print("=" * 55)

    user_query = input("\n請輸入部署指令（中英文皆可）: ").strip()
    if not user_query:
        print("❌ 輸入不能為空")
        return

    print("\n🧠 AI 推論中...")
    resp = ask_llama(user_query)

    print(f"\n📦 AI 解析結果：")
    for k, v in resp.items():
        print(f"   {k:10s}: {v}")

    if _is_ai_success(resp):
        pods   = int(resp["pods"])
        app    = resp.get("app_name", APP_DEFAULT)
        img    = resp.get("image",    DEFAULT_IMAGE)
        port   = int(resp.get("port",   DEFAULT_PORT))
        memory = resp.get("memory",  None)

        print(f"\n✅ AI 解析成功")
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
        print("\n⚠️  AI 解析失敗 → 啟動人工標註模式")
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

    # 執行 K8s 部署
    print(f"\n🚀 開始部署 {app} ...")
    deploy = build_deploy(app, img, pods, port=port, memory=memory)
    svc    = build_svc(app, port=port)
    save_yaml(app, deploy, svc)

    try:
        api.replace_namespaced_deployment(app, NS, deploy)
        print(f"♻️  更新現有 Deployment：{app}")
    except Exception:
        api.create_namespaced_deployment(NS, deploy)
        print(f"🆕 建立新 Deployment：{app}")

    try:
        core.replace_namespaced_service(f"{app}-svc", NS, svc)
        print(f"♻️  更新現有 Service：{app}-svc")
    except Exception:
        core.create_namespaced_service(NS, svc)
        print(f"🆕 建立新 Service：{app}-svc")

    wait_ready(api, app, pods)

    # 狀態輸出
    print("\n" + "=" * 55)
    print("  📡 Kubernetes Cluster Status")
    print("=" * 55)

    pods_list = core.list_namespaced_pod(NS, label_selector=f"app={app}")
    print(f"\n🟦 Pods ({len(pods_list.items)}):")
    for p in pods_list.items:
        phase = p.status.phase or "Unknown"
        icon  = "✅" if phase == "Running" else "⏳"
        print(f"  {icon} {p.metadata.name} | {phase}")

    svc_info = core.read_namespaced_service(f"{app}-svc", NS)
    print(f"\n🌐 Service:")
    print(f"   Name      : {svc_info.metadata.name}")
    print(f"   ClusterIP : {svc_info.spec.cluster_ip}")
    print(f"   Port      : {svc_info.spec.ports[0].port}")

    dep_info = api.read_namespaced_deployment(app, NS)
    ready    = dep_info.status.ready_replicas or 0
    print(f"\n📦 Deployment:")
    print(f"   {dep_info.metadata.name} | Ready {ready}/{pods}")

    print(f"\n🎉 Done! Gold sample saved to training dataset.\n")


if __name__ == "__main__":
    main()
