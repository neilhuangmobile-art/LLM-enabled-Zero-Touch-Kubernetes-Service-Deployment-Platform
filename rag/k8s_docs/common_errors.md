# Kubernetes 常見錯誤與解決方案

## CrashLoopBackOff

**原因：** 容器持續崩潰後重新啟動，Kubernetes 會以指數退避（exponential backoff）延遲重啟。

**常見根因：**
- 應用程式啟動時找不到必要的環境變數或設定
- ConfigMap / Secret 未掛載或 key 名稱錯誤
- 依賴的服務（資料庫、API）尚未就緒
- 應用程式本身有 bug 導致立即退出
- 記憶體或 CPU 不足讓應用程式在初始化時崩潰

**診斷指令：**
```bash
kubectl logs <pod-name> --previous
kubectl describe pod <pod-name>
kubectl get events --field-selector involvedObject.name=<pod-name>
```

**解決方法：**
1. 查看前一個容器的日誌（`--previous` flag）
2. 確認所有環境變數和 ConfigMap 已正確掛載
3. 增加 `initialDelaySeconds` 給予應用程式更多啟動時間
4. 使用 `readinessProbe` 而非 `livenessProbe` 避免過早殺掉容器

---

## OOMKilled (Exit Code 137)

**原因：** 容器使用的記憶體超過 `resources.limits.memory` 設定值，被 Kubernetes OOM Killer 強制終止。

**記憶體計算公式：**
- 最低記憶體（GB） ≈ 參數量（Billion） × 2
- LLM 推論需額外預留 20-30% 作為 KV Cache

**解決方法：**
1. 提高 `resources.limits.memory` 值
2. 同時設定 `resources.requests.memory`（建議與 limits 相同或稍低）
3. 優化應用程式記憶體使用（如 batch size 調整）

**YAML 範例：**
```yaml
resources:
  requests:
    memory: "512Mi"
    cpu: "250m"
  limits:
    memory: "1Gi"
    cpu: "500m"
```

---

## ImagePullBackOff / ErrImagePull

**原因：** kubelet 無法從容器映像倉庫拉取指定映像。

**常見根因：**
- 映像名稱或 tag 拼錯
- 私有倉庫缺少 `imagePullSecrets`
- 網路問題（節點無法連線到倉庫）
- Docker Hub rate limit 觸發

**解決方法：**
1. 確認映像存在：`docker pull <image>:<tag>`
2. 私有倉庫需建立 Secret：
   ```bash
   kubectl create secret docker-registry regcred \
     --docker-server=<registry> \
     --docker-username=<user> \
     --docker-password=<password>
   ```
3. 在 Pod spec 加入 `imagePullSecrets`

---

## Pending Pod（無法調度）

**原因：** Scheduler 找不到滿足 Pod 需求的節點。

**常見根因：**
- 資源不足（CPU/Memory requests 超過節點可用量）
- NodeSelector / Affinity 規則無法匹配任何節點
- Taints 未設定對應 Tolerations
- PersistentVolumeClaim 無法綁定

**診斷：**
```bash
kubectl describe pod <pod-name>
# 看 Events 區塊的 "FailedScheduling" 訊息
```

---

## CreateContainerConfigError

**原因：** 容器設定有誤，通常是引用了不存在的 ConfigMap 或 Secret。

**解決方法：**
1. 確認 ConfigMap/Secret 已在相同 namespace 建立
2. 確認 key 名稱正確
3. 先執行 `kubectl get configmap` / `kubectl get secret` 驗證

---

## Terminating Pod 卡住

**原因：** Pod 在刪除時卡在 Terminating 狀態，通常因為 finalizer 未清除。

**解決方法：**
```bash
kubectl patch pod <pod-name> -p '{"metadata":{"finalizers":[]}}' --type=merge
kubectl delete pod <pod-name> --force --grace-period=0
```
