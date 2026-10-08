"""
tests/test_rollback.py
測 gitops/rollback.py 的 auto 策略選擇邏輯——2026-10-03 發現的真實 bug：
`rollback(strategy="auto")` 原本寫成 `if result["ok"] is not False:` 就提前回傳，
把 ok=None（策略不可用，例如 Argo CD 沒部署）跟 ok=True（真的成功）當同一件事。
這個專案從沒真的部署過 Argo CD（`gitops/argocd_sync.py` 預設打
`https://localhost:8080`，這台機器上這個位址連不上），所以 `_rollback_argocd`
一定回傳 ok=None，導致 auto 迴圈每次都在第一輪就停下、回傳一個「不可用」的結果，
後面真的會成功的 git／kubectl 策略永遠輪不到——Web UI 上的 Rollback 按鈕因此
實質上從沒真的用 auto 策略回滾成功過。修法：只有 ok is True 才算「這個策略真的
成功了」提前返回，ok 是 None 或 False 都要繼續試下一個。
"""
import importlib

import pytest

# `gitops/__init__.py` 做了 `from gitops.rollback import rollback`，把套件
# 命名空間裡的 `gitops.rollback` 屬性換成「那個函式」，蓋掉了子模組本身——
# 一般的 `import gitops.rollback as x` 或 `from gitops import rollback as x`
# 都會因為屬性查找被蓋掉而意外抓到函式，不是模組，要用 importlib 繞過屬性查找。
rollback_mod = importlib.import_module("gitops.rollback")


def test_auto_skips_unavailable_strategy_and_tries_next(monkeypatch):
    """argocd 回傳 ok=None（不可用，不是失敗）時，auto 不能把它當成功提早停下，
    要繼續試 git，git 真的成功就用 git 的結果。"""
    calls = []

    def fake_run_strategy(strategy, app_name, namespace, repo_path, dry_run):
        calls.append(strategy)
        if strategy == "argocd":
            return {"ok": None, "message": "Argo CD 不可用", "details": {}}
        if strategy == "git":
            return {"ok": True, "message": "git 回滾成功", "details": {}}
        return {"ok": False, "message": "不該走到這裡", "details": {}}

    monkeypatch.setattr(rollback_mod, "_run_strategy", fake_run_strategy)
    monkeypatch.setattr(rollback_mod, "_print_result", lambda result: None)

    result = rollback_mod.rollback("demo-app", namespace="default", strategy="auto")

    assert calls == ["argocd", "git"]
    assert result["ok"] is True
    assert result["strategy"] == "git"


def test_auto_skips_failed_strategy_and_tries_next(monkeypatch):
    """git 回傳 ok=False（真的執行過但失敗，例如沒有足夠 commit 歷史）時，
    也要繼續試 kubectl，不能跟 ok=True 混為一談提早停下。"""
    calls = []

    def fake_run_strategy(strategy, app_name, namespace, repo_path, dry_run):
        calls.append(strategy)
        if strategy == "argocd":
            return {"ok": None, "message": "Argo CD 不可用", "details": {}}
        if strategy == "git":
            return {"ok": False, "message": "沒有足夠的 commit 歷史可以回滾", "details": {}}
        return {"ok": True, "message": "kubectl rollout undo 成功", "details": {}}

    monkeypatch.setattr(rollback_mod, "_run_strategy", fake_run_strategy)
    monkeypatch.setattr(rollback_mod, "_print_result", lambda result: None)

    result = rollback_mod.rollback("demo-app", namespace="default", strategy="auto")

    assert calls == ["argocd", "git", "kubectl"]
    assert result["ok"] is True
    assert result["strategy"] == "kubectl"


def test_auto_all_strategies_fail_reports_failure(monkeypatch):
    """三個策略全部失敗/不可用時，要老實回報失敗，不能假裝成功。"""
    def fake_run_strategy(strategy, app_name, namespace, repo_path, dry_run):
        return {"ok": False, "message": f"{strategy} 失敗", "details": {}}

    monkeypatch.setattr(rollback_mod, "_run_strategy", fake_run_strategy)

    result = rollback_mod.rollback("demo-app", namespace="default", strategy="auto")

    assert result["ok"] is False
    assert result["strategy"] == "auto"


def test_print_result_uses_ascii_safe_icons_not_emoji(capsys):
    """2026-10-03 發現的另一個真實 bug：icon 原本是 emoji（✅/⚠️/❌），這台
    Windows 機器的主控台預設 cp950 編碼印不出 emoji，print() 會直接丟
    UnicodeEncodeError，讓整個 rollback() 在送出任何結果前就當掉——這正是
    Web UI 上 Rollback 按鈕回報「no previous image annotation」這種誤導訊息的
    根本原因（真正的例外被 web_demo.py 的 try/except 吃掉，才掉進那個備援分支）。
    呼叫真正的 `_print_result()`（不是重複一份邏輯），鎖死印出來的 icon 不可以
    再含 emoji，防止之後改回去。
    """
    import re

    for ok_value in (True, None, False):
        rollback_mod._print_result({"ok": ok_value, "strategy": "git", "message": "測試"})
        out = capsys.readouterr().out
        assert re.match(r"^\s*\[[A-Z]+\]", out), f"輸出 {out!r} 的 icon 不是 ASCII 安全格式"


