"""Healer 補強方案階段 4.1／4.2：log 去重＋挑重點、處理手冊檢索（healer/logsift.py，2026-11-25）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from healer import logsift


def test_template_masks_variable_parts():
    a = logsift.template("2026-11-25T10:00:01Z GET /api/1234 from 10.0.0.5:5555 id=0xdeadbeef took 12ms")
    b = logsift.template("2026-11-25T10:00:09Z GET /api/9 from 10.0.0.7:41 id=0xabc took 3ms")
    assert a == b


def test_dedupe_counts_and_keeps_latest_text():
    logs = "\n".join([f"retrying connection attempt {i}" for i in range(20)] + ["FATAL: config missing"])
    groups = logsift.dedupe(logs)
    assert groups[0][1] == 20 and groups[0][0] == "retrying connection attempt 19"
    assert groups[-1][0] == "FATAL: config missing"


def test_sift_keeps_error_lines_and_tail_within_limit():
    noise = [f"INFO request {i} served in {i % 7}ms path=/item/{i * 31}" for i in range(400)]
    noise[50] = "ERROR: could not connect to postgres at db:5432: connection refused"
    logs = "\n".join(noise + [f"DEBUG heartbeat tick {i} uuid=123e4567-e89b-12d3-a456-42661417400{i % 10}" for i in range(30)]
                     + ["shutting down"])
    out = logsift.sift(logs, "CrashLoopBackOff", limit=800)
    assert len(out) <= 800
    assert "could not connect to postgres" in out          # 錯誤行被挑出來
    assert out.splitlines()[-1] == "shutting down"           # 最後一行一定保留
    assert "(x" in out                                       # 重複的行收斂成次數


def test_sift_short_logs_unchanged_except_dedupe():
    assert logsift.sift("a\nb\na") == "a (x2)\nb"
    assert logsift.sift("") == ""


def test_bm25_prefers_matching_doc():
    s = logsift.bm25_scores("oomkilled memory", ["memory oomkilled exit", "image pull failed", "probe failed"])
    assert s[0] > s[1] and s[0] > s[2]


def test_pick_runbook():
    assert logsift.pick_runbook("ImagePullBackOff manifest unknown")["name"] == "image"
    assert logsift.pick_runbook("java.lang.OutOfMemoryError Java heap space")["name"] == "heap"
    assert logsift.pick_runbook("Readiness probe failed connection refused")["name"] == "probe"
    assert logsift.pick_runbook("zzzz qqqq") is None
    book = logsift.pick_runbook("CreateContainerConfigError configmap not found")
    assert book["name"] in ("config", "storage") and len(book["summary"]) <= 500


def test_every_runbook_has_summary_and_source():
    books = logsift._load_runbooks()
    assert len(books) >= 10
    for b in books:
        assert b["keywords"] and b["summary"] and "https://" in b["text"], b["name"]
