"""對話存伺服器＋專案（2026-10-21）：core/workspace_store.py 與 web_demo 的 /api/chats、/api/projects。

重點：每個帳號只讀寫得到自己的對話與專案；一個服務只屬一個專案；服務刪掉後歸屬清掉；
刪除專案的各種勾選組合；要刪服務必須輸入正確的專案名稱，有服務刪不掉時專案保留。
"""
import pytest

from core import workspace_store as ws


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    ws.init(str(tmp_path / "ws.db"))
    yield


# ── store ──
def test_chats_are_scoped_to_owner():
    ws.put_chat("alice", "chat_1", "部署 nginx", [{"role": "user", "content": "hi"}])
    ws.put_chat("bob", "chat_1", "bob 的對話", [])
    assert [c["title"] for c in ws.list_chats("alice")] == ["部署 nginx"]
    assert ws.list_chats("alice")[0]["messages"] == [{"role": "user", "content": "hi"}]
    assert ws.delete_chat("alice", "chat_1")
    assert [c["title"] for c in ws.list_chats("bob")] == ["bob 的對話"]


def test_chat_limits_and_id_format():
    with pytest.raises(ws.WorkspaceError):
        ws.put_chat("a", "../etc", "x", [])
    with pytest.raises(ws.WorkspaceError):
        ws.put_chat("a", "chat_big", "x", [{"content": "x" * (ws.MAX_CHAT_BYTES + 10)}])
    with pytest.raises(ws.WorkspaceError):
        ws.put_chat("a", "chat_bad", "x", "not a list")


def test_chat_with_someone_elses_project_becomes_plain_chat():
    p = ws.create_project("bob", "bob 的專案")
    ws.put_chat("alice", "chat_1", "x", [], project_id=p["id"])
    assert ws.list_chats("alice")[0]["projectId"] is None


def test_projects_are_scoped_and_validated():
    p = ws.create_project("alice", "  網路商店  ", "說明")
    assert p["name"] == "網路商店"
    assert ws.get_project("bob", p["id"]) is None
    assert ws.list_projects("bob") == []
    with pytest.raises(ws.WorkspaceError):
        ws.create_project("alice", "   ")
    with pytest.raises(ws.WorkspaceError):
        ws.create_project("alice", "x" * (ws.MAX_PROJECT_NAME + 1))
    with pytest.raises(ws.WorkspaceError):
        ws.update_project("bob", p["id"], name="搶走")


def test_one_service_belongs_to_one_project_and_forget():
    a = ws.create_project("alice", "A")
    b = ws.create_project("alice", "B")
    ws.assign_service("user-alice", "web", "alice", a["id"])
    ws.assign_service("user-alice", "web", "alice", b["id"])      # 移到 B：A 就沒有了
    assert ws.services_of("user-alice", "alice", a["id"]) == []
    assert ws.services_of("user-alice", "alice", b["id"]) == ["web"]
    ws.forget_service("user-alice", "web")
    assert ws.service_projects("user-alice", "alice") == {}
    with pytest.raises(ws.WorkspaceError):
        ws.assign_service("user-bob", "x", "bob", a["id"])        # 不是自己的專案


def test_files_are_scoped_and_size_limited():
    p = ws.create_project("alice", "A")
    f = ws.add_file("alice", p["id"], "../docker-compose.yml", "compose", "services: {}")
    assert "/" not in f["name"] and "\\" not in f["name"]
    assert ws.get_file("bob", p["id"], f["id"]) is None
    assert ws.get_file("alice", p["id"], f["id"])["content"] == "services: {}"
    with pytest.raises(ws.WorkspaceError):
        ws.add_file("alice", p["id"], "big.yml", "compose", "x" * (ws.MAX_FILE_BYTES + 1))


# ── 路由 ──
@pytest.fixture()
def web(monkeypatch):
    wd = pytest.importorskip("web_demo")
    deployed = {"web", "db", "other"}
    deleted = []

    def fake_deps(ns=None):
        return [{"name": n, "replicas": 1, "ready": 1, "sleeping": False} for n in sorted(deployed)]

    def fake_delete(app, ns=None):
        if app == "stuck":
            return False, "叢集沒有回應"
        deployed.discard(app)
        deleted.append(app)
        ws.forget_service(ns, app)
        return True, "ok"

    monkeypatch.setattr(wd, "k8s_get_deployments", fake_deps)
    monkeypatch.setattr(wd, "k8s_delete_deployment", fake_delete)
    monkeypatch.setattr(wd, "_session_ns", lambda: "user-" + wd.session["username"])
    wd._test_deleted = deleted
    wd._test_deployed = deployed
    return wd


