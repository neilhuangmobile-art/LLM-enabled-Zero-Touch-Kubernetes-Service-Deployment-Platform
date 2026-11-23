"""scripts/build_software_catalog.py 的 --add-selfhosted（2026-11-23）：選 port、對名稱、下載次數門檻。假的 Docker Hub，不連網。"""
import importlib.util
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("build_software_catalog", os.path.join(ROOT, "scripts", "build_software_catalog.py"))
bsc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bsc)


class _Resp:
    def __init__(self, code, data):
        self.status_code, self._d = code, data

    def json(self):
        return self._d


class _FakeHub:
    def __init__(self, repos, search, layers):
        self.repos, self.search, self.layers = repos, search, layers

    def get(self, url):
        if "/search/repositories/" in url:
            return _Resp(200, {"results": self.search})
        if url.endswith("/tags/latest/images"):
            return _Resp(200, [{"architecture": "amd64", "layers": [{"instruction": i} for i in self.layers]}])
        if "/tags/?" in url:
            return _Resp(200, {"results": [{"name": "latest"}]})
        name = url.split("/v2/repositories/")[1].rstrip("/")
        return _Resp(200, {"pull_count": self.repos[name]}) if name in self.repos else _Resp(404, {})


APP = {"name": "Vaultwarden", "desc": "Password manager", "owner": "dani-garcia", "repo": "vaultwarden"}


def test_pick_port_prefers_web_ports():
    assert bsc._pick_port([53, 67, 80, 443]) == 80
    assert bsc._pick_port([3170, 3000, 80]) == 80
    assert bsc._pick_port([9200, 9300]) == 9200
    assert bsc._pick_port([]) is None


def test_resolve_prefers_publisher_and_exact_name():
    hub = _FakeHub({}, [{"repo_name": "vaultwarden/server", "pull_count": 5_000_000},
                        {"repo_name": "someone/vaultwarden", "pull_count": 90_000_000},
                        {"repo_name": "vaultwarden/vaultwarden-backup", "pull_count": 9_000_000},
                        {"repo_name": "other/unrelated", "pull_count": 99_000_000}],
                   ["EXPOSE map[80/tcp:{}]", "VOLUME [/data]"])
    x = bsc._resolve(hub, APP)
    # 別人重包的（someone/）、零件（-backup）、不相關的都不收；帳號就是軟體名稱的 vaultwarden/server 收
    assert x["image"] == "vaultwarden/server" and x["port"] == 80 and x["data_path"] == "/data"
    assert "awesome-selfhosted-data" in x["source"]
    hub.search.append({"repo_name": "vaultwarden/vaultwarden", "pull_count": 200_000})
    assert bsc._resolve(hub, APP)["image"] == "vaultwarden/vaultwarden"   # 名稱完全一樣的優先，就算下載次數比較少


def test_resolve_rejects_unrelated_image_of_same_account():
    hub = _FakeHub({}, [{"repo_name": "vaultwarden/watchdog", "pull_count": 9_000_000}], [])
    assert bsc._resolve(hub, APP) is None           # 帳號對得上，但名稱不是 server／core 這種通用名稱


def test_udp_ports_ignored():
    hub = _FakeHub({"vaultwarden/vaultwarden": 500_000}, [], ["EXPOSE map[21027/udp:{} 22000/tcp:{} 8384/tcp:{}]"])
    assert bsc._resolve(hub, APP)["port"] == 22000  # UDP 不算；沒有常見網頁 port 就取第一個 TCP


def test_resolve_allows_trusted_repacker():
    hub = _FakeHub({}, [{"repo_name": "bitnami/vaultwarden", "pull_count": 300_000}], [])
    assert bsc._resolve(hub, APP)["image"] == "bitnami/vaultwarden"


def test_resolve_skips_rare_and_official():
    hub = _FakeHub({"dani-garcia/vaultwarden": 50_000}, [{"repo_name": "vaultwarden", "pull_count": 10**9}], [])
    assert bsc._resolve(hub, APP) is None          # 太冷門（< 10 萬）、官方映像檔（沒有 /）都不收


def test_hub_get_survives_timeouts(monkeypatch):
    import requests
    h = bsc._Hub()
    monkeypatch.setattr("time.sleep", lambda s: None)
    calls = {"n": 0}

    def boom(url, timeout):
        calls["n"] += 1
        raise requests.ReadTimeout("slow")
    monkeypatch.setattr(h.s, "get", boom)
    assert h.get("https://hub.docker.com/x") is None and calls["n"] == 6   # 重試完就跳過，不讓整批中斷


def test_field_catalog_entries_are_not_overridden(tmp_path, monkeypatch):
    """柏碩的 rag/knowledge/catalog.json 已經有的軟體不重複加（syncthing 的 port 曾被蓋掉）。"""
    import json
    p = tmp_path / "catalog.json"
    p.write_text(json.dumps({"entries": [{"image": "syncthing/syncthing:1.27"}, {"image": "nginx:1.27", "repo": "library/nginx"}]}),
                 encoding="utf-8")
    monkeypatch.setattr(bsc, "FIELD_CATALOG", str(p))
    assert bsc._field_catalog_repos() == {"syncthing/syncthing", "nginx"}
