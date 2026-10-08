# Kubernetes 排程：Affinity、Taints/Tolerations、Pending 排查

## Node Affinity / Anti-Affinity

讓排程器根據 Node 的標籤（label）決定 Pod 要不要排在某個 Node 上，比舊版的
`nodeSelector` 更有彈性（可以設定「偏好但不強制」）。

```yaml
affinity:
  nodeAffinity:
    requiredDuringSchedulingIgnoredDuringExecution:   # 硬性要求，不符合就不排程
      nodeSelectorTerms:
      - matchExpressions:
        - key: disktype
          operator: In
          values: ["ssd"]
    preferredDuringSchedulingIgnoredDuringExecution:  # 偏好，不符合也能排，只是優先度較低
    - weight: 1
      preference:
        matchExpressions:
        - key: zone
          operator: In
          values: ["us-east-1a"]
```

**Pod Anti-Affinity** 常見用途：把同一個應用的多個副本分散到不同 Node，避免
單一 Node 掛掉就全部副本一起消失。

---

## Taints 與 Tolerations

Taint 加在 Node 上，代表「除非 Pod 明確容忍這個 taint，否則不要排進來」；
Toleration 加在 Pod 上，宣告「我可以容忍這個 taint」。兩者是排程器的互斥機制，
跟 Affinity（正面篩選）是互補的兩種工具。

```bash
kubectl taint nodes node1 key=value:NoSchedule
```

```yaml
tolerations:
- key: "key"
  operator: "Equal"
  value: "value"
  effect: "NoSchedule"
```

**三種 effect：**
- `NoSchedule`：新 Pod 不會被排上來，但已經在上面的 Pod 不受影響
- `PreferNoSchedule`：盡量不排，但不是強制
- `NoExecute`：新 Pod 不會排上來，**已經在上面、不能容忍的 Pod 也會被驅逐**

新建的叢集 control-plane Node 通常會自動打上 `NoSchedule` taint，避免一般
應用被排到控制平面節點上，這是正常現象，不是 bug。

---

## resources.requests 如何影響排程

排程器決定把 Pod 放到哪個 Node，看的是 `requests`（不是 `limits`）——排程器會
確保這個 Node 剩餘可分配資源扣掉這個 Pod 的 `requests` 後還夠用，`limits` 只影響
執行時能不能超用，不影響排程判斷。

**常見誤解：** 設定很高的 `limits` 但 `requests` 很低，Pod 很容易被排上去，但
執行時如果真的用到接近 `limits` 的資源量，整個 Node 可能超賣（overcommit），
導致其他 Pod 被擠壓甚至被驅逐。

---

## Pod 卡在 Pending 的排查

**診斷指令：**
```bash
kubectl describe pod <pod-name>    # 看 Events 區塊的具體原因
kubectl get nodes
kubectl describe node <node-name>  # 看這個 Node 剩餘可分配資源
```

**`kubectl describe pod` 常見的 Events 訊息跟對應原因：**

| Events 訊息關鍵字 | 原因 |
|---|---|
| `Insufficient cpu` / `Insufficient memory` | 所有 Node 剩餘可分配資源都不夠這個 Pod 的 `requests` |
| `node(s) had taint that the pod didn't tolerate` | 所有候選 Node 都有 Pod 不能容忍的 taint |
| `node(s) didn't match Pod's node affinity/selector` | nodeAffinity/nodeSelector 條件沒有任何 Node 符合 |
| `0/N nodes are available: N node(s) had volume node affinity conflict` | PVC 綁定的 PV 跟目前候選 Node 的可用區域（zone）不一致 |
| （完全沒有 Events，一直空白） | 可能是 scheduler 本身沒有正常運作，檢查 `kubectl get pods -n kube-system` 裡的 scheduler 元件 |
