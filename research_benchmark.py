"""
research_benchmark.py
給論文/口試用的量化數據蒐集腳本——全部對著「正在跑」的 Model Server
（core/model_server.py，127.0.0.1:8765）送真實請求、量真實時間，不產生任何
憑空捏造的數字。跑完直接印出摘要，並把完整結果存成 JSON，供另外做圖表用。

涵蓋五項：
  1. 部署解析準確率（deploy accuracy）：走真正的生產路徑 llama_client.ask_llama()
     （確定性解析器 → RAG few-shot → /infer），刻意關掉 Gemini 正規化層
     （USE_LLM_NORMALIZE=0，只在這個程序的環境變數生效，不動 .env），原因是
     Gemini 免費層有速率限制（每把 key 每分鐘 5 次），開著會讓測試又慢又不穩定，
     也讓這組數字更能反映「本地模型+RAG+規則層」這一段本身的準確率，不跟外部
     API 的穩定度混在一起。測試集直接沿用 eval_simple.py 既有的 20 筆標註案例
     （不是我這次新編的），避免球員兼裁判。
  2. 意圖分類準確率（intent accuracy）：/classify 端點，20 筆新標註案例，涵蓋
     INTENT_FEWSHOT 沒出現過的措辭。
  3. 各端點回應延遲（/infer、/classify、/diagnose、/chat）：多筆真實請求量測，
     算 mean/median/p95。
  4. 穩定度（stability）：同一個輸入重複呼叫 N 次，看輸出一不一致——不同端點
     的 temperature 設定不同（/classify=0.0、/diagnose=0.2、/infer=0.3、
     /chat=0.5），預期穩定度會隨 temperature 升高而下降，這本身就是一個有意義
     的量化對照。
  5. RAG 檢索品質：直接重跑既有的 rag/eval_retrieval.py（24 筆標註查詢），
     拿現在這個當下真正部署的索引去測，不是沿用舊報告檔案裡的數字。

執行前提：core/model_server.py 要是常駐在跑的（python core/model_server.py），
跑 `curl http://127.0.0.1:8765/health` 確認 deploy_loaded/monitor_loaded 都是
true。整個腳本跑完大約數分鐘（LLM 推論＋重複呼叫次數影響，視顯卡而定）。

輸出：reports/research_benchmark_<timestamp>.json
"""
import os
import sys
import json
import time
import statistics
import subprocess
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 必須在 import llama_client / core.config 之前設，且只影響這個程序，不碰 .env。
os.environ["USE_LLM_NORMALIZE"] = "0"

from core.config import ensure_utf8_output, REPORTS_DIR, MODEL_SERVER_URL
ensure_utf8_output()

import requests
import llama_client


# ══════════════════════════════════════════════════════════════════
# 測試集
# ══════════════════════════════════════════════════════════════════

# 沿用 eval_simple.py 既有的 20 筆標註案例（原封不動複製，不是這次新編的）。
DEPLOY_TEST_CASES = [
    ("deploy 3 pods of nginx:latest",                       3,  ["nginx"]),
    ("start 5 replicas of redis:7-alpine for cache-server", 5,  ["redis"]),
    ("launch 2 postgres:15 pods named db-primary",          2,  ["postgres"]),
    ("spin up 1 node:20-alpine container for api-gateway",  1,  ["node"]),
    ("run 4 python:3.11-slim pods for data-processor",      4,  ["python"]),
    ("部署 3 個 web-frontend 的 pod，使用 nginx:latest",    3,  ["nginx"]),
    ("幫我起 5 個 auth-service，映像檔是 python:3.11-slim", 5,  ["python"]),
    ("建立 2 個 redis:7-alpine 容器，命名為 cache-server",  2,  ["redis"]),
    ("我需要 1 個 db-primary pod，image 用 postgres:15",    1,  ["postgres"]),
    ("請部署 4 個 node:20-alpine 作為 api-gateway",         4,  ["node"]),
    ("deploy 2 nginx:latest pods for web-frontend, port 80",2,  ["nginx", "80"]),
    ("start 3 node:20-alpine for api-gateway, expose port 3000", 3, ["node", "3000"]),
    ("部署 2 個 web-frontend，image nginx:latest，開放 port 80", 2, ["nginx", "80"]),
    ("幫我跑 3 個 api-gateway，用 node:20-alpine，port 3000", 3, ["node", "3000"]),
    ("run 1 postgres:15 pod for db-primary, container port 5432", 1, ["postgres", "5432"]),
    ("deploy 2 nginx:latest for web-frontend, memory limit 256Mi", 2, ["nginx", "256"]),
    ("start 3 python:3.11-slim pods, memory 512Mi",         3,  ["python", "512"]),
    ("部署 2 個 web-frontend，image nginx:latest，記憶體限制 256Mi", 2, ["nginx", "256"]),
    ("幫我跑 4 個 data-processor，python:3.11-slim，記憶體 512Mi", 4, ["python", "512"]),
    ("launch 1 elasticsearch:8.11.0 pod, memory=2Gi",       1,  ["elasticsearch", "2"]),
]

