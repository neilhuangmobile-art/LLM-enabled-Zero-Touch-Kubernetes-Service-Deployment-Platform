"""所有對照實驗與盲測的題目、結果、失敗原因、根本原因、修正狀態彙整（2026-11-08）。

讀：
- 第一次實驗 docs/bench_20261106/（A 部署、A2 追問、B 日常操作、C 故障修復、D 問答、E 匯入；題目由開發端出）
- 第二次實驗 docs/bench_20261107_r2/（before、after；盲測 30 題由 Gemini 出題，gemini-pro 評分；after 另有原 22 題重跑）
- r3 驗證 docs/bench_20261108_r3/（新盲測 15 題）
輸出到 docs/bench_record/：
- all_results.jsonl：一列＝一題×一個系統×一輪（含系統怎麼判斷、結果、失敗原因、根本原因代碼）
- 題目與結果總表.md：給人看的總表
**不讀**還沒跑過的盲測題（blind_tasks_r4.jsonl 等）的內容，只記檔名與題數。

用法：python scripts/bench/build_test_record.py
"""
import json, math, re
from collections import defaultdict, OrderedDict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "docs" / "bench_record"

# ── 根本原因代碼 ────────────────────────────────────────────────────
RC = OrderedDict([
    ("INTENT_QA", ("部署或操作需求被判成一般問答",
                   "規則只認「部署／deploy」字眼；3B 分類器把 qa 當成「其他所有情況」、沒把握就選 qa，新手說法被當成問題，只回一段教學",
                   "意圖判斷改由 Gemini 先判斷並附上使用者現有的服務，固定規則查核（agents/intent_route.py）", "已修（11/08）")),
    ("OPS_NO_NAME", ("操作沒講服務名稱，只反問不執行",
                     "3B 不知道使用者有哪些服務，「那個 nginx 網站」對不到名稱；不准捏造名稱，所以改成反問",
                     "判斷時附上服務清單，從描述對到實際名稱（同上）", "已修（11/08）")),
    ("INTENT_OPS_AS_DEPLOY", ("對現有服務的操作被當成新部署",
                              "3B 分類器把「從原本的數量加倍改成 6 台」判成 deploy，還捏造名稱 web-frontend；部署解析再因為缺映像檔退回",
                              "意圖判斷改由 Gemini＋服務清單（同 INTENT_QA）", "已修（11/08）")),
    ("PORT_CONTAINER", ("使用者說的對外 port 被當成程式監聽的 port",
                        "「端口開 8080」被寫成 containerPort 8080，但 WordPress 實際聽 80，健康檢查一直失敗、服務不就緒",
                        "容器 port 用映像檔已知的監聽 port，使用者講的 port 當成對外的 Service port", "計畫中")),
    ("ROLLBACK_PHRASE",("聽不懂「換回上一版」",
                         "規則只認 rollback／回滾，「換回上一版」落到分類器被判成換映像檔",
                         "前後端規則加入換回／退回／恢復上一版", "已修（11/07）")),
    ("SPEC_FIELDS", ("使用者講的指令、密碼、環境變數、「不用對外」被丟掉",
                     "Gemini 翻譯層的規格格式只有副本數、映像檔、名稱、port、記憶體、CPU 六個欄位，提示詞規定不准加其他欄位；後面的程式支援 command／env／Secret 卻收不到。自動補的密碼與空殼指令反而蓋掉使用者給的值",
                     "規格新增 command、env、storage、schedule、expose；值必須在原句出現；部署單可改", "計畫中")),
    ("NO_PVC", ("要保存資料卻沒有建立儲存空間",
                "部署程式不會產生 PersistentVolumeClaim，規格也沒有欄位表達「要保存」「硬碟多大」",
                "storage 欄位＋依映像檔掛到資料路徑（postgres、mysql、mariadb、mongo、redis、elasticsearch、ghost…）", "計畫中")),
    ("NO_CRONJOB", ("定時工作被做成常駐服務",
                    "部署程式只會產生 Deployment，沒有 CronJob",
                    "schedule 欄位＋產生 CronJob", "計畫中")),
    ("PORT_DEFAULT", ("port 錯誤或背景程式被開對外 port",
                      "已知 port 表只有 8 個資料庫類映像檔，其他一律 80（ghost 2368、grafana 3000 都錯）；背景程式也建了 Service",
                      "擴充 port 表；expose=no 或背景程式不建 Service", "計畫中")),
    ("SCENARIO_IMAGE", ("只描述情境、沒講軟體就退回",
                        "第一次：拿掉「自動補 nginx」後情境句一律缺映像檔。第二次後：情境判斷要有人數或天數；模型合理推薦的 wikijs、gitea 不在已知清單、原句沒提到，被防捏造檢查擋掉",
                        "11/07 網站類情境預填 nginx 建議；計畫：審核過的軟體清單（wiki→wikijs、程式碼→gitea、檔案→filebrowser…）當建議預填", "部分修正，其餘計畫中")),
    ("IMAGE_FABRICATED", ("映像檔被捏造或寫錯",
                          "翻譯層把一般名詞當映像檔（website:latest），或用錯名稱（mongodb 而不是 mongo）",
                          "映像檔要在原句出現或是已知官方映像檔（agents/deploy_fill.image_grounded）；名稱別名表計畫中", "部分修正")),
    ("DB_ENV", ("資料庫缺必要環境變數，啟動失敗",
                "mysql／postgres 沒有密碼就不會啟動，系統沒補",
                "自動產生密碼存進 Secret，部署單可改（agents/deploy_fill.db_required_env）", "已修（11/07），但使用者有給密碼時被蓋掉→見 SPEC_FIELDS")),
    ("BG_COMMAND", ("背景程式沒給指令，容器一啟動就結束",
                    "解析階段從沒產生 command",
                    "11/07 先建保持運作的空殼並說明；使用者自己給的指令要照用→見 SPEC_FIELDS", "部分修正")),
    ("MULTI_SERVICE", ("一句話要兩個服務只部署一個",
                       "部署流程一次只處理一個服務", "拆成多張部署單（未排入）", "未排入")),
    ("EXTRA_PORT", ("要多開管理介面的 port 沒開",
                    "規格只有一個 port", "port 欄位改成可以多個（未排入）", "未排入")),
    ("HEALER_PROBE", ("健康檢查 port 設錯不會自動修",
                      "fix_probe 只加大等待時間，不檢查 port", "比對容器實際監聽的 port 並修正（healer/remediate.py）", "已修（11/07）")),
    ("QA_DEPTH", ("問答漏講關鍵概念", "知識庫沒有對應文件", "補知識庫文件（rag/k8s_docs/pod_status_explained.md）", "已修（11/07）")),
    ("NOT_READY_OTHER", ("部署了但服務沒有就緒（其他原因）", "需逐筆看快照", "—", "待查")),
    ("OTHER", ("其他／需人工判讀", "—", "—", "待查")),
])

