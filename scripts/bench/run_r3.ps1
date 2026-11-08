# 第三次驗證（2026-11-08）：意圖判斷改由 Gemini 之後，用新的盲測題（Gemini 出題，開發端沒看過）測 ZeroTouch。
# 用法：powershell -ExecutionPolicy Bypass -File scripts\bench\run_r3.ps1 [-Rounds 1]
param([int]$Rounds = 1)
$ErrorActionPreference = 'Continue'
$repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$zt = 'D:\zt-demo'; $bench = Join-Path $repo 'scripts\bench'
$out = Join-Path $repo 'docs\bench_20261108_r3'
New-Item -ItemType Directory -Force $out, (Join-Path $out 'logs') | Out-Null
$runLog = Join-Path $out 'logs\run_r3.log'
function Log($m) { $l = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $m"; Write-Host $l; Add-Content -Path $runLog -Value $l -Encoding UTF8 }
$env:PYTHONIOENCODING = 'utf-8'; $env:PYTHONUNBUFFERED = '1'
$env:BENCH_OUT = $out; $env:BENCH_SNAP = 'D:\bench\snapshots_r3'; $env:BENCH_SYSTEMS = 'zerotouch'
$env:BLIND_TASKS = Join-Path $repo 'docs\bench_blind\blind_tasks_r3.jsonl'
try {
    # 5050 的 Healer 會修所有 namespace，干擾實驗：先停，結束後重新啟動
    Get-CimInstance Win32_Process -Filter "Name like 'python%'" | Where-Object { $_.CommandLine -match 'LLM-enabled-Zero-Touch-Kubernetes-Service-Deployment-Platform\web_demo\.py' } |
        ForEach-Object { Log "停止 5050 PID $($_.ProcessId)"; Stop-Process -Id $_.ProcessId -Force }
    $up = $false; try { $up = (Invoke-WebRequest -UseBasicParsing 'http://127.0.0.1:5051/' -TimeoutSec 3).StatusCode -eq 200 } catch {}
    if (-not $up) {
        $env:WEB_PORT = '5051'; $env:CUSTOMER_CLUSTER_MODE = '0'; $env:HEALER_ONLY_NAMESPACES = 'user-bench-zt'; $env:PUBLIC_BASE_URL = 'http://127.0.0.1:5051'
        Start-Process -FilePath python -ArgumentList (Join-Path $zt 'web_demo.py') -WorkingDirectory $zt -WindowStyle Hidden `
            -RedirectStandardOutput (Join-Path $out 'logs\web5051.log') -RedirectStandardError (Join-Path $out 'logs\web5051.err')
        Remove-Item Env:WEB_PORT, Env:CUSTOMER_CLUSTER_MODE, Env:HEALER_ONLY_NAMESPACES, Env:PUBLIC_BASE_URL
        for ($i = 0; $i -lt 90; $i++) { Start-Sleep 2; try { if ((Invoke-WebRequest -UseBasicParsing 'http://127.0.0.1:5051/' -TimeoutSec 3).StatusCode -eq 200) { break } } catch {} }
    }
    Log "盲測 r3：ZeroTouch $Rounds 輪"
    & python (Join-Path $bench 'bench_blind.py') $Rounds 2>&1 | Out-File -FilePath (Join-Path $out 'logs\blind.log') -Append -Encoding utf8
    Log "評分"
    & python (Join-Path $bench 'grade_blind.py') 2>&1 | Out-File -FilePath (Join-Path $out 'logs\grade.log') -Append -Encoding utf8
    & python (Join-Path $bench 'analyze_r2.py') 2>&1 | Out-File -FilePath (Join-Path $out 'logs\analyze.log') -Append -Encoding utf8
    Log '===== 全部完成 ====='
}
catch { Log "中斷：$_" }
finally {
    Log '重新啟動 5050 網站'
    & powershell -ExecutionPolicy Bypass -File (Join-Path $repo 'scripts\start_services.ps1')
}
