"""
gitops/tenant_repo.py
客戶自帶叢集（GitOps 模式 C）：平台替每位客戶在 GitHub 開一個私有 repo，所有部署操作都變成
這個 repo 的 commit，客戶叢集裡的 Flux 用唯讀 deploy key 每 30 秒拉一次套用。

2026-10-09 新增（計畫：客戶自帶叢集第 2 階段）。平台永遠不連進客戶叢集——這裡就是平台
「改變客戶叢集」的唯一管道，每個動作都留下 commit，天然有稽核軌跡與回滾能力。

repo 結構：
    README.md
    apps/_platform/zerotouch-info.yaml   ← 平台資訊 ConfigMap；Flux 套用成功即代表同步管道通了
    apps/<app>/deployment.yaml
    apps/<app>/service.yaml              ← 有 port 才有
（不放 kustomization.yaml：Flux kustomize-controller 會自動把 ./apps 底下所有 YAML 納入。）

兩個類別：
    GitHubTenantHost：用 GitHub API 建 repo、輪換唯讀 deploy key（需要 TENANT_GITHUB_TOKEN）
    TenantRepo      ：本機工作副本上的各種操作（commit + push）；測試時 remote 可以是本機 bare repo
"""
import base64
import hashlib
import os
import re
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml

from gitops.manifest_writer import _build_deployment_yaml, _build_service_yaml, _sanitize_name
from gitops.rollback import is_valid_k8s_name

APPS_DIR = "apps"
PLATFORM_DIR = "_platform"
COMMIT_AUTHOR = "ZeroTouch Platform <platform@zerotouch.invalid>"
PREV_IMAGE_KEY = "zerotouch.k8s/previous-image"


class TenantRepoError(Exception):
    """給使用者看的錯誤（訊息已經是中英雙語、可以直接顯示）。"""


def tenant_slug(username: str) -> str:
    """帳號 → 客戶端 repo 名稱與 namespace（同一個字串），例如 zt-alice-test-3f2a1b9c。

    2026-10-09 背景安全審查抓到：只做「轉小寫、非法字元換 -、截斷」的話，`Alice_Test` 和
    `alice-test` 會對到同一個 repo/namespace，兩個帳號共用部署設定，跨帳號隔離失效。
    固定加上帳號原文的雜湊後綴，保證一個帳號一個名稱、而且同一帳號永遠得到同一個名稱。
    總長 <= 60（DNS-1123 label 上限 63）。"""
    raw = username or ""
    safe = re.sub(r"[^a-z0-9-]", "-", raw.lower())
    safe = re.sub(r"-+", "-", safe).strip("-")[:40].strip("-") or "user"
    # 16 個十六進位字元（64 位元）：8 個字元只有 32 位元，可以暴力找出另一個帳號名稱
    # 撞到同一個 slug（2026-10-09 背景安全審查第二輪）。註冊時另有確定性的撞名檢查。
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"zt-{safe}-{digest}"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ══════════════════════════════════════════════════════════════════
# GitHub：建 repo、deploy key
# ══════════════════════════════════════════════════════════════════

