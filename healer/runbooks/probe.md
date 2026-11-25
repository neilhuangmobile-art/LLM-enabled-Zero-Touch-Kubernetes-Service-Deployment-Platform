# 健康檢查（探針）失敗
keywords: liveness readiness startup probe failed unhealthy connection refused timeout initialdelayseconds
## 給模型的摘要
探針失敗常見原因：探針的 port 或路徑跟程式實際監聽的不一樣；或程式啟動比較慢、還沒啟動完就被判定失敗。啟動慢應該加 startupProbe（failureThreshold × periodSeconds 涵蓋最慢的啟動時間），不是把每個探針的 initialDelaySeconds 加長。探針事件裡的 connection refused 通常代表程式還沒在那個 port 上，不是相依服務的問題。
## 來源
- https://kubernetes.io/docs/tasks/configure-pod-container/configure-liveness-readiness-startup-probes/