def _init_repo_with_two_commits(tmp_path, namespace, app_name, image_v1, image_v2):
    """建一個真的 git repo，對同一個 manifest 檔案 commit 兩次（不同 image），
    回傳 (repo, manifest_dir)。用真的 GitPython repo 而不是 mock，因為下面兩個
    迴歸測試要驗證的正是 GitPython tree 物件的路徑格式／型別這種只有真的 repo
    才測得出來的細節。"""
    git = pytest.importorskip("git")
    repo = git.Repo.init(tmp_path)
    with repo.config_writer() as cw:
        cw.set_value("user", "name", "test").set_value("user", "email", "t@test.com")

    manifest_dir = tmp_path / "manifests" / namespace / app_name
    manifest_dir.mkdir(parents=True)
    deployment_yaml = manifest_dir / "deployment.yaml"

    deployment_yaml.write_text(
        f"apiVersion: apps/v1\nkind: Deployment\nspec:\n  template:\n    spec:\n"
        f"      containers:\n        - image: {image_v1}\n",
        encoding="utf-8",
    )
    rel = str(manifest_dir.relative_to(tmp_path))
    repo.index.add([rel])
    repo.index.commit("deploy v1")

    deployment_yaml.write_text(
        f"apiVersion: apps/v1\nkind: Deployment\nspec:\n  template:\n    spec:\n"
        f"      containers:\n        - image: {image_v2}\n",
        encoding="utf-8",
    )
    repo.index.add([rel])
    repo.index.commit("deploy v2")

    return repo, manifest_dir


def test_rollback_git_actually_restores_file_content_on_windows_style_path(monkeypatch, tmp_path):
    """2026-10-03 發現的真實 bug：`rel = str(manifest_dir.relative_to(...))` 在
    Windows 上會是反斜線路徑（`manifests\\ns\\app`），但 GitPython tree 物件內部的
    `item.path` 永遠是用 `/` 分隔——`item.path.startswith(rel)` 在 Windows 上永遠
    對不上，於是「從上一個 commit 取出檔案」這個迴圈實測一次都不會進去，
    `_rollback_git()` 回報「已回滾」，但 manifest 檔案內容其實完全沒被改過。
    這個專案只在 Windows 上開發跑測試（見 CLAUDE.md），`Path.relative_to()` 在
    這台機器上本來就會自然產生反斜線路徑，不用額外模擬就能重現／驗證這個 bug；
    在非 Windows 環境執行這個測試不會重現原始 bug（該環境的路徑分隔符本來就是
    `/`），但修好後的程式碼在兩種平台都正確，測試一樣會過。
    """
    repo, manifest_dir = _init_repo_with_two_commits(
        tmp_path, "ns", "app", "nginx:1.25", "nginx:1.27"
    )

    captured_apply_paths = []

    class _FakeCompleted:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, capture_output, text, timeout):
        captured_apply_paths.append(cmd)
        return _FakeCompleted()

    monkeypatch.setattr(rollback_mod.subprocess, "run", fake_run)

    result = rollback_mod._rollback_git("app", "ns", str(tmp_path), dry_run=False)

    deployment_yaml = manifest_dir / "deployment.yaml"
    content = deployment_yaml.read_text(encoding="utf-8")

    assert result["ok"] is True, result["message"]
    assert "nginx:1.25" in content, (
        "回滾後檔案內容應該換回上一版 nginx:1.25，但實際內容是：\n" + content
    )
    assert "nginx:1.27" not in content
    assert captured_apply_paths, "應該呼叫過 kubectl apply 把還原後的檔案套到叢集"


def test_rollback_git_skips_directory_entry_not_just_files(tmp_path):
    """2026-10-03 發現的另一個真實 bug：修好上面那個路徑分隔符問題之後，
    `.traverse()` 連目錄本身（Tree 物件）也會被列出，`item.path` 剛好等於
    `rel`（目錄路徑本身）也會通過 `startswith()`，把目錄當成檔案寫入會撞到
    「路徑其實是目錄」的 Permission denied。`_rollback_git()` 必須只處理真正
    的檔案（blob），跳過目錄本身。"""
    repo, manifest_dir = _init_repo_with_two_commits(
        tmp_path, "ns2", "app2", "nginx:1.25", "nginx:1.27"
    )

    # 直接驗證 tree.traverse() 的過濾條件本身不會把目錄節點誤判成檔案
    # （上面那個端到端測試已經證實完整的 `_rollback_git()` 行為正確；這裡
    # 單獨鎖住「為什麼需要 `item.type == "blob"` 這個條件」的具體原因）。
    commits = list(repo.iter_commits(paths=str(manifest_dir.relative_to(tmp_path)), max_count=2))
    previous_commit = commits[1]
    rel_posix = str(manifest_dir.relative_to(tmp_path)).replace("\\", "/")

    matched_dirs = [
        item for item in previous_commit.tree.traverse()
        if hasattr(item, "path") and item.path.startswith(rel_posix) and item.type != "blob"
    ]
    matched_files = [
        item for item in previous_commit.tree.traverse()
        if hasattr(item, "path") and item.path.startswith(rel_posix) and item.type == "blob"
    ]

    assert matched_dirs, "這個測試的前提是目錄節點真的會被 traverse() 列出來"
    assert matched_files, "也應該要能列出真正的檔案"
    # _rollback_git() 現在的篩選條件 `item.type == "blob"` 必須把 matched_dirs
    # 排除在外，只處理 matched_files——這正是上面那個測試能通過的原因。


