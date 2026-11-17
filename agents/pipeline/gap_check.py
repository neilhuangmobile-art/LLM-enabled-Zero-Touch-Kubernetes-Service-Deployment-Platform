"""第 3 步：空缺檢查——六個必填欄位哪些還空著（固定規則判斷），空的交給推理代理補，最多 3 次。

3 次之後還空著：
- port／memory／cpu／replicas／app_name：用查表或固定預設值（source=default）
- image：沒辦法替使用者決定，變成問題
port 一律以查表為準（映像檔名稱查得到時，蓋過推理代理猜的 port；使用者講的不蓋）。"""
from __future__ import annotations

import time
from typing import Dict, Optional

from agents.pipeline import defaults, reasoner, schema, software_rag
from agents.pipeline.llm import LLM, extract_json
from agents.pipeline.prompts import SELECT_SYSTEM

MAX_ROUNDS = 3


def run(spec: Dict, local: Optional[LLM]) -> None:
    t0 = time.time()
    for rnd in range(1, MAX_ROUNDS + 1):
        if not _needs_model(spec):
            break
        spec["attempts"]["gap"] = rnd
        before = _missing_count(spec)
        out = reasoner.fill_gaps(spec, local)
        if local is None:
            break
        # 模型有回答但補不出新東西（例如它認為要問使用者），再問一次也一樣，不浪費時間重試
        if out and _missing_count(spec) >= before:
            break
        # 模型已經說軟體要問使用者：重試也補不出映像檔
        if out and out.get("questions") and schema.value(spec, "image") is None:
            break
    _verify_image(spec)
    _select_software(spec, local)
    _table_and_defaults(spec)
    still = schema.empty_fields(spec)
    if "image" in still:
        _ask_image(spec)
    spec["trace"].append({"step": "gap", "ms": int((time.time() - t0) * 1000),
                          "detail": f"推理 {spec['attempts']['gap']} 次；仍空著：{', '.join(still) or '無'}"})


def _needs_model(spec: Dict) -> bool:
    """有空著的欄位、或還不知道是哪種服務／要不要對外，才需要問推理代理。"""
    return bool(schema.empty_fields(spec)) or spec["workload"]["value"] is None or spec["expose"]["value"] is None


def _missing_count(spec: Dict) -> int:
    return len(schema.empty_fields(spec)) + (spec["workload"]["value"] is None) + (spec["expose"]["value"] is None)


def _table_and_defaults(spec: Dict) -> None:
    image = schema.value(spec, "image")
    port_src = spec["base"]["port"]["source"]
    table_port = defaults.listen_port(image) if image else None
    if table_port and port_src != "user":
        spec["base"]["port"] = {"value": table_port, "source": "default", "evidence": "查表：映像檔預設 port"}
    if schema.value(spec, "port") is None:
        # 背景程式、排程用不到 port，但部署格式需要一個值；只開內部（ClusterIP），不會對外
        schema.set_field(spec, "port", 80, "default", "預設值")
        if spec["workload"]["value"] in ("worker", "cronjob"):
            schema.set_choice(spec, "expose", "internal", "default")
    if schema.value(spec, "replicas") is None:
        schema.set_field(spec, "replicas", 1, "default", "預設值")
    if image and schema.value(spec, "app_name") is None:
        schema.set_field(spec, "app_name", defaults.name(image), "default", "用映像檔名稱")
    if image and (schema.value(spec, "memory") is None or schema.value(spec, "cpu") is None):
        from agents.cost_agent import recommended_resources
        req = recommended_resources(image, schema.value(spec, "app_name") or "")["requests"]
        schema.set_field(spec, "memory", req["memory"], "default", "依映像檔類型的預設值")
        schema.set_field(spec, "cpu", req["cpu"], "default", "依映像檔類型的預設值")
    if spec["workload"]["value"] is None:
        schema.set_choice(spec, "workload", "service", "default")
    if spec["expose"]["value"] is None:
        schema.set_choice(spec, "expose", "public", "default")
    # 背景程式和排程沒人要連，一律不對外
    if spec["workload"]["value"] in ("worker", "cronjob") and spec["expose"]["value"] == "public":
        spec["expose"] = {"value": "internal", "source": "default"}


# ── 選軟體：查證與候選（agents/pipeline/software_rag.py，用原句查） ─────────────
_ALIASES = {"postgresql": "postgres", "mongodb": "mongo", "apache": "httpd", "node.js": "node", "nodejs": "node",
            "golang": "golang", "mysql": "mysql", "redis": "redis"}


