# 權限不足
keywords: permission denied forbidden rbac serviceaccount cannot list unauthorized eacces read only file system securitycontext
## 給模型的摘要
Kubernetes API 回 forbidden 是服務帳號（ServiceAccount）沒有 RBAC 權限；permission denied／EACCES 是容器的使用者對檔案或掛載的資料夾沒有權限（securityContext、fsGroup）。HTTP 存取紀錄裡的 403 是程式正常回應，不是權限設定問題。
## 來源
- https://kubernetes.io/docs/reference/access-authn-authz/rbac/
