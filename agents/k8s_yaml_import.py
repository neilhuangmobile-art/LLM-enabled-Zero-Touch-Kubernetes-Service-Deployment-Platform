"""
Kubernetes YAML 匯入（2026-10-21）：把使用者手上現成的 K8s 設定檔轉成平台的部署規格清單。

跟 docker-compose 匯入（agents/compose_import.py）輸出同一種格式，前端用同一張流程卡，
每個服務一樣送進 /api/deploy/parse、/api/deploy，三方審查與 GitOps 都不繞過。
平台**只取出認得的欄位**重新產生設定，不會把使用者的 YAML 原封不動套進叢集。

轉換規則：
- Deployment（StatefulSet 改用 Deployment 部署並說明差異）→ 一個服務；只取第一個容器。
- Service 用 selector 對到 Deployment：LoadBalancer／NodePort 對外開放，ClusterIP 只給內部；
  Service 的 port 是其他服務連過來的 port，targetPort 是容器的 port。其他服務用 Service 的名字連，
  所以名稱不同時部署名稱改用 Service 的名字。
- 同一份檔案裡的 ConfigMap、Secret 值直接帶入；從 Secret 來的值一律存成 Kubernetes Secret（secret_keys），
  不會因為名稱看起來不像密碼就寫進 Git。
- PVC 轉成保存空間（1～5Gi）；emptyDir 轉成暫存空間。
不轉、會告知的：DaemonSet／Job／CronJob／裸 Pod（擋下）、hostPath、privileged、hostNetwork、
權限設定（Role、ServiceAccount…）、Ingress、NetworkPolicy、namespace（一律部署到使用者自己的空間）。
"""

import base64
import re
from typing import Dict, List, Optional

import yaml

from agents.compose_import import (ComposeError, MAX_COMPOSE_BYTES, MAX_ENV, MAX_SERVICES, MAX_VOLUMES,
                                   _SECRET_KEY_RE, looks_like_secret, extra_ports, MAX_EXTRA_PORTS, _cpu, _expanded_size_ok, _k8s_name)

MAX_DOCS = 50
_MEM_RE = re.compile(r"^\d+(?:\.\d+)?(?:Ki|Mi|Gi|Ti|K|M|G|T)?$")
_IGNORED_INFO = {
    "Ingress": "對外網址（Ingress）由平台管理，不會套用",
    "NetworkPolicy": "網路規則（NetworkPolicy）不會套用",
    "Namespace": "namespace 設定不會套用，服務一律部署到你自己的空間",
    "ServiceAccount": "權限設定（ServiceAccount）不會套用",
    "Role": "權限設定（Role）不會套用", "RoleBinding": "權限設定（RoleBinding）不會套用",
    "ClusterRole": "叢集層級的權限設定（ClusterRole）不會套用",
    "ClusterRoleBinding": "叢集層級的權限設定（ClusterRoleBinding）不會套用",
    "HorizontalPodAutoscaler": "自動擴縮（HPA）不會套用，副本數用 Deployment 寫的數字",
    "PodDisruptionBudget": "PodDisruptionBudget 不會套用",
    "ResourceQuota": "資源配額由平台設定，不會套用", "LimitRange": "LimitRange 由平台設定，不會套用",
}
_BLOCKED_KINDS = {
    "DaemonSet": "DaemonSet 會在每一台機器上各跑一份，多人共用的平台不提供",
    "Job": "一次性工作（Job）目前不支援，平台只部署長時間執行的服務",
    "CronJob": "排程工作（CronJob）目前不支援，平台只部署長時間執行的服務",
    "Pod": "單獨的 Pod 沒有人負責重啟，請改寫成 Deployment",
}


def _is_k8s_doc(d) -> bool:
    return isinstance(d, dict) and isinstance(d.get("kind"), str) and isinstance(d.get("apiVersion"), str)


def looks_like_k8s(text: str) -> bool:
    return bool(re.search(r"(?m)^apiVersion\s*:", text or "")) and bool(re.search(r"(?m)^kind\s*:", text or ""))


def _name(d) -> str:
    return str(((d.get("metadata") or {}).get("name")) or "")


def _mem(v) -> Optional[str]:
    s = str(v or "").strip()
    return s if s and _MEM_RE.match(s) else None