# 這次新標註的意圖分類測試集（20 筆），刻意用跟 INTENT_FEWSHOT 不同的措辭，
# 避免「考試範圍剛好是背過的題目」。
INTENT_TEST_CASES = [
    ("列出目前所有 pod", "list_pods"),
    ("show all deployments please", "list_deployments"),
    ("把 shop-api 擴成 6 個副本", "scale"),
    ("can you bump api-gateway up to 4 replicas", "scale"),
    ("幫我把 worker 的 image 換成 python:3.12", "update_image"),
    ("update the image of cache-server to redis:7", "update_image"),
    ("api 這個部署回滾到上一版", "rollback"),
    ("please roll back web-frontend to the previous version", "rollback"),
    ("把 old-service 刪掉", "delete"),
    ("remove the deployment named test-app", "delete"),
    ("幫我開 3 個 nginx 的 pod", "deploy"),
    ("I need a redis instance running, 2 replicas", "deploy"),
    ("checker-pod 這個 pod 狀態如何", "describe_pod"),
    ("give me the details of pod data-sync-9f8a", "describe_pod"),
    ("billing-service 正常嗎", "pod_health"),
    ("is auth-worker currently broken", "pod_health"),
    ("auto-app 這個部署現在狀態怎樣", "describe_deployment"),
    ("what's the status of the payments deployment", "describe_deployment"),
    ("Kubernetes 的 Service 跟 Deployment 差在哪裡", "qa"),
    ("what does CrashLoopBackOff actually mean", "qa"),
]

# 延遲/穩定度測試用的固定輸入（跟上面準確率測試集分開，避免同一次呼叫身兼兩種
# 統計用途，讓每組數字的意義單純）。
LATENCY_DEPLOY_INPUT = "deploy 3 pods of nginx:latest for web-frontend, port 80"
LATENCY_CHAT_INPUT = "什麼是 Kubernetes 的 readiness probe？"
LATENCY_DIAGNOSE_CONTEXT = {
    "pod_name": "bench-pod", "container": "app", "reason": "CrashLoopBackOff",
    "restart_count": 5, "logs": "panic: out of memory",
    "events": [{"reason": "OOMKilling", "message": "Memory limit reached"}],
}
LATENCY_CLASSIFY_INPUT = "幫我把 web-frontend 擴到 5 個"

STABILITY_DEPLOY_INPUT = "deploy 2 pods of redis:7-alpine for cache-service, port 6379"
STABILITY_CLASSIFY_INPUT = "delete web-frontend"


def _now_ms():
    return time.perf_counter() * 1000