# 第一次實驗：逐題人工標記（見 D:/ZeroTouch_對照實驗_20261107 交接說明第八節）
R1_MANUAL = {
    "A05": ["BG_COMMAND"], "A06": ["SCENARIO_IMAGE"], "A07": ["SCENARIO_IMAGE"], "A08": ["SCENARIO_IMAGE"],
    "A09": ["SCENARIO_IMAGE"], "A10": ["SCENARIO_IMAGE"], "A20": ["IMAGE_FABRICATED"], "A21": ["DB_ENV"],
    "A22": ["DB_ENV"], "B3_rollback": ["ROLLBACK_PHRASE"], "F3_probe": ["HEALER_PROBE"],
    "D03": ["QA_DEPTH"], "D06": ["QA_DEPTH"], "D09": ["QA_DEPTH"],
}


# 盲測：關鍵字分類不準的題目，人工看過快照與評分理由後標記（2026-11-08）
BLIND_MANUAL = {
    "B02": ["PORT_CONTAINER"],          # WordPress 6.4、使用者要 8080：containerPort 寫成 8080，實際聽 80
    "B27": ["INTENT_OPS_AS_DEPLOY"],    # 「加倍改成 6 台」被判成新部署
    "B30": ["OPS_NO_NAME"],             # 評分理由裡的「指令」是在說系統只回刪除指令教學，不是規格欄位問題
}


