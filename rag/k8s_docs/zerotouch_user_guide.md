# ZeroTouch K8s User Guide

ZeroTouch K8s is a web assistant for learning Kubernetes, deploying workloads, inspecting cluster state, and recovering unhealthy Pods. Users can operate it through the Chat page or through the dedicated sidebar tools.

## Chat

Use Chat for natural-language operations and questions. The assistant can answer how to use this system, explain Kubernetes concepts, list Pods and Deployments, scale workloads, update images, roll back apps, and dispatch deployments.

Useful chat examples:

- `list pods`
- `show deployments`
- `deploy 3 nginx:latest pods for web-frontend, port 80`
- `spin up 4 node:20-alpine pods for api-gateway, port 3000`
- `scale api-gateway to 5`
- `update api-gateway to node:22-alpine`
- `rollback api-gateway`
- `How do I debug CrashLoopBackOff?`
- `How do I use Healer?`

## Deploy Console

Deploy Console is for creating Kubernetes Deployments and Services from natural language. Type a request that includes the image, replica count, app name, and optional port or memory limit. The system parses the request, enriches dataset fields, runs Guardian validation, runs agent review, optionally runs kubectl dry-run, then creates the resources when Kubernetes is connected.

Recommended format:

`deploy <replicas> <image> pods for <app-name>, port <port>`

Examples:

- `deploy 3 nginx:latest pods for web-frontend, port 80`
- `deploy 2 redis:latest pods for cache-service, port 6379`
- `spin up 4 node:20-alpine pods for api-gateway, port 3000`
- `幫我部署 5 個 redis pods 給 cache-service port 6379`

After deployment, use Pods or Deployments pages to inspect the result.

## Pods Page

The Pods page lists Pods in the default namespace. It shows Pod name, app label, phase, IP, node, restart count, age, and available actions. Use it to confirm whether a deployment actually created running Pods.

Chat shortcut: `list pods`.

## Deployments Page

The Deployments page lists Kubernetes Deployments. It shows app name, image, desired replicas, ready replicas, age, and actions such as delete. Use it to check whether a rollout is ready or stuck.

Chat shortcut: `show deployments`.

## Healer

Healer scans the default namespace for abnormal Pods such as CrashLoopBackOff, OOMKilled, ImagePullBackOff, ErrImagePull, and Error. It helps recover apps by deleting unhealthy Pods so the owning ReplicaSet or Deployment can recreate them.

How to use Healer:

1. Click `Healer` in the left sidebar.
2. Click `Scan Now` to detect abnormal Pods.
3. Review the issue list and status reason.
4. Click a single fix action for one Pod, or `Auto Fix All` to delete all detected unhealthy Pods.
5. Return to Pods or Deployments to confirm the new Pods become Running.

Important: Healer does not fix a bad image tag, bad environment variable, missing Secret, or broken application code by itself. It restarts/recreates Pods. If the root cause remains, the Pod may enter the same failure state again.

## GitOps Log

GitOps Log shows deployment history from git commits. Use it to audit what was deployed and identify versions for rollback. Click `Refresh` to reload recent deployment commits.

Chat shortcuts:

- `rollback <app-name>` rolls back an app when rollback support is available.
- `show deployments` helps identify the app name before rollback.

## Metrics

Metrics shows Prometheus observability status. It reports whether Prometheus is connected, the running Pod count, the endpoint URL, and quick PromQL references. Use it to check cluster health and workload counts.

If Prometheus is offline, start or expose Prometheus first. The UI suggests a port-forward command when it cannot connect.

## Dataset Manager

Dataset Manager inspects and enriches training data used by the local Kubernetes model. It shows total records, output coverage, K8s/non-K8s ratio, files, and top categories.

Buttons:

- `Quick Fill (rules only)`: fast enrichment without model output.
- `Full Enrich (LLaMA output)`: slower enrichment using model output.
- `Dry Run`: preview without writing changes.
- `Refresh Stats`: reload dataset statistics.

## Safe Deployment Behavior

For deployment requests, the system prioritizes exact fields: replica count, image, app name, port, and memory. It should not deploy when a request is not a Kubernetes task. Guardian, agent review, and dry-run are used to reduce invalid manifests and unsafe operations.

If a user wants to learn the system, answer with page names and concrete steps. If a user wants to deploy Pods, ask for missing required fields only when needed: image, replica count, app name, and port.
