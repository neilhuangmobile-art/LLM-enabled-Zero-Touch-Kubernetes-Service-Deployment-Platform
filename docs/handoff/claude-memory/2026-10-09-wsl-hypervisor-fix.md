---
name: 2026-10-09-wsl-hypervisor-fix
description: 家裡新電腦 WSL2/Docker 報「未啟用虛擬化」但 BIOS 與 VirtualMachinePlatform 都已開，根因是 bcdedit 缺 hypervisorlaunchtype，設 auto 後重開即正常
metadata:
  node_type: memory
  type: project
  originSessionId: 110ca64c-0ce9-476a-aa64-bad49bb5b63d
  modified: 2026-10-08T18:28:34.364Z
---

2026-10-09 裝完 WSL（3.0.1）重開機後，Docker Desktop 顯示「Virtual Machine Platform not enabled」、
`wsl --status` 說「此機未啟用虛擬化」——但查證 `VirtualMachinePlatform` 已 Enabled、
`Win32_Processor.VirtualizationFirmwareEnabled` = True（BIOS SVM 已開），訊息互相矛盾。

根因：`bcdedit /enum {current}` 裡**完全沒有 `hypervisorlaunchtype`**。
修法（系統管理員 PowerShell）：`bcdedit /set hypervisorlaunchtype auto` →
`wsl.exe --install --no-distribution` → 重開機。之後 docker-desktop WSL2 distro Running、
Docker 29.8.2、K8s v1.36.1 node Ready。

**Why**：Docker/WSL 的錯誤訊息會誤導人去進 BIOS，其實不用。
**How to apply**：學校那台（[[2026-10-09-dual-machine-gpu-requirement]]）或之後重灌遇到同樣訊息，先查 bcdedit 這項；
可補進 docs/setup.md 的疑難排解（符合 AGENT_RULES 新手友善原則）。
