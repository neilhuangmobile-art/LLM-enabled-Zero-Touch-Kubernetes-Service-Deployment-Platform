# RAG 知識庫模組

## 功能說明

本模組實現「檢索增強生成」（Retrieval-Augmented Generation, RAG），
將 Kubernetes 官方文件與最佳實踐注入到 LLM prompt 中，
有效防止模型「幻覺」出不存在的 API 版本或設定欄位。

除了基本檢索之外，也提供：

- **引用來源與信心分數**：每個回答都可以附上來源文件、片段與相似度分數，
  在 Chat 介面可展開查看（`📎 N 個引用來源`）。
- **知識庫管理介面**：在網頁的 `Knowledge Base` 分頁新增/刪除知識文件、
  重建索引，不需要 SSH 進終端機。
- **檢索品質評估**：一組人工標註的測試查詢，量化比較 ChromaDB 與舊版
  TF-IDF 的 Recall@K / MRR，證明升級後的效果。
- **多輪對話記憶整合**：追問（例如「那個要怎麼修？」）會考慮最近幾輪對話
  再檢索，而不是只看單一句話。

## 快速開始

```bash
# 建立索引（ChromaDB 向量資料庫 + TF-IDF 後備索引）
python rag/build_index.py --rebuild

# 測試查詢
python rag/retriever.py "CrashLoopBackOff 怎麼解決"
python rag/retriever.py "如何設定 resources limits" --top-k 5

# 顯示增強後的 Prompt（含引用來源）
python rag/retriever.py "部署 nginx 3 個副本" --augment

# 檢索品質評估報告（ChromaDB vs TF-IDF）
python rag/eval_retrieval.py
```

## 架構說明

```
rag/
├── k8s_docs/               # K8s 知識文件庫（可透過網頁 Knowledge Base 分頁管理）
│   ├── common_errors.md
│   ├── deployment_patterns.md
│   ├── resource_management.md
│   ├── security_best_practices.md
│   └── zerotouch_user_guide.md
├── vector_store.py         # ChromaDB 持久化向量儲存層
├── build_index.py          # 建立索引（Chroma + TF-IDF 後備）
├── retriever.py            # 查詢 + prompt 增強 + 多輪對話感知
├── kb_manager.py           # 知識庫文件管理（新增/刪除/重建/狀態）
├── eval_retrieval.py       # 檢索品質評估（Recall@K / MRR）
├── index.json              # TF-IDF 後備索引（自動生成）
├── index_meta.json         # 索引建立狀態摘要（自動生成）
└── chroma_db/              # ChromaDB 資料（本機開發預設路徑；
                             #   這台伺服器實際存在 /mnt/Data/capstone2025/cache/chroma_db，
                             #   因為系統碟空間有限）
```

## 檢索方法（依優先順序自動退化）

| 順序 | 方法 | 需求 | 品質 | 說明 |
|------|------|------|------|------|
| 1 | ChromaDB + sentence-transformers | `pip install chromadb sentence-transformers` | 高 | 持久化向量資料庫，語意搜尋，有 GPU 時用 CUDA 加速嵌入 |
| 2 | TF-IDF JSON | 僅需 Python 內建模組 | 中 | 輕量後備方案，任何環境都能跑 |
| 3 | 關鍵字比對 | 無 | 低 | 連 TF-IDF 索引都建立失敗時的最後防線 |

任何一層不可用都會靜默退化到下一層，不會讓應用程式壞掉。

## 整合方式

```python
from rag.retriever import augment_prompt, augment_prompt_ex

# 舊版介面（向後相容）：只要增強後的 prompt
enhanced_prompt = augment_prompt("幫我部署一個 nginx，3 個副本")

# 新版介面：同時取得引用來源（供前端顯示），並考慮對話歷史
enhanced_prompt, sources = augment_prompt_ex(
    "那個要怎麼修？", history=chat_history, top_k=3,
)
```

`llama_client.chat_llama()` 回傳 `(reply, sources)`；`web_demo.py` 的
`/api/chat` 會把 `sources` 一併回傳給前端，在助理訊息下方顯示可展開的引用來源。

## 知識庫管理（網頁介面）

登入後點左側 `Knowledge Base`：

- 查看目前使用的檢索方法、已索引 chunk 數、最後建立時間
- 上傳新的 `.md` / `.txt` 知識文件、刪除既有文件（會自動觸發重建索引）
- 用測試查詢即時檢視檢索結果與信心分數
- 一鍵執行檢索品質評估，比較 ChromaDB 與 TF-IDF 的 Recall@K / MRR

對應的 API：`/api/rag/status`、`/api/rag/docs`、`/api/rag/rebuild`、
`/api/rag/query`、`/api/rag/eval`。

## 新增文件（CLI 方式）

將 `.md` 或 `.txt` 文件放入 `rag/k8s_docs/` 後，重新執行：

```bash
python rag/build_index.py --rebuild
```

## 研究依據

> 「將 LLM 與最新的 Kubernetes 文檔、內部運行手冊以及組織最佳實踐相結合。
> 這能有效防止模型「想像」出不存在的 API 版本或欄位」
>
> — 基於大型語言模型之零接觸 Kubernetes 部署平台架構與產業實務研究報告
