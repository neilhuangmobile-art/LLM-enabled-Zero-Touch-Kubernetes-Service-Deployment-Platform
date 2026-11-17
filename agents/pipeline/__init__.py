"""多代理人部署解析流程（2026-11-16，第一階段）。

正規化（Gemini，失敗改用本地模型）→ 意圖檢查②（講了沒寫進去的）→ 空缺檢查（必填欄位補齊）
→ 額外欄位對應到部署設定 → 產出和 llama_client.ask_llama 相同格式的結果，外加 _pipeline（來源、問題、提醒）。

本地模型是 Qwen3.5-4B 4-bit（docs/bench_record/2026-11-15_推理模型選型報告.md），
跑在 core/pipeline_model_server.py（127.0.0.1:8766，用 C:\\zt-k8s\\.venv-qwen35）。
網站用 .env 的 DEPLOY_PIPELINE=multiagent 開啟，預設關閉。
"""