def _k8s_cpu(v) -> Optional[str]:
    s = str(v or "").strip()
    if re.fullmatch(r"\d+m", s):
        return s
    return _cpu(s)


def _decode_secret(doc) -> Dict[str, str]:
    out = {}
    for k, v in (doc.get("stringData") or {}).items():
        out[str(k)] = str(v)
    for k, v in (doc.get("data") or {}).items():
        try:
            out.setdefault(str(k), base64.b64decode(str(v)).decode("utf-8"))
        except Exception:
            pass
    return out


def _raw_gi(v) -> float:
    """「10Gi」「500Mi」「2G」→ Gi（看不懂回 1）。"""
    m = re.fullmatch(r"(\d+(?:\.\d+)?)(Mi|Gi|Ti|M|G|T)?", str(v or "").strip())
    if not m:
        return 1.0
    n, u = float(m.group(1)), m.group(2) or "Gi"
    return {"Mi": n / 1024, "M": n / 1024, "Gi": n, "G": n, "Ti": n * 1024, "T": n * 1024}[u]


def _size_gi(v) -> int:
    """平台的保存空間大小：整數 Gi，限制在 1～5。"""
    return max(1, min(5, int(round(_raw_gi(v))) or 1))


def _env_from_container(c, configmaps, secrets, probs) -> tuple:
    env, secret_keys = {}, []
    for ef in c.get("envFrom") or []:
        if not isinstance(ef, dict):
            continue
        if ef.get("configMapRef"):
            ref = str(ef["configMapRef"].get("name") or "")
            if ref in configmaps:
                env.update(configmaps[ref])
            else:
                probs.append({"level": "warn", "text": f"引用的 ConfigMap「{ref}」不在這份檔案裡，裡面的設定不會套用"})
        if ef.get("secretRef"):
            ref = str(ef["secretRef"].get("name") or "")
            if ref in secrets:
                env.update(secrets[ref]); secret_keys += list(secrets[ref])
            else:
                probs.append({"level": "warn", "text": f"引用的 Secret「{ref}」不在這份檔案裡，裡面的設定不會套用"})
    for e in c.get("env") or []:
        if not isinstance(e, dict) or not e.get("name"):
            continue
        k = str(e["name"])
        if "value" in e and e["value"] is not None:
            env[k] = str(e["value"])
            continue
        vf = e.get("valueFrom") or {}
        if vf.get("secretKeyRef"):
            r = vf["secretKeyRef"]
            val = secrets.get(str(r.get("name") or ""), {}).get(str(r.get("key") or ""))
            if val is None:
                probs.append({"level": "warn", "text": f"環境變數 {k} 要從 Secret 讀，但那個 Secret 不在這份檔案裡，不會套用"})
            else:
                env[k] = val; secret_keys.append(k)
        elif vf.get("configMapKeyRef"):
            r = vf["configMapKeyRef"]
            val = configmaps.get(str(r.get("name") or ""), {}).get(str(r.get("key") or ""))
            if val is None:
                probs.append({"level": "warn", "text": f"環境變數 {k} 要從 ConfigMap 讀，但那個 ConfigMap 不在這份檔案裡，不會套用"})
            else:
                env[k] = val
        else:
            probs.append({"level": "info", "text": f"環境變數 {k} 讀的是執行時才知道的值（例如 Pod 名稱），不會套用"})
    env = {k: v for k, v in env.items() if re.match(r"^[A-Za-z_][A-Za-z0-9_.-]{0,100}$", k)}
    if len(env) > MAX_ENV:
        env = dict(list(env.items())[:MAX_ENV])
        probs.append({"level": "warn", "text": f"環境變數太多，只保留前 {MAX_ENV} 個"})
    secret_keys = sorted({k for k in secret_keys if k in env})
    return env, secret_keys