# ══════════════════════════════════════════════════════════════════
# 2026-10-08 安全審查：路徑穿越 / 跨帳號回滾（docs/security_review.md 30 節）
# ══════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("bad_name", [
    "../user-victim/their-app", "..", ".", "a/b", r"a\b", "App", "-app", "app-", "", "a" * 64,
])
def test_rollback_rejects_invalid_app_name_before_any_strategy(monkeypatch, bad_name):
    """不合法的名稱（含路徑穿越寫法）要在任何策略執行前就被擋下。"""
    calls = []
    monkeypatch.setattr(rollback_mod, "_run_strategy",
                        lambda *a, **k: calls.append(a) or {"ok": True, "message": "", "details": {}})
    result = rollback_mod.rollback(bad_name, namespace="user-attacker", strategy="auto")
    assert result["ok"] is False
    assert calls == []


def test_rollback_rejects_invalid_namespace(monkeypatch):
    calls = []
    monkeypatch.setattr(rollback_mod, "_run_strategy",
                        lambda *a, **k: calls.append(a) or {"ok": True, "message": "", "details": {}})
    result = rollback_mod.rollback("my-app", namespace="../user-victim", strategy="git")
    assert result["ok"] is False
    assert calls == []


def test_get_rollback_history_rejects_traversal():
    assert rollback_mod.get_rollback_history("../user-victim/app", namespace="user-a") == []


def test_manifest_dir_within_blocks_escape(tmp_path):
    assert rollback_mod._manifest_dir_within(tmp_path, "user-a", "my-app") == \
        (tmp_path / "manifests" / "user-a" / "my-app").resolve()
    assert rollback_mod._manifest_dir_within(tmp_path, "user-a", "../user-b/app") is None
    assert rollback_mod._manifest_dir_within(tmp_path, "user-a", "..") is None


def test_argocd_refuses_app_deployed_to_other_namespace(monkeypatch):
    """Argo CD Application 名稱不分帳號：目的地 namespace 不是呼叫者自己的，就拒絕回滾，
    而且絕對不能呼叫 rollback API。"""
    import gitops.argocd_sync as argocd_sync
    requests = pytest.importorskip("requests")

    class FakeClient:
        def get_status(self, app_name):
            return {"ok": True, "message": ""}

    class FakeResp:
        status_code = 200
        def json(self):
            return {"spec": {"destination": {"namespace": "user-victim"}}}

    posted = []
    monkeypatch.setattr(argocd_sync, "ArgocdClient", FakeClient)
    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResp())
    monkeypatch.setattr(requests, "post", lambda *a, **k: posted.append(a))

    result = rollback_mod._rollback_argocd("their-app", "user-attacker", ".", dry_run=False)
    assert result["ok"] is False
    assert posted == []


def test_git_rollback_does_not_touch_sibling_app_with_same_prefix(tmp_path, monkeypatch):
    """回滾 `app` 不能連帶覆寫 `app123/` 的檔案（原本用 startswith 比對前綴的 bug）。"""
    git = pytest.importorskip("git")
    repo = git.Repo.init(tmp_path)
    with repo.config_writer() as cw:
        cw.set_value("user", "name", "t")
        cw.set_value("user", "email", "t@t")
    app_dir = tmp_path / "manifests" / "user-a" / "app"
    sib_dir = tmp_path / "manifests" / "user-a" / "app123"
    for d in (app_dir, sib_dir):
        d.mkdir(parents=True)
    for ver in ("v1", "v2"):
        (app_dir / "deployment.yaml").write_text(f"app {ver}\n")
        (sib_dir / "deployment.yaml").write_text(f"sibling {ver}\n")
        repo.index.add(["manifests/user-a/app/deployment.yaml",
                        "manifests/user-a/app123/deployment.yaml"])
        repo.index.commit(ver)

    class Done:
        returncode = 0
        stdout = stderr = ""
    monkeypatch.setattr(rollback_mod.subprocess, "run", lambda *a, **k: Done())

    result = rollback_mod._rollback_git("app", "user-a", str(tmp_path), dry_run=False)
    assert result["ok"] is True, result
    assert (app_dir / "deployment.yaml").read_text() == "app v1\n"
    assert (sib_dir / "deployment.yaml").read_text() == "sibling v2\n"
