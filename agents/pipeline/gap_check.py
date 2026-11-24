"""第 3 步：空缺檢查——六個必填欄位哪些還空著（固定規則判斷），空的交給推理代理補，最多 3 次。

3 次之後還空著：
- port／memory／cpu／replicas／app_name：用查表或固定預設值（source=default）
- image：沒辦法替使用者決定，變成問題
port 一律以查表為準（映像檔名稱查得到時，蓋過推理代理猜的 port；使用者講的不蓋）。"""
from __future__ import annotations

import os
import re
import time
from typing import Dict, Optional

from agents.pipeline import defaults, reasoner, schema, software_rag
from agents.pipeline.llm import LLM, extract_json
from agents.pipeline.prompts import CHOOSE_SYSTEM, FIT_SYSTEM, SELECT_SYSTEM

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
    if select_mode() == "choose":
        _choose_software(spec, local)
    else:
        _select_software(spec, local)
    _table_and_defaults(spec)
    adjust_resources(spec)
    still = schema.empty_fields(spec)
    if "image" in still:
        _ask_image(spec)
    facet_questions(spec)
    spec["trace"].append({"step": "gap", "ms": int((time.time() - t0) * 1000),
                          "detail": f"推理 {spec['attempts']['gap']} 次；仍空著：{', '.join(still) or '無'}"})


# 句子裡有上百、上千的人數，或要求一台壞了也要能用：保留推理代理推的副本數。
# 數字要接著人、學生、訂單這類單位才算（不把 port 8080、512Mi、密碼數字、「千萬不要」「萬一」當成人數）；
# 「網站壞掉就通知我」是監控需求，不是高可用，所以「壞掉」要接「一台」才算。
_MANY_USERS = re.compile(
    r"(?:\d{3,}|[一二兩三四五六七八九十幾上數好]+[百千萬])\s*多?\s*[個位名張筆]?.{0,4}?"
    r"(?:人|學生|員工|粉絲|會員|使用者|用戶|客人|顧客|觀眾|訂單|單|users?\b|people)"
    r"|上百|上千|幾百|幾千|數百|數千|成千|上萬", re.I)
_WANTS_HA = re.compile(
    r"壞[掉了]?一台|一台壞|掛[掉了]?一台|一台掛|不能(?:隨便)?(?:停|中斷|斷|掛|當機)|不停機|零停機|高可用|備援|容錯"
    r"|high.?availab|no.?downtime|fail.?over|\bHA\b", re.I)


def _switch_on(name: str, default: str = "1") -> bool:
    """開關型環境變數：0／false／off／no 關閉，沒設或 1／true／on／yes 開啟，其他值警告後當成開啟。
    default：沒設時的值（2026-11-23 加；預設關的新開關傳 "0"）。"""
    v = os.environ.get(name, default).strip().lower()
    if v in ("0", "false", "off", "no"):
        return False
    if v not in ("1", "true", "on", "yes", ""):
        _warn_once(f"{name}={v!r} 不認得，當成開啟")
    return True


def adjust_resources(spec: Dict) -> None:
    """副本數、記憶體依軟體目錄修正。空缺檢查結尾跑一次；意圖檢查（verify）可能改回欄位，runner 之後再跑一次。
    兩個步驟都只改系統推的值、改過一次後再跑不會再變（冪等）。"""
    if not software_rag.field_catalog_ok():
        if not any(t.get("detail") == _NO_CATALOG for t in spec["trace"]):
            spec["trace"].append({"step": "gap", "ms": 0, "detail": _NO_CATALOG})
        return
    _stateful_replicas(spec)
    _memory_floor(spec)
    _tier_resources(spec)


_NO_CATALOG = "軟體目錄讀取失敗，跳過副本數／記憶體依軟體的調整"

# 資源刻度（2026-11-23）：知識表的建議值和題組的標準答案都用這些常見值，倍數算出來的值（×1.5、×1.75）會落在刻度之間
CPU_TIERS = (50, 100, 250, 500, 1000, 2000, 4000)          # m
MEM_TIERS = (64, 128, 256, 512, 1024, 2048, 4096)          # Mi


def _mc(v) -> float:
    """「250m」「1」「0.5」→ 毫核；看不懂回 -1。"""
    s = str(v or "").strip()
    try:
        return float(s[:-1]) if s.endswith("m") else float(s) * 1000
    except ValueError:
        return -1


def _tier_up(tiers, v: float) -> int:
    """v 換到最接近的刻度，再往上一格（已經是最高格就維持）。"""
    i = min(range(len(tiers)), key=lambda j: abs(tiers[j] - v))
    return tiers[min(i + 1, len(tiers) - 1)]


def _fmt_mem(mi: float) -> str:
    return f"{int(mi // 1024)}Gi" if mi >= 1024 and mi % 1024 == 0 else f"{int(mi)}Mi"


def size_tier_mode() -> str:
    v = os.environ.get("PIPELINE_SIZE_TIER", "").strip().lower()
    return v if v in ("floor", "set") else "off"


