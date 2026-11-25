# 程式自己的 heap 用完（JVM／Node.js）
keywords: java lang outofmemoryerror heap space javascript heap out of memory xmx max old space size gc overhead
## 給模型的摘要
log 出現 java.lang.OutOfMemoryError 或 JavaScript heap out of memory，是程式自己的 heap 上限不夠，不是容器被 OOMKilled。只加容器記憶體沒有用，要調 -Xmx（JAVA_TOOL_OPTIONS）或 --max-old-space-size，或修記憶體洩漏。
## 來源
- https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/
