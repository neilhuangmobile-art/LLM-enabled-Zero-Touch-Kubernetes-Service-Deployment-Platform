"""使用者 2026-11-25 實測回報的兩個問題：
1. 「部署 web 映像檔是 nginx:1.27」被判成換映像檔（把「是」當成新映像檔），部署直接失敗
2. nginx 因為句子有「報名」被推論要保存資料，空的保存空間蓋掉打包好的網頁 → 403、一直重啟"""
import os
import re

import pytest

from agents.pipeline import reasoner, runner, schema

ROOT = os.path.join(os.path.dirname(__file__), "..")

NOT_UPDATE = ["幫我部署一個網站，映像檔是 nginx:1.27", "部署 web 映像檔是 nginx:1.27，port 80",
              "deploy web, image is nginx:1.27", "映像檔用 nginx:1.27 就好"]
UPDATE = [("把 web 的映像檔換成 nginx:1.28", "web", "nginx:1.28"), ("web 映像檔改成 nginx:1.28", "web", "nginx:1.28"),
          ("把 shop 的 image 更新為 redis:7", "shop", "redis:7")]


def _js_update_image_rule():
    """前端 matchClientRule 的那條規則（從網頁原始碼取出，前後端要一致）。"""
    src = open(os.path.join(ROOT, "web_demo.py"), encoding="utf-8").read()
    line = next(ln for ln in src.splitlines() if ln.strip().startswith("['update_image', /\\u628a"))
    body = line[line.index("/") + 1: line.rindex("/i")]
    return re.compile(body, re.I)


@pytest.mark.parametrize("text", NOT_UPDATE)
def test_frontend_rule_needs_change_verb(text):
    m = _js_update_image_rule().search(text)
    assert m is None, f"{text!r} 被判成換映像檔：{m.groups()}"


@pytest.mark.parametrize("text,name,image", UPDATE)
def test_frontend_rule_still_matches_real_updates(text, name, image):
    m = _js_update_image_rule().search(text)
    assert m and (m.group(1), m.group(2)) == (name, image)


@pytest.fixture(scope="module")
def web():
    import web_demo
    return web_demo


@pytest.mark.parametrize("text", NOT_UPDATE)
def test_backend_rule_needs_change_verb(web, text):
    r = web._rule_intent(text)
    assert not r or r["action"] != "update_image"


@pytest.mark.parametrize("text,name,image", UPDATE)
def test_backend_rule_still_matches_real_updates(web, text, name, image):
    assert web._rule_intent(text) == {"action": "update_image", "args": {"name": name, "image": image}}


def _spec(image, source="inferred"):
    s = schema.new_spec("社團報名頁面，大家填完要看得到")
    schema.set_field(s, "image", image, "inferred")
    s["extras"].append({"kind": "storage", "desc": "保存資料（系統建議）", "value": {"size": None}, "evidence": "",
                        "source": source, "status": "pending", "note": ""})
    return s


@pytest.mark.parametrize("image", ["nginx", "nginx:1.27", "httpd:2.4"])
@pytest.mark.parametrize("source", ["inferred", "user"])
def test_static_web_server_gets_no_storage(image, source):
    s = _spec(image, source)
    reasoner.apply_extras(s)
    assert not s["manifest"].get("volumes")
    assert reasoner.STATIC_WEB_STORAGE_NOTICE in s["notices"]
    assert s["extras"][-1]["status"] == "dropped"


def test_other_software_still_gets_storage():
    s = _spec("wordpress")
    reasoner.apply_extras(s)
    assert s["manifest"]["volumes"][0]["mount"]
    assert reasoner.STATIC_WEB_STORAGE_NOTICE not in s["notices"]


def test_resume_does_not_remount_on_nginx():
    s = _spec("nginx")
    reasoner.apply_extras(s)
    s["questions"] = [{"id": "q1", "field": "replicas", "question": "要幾台？", "options": []}]
    s2 = runner.resume(s, {"q1": "1"}, None)
    assert not s2["manifest"].get("volumes")
    assert s2["notices"].count(reasoner.STATIC_WEB_STORAGE_NOTICE) == 1


def test_eval_does_not_count_dropped_storage():
    import sys
    sys.path.insert(0, os.path.join(ROOT, "experiments", "model_select", "reason"))
    from run_pipeline_eval import to_pred
    s = _spec("nginx")
    reasoner.apply_extras(s)
    assert to_pred(s)["storage"] is None
