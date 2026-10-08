# Kubernetes 更新策略：滾動更新、藍綠、金絲雀

## 滾動更新（Rolling Update，Deployment 預設策略）

逐步用新版本的 Pod 取代舊版本，中間維持服務不中斷（部分舊版本、部分新版本
同時存在）。

```yaml
spec:
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxSurge: 1          # 更新過程中，最多可以比預期副本數多出幾個 Pod
      maxUnavailable: 0    # 更新過程中，最多允許幾個副本處於「不可用」狀態
```

**`maxSurge` 跟 `maxUnavailable` 的取捨：**
- `maxUnavailable: 0`（搭配 `maxSurge: 1` 或更高）：更新過程中服務容量不會
  下降，但會短暫用到比正常更多的資源（新舊版本同時存在的那段時間）
- `maxSurge: 0`（搭配 `maxUnavailable: 1` 或更高）：不會用到額外資源，但更新
  過程中服務容量會短暫下降
- 兩者都設 0 是不合法的設定（更新永遠無法進行）

**常用指令：**
```bash
kubectl rollout status deployment/<name>     # 查看更新進度
kubectl rollout history deployment/<name>    # 查看歷史版本
kubectl rollout undo deployment/<name>       # 回滾到上一版
kubectl rollout undo deployment/<name> --to-revision=<N>   # 回滾到指定版本
```

**滾動更新卡住的常見原因**：新版本的 Pod 一直無法通過 `readinessProbe`（例如
新版本有 bug 或缺少某個環境變數），Kubernetes 不會繼續往下推進更新，舊版本
也不會被移除——這其實是一種保護機制，但使用者常誤以為「更新失敗了」，實際上
是卡在等新 Pod Ready，要去看新 Pod 的 `kubectl describe pod` 跟 log 找原因。

---

## 藍綠部署（Blue-Green Deployment）

同時維持**兩套完整環境**（藍=目前上線版本、綠=新版本），新版本部署完整測試
通過後，**一次性**把流量入口（通常是改 Service 的 selector 或 Ingress 規則）
切到新版本。好處是切換瞬間完成、有問題可以立刻切回去；代價是需要同時維持兩套
完整規模的環境，資源成本是平時的兩倍。

Kubernetes 沒有內建的「藍綠部署」資源類型，是用兩個獨立的 Deployment
（例如 `app-blue`、`app-green`）+ 一個 Service，透過改 Service 的 `selector`
標籤達成切換效果。

---

## 金絲雀部署（Canary Deployment）

先讓**小部分流量**（例如 5%）導向新版本，確認沒問題再逐步擴大比例，最後完全
取代舊版本。跟滾動更新的差異是：滾動更新是「依副本數比例」，金絲雀更強調
「依流量比例」，且通常會搭配監控指標決定要不要繼續擴大。

Kubernetes 原生做法：部署一個新的、少副本數的 Deployment（例如舊版本 9
副本、新版本 1 副本），搭配同一個 Service 的 selector 同時匹配兩者——流量會
依照副本數比例近似分配（不是精確的流量百分比控制）。要做到精確流量比例控制、
自動化分析指標決定是否繼續推進，通常需要服務網格（Istio/Linkerd）或專門的
漸進式交付工具（例如 Argo Rollouts），不是單靠原生 Deployment 資源就能做到。
