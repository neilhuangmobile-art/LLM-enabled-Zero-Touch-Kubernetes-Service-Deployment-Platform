"""
過濾自動產生的情境描述（2026-11-15，做法參考 Doc2Query--，Gospodinov et al., ECIR 2023）

Doc2Query-- 發現：自動產生的查詢句有一部分是幻想或太籠統，會拖累檢索；依「與原文件的相關程度」
濾掉最差的一部分後，檢索反而變好。這裡的對應做法：
  專屬度 = 這句描述與「自己軟體的英文簡介」的向量相似度
          − 這句描述與「最像的 5 個其他軟體簡介」的平均相似度
專屬度最低的 p 比例刪掉。只處理自動擴充的條目（generated: true），人工目錄不動。
只用目錄本身的資料（英文簡介、情境描述），不讀開發集或盲測集。

用法：python rag/filter_scenarios.py <catalog.json> <輸出目錄> 0.2 0.35
輸出：<輸出目錄>/scen_drop_<p>.json（要排除的 "軟體id#序號" 清單）＋ scen_scores.json（每句分數，方便抽查）
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from rag.field_retriever import embed  # noqa: E402


def main(cat_path, out_dir, ps):
    entries = json.load(open(cat_path, encoding="utf-8"))["entries"]
    gen = [e for e in entries if e.get("generated")]
    desc = [f"{(e.get('upstream') or {}).get('name', e['id'])}: {(e.get('upstream') or {}).get('description', '')}" for e in gen]
    dv = embed(desc)
    keys, sents, owner = [], [], []
    for i, e in enumerate(gen):
        for j, s in enumerate(e["scenarios"]):
            keys.append(f"{e['id']}#{j}")
            sents.append(s)
            owner.append(i)
    sv = embed(sents)
    sim = sv @ dv.T                        # 句子 × 軟體簡介
    scores = []
    for k in range(len(sents)):
        own = float(sim[k, owner[k]])
        row = sim[k].copy()
        row[owner[k]] = -1
        others = sorted(row.tolist(), reverse=True)[:5]
        scores.append(own - sum(others) / len(others))
    order = sorted(range(len(sents)), key=lambda k: scores[k])
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    json.dump([{"key": keys[k], "text": sents[k], "score": round(scores[k], 4)} for k in order],
              open(out / "scen_scores.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    for p in ps:
        drop = [keys[k] for k in order[: int(len(order) * p)]]
        json.dump(drop, open(out / f"scen_drop_{p}.json", "w", encoding="utf-8"), ensure_ascii=False)
        print(f"p={p}：{len(sents)} 句中刪除 {len(drop)} 句")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], [float(x) for x in sys.argv[3:]])
