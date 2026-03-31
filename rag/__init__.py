"""
rag/ — 檢索增強生成（RAG）模組

防止 LLM 在生成 K8s YAML 時「幻覺」出不存在的 API 版本或欄位。

快速使用：
    from rag.retriever import retrieve, augment_prompt

    # 查詢相關文件
    docs = retrieve("OOMKilled 怎麼解決", top_k=3)

    # 增強 LLM prompt（最常用）
    enhanced = augment_prompt("部署 nginx 3 個副本")
"""