def _tier_resources(spec: Dict) -> None:
    """資源改用「知識表建議值高一格」（2026-11-23，PIPELINE_SIZE_TIER=floor｜set，預設關）。

    推理代理的 CPU 有 98/157 題（dev_scenario）照抄 prompt 範本的 250m（錨定效應，arXiv 2505.15392）。
    數字交給知識表＋規則、模型只判斷軟體（LASER arXiv 2512.19701 的分工）。開發題組離線模擬：
      現在 scenario 75／claude 202；直接用建議值 64／199；高一格 85／208；高一格當下限 86／205；
      ×2 是 90／208，但 ×1.75、×2.25 就崩（63／194、51／108），所以用刻度、不用倍數。
    floor：低於高一格才調高，不把推得比較大的調小；set：直接設成高一格。
    使用者講的、執行環境（使用者自己的程式）、知識表沒有建議值的都不動。"""
    mode = size_tier_mode()
    image = schema.value(spec, "image")
    if mode == "off" or not image or defaults.name(image) in defaults.RUNTIME_IMAGES:
        return
    rec = {"cpu": software_rag.recommended_cpu(image), "memory": software_rag.recommended_memory(image)}
    changed = []
    for f, conv, tiers, fmt in (("cpu", _mc, CPU_TIERS, lambda x: f"{int(x)}m"), ("memory", _mi, MEM_TIERS, _fmt_mem)):
        b = spec["base"][f]
        r = conv(rec[f])
        if b["source"] == "user" or r <= 0:
            continue
        want = _tier_up(tiers, r)
        cur = conv(b["value"])
        if (mode == "floor" and cur >= want) or cur == want:
            continue
        spec["base"][f] = {"value": fmt(want), "source": b["source"] or "default",
                           "evidence": f"這套軟體建議 {rec[f]}，多留一格餘裕"}
        changed.append(f"{'CPU' if f == 'cpu' else '記憶體'} {fmt(want)}")
    if changed:
        schema.add_notice(spec, "依這套軟體的建議值多留一格餘裕：" + "、".join(changed))
        spec["trace"].append({"step": "gap", "ms": 0, "detail": f"資源刻度（{mode}）：" + "、".join(changed)})


def _stateful_replicas(spec: Dict) -> None:
    """會自己保存資料的軟體（資料庫、快取、佇列、維基、檔案分享、密碼管理、監控…）系統推的副本數改成 1。

    2026-11-19：推理代理的提示詞已有這條規則（prompts.REPLICAS_RULE），4B 模型仍常給 2～3 份
    （uptime-kuma、vaultwarden、redis 等）。多份同時寫同一份資料本來就不對，資料要保存靠儲存空間。
    例外：使用者講的、句子裡有上百上千的人數、或要求一台壞了也要能用（_MANY_USERS、_WANTS_HA）。
    離線模擬（dev_kbfix）：dev_claude 整張全對 +5／−0，dev_scenario 0／0。PIPELINE_STATEFUL_REPLICAS=0 關閉。"""
    if not _switch_on("PIPELINE_STATEFUL_REPLICAS"):
        return
    b = spec["base"]["replicas"]
    try:
        n = int(b["value"])
    except (TypeError, ValueError):
        return
    image = schema.value(spec, "image")
    if b["source"] == "user" or n <= 1 or not image:
        return
    cat = software_rag.category(image)
    if cat not in defaults.STATEFUL_CATEGORIES:
        return
    hit = _MANY_USERS.search(spec["text"]) or _WANTS_HA.search(spec["text"])
    if hit:
        detail = f"副本數維持 {n}：句子提到大量使用者或要能撐過故障（「{hit.group(0)}」）"
        if not any(t.get("detail") == detail for t in spec["trace"]):
            spec["trace"].append({"step": "gap", "ms": 0, "detail": detail})
        return
    spec["base"]["replicas"] = {"value": 1, "source": b["source"], "evidence": f"{cat}會自己保存資料，先開 1 份"}
    schema.add_notice(spec, f"這類軟體（{cat}）會自己保存資料，先開 1 份；如果同時有上百人使用、或需要一台壞了也能繼續用，請把副本數調高")
    spec["trace"].append({"step": "gap", "ms": 0, "detail": f"副本數 {n} → 1（{cat}）"})


def _mi(v) -> float:
    """「512Mi」「1Gi」→ MiB；看不懂回 -1。"""
    s = str(v or "").strip()
    try:
        if s.endswith("Gi"):
            return float(s[:-2]) * 1024
        if s.endswith("Mi"):
            return float(s[:-2])
    except ValueError:
        pass
    return -1


