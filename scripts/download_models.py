"""
scripts/download_models.py
下載專案用到的 Hugging Face 模型到 HF 快取（.env 的 HF_HOME），單一連線、逐檔驗 SHA256。

為什麼不用 `hf download` / snapshot_download：2026-10-09 家裡新電腦的網路遇到大量平行
連線就整個卡在 0 bytes（huggingface_hub 一次開幾十條連線，裝了 hf_xet、設 max_workers=1
都一樣卡），單一連線的 requests 卻有 20MB/s 以上。這支腳本直接用 requests 單線下載，
寫進 HF 快取的標準結構（hub/models--X/snapshots/<commit>/ + refs/main），
transformers / sentence-transformers 照常用模型名稱就讀得到，程式碼不用改。

用法：
    python scripts/download_models.py                 # 下載預設三個模型
    python scripts/download_models.py BAAI/bge-m3     # 只下載指定的
已經下載過、大小相符的檔案會跳過，中斷後重跑會接著下載剩下的。
"""
import fnmatch
import hashlib
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.config import BASE_MODEL, MONITOR_MODEL, HF_TOKEN, ensure_utf8_output  # noqa: E402  (會順便載入 .env 的 HF_HOME)

import requests  # noqa: E402
from huggingface_hub import HfApi  # noqa: E402

ensure_utf8_output()

EMBED_MODEL = "BAAI/bge-m3"
PATTERNS = ["*.json", "*.safetensors", "*.bin", "*.txt", "*.model", "tokenizer*", "*.py"]
SKIP_DIRS = ("onnx/", "imgs/")


def hf_home() -> str:
    return os.environ.get("HF_HOME") or os.path.join(os.path.expanduser("~"), ".cache", "huggingface")


def download(repo: str) -> None:
    api = HfApi(token=HF_TOKEN)
    info = api.model_info(repo, files_metadata=True)
    commit = info.sha or "main"
    base = os.path.join(hf_home(), "hub", "models--" + repo.replace("/", "--"))
    snap = os.path.join(base, "snapshots", commit)
    os.makedirs(os.path.join(base, "refs"), exist_ok=True)
    with open(os.path.join(base, "refs", "main"), "w") as f:
        f.write(commit)

    headers = {"Authorization": f"Bearer {HF_TOKEN}"} if HF_TOKEN else {}
    for s in info.siblings or []:
        name = s.rfilename
        if name.startswith(SKIP_DIRS) or not any(fnmatch.fnmatch(name.rsplit("/", 1)[-1], p) for p in PATTERNS):
            continue
        dst = os.path.join(snap, name)
        expected_sha = s.lfs.sha256 if s.lfs else None
        if os.path.exists(dst) and (s.size is None or os.path.getsize(dst) == s.size):
            print(f"  已存在，跳過：{name}")
            continue
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        url = f"https://huggingface.co/{repo}/resolve/{commit}/{name}"
        tmp, h, n, t0 = dst + ".part", hashlib.sha256(), 0, time.time()
        with requests.get(url, headers=headers, stream=True, timeout=30) as r:
            r.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(4 << 20):
                    f.write(chunk)
                    h.update(chunk)
                    n += len(chunk)
        if expected_sha and h.hexdigest() != expected_sha:
            os.remove(tmp)
            raise SystemExit(f"❌ SHA256 不符，檔案可能損毀，請重跑：{repo}/{name}")
        os.replace(tmp, dst)
        mb = n / 2**20
        print(f"  ✅ {name}  {mb:.1f}MB  {mb / max(time.time() - t0, 1e-3):.1f}MB/s"
              + ("  (SHA256 OK)" if expected_sha else ""))
    print(f"完成：{repo} → {snap}")


if __name__ == "__main__":
    repos = sys.argv[1:] or [BASE_MODEL, MONITOR_MODEL, EMBED_MODEL]
    print(f"HF 快取位置：{hf_home()}")
    for repo in repos:
        print(f"下載 {repo} …")
        download(repo)
