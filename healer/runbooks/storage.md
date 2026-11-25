# 保存空間（PVC）綁不上、掛載失敗
keywords: persistentvolumeclaim pvc unbound storageclass waitforfirstconsumer failedmount mountvolume configmap secret not found
## 給模型的摘要
PVC 一直 Pending 多半是 StorageClass 不存在；WaitForFirstConsumer 的 PVC 要等 Pod 排上去才綁，不是錯誤。FailedMount 通常是要掛的 ConfigMap、Secret 或 PVC 不存在或名稱打錯。
## 來源
- https://kubernetes.io/docs/concepts/storage/persistent-volumes/
- https://kubernetes.io/docs/concepts/storage/storage-classes/