def _memory_floor(spec: Dict) -> None:
    """系統推的記憶體低於知識庫對這套軟體的建議值時，拉到建議值（2026-11-19）。

    推理代理常給得比軟體本身需要的少（rabbitmq 建議 512Mi 給 128Mi、elasticsearch 建議 1Gi 給 512Mi），
    記憶體不夠容器會被系統砍掉（OOMKilled）。只往上調、不往下調；使用者講的不動。
    試過直接改用建議值（會把推得比較多的也降下來）：兩組都變差；當下限：dev_claude 整張全對 +4／−0、dev_scenario +1／−0。
    CPU 同樣做法是持平（+3／−2），不做。PIPELINE_MEMORY_FLOOR=0 關閉。"""
    if not _switch_on("PIPELINE_MEMORY_FLOOR"):
        return
    b = spec["base"]["memory"]
    image = schema.value(spec, "image")
    if b["source"] == "user" or not image:
        return
    # python、node 這類執行環境跑的是使用者自己的程式，目錄的建議值只是通用猜測，不當下限（意圖檢查依句子調低的要留著）
    if defaults.name(image) in defaults.RUNTIME_IMAGES:
        return
    rec = software_rag.recommended_memory(image)
    cur, want = _mi(b["value"]), _mi(rec)
    if cur < 0 or want < 0 or cur >= want:
        return
    spec["base"]["memory"] = {"value": rec, "source": b["source"], "evidence": f"這套軟體建議至少 {rec}"}
    schema.add_notice(spec, f"這套軟體建議至少 {rec} 記憶體，已從 {b['value']} 調高，避免記憶體不足被系統停掉")
    spec["trace"].append({"step": "gap", "ms": 0, "detail": f"記憶體 {b['value']} → {rec}（軟體建議值）"})


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


def _target_ask(spec: Dict) -> Optional[str]:
    """PIPELINE_TARGET_CHECK=on（2026-11-24）：依獨立的目標判斷（normalize._target_check）決定要不要改問。
    回傳理由；不用問就回傳 None。有講語言的自己的程式照原本做法（維持執行環境），不問。"""
    from agents.pipeline.normalize import target_check_mode
    t = spec.get("target")
    if target_check_mode() != "on" or not t or t.get("artifact_given"):
        return None
    # 定時執行的工作不套用（2026-11-24，開發題組驗證）：原本的設計是維持執行環境＋範例指令直接部署。
    # 套用時 scenario 抽樣 80 題多問 7 題、不該問的題 34 → 30；排除後兩組不該問的題都不變，該問的題 9 → 11
    if any(e["kind"] == "schedule" and e["status"] != "dropped" for e in spec["extras"]):
        return None
    if t["target"] == "own_program" and not t.get("runtime_known"):
        return "你要部署的是自己（或別人）寫的程式，但還不知道映像檔或程式語言"
    if t["target"] == "existing_system":
        return f"你說的「{t.get('name_or_ref') or '那一套系統'}」沒有講是哪一套軟體"
    return None


# 自己的程式還沒打包時，可以選的執行環境（值會成為使用者指定的映像檔）
_OWN_RUNTIMES = [("python:3.12-slim", "Python"), ("node:20-alpine", "Node.js（JavaScript）"),
                 ("eclipse-temurin:21-jre", "Java"), ("golang:1.22-alpine", "Go")]


def _ask_own_program(spec: Dict) -> None:
    schema.add_question(spec, "image",
                        "你的程式有打包好的映像檔嗎？有的話請填名稱（例如 yourname/app:1.0）；還沒打包的話，請選程式是用什麼語言寫的",
                        options=[{"value": v, "label": f"用 {n} 寫的，還沒打包"} for v, n in _OWN_RUNTIMES],
                        reason="這是你自己（或別人）寫的程式，系統不能用現成軟體代替")


def facet_questions(spec: Dict) -> None:
    """PIPELINE_TARGET_CHECK=on（2026-11-24）：已經要問軟體時，順便問原句沒講過的面向，最多 2 張
    （SAGE-Agent arXiv 2511.08798：只問有價值、還沒講過的）。每張都有推薦值，看過沒問題可以直接送出。"""
    from agents.pipeline.normalize import target_check_mode
    if target_check_mode() != "on" or not any(q["field"] == "image" for q in spec["questions"]):
        return
    added = 0
    if spec["expose"]["source"] != "user":
        rec = "internal" if spec["expose"]["value"] in ("internal", "none") else "public"
        schema.add_question(spec, "ask:expose", "要讓外面的人（例如用手機）連進來，還是只有內部的程式會用？",
                            options=[{"value": "public", "label": "要讓外面的人連進來"},
                                     {"value": "internal", "label": "只有內部的程式會用"}],
                            recommended=rec, reason="決定要不要對外開放")
        added += 1
    if added < 2 and not any(e["kind"] == "storage" and e.get("source") == "user" for e in spec["extras"]):
        rec = "keep" if any(e["kind"] == "storage" for e in spec["extras"]) else "no"
        schema.add_question(spec, "ask:storage", "資料要不要保存？（重新啟動之後還在）",
                            options=[{"value": "keep", "label": "要保存"}, {"value": "no", "label": "不用保存"}],
                            recommended=rec, reason="決定要不要準備保存資料的空間")


def _ask_image(spec: Dict) -> None:
    """問「要用哪個軟體」：附上用原句查到的候選；推理代理自己推薦的軟體在知識表裡就排第一個。"""
    t = spec.get("target") or {}
    if _target_ask(spec) and t.get("target") == "own_program":
        if not any(q["field"] == "image" for q in spec["questions"]):
            _ask_own_program(spec)
        return
    if _target_ask(spec) and t.get("target") == "existing_system" \
            and not any(q["field"] == "image" for q in spec["questions"]):
        cands = software_rag.candidates(spec["text"])
        schema.add_question(spec, "image", f"你說的「{t.get('name_or_ref') or '那一套系統'}」是哪一套軟體？請填名稱或映像檔",
                            options=software_rag.options(cands),
                            reason="你提到的系統沒有講是哪一套，系統不能自己挑一個代替；下面是依你的描述找到的候選")
        return
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