# ══════════════════════════════════════════════════════════════════
# 1. 部署解析準確率
# ══════════════════════════════════════════════════════════════════
def run_deploy_accuracy():
    print("\n=== 1. 部署解析準確率（走真正的生產路徑，Gemini 正規化層關閉）===")
    results = []
    for text, expect_pods, expect_keywords in DEPLOY_TEST_CASES:
        t0 = _now_ms()
        try:
            out = llama_client.ask_llama(text)
        except Exception as e:
            out = {"error": str(e)}
        latency_ms = _now_ms() - t0

        ok_pods = isinstance(out, dict) and out.get("pods") == expect_pods
        image_str = str(out.get("image", "")).lower() if isinstance(out, dict) else ""
        port_str = str(out.get("port", "")) if isinstance(out, dict) else ""
        mem_str = str(out.get("memory", "")) if isinstance(out, dict) else ""
        haystack = image_str + " " + port_str + " " + mem_str
        ok_keywords = all(kw.lower() in haystack for kw in expect_keywords)
        passed = ok_pods and ok_keywords

        results.append({
            "input": text, "expect_pods": expect_pods, "expect_keywords": expect_keywords,
            "output": out, "latency_ms": round(latency_ms, 1), "passed": passed,
        })
        print(f"  [{'OK' if passed else 'FAIL'}] ({latency_ms:6.0f}ms) {text[:50]}")

    accuracy = sum(r["passed"] for r in results) / len(results)
    print(f"  → 準確率: {accuracy:.1%} ({sum(r['passed'] for r in results)}/{len(results)})")
    return {"accuracy": accuracy, "n": len(results), "cases": results}


# ══════════════════════════════════════════════════════════════════
# 2. 意圖分類準確率
# ══════════════════════════════════════════════════════════════════
def run_intent_accuracy():
    print("\n=== 2. 意圖分類準確率（/classify，新標註 20 題，跟 few-shot 不同措辭）===")
    results = []
    for text, expect_action in INTENT_TEST_CASES:
        t0 = _now_ms()
        try:
            out = llama_client.classify_intent(text)
        except Exception as e:
            out = {"action": "error", "error": str(e)}
        latency_ms = _now_ms() - t0

        passed = isinstance(out, dict) and out.get("action") == expect_action
        results.append({
            "input": text, "expect_action": expect_action, "output": out,
            "latency_ms": round(latency_ms, 1), "passed": passed,
        })
        print(f"  [{'OK' if passed else 'FAIL'}] ({latency_ms:6.0f}ms) {text[:40]} "
              f"→ got={out.get('action') if isinstance(out, dict) else out}")

    accuracy = sum(r["passed"] for r in results) / len(results)
    print(f"  → 準確率: {accuracy:.1%} ({sum(r['passed'] for r in results)}/{len(results)})")
    return {"accuracy": accuracy, "n": len(results), "cases": results}