def classify(row):
    """沒通過的 ZeroTouch 紀錄 → 根本原因代碼（關鍵字規則；第一次實驗用人工標記）。"""
    if row["passed"] or row["system"] != "zerotouch":
        return []
    if row["run"] == "r1" or (row["run"] == "r2_after" and row["suite"] != "blind"):
        return R1_MANUAL.get(row["task_id"], ["OTHER"])
    if row["task_id"] in BLIND_MANUAL:
        return BLIND_MANUAL[row["task_id"]]
    why = (row.get("fail_reason") or "")
    route, action = row.get("route"), (row.get("intent") or {}).get("action")
    codes = []
    if route == "chat" and action in ("qa", None) and row.get("category") not in ("危險或不合理的要求",):
        if row.get("category") == "部署完之後的操作":
            codes.append("OPS_NO_NAME" if row.get("setup") else "INTENT_QA")
        else:
            codes.append("INTENT_QA")
    if action == "clarify":
        codes.append("OPS_NO_NAME")
    if re.search(r"PVC|持久|PersistentVolume", why):
        codes.append("NO_PVC")
    if re.search(r"CronJob", why, re.I):
        codes.append("NO_CRONJOB")
    if re.search(r"指令|command|環境變數|密碼|POSTGRES_PASSWORD|MYSQL|sleep|echo", why, re.I) and "INTENT_QA" not in codes:
        codes.append("SPEC_FIELDS")
    if re.search(r"Service|對外開放|port", why) and "INTENT_QA" not in codes:
        codes.append("PORT_DEFAULT")
    if row.get("outcome") == "rejected" and re.search(r"映像|拒絕", why) and row.get("category", "").startswith("只描述情境"):
        codes.append("SCENARIO_IMAGE")
    if re.search(r"ImagePullBackOff|映像檔錯誤|而非 mongo", why):
        codes.append("IMAGE_FABRICATED")
    if re.search(r"沒有部署 nginx|同時有|兩個|另外.*服務", why):
        codes.append("MULTI_SERVICE")
    if re.search(r"15672|管理介面", why):
        codes.append("EXTRA_PORT")
    if not codes:
        codes.append("NOT_READY_OTHER" if row.get("outcome") == "deployed" else "OTHER")
    return list(OrderedDict.fromkeys(codes))


def load(p):
    p = Path(p)
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()] if p.exists() else []


def load_tasks(p):
    p = Path(p)
    return {t["id"]: t for t in (json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()
                                 if x.strip() and not x.startswith("```"))} if p.exists() else {}


def snap_brief(snap):
    return [{k: x.get(k) for k in ("kind", "name", "image", "replicas", "ports", "env", "command")}
            for x in (snap or {}).get("spec", [])]


def rows_r1():
    base = ROOT / "docs" / "bench_20261106"
    out = []
    for suite in ("A_deploy", "A2_followup", "B_ops", "C_fault", "D_qa", "E_import"):
        for r in load(base / f"{suite}.jsonl"):
            d = r.get("detail") or {}
            out.append({"run": "r1", "run_label": "第一次實驗（11/06，開發端出題）", "suite": suite, "task_id": r["task_id"],
                        "category": r.get("category"), "input": r.get("input"), "expected": None, "pass_if": None,
                        "system": r["system"], "round": r.get("round"), "passed": bool(r.get("passed")),
                        "outcome": r.get("outcome") or ("executed" if r.get("executed") else None),
                        "route": None, "intent": r.get("intent"),
                        "how_judged": {k: d.get(k) for k in ("decision", "warnings", "blockers", "message") if d.get(k)} or
                                      {k: r.get(k) for k in ("api", "api_result", "asked_user", "healer_actions", "fault_reason") if r.get(k)},
                        "deployed": snap_brief(r.get("snapshot")), "fail_reason": None if r.get("passed") else r.get("pass_rule"),
                        "judge": "測試程式的固定規則", "latency_s": r.get("latency_s"), "cost_usd": r.get("cost_usd")})
    return out