def select_mode() -> str:
    """choose：從清單選編號、正反兩種順序一致才採用（2026-11-18）；select：原本的重選做法（對照用）。"""
    return "choose" if os.environ.get("PIPELINE_SELECT_MODE", "select").strip().lower() == "choose" else "select"


_NONE = "__none__"


def _choose_once(spec: Dict, local: LLM, order: list, desc: Dict[str, str]) -> Optional[str]:
    """問一次：回傳選到的映像檔、_NONE（都不符合），或 None（輸出看不懂）。"""
    lines = "\n".join(f"{i + 1}. {img}: {desc.get(img, '')[:150]}" for i, img in enumerate(order))
    out = extract_json(local(CHOOSE_SYSTEM, f"User request: {spec['text']}\nSoftware:\n{lines}", 64))
    try:
        n = int(out.get("choice")) if isinstance(out, dict) else None
    except (TypeError, ValueError):
        n = None
    if n == 0:
        return _NONE
    return order[n - 1] if n is not None and 1 <= n <= len(order) else None


def _perm_vote(spec: Dict, local: LLM, pool: list, desc: Dict[str, str]) -> Optional[str]:
    """2026-11-24（PIPELINE_CHOOSE_STYLE=perm）：Permutation Self-Consistency（Tang 等，NAACL 2024，arXiv 2310.07712）。
    整份清單一次選，4B 有位置偏誤（list 版不管怎麼排都選同一個位置）；把清單輪換 len(pool) 次，每個候選在每個位置
    各出現一次，各問一次再投票，位置偏誤互相抵銷。某個軟體拿到過半票才回傳；「都不符合」過半、沒有過半、
    看不懂的都回 None，交回原本的信心度決定（不新增反問）。票數記在 spec["choose_votes"]。"""
    from collections import Counter
    if len(pool) < 2:
        return None
    votes: Counter = Counter()
    for k in range(len(pool)):
        got = _choose_once(spec, local, pool[k:] + pool[:k], desc)
        if got is not None:
            votes[got] += 1
    spec["choose_votes"] = dict(votes)
    if not votes:
        return None
    win, n = votes.most_common(1)[0]
    return win if win != _NONE and n * 2 > len(pool) else None


