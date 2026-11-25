# 結束碼的意思
keywords: exit code 0 1 2 126 127 137 143 completed error crashloopbackoff executable file not found command
## 給模型的摘要
exit 0（Completed）一直重啟：映像檔不是常駐服務。exit 1／2：程式自己回報錯誤，看 log 最後幾行。exit 126／127：啟動指令不能執行或找不到。exit 137：被 SIGKILL（OOMKilled、liveness 重啟或驅逐）。exit 143：收到 SIGTERM 正常停止。
## 來源
- https://kubernetes.io/docs/tasks/debug/debug-application/debug-pods/
- https://runbooks.prometheus-operator.dev/runbooks/kubernetes/kubepodcrashlooping/
