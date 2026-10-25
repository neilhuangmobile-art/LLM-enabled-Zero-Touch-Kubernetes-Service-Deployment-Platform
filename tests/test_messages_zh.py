"""部署規格的錯誤訊息改中文（2026-10-24，柏碩交接文件第五部分「少數輸入檢查的訊息還是英文」）。"""
import json

import pytest

wd = pytest.importorskip("web_demo")


@pytest.fixture
def prep(monkeypatch):
    monkeypatch.setattr(wd, "_review_deployment", lambda p: {"decision": "approve", "blockers": []})

    def run(spec):
        with wd.app.test_request_context():
            wd.session["username"] = "zh-msg"
            _, _, _, err = wd._prepare_deploy("x", spec)
        return err
    return run


BASE = {"app_name": "web", "image": "nginx:1.27", "pods": 1, "port": 80}


@pytest.mark.parametrize("patch,expect", [
    ({"pods": 500}, "副本數要在 1 到 100 之間。"),
    ({"port": 70000}, "Port 要在 1 到 65535 之間。"),
    ({"memory": "lots"}, "記憶體的寫法要像 128Mi 或 1Gi。"),
], ids=["pods", "port", "memory"])
def test_spec_errors_are_chinese(prep, patch, expect):
    payload, status = prep(dict(BASE, **patch))
    assert status == 422 and payload["error"] == expect


def test_missing_fields_named_in_chinese(prep):
    payload, status = prep({"app_name": "web", "pods": 2, "port": 80})
    assert status == 422 and payload["error"] == "還缺少：映像檔。" and payload["missing"] == ["image"]


def test_scale_and_delete_messages(monkeypatch):
    c = wd.app.test_client()
    with c.session_transaction() as s:
        s["username"] = "zh-msg"
    from core.web_security import RateLimiter
    monkeypatch.setattr(wd, "_rate_limiter", RateLimiter())
    post = lambda url, body: c.post(url, data=json.dumps(body), content_type="application/json").get_json()
    assert post("/api/delete", {"name": ""})["error"] == "請提供服務名稱。"
    assert post("/api/scale", {"name": "web", "replicas": "x"})["error"] == "副本數要是整數。"
    assert post("/api/chat", {"message": ""})["error"] == "訊息是空的。"
