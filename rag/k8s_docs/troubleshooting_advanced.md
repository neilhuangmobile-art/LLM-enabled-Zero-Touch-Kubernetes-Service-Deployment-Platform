# Kubernetes 進階故障排除（補 common_errors.md 沒蓋到的情境）

## Node NotReady

**症狀：** `kubectl get nodes` 顯示某個 Node 的 `STATUS` 是 `NotReady`，上面的
Pod 可能會在一段時間後被驅逐重新排程到其他 Node。

**診斷指令：**
```bash
kubectl describe node <node-name>
kubectl get events --field-selector involvedObject.kind=Node
```

**常見根因：**
- kubelet 服務掛掉或無法跟 control plane 通訊（網路問題、憑證過期）
- Node 磁碟空間不足（`DiskPressure` condition 為 True）
- Node 記憶體不足（`MemoryPressure` condition 為 True）
- 容器執行時（containerd/Docker）異常

本機 Docker Desktop 環境：Node NotReady 常見是 Docker Desktop 本身重啟中
或資源不足（宿主機記憶體/CPU 被其他程式佔滿），先確認 Docker Desktop 狀態，
不一定是 K8s 設定問題。

---

## Pod 卡在 Terminating 刪不掉

**症狀：** `kubectl delete pod` 之後，Pod 狀態長時間停在 `Terminating`，
一直沒有真正消失。

**常見根因：**
1. 容器沒有正確處理 `SIGTERM` 訊號，Kubernetes 預設等待
   `terminationGracePeriodSeconds`（預設 30 秒）後才強制 `SIGKILL`，如果應用
   程式忽略 `SIGTERM`，這段等待期間看起來就像「卡住」
2. Pod 上有 `finalizer`（一種「刪除前必須先完成某個清理動作」的機制），清理
   動作沒有完成，Pod 就不會真的被移除
3. Node 本身跟 control plane 失聯（例如前述的 NotReady），kubelet 沒辦法
   回報「已經清理完畢」

**強制移除（有風險，只在確認不會有殘留資源問題時用）：**
```bash
kubectl delete pod <pod-name> --grace-period=0 --force
```

---

## DNS 解析失敗（CoreDNS 層面，補 networking.md 的應用層視角）

**症狀：** Pod 內執行 `nslookup kubernetes.default` 或連接其他 Service 時
得到解析失敗。

**診斷指令：**
```bash
kubectl get pods -n kube-system -l k8s-app=kube-dns
kubectl logs -n kube-system -l k8s-app=kube-dns
kubectl run -it --rm debug --image=busybox --restart=Never -- nslookup kubernetes.default
```

**常見根因：**
- CoreDNS Pod 本身沒有 Running（資源不足被驅逐，或映像拉取失敗）
- CoreDNS 的 `ConfigMap`（`coredns` in `kube-system`）設定被誤改
- Pod 的 `/etc/resolv.conf` 被應用程式或自訂 `dnsConfig` 覆蓋成錯誤設定

---

## ConfigMap/Secret 更新後，Pod 沒有套用新內容

**症狀：** 改了 ConfigMap 或 Secret 的內容，但 Pod 裡讀到的還是舊值。

**根因跟行為差異（容易被誤會成 bug，實際上是設計行為）：**
- 用**環境變數**方式掛載（`env.valueFrom.configMapKeyRef`）：**不會自動更新**，
  只有 Pod 重新建立時才會讀到新值
- 用**檔案掛載**方式（`volumeMounts` 掛整個 ConfigMap/Secret）：kubelet 會
  定期同步，通常在幾十秒到幾分鐘內檔案內容會更新，但**應用程式自己不會自動
  重新讀取檔案**，除非程式本身有監聽檔案變化的邏輯（多數程式只在啟動時讀一次）

**正確的更新流程：** 改完 ConfigMap/Secret 後，如果應用程式沒有動態重載機制，
要手動觸發 Pod 重建才會真正生效：
```bash
kubectl rollout restart deployment/<name>
```

---

## 資源配額（ResourceQuota）導致部署失敗

**症狀：** 建立 Pod/Deployment 時收到
`exceeded quota: <quota-name>, requested: ..., used: ..., limited: ...`。

**診斷指令：**
```bash
kubectl get resourcequota -n <namespace>
kubectl describe resourcequota <quota-name> -n <namespace>
```

代表這個 namespace 設定了資源用量上限（ResourceQuota），目前請求的資源量
（或是這個 namespace 已經用掉的總量加上這次請求）超過了上限——這是管理員
刻意設定的多租戶保護機制，不是錯誤，解法是調整這次部署的資源需求，或請管理員
評估是否要調高這個 namespace 的配額。
