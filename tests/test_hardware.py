"""
core/hardware.py 的 plan_loading()：依硬體決定模型精度/裝置。
純函式、不需要 torch，CI 也能跑。重點是兩台實際機器（家裡 AMD 16GB、學校 NVIDIA 16GB）
必須得到「完全相同」的載入方式，QA 數據才對得上。
"""
from core.hardware import plan_loading, embed_device

AMD_16 = {"available": True, "backend": "rocm", "name": "AMD Radeon RX 9060 XT",
          "vram_gb": 15.9, "bf16": True, "bnb": False}
NV_16 = {"available": True, "backend": "cuda", "name": "NVIDIA GeForce RTX 5080 Laptop GPU",
         "vram_gb": 15.9, "bf16": True, "bnb": True}
NV_6 = {"available": True, "backend": "cuda", "name": "RTX 3060 Laptop",
        "vram_gb": 6.0, "bf16": True, "bnb": True}
AMD_6 = {"available": True, "backend": "rocm", "name": "small AMD",
         "vram_gb": 6.0, "bf16": True, "bnb": False}
NO_GPU = {"available": False, "backend": None, "name": None, "vram_gb": 0.0,
          "bf16": False, "bnb": False}

_KEYS = ("deploy_precision", "deploy_device", "monitor_precision", "monitor_device")


def _core(plan):
    return {k: plan[k] for k in _KEYS}


def test_amd_16gb_all_bf16_on_gpu():
    p = plan_loading(AMD_16)
    assert _core(p) == {"deploy_precision": "bf16", "deploy_device": "gpu",
                        "monitor_precision": "bf16", "monitor_device": "gpu"}


def test_two_machines_get_identical_plan():
    # 學校 NVIDIA 就算有 bitsandbytes，16GB 也不該走 4-bit，否則兩台結果不一致
    assert _core(plan_loading(NV_16)) == _core(plan_loading(AMD_16))


def test_small_nvidia_uses_4bit_and_monitor_cpu():
    p = plan_loading(NV_6)
    assert p["deploy_precision"] == "4bit" and p["deploy_device"] == "gpu"
    assert p["monitor_device"] == "cpu" and p["monitor_precision"] == "fp32"


def test_small_amd_without_4bit_falls_back_to_cpu():
    p = plan_loading(AMD_6)
    assert p["deploy_device"] == "cpu" and p["deploy_precision"] == "fp32"
    assert p["notes"], "退回 CPU 要留說明給啟動 log"


def test_no_gpu_everything_cpu():
    p = plan_loading(NO_GPU)
    assert _core(p) == {"deploy_precision": "fp32", "deploy_device": "cpu",
                        "monitor_precision": "fp32", "monitor_device": "cpu"}


def test_forced_4bit_on_amd_degrades_with_note():
    p = plan_loading(AMD_16, precision="4bit")
    assert p["deploy_precision"] == "bf16"
    assert any("4bit" in n for n in p["notes"])


def test_forced_bf16_on_card_without_bf16_uses_fp16():
    p = plan_loading(dict(NV_16, bf16=False), precision="bf16")
    assert p["deploy_precision"] == "fp16"


def test_forced_cpu_moves_both_models():
    p = plan_loading(AMD_16, precision="cpu")
    assert p["deploy_device"] == "cpu" and p["monitor_device"] == "cpu"


def test_legacy_monitor_device_cuda_means_gpu():
    # 舊 .env 可能寫 MONITOR_DEVICE=cuda 或 cuda:0
    assert plan_loading(NV_6, monitor_device="cuda")["monitor_device"] == "gpu"
    assert plan_loading(NV_6, monitor_device="cuda:0")["monitor_device"] == "gpu"


def test_monitor_cpu_override_on_big_gpu():
    p = plan_loading(AMD_16, monitor_device="cpu")
    assert p["monitor_device"] == "cpu" and p["monitor_precision"] == "fp32"
    assert p["deploy_device"] == "gpu"


def test_unknown_values_fall_back_to_auto():
    p = plan_loading(AMD_16, precision="int3", monitor_device="tpu")
    assert _core(p) == _core(plan_loading(AMD_16))
    assert len(p["notes"]) == 2


def test_embed_device_follows_vram():
    assert embed_device(AMD_16) == "cuda"
    assert embed_device(NV_6) == "cpu"
    assert embed_device(NO_GPU) == "cpu"
