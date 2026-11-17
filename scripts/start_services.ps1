# 一鍵啟動：Model Server（8765）與網站（WEB_PORT，預設 5050），用獨立的 Windows 程序執行。
#
# 為什麼要有這個腳本（2026-10-10）：模型伺服器曾在 Claude Code 的背景工作裡啟動，
# 對話重新開啟時那個背景工作被結束，模型伺服器跟著停掉，網站頂端出現紅色橫幅。
# 用 Start-Process 啟動的程序跟終端機、對話都無關，關掉視窗也會繼續執行。
#
# 用法（在專案根目錄）：
#   powershell -ExecutionPolicy Bypass -File scripts\start_services.ps1           # 已經在跑的不重複啟動
#   powershell -ExecutionPolicy Bypass -File scripts\start_services.ps1 -Restart  # 先關掉再重新啟動
#   開機自動啟動：powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1
# 輸出記在 logs\model_server.log、logs\web.log（錯誤訊息在 .err），啟動過程記在 logs\startup.log。
# 不要把這個腳本的輸出接到 | Out-Null 之類的管線：啟動的程序會繼承管線，呼叫端會一直等到網站結束。
param([switch]$Restart, [switch]$AtBoot)

$root = Split-Path -Parent $PSScriptRoot
$logs = Join-Path $root "logs"
New-Item -ItemType Directory -Force $logs | Out-Null
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUNBUFFERED = "1"   # 輸出即時寫進紀錄檔，不然出問題時紀錄檔可能還是空的
$startupLog = Join-Path $logs "startup.log"

function Log($msg) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $msg"
    Write-Host $line
    Add-Content -Path $startupLog -Value $line -Encoding UTF8
}

function Find-Proc($pattern) {
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -and $_.CommandLine -match $pattern -and $_.CommandLine -notmatch "zt-demo" }
}

function Start-One($name, $script, $pattern, $log, $py = "python") {
    $running = Find-Proc $pattern
    if ($running -and $Restart) {
        $running | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
        Start-Sleep 2
        $running = $null
    }
    if ($running) {
        Log "$name 已經在執行（PID $($running[0].ProcessId)），沒有重複啟動。要重新啟動請加 -Restart。"
        return
    }
    Start-Process -FilePath $py -ArgumentList (Join-Path $root $script) -WorkingDirectory $root -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $logs "$log.log") -RedirectStandardError (Join-Path $logs "$log.err")
    Log "$name 已啟動，輸出記在 logs\$log.log"
}

function Test-Cluster {
    $out = kubectl get nodes --no-headers 2>$null
    return ($LASTEXITCODE -eq 0 -and "$out" -match "\sReady\s")
}

function Find-DockerDesktop {
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA "Programs\DockerDesktop\Docker Desktop.exe"),
        (Join-Path $env:ProgramFiles "Docker\Docker\Docker Desktop.exe")
    )
    return $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
}

Log "===== 開始啟動$(if ($AtBoot) { '（開機自動啟動）' }) ====="

# 1. 模型伺服器只用顯卡，不用等 Docker，先開始載入（需要 1 到 2 分鐘）
Start-One "Model Server" "core\model_server.py" "core[\\/]model_server\.py" "model_server"

# 1b. 多代理人部署解析的本地模型（Qwen3.5-4B 4-bit，127.0.0.1:8766，2026-11-17）。
#     要 transformers 5，用另一個 venv（路徑可用環境變數 PIPELINE_PYTHON 改）。沒有這個 venv 時網站照樣啟動，
#     部署解析會退回原本的方式，部署單上會提示。
$qwenPy = if ($env:PIPELINE_PYTHON) { $env:PIPELINE_PYTHON } else { "C:\zt-k8s\.venv-qwen35\Scripts\python.exe" }
if (Test-Path $qwenPy) {
    $env:HF_HUB_OFFLINE = "1"   # 模型已經下載好，啟動時不連網查新版
    Start-One "多代理人模型（8766）" "core\pipeline_model_server.py" "pipeline_model_server\.py" "pipeline_model" $qwenPy
    Remove-Item Env:HF_HUB_OFFLINE
} else {
    Log "找不到 $qwenPy，多代理人部署解析會退回原本的方式"
}

# 2. 網站啟動時只檢查一次 Kubernetes 有沒有連上，太早啟動會一直當成沒有叢集，所以先等叢集就緒
if (-not (Test-Cluster)) {
    if (-not (Get-Process "Docker Desktop" -ErrorAction SilentlyContinue)) {
        $docker = Find-DockerDesktop
        if ($docker) {
            Log "Docker Desktop 沒有在執行，正在啟動：$docker"
            Start-Process -FilePath $docker
        } else {
            Log "找不到 Docker Desktop，請手動開啟。網站仍會啟動，畫面上會提示 Docker 沒開。"
        }
    }
    Log "等待 Kubernetes 就緒（最多 10 分鐘）..."
    $ready = $false
    for ($i = 0; $i -lt 120; $i++) {
        Start-Sleep 5
        if (Test-Cluster) { $ready = $true; break }
    }
    if ($ready) { Log "Kubernetes 已就緒（等了約 $(($i + 1) * 5) 秒）" }
    else { Log "Kubernetes 10 分鐘內沒有就緒，網站照樣啟動，畫面會顯示 Docker 沒開的提示與處理步驟。" }
} else {
    Log "Kubernetes 已就緒"
}

# 3. 網站
Start-One "網站" "web_demo.py" "[\\/]web_demo\.py" "web"

# 4. 確認模型載入完成
for ($i = 0; $i -lt 60; $i++) {
    Start-Sleep 5
    try {
        $h = Invoke-RestMethod "http://127.0.0.1:8765/health" -TimeoutSec 3
        if ($h.deploy_loaded -and $h.monitor_loaded) {
            Log "Model Server 就緒：$($h.gpu)，$($h.deploy_precision)"
            break
        }
    } catch {}
    if ($i -eq 59) { Log "Model Server 5 分鐘內沒有就緒，請看 logs\model_server.err" }
}
for ($i = 0; $i -lt 36; $i++) {
    try {
        $p = Invoke-RestMethod "http://127.0.0.1:8766/health" -TimeoutSec 3
        if ($p.loaded) { Log "多代理人模型就緒：$($p.model)，顯存 $($p.vram_gb) GB"; break }
    } catch {}
    Start-Sleep 5
    if ($i -eq 35) { Log "多代理人模型 3 分鐘內沒有就緒，請看 logs\pipeline_model.err（部署解析會先退回原本的方式）" }
}
try {
    $code = (Invoke-WebRequest "http://127.0.0.1:5050/" -UseBasicParsing -TimeoutSec 10).StatusCode
    Log "網站回應 $code"
} catch { Log "網站沒有回應，請看 logs\web.err" }
Log "===== 啟動流程結束 ====="
