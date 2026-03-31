# RAG 知識庫模組

## 功能說明

本模組實現「檢索增強生成」（Retrieval-Augmented Generation, RAG），
將 Kubernetes 官方文件與最佳實踐注入到 LLM prompt 中，
有效防止模型「幻覺」出不存在的 API 版本或設定欄位。

## 快速開始

```bash
# 建立向量索引
python rag/build_index.py

# 測試查詢
python rag/retriever.py "CrashLoopBackOff 怎麼解決"
python rag/retriever.py "如何設定 resources limits" --top-k 5

# 顯示增強後的 Prompt
python rag/retriever.py "部署 nginx 3 個副本" --augment
```

## 架構說明

```
rag/
├── k8s_docs/               # K8s 知識文件庫
│   ├── common_errors.md    # 常見錯誤與解決方案
│   ├── deployment_patterns.md  # Deployment 最佳實踐
│   ├── resource_management.md  # 資源管理指南
│   └── security_best_practices.md  # 安全設定指南
├── build_index.py          # 建立向量索引
├── retriever.py            # 查詢相關文件片段
└── index.json              # 向量索引（自動生成）
```

## 嵌入方法

| 方法 | 需求 | 品質 | 說明 |
|------|------|------|------|
| sentence-transformers | `pip install sentence-transformers` | 高 | 語意理解，推薦 |
| TF-IDF | 僅需 Python 內建模組 | 中 | 輕量後備方案 |

若未安裝 sentence-transformers，自動使用 TF-IDF。

## 整合方式

在 `llama_client.py` 中使用 RAG 增強：

```python
from rag.retriever import augment_prompt

# 原始請求
user_input = "幫我部署一個 nginx，3 個副本"

# RAG 增強後的 prompt（包含相關知識）
enhanced_prompt = augment_prompt(user_input)
result = ask_llama(enhanced_prompt)
```

## 新增文件

將 `.md` 或 `.txt` 文件放入 `rag/k8s_docs/` 後，重新執行：

```bash
python rag/build_index.py --rebuild
```

## 研究依據

> 「將 LLM 與最新的 Kubernetes 文檔、內部運行手冊以及組織最佳實踐相結合。
> 這能有效防止模型「想像」出不存在的 API 版本或欄位」
>
> — 基於大型語言模型之零接觸 Kubernetes 部署平台架構與產業實務研究報告