def _choose_software(spec: Dict, local: Optional[LLM]) -> None:
    """選軟體（2026-11-18，PIPELINE_SELECT_MODE=choose）：使用者沒指定軟體時，在「RAG 前 5 名＋推理代理的選擇」裡決定。
    預設 confidence：逐一問每個候選「是不是使用者要的那類軟體」，讀模型回答 Yes 的機率；最高分過門檻才採用，否則反問。
    （逐一判斷避開清單位置偏誤：實測 4B 不管清單怎麼排都選同一個位置。）拿不到機率時退回 pointwise（第一個說是的），
    list（正反兩序選編號）只留作對照。
    不動的情況：使用者指定的、同一句話已經選過。
    推理代理選的是執行環境（python、node、alpine…）時（2026-11-19）：
      - 定時執行、或使用者給了啟動指令 → 真的是要跑一段程式，維持執行環境
      - 其他 → 仍然檢查現成軟體（實測推理代理常把訊息佇列、快取、網站監控當成「自己的程式」選 node／alpine，
        兩份開發題組共 34 題）；有現成軟體過門檻就換，沒有就維持執行環境，不改成反問
      （試過用關鍵字判斷「使用者自己的程式」：32 題只抓到 8 題、誤判 45 題，不採用）"""
    b = spec["base"]["image"]
    if b["source"] == "user":
        return
    why = _target_ask(spec)
    if why:
        # 2026-11-24（PIPELINE_TARGET_CHECK=on）：使用者自己的程式、或指一套沒講名字的系統，不能用現成軟體代替
        spec["trace"].append({"step": "select", "ms": 0, "detail": f"目標判斷：{why}，不用現成軟體代替，改問使用者"})
        spec["base"]["image"] = {"value": None, "source": None, "evidence": None}
        spec["choose_pick"] = None
        spec["choose_reason"] = why
        spec["questions"] = [q for q in spec["questions"] if q["field"] != "image"]
        _ask_image(spec)
        return
    if local is None:
        return
    cur = b["value"]
    runtime = bool(cur) and defaults.name(cur) in defaults.RUNTIME_IMAGES
    if runtime and (os.environ.get("PIPELINE_RUNTIME_CHOOSE", "1") == "0" or spec["workload"]["value"] == "cronjob"):
        return
    if runtime and any(e["kind"] == "command" and e.get("source") == "user" for e in spec["extras"]):
        _adopt_over_command(spec, cur)
        return
    # 回答問題後會再跑一次 gap_check：原句沒變、軟體也還是上次選的，就沿用，不重算（省 5 次模型呼叫、結果也不會跳動）
    if spec.get("choose_text") == spec["text"] and spec.get("choose_pick") == (software_rag.repo_of(cur) if cur else None):
        return
    spec.pop("choose_scores", None)          # 上一輪的分數、理由不能沿用到這一輪
    spec.pop("choose_reason", None)
    spec.pop("choose_votes", None)
    spec["choose_text"] = spec["text"]
    pool = [c["image"] for c in software_rag.candidates(spec["text"], k=choose_k())]
    cur_repo = software_rag.repo_of(cur) if cur else ""
    if runtime:   # 執行環境本身不參加比較（它是「沒有現成軟體」時的退路）
        pool = [i for i in pool if defaults.name(i) not in defaults.RUNTIME_IMAGES]
    elif cur_repo and cur_repo not in pool:
        pool.append(cur_repo)
    if not pool:
        return
    desc = software_rag.choose_desc()
    t0 = time.time()
    style = os.environ.get("PIPELINE_CHOOSE_STYLE", "confidence").strip().lower()
    if style not in ("confidence", "pointwise", "list", "perm"):
        _warn_once(f"PIPELINE_CHOOSE_STYLE={style!r} 不認得，改用 confidence")
        style = "confidence"
    perm = style == "perm"
    if perm:   # perm＝信心度照算（問題卡選項、網站候選要用），再用輪換順序的清單投票決定
        style = "confidence"
    scored = _confidence_scores(spec, pool, desc) if style == "confidence" else None
    if style == "confidence" and scored is None:
        # 拿不到信心度（llama.cpp 沒回 logprobs、逾時、不是 llama.cpp）：改用逐一判斷，並明白告訴使用者
        style = "pointwise"
        spec["trace"].append({"step": "select", "ms": 0, "detail": "信心度查詢失敗，改用逐一判斷"})
        schema.add_notice(spec, "這次沒有取得系統對軟體的信心度（本地模型沒有回傳機率），軟體欄位不會顯示信心度數字，請自己確認是不是你要的軟體")
    if scored is not None:
        a = z = _decide_by_confidence(spec, scored, desc)
        # 資料庫、快取：推理代理選的這個本身就過門檻，就維持（原本 select 模式的 _NO_SELECT 保護）
        own = dict(scored).get(cur_repo)
        if cur_repo and defaults.name(cur_repo) in _NO_SELECT and own is not None and own >= choose_threshold():
            a = z = cur_repo
        elif perm:
            win = _perm_vote(spec, local, pool, desc)
            if win:
                if win != a:
                    spec["trace"].append({"step": "select", "ms": 0, "detail": f"輪換投票選 {win}（信心度原本 {a}）"})
                a = z = win
    elif style == "list":
        a, z = (_choose_once(spec, local, order, desc) for order in (pool, pool[::-1]))
    else:
        # 逐一判斷：依 RAG 名次，第一個說是的就採用；全部明確說不是＝都不符合。
        # 模型沒回應或輸出看不懂不算「不是」：全部都這樣時維持推理代理原本的選擇，不因暫時的錯誤清掉
        a, answered = _NONE, 0
        if cur_repo in pool and defaults.name(cur_repo) in _NO_SELECT:
            # 資料庫、快取：先問推理代理原本選的（confidence 模式的 _NO_SELECT 保護），說是就維持
            pool = [cur_repo] + [i for i in pool if i != cur_repo]
        for img in pool:
            out = extract_json(local(FIT_SYSTEM, f"User request: {spec['text']}\nSoftware: {img}: {desc.get(img, '')[:200]}", 16))
            fits = out.get("fits") if isinstance(out, dict) else None
            answered += isinstance(fits, bool)
            if fits is True:
                a = img
                break
        if a == _NONE and answered == 0:
            spec["trace"].append({"step": "select", "ms": int((time.time() - t0) * 1000),
                                  "detail": f"本地模型沒有給出判斷，維持 {cur_repo or '未填'}"})
            spec.pop("choose_text", None)    # 模型暫時沒回應：下一輪（回答問題後）要重新判斷，不沿用
            return
        z = a
    # 2026-11-22 反問改善（PIPELINE_CLARITY=on）：信心度只看「軟體符不符合」，看不出「句子有沒有講清楚」。
    # 沒講清楚要哪一類 → 就算信心度高也問；講清楚了但信心度低 → 不問，用 Gemini 建議的或信心度最高的
    clear = _clarity(spec, local)
    if clear is False:
        a = z = _NONE
        spec["choose_reason"] = "判斷你沒有講清楚要哪一類軟體"
    elif clear is True and _switch_on("PIPELINE_TRUST_SUGGEST", "0") and _gemini_suggest(spec):
        # 2026-11-23（PIPELINE_TRUST_SUGGEST=1）：講清楚了而且 Gemini 建議的軟體在知識表裡，就直接用它，不看信心度。
        # scenario 開發題組：選錯軟體的 41 題有 23 題是 Gemini 建議對、系統沒採用；離線模擬選對 116→128、整張全對 67→74
        pick = _gemini_suggest(spec)
        if pick != a or a != z:
            spec["trace"].append({"step": "select", "ms": 0, "detail": f"採用 Gemini 建議的 {pick}（原本 {a if a == z else '未定'}）"})
        a = z = pick
        spec.pop("choose_reason", None)
    elif clear is True and not runtime and (a != z or a in (None, _NONE)):
        pick = _clear_pick(spec)
        if pick:
            a = z = pick
            spec.pop("choose_reason", None)
            schema.add_notice(spec, f"系統依你的描述選了 {pick}，請確認是不是你要的軟體")
    ms = int((time.time() - t0) * 1000)
    if runtime and clear is not False and (a != z or a in (None, _NONE) or defaults.name(a) in defaults.RUNTIME_IMAGES):
        # 沒有現成軟體過門檻：維持執行環境（推理代理原本的判斷），不反問
        spec["choose_pick"] = cur_repo
        spec.pop("choose_reason", None)
        spec["trace"].append({"step": "select", "ms": ms, "detail": f"沒有現成軟體過門檻，維持執行環境 {cur_repo}"})
        return
    if a == z and a not in (None, _NONE):
        if a != cur_repo:
            spec["base"]["image"] = {"value": a, "source": "inferred", "evidence": "依原句從知識表的候選選出"}
            # 換了軟體：推理代理照舊軟體推的 port、名稱不再適用（例：ghost 的 2368 留給 flarum），清掉讓查表重填。
            # 使用者講的不動。記憶體／CPU 是依用量推的、和軟體種類關係較小，保留
            for f in ("port", "app_name"):
                if spec["base"][f]["source"] != "user":
                    spec["base"][f] = {"value": None, "source": None, "evidence": None}
        spec["choose_pick"] = a
        spec["questions"] = [q for q in spec["questions"] if q["field"] != "image"]
        spec["trace"].append({"step": "select", "ms": ms, "detail": f"選 {a}（{style}；原本 {cur_repo or '未填'}）"})
        return
    spec["choose_pick"] = None
    why = spec.get("choose_reason") or ("都不符合" if a == z == _NONE else f"兩次不一致（{a} / {z}）")
    spec["base"]["image"] = {"value": None, "source": None, "evidence": None}
    spec["trace"].append({"step": "select", "ms": ms, "detail": f"{why}，改問使用者；候選：{', '.join(pool)}"})
    # 推薦值：選到的那個（在清單內才算），否則信心度最高的
    rec = a if a not in (None, _NONE) else None
    if rec is None and spec.get("choose_scores"):
        rec = max(spec["choose_scores"], key=lambda x: x[1])[0]
    _ask_image(spec)
    q = next((q for q in spec["questions"] if q["field"] == "image"), None)
    if q is not None and rec:
        q["recommended"] = rec
        scores = spec.get("choose_scores")
        if scores:
            # 有信心度：選項依分數排序；只有落在校準範圍內的才標數字（低分的沒量過，不顯示，避免看起來一樣可靠）
            from agents.pipeline.calibration import reliability
            opts = []
            for img, p in sorted(scores, key=lambda x: -x[1]):
                rel = reliability(p)
                tail = f"（信心度 {rel['pct']}%）" if rel else ""
                opts.append({"value": img, "label": f"{img}：{desc.get(img, '')[:60]}{tail}", "confidence": rel})
            q["options"] = opts + [{"value": "__auto__", "label": "幫我決定"}]
        else:
            opts = [o for o in q.get("options") or [] if o.get("value") not in (rec, "__auto__")]
            q["options"] = [{"value": rec, "label": rec}] + opts + [{"value": "__auto__", "label": "幫我決定"}]


