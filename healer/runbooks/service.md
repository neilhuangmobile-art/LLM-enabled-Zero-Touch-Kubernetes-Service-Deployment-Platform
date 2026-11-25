# Service 連不到 Pod
keywords: service endpoints endpointslice selector label targetport no endpoints connection refused
## 給模型的摘要
Service 用 selector 找 Pod，selector 跟 Pod 的 label 對不上就沒有端點。targetPort 要等於容器實際監聽的 port。Pod 沒就緒（readiness 失敗）也不會被加進端點。
## 來源
- https://kubernetes.io/docs/tasks/debug/debug-application/debug-service/
