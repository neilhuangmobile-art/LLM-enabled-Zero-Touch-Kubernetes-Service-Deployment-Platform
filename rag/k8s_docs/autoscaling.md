# Kubernetes 自動擴縮：HPA 與 VPA

## HorizontalPodAutoscaler（HPA）

根據 CPU/記憶體使用率（或自訂指標）**自動調整副本數量**，穩定版 API 是
`autoscaling/v2`（`v2beta2` 已在 Kubernetes v1.23 畢業成 GA，正式環境應該用
`autoscaling/v2`，不要用已棄用的 `v2beta2`/`v2beta1`）。

```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: my-app-hpa
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: my-app
  minReplicas: 2
  maxReplicas: 10
  metrics:
  - type: Resource
    resource:
      name: cpu
      target:
        type: Utilization
        averageUtilization: 70
```

**先決條件：HPA 要能運作，叢集裡必須先裝好 metrics-server**，不然 HPA 永遠
查不到即時的 CPU/記憶體使用率，`kubectl get hpa` 會顯示 `<unknown>`。

**診斷指令：**
```bash
kubectl get hpa
kubectl describe hpa <hpa-name>
kubectl top pods          # 需要 metrics-server，驗證有沒有裝好
kubectl get deployment metrics-server -n kube-system
```

---

## 常見踩坑

**1. metrics-server 沒裝，HPA 完全不動作**：這是最常見的問題，`kubectl get hpa`
看到 `TARGETS` 欄位一直是 `<unknown>/70%` 就是這個原因。Docker Desktop 內建的
K8s 預設沒有裝 metrics-server，需要另外裝。

**2. 副本數量劇烈震盪（scale up 又馬上 scale down，反覆發生）**：
- 預設的 `stabilizationWindowSeconds` 太短，瞬間的流量波動就觸發調整
- 應用程式啟動需要時間才能真正開始處理流量（還在初始化），但 HPA 已經把它
  算進「已有副本」，誤判現有副本數足夠，建議搭配 `readinessProbe` 確保新副本
  真的準備好才被納入負載平衡
- 可以在 `behavior` 欄位設定 scale down 的 `stabilizationWindowSeconds`
  拉長（例如 300 秒），避免太敏感

**3. `minReplicas` 設定太低，流量突然暴增時來不及擴容**：HPA 本身也需要時間
偵測到指標超標、拉起新副本、等新副本 Ready，這中間有延遲，不是即時的，
如果預期流量會突然大幅波動，`minReplicas` 要抓保守一點當緩衝。

**4. 同時用 HPA 跟手動 `kubectl scale`**：手動改的副本數會被 HPA 下一次
評估週期覆蓋掉，兩者衝突，有 HPA 的 Deployment 不要再手動調整副本數。

---

## VerticalPodAutoscaler（VPA，選用元件，不是內建功能）

跟 HPA 不同方向——HPA 調整**副本數量**，VPA 調整**每個副本的資源 requests/
limits** 本身。VPA 不是 Kubernetes 內建功能，需要額外安裝（
`kubernetes/autoscaler` 專案下的獨立元件）。

**VPA 跟 HPA 不要對同一個 Deployment 的 CPU/記憶體指標同時生效**——兩者都想
調整資源相關的設定，同時啟用容易互相干擾，造成排程不穩定。VPA 常見用法是用
在沒有搭配 HPA 的 StatefulSet，或是先用「僅推薦模式」（`updateMode: Off`）
觀察 VPA 的建議值，再手動套用，不要一開始就讓它自動改。
