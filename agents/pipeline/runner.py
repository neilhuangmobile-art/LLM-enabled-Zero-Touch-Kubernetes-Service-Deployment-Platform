"""把各步驟串起來，並換成和 llama_client.ask_llama 相同格式的結果。

run()：正規化 → 意圖檢查② → 空缺檢查 → 額外欄位對應。
resume()：使用者回答問題卡之後，從原本的中間 JSON 接著跑（不重新正規化，使用者講過的欄位不會被蓋掉）。"""
from __future__ import annotations

import copy
import time
from typing import Dict, Optional

from agents.pipeline import format_check, gap_check, intent_check, normalize, reasoner, schema, verify
from agents.pipeline.llm import LLM


def run(text: str, gemini: Optional[LLM], local: Optional[LLM]) -> Dict:
    t0 = time.time()
    spec = schema.new_spec(text)
    normalize.normalize(spec, gemini, local)
    intent_check.run(spec, local)
    _finish(spec, local, t0)
    return spec


def resume(prior: Dict, answers: Dict[str, str], local: Optional[LLM]) -> Dict:
    """answers：{問題 id: 選的值或自己打的字}。"__auto__" 代表「幫我決定」＝採用推薦值。"""
    t0 = time.time()
    spec = copy.deepcopy(prior)
    questions, spec["questions"] = spec.get("questions", []), []
    notes = []
    for q in questions:
        ans = str(answers.get(q["id"], "") or "").strip()
        # 「幫我決定」＝採用系統推薦，不是使用者自己講的：來源記成 inferred，部署單不會顯示 100%（2026-11-19）
        auto = ans == "__auto__"
        if auto:
            ans = str(q.get("recommended") or "")
        if not ans:
            schema.add_question(spec, q["field"], q["question"], q.get("options"), q.get("recommended"), q.get("reason", ""))
            continue
        field = q["field"]
        if ans == "cancel":
            spec["cancelled"] = True
        elif field == "schedule" and ans == "run_forever":
            for e in spec["extras"]:
                if e["kind"] == "schedule":
                    e["status"], e["note"] = "dropped", "使用者選擇改成一直執行"
            schema.set_choice(spec, "workload", "worker", "user")
        elif field == "storage_size":
            for v in spec["manifest"].get("volumes") or []:
                v["size"] = ans if ans.endswith("Gi") else v["size"]
        elif field.startswith("verify:"):
            verify.resume_answer(spec, field, ans, auto=auto)
        elif field.startswith("format:") or field == "schedule_interval":
            format_check.resume_answer(spec, field, ans)
        elif field == "cron_command":
            # 定時執行要跑的指令（使用者自己打的）：當成使用者講的啟動指令
            spec["extras"].append({"kind": "command", "desc": "定時執行的指令", "value": ans[:1000], "evidence": "",
                                   "source": "user", "status": "pending", "note": ""})
        elif field.startswith("extra:"):
            if ans == "drop":
                for e in spec["extras"]:
                    if e["evidence"][:30] == field[len("extra:"):]:
                        e["status"] = "dropped"
        elif field == "image" and auto:
            if schema.set_field(spec, "image", ans, "inferred", "幫我決定（系統推薦）"):
                # 選軟體步驟不要再重算一次（同一句話、就是這個推薦）
                from agents.pipeline import software_rag
                spec["choose_pick"] = software_rag.repo_of(schema.value(spec, "image"))
                spec["choose_text"] = spec["text"]
        elif field == "image" and schema.set_field(spec, "image", ans, "user", "使用者回答"):
            pass
        else:
            notes.append(f"{q['question']} → {ans}")
    if notes:   # 自由回答：附在原句後面，讓推理代理接著用；evidence 也能對得到
        spec["text"] += "\n（使用者補充）" + "；".join(notes)
    # 先前對應不到的額外欄位（例如當時還不知道映像檔），用新資訊再試一次
    for e in spec["extras"]:
        if e["status"] == "unsupported" and e["kind"] != "schedule":
            msg = f"「{e['evidence'] or e['desc']}」：{e['note']}"
            spec["notices"] = [n for n in spec["notices"] if n != msg]
            e["status"], e["note"] = "pending", ""
    spec["trace"].append({"step": "resume", "ms": 0, "detail": f"回答 {len(answers)} 題"})
    if not spec.get("cancelled"):
        _finish(spec, local, t0)
    return spec


def _finish(spec: Dict, local: Optional[LLM], t0: float) -> None:
    gap_check.run(spec, local)
    # 意圖符合檢查（11-17）：系統填的值有沒有和使用者講的矛盾。有問題卡還沒回答就先不檢查，回答完 resume 會再跑一次
    if not spec["questions"]:
        verify.run(spec, local, refill=lambda s: gap_check.run(s, local))
        # 意圖檢查可能把副本數改回 2、把記憶體改低：依軟體目錄的修正再套一次（冪等，使用者講的不動）
        gap_check.adjust_resources(spec)
    reasoner.apply_extras(spec)
    # 最後的整合與格式檢查（手稿「agent_格式及負荷檢查」）：格式不合格的值試著修正、修正後重新檢查
    format_check.run(spec, local)
    spec["total_ms"] = int((time.time() - t0) * 1000)


def to_payload(spec: Dict) -> Dict:
    """轉成 ask_llama 的格式，_prepare_deploy 後面的步驟（補全、情境、負荷檢查、審查）照常接手。"""
    if spec.get("cancelled"):
        return {"error": "已取消部署", "_pipeline": _public(spec)}
    b = spec["base"]
    out = {
        "app_name": b["app_name"]["value"] or "",
        "image": b["image"]["value"] or "",
        "pods": b["replicas"]["value"] or 1,
        "port": b["port"]["value"] or 80,
        "_parser": "multiagent",
        "_pipeline": _public(spec),
    }
    for f in ("memory", "cpu"):
        if b[f]["value"]:
            out[f] = b[f]["value"]
    man = spec["manifest"]
    for k in ("env", "secret_keys", "volumes", "command", "extra_ports"):
        if man.get(k):
            out[k] = copy.deepcopy(man[k])
    if spec["expose"]["value"] in ("internal", "none"):
        out["expose"] = False
    # 定時執行（11-17）：排程一定要有，副本數固定 1、不對外
    if spec["workload"]["value"] == "cronjob" and man.get("schedule"):
        out.update({"workload": "cronjob", "schedule": man["schedule"], "pods": 1, "expose": False})
    return out


def _public(spec: Dict) -> Dict:
    """給前端與紀錄看的部分（含原句的中間 JSON，回答問題時整份送回來）。"""
    keep = ("text", "purpose", "base", "workload", "expose", "extras", "manifest", "questions", "notices", "verified", "verify_skip", "format_fixed", "rejected",
            "trace", "attempts", "backend", "total_ms", "cancelled", "choose_scores", "choose_reason", "choose_text", "choose_pick")
    return {k: copy.deepcopy(spec[k]) for k in keep if k in spec}
