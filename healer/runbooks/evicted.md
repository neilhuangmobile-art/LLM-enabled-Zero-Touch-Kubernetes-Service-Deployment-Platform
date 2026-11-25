# Pod 被驅逐、初始化容器失敗
keywords: evicted eviction memorypressure diskpressure node init container initcontainer init error podinitializing
## 給模型的摘要
Evicted：節點記憶體或磁碟壓力，系統把 Pod 趕走；被驅逐的 Pod 留著不會自己消失。initContainer 失敗時主程式不會開始跑，要看初始化容器自己的 log。
## 來源
- https://kubernetes.io/docs/concepts/scheduling-eviction/node-pressure-eviction/
- https://kubernetes.io/docs/concepts/workloads/pods/init-containers/