def client_as(wd, user):
    c = wd.app.test_client()
    with c.session_transaction() as s:
        s["username"] = user
    return c


def test_routes_require_login(web):
    c = web.app.test_client()
    assert c.get("/api/chats").status_code == 401
    assert c.get("/api/projects").status_code == 401
    assert c.put("/api/chats/chat_1", json={"title": "x", "messages": []}).status_code == 401


def test_chat_roundtrip_and_isolation(web):
    a, b = client_as(web, "alice"), client_as(web, "bob")
    assert a.put("/api/chats/chat_1", json={"title": "你好", "messages": [{"role": "user", "content": "hi"}]}).get_json()["ok"]
    assert a.get("/api/chats").get_json()["chats"][0]["title"] == "你好"
    assert b.get("/api/chats").get_json()["chats"] == []
    b.delete("/api/chats/chat_1")                          # 刪不到別人的
    assert len(a.get("/api/chats").get_json()["chats"]) == 1
    assert a.put("/api/chats/bad id", json={"title": "x", "messages": []}).status_code in (400, 404)


def test_project_view_and_cross_account(web):
    a, b = client_as(web, "alice"), client_as(web, "bob")
    pid = a.post("/api/projects", json={"name": "商店"}).get_json()["project"]["id"]
    assert b.get(f"/api/projects/{pid}").status_code == 404
    assert b.patch(f"/api/projects/{pid}", json={"name": "x"}).status_code == 404
    r = a.post(f"/api/projects/{pid}/services", json={"action": "add", "name": "web"}).get_json()
    assert r["ok"]
    v = a.get(f"/api/projects/{pid}").get_json()["project"]
    assert [s["name"] for s in v["services"]] == ["web"]
    assert "web" not in v["available_services"] and "db" in v["available_services"]
    # 不存在的服務、已經在別的專案的服務
    assert a.post(f"/api/projects/{pid}/services", json={"action": "add", "name": "nope"}).status_code == 404
    pid2 = a.post("/api/projects", json={"name": "另一個"}).get_json()["project"]["id"]
    assert a.post(f"/api/projects/{pid2}/services", json={"action": "add", "name": "web"}).status_code == 409


def test_deploy_assigns_service_only_to_own_project(web):
    with web.app.test_request_context():
        web.session["username"] = "alice"
        mine = ws.create_project("alice", "A")["id"]
        theirs = ws.create_project("bob", "B")["id"]
        web._ws_assign_after_deploy(mine, "web", "user-alice")
        web._ws_assign_after_deploy(theirs, "db", "user-alice")
        web._ws_assign_after_deploy(None, "other", "user-alice")
    assert ws.service_projects("user-alice", "alice") == {"web": mine}


def _setup_full_project(web, c):
    pid = c.post("/api/projects", json={"name": "商店"}).get_json()["project"]["id"]
    c.post(f"/api/projects/{pid}/services", json={"action": "add", "name": "web"})
    c.patch(f"/api/projects/{pid}", json={"instructions": "web 至少 2 個"})
    c.put("/api/chats/chat_1", json={"title": "在專案裡", "messages": [], "projectId": pid})
    ws.add_file("alice", pid, "docker-compose.yml", "compose", "services: {}")
    return pid


def test_delete_move_out_keeps_services_and_chats(web):
    c = client_as(web, "alice")
    pid = _setup_full_project(web, c)
    r = c.post(f"/api/projects/{pid}/delete", json={"remove": {}, "rest": "move"}).get_json()
    assert r["ok"] and r["project_deleted"]
    assert web._test_deleted == []                          # 服務沒被刪
    assert ws.service_projects("user-alice", "alice") == {}
    chats = c.get("/api/chats").get_json()["chats"]
    assert chats[0]["title"] == "在專案裡" and chats[0]["projectId"] is None
    assert ws.list_projects("alice") == []


def test_delete_services_requires_project_name(web):
    c = client_as(web, "alice")
    pid = _setup_full_project(web, c)
    r = c.post(f"/api/projects/{pid}/delete", json={"remove": {"all": True}, "confirm_name": "錯的"})
    assert r.status_code == 400 and web._test_deleted == []
    r = c.post(f"/api/projects/{pid}/delete", json={"remove": {"all": True}, "confirm_name": "商店"}).get_json()
    assert r["ok"] and r["project_deleted"] and web._test_deleted == ["web"]
    assert c.get("/api/chats").get_json()["chats"] == []


