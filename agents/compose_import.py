"""
docker-compose 匯入（2026-10-10）：把 docker-compose.yml 轉成平台的部署規格清單。

只做轉換與檢查，不碰叢集。每個服務轉成一份跟一句話部署相同格式的規格，之後一個一個送進
既有的部署流程（三方審查、guardian、dry-run、GitOps），沒有任何服務能繞過把關。

轉換規則：
- ports "8080:80"：對外 8080、容器 80；沒寫 ports 的服務只給內部連（不對外開放）。
- 每個服務另外建一個跟 compose 服務同名的內部連線入口，後端照樣用「db:5432」連資料庫。
- environment 照搬；名稱像密碼、金鑰的另外存成 Kubernetes Secret，不寫進部署紀錄（見 manifest_writer.split_secret_env）。
- deploy.replicas、deploy.resources.limits、mem_limit、cpus 轉成副本數與資源。
- depends_on 決定部署順序（被依賴的先部署），有循環就擋下。

- command／entrypoint 轉成容器的啟動指令（compose 的 command 對應 K8s 的 args，entrypoint 對應 command）。
- 具名 volume 轉成 1Gi 的保存空間（PVC），容器重開資料還在；匿名 volume 與 tmpfs 用暫存空間。

不支援、會明確告知的：build（不從原始碼建置映像檔，安全取捨）、掛載本機資料夾（電腦上的檔案沒有上傳）、
privileged、host 網路、cap_add、env_file。
"""

import re
import shlex
from typing import Dict, List, Optional

import yaml

MAX_COMPOSE_BYTES = 100_000
MAX_SERVICES = 10
MAX_ENV = 50

_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,100}$")
_SECRET_KEY_RE = re.compile(r"PASS|SECRET|TOKEN|KEY|CREDENTIAL|PRIVATE", re.IGNORECASE)
# 2026-10-24：名稱看不出來、但值裡有帳號密碼的也算（DATABASE_URL=postgres://user:pass@db/app、私鑰本體）
_SECRET_VALUE_RE = re.compile(r"[a-z][a-z0-9+.-]{0,20}://[^/\s:@]+:[^/\s@]+@|-----BEGIN [A-Z ]*PRIVATE KEY-----", re.IGNORECASE)


def looks_like_secret(key, value=None) -> bool:
    """環境變數要不要當密碼存成 Kubernetes Secret：名稱像密碼，或值裡帶帳號密碼／私鑰。
    compose 匯入、K8s YAML 匯入、gitops/manifest_writer.split_secret_env 都用這一個判斷。"""
    return bool(_SECRET_KEY_RE.search(str(key)) or (value is not None and _SECRET_VALUE_RE.search(str(value))))


class ComposeError(ValueError):
    """整份檔案無法處理（格式錯、太大、沒有服務）。"""


MAX_EXPANDED_NODES = 20_000


def _expanded_size_ok(obj, budget: int = MAX_EXPANDED_NODES) -> bool:
    """YAML 的別名（&a / *a）讀進來是共用同一個物件，看起來很小，但後面任何走訪或轉成字串
    都會整個展開。2026-10-10 背景安全審查：100 KB 的上限擋不住「一層參照十個、疊九層」這種
    寫法，展開後是天文數字，一個請求就能拖垮伺服器。這裡照「展開後」的節點數算，超過就拒絕；
    compose 常見的 x-common: &common 共用設定遠低於這個上限。"""
    remaining = [budget]

    def walk(o, depth):
        remaining[0] -= 1
        if remaining[0] < 0 or depth > 40:
            return False
        if isinstance(o, dict):
            return all(walk(k, depth + 1) and walk(v, depth + 1) for k, v in o.items())
        if isinstance(o, (list, tuple)):
            return all(walk(v, depth + 1) for v in o)
        return True

    return walk(obj, 0)


def _k8s_name(name: str) -> str:
    """compose 服務名稱轉成 K8s 能用的名稱：小寫、底線換連字號、字母開頭，最長 40 字。"""
    n = re.sub(r"[^a-z0-9-]+", "-", str(name).lower()).strip("-")
    if not n or not n[0].isalpha():
        n = "svc-" + n
    return n[:40].rstrip("-") or "svc"


