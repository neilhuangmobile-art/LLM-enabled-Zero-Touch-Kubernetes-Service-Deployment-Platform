"""rag/field_retriever.py、rag/router.py、知識庫內容的測試（2026-11-15）。

只用 BM25 路徑（不需要 sentence-transformers、不需要 GPU），CI 也能跑。
防汙染測試：開發集、盲測集的句子不得出現在知識庫；只輸出數量，不輸出題目內容。
"""
import json
import os
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

from rag import field_retriever as fr  # noqa: E402
from rag.router import route  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
KDIR = ROOT / "rag" / "knowledge"


def test_catalog_has_verified_ports_and_volumes():
    cat = json.load(open(KDIR / "catalog.json", encoding="utf-8"))
    entries = {e["id"]: e for e in cat["entries"]}
    assert len(entries) >= 30
    assert entries["ghost"]["ports"] == [2368]
    assert entries["ghost"]["volumes"] == ["/var/lib/ghost/content"]
    assert entries["postgres"]["volumes"] == ["/var/lib/postgresql/data"]   # 16 版
    assert "POSTGRES_PASSWORD" in entries["postgres"]["env_required"]
    for e in entries.values():
        assert e["sources"][0].startswith("registry:"), e["id"]


def test_lookup_software_aliases():
    assert fr.lookup_software("mongodb")["image"].startswith("mongo:")
    assert fr.lookup_software("postgresql")["id"] == "postgres"
    assert fr.lookup_software("ghost:5")["id"] == "ghost"
    assert fr.lookup_software("not-a-real-thing") is None


def test_retrieve_for_field_with_known_software():
    r = fr.retrieve_for_field("", "volume", version="ghost")
    assert r and r[0]["value"] == "/var/lib/ghost/content" and r[0]["score"] == 1.0
    assert fr.retrieve_for_field("任何", "app_name") == []   # 名稱不由 RAG 決定


def test_bm25_retrieval_shapes():
    r = fr.retrieve_for_field("想要一個寫文章發表的部落格", "image", method="bm25")
    assert isinstance(r, list)
    for x in r:
        assert {"value", "field", "score", "evidence", "source", "related"} <= set(x)
    reqs = fr.retrieve_requirements("每天半夜自動跑一次備份", method="bm25")
    assert any(x["need"] == "scheduled_job" for x in reqs)
    les = fr.retrieve_lessons("使用者給的密碼被系統覆蓋", method="bm25")
    assert les and les[0]["id"] == "L07"


def test_chinese_tokenizer_splits_bigrams():
    # 舊的 TF-IDF 把整串中文當一個詞；新的切成雙字
    assert "資料" in fr._tokens("資料不能不見") and "不見" in fr._tokens("資料不能不見")


def test_router():
    assert route("deploy", base_complete=True) == []
    assert "catalog" in route("deploy", field="image", scenario=True)
    assert "requirements" in route("deploy", has_extra=True)
    assert route("scale") == [] and route("as_wake") == []
    assert route("qa") == ["knowledge"]
    assert route("troubleshoot")[0] == "troubleshoot"


def test_lessons_and_requirements_schema():
    for r in yaml.safe_load(open(KDIR / "lessons.yaml", encoding="utf-8")):
        assert {"id", "title", "trigger", "wrong", "correct", "evidence", "status"} <= set(r)
        assert r["status"] in ("fixed", "open")
    needs = {r["need"] for r in yaml.safe_load(open(KDIR / "requirements.yaml", encoding="utf-8"))}
    assert {"persistent_storage", "scheduled_job", "credentials", "custom_command"} <= needs


def _ngrams(s, n=8):
    """8 字片段，且至少 6 個是中文：英文技術詞（example.com、kube-system）本來就會共同出現，不算抄襲。"""
    s = "".join(ch for ch in s if not ch.isspace())
    grams = (s[i:i + n] for i in range(len(s) - n + 1))
    return {g for g in grams if sum("一" <= ch <= "鿿" for ch in g) >= 6}


@pytest.mark.parametrize("test_file", ["dev_scenario.jsonl", "blind_scenario.jsonl", "blind_tasks.jsonl",
                                       "blind_tasks_r3.jsonl", "blind_tasks_r4.jsonl"])
def test_knowledge_base_not_contaminated(test_file):
    """題目句子與知識庫內容不得有長片段重疊（8 字以上連續相同視為重疊）。只回報數量。"""
    p = ROOT / "docs" / "bench_blind" / test_file
    if not p.exists():
        pytest.skip(f"{test_file} 不存在")
    texts = [json.loads(x).get("text", "") for x in open(p, encoding="utf-8")
             if x.strip() and x.strip().startswith("{")]
    kb = set()
    for docs in fr.load_docs().values():
        for d in docs:
            kb |= _ngrams(d["text"])
    leaked = sum(1 for t in texts if len(_ngrams(t) & kb) >= 3)
    assert leaked == 0, f"{test_file}：{leaked} 題與知識庫有長片段重疊"
