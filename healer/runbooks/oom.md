# 容器記憶體不足（OOMKilled）
keywords: oomkilled oom memory limit killed exit 137 out of memory oomkilling
## 給模型的摘要
容器用的記憶體超過 limits.memory，被核心砍掉（reason OOMKilled、exit 137）。處理方式是提高 limits.memory，或找出記憶體洩漏。只有 exit 137 但沒有 OOMKilled，通常是被 liveness 探針重啟或被驅逐，不是記憶體不足。
## 來源
- https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/
- https://kubernetes.io/docs/tasks/debug/debug-application/debug-pods/
