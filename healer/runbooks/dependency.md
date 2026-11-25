# 相依服務連不上
keywords: econnrefused connection refused cannot connect database dns name resolution no route timed out enotfound redis postgres mysql
## 給模型的摘要
程式 log 說連不上資料庫或其他服務：先確認那個服務有在跑、Service 名稱和 port 對、在同一個 namespace（不同 namespace 要用 名稱.namespace）。重建自己這個 Pod 沒有用。
## 來源
- https://kubernetes.io/docs/tasks/debug/debug-application/debug-service/
