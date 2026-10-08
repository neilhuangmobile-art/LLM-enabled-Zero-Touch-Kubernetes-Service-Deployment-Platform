# Kubernetes 網路：Service、Ingress、NetworkPolicy

## Service 的三種類型

**ClusterIP（預設）：** 只能從叢集內部存取，給一個固定的內部虛擬 IP，透過
Service 名稱做 DNS 解析（`<service-name>.<namespace>.svc.cluster.local`）。
適合微服務之間互相呼叫，不對外曝露。

**NodePort：** 在每個 Node 上開一個固定連接埠（預設範圍 30000-32767），外部可以用
`<任一 Node IP>:<NodePort>` 存取。適合開發/測試環境，不建議生產環境直接曝露給
使用者（沒有負載平衡、連接埠範圍受限）。

**LoadBalancer：** 向雲端供應商（AWS/GCP/Azure）或本地叢集（例如 Docker Desktop）
申請一個外部負載平衡器，自動把流量導進 Service。本機開發環境（Docker Desktop）會
把 LoadBalancer 曝露到 `127.0.0.1`。

**診斷指令：**
```bash
kubectl get svc
kubectl describe svc <service-name>
kubectl get endpoints <service-name>   # 確認有沒有真的綁到 Pod
```

**常見問題：Service 有建立，但連不到 Pod：**
- 檢查 Service 的 `selector` 有沒有對應到 Pod 的 `labels`（最常見的根因，打錯
  label 名稱或值）
- `kubectl get endpoints <service-name>` 如果是空的，代表 selector 沒有匹配到
  任何 Pod
- 確認 Pod 本身是 `Running` 且 `Ready`，沒 Ready 的 Pod 不會被加進 Endpoints

---

## Ingress

Ingress 是 Service 之上的第七層（HTTP/HTTPS）路由規則，用一個固定的入口把流量依
網域名稱/路徑分流到不同的 Service，需要搭配一個 Ingress Controller（例如
nginx-ingress、Traefik）才會真的生效——**單純建立 Ingress 資源，沒裝 Controller
是沒用的**，新手很常漏掉這一步。

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: example-ingress
spec:
  rules:
  - host: app.example.com
    http:
      paths:
      - path: /
        pathType: Prefix
        backend:
          service:
            name: my-service
            port:
              number: 80
```

**診斷指令：**
```bash
kubectl get ingress
kubectl describe ingress <ingress-name>
kubectl get pods -n ingress-nginx   # 確認 Ingress Controller 本身有在跑
```

---

## NetworkPolicy

預設情況下 Kubernetes **不限制** Pod 之間的網路流量，任何 Pod 都能連到任何其他
Pod。NetworkPolicy 用來限制這件事，常見模式是「預設拒絕所有流量，再明確開放
需要的連線」：

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: default-deny-all
spec:
  podSelector: {}
  policyTypes:
  - Ingress
  - Egress
```

**注意：NetworkPolicy 需要 CNI（容器網路介面）外掛支援才會真的生效**（例如
Calico、Cilium），有些 CNI（像 Docker Desktop 內建的）不支援 NetworkPolicy，
設定了也不會有任何效果，不會報錯，是另一種「靜默失敗」。

---

## DNS 解析常見問題

Kubernetes 叢集內部用 CoreDNS 做服務發現，Pod 要連另一個 Service 時用服務名稱
即可（同 namespace）或 `<service>.<namespace>` （跨 namespace）。

**症狀：Pod 連不到另一個 Service，錯誤訊息像 `could not resolve host` / `DNS
lookup failed`：**
- 確認目標 Service 真的存在、名稱拼對（含 namespace）
- 檢查 CoreDNS 本身是否健康：`kubectl get pods -n kube-system -l k8s-app=kube-dns`
- Pod 的 `dnsPolicy` 設定異常（通常不需要自訂，預設值就對）
- 多租戶/跨 namespace 存取時，記得 Service 本身可能受 NetworkPolicy 限制，
  DNS 解析得到 IP 但連線被擋，容易被誤判成 DNS 問題