class GitHubTenantHost:
    def __init__(self, token: str, org: str, api: str = "https://api.github.com", session=None):
        if not token or not org:
            raise TenantRepoError(
                "平台尚未設定 GitHub（.env 缺 TENANT_GITHUB_TOKEN 或 TENANT_GITHUB_ORG），請聯絡管理員。 / "
                "Platform GitHub is not configured (TENANT_GITHUB_TOKEN / TENANT_GITHUB_ORG missing).")
        import requests
        self.org, self.api = org, api.rstrip("/")
        self.s = session or requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {token}",
                               "Accept": "application/vnd.github+json",
                               "X-GitHub-Api-Version": "2022-11-28"})
        self._token = token

    def _call(self, method: str, path: str, **kw):
        r = self.s.request(method, self.api + path, timeout=20, **kw)
        return r

    def ensure_repo(self, name: str) -> Dict[str, str]:
        r = self._call("GET", f"/repos/{self.org}/{name}")
        if r.status_code == 404:
            r = self._call("POST", f"/orgs/{self.org}/repos", json={
                "name": name, "private": True, "auto_init": True,
                "description": "ZeroTouch 平台管理的部署設定（Flux 從這裡拉取）",
            })
            if r.status_code not in (201, 422):
                raise TenantRepoError(f"建立 GitHub repo 失敗（HTTP {r.status_code}）。 / Failed to create repo: {r.text[:200]}")
            if r.status_code == 422:  # 剛好被同時建立
                r = self._call("GET", f"/repos/{self.org}/{name}")
        if r.status_code not in (200, 201):  # 201 = 剛建立，回應本身就帶完整 repo 資料
            raise TenantRepoError(f"讀取 GitHub repo 失敗（HTTP {r.status_code}）。 / Failed to read repo.")
        d = r.json()
        return {"full_name": d["full_name"], "https_url": d["clone_url"], "ssh_url": d["ssh_url"],
                "default_branch": d.get("default_branch") or "main"}

    def rotate_deploy_key(self, name: str, title: str = "zerotouch-flux") -> Dict[str, str]:
        """刪掉舊的同名 key，產生新的 ed25519 金鑰對，公鑰設成**唯讀** deploy key。
        私鑰只回傳給呼叫端放進安裝包，平台不保存。"""
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        r = self._call("GET", f"/repos/{self.org}/{name}/keys")
        if r.status_code == 200:
            for k in r.json():
                if k.get("title") == title:
                    self._call("DELETE", f"/repos/{self.org}/{name}/keys/{k['id']}")
        key = Ed25519PrivateKey.generate()
        private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH,
                                    serialization.NoEncryption()).decode()
        public = key.public_key().public_bytes(serialization.Encoding.OpenSSH,
                                               serialization.PublicFormat.OpenSSH).decode()
        r = self._call("POST", f"/repos/{self.org}/{name}/keys",
                       json={"title": title, "key": public, "read_only": True})
        if r.status_code != 201:
            raise TenantRepoError(f"設定 deploy key 失敗（HTTP {r.status_code}）。 / Failed to add deploy key.")
        return {"private_key": private, "public_key": public, "known_hosts": self.known_hosts()}

    def known_hosts(self) -> str:
        """GitHub 官方公布的 SSH 主機金鑰（/meta），讓 Flux 驗證連到的真的是 GitHub。"""
        r = self._call("GET", "/meta")
        keys = r.json().get("ssh_keys", []) if r.status_code == 200 else []
        if not keys:
            raise TenantRepoError("無法取得 GitHub 主機金鑰。 / Could not fetch GitHub host keys.")
        return "\n".join(f"github.com {k}" for k in keys) + "\n"

    def auth_header(self) -> str:
        raw = base64.b64encode(f"x-access-token:{self._token}".encode()).decode()
        return f"Authorization: Basic {raw}"


# ══════════════════════════════════════════════════════════════════
# 本機工作副本上的操作
# ══════════════════════════════════════════════════════════════════

