# Pod 一直 Pending
keywords: pending failedscheduling insufficient cpu memory unschedulable node quota exceeded
## 給模型的摘要
FailedScheduling 含 Insufficient cpu／memory 是叢集剩下的資源不夠 requests，要調小 requests 或副本數。exceeded quota 是帳號的資源配額用完。unbound PersistentVolumeClaims 是保存空間沒綁上。
## 來源
- https://kubernetes.io/docs/tasks/debug/debug-application/debug-pods/
- https://kubernetes.io/docs/concepts/policy/resource-quotas/
