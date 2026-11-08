"""實驗 D：K8s 新手問答（ZeroTouch /api/chat vs. Claude Code）

判定：每題事先列出「答案一定要提到的關鍵概念」，每組概念只要出現任一個同義詞就算有提到，全部組別都有提到才算通過。
這是粗略的自動判定（不判斷解釋得好不好），報告會附上原文讓人檢查。
用法：python scripts/bench/bench_qa.py [輪數=2]
"""
import re, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import OUT, SYSTEMS, ZT, ZeroTouch, append, order_for, rounds_to_run, run_claude  # noqa: E402

QS = [
    ("D01", "CrashLoopBackOff 是什麼意思？要怎麼解決？", [["重啟", "重新啟動", "restart"], ["日誌", "log"]]),
    ("D02", "Pod 一直卡在 Pending 可能是什麼原因？", [["資源", "resource", "CPU", "記憶體"], ["排程", "schedul", "節點", "node"]]),
    ("D03", "我的 Pod 出現 OOMKilled，要怎麼辦？", [["記憶體", "memory"], ["上限", "limit"]]),
    ("D04", "Deployment 跟 StatefulSet 差在哪裡？", [["狀態", "stateful", "state"], ["名稱", "順序", "identity", "持久", "儲存", "volume"]]),
    ("D05", "HPA 是什麼？", [["自動", "auto"], ["擴", "scal", "副本", "replica"]]),
    ("D06", "ImagePullBackOff 是什麼意思？", [["映像", "image"], ["拉", "下載", "pull"]]),
    ("D07", "Service 的 ClusterIP 跟 NodePort 差在哪？", [["叢集內", "內部", "cluster"], ["外部", "節點", "node"]]),
    ("D08", "要怎麼讓我的網站用 HTTPS？", [["Ingress", "ingress", "憑證", "cert", "TLS", "tls"]]),
    ("D09", "readiness probe 跟 liveness probe 差在哪？", [["流量", "traffic", "接收", "請求"], ["重啟", "重新啟動", "restart"]]),
    ("D10", "requests 跟 limits 有什麼不同？", [["保證", "預留", "排程", "schedul", "最少"], ["上限", "最多", "超過", "限制"]]),
]


def judge(text, groups):
    t = text or ""
    miss = [g[0] for g in groups if not any(re.search(re.escape(k), t, re.I) for k in g)]
    return (not miss), ("提到全部關鍵概念" if not miss else "缺少：" + "、".join(miss))


if __name__ == "__main__":
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    zt = ZeroTouch() if "zerotouch" in SYSTEMS else None
    for rnd in rounds_to_run(rounds):
        for qid, q, groups in QS:
            for system in order_for(rnd):
                t0 = time.time()
                if system == "zerotouch":
                    r = zt.s.post(ZT + "/api/chat", json={"message": q, "history": []}, timeout=600)
                    try:
                        d = r.json()
                    except Exception:  # noqa: BLE001
                        d = {"reply": r.text[:500]}
                    reply, cost = d.get("reply") or d.get("error") or "", 0.0
                    extra = {"sources": [s.get("source") for s in d.get("sources") or []]}
                else:
                    c = run_claude(q, f"D:/bench/claude_ws/{qid}_r{rnd}", timeout=300,
                                   raw_prompt=f"我是新手，不懂 Kubernetes。請用繁體中文、簡單易懂地回答：{q}")
                    reply, cost = c.get("result") or "", c.get("cost_usd")
                    extra = {"turns": c.get("turns")}
                passed, why = judge(reply, groups)
                out = {"suite": "D_qa", "task_id": qid, "category": "問答", "input": q, "system": system, "round": rnd,
                       "passed": passed, "pass_rule": why, "latency_s": round(time.time() - t0, 2), "cost_usd": cost,
                       "reply": reply[:2000], "reply_len": len(reply), **extra, "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
                append(OUT / "D_qa.jsonl", out)
                print(f"[{out['ts']}] {qid} r{rnd} {system:9} pass={passed!s:5} {out['latency_s']}s ${cost or 0:.3f} | {why}", flush=True)