_warned: set = set()


def _warn_once(msg: str) -> None:
    if msg not in _warned:
        _warned.add(msg)
        print(f"[pipeline] 設定警告：{msg}")


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        v = float(raw)
        if 0.0 <= v <= 1.0:
            return v
    except ValueError:
        pass
    _warn_once(f"{name}={raw!r} 不是 0～1 的數字，改用 {default}")
    return default


def choose_k() -> int:
    """選軟體時比較 RAG 前幾名（預設 5；每多一個候選多一次模型呼叫）。"""
    raw = os.environ.get("PIPELINE_CHOOSE_K", "").strip()
    if not raw:
        return 5
    if raw.isdigit() and 1 <= int(raw) <= 15:
        return int(raw)
    _warn_once(f"PIPELINE_CHOOSE_K={raw!r} 不是 1～15 的整數，改用 5")
    return 5


def choose_threshold() -> float:
    """信心度門檻：最高分的候選要超過這個值才自動採用，否則反問。用 dev_scenario 校準、dev_claude 驗證。"""
    return _env_float("PIPELINE_CHOOSE_THRESHOLD", 0.8)


def choose_margin() -> float:
    """第一、二名是不同類型的軟體、而且信心度差距小於這個值＝需求有歧義，反問。0＝不檢查。"""
    return _env_float("PIPELINE_CHOOSE_MARGIN", 0.0)


def _category(img: str, desc: Dict[str, str]) -> str:
    return (desc.get(img) or img).split("：", 1)[0]


def clarity_bounds():
    """方案 C（本地備援）的門檻：Yes 機率 ≥ 上門檻＝清楚、≤ 下門檻＝沒講清楚、中間＝不判斷（照原本做法）。"""
    return _env_float("PIPELINE_CLARITY_LO", 0.1), _env_float("PIPELINE_CLARITY_HI", 0.9)


