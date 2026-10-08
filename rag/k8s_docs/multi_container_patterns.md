# Kubernetes 多容器 Pod 模式：Sidecar 與 Init Container

## 為什麼一個 Pod 可以有多個容器

Pod 裡的所有容器共享同一個網路命名空間（用 `localhost` 互相溝通）跟可選的
共享儲存（`emptyDir` volume），適合「主程式」跟「輔助功能」緊密搭配、生命週期
綁在一起的情境。**不是把無關的服務塞進同一個 Pod 省事**——不相關的服務應該各自
獨立一個 Deployment，才能各自獨立擴縮、各自獨立更新版本，塞進同一個 Pod 等於
放棄了這些彈性。

---

## Init Container：啟動前的準備工作

在主容器啟動**之前**依序執行完畢的容器，常見用途：等待依賴服務就緒、下載設定檔、
資料庫 migration。

```yaml
spec:
  initContainers:
  - name: wait-for-db
    image: busybox
    command: ['sh', '-c', 'until nc -z db-service 5432; do sleep 2; done']
  containers:
  - name: main-app
    image: my-app:latest
```

**特性：**
- 依序執行，前一個沒有成功結束（exit code 0），後面的 init container 跟主容器
  都不會啟動
- 失敗會導致整個 Pod 重試（跟主容器的重啟邏輯一樣受 restartPolicy 影響）
- 跟主容器用不同的 image 是正常的，不需要功能一樣

---

## Sidecar：跟主容器一起長期運行

跟主容器同時啟動、生命週期綁在一起的輔助容器，常見用途：
- **日誌收集**：主容器把 log 寫到共享的 `emptyDir`，sidecar 容器讀取並轉送到
  外部日誌系統
- **服務網格 proxy**（例如 Istio 的 Envoy sidecar）：自動注入，代理所有進出
  主容器的網路流量
- **設定檔動態更新**：sidecar 定期從外部來源拉取最新設定，寫進共享 volume，
  主容器讀取

```yaml
spec:
  containers:
  - name: main-app
    image: my-app:latest
    volumeMounts:
    - name: shared-logs
      mountPath: /var/log/app
  - name: log-shipper
    image: log-shipper:latest
    volumeMounts:
    - name: shared-logs
      mountPath: /var/log/app
  volumes:
  - name: shared-logs
    emptyDir: {}
```

---

## 常見的診斷/操作注意事項

**指定容器名稱才能操作多容器 Pod：**
```bash
kubectl logs <pod-name> -c <container-name>
kubectl exec -it <pod-name> -c <container-name> -- sh
```
一個 Pod 有多個容器時，`kubectl logs`/`kubectl exec` 沒有指定 `-c` 會預設抓
第一個容器，如果要查的是 sidecar 的狀況，一定要加 `-c` 指定正確的容器名稱，
否則會一直看錯容器的 log 卻找不到問題在哪。

**Pod 的健康狀態是看所有容器的綜合狀態**：任何一個容器沒有 Ready，整個 Pod
都不會被視為 Ready（不會被 Service 導入流量）——sidecar 容器掛掉或卡住，
也會拖累看起來完全正常的主容器，使用者誤以為是主程式出問題，實際上要先排查
sidecar。
