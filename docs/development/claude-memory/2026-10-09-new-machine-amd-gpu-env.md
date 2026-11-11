---
name: 2026-10-09-new-machine-amd-gpu-env
description: 家裡新電腦是 AMD RX 9060 XT 16GB；用全域 Python 3.12.10（已無 .venv）+ ROCm 7.2.1 torch；bitsandbytes 不能用；uv/新 shell 的 PATH 陷阱
metadata:
  node_type: memory
  type: project
  originSessionId: 110ca64c-0ce9-476a-aa64-bad49bb5b63d
  modified: 2026-10-08T18:42:36.420Z
---

2026-10-09 家裡新電腦實測：**AMD Radeon RX 9060 XT 16GB**、Ryzen 9 9950X（有內顯，但 torch 只看到 9060 XT，device 0）、94GB RAM。

環境（2026-10-09 使用者要求改成不用 venv）：
- repo 在 `D:\LLM-enabled-Zero-Touch-Kubernetes-Service-Deployment-Platform`
- **全域 Python 3.12.10**（python.org 安裝，`C:\Users\kaddy\AppData\Local\Programs\Python\Python312`），
  User PATH 最前面 + `PY_PYTHON=3.12`。`.venv` 已刪除。另有 3.14（Python install manager）仍在，`py -3.14` 可叫。
- torch `2.9.1+rocm7.2.1`（repo.radeon.com/rocm/windows/rocm-rel-7.2.1，含 rocm_sdk_*），bf16 matmul OK；
  其餘套件照 requirements.txt 原版號（跳過 bitsandbytes）。`pip check` 乾淨，`pytest` 295 全過。
- requirements.txt **還沒更新**反映 ROCm torch / 拿掉 bitsandbytes（併入雙機計畫，見 [[2026-10-09-dual-machine-gpu-requirement]]）

**陷阱**：
- Claude Code 的 shell 繼承 VS Code 啟動時的 PATH；改了 User PATH 後 VS Code 沒重開，`python` 會解析到 3.14 → pip 裝 cp312 wheel 報 "not a supported wheel"。重開前用完整路徑叫 3.12。
- **這台網路遇到「大量平行連線」下載就卡在 0 bytes，單一連線完全正常（curl 4MB/s、requests 單線 ~23MB/s）**。
  2026-10-09 查清：uv（平行）卡死、`huggingface_hub.snapshot_download`（開 ~60 條連線，裝了 hf_xet、設
  `HF_HUB_DISABLE_XET=1`+`max_workers=1` 都還是卡）卡死；pip、curl、requests 單線都正常。
  做法：套件用 `python -m pip install --timeout 20 --retries 10`；HF 模型用自寫單線 requests 腳本
  （`HfApi.model_info(files_metadata=True)` 取 commit sha + LFS sha256，直接寫 `hub/models--X/snapshots/<sha>/` +
  `refs/main`，下載完驗 sha256；要含 `*.bin` 與子資料夾如 `1_Pooling/`）。根因（路由器/防毒/防火牆）未查。
- HF 快取在 `D:/k8s畢業專題/huggingface_cache`（`.env` 的 HF_HOME），2026-10-09 已有 Qwen2.5-3B、Qwen2.5-1.5B、
  BGE-M3（BGE-M3 在 ROCm GPU 上 encode 實測正常）。不支援 symlink（沒開開發人員模式），快取是實體檔。

`core/model_server.py` 在 GPU 可用時會用 bitsandbytes 4-bit，ROCm 上會失敗——待雙機計畫處理。

相關：[[2026-10-07-new-machine-python-version-risk]]、[[new-machine-setup-checklist]]
