"""2026-11-18 Claude 題組：Qwen3.5-4B Q8、Q4（Vulkan）依序跑 run_pipeline_claude.py（舊規則 always；agree 由紀錄算出）。
用 Python 啟動 llama-server（不用 bash 包 PowerShell，避免路徑被跳脫字元弄壞，11-18 踩過）。"""
import os
import subprocess
import sys
import time

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
LC, GG, PORT = r"D:\zt-venvs\llamacpp\vulkan\llama-server.exe", r"D:\zt-venvs\gguf", 8781
CONFIGS = [("q8_vulkan", "Qwen3.5-4B-Q8_0.gguf"), ("q4_vulkan", "Qwen3.5-4B-Q4_K_M.gguf")]

for tag, model in CONFIGS:
    path = os.path.join(GG, model)
    assert os.path.exists(path), path
    log = open(os.path.join(HERE, "results", f"server_claude_{tag}.log"), "w", encoding="utf-8")
    p = subprocess.Popen([LC, "-m", path, "-ngl", "99", "-c", "8192", "-np", "1", "--device", "Vulkan0",
                          "--host", "127.0.0.1", "--port", str(PORT)], stdout=log, stderr=subprocess.STDOUT)
    try:
        for _ in range(90):
            try:
                if requests.get(f"http://127.0.0.1:{PORT}/health", timeout=2).json().get("status") == "ok":
                    break
            except Exception:  # noqa: BLE001
                pass
            if p.poll() is not None:
                sys.exit(f"{tag}：llama-server 啟動失敗，見 server_claude_{tag}.log")
            time.sleep(2)
        print(f"== {tag} 開始 {time.strftime('%H:%M')}", flush=True)
        env = {**os.environ, "PIPELINE_SELECT_RULE": "always", "RAG_BACKEND": "field", "PYTHONIOENCODING": "utf-8",
               "TQDM_DISABLE": "1"}
        r = subprocess.run([sys.executable, "-u", os.path.join(HERE, "run_pipeline_claude.py"), "--url",
                            f"http://127.0.0.1:{PORT}", "--tag", tag], env=env, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        print(r.stdout[-3000:], r.stderr[-1500:] if r.returncode else "", flush=True)
    finally:
        p.terminate()
        p.wait(timeout=30)
        log.close()
        time.sleep(3)
print("DONE", time.strftime("%H:%M"))
