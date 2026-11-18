"""2026-11-18 多代理人流程推理模型（Qwen3.5-4B）在 llama.cpp 的設定比較：依序啟動每種設定的 llama-server、
跑組員 S01～S40（run_pipeline_llamacpp.py，RAG 用欄位檢索）、量顯存，跑完關掉。

挑選規則（實驗前寫死）：
1. 必須：沒有當掉、40 題都有結果；通過題數 ≥ 22（v8 是 23，容許 1 題波動）
2. 顯存 ≤ 6GB（整體預算 13GB：3B Q8 約 3.5、BGE-M3 約 1.2、生成暫存約 1、其餘給 4B）
3. 符合以上的設定中，p90 延遲最低者

  python experiments/model_select/reason/llamacpp_grid.py
結果：results/grid_summary.json，各設定的逐題結果 results/pipeline_llamacpp_<tag>.jsonl
"""
import json
import os
import subprocess
import sys
import time

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
LC = r"D:\zt-venvs\llamacpp"
GG = r"D:\zt-venvs\gguf"
PORT = 8781
CONFIGS = [
    ("q8_vulkan", "vulkan", "Qwen3.5-4B-Q8_0.gguf", ["-ngl", "99", "--device", "Vulkan0"]),
    ("q4_vulkan", "vulkan", "Qwen3.5-4B-Q4_K_M.gguf", ["-ngl", "99", "--device", "Vulkan0"]),
    ("q8_cpu", "cpu", "Qwen3.5-4B-Q8_0.gguf", ["-t", "16"]),
]


def gpu_mem(pid):
    """Windows 效能計數器：這個程序的專用／共用顯存（GB）。"""
    ps = ("$d=@{};(Get-Counter '\\GPU Process Memory(*)\\Dedicated Usage','\\GPU Process Memory(*)\\Shared Usage')"
          ".CounterSamples | ForEach-Object { if ($_.InstanceName -like ('pid_' + %d + '_*')) { "
          "$k=$_.Path.Split('\\')[-1]; $d[$k]=[double]$d[$k]+$_.CookedValue } }; $d | ConvertTo-Json" % pid)
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=60).stdout
        d = json.loads(out or "{}")
        return {k.replace(" usage", ""): round(v / 1024 ** 3, 2) for k, v in d.items()}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:100]}


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, help="試跑：每個設定只跑前幾題（結果不寫進 grid_summary.json）")
    ap.add_argument("--only", help="只跑這些設定，逗號分隔（例如 q8_vulkan,q4_vulkan）")
    a = ap.parse_args()
    summary = {}
    only = set(a.only.split(",")) if a.only else None
    for tag, build, model, extra in CONFIGS:
        if only and tag not in only:
            continue
        if not os.path.exists(os.path.join(GG, model)):
            print(f"略過 {tag}：沒有 {model}", flush=True)
            continue
        args = [os.path.join(LC, build, "llama-server.exe"), "-m", os.path.join(GG, model), "-c", "8192", "-np", "1",
                "--host", "127.0.0.1", "--port", str(PORT)] + extra
        log = open(os.path.join(HERE, "results", f"server_{tag}.log"), "w", encoding="utf-8")
        p = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT)
        try:
            for _ in range(120):
                try:
                    if requests.get(f"http://127.0.0.1:{PORT}/health", timeout=2).json().get("status") == "ok":
                        break
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(2)
            mem = gpu_mem(p.pid)
            print(f"== {tag} 載入完成，顯存 {mem}", flush=True)
            cmd = [sys.executable, "-u", os.path.join(HERE, "run_pipeline_llamacpp.py"),
                   "--url", f"http://127.0.0.1:{PORT}", "--tag", tag + ("_try" if a.limit else "")]
            if a.limit:
                cmd += ["--limit", str(a.limit)]
            # Windows 的管線預設 cp950，子程序印中文會出錯：一律用 UTF-8
            env = {**os.environ, "RAG_BACKEND": "field", "PYTHONIOENCODING": "utf-8", "TQDM_DISABLE": "1"}
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
                res = os.path.join(HERE, "results", f"pipeline_llamacpp_{tag}{'_try' if a.limit else ''}.jsonl")
                last = json.loads(open(res, encoding="utf-8").read().strip().splitlines()[-1])
                s = last.get("summary", {})
                if r.returncode != 0 or not s:
                    raise RuntimeError(f"returncode={r.returncode}；{r.stderr[-500:]}")
            except Exception as e:  # noqa: BLE001  一個設定失敗不影響其他設定，失敗原因記下來
                print(f"   {tag} 失敗：{e}", flush=True)
                summary[tag] = {"error": str(e)[:800], "vram": mem, "crashed": p.poll() is not None}
                continue
            s.update({"vram": mem, "vram_after": gpu_mem(p.pid), "crashed": p.poll() is not None})
            summary[tag] = s
            print(f"   通過 {s.get('pass_rate')}，p50 {s.get('latency_p50_s')}s，p90 {s.get('latency_p90_s')}s，"
                  f"{s.get('tok_per_s')} tok/s", flush=True)
        finally:
            p.terminate()
            p.wait(timeout=30)
            log.close()
            time.sleep(3)
    ok = {k: v for k, v in summary.items()
          if not v.get("crashed") and v.get("n") == 40 and round(v.get("pass_rate", 0) * 40) >= 22
          and (v.get("vram") or {}).get("dedicated", 99) + (v.get("vram") or {}).get("shared", 0) <= 6.0}
    best = min(ok, key=lambda k: ok[k].get("latency_p90_s", 1e9)) if ok else None
    out = {"rule": "沒有當掉且 40 題都有結果、通過 ≥ 22、顯存 ≤ 6GB，取 p90 最低", "best": best, "configs": summary}
    name = "grid_summary_try.json" if a.limit else "grid_summary.json"
    json.dump(out, open(os.path.join(HERE, "results", name), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps({"best": best}, ensure_ascii=False))


if __name__ == "__main__":
    main()
