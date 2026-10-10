# 一鍵啟動：Model Server（8765）與網站（WEB_PORT，預設 5050），用獨立的 Windows 程序執行。
#
# 為什麼要有這個腳本（2026-10-10）：模型伺服器曾在 Claude Code 的背景工作裡啟動，
# 對話重新開啟時那個背景工作被結束，模型伺服器跟著停掉，網站頂端出現紅色橫幅。
# 用 Start-Process 啟動的程序跟終端機、對話都無關，關掉視窗也會繼續執行。
#
# 用法（在專案根目錄）：
#   powershell -ExecutionPolicy Bypass -File scripts\start_services.ps1           # 已經在跑的不重複啟動
#   powershell -ExecutionPolicy Bypass -File scripts\start_services.ps1 -Restart  # 先關掉再重新啟動
# 輸出記在 logs\model_server.log、logs\web.log（錯誤訊息在 .err）。
param([switch]$Restart)

$root = Split-Path -Parent $PSScriptRoot
$logs = Join-Path $root "logs"
New-Item -ItemType Directory -Force $logs | Out-Null
$env:PYTHONIOENCODING = "utf-8"

function Find-Proc($pattern) {
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -and $_.CommandLine -match $pattern -and $_.CommandLine -notmatch "zt-demo" }
}

function Start-One($name, $script, $pattern, $log) {
    $running = Find-Proc $pattern
    if ($running -and $Restart) {
        $running | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
        Start-Sleep 2
        $running = $null
    }
    if ($running) {
        Write-Host "$name 已經在執行（PID $($running[0].ProcessId)），沒有重複啟動。要重新啟動請加 -Restart。"
        return
    }
    Start-Process -FilePath python -ArgumentList (Join-Path $root $script) -WorkingDirectory $root -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $logs "$log.log") -RedirectStandardError (Join-Path $logs "$log.err")
    Write-Host "$name 已啟動，輸出記在 logs\$log.log"
}

Start-One "Model Server" "core\model_server.py" "core[\\/]model_server\.py" "model_server"
Start-One "網站" "web_demo.py" "[\\/]web_demo\.py" "web"

Write-Host "等待模型載入（第一次約 1 到 2 分鐘）..."
for ($i = 0; $i -lt 60; $i++) {
    Start-Sleep 5
    try {
        $h = Invoke-RestMethod "http://127.0.0.1:8765/health" -TimeoutSec 3
        if ($h.deploy_loaded -and $h.monitor_loaded) {
            Write-Host "Model Server 就緒：$($h.gpu)，$($h.deploy_precision)"
            break
        }
    } catch {}
    if ($i -eq 59) { Write-Host "Model Server 5 分鐘內沒有就緒，請看 logs\model_server.err" }
}