def rows_blind(run, label, folder, tasks_file):
    tasks = load_tasks(tasks_file)
    graded = {(g["task_id"], g["system"], g["round"]): g for g in load(folder / "blind_graded.jsonl")}
    out = []
    for r in load(folder / "blind_raw.jsonl"):
        g = graded.get((r["task_id"], r["system"], r["round"])) or {}
        t = tasks.get(r["task_id"], {})
        d = r.get("detail") or {}
        out.append({"run": run, "run_label": label, "suite": "blind", "task_id": r["task_id"], "category": r.get("category"),
                    "input": r.get("input"), "expected": t.get("expected"), "pass_if": t.get("pass_if"),
                    "system": r["system"], "round": r["round"], "passed": bool(g.get("passed")), "setup": r.get("setup"),
                    "outcome": r.get("outcome"), "route": d.get("route"), "intent": d.get("intent"),
                    "how_judged": {**(r.get("said") or {}), **({"notices": r["notices"]} if r.get("notices") else {})},
                    "deployed": snap_brief(r.get("snapshot")), "ready": r.get("ready"),
                    "fail_reason": None if g.get("passed") else g.get("judge_reason"),
                    "judge": g.get("judge_model"), "latency_s": r.get("latency_s"), "cost_usd": r.get("cost_usd")})
    return out


def wilson(k, n, z=1.96):
    if not n:
        return None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return f"{100 * max(0, c - h):.1f}–{100 * min(1, c + h):.1f}%"