_locks: Dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(key: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


class TenantRepo:
    """一位客戶的 repo。所有寫入操作：先同步遠端 → 改檔 → commit → push，整段持有該客戶的鎖，
    兩個請求同時進來（例如兩個分頁）也不會互相蓋掉。"""

    def __init__(self, username: str, remote_url: str, workdir: str,
                 auth_header: Optional[str] = None, branch: str = "main"):
        self.username = username
        self.slug = tenant_slug(username)
        self.namespace = self.slug
        self.remote_url = remote_url
        self.workdir = Path(workdir)
        self.branch = branch
        self._auth = auth_header
        self._lock = _lock_for(self.slug)

    # ── git 底層 ──
    def _git(self, *args, cwd: Optional[Path] = None, check: bool = True) -> str:
        cmd = ["git", "-c", "core.autocrlf=false", "-c", "core.safecrlf=false", *args]
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
        if self._auth:
            # 認證標頭走 GIT_CONFIG_* 環境變數（git >= 2.31），不放在指令列參數——
            # 指令列在本機行程清單裡任何程式都看得到（2026-10-09 背景安全審查）。
            # 也不寫進 .git/config。
            env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http.extraHeader",
                       GIT_CONFIG_VALUE_0=self._auth)
        p = subprocess.run(cmd, cwd=str(cwd or self.workdir), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60, env=env)
        if check and p.returncode != 0:
            raise TenantRepoError(
                "同步部署設定到 Git 失敗，請稍後再試。 / Failed to sync deployment config to Git. "
                f"(git {args[0]}: {self._redact(p.stderr.strip())[:200]})")
        return p.stdout

    def _redact(self, text: str) -> str:
        """錯誤訊息會顯示給使用者，萬一 git 把認證資訊印出來要遮掉。"""
        if self._auth:
            secret = self._auth.split(" ")[-1]
            text = text.replace(self._auth, "***").replace(secret, "***")
        return re.sub(r"(x-access-token:)[^@\s]+", r"\1***", text)

    def _sync(self):
        """確保本機副本存在且等於遠端最新狀態（平台是唯一寫入者，直接 reset 到遠端）。"""
        if not (self.workdir / ".git").exists():
            self.workdir.parent.mkdir(parents=True, exist_ok=True)
            self._git("clone", self.remote_url, str(self.workdir), cwd=self.workdir.parent)
        self._git("fetch", "origin")
        has_remote_branch = self._git("ls-remote", "--heads", "origin", self.branch).strip() != ""
        if has_remote_branch:
            self._git("checkout", "-B", self.branch, f"origin/{self.branch}")
            self._git("reset", "--hard", f"origin/{self.branch}")
            self._git("clean", "-fd")
        else:
            self._git("checkout", "-B", self.branch)

    def _commit_push(self, message: str) -> Optional[str]:
        self._git("add", "-A")
        if not self._git("status", "--porcelain").strip():
            return None  # 沒有變更
        self._git("-c", f"user.name={COMMIT_AUTHOR.split(' <')[0]}",
                  "-c", f"user.email={COMMIT_AUTHOR.split('<')[1].rstrip('>')}",
                  "commit", "-m", message)
        self._git("push", "origin", f"HEAD:{self.branch}")
        return self._git("rev-parse", "HEAD").strip()

    def _app_dir(self, app: str) -> Path:
        app = _sanitize_name(app)
        if not is_valid_k8s_name(app) or app == PLATFORM_DIR:
            raise TenantRepoError("應用程式名稱不合法。 / Invalid app name.")
        d = (self.workdir / APPS_DIR / app).resolve()
        if (self.workdir / APPS_DIR).resolve() not in d.parents:
            raise TenantRepoError("應用程式名稱不合法。 / Invalid app name.")
        return d

    def _load(self, app: str) -> dict:
        p = self._app_dir(app) / "deployment.yaml"
        if not p.exists():
            raise TenantRepoError(f"找不到名為「{app}」的部署。 / No deployment named '{app}'.")
        return yaml.safe_load(p.read_text(encoding="utf-8"))

    def _save(self, app: str, doc: dict):
        p = self._app_dir(app) / "deployment.yaml"
        p.write_text("---\n" + yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")

    def _msg(self, action: str) -> str:
        return f"{action} (by {self.username} via ZeroTouch, {_now()})"

    # ── 初始化 ──
    def ensure_initialized(self) -> Optional[str]:
        """放進 README 與平台資訊 ConfigMap。重複呼叫安全。"""
        with self._lock:
            self._sync()
            readme = self.workdir / "README.md"
            info = self.workdir / APPS_DIR / PLATFORM_DIR / "zerotouch-info.yaml"
            info.parent.mkdir(parents=True, exist_ok=True)
            readme.write_text(
                f"# {self.slug}\n\n這個 repo 由 ZeroTouch 平台管理，存放 `{self.username}` 的部署設定。\n"
                f"客戶叢集裡的 Flux 會用唯讀金鑰拉取 `apps/` 並套用到 namespace `{self.namespace}`。\n"
                "請不要手動修改，平台的下一次操作會覆蓋手動變更。\n", encoding="utf-8")
            info.write_text("---\n" + yaml.safe_dump({
                "apiVersion": "v1", "kind": "ConfigMap",
                "metadata": {"name": "zerotouch-info", "namespace": self.namespace,
                             "labels": {"app.kubernetes.io/managed-by": "zerotouch"}},
                "data": {"owner": self.username, "managed-by": "ZeroTouch platform via Flux"},
            }, allow_unicode=True, sort_keys=False), encoding="utf-8")
            return self._commit_push(self._msg("init: ZeroTouch tenant repo"))

    # ── 查詢 ──
    def list_apps(self) -> List[str]:
        with self._lock:
            self._sync()
            base = self.workdir / APPS_DIR
            if not base.exists():
                return []
            return sorted(p.name for p in base.iterdir() if p.is_dir() and p.name != PLATFORM_DIR)

    def get_app(self, app: str) -> dict:
        with self._lock:
            self._sync()
            return self._load(app)

    # ── 寫入操作 ──
    def write_app(self, parsed: dict) -> dict:
        app = _sanitize_name(parsed.get("app_name", "app"))
        with self._lock:
            self._sync()
            d = self._app_dir(app)
            d.mkdir(parents=True, exist_ok=True)
            doc = yaml.safe_load(_build_deployment_yaml(parsed, self.namespace))
            # 跟 web_demo.k8s_deploy() 一致的安全硬化（2026-09-14 那次加的）
            for c in doc["spec"]["template"]["spec"]["containers"]:
                c.setdefault("securityContext", {})["allowPrivilegeEscalation"] = False
            self._save(app, doc)
            svc = _build_service_yaml(parsed, self.namespace)
            svc_path = d / "service.yaml"
            if svc:
                svc_path.write_text(svc, encoding="utf-8")
            elif svc_path.exists():
                svc_path.unlink()
            sha = self._commit_push(self._msg(f"deploy {app} ({doc['spec']['replicas']}x {parsed.get('image')})"))
        return {"app": app, "commit": sha, "namespace": self.namespace}

    def delete_app(self, app: str) -> dict:
        with self._lock:
            self._sync()
            d = self._app_dir(app)
            if not d.exists():
                raise TenantRepoError(f"找不到名為「{app}」的部署。 / No deployment named '{app}'.")
            for f in d.iterdir():
                f.unlink()
            d.rmdir()
            sha = self._commit_push(self._msg(f"delete {app}"))
        return {"app": app, "commit": sha}

    def _modify(self, app: str, action: str, fn) -> dict:
        with self._lock:
            self._sync()
            doc = self._load(app)
            extra = fn(doc) or {}
            self._save(app, doc)
            sha = self._commit_push(self._msg(f"{action} {app}"))
        return {"app": app, "commit": sha, **extra}

    def set_replicas(self, app: str, replicas: int) -> dict:
        def fn(doc):
            old = doc["spec"].get("replicas")
            doc["spec"]["replicas"] = int(replicas)
            return {"previous_replicas": old}
        return self._modify(app, f"scale to {int(replicas)}:", fn)

    def set_image(self, app: str, image: str) -> dict:
        def fn(doc):
            c = doc["spec"]["template"]["spec"]["containers"][0]
            old = c["image"]
            c["image"] = image
            ann = doc["spec"]["template"].setdefault("metadata", {}).setdefault("annotations", {})
            ann[PREV_IMAGE_KEY] = old
            return {"previous_image": old}
        return self._modify(app, f"update image -> {image}:", fn)

    def set_memory_limit(self, app: str, memory: str) -> dict:
        def fn(doc):
            res = doc["spec"]["template"]["spec"]["containers"][0].setdefault("resources", {})
            old = res.get("limits", {}).get("memory")
            res.setdefault("limits", {})["memory"] = memory
            res.setdefault("requests", {})["memory"] = memory
            return {"previous_memory": old}
        return self._modify(app, f"set memory {memory}:", fn)

    def restart_app(self, app: str) -> dict:
        def fn(doc):
            ann = doc["spec"]["template"].setdefault("metadata", {}).setdefault("annotations", {})
            ann["kubectl.kubernetes.io/restartedAt"] = _now()
        return self._modify(app, "restart", fn)

    def revert_app(self, app: str) -> dict:
        """把 apps/<app>/ 回到「最近一次變更之前」的狀態（新增一個 commit，不改寫歷史）。"""
        with self._lock:
            self._sync()
            d = self._app_dir(app)
            rel = d.relative_to(self.workdir.resolve()).as_posix()
            shas = self._git("log", "--format=%H", "--", rel).split()
            if not d.exists() or len(shas) < 2:
                raise TenantRepoError(
                    f"「{app}」沒有可以回滾的上一個版本。 / '{app}' has no previous version to roll back to.")
            target = shas[1]
            for f in list(d.iterdir()):
                f.unlink()
            self._git("checkout", target, "--", rel)
            sha = self._commit_push(self._msg(f"rollback {app} to {target[:7]}"))
        return {"app": app, "commit": sha, "rolled_back_to": target[:7]}

    def history(self, app: Optional[str] = None, limit: int = 20) -> List[dict]:
        with self._lock:
            self._sync()
            args = ["log", f"-{int(limit)}", "--format=%H%x1f%cI%x1f%s"]
            if app:
                args += ["--", self._app_dir(app).relative_to(self.workdir.resolve()).as_posix()]
            out = self._git(*args)
        rows = []
        for line in out.splitlines():
            sha, when, subject = line.split("\x1f", 2)
            rows.append({"commit": sha[:7], "time": when, "message": subject})
        return rows