def test_delete_keep_only_removes_checked_parts(web):
    c = client_as(web, "alice")
    pid = _setup_full_project(web, c)
    r = c.post(f"/api/projects/{pid}/delete", json={"remove": {"files": True, "instructions": True}, "rest": "keep"}).get_json()
    assert r["ok"] and not r["project_deleted"]
    p = ws.get_project("alice", pid)
    assert p["instructions"] == "" and ws.list_files("alice", pid) == []
    assert ws.services_of("user-alice", "alice", pid) == ["web"]
    # keep 又沒勾任何東西：不做事
    assert c.post(f"/api/projects/{pid}/delete", json={"remove": {}, "rest": "keep"}).status_code == 400


def test_failed_service_delete_keeps_project(web):
    c = client_as(web, "alice")
    pid = _setup_full_project(web, c)
    ws.assign_service("user-alice", "stuck", "alice", pid)
    r = c.post(f"/api/projects/{pid}/delete", json={"remove": {"services": True}, "rest": "move", "confirm_name": "商店"})
    assert r.status_code == 502
    d = r.get_json()
    assert not d["project_deleted"] and "stuck" in d["error"]
    assert ws.get_project("alice", pid) is not None


def test_file_download_is_scoped(web):
    a, b = client_as(web, "alice"), client_as(web, "bob")
    pid = a.post("/api/projects", json={"name": "A"}).get_json()["project"]["id"]
    fid = ws.add_file("alice", pid, "app.yaml", "k8s", "kind: Deployment")["id"]
    r = a.get(f"/api/projects/{pid}/files/{fid}")
    assert r.status_code == 200 and r.data == b"kind: Deployment"
    assert "attachment" in r.headers["Content-Disposition"]
    assert b.get(f"/api/projects/{pid}/files/{fid}").status_code == 404


def test_real_delete_wrapper_forgets_project(monkeypatch):
    wd = pytest.importorskip("web_demo")
    p = ws.create_project("alice", "A")
    ws.assign_service("user-alice", "web", "alice", p["id"])
    monkeypatch.setattr(wd, "_k8s_delete_deployment_by_mode", lambda app, ns=None: (False, "失敗"))
    wd.k8s_delete_deployment("web", "user-alice")
    assert ws.services_of("user-alice", "alice", p["id"]) == ["web"]      # 沒刪掉就不清
    monkeypatch.setattr(wd, "_k8s_delete_deployment_by_mode", lambda app, ns=None: (True, "ok"))
    wd.k8s_delete_deployment("web", "user-alice")
    assert ws.services_of("user-alice", "alice", p["id"]) == []


def test_project_summary_only_counts_project_services(web, monkeypatch):
    """2026-10-23：專案摘要只算專案裡的服務，其他服務的事件不算；別人的專案 404。"""
    import time as _t
    now = _t.time()
    evs = [{"ts": now - 3600, "kind": "deploy", "app": "web", "outcome": "ok", "data": {"image": "nginx", "pods": 1}},
           {"ts": now - 1800, "kind": "scale", "app": "web", "outcome": "ok", "data": {"replicas": 3}},
           {"ts": now - 900, "kind": "deploy", "app": "db", "outcome": "ok", "data": {"image": "redis", "pods": 1}},
           {"ts": now - 600, "kind": "heal", "app": "web", "outcome": "inspect", "data": {"root_cause": "記憶體不足"}}]
    monkeypatch.setattr(web._events, "query", lambda ns, since=0, kinds=None, limit=5000: [e for e in evs if e["ts"] >= since])
    a, b = client_as(web, "alice"), client_as(web, "bob")
    pid = a.post("/api/projects", json={"name": "商店"}).get_json()["project"]["id"]
    empty = a.get(f"/api/projects/{pid}/summary").get_json()
    assert empty["ok"] and empty["deploys"] == 0 and empty["recent"] == [] and empty["services"]["total"] == 0
    a.post(f"/api/projects/{pid}/services", json={"action": "add", "name": "web"})
    d = a.get(f"/api/projects/{pid}/summary?days=7").get_json()
    assert d["deploys"] == 1 and d["changes"] == 1 and d["heals"] == 1 and d["heals_need_you"] == 1
    assert all(e["app"] == "web" for e in d["recent"]) and len(d["recent"]) == 3
    assert d["services"] == {"total": 1, "ready": 1, "sleeping": 0, "attention": 0}
    assert b.get(f"/api/projects/{pid}/summary").status_code == 404
