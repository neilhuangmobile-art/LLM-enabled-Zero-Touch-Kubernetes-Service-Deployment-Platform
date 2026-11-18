"""
下載 Kubernetes 官方文件（kubernetes/website，CC BY 4.0）到 rag/knowledge/k8s_official/（2026-11-15）。

只收新手部署與排錯會用到的頁面；移除 Hugo 排版語法（{{< ... >}}、front matter），保留原文。
每個檔案開頭標示來源網址與授權。這台電腦遇到平行連線會卡住，所以循序下載。
用法：python scripts/rag/fetch_k8s_docs.py
"""
import re
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "rag" / "knowledge" / "k8s_official"
RAW = "https://raw.githubusercontent.com/kubernetes/website/main/content/en/docs/"
SITE = "https://kubernetes.io/docs/"
PAGES = {  # 檔名: 文件路徑（不含 .md）
    # 既有 8 篇（部署設定）
    "persistent-volumes": "concepts/storage/persistent-volumes",
    "cron-jobs": "concepts/workloads/controllers/cron-jobs",
    "secret": "concepts/configuration/secret",
    "define-environment-variable": "tasks/inject-data-application/define-environment-variable-container",
    "service": "concepts/services-networking/service",
    "probes": "tasks/configure-pod-container/configure-liveness-readiness-startup-probes",
    "manage-resources": "concepts/configuration/manage-resources-containers",
    "deployment": "concepts/workloads/controllers/deployment",
    # 新增：排錯（疑難排解資料庫的依據）
    "debug-pods": "tasks/debug/debug-application/debug-pods",
    "debug-running-pod": "tasks/debug/debug-application/debug-running-pod",
    "determine-reason-pod-failure": "tasks/debug/debug-application/determine-reason-pod-failure",
    "debug-service": "tasks/debug/debug-application/debug-service",
    "debug-init-containers": "tasks/debug/debug-application/debug-init-containers",
    "pod-lifecycle": "concepts/workloads/pods/pod-lifecycle",
    "images": "concepts/containers/images",
    "assign-memory-resource": "tasks/configure-pod-container/assign-memory-resource",
    "assign-cpu-resource": "tasks/configure-pod-container/assign-cpu-resource",
    "configmap": "concepts/configuration/configmap",
    "statefulset": "concepts/workloads/controllers/statefulset",
    "job": "concepts/workloads/controllers/job",
    "horizontal-pod-autoscale": "concepts/workloads/autoscaling/horizontal-pod-autoscale",
    "ingress": "concepts/services-networking/ingress",
}


def clean(text: str) -> str:
    text = re.sub(r"\A---\n.*?\n---\n", "", text, flags=re.S)           # front matter
    text = re.sub(r"\{\{[<%]\s*/?\s*(?:glossary_tooltip|link)[^}]*text=\"([^\"]*)\"[^}]*[>%]\}\}", r"\1", text)
    text = re.sub(r"\{\{[<%].*?[>%]\}\}", "", text, flags=re.S)        # 其他 shortcode
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for name, path in PAGES.items():
        dst = OUT / f"{name}.md"
        if dst.exists():
            continue
        body = None
        for cand in (f"{path}.md", f"{path}/_index.md", f"{path}/index.md"):
            r = requests.get(RAW + cand, timeout=30)
            if r.status_code == 200:
                body = r.text
                break
        if body is None:
            print(f"  ✗ {name}：找不到")
            continue
        head = (f"# Kubernetes 官方文件：{name}\n\n> 來源：{SITE}{path}/ （kubernetes/website，CC BY 4.0）。"
                f"內容為原文節錄，已移除網站排版語法。\n\n")
        dst.write_text(head + clean(body), encoding="utf-8")
        print(f"  ✓ {name}：{dst.stat().st_size // 1024} KB", flush=True)
        time.sleep(0.3)


if __name__ == "__main__":
    main()