def _memory(v) -> Optional[str]:
    """「512M」「1g」「256mb」「268435456」→ 512Mi、1Gi、256Mi、256Mi。"""
    if v is None:
        return None
    s = str(v).strip().lower().replace(" ", "")
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([kmg]?)(?:i?b?)?", s)
    if not m:
        return None
    num, unit = float(m.group(1)), m.group(2)
    mi = {"": num / (1024 ** 2), "k": num / 1024, "m": num, "g": num * 1024}[unit]
    mi = max(1, int(round(mi)))
    return f"{mi // 1024}Gi" if mi % 1024 == 0 and mi >= 1024 else f"{mi}Mi"


def _cpu(v) -> Optional[str]:
    """「0.5」「1.5」「2」→ 500m、1500m、2000m。"""
    try:
        f = float(str(v).strip())
    except (TypeError, ValueError):
        return None
    if f <= 0:
        return None
    return f"{int(round(f * 1000))}m"


def _parse_port(p) -> Optional[Dict]:
    """支援 "80"、"8080:80"、"127.0.0.1:8080:80"、"8080:80/tcp"、長格式 {target, published}。"""
    if isinstance(p, dict):
        target = p.get("target")
        published = p.get("published") or target
        try:
            return {"external": int(str(published).split("-")[0]), "container": int(target)}
        except (TypeError, ValueError):
            return None
    s = str(p).split("/")[0].strip()
    parts = s.split(":")
    try:
        if len(parts) == 1:
            n = int(parts[0].split("-")[0])
            return {"external": n, "container": n}
        container = int(parts[-1].split("-")[0])
        external = int(parts[-2].split("-")[0]) if parts[-2] else container
        return {"external": external, "container": container}
    except ValueError:
        return None


def _env(raw) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if isinstance(raw, dict):
        items = raw.items()
    elif isinstance(raw, list):
        items = []
        for e in raw:
            k, _, v = str(e).partition("=")
            items.append((k, v if _ else None))
    else:
        return out
    for k, v in items:
        k = str(k).strip()
        if not _ENV_KEY_RE.match(k) or v is None:
            continue   # 沒有值的（「A」這種從主機帶入的寫法）平台拿不到主機的值，略過
        out[k] = str(v)[:4096]
    return out


DEFAULT_VOLUME_SIZE = "1Gi"
MAX_VOLUMES = 5
MAX_CMD_ITEMS = 50


def _cmd(raw) -> Optional[List[str]]:
    """「redis-server --appendonly yes」或 ["sh", "-c", "..."] → 字串清單。不經過 shell，跟 compose 一樣。"""
    if raw is None or raw == "" or raw == []:
        return None
    if isinstance(raw, str):
        try:
            parts = shlex.split(raw)
        except ValueError:
            parts = raw.split()
    elif isinstance(raw, list):
        parts = [str(x) for x in raw]
    else:
        return None
    return [p[:1000] for p in parts[:MAX_CMD_ITEMS]] or None


def _is_host_path(src: str) -> bool:
    return src.startswith((".", "/", "~")) or bool(re.match(r"^[A-Za-z]:[\\/]", src))


def _volumes(raw, probs: List[Dict]) -> List[Dict]:
    """compose volumes → [{"name", "mount", "kind": "pvc"|"empty", "read_only", "size"}]。本機資料夾不轉。"""
    out, binds, seen = [], [], set()
    for v in raw or []:
        if isinstance(v, dict):
            vtype = str(v.get("type") or "volume")
            src, target, ro = str(v.get("source") or ""), str(v.get("target") or ""), bool(v.get("read_only"))
            if vtype == "bind":
                binds.append(f"{src}:{target}")
                continue
            kind = "empty" if vtype == "tmpfs" or not src else "pvc"
        else:
            parts = str(v).split(":")
            # Windows 路徑「C:\data:/x」的磁碟機代號會被冒號切開，接回去
            if len(parts) >= 3 and re.fullmatch(r"[A-Za-z]", parts[0]):
                parts = [parts[0] + ":" + parts[1]] + parts[2:]
            if len(parts) == 1:
                src, target, ro, kind = "", parts[0], False, "empty"
            else:
                src, target = parts[0], parts[1]
                ro = len(parts) > 2 and "ro" in parts[2].split(",")
                if _is_host_path(src):
                    binds.append(f"{src}:{target}")
                    continue
                kind = "pvc"
        if not target.startswith("/") or target == "/" or ".." in target.split("/"):
            probs.append({"level": "warn", "text": f"資料空間的掛載位置「{target}」不合法，不會套用"})
            continue
        name = re.sub(r"[^a-z0-9-]+", "-", (src or f"tmp{len(out)}").lower()).strip("-")[:30] or f"v{len(out)}"
        if name in seen:
            name = f"{name}-{len(out)}"
        seen.add(name)
        out.append({"name": name, "mount": target[:200], "kind": kind, "read_only": ro,
                    "size": DEFAULT_VOLUME_SIZE if kind == "pvc" else None})
    if binds:
        probs.append({"level": "warn", "text": "掛載本機資料夾（" + "、".join(binds[:2]) + "）不會套用，"
                                               "你電腦上的檔案不會一起上傳"})
    if len(out) > MAX_VOLUMES:
        probs.append({"level": "warn", "text": f"資料空間太多，只保留前 {MAX_VOLUMES} 個"})
        out = out[:MAX_VOLUMES]
    pvcs = [v for v in out if v["kind"] == "pvc"]
    if pvcs:
        probs.append({"level": "info", "text": "資料會存在保存空間（每個 " + DEFAULT_VOLUME_SIZE + "）：" +
                      "、".join(v["mount"] for v in pvcs) + "。容器重新啟動資料還在；刪除這個服務時資料會一起刪除"})
    if any(v["kind"] == "empty" for v in out):
        probs.append({"level": "info", "text": "沒有名字的資料空間用暫存空間，容器重新啟動後內容會清空"})
    return out


