"""
generate_qa_data.py
Generate K8s Q&A training data (Chinese + English)
"""
import json, os

QA_PAIRS = [
    {"q": "What is Kubernetes?", "a": "Kubernetes (K8s) is an open-source container orchestration platform that automates deployment, scaling, and management of containerized applications. It groups containers into logical units called Pods and manages their lifecycle."},
    {"q": "Kubernetes is what?", "a": "Kubernetes (K8s) is an open-source container orchestration platform. It automates deployment, scaling, and management of containerized applications. Key features: auto-restart failed containers, load balancing, rolling updates, and secret management."},
    {"q": "k8s shime", "a": "Kubernetes (K8s) is Google's open-source container orchestration system. It automates deployment, scaling, and management of containerized apps, and supports auto-healing, load balancing, and rolling updates."},
    {"q": "Why use Kubernetes?", "a": "Kubernetes helps you: automatically restart failed containers, scale applications based on load, distribute traffic across instances, roll out updates without downtime, and manage configuration and secrets securely."},
    {"q": "What is a Pod in Kubernetes?", "a": "A Pod is the smallest deployable unit in Kubernetes. It contains one or more containers that share the same network namespace (IP address) and storage volumes. Containers within a Pod can communicate via localhost."},
    {"q": "What is the difference between a Pod and a Container?", "a": "A Container packages an application and its dependencies. A Pod is Kubernetes' management unit that wraps one or more containers sharing network and storage. Most Pods contain just one container."},
    {"q": "What causes CrashLoopBackOff?", "a": "CrashLoopBackOff means a container keeps crashing and Kubernetes keeps restarting it. Common causes: application startup error, missing environment variables or config, insufficient memory (OOMKilled), wrong startup command, or missing dependencies."},
    {"q": "What is ImagePullBackOff?", "a": "ImagePullBackOff means Kubernetes cannot pull the container image. Causes: wrong image name or tag, image does not exist in the registry, authentication required for private registry, or network connectivity issues."},
    {"q": "What is a Deployment?", "a": "A Deployment is a Kubernetes resource that manages a ReplicaSet, which manages a set of identical Pods. It ensures the desired number of pods are always running and supports rolling updates and rollbacks."},
    {"q": "What is the difference between Deployment and StatefulSet?", "a": "Deployment is for stateless applications where pods are identical and interchangeable. StatefulSet is for stateful applications like databases where each pod has a stable identity and persistent storage that survives restarts."},
    {"q": "How do I roll back a deployment?", "a": "Use: kubectl rollout undo deployment/<name>. To roll back to a specific version: kubectl rollout undo deployment/<name> --to-revision=2. Check history with: kubectl rollout history deployment/<name>"},
    {"q": "What is a Kubernetes Service?", "a": "A Service provides a stable network endpoint for a set of Pods. Since Pod IPs change when pods restart, Services give a fixed IP or DNS name. Types: ClusterIP (internal), NodePort (external via node), LoadBalancer (cloud load balancer)."},
    {"q": "What are the types of Kubernetes Services?", "a": "There are four Service types: ClusterIP (internal access only, default), NodePort (external access via node IP and fixed port), LoadBalancer (cloud load balancer for external access), ExternalName (maps Service to external DNS name)."},
    {"q": "What is a ReplicaSet?", "a": "A ReplicaSet ensures a specified number of Pod replicas are running at any time. If a Pod fails, ReplicaSet creates a new one. Usually you do not create ReplicaSets directly as Deployments manage them for you."},
    {"q": "What is HPA?", "a": "HPA (Horizontal Pod Autoscaler) automatically scales the number of pods based on CPU usage, memory, or custom metrics. It scales up when load increases and scales down when load decreases, optimizing resource usage."},
    {"q": "What is a Namespace?", "a": "A Namespace provides a way to divide a Kubernetes cluster into virtual sub-clusters. Resources in different namespaces are isolated. Common use: separate environments like dev, staging, prod in the same cluster."},
    {"q": "What is a ConfigMap?", "a": "A ConfigMap stores non-confidential configuration data as key-value pairs. Pods can consume ConfigMaps as environment variables, command-line arguments, or as config files mounted in a volume."},
    {"q": "What is the difference between ConfigMap and Secret?", "a": "ConfigMap stores non-sensitive configuration data like config files and environment variables. Secret stores sensitive data like passwords, API keys, and certificates encoded in Base64 with stricter access controls."},
    {"q": "What is a PersistentVolume?", "a": "A PersistentVolume (PV) is storage provisioned in the cluster. A PersistentVolumeClaim (PVC) is a request for storage by a Pod. PVCs bind to PVs, allowing pods to use persistent storage that survives pod restarts."},
    {"q": "What is an Ingress?", "a": "Ingress manages external HTTP and HTTPS traffic to services inside the cluster. It provides URL-based routing, SSL termination, and virtual hosting, acting like a smart reverse proxy for your cluster."},
    {"q": "What is RBAC in Kubernetes?", "a": "RBAC (Role-Based Access Control) controls who can do what in the cluster. Key concepts: Role (permissions within a namespace), ClusterRole (cluster-wide permissions), RoleBinding (assigns role to user or service account)."},
    {"q": "What is a DaemonSet?", "a": "A DaemonSet ensures that a copy of a Pod runs on all (or some) nodes in the cluster. Used for cluster-wide services like log collectors, monitoring agents, or network plugins that need to run on every node."},
    {"q": "What is a Job in Kubernetes?", "a": "A Job creates one or more Pods that run to completion. Unlike Deployments which run continuously, Jobs are for batch processing tasks. When the task finishes, the Job is marked complete. CronJob runs Jobs on a schedule."},
    {"q": "What is kubectl?", "a": "kubectl is the command-line tool for interacting with Kubernetes clusters. Common commands: kubectl get pods (list pods), kubectl apply -f file.yaml (deploy from file), kubectl delete deployment <name> (delete), kubectl logs <pod> (view logs)."},
    {"q": "What is a container image?", "a": "A container image is a lightweight, standalone package that includes everything needed to run an application: code, runtime, libraries, environment variables, and config files. Images are stored in registries like Docker Hub."},
    {"q": "What is LoRA fine-tuning?", "a": "LoRA (Low-Rank Adaptation) fine-tunes large language models efficiently by adding small trainable matrices to existing layers instead of retraining all parameters. This reduces memory and compute requirements by 10-100x while achieving near full fine-tuning quality."},
    {"q": "How accurate is this system?", "a": "In 60 test cases covering 1-10 pods, Chinese and English, with port and memory: pods accuracy 98.3 percent, image accuracy 98.3 percent, overall exact match 93.3 percent. The model uses LLaMA-3.1-8B plus LoRA fine-tuned on 800 deployment examples."},
    {"q": "How do I deploy a service?", "a": "Type a natural language command in the chat, for example: deploy 3 nginx:latest pods for web-frontend, or start 2 redis pods on port 6379. The AI will parse your request and ask for confirmation before deploying to Kubernetes."},
    {"q": "How do I delete a deployment?", "a": "Type in the chat: delete web-frontend-1234 or remove auto-app-5678. The AI will find the matching Deployment and delete it. If unsure of the name, say list deployments to see all current deployments."},
    {"q": "How do I check pod status?", "a": "Two ways: say list pods in the chat to get a text summary, or click the Pods button in the left sidebar to open the Pods panel which shows all pods with status, IP, age, and a Details button for more information."},
    {"q": "How do I scale a deployment?", "a": "Say in the chat: scale web-frontend to 5, or change redis to 3 replicas. The AI will update the deployment replica count immediately."},
    {"q": "What is the difference between kubectl apply and kubectl create?", "a": "kubectl create creates a new resource and fails if it already exists. kubectl apply creates or updates a resource, applying the changes declaratively. kubectl apply is preferred for production as it supports idempotent updates."},
    {"q": "What is a node in Kubernetes?", "a": "A node is a worker machine in Kubernetes, either a physical or virtual machine. Each node runs the kubelet (agent), container runtime (like Docker), and kube-proxy. The control plane manages all nodes."},
    {"q": "What is the control plane?", "a": "The control plane manages the Kubernetes cluster. It includes: API Server (entry point for all commands), etcd (distributed data store), Scheduler (assigns pods to nodes), Controller Manager (ensures desired state is maintained)."},
    {"q": "What is resource limits in Kubernetes?", "a": "Resource limits control how much CPU and memory a container can use. Requests are what the container is guaranteed. Limits are the maximum it can use. Setting these prevents one container from consuming all cluster resources."},
    {"q": "What is a liveness probe?", "a": "A liveness probe checks if a container is running properly. If the probe fails, Kubernetes restarts the container. A readiness probe checks if the container is ready to serve traffic. If it fails, the pod is removed from service endpoints."},
    {"q": "What is rolling update?", "a": "A rolling update gradually replaces old pod instances with new ones, ensuring zero downtime. Kubernetes updates pods one by one (or in batches), waiting for each new pod to be healthy before proceeding. You can configure maxSurge and maxUnavailable."},
    {"q": "What is etcd?", "a": "etcd is a distributed key-value store used by Kubernetes to store all cluster state and configuration data. It is the single source of truth for the cluster. High availability requires running multiple etcd instances."},
    {"q": "What is a ServiceAccount?", "a": "A ServiceAccount provides an identity for processes running in a Pod to interact with the Kubernetes API. Pods use ServiceAccounts to authenticate API requests. Combined with RBAC, you can control what each pod is allowed to do."},
    {"q": "How does load balancing work in Kubernetes?", "a": "Kubernetes load balances traffic using Services and kube-proxy. When you create a Service, kube-proxy sets up rules to distribute traffic across all healthy pods matching the service selector. LoadBalancer Services use cloud provider load balancers for external traffic."},
    {"q": "What is container orchestration?", "a": "Container orchestration automates the deployment, management, scaling, and networking of containers. It handles: scheduling containers on available nodes, restarting failed containers, scaling based on demand, networking between containers, and managing storage."},
]

os.makedirs("dataset", exist_ok=True)
output_path = "dataset/k8s_qa_samples.jsonl"

with open(output_path, "w", encoding="utf-8") as f:
    for pair in QA_PAIRS:
        entry = {
            "input": pair["q"],
            "output": pair["a"],
            "type": "qa"
        }
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

print(f"Done! Generated {len(QA_PAIRS)} Q&A pairs")
print(f"Saved to: {output_path}")
