# 缺 ConfigMap／Secret 或環境變數
keywords: createcontainerconfigerror configmap secret not found couldn't find key environment variable env missing required
## 給模型的摘要
CreateContainerConfigError 或 configmap／secret not found：部署參照的 ConfigMap 或 Secret 不存在，要先建立或改正名稱。log 說缺少某個環境變數，就在 Deployment 的 env 補上。換映像檔版本沒有幫助。
## 來源
- https://kubernetes.io/docs/tasks/debug/debug-application/debug-pods/
