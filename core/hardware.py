"""
core/hardware.py
偵測 GPU 並決定模型怎麼載入（精度、放 GPU 還是 CPU）。

2026-10-09：專案要同時在兩台機器跑——家裡 AMD RX 9060 XT 16GB（ROCm 版 torch）、
學校 NVIDIA RTX 5080 Laptop 16GB（CUDA 版 torch）。舊版 model_server 只要
torch.cuda.is_available() 就一律 bitsandbytes 4-bit，但 bitsandbytes 只支援 NVIDIA，
在 ROCm 上會直接載入失敗。

做法：顯存夠（>= LOW_VRAM_GB）就不量化，兩台都用 bf16，讓兩台跑出來的結果一致
（QA 數據要拿去口試，兩台設定不同結果會對不上）；只有小顯存（例如舊的 6GB 筆電）
而且是 NVIDIA 才用 4-bit。

plan_loading() 是純函式、不 import torch，讓 CI（沒裝 torch）也能測這段決策邏輯；
真正碰硬體的只有 detect_gpu()。
"""
from typing import Any, Dict, Optional

# 3B bf16 約 6.2GB + 1.5B bf16 約 3.1GB + KV cache / runtime 約 1.5GB ≈ 11GB，
# 低於這個顯存就不把監控模型放上 GPU，部署模型也改走量化或 CPU。
LOW_VRAM_GB = 10.0
# AMD 沒有 4-bit 可用，3B 用 fp16 至少要約 8GB 才放得下。
MIN_VRAM_FOR_FP16_3B_GB = 8.0

PRECISION_CHOICES = ("auto", "bf16", "fp16", "4bit", "cpu")
DEVICE_CHOICES = ("auto", "gpu", "cpu")


def detect_gpu() -> Dict[str, Any]:
    """回傳 {available, backend('cuda'|'rocm'|None), name, vram_gb, bf16, bnb}。"""
    info: Dict[str, Any] = {"available": False, "backend": None, "name": None,
                            "vram_gb": 0.0, "bf16": False, "bnb": False}
    try:
        import torch
    except Exception:
        return info
    if not torch.cuda.is_available():
        return info
    # ROCm 版 torch 也走 torch.cuda 介面，用 torch.version.hip 區分廠牌
    info["available"] = True
    info["backend"] = "rocm" if getattr(torch.version, "hip", None) else "cuda"
    info["name"] = torch.cuda.get_device_name(0)
    info["vram_gb"] = round(torch.cuda.get_device_properties(0).total_memory / 2**30, 1)
    try:
        info["bf16"] = bool(torch.cuda.is_bf16_supported())
    except Exception:
        info["bf16"] = False
    if info["backend"] == "cuda":
        try:
            import bitsandbytes  # noqa: F401
            info["bnb"] = True
        except Exception:
            info["bnb"] = False
    return info


def plan_loading(gpu: Dict[str, Any], precision: str = "auto",
                 monitor_device: str = "auto") -> Dict[str, Any]:
    """
    依 GPU 資訊與使用者設定決定載入方式。回傳：
        deploy_precision : "bf16" | "fp16" | "4bit" | "fp32"
        deploy_device    : "gpu" | "cpu"
        monitor_precision: "bf16" | "fp16" | "fp32"
        monitor_device   : "gpu" | "cpu"
        notes            : list[str]，給啟動 log 看的說明（為什麼這樣選、有沒有退回）
    """
    precision = (precision or "auto").lower()
    monitor_device = (monitor_device or "auto").lower()
    notes = []
    if precision not in PRECISION_CHOICES:
        notes.append(f"MODEL_PRECISION={precision} 不認得，改用 auto")
        precision = "auto"
    if monitor_device not in DEVICE_CHOICES:
        # 舊設定可能寫 MONITOR_DEVICE=cuda，視同 gpu
        if monitor_device.startswith("cuda"):
            monitor_device = "gpu"
        else:
            notes.append(f"MONITOR_DEVICE={monitor_device} 不認得，改用 auto")
            monitor_device = "auto"

    has_gpu = bool(gpu.get("available"))
    vram = float(gpu.get("vram_gb") or 0.0)
    half = "bf16" if gpu.get("bf16") else "fp16"
    can_4bit = gpu.get("backend") == "cuda" and bool(gpu.get("bnb"))

    # ── 部署模型 ──
    if not has_gpu or precision == "cpu":
        deploy_prec, deploy_dev = "fp32", "cpu"
        if not has_gpu:
            notes.append("沒有偵測到 GPU，部署模型跑 CPU（會很慢）")
    elif precision == "4bit":
        if can_4bit:
            deploy_prec, deploy_dev = "4bit", "gpu"
        else:
            deploy_prec, deploy_dev = half, "gpu"
            notes.append("要求 4bit 但這台不支援 bitsandbytes（AMD 或未安裝），改用 " + half)
    elif precision in ("bf16", "fp16"):
        deploy_prec, deploy_dev = precision, "gpu"
        if precision == "bf16" and not gpu.get("bf16"):
            deploy_prec = "fp16"
            notes.append("這張顯卡不支援 bf16，改用 fp16")
    elif vram >= LOW_VRAM_GB:
        deploy_prec, deploy_dev = half, "gpu"
    elif can_4bit:
        deploy_prec, deploy_dev = "4bit", "gpu"
        notes.append(f"顯存 {vram}GB < {LOW_VRAM_GB}GB，部署模型用 4-bit 量化")
    elif vram >= MIN_VRAM_FOR_FP16_3B_GB:
        deploy_prec, deploy_dev = half, "gpu"
    else:
        deploy_prec, deploy_dev = "fp32", "cpu"
        notes.append(f"顯存 {vram}GB 太小且無法 4-bit 量化，部署模型改跑 CPU")

    # ── 監控模型 ──
    if monitor_device == "cpu" or not has_gpu or precision == "cpu":
        mon_dev = "cpu"
    elif monitor_device == "gpu":
        mon_dev = "gpu"
    else:  # auto：顯存夠才跟部署模型一起放 GPU
        mon_dev = "gpu" if vram >= LOW_VRAM_GB else "cpu"
    mon_prec = half if mon_dev == "gpu" else "fp32"

    return {"deploy_precision": deploy_prec, "deploy_device": deploy_dev,
            "monitor_precision": mon_prec, "monitor_device": mon_dev, "notes": notes}


def embed_device(gpu: Optional[Dict[str, Any]] = None) -> str:
    """RAG embedding（BGE-M3）預設裝置：顯存夠就上 GPU，否則 CPU。"""
    gpu = gpu if gpu is not None else detect_gpu()
    return "cuda" if gpu.get("available") and float(gpu.get("vram_gb") or 0) >= LOW_VRAM_GB else "cpu"