def _clarity(spec: Dict, local: Optional[LLM]) -> Optional[bool]:
    """使用者有沒有講清楚要哪一類軟體（2026-11-22）。True／False；不判斷或開關沒開回 None。
    優先用 Gemini 正規化時順便給的判斷（方案 E）；沒有就用本地模型讀「Yes」的機率（方案 C）。
    log 模式照樣判斷並記錄，但回 None（不影響結果，評估用）。"""
    from agents.pipeline import normalize
    mode = normalize.clarity_mode()
    if mode == "off":
        return None
    c = spec.get("clarity")
    if c and c.get("source") == "gemini" and c.get("software_clear") is False and _gemini_contradicts(c):
        # Gemini 說沒講清楚，卻自己說得出類別、還建議了知識表裡的軟體：前後矛盾（第一版 prompt 實測 16/36 題這樣），
        # 不採用它的判斷，改用本地判斷
        spec["trace"].append({"step": "clarity", "ms": 0, "detail": f"Gemini 說沒講清楚但建議了 {c.get('suggest')}，改用本地判斷"})
        spec["gemini_clarity"] = c
        c = spec["clarity"] = None
    if not (c and (c.get("source") == "gemini" or c.get("text") == spec["text"])):
        if local is None:
            return None
        from agents.pipeline import llm as pllm
        from agents.pipeline.prompts import CLARITY_PROB_SYSTEM
        p = pllm.local_yes_prob(CLARITY_PROB_SYSTEM, f"User request: {spec['text']}")
        if p is None:
            spec["trace"].append({"step": "clarity", "ms": 0, "detail": "本地判斷拿不到機率，照原本做法"})
            return None
        lo, hi = clarity_bounds()
        res = True if p >= hi else False if p <= lo else None
        c = spec["clarity"] = {"source": "local", "p": round(p, 4), "software_clear": res, "text": spec["text"]}
        spec["trace"].append({"step": "clarity", "ms": 0, "detail": f"本地判斷講清楚的機率 {p:.2f}（門檻 {lo}～{hi}）"})
    elif c.get("source") == "gemini" and c.get("software_clear") is True and local is not None \
            and os.environ.get("PIPELINE_CLARITY_COMBINE", "").strip().lower() == "or":
        # 2026-11-23（PIPELINE_CLARITY_COMBINE=or）：Gemini 說清楚時，再問本地；本地很確定沒講清楚（≤ 下門檻）就照樣問。
        # claude 開發題組：Gemini 該問有問 23/38、本地 29/38，Gemini 判斷偏寬鬆
        if c.get("local_text") != spec["text"]:
            from agents.pipeline import llm as pllm
            from agents.pipeline.prompts import CLARITY_PROB_SYSTEM
            p = pllm.local_yes_prob(CLARITY_PROB_SYSTEM, f"User request: {spec['text']}")
            c["local_p"], c["local_text"] = (None if p is None else round(p, 4)), spec["text"]
        if c.get("local_p") is not None and c["local_p"] <= clarity_bounds()[0]:
            spec["trace"].append({"step": "clarity", "ms": 0, "detail": f"Gemini 說清楚，但本地判斷講清楚的機率只有 {c['local_p']:.2f}，照樣問"})
            return False if mode == "on" else None
    if c.get("source") == "gemini" and c.get("software_clear") is True and c.get("check_clear") is False:
        # 2026-11-23（PIPELINE_CLARITY_CHECK=gemini2）：第二次獨立判斷說沒講清楚 → 問
        spec["trace"].append({"step": "clarity", "ms": 0, "detail": "第二次判斷說沒講清楚要哪一類軟體，照樣問"})
        return False if mode == "on" else None
    return c.get("software_clear") if mode == "on" else None


def _gemini_contradicts(c: Dict) -> bool:
    sug = c.get("suggest")
    if not (c.get("category") and sug):
        return False
    repo = software_rag.repo_of(software_rag.full_name(sug))
    return software_rag.is_known(repo) and defaults.name(repo) not in defaults.RUNTIME_IMAGES


def _gemini_suggest(spec: Dict) -> Optional[str]:
    """Gemini 判斷時建議的軟體：在知識表裡、而且不是執行環境（python、alpine…）才回傳，否則 None。"""
    c = spec.get("clarity") or {}
    sug = c.get("suggest")
    if c.get("source") != "gemini" or not sug:
        return None
    repo = software_rag.repo_of(software_rag.full_name(sug))
    return repo if software_rag.is_known(repo) and defaults.name(repo) not in defaults.RUNTIME_IMAGES else None


