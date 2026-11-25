# 映像檔拉不下來
keywords: imagepullbackoff errimagepull manifest unknown not found pull access denied unauthorized toomanyrequests 429 image tag registry
## 給模型的摘要
三種原因處理不同：manifest unknown／not found 是名稱或 tag 打錯；pull access denied／unauthorized 是私有倉庫要登入（imagePullSecrets）；429 Too Many Requests 是 Docker Hub 限流，映像檔本身沒問題，等一下會自動重試。
## 來源
- https://kubernetes.io/docs/concepts/containers/images/
- https://kubernetes.io/docs/tasks/debug/debug-application/debug-pods/