def _volumes(pod_spec, c, pvcs, probs) -> List[Dict]:
    mounts = {str(m.get("name")): m for m in (c.get("volumeMounts") or []) if isinstance(m, dict)}
    out = []
    for v in pod_spec.get("volumes") or []:
        if not isinstance(v, dict) or str(v.get("name")) not in mounts:
            continue
        m = mounts[str(v["name"])]
        target = str(m.get("mountPath") or "")
        if not target.startswith("/") or target == "/" or ".." in target.split("/"):
            probs.append({"level": "warn", "text": f"資料空間的掛載位置「{target}」不合法，不會套用"})
            continue
        name = re.sub(r"[^a-z0-9-]+", "-", str(v["name"]).lower()).strip("-")[:30] or f"v{len(out)}"
        if v.get("persistentVolumeClaim"):
            claim = str(v["persistentVolumeClaim"].get("claimName") or "")
            want = ((pvcs.get(claim) or {}).get("spec") or {}).get("resources", {}).get("requests", {}).get("storage")
            gi = _size_gi(want) if want else 1
            if want and _raw_gi(want) > 5:
                probs.append({"level": "warn", "text": f"保存空間「{claim}」要求 {want}，平台每個最多 5Gi，改成 {gi}Gi"})
            out.append({"name": name, "mount": target[:200], "kind": "pvc", "read_only": bool(m.get("readOnly")),
                        "size": f"{gi}Gi"})
        elif "emptyDir" in v:
            out.append({"name": name, "mount": target[:200], "kind": "empty", "read_only": False, "size": None})
        elif v.get("hostPath"):
            probs.append({"level": "warn", "text": f"掛載主機資料夾（hostPath {v['hostPath'].get('path')}）在共用平台上不安全，不會套用"})
        elif v.get("configMap") or v.get("secret"):
            probs.append({"level": "warn", "text": f"把 ConfigMap／Secret 掛成檔案（{target}）目前不支援，請改用環境變數"})
        else:
            probs.append({"level": "warn", "text": f"資料空間「{v['name']}」的類型不支援，不會套用"})
    if len(out) > MAX_VOLUMES:
        probs.append({"level": "warn", "text": f"資料空間太多，只保留前 {MAX_VOLUMES} 個"})
        out = out[:MAX_VOLUMES]
    pv = [v for v in out if v["kind"] == "pvc"]
    if pv:
        probs.append({"level": "info", "text": "資料會存在保存空間：" + "、".join(f"{v['mount']}（{v['size']}）" for v in pv) +
                      "。容器重新啟動資料還在；刪除這個服務時資料會一起刪除"})
    return out