def md_escape(s):
    return str(s if s is not None else "").replace("|", "／").replace("\n", " ")


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    blind_dir = ROOT / "docs" / "bench_blind"
    r2 = ROOT / "docs" / "bench_20261107_r2"
    rows = rows_r1()
    rows += rows_blind("r2_before", "第二次實驗・盲測・修正前（11/07，Gemini 出題）", r2 / "before", blind_dir / "blind_tasks.jsonl")
    rows += rows_blind("r2_after", "第二次實驗・盲測・修正後（11/08）", r2 / "after", blind_dir / "blind_tasks.jsonl")
    for r in load(r2 / "after" / "A_deploy.jsonl"):
        rows.append({"run": "r2_after", "run_label": "第二次實驗・原 22 題重跑・修正後（已知題型）", "suite": "A_deploy",
                     "task_id": r["task_id"], "category": r.get("category"), "input": r.get("input"), "expected": None,
                     "pass_if": None, "system": r["system"], "round": r.get("round"), "passed": bool(r.get("passed")),
                     "outcome": r.get("outcome"), "route": "deploy", "intent": None,
                     "how_judged": {k: (r.get("detail") or {}).get(k) for k in ("decision", "message") if (r.get("detail") or {}).get(k)},
                     "deployed": snap_brief(r.get("snapshot")), "fail_reason": None if r.get("passed") else r.get("pass_rule"),
                     "judge": "測試程式的固定規則", "latency_s": r.get("latency_s"), "cost_usd": r.get("cost_usd")})
    rows += rows_blind("r3", "r3 驗證・新盲測（11/08，意圖判斷修正後）", ROOT / "docs" / "bench_20261108_r3",
                       blind_dir / "blind_tasks_r3.jsonl")
    for r in rows:
        r["root_causes"] = classify(r)
    with open(OUT / "all_results.jsonl", "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")

    # ── Markdown ──
    L = ["# 對照實驗與盲測：所有題目、結果與失敗原因總表", "",
         "由 `scripts/bench/build_test_record.py` 自動產生（結構化資料在同資料夾 `all_results.jsonl`）。"
         "根本原因代碼：第一次實驗是人工標記；盲測是依評分理由的關鍵字自動分類，個別題目可能需要人工確認。", "",
         "## 一、各次測試總覽", "", "| 測試 | 題目來源 | 系統 | 通過 | 95% 信賴區間 |", "|---|---|---|---|---|"]
    groups = defaultdict(list)
    for r in rows:
        groups[(r["run_label"], r["suite"], r["system"])].append(r)
    for (label, suite, system), rs in groups.items():
        k = sum(1 for r in rs if r["passed"])
        L.append(f"| {label}／{suite} | {'Gemini 盲測' if suite == 'blind' else '開發端出題'} | {system} | {k}/{len(rs)} = {100 * k / len(rs):.1f}% | {wilson(k, len(rs))} |")
    unseen = [p.name for p in sorted(blind_dir.glob("blind_tasks_r*.jsonl")) if p.name != "blind_tasks_r3.jsonl"]
    if unseen:
        L += ["", f"尚未執行的盲測題：{', '.join(unseen)}（開發端沒有讀內容，這份總表也不收錄）。"]

    L += ["", "## 二、根本原因代碼、系統當時怎麼判斷、怎麼修", "",
          "| 代碼 | 現象 | 根本原因（系統怎麼判斷的） | 修法 | 狀態 | ZeroTouch 失敗次數 |", "|---|---|---|---|---|---|"]
    cnt = defaultdict(int)
    for r in rows:
        for c in r["root_causes"]:
            cnt[c] += 1
    for c, (sym, cause, fix, status) in RC.items():
        L.append(f"| {c} | {sym} | {cause} | {fix} | {status} | {cnt.get(c, 0)} |")

    L += ["", "## 三、逐題紀錄", ""]
    by_run = OrderedDict()
    for r in rows:
        by_run.setdefault((r["run_label"], r["suite"]), OrderedDict()).setdefault(r["task_id"], []).append(r)
    for (label, suite), tasks in by_run.items():
        L += [f"### {label}／{suite}", ""]
        for tid, rs in tasks.items():
            r0 = rs[0]
            L.append(f"**{tid}**（{md_escape(r0.get('category'))}）：{md_escape(r0.get('input'))}")
            if r0.get("expected"):
                L.append(f"- 正確處理（出題者）：{md_escape(r0['expected'])}")
            if r0.get("pass_if"):
                L.append(f"- 通過標準（出題者）：{md_escape(r0['pass_if'])}")
            for r in sorted(rs, key=lambda x: (x["system"], x.get("round") or 0)):
                mark = "✅ 通過" if r["passed"] else "❌ 未通過"
                intent = r.get("intent") or {}
                how = f"路徑 {r.get('route') or '—'}" + (f"、意圖 {intent.get('action')}" if intent.get("action") else "")
                dep = "、".join(f"{x.get('image')}×{x.get('replicas')}" + (f" port {x.get('ports')}" if x.get("ports") else "")
                               for x in (r.get("deployed") or [])) or "沒有部署"
                line = f"- {r['system']} 第 {r.get('round')} 輪：{mark}；結果 {r.get('outcome')}；{how}；部署出：{dep}"
                if not r["passed"]:
                    line += f"；失敗原因：{md_escape(r.get('fail_reason'))}"
                    if r["root_causes"]:
                        line += f"；根本原因：{'、'.join(r['root_causes'])}"
                L.append(line)
            L.append("")
    (OUT / "題目與結果總表.md").write_text("\n".join(L), encoding="utf-8")
    print(f"{len(rows)} 筆 → {OUT}")
    print({c: cnt[c] for c in RC if cnt.get(c)})


if __name__ == "__main__":
    main()
