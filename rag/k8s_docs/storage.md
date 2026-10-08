# Kubernetes 儲存：PV、PVC、StorageClass

## 基本概念

**PersistentVolume（PV）：** 叢集層級的實體儲存資源（一塊硬碟空間），由管理員
或 StorageClass 動態建立，跟哪個 Pod 用沒有關係，是獨立存在的資源。

**PersistentVolumeClaim（PVC）：** 使用者對儲存空間的「申請」，描述需要多大
容量、什麼存取模式，Kubernetes 會幫忙找一個符合條件的 PV 綁定上去（或透過
StorageClass 動態建立一個新的）。

**StorageClass：** 定義「怎麼動態建立 PV」的範本（用哪種儲存後端、效能等級），
有了 StorageClass，使用者申請 PVC 時不用先手動建立 PV，系統會自動生成。

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: my-pvc
spec:
  accessModes:
  - ReadWriteOnce
  resources:
    requests:
      storage: 10Gi
  storageClassName: standard
```

**存取模式（accessMode）：**
- `ReadWriteOnce (RWO)`：同時只能被一個 Node 上的 Pod 掛載讀寫（最常見）
- `ReadOnlyMany (ROX)`：可以被多個 Node 同時掛載，但只能讀
- `ReadWriteMany (RWX)`：可以被多個 Node 同時掛載讀寫（需要支援的儲存後端，
  例如 NFS，本機 Docker Desktop 預設不支援）

---

## StatefulSet 的儲存模式

一般 Deployment 的 Pod 是無狀態、可以任意替換的；需要「每個副本都有自己專屬、
重啟後資料還在」的儲存時要用 StatefulSet 搭配 `volumeClaimTemplates`——每個
副本會各自拿到一個獨立的 PVC（例如 `data-myapp-0`、`data-myapp-1`），不是
共用同一份。

```yaml
spec:
  volumeClaimTemplates:
  - metadata:
      name: data
    spec:
      accessModes: ["ReadWriteOnce"]
      resources:
        requests:
          storage: 5Gi
```

**常見誤解：** 直接用 Deployment + PVC 掛載，多個副本會**搶同一個 PVC**（如果
accessMode 是 RWO，第二個副本會卡在排程失敗，因為 PVC 已經被第一個 Pod 獨占）。
需要「每個副本各自一份資料」一定要用 StatefulSet，不是 Deployment。

---

## 常見的 PVC 綁定失敗

**症狀：PVC 狀態一直是 `Pending`，Pod 也跟著卡住不會變成 Running：**

**診斷指令：**
```bash
kubectl get pvc
kubectl describe pvc <pvc-name>
kubectl get storageclass
```

**常見根因：**
1. `storageClassName` 打錯名字或叢集裡根本沒有這個 StorageClass
   （`kubectl get storageclass` 查不到對應名稱）
2. 要求的容量超過底層儲存實際能提供的上限
3. `accessMode` 跟 StorageClass 支援的模式不相容（例如要求 RWX 但底層
   儲存只支援 RWO）
4. 沒有預設 StorageClass、PVC 又沒指定 `storageClassName`，系統不知道該用
   哪個範本動態建立
5. 本機 Docker Desktop 環境：預設有一個叫 `hostpath` 的 StorageClass 可以動態
   建立，但資料實際上存在本機磁碟的某個路徑，重建叢集（不是重啟 Pod）資料
   可能會消失，不要當成正式的持久化方案
