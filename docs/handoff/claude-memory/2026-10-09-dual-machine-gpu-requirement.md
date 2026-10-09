---
name: 2026-10-09-dual-machine-gpu-requirement
description: 雙機（家AMD 9060XT/學校RTX 5080 Laptop，都16GB）同設定已實作並在AMD實測：core/hardware.py自動bf16、requirements拆amd/nvidia、transformers升4.45.2；學校NVIDIA尚未實機驗證
metadata:
  node_type: memory
  type: project
  originSessionId: 110ca64c-0ce9-476a-aa64-bad49bb5b63d
  modified: 2026-10-08T19:09:44.383Z
---

2026-10-09 使用者要求系統在兩台電腦都能用，並說「直接改」（已實作，未 commit）：
- 家裡：AMD RX 9060 XT 16GB、Ryzen 9 9950X、94GB RAM（ROCm 7.2.1 torch 2.9.1）
- 學校：NVIDIA RTX 5080 Laptop 16GB（Blackwell → 必須 torch 2.9.1+cu128）、內顯 Radeon 890M、RAM 推測 32GB
- 兩台都全域 Python 3.12（使用者不要 venv）

實作：`core/hardware.py`（detect_gpu + 純函式 plan_loading：>=10GB 兩顆模型 bf16 上 GPU；
小顯存 NVIDIA 才 4-bit）、`model_server.py` 用它且 /health 回報 gpu/precision/vram、
`rag/vector_store.py` BGE-M3 GPU fp16、`requirements-amd.txt`/`requirements-nvidia.txt`、
transformers 4.44.2→4.45.2（修長 prompt 全詞彙表 logits 顯存高峰）、`scripts/download_models.py`、
`tests/test_hardware.py`（12個）。文件：setup.md 改寫、observability/README 補 helm 指令、
CHANGELOG、security_review §31。

AMD 實測：pytest 307 全過；reserved 13.27→10.05GB；整卡高峰 14.0/15.9GB；RAG GPU fp16 與 CPU
Recall@3/MRR 相同、延遲 1970→14.8ms；web_demo e2e chat 正確但 18–28s，主因是 Gemini API
（惡意意圖偵測 504 DEADLINE_EXCEEDED），非 GPU。

**待辦**：學校 RTX 5080 實機驗證（裝 requirements-nvidia、/health 要是 gpu-bf16、跑 QA 比通過率）。

**Why**：使用者拿 QA 數據去口試，兩台設定不同會讓結果對不上。
**How to apply**：任何模型/精度/量化改動都要兩台能跑同一設定；換模型（如 Qwen3-4B）要先用 206 題
QA 對照量測，見 [[2026-10-08-rag-upgrade-result]]、[[2026-10-09-new-machine-amd-gpu-env]]。
