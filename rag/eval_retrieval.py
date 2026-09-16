"""
rag/eval_retrieval.py
檢索品質評估：用一組有標準答案（預期來源文件）的查詢，
量化比較新版 ChromaDB 語意搜尋與舊版 TF-IDF 方法的準確率與延遲。

指標：
    - Recall@K：top-K 結果中有命中預期來源文件的查詢比例
    - MRR（Mean Reciprocal Rank）：命中結果排名倒數的平均值
    - 平均延遲（ms）

輸出：reports/eval_rag_report.json + 終端機摘要

使用方式：
    python rag/eval_retrieval.py
    python rag/eval_retrieval.py --top-k 5
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.config import ensure_utf8_output, REPORTS_DIR
ensure_utf8_output()

import json
import time
from typing import Callable, Dict, List, Optional

REPORT_PATH = os.path.join(REPORTS_DIR, "eval_rag_report.json")

# 手工標註的測試集：query -> 預期來源文件（可以有多個合理答案，
# 例如「image tag 策略」在 deployment_patterns.md 與 security_best_practices.md
# 都有覆蓋，兩者皆算命中）。查詢刻意用口語化敘述而非照抄標題，
# 用來測試語意理解而不是關鍵字比對。
EVAL_SET = [
    {"query": "我的 pod 一直重啟，狀態顯示 CrashLoopBackOff，該怎麼辦？", "expected_sources": ["common_errors.md"]},
    {"query": "Pod 被 OOMKilled，exit code 137，是什麼原因造成的？", "expected_sources": ["common_errors.md"]},
    {"query": "deployment 一直卡在 ImagePullBackOff，可能是什麼問題？", "expected_sources": ["common_errors.md"]},
    {"query": "Why is my pod stuck in Pending and never getting scheduled onto a node?", "expected_sources": ["common_errors.md"]},
    {"query": "容器狀態顯示 CreateContainerConfigError 代表什麼？", "expected_sources": ["common_errors.md"]},
    {"query": "Pod 一直卡在 Terminating 刪不掉要怎麼處理？", "expected_sources": ["common_errors.md"]},
    {"query": "How should I decide how many replicas a Deployment should run?", "expected_sources": ["deployment_patterns.md"]},
    {"query": "readiness probe 跟 liveness probe 差在哪裡？", "expected_sources": ["deployment_patterns.md"]},
    {"query": "怎麼設定 HPA 讓副本數依 CPU 使用率自動調整？", "expected_sources": ["deployment_patterns.md"]},
    {"query": "ConfigMap 跟 Secret 要怎麼掛載成容器的環境變數？", "expected_sources": ["deployment_patterns.md"]},
    {"query": "image tag 應該固定版本還是可以用 latest？", "expected_sources": ["deployment_patterns.md", "security_best_practices.md"]},
    {"query": "requests 跟 limits 這兩個資源設定有什麼差別？", "expected_sources": ["resource_management.md"]},
    {"query": "跑一個 Java Spring Boot 服務，resource requests/limits 大概怎麼抓比較合理？", "expected_sources": ["resource_management.md"]},
    {"query": "CPU 資源寫 500m 是什麼意思？", "expected_sources": ["resource_management.md"]},
    {"query": "怎麼用 ResourceQuota 限制整個 namespace 能用的資源總量？", "expected_sources": ["resource_management.md"]},
    {"query": "Kubernetes 的 QoS 等級 Guaranteed / Burstable / BestEffort 是怎麼判斷的？", "expected_sources": ["resource_management.md"]},
    {"query": "How do I stop containers from running as privileged or using hostNetwork?", "expected_sources": ["security_best_practices.md"]},
    {"query": "怎麼設定 RBAC 讓 ServiceAccount 只有最小必要權限？", "expected_sources": ["security_best_practices.md"]},
    {"query": "NetworkPolicy 要怎麼設成預設拒絕所有流量再開放特定連線？", "expected_sources": ["security_best_practices.md"]},
    {"query": "Secret 用 base64 儲存是不是就等於加密了？", "expected_sources": ["security_best_practices.md"]},
    {"query": "怎麼用 Trivy 掃描容器映像有沒有安全漏洞？", "expected_sources": ["security_best_practices.md"]},
    {"query": "這個平台的 Healer 功能實際上是做什麼用的？", "expected_sources": ["zerotouch_user_guide.md"]},
    {"query": "要怎麼在這個系統上看部署歷史並且 rollback？", "expected_sources": ["zerotouch_user_guide.md"]},
    {"query": "Dataset Manager 頁面是用來做什麼的？", "expected_sources": ["zerotouch_user_guide.md"]},
]


def _hit_rank(results: List[Dict], expected_sources: List[str], top_k: int) -> Optional[int]:
    for i, r in enumerate(results[:top_k], 1):
        if r.get("source") in expected_sources:
            return i
    return None


def evaluate_method(method_name: str, retrieve_fn: Callable[[str, int], List[Dict]],
                     top_k: int = 3) -> Dict:
    hits = 0
    reciprocal_ranks = []
    latencies_ms = []
    per_query = []

    for case in EVAL_SET:
        t0 = time.time()
        try:
            results = retrieve_fn(case["query"], top_k) or []
        except Exception as e:
            results = []
            print(f"[Eval] {method_name} 查詢失敗：{e}")
        latencies_ms.append((time.time() - t0) * 1000)

        rank = _hit_rank(results, case["expected_sources"], top_k)
        if rank:
            hits += 1
            reciprocal_ranks.append(1.0 / rank)
        else:
            reciprocal_ranks.append(0.0)

        per_query.append({
            "query":       case["query"],
            "expected":    case["expected_sources"],
            "hit_rank":    rank,
            "top_result":  results[0]["source"] if results else None,
            "top_score":   results[0]["score"] if results else None,
        })

    n = len(EVAL_SET)
    return {
        "method":         method_name,
        "recall_at_k":    round(hits / n, 3),
        "mrr":            round(sum(reciprocal_ranks) / n, 3),
        "avg_latency_ms": round(sum(latencies_ms) / n, 1),
        "num_queries":    n,
        "per_query":      per_query,
    }


def run(top_k: int = 3) -> Dict:
    """比較 chroma 與 tfidf 兩種 backend 在同一測試集上的表現，即使目前只有一種是啟用中的。"""
    from rag import retriever, vector_store

    results = {}

    try:
        if vector_store.is_available() and vector_store.count() > 0:
            results["chroma"] = evaluate_method(
                "chroma (sentence-transformers)",
                lambda q, k: retriever._retrieve_chroma(q, k, 0.0),
                top_k,
            )
        else:
            results["chroma"] = {"method": "chroma", "error": "ChromaDB 未安裝或索引是空的"}
    except Exception as e:
        results["chroma"] = {"method": "chroma", "error": str(e)}

    try:
        results["tfidf"] = evaluate_method(
            "tfidf (legacy)",
            lambda q, k: retriever._retrieve_tfidf(q, k, 0.0),
            top_k,
        )
    except Exception as e:
        results["tfidf"] = {"method": "tfidf", "error": str(e)}

    report = {
        "generated_at": time.time(),
        "top_k":        top_k,
        "num_queries":  len(EVAL_SET),
        "results":      results,
    }

    os.makedirs(REPORTS_DIR, exist_ok=True)
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    return report


def print_report(report: Dict) -> None:
    print("=" * 60)
    print(f"RAG 檢索品質評估報告（top_k={report['top_k']}，{report['num_queries']} 條測試查詢）")
    print("=" * 60)
    for key in ("chroma", "tfidf"):
        r = report["results"].get(key)
        if not r:
            continue
        if "error" in r:
            print(f"\n[{key}] 無法評估：{r['error']}")
            continue
        print(f"\n[{r['method']}]")
        print(f"  Recall@{report['top_k']}: {r['recall_at_k'] * 100:.1f}%")
        print(f"  MRR:            {r['mrr']:.3f}")
        print(f"  平均延遲:        {r['avg_latency_ms']:.1f}ms")

    chroma, tfidf = report["results"].get("chroma"), report["results"].get("tfidf")
    if chroma and tfidf and "error" not in chroma and "error" not in tfidf:
        delta = (chroma["recall_at_k"] - tfidf["recall_at_k"]) * 100
        print(f"\nChromaDB 相較 TF-IDF 的 Recall@{report['top_k']} 提升：{delta:+.1f} 個百分點")
    print(f"\n完整報告已存至：{REPORT_PATH}\n")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="RAG 檢索品質評估")
    parser.add_argument("--top-k", type=int, default=3, help="評估用的 top-K")
    args = parser.parse_args()

    report = run(top_k=args.top_k)
    print_report(report)
