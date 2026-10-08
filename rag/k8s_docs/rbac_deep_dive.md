# Kubernetes RBAC 深入：Role、ClusterRole、RoleBinding

## 四個核心物件

**Role：** 定義一組權限（可以對哪些資源做哪些動作），**範圍限定在單一
namespace**。

**ClusterRole：** 跟 Role 一樣定義權限，但**範圍是整個叢集**（跨 namespace，
或者是叢集層級的資源，例如 Node、PersistentVolume 這類本身不屬於任何
namespace 的資源）。

**RoleBinding：** 把一個 Role（或 ClusterRole）的權限，授予給特定的
使用者/群組/ServiceAccount，**綁定本身限定在單一 namespace**。

**ClusterRoleBinding：** 把一個 ClusterRole 的權限，授予給使用者/群組/
ServiceAccount，**綁定範圍是整個叢集**。

**容易搞混的地方：** ClusterRole 可以被 RoleBinding 綁定（這時權限範圍會被
RoleBinding 限制在它所在的 namespace，即使原本的 ClusterRole 定義是叢集層級的）
——這是一個常見的合法用法：定義一次 ClusterRole（例如「唯讀所有常見資源」），
在多個不同 namespace 用各自的 RoleBinding 重複套用，不用每個 namespace 各寫
一份一樣的 Role。

---

## 範例：只給一個 ServiceAccount 讀取 Pod 的權限

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  namespace: my-app
  name: pod-reader
rules:
- apiGroups: [""]
  resources: ["pods"]
  verbs: ["get", "watch", "list"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: read-pods
  namespace: my-app
subjects:
- kind: ServiceAccount
  name: my-service-account
  namespace: my-app
roleRef:
  kind: Role
  name: pod-reader
  apiGroup: rbac.authorization.k8s.io
```

**`apiGroups` 常見值：**
- `""`（空字串）：core API group，涵蓋 Pod、Service、ConfigMap、Secret 這類
  最基本的資源
- `"apps"`：Deployment、StatefulSet、ReplicaSet
- `"rbac.authorization.k8s.io"`：Role、RoleBinding 這些 RBAC 物件本身

---

## 最小權限原則的實務做法

1. **不要用 `verbs: ["*"]` 或 `resources: ["*"]`**，除非真的需要管理整個叢集
   （例如 cluster-admin），明確列出需要的動作跟資源類型
2. **每個應用用自己專屬的 ServiceAccount**，不要共用 `default` ServiceAccount
   （預設 ServiceAccount 通常不該有任何額外權限）
3. **先用 `kubectl auth can-i` 驗證權限設定**：
   ```bash
   kubectl auth can-i get pods --as=system:serviceaccount:my-app:my-service-account -n my-app
   ```

---

## 常見的權限不足錯誤

**症狀：應用程式或 `kubectl` 回報 `Forbidden` / `403` / `cannot list resource
"pods" in API group ""`：**

**診斷指令：**
```bash
kubectl auth can-i <verb> <resource> --as=<user-or-serviceaccount> -n <namespace>
kubectl get rolebindings,clusterrolebindings -A | grep <service-account-name>
kubectl describe role <role-name> -n <namespace>
```

**常見根因：**
- RoleBinding 綁的 Role/ClusterRole 名稱拼錯，或綁的是不存在的物件
- RoleBinding 跟 ServiceAccount 不在同一個 namespace（RoleBinding 本身的
  `namespace` 要跟 subject 裡的 ServiceAccount 所在 namespace 對應清楚，很容易
  搞混）
- 需要跨 namespace 存取卻只設定了 RoleBinding（範圍限定單一 namespace），
  這種情況要用 ClusterRole + ClusterRoleBinding，或在每個目標 namespace
  各自建一個 RoleBinding