def parse_k8s_yaml(text: str, default_port_fn=None) -> Dict:
    """回傳跟 parse_compose 相同格式：{"services": [...], "problems": [...], "deployable": bool, "format": "k8s"}。"""
    if not text or not str(text).strip():
        raise ComposeError("檔案是空的。")
    if len(text.encode("utf-8")) > MAX_COMPOSE_BYTES:
        raise ComposeError(f"檔案太大（上限 {MAX_COMPOSE_BYTES // 1000} KB）。")
    try:
        docs = [d for d in yaml.safe_load_all(text) if d is not None]
    except yaml.YAMLError as e:
        line = getattr(getattr(e, "problem_mark", None), "line", None)
        raise ComposeError("看不懂這份 YAML" + (f"，第 {line + 1} 行附近格式有誤" if line is not None else "") + "。")
    except RecursionError:
        raise ComposeError("這份 YAML 的巢狀層數太深。")
    if len(docs) > MAX_DOCS:
        raise ComposeError(f"這份檔案有 {len(docs)} 個設定，一次最多 {MAX_DOCS} 個。")
    if not _expanded_size_ok(docs):
        raise ComposeError("這份 YAML 展開後內容過大（大量使用 & 與 * 別名互相參照），為了保護平台不予處理。")
    # 「kind: List」把多個資源包在 items 裡
    flat = []
    for d in docs:
        if _is_k8s_doc(d) and d["kind"] == "List":
            flat += [x for x in (d.get("items") or []) if _is_k8s_doc(x)]
        elif _is_k8s_doc(d):
            flat.append(d)
    if not flat:
        raise ComposeError("這不是 Kubernetes 設定檔，找不到 apiVersion 與 kind。")

    top: List[Dict] = []
    configmaps = {_name(d): {str(k): str(v) for k, v in (d.get("data") or {}).items()} for d in flat if d["kind"] == "ConfigMap"}
    secrets = {_name(d): _decode_secret(d) for d in flat if d["kind"] == "Secret"}
    pvcs = {_name(d): d for d in flat if d["kind"] == "PersistentVolumeClaim"}
    k8s_services = [d for d in flat if d["kind"] == "Service"]
    workloads = [d for d in flat if d["kind"] in ("Deployment", "StatefulSet")]

    seen_kinds = set()
    for d in flat:
        k = d["kind"]
        if k in _BLOCKED_KINDS and k not in seen_kinds:
            top.append({"level": "warn", "text": f"{k}「{_name(d)}」：{_BLOCKED_KINDS[k]}，這個項目不會部署"})
        elif k in _IGNORED_INFO and k not in seen_kinds:
            top.append({"level": "info", "text": _IGNORED_INFO[k]})
        elif k not in _IGNORED_INFO and k not in _BLOCKED_KINDS and k not in (
                "Deployment", "StatefulSet", "Service", "ConfigMap", "Secret", "PersistentVolumeClaim") and k not in seen_kinds:
            top.append({"level": "info", "text": f"{k} 類型的設定平台不認得，不會套用"})
        seen_kinds.add(k)
    if "Namespace" not in seen_kinds and any((d.get("metadata") or {}).get("namespace") for d in flat):
        top.append({"level": "info", "text": "檔案裡指定的 namespace 不會套用，服務一律部署到你自己的空間"})
    if not workloads:
        raise ComposeError("這份檔案裡沒有 Deployment，平台只部署長時間執行的服務。" +
                           ("（" + "；".join(p["text"] for p in top if p["level"] == "warn") + "）" if top else ""))
    if len(workloads) > MAX_SERVICES:
        raise ComposeError(f"服務太多（{len(workloads)} 個），一次最多 {MAX_SERVICES} 個。")

    services, used = [], set()
    for w in workloads:
        probs: List[Dict] = []
        spec = w.get("spec") or {}
        tmpl = spec.get("template") or {}
        labels = ((tmpl.get("metadata") or {}).get("labels")) or {}
        pod = tmpl.get("spec") or {}
        containers = [c for c in (pod.get("containers") or []) if isinstance(c, dict)]
        if w["kind"] == "StatefulSet":
            probs.append({"level": "warn", "text": "StatefulSet 會改用 Deployment 部署：各副本固定名稱、各自獨立保存空間的特性不支援；"
                                                   "資料庫這類服務請維持 1 個副本"})
        if not containers:
            services.append({"service": _name(w), "app_name": _k8s_name(_name(w) or "app"), "image": "", "pods": 1,
                             "port": 80, "container_port": 80, "expose": False, "dns_alias": True, "env": {},
                             "memory": None, "cpu": None, "depends_on": [], "command": None, "args": None,
                             "volumes": [], "secret_keys": [],
                             "problems": probs + [{"level": "block", "text": "沒有任何容器設定"}]})
            continue
        c = containers[0]
        if len(containers) > 1:
            probs.append({"level": "warn", "text": f"有 {len(containers)} 個容器，平台目前只部署第一個（{c.get('name')}），"
                                                   "其他輔助容器不會套用"})
        if pod.get("initContainers"):
            probs.append({"level": "warn", "text": "初始化容器（initContainers）不會套用"})
        sc = c.get("securityContext") or {}
        if sc.get("privileged") or (sc.get("capabilities") or {}).get("add"):
            probs.append({"level": "warn", "text": "特權模式或額外的系統權限在共用平台上不安全，不會套用"})
        for key in ("hostNetwork", "hostPID", "hostIPC"):
            if pod.get(key):
                probs.append({"level": "warn", "text": f"{key} 會直接使用主機的資源，在共用平台上不安全，不會套用"})

        image = str(c.get("image") or "").strip()
        if not image:
            probs.append({"level": "block", "text": "沒有寫映像檔（image）"})
        cports = [p for p in (c.get("ports") or []) if isinstance(p, dict) and p.get("containerPort")]
        # 找選到這個 Deployment 的 Service
        svc = next((s for s in k8s_services
                    if (s.get("spec") or {}).get("selector") and labels
                    and all(labels.get(k) == v for k, v in (s["spec"]["selector"] or {}).items())), None)
        known = default_port_fn(image) if default_port_fn else None
        cport = int(cports[0]["containerPort"]) if cports else (known or 80)
        port, expose = cport, False
        extras = []
        name = _name(w)
        if svc:
            sp = [p for p in ((svc.get("spec") or {}).get("ports") or []) if isinstance(p, dict) and p.get("port")]
            if sp:
                port = int(sp[0]["port"])
                tp = sp[0].get("targetPort")
                if isinstance(tp, int):
                    cport = tp
                elif isinstance(tp, str) and tp.isdigit():
                    cport = int(tp)
                elif isinstance(tp, str):   # 用名稱指到容器 port
                    named = next((p for p in cports if p.get("name") == tp), None)
                    if named:
                        cport = int(named["containerPort"])
                # 2026-10-24：第 2 個以後的 port 也開（targetPort 的解析規則跟第一個相同）
                more = []
                for p in sp[1:]:
                    t = p.get("targetPort")
                    if isinstance(t, str) and not t.isdigit():
                        t = next((int(c["containerPort"]) for c in cports if c.get("name") == t), None)
                    more.append({"port": p.get("port"), "container": t if t is not None else p.get("port")})
                extras = extra_ports(more, port)
                if len(sp) > 1 + MAX_EXTRA_PORTS:
                    probs.append({"level": "warn", "text": f"Service 寫了 {len(sp)} 個 port，平台最多開 {1 + MAX_EXTRA_PORTS} 個，其餘不開"})
            stype = str((svc.get("spec") or {}).get("type") or "ClusterIP")
            expose = stype in ("LoadBalancer", "NodePort")
            if _name(svc) and _name(svc) != name:
                probs.append({"level": "info", "text": f"部署名稱改用 Service 的名字「{_name(svc)}」，其他服務才能照原本的設定連到它"})
                name = _name(svc)
        else:
            probs.append({"level": "info", "text": f"沒有對應的 Service，只給其他服務在內部連線（port {cport}）"})
        if not expose and svc:
            probs.append({"level": "info", "text": f"Service 是 ClusterIP，只給其他服務在內部連線（port {port}）"})

        app_name = _k8s_name(name or "app")
        if app_name in used:
            app_name = _k8s_name(f"{app_name}-{len(used)}")
        used.add(app_name)
        if app_name != name:
            probs.append({"level": "info", "text": f"名稱改成 {app_name}（K8s 名稱只能用小寫英文、數字和連字號，且要英文字母開頭）"})

        env, secret_keys = _env_from_container(c, configmaps, secrets, probs)
        named_secret = [k for k, v in env.items() if looks_like_secret(k, v)]
        if secret_keys or named_secret:
            probs.append({"level": "info", "text": "環境變數 " + "、".join(sorted(set(secret_keys + named_secret))[:5]) +
                          " 會另外存成 Kubernetes Secret，不會寫進部署紀錄"})
        res = c.get("resources") or {}
        limits, requests = res.get("limits") or {}, res.get("requests") or {}
        try:
            pods = max(1, int(spec.get("replicas") or 1))
        except (TypeError, ValueError):
            pods = 1
        vols = _volumes(pod, c, pvcs, probs)
        if pods > 1 and any(v["kind"] == "pvc" for v in vols):
            probs.append({"level": "warn", "text": f"{pods} 個副本會共用同一份保存空間，只有單一節點的叢集能這樣用"})
        cmd = c.get("command") if isinstance(c.get("command"), list) else None
        args = c.get("args") if isinstance(c.get("args"), list) else None
        services.append({
            "service": _name(w), "app_name": app_name, "image": image, "pods": pods,
            "port": int(port), "container_port": int(cport), "expose": expose, "dns_alias": True,
            **({"extra_ports": extras} if extras else {}),
            "env": env, "secret_keys": secret_keys,
            "memory": _mem(limits.get("memory") or requests.get("memory")),
            "cpu": _k8s_cpu(limits.get("cpu") or requests.get("cpu")),
            "depends_on": [], "command": [str(x)[:1000] for x in cmd[:50]] if cmd else None,
            "args": [str(x)[:1000] for x in args[:50]] if args else None, "volumes": vols, "problems": probs,
        })

    # 只給內部連的（資料庫、後端）先部署，對外的前端最後：前端啟動時要連的服務已經在了
    services.sort(key=lambda x: x["expose"])
    return {
        "format": "k8s",
        "services": services,
        "problems": top,
        "deployable": all(not any(p["level"] == "block" for p in s["problems"]) for s in services),
    }