def _adopt_over_command(spec: Dict, cur: str) -> None:
    """推理代理選了執行環境、而且有「使用者給的指令」時（2026-11-23，PIPELINE_TRUST_SUGGEST=1 才做）。
    Gemini 會把「一個小程式在背後默默跑，把紀錄收起來」這種描述抽成指令，而 Gemini 抽出來的額外欄位一律標成使用者給的，
    原本這裡直接維持執行環境，現成軟體（rabbitmq、redis、fluentd…）沒機會被選到（claude 開發題組 11 題）。
    Gemini 也判斷講清楚了、建議的軟體在知識表裡又不是執行環境 → 改用它；那條「指令」標成不處理並在部署單說明，
    不把描述當成指令塞給現成軟體。定時執行不走這裡（模擬 2 對 1 錯，維持執行環境）。"""
    c = spec.get("clarity") or {}
    if not (_switch_on("PIPELINE_TRUST_SUGGEST", "0") and normalize_clarity_on() and c.get("source") == "gemini"
            and c.get("software_clear") is True and c.get("check_clear") is not False):
        return
    pick = _gemini_suggest(spec)
    if not pick:
        return
    spec["base"]["image"] = {"value": pick, "source": "inferred", "evidence": "採用 Gemini 依原句建議的現成軟體"}
    for f in ("port", "app_name"):
        if spec["base"][f]["source"] != "user":
            spec["base"][f] = {"value": None, "source": None, "evidence": None}
    for e in spec["extras"]:
        if e["kind"] == "command" and e["status"] == "pending":
            e["status"], e["final"] = "unsupported", True
            e["note"] = f"已改用現成軟體 {pick}，不另外設啟動指令；如果其實是你自己寫的程式，請回上一步改軟體並填指令"
    spec["questions"] = [q for q in spec["questions"] if q["field"] != "image"]
    spec["choose_text"], spec["choose_pick"] = spec["text"], pick
    spec["trace"].append({"step": "select", "ms": 0, "detail": f"採用 Gemini 建議的 {pick}（原本執行環境 {cur}，描述不當成指令）"})


def normalize_clarity_on() -> bool:
    from agents.pipeline import normalize
    return normalize.clarity_mode() == "on"


def _clear_pick(spec: Dict) -> Optional[str]:
    """講清楚了但信心度不夠時要用哪個：Gemini 建議的在知識表裡（而且不是執行環境）就用它，否則用信心度最高的候選。"""
    sug = _gemini_suggest(spec)
    if sug:
        return sug
    scores = spec.get("choose_scores")
    return max(scores, key=lambda x: x[1])[0] if scores else None


def _confidence_scores(spec: Dict, pool: list, desc: Dict[str, str]):
    """每個候選的信心度＝模型回答 Yes 的機率（llama.cpp logprob）。拿不到（非 llama.cpp、連不上）回 None，
    呼叫端改用逐一判斷。結果記在 spec["choose_scores"]，評估時可以離線掃描門檻。"""
    from agents.pipeline import llm as pllm
    from agents.pipeline.prompts import FIT_PROB_SYSTEM
    scored = []
    for img in pool:
        p = pllm.local_yes_prob(FIT_PROB_SYSTEM, f"User request: {spec['text']}\nSoftware: {img}: {desc.get(img, '')[:200]}")
        if p is None:
            return None
        scored.append((img, round(p, 4)))
    spec["choose_scores"] = scored
    return scored


def choose_tie() -> float:
    """分數差距在這個範圍內視為同分，取 RAG 名次較前的（2026-11-19：模型分數常常擠在 0.95～0.99 分不開，
    離線模擬 0.01 在兩份開發題組都最好）。"""
    return _env_float("PIPELINE_CHOOSE_TIE", 0.01)


def _decide_by_confidence(spec: Dict, scored: list, desc: Dict[str, str]) -> str:
    """最高分超過門檻才採用；差距在 choose_tie() 內的視為同分，取 RAG 名次較前的。第一、二名類型不同且分數接近也算沒把握。"""
    order = sorted(range(len(scored)), key=lambda i: (-scored[i][1], i))
    top = scored[order[0]][1]
    tie = choose_tie()
    # scored 依 RAG 名次排列。最高分過門檻時，同分只在過門檻的候選之間比（不能讓同分規則換成門檻以下的軟體而變成反問）
    floor = max(top - tie, choose_threshold()) if top >= choose_threshold() else top - tie
    first = min(i for i in range(len(scored)) if scored[i][1] >= floor)
    order = [first] + [i for i in order if i != first]
    best_img, best = scored[order[0]]
    thr, margin = choose_threshold(), choose_margin()
    if best < thr:
        spec["choose_reason"] = f"最高信心度 {best:.2f}（{best_img}）低於門檻 {thr:.2f}"
        return _NONE
    if margin > 0:
        for i in order[1:]:
            img, p = scored[i]
            if _category(img, desc) != _category(best_img, desc):
                if best - p < margin:
                    spec["choose_reason"] = f"{best_img} {best:.2f} 和 {img} {p:.2f} 類型不同又分數接近"
                    return _NONE
                break
    return best_img


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
    # 2026-11-18：推理代理選的軟體也在 RAG 候選裡＝兩邊意見一致，維持原本選擇，不再請模型重選。
    # 換成 RAG 欄位檢索後候選變得像樣，模型常被句子裡的「不中斷」「偵測故障」帶偏（S25 nginx→uptime-kuma、
    # S17 nginx→httpd）。只有兩邊意見不同時才重選，也省一次模型呼叫。
    # PIPELINE_SELECT_RULE=always 恢復原本「每次都重選」（對照實驗用）。
    if os.environ.get("PIPELINE_SELECT_RULE", "agree") != "always" and any(c["image"] == cur for c in cands):
        spec["trace"].append({"step": "select", "ms": 0, "detail": f"維持 {cur}（RAG 候選也有它）"})
        return
    pool = [cur] + [c["image"] for c in cands if c["image"] != cur]
    if len(pool) < 2:
        return
    desc = software_rag.choose_desc()
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