# ══════════════════════════════════════════════════════════════════
# 3. 延遲
# ══════════════════════════════════════════════════════════════════
def _percentile(values, p):
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * p
    f, c = int(k), min(int(k) + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (k - f) * (s[c] - s[f])


def _timed_runs(fn, n):
    times = []
    for _ in range(n):
        t0 = _now_ms()
        try:
            fn()
        except Exception:
            pass
        times.append(_now_ms() - t0)
    return times


def run_latency_benchmark(n=12):
    print(f"\n=== 3. 端點延遲（每個端點 {n} 次呼叫）===")
    out = {}

    def call_infer():
        llama_client.ask_llama(LATENCY_DEPLOY_INPUT)

    def call_classify():
        llama_client.classify_intent(LATENCY_CLASSIFY_INPUT)

    def call_diagnose():
        llama_client.diagnose_with_llm(LATENCY_DIAGNOSE_CONTEXT)

    def call_chat():
        requests.post(f"{MODEL_SERVER_URL}/chat", json={"message": LATENCY_CHAT_INPUT}, timeout=60)

    for name, fn in [("infer", call_infer), ("classify", call_classify),
                      ("diagnose", call_diagnose), ("chat", call_chat)]:
        times = _timed_runs(fn, n)
        stat = {
            "n": n,
            "mean_ms": round(statistics.mean(times), 1),
            "median_ms": round(statistics.median(times), 1),
            "p95_ms": round(_percentile(times, 0.95), 1),
            "min_ms": round(min(times), 1),
            "max_ms": round(max(times), 1),
            "raw_ms": [round(t, 1) for t in times],
        }
        out[name] = stat
        print(f"  {name:10s}: mean={stat['mean_ms']:7.0f}ms  median={stat['median_ms']:7.0f}ms  "
              f"p95={stat['p95_ms']:7.0f}ms")
    return out


# ══════════════════════════════════════════════════════════════════
# 4. 穩定度
# ══════════════════════════════════════════════════════════════════
def run_stability_test(n=10):
    print(f"\n=== 4. 穩定度（同一輸入重複呼叫 {n} 次，看輸出一不一致）===")
    out = {}

    # /infer（temperature=0.3）：部署解析，看 pods/image 是否每次都一樣
    deploy_outs = []
    for _ in range(n):
        try:
            r = llama_client.ask_llama(STABILITY_DEPLOY_INPUT)
            deploy_outs.append((r.get("pods"), str(r.get("image", "")).strip()))
        except Exception:
            deploy_outs.append(None)
    most_common_deploy = max(set(deploy_outs), key=deploy_outs.count)
    deploy_agreement = deploy_outs.count(most_common_deploy) / n
    out["infer_temperature_0.3"] = {
        "input": STABILITY_DEPLOY_INPUT, "n": n,
        "agreement_rate": deploy_agreement, "distinct_outputs": len(set(deploy_outs)),
        "samples": [list(d) if d else None for d in deploy_outs],
    }
    print(f"  /infer  (temp=0.3): 一致率 {deploy_agreement:.0%}，{len(set(deploy_outs))} 種不同結果")

    # /classify（temperature=0.0）：理論上應該幾乎 100% 一致
    classify_outs = []
    for _ in range(n):
        try:
            r = llama_client.classify_intent(STABILITY_CLASSIFY_INPUT)
            classify_outs.append(r.get("action"))
        except Exception:
            classify_outs.append(None)
    most_common_classify = max(set(classify_outs), key=classify_outs.count)
    classify_agreement = classify_outs.count(most_common_classify) / n
    out["classify_temperature_0.0"] = {
        "input": STABILITY_CLASSIFY_INPUT, "n": n,
        "agreement_rate": classify_agreement, "distinct_outputs": len(set(classify_outs)),
        "samples": classify_outs,
    }
    print(f"  /classify (temp=0.0): 一致率 {classify_agreement:.0%}，{len(set(classify_outs))} 種不同結果")

    return out


# ══════════════════════════════════════════════════════════════════
# 5. RAG 檢索品質（重跑既有的 eval_retrieval.py，拿當下真正的索引測）
# ══════════════════════════════════════════════════════════════════
def run_rag_eval():
    print("\n=== 5. RAG 檢索品質（重跑 rag/eval_retrieval.py）===")
    try:
        subprocess.run([sys.executable, "rag/eval_retrieval.py"], check=True,
                        cwd=os.path.dirname(os.path.abspath(__file__)), timeout=120)
        with open(os.path.join(REPORTS_DIR, "eval_rag_report.json"), encoding="utf-8") as f:
            report = json.load(f)
        print("  → 已重新產生 reports/eval_rag_report.json")
        return report
    except Exception as e:
        print(f"  → 跳過（{e}）")
        return {"error": str(e)}


# ══════════════════════════════════════════════════════════════════
# main
# ══════════════════════════════════════════════════════════════════
def main():
    health = requests.get(f"{MODEL_SERVER_URL}/health", timeout=5).json()
    print(f"Model Server 狀態: {health}")
    if not (health.get("deploy_loaded") and health.get("monitor_loaded")):
        print("deploy_loaded / monitor_loaded 不是 true，Model Server 可能還沒載完模型，請稍後再跑。")
        sys.exit(1)

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "note": "USE_LLM_NORMALIZE=0（只在本程序環境變數生效，.env 未變動）——"
                "測的是本地模型+RAG+規則層這一段，不含 Gemini 正規化層。",
        "deploy_accuracy": run_deploy_accuracy(),
        "intent_accuracy": run_intent_accuracy(),
        "latency": run_latency_benchmark(),
        "stability": run_stability_test(),
        "rag_retrieval": run_rag_eval(),
    }

    out_path = os.path.join(REPORTS_DIR, f"research_benchmark_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n完整結果已存到 {out_path}")


if __name__ == "__main__":
    main()