def _depends(raw) -> List[str]:
    if isinstance(raw, dict):
        return [str(k) for k in raw]
    if isinstance(raw, list):
        return [str(x) for x in raw]
    return []


def _order(services: Dict[str, Dict]) -> List[str]:
    """依 depends_on 排出部署順序（被依賴的在前）。有循環時丟 ComposeError。"""
    order, state = [], {}

    def visit(n, path):
        if state.get(n) == "done":
            return
        if state.get(n) == "visiting":
            raise ComposeError("服務之間的依賴繞成一圈（" + " → ".join(path + [n]) + "），沒辦法決定誰先部署。")
        state[n] = "visiting"
        for d in services[n]["depends_on"]:
            if d in services:
                visit(d, path + [n])
        state[n] = "done"
        order.append(n)

    for n in services:
        visit(n, [])
    return order


def parse_compose(text: str, default_port_fn=None) -> Dict:
    """回傳 {"services": [...依部署順序...], "problems": [...], "deployable": bool}。

    每個服務：{"service", "app_name", "image", "pods", "port", "container_port", "expose",
             "dns_alias", "env", "memory", "cpu", "depends_on", "problems": [...]}。
    problems 每筆：{"level": "block"|"warn"|"info", "text": str}；有 block 的服務不能部署。
    """
    if not text or not str(text).strip():
        raise ComposeError("檔案是空的。")
    if len(text.encode("utf-8")) > MAX_COMPOSE_BYTES:
        raise ComposeError(f"檔案太大（上限 {MAX_COMPOSE_BYTES // 1000} KB）。")
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as e:
        line = getattr(getattr(e, "problem_mark", None), "line", None)
        raise ComposeError("看不懂這份 YAML" + (f"，第 {line + 1} 行附近格式有誤" if line is not None else "") + "。")
    except RecursionError:
        raise ComposeError("這份 YAML 的巢狀層數太深。")
    if not _expanded_size_ok(doc):
        raise ComposeError("這份 YAML 展開後內容過大（大量使用 & 與 * 別名互相參照），為了保護平台不予處理。")
    if not isinstance(doc, dict) or not isinstance(doc.get("services"), dict) or not doc["services"]:
        raise ComposeError("這不是 docker-compose 檔，找不到 services 區塊。")
    if len(doc["services"]) > MAX_SERVICES:
        raise ComposeError(f"服務太多（{len(doc['services'])} 個），一次最多 {MAX_SERVICES} 個。")

    top_problems: List[Dict] = []
    services: Dict[str, Dict] = {}
    used_names = set()
    for raw_name, svc in doc["services"].items():
        svc = svc if isinstance(svc, dict) else {}
        probs: List[Dict] = []
        name = _k8s_name(raw_name)
        if name in used_names:
            name = _k8s_name(f"{name}-{len(used_names)}")
        used_names.add(name)
        if name != str(raw_name):
            probs.append({"level": "info", "text": f"名稱改成 {name}（K8s 只能用小寫英文、數字和連字號）。"
                                                   f"其他服務用 {raw_name} 連它的設定要一起改成 {name}"})

        image = str(svc.get("image") or "").strip()
        if svc.get("build") is not None:
            if image:
                probs.append({"level": "info", "text": "有 build 設定，平台不會從原始碼建置，直接用 image 指定的映像檔"})
            else:
                probs.append({"level": "block", "text": "這個服務要從原始碼建置映像檔（build），平台不支援。"
                                                        "請先把映像檔推到 Docker Hub 等映像檔倉庫，再把 build 換成 image"})
        elif not image:
            probs.append({"level": "block", "text": "沒有寫映像檔（image）"})

        ports = [p for p in (_parse_port(x) for x in (svc.get("ports") or [])) if p]
        expose = bool(ports)
        if len(ports) > 1:
            probs.append({"level": "warn", "text": f"寫了 {len(ports)} 個 port，平台目前只開第一個（{ports[0]['external']}）"})
        if ports:
            port, cport = ports[0]["external"], ports[0]["container"]
        else:
            exposed = [_parse_port(x) for x in (svc.get("expose") or [])]
            exposed = [p for p in exposed if p]
            known = default_port_fn(image) if default_port_fn else None
            cport = exposed[0]["container"] if exposed else (known or 80)
            port = cport
            probs.append({"level": "info", "text": f"沒有對外 port，只給其他服務在內部連線（port {cport}）"})

        env = _env(svc.get("environment"))
        secret_keys = [k for k, v in env.items() if looks_like_secret(k, v)]
        if secret_keys:
            probs.append({"level": "warn", "text": "環境變數 " + "、".join(secret_keys[:5]) +
                          " 看起來是密碼或金鑰（名稱或內容），會另外存成 Kubernetes Secret，不會寫進部署紀錄。"
                          "使用客戶叢集模式時，會放在你自己的私有設定 repo 裡"})
        if len(env) > MAX_ENV:
            env = dict(list(env.items())[:MAX_ENV])
            probs.append({"level": "warn", "text": f"環境變數太多，只保留前 {MAX_ENV} 個"})
        if svc.get("env_file"):
            probs.append({"level": "warn", "text": "env_file 指到的檔案沒有一起上傳，裡面的設定不會套用，請直接寫在 environment"})

        deploy = svc.get("deploy") if isinstance(svc.get("deploy"), dict) else {}
        try:
            pods = int(deploy.get("replicas") or svc.get("scale") or 1)
        except (TypeError, ValueError):
            pods = 1
        limits = ((deploy.get("resources") or {}).get("limits") or {}) if isinstance(deploy.get("resources"), dict) else {}
        memory = _memory(limits.get("memory") or svc.get("mem_limit"))
        cpu = _cpu(limits.get("cpus") or svc.get("cpus"))

        volumes = _volumes(svc.get("volumes"), probs)
        if pods > 1 and any(v["kind"] == "pvc" for v in volumes):
            probs.append({"level": "warn", "text": f"{pods} 個副本會共用同一份保存空間，只有單一節點的叢集能這樣用；"
                                                   "資料庫這類服務請維持 1 個副本"})
        for key, label in (("privileged", "特權模式"), ("cap_add", "額外的系統權限（cap_add）"),
                           ("devices", "主機裝置（devices）")):
            if svc.get(key):
                probs.append({"level": "warn", "text": f"{label}在共用平台上不安全，不會套用；如果服務一定要它，部署後可能無法正常運作"})
        for key in ("network_mode", "pid", "ipc"):
            if str(svc.get(key) or "") == "host":
                probs.append({"level": "warn", "text": f"{key}: host 會直接使用主機的資源，在共用平台上不安全，不會套用"})

        services[str(raw_name)] = {
            "service": str(raw_name), "app_name": name, "image": image, "pods": max(1, pods),
            "port": int(port), "container_port": int(cport), "expose": expose, "dns_alias": True,
            "env": env, "memory": memory, "cpu": cpu, "depends_on": _depends(svc.get("depends_on")),
            "command": _cmd(svc.get("entrypoint")), "args": _cmd(svc.get("command")), "volumes": volumes,
            "problems": probs,
        }

    for s in services.values():
        missing = [d for d in s["depends_on"] if d not in services]
        if missing:
            s["problems"].append({"level": "warn", "text": "依賴的服務 " + "、".join(missing) + " 不在這份檔案裡"})
        # 依賴改用轉換後的名稱
        s["depends_on"] = [services[d]["app_name"] for d in s["depends_on"] if d in services]

    order = _order(services)
    for key in ("volumes", "networks", "secrets", "configs"):
        if doc.get(key):
            top_problems.append({"level": "info", "text": f"最外層的 {key} 設定不會套用"})

    listed = [services[n] for n in order]
    return {
        "services": listed,
        "problems": top_problems,
        "deployable": all(not any(p["level"] == "block" for p in s["problems"]) for s in listed),
    }