def _verify_image(spec: Dict) -> None:
    """推理代理推出來的映像檔不在知識表裡（多半是捏造的，例如 redis/redis），不採用，改問使用者。使用者講的不查。"""
    b = spec["base"]["image"]
    # 簡稱補成完整名稱：Docker Hub 沒有叫 grafana 的官方映像檔，實際是 grafana/grafana（S08 實測）。使用者講的也補，並告知
    full = software_rag.full_name(b["value"]) if b["value"] else None
    if full and full != b["value"]:
        spec["base"]["image"] = {**b, "value": full}
        schema.add_notice(spec, f"映像檔「{b['value']}」在 Docker Hub 的完整名稱是 {full}，已經幫你改好")
        b = spec["base"]["image"]
    if b["value"] and b["source"] == "inferred" and software_rag.items() and not software_rag.is_known(b["value"]):
        spec["trace"].append({"step": "gap", "ms": 0, "detail": f"推理代理選的映像檔 {b['value']} 不在知識表，改問使用者"})
        spec["base"]["image"] = {"value": None, "source": None, "evidence": None}


def _as_image(text) -> str:
    t = str(text or "").strip().lower()
    t = _ALIASES.get(t, t)
    return t if software_rag.is_known(t) else ""


def _ask_image(spec: Dict) -> None:
    """問「要用哪個軟體」：附上用原句查到的候選；推理代理自己推薦的軟體在知識表裡就排第一個。"""
    cands = software_rag.candidates(spec["text"])
    q = next((q for q in spec["questions"] if q["field"] == "image"), None)
    rec = _as_image(q.get("recommended")) if q else ""
    opts = ([{"value": rec, "label": rec}] if rec else []) + [o for o in software_rag.options(cands) if o["value"] != rec]
    recommended = rec or (cands[0]["image"] if cands else None)
    if opts and recommended:
        opts.append({"value": "__auto__", "label": "幫我決定"})
    if q:
        # 模型的問題原本只有「幫我決定」（或沒選項）時才換成候選；查不到候選就維持模型原本的問法
        if opts and not [o for o in q.get("options") or [] if o.get("value") != "__auto__"]:
            q["options"], q["recommended"] = opts, recommended
        return
    schema.add_question(spec, "image", "你想部署的是什麼軟體或程式？（例如網站、部落格、資料庫，或你自己的程式的映像檔名稱）",
                        options=[o for o in opts if o["value"] != "__auto__"], recommended=recommended,
                        reason="沒辦法從你的描述確定要用哪個軟體，下面是依你的描述找到的候選")


# 資料庫、程式執行環境不換：使用者說要資料庫或跑自己的程式時，推理代理挑的通常就對，候選反而會換成別種
_NO_SELECT = {"postgres", "mysql", "mariadb", "mongo", "redis", "memcached"}


def _select_software(spec: Dict, local: Optional[LLM]) -> None:
    """推理代理推出軟體之後，再用一次本地模型在「它的選擇＋用原句查到的候選」裡挑最合適的（11-17）。
    不改推理代理的輸入；只換推理代理推的（inferred），使用者講的不動；只能選清單裡的，選不出來就維持原本的。"""
    b = spec["base"]["image"]
    if local is None or not b["value"] or b["source"] != "inferred":
        return
    n = defaults.name(b["value"])
    if n in defaults.RUNTIME_IMAGES or n in _NO_SELECT:
        return
    cands = software_rag.candidates(spec["text"])
    cur = software_rag.repo_of(b["value"])
    pool = [cur] + [c["image"] for c in cands if c["image"] != cur]
    if len(pool) < 2:
        return
    desc = {it["image"]: it["desc"] for it in software_rag.items()}
    lines = "\n".join(f"{i + 1}. {img}: {desc.get(img, '')[:150]}" for i, img in enumerate(pool))
    t0 = time.time()
    out = extract_json(local(SELECT_SYSTEM, f"User request: {spec['text']}\nCurrent choice: {cur}\nCandidates:\n{lines}"))
    pick = software_rag.repo_of(out.get("image")) if isinstance(out, dict) else ""
    ms = int((time.time() - t0) * 1000)
    if pick in pool and pick != cur:
        spec["base"]["image"] = {"value": pick, "source": "inferred", "evidence": "依原句從知識表的候選選出"}
        spec["trace"].append({"step": "select", "ms": ms, "detail": f"{cur} → {pick}：{str(out.get('reason', ''))[:150]}"})
    else:
        spec["trace"].append({"step": "select", "ms": ms, "detail": f"維持 {cur}（候選：{', '.join(pool[1:])}）"})

