# 第二次對照實驗（2026-11-07）：一個指令依序跑完全部階段。
#
# 用法（在專案根目錄）：
#   powershell -ExecutionPolicy Bypass -File scripts\bench\run_r2.ps1            # 全部：before → update → after → grade
#   powershell -ExecutionPolicy Bypass -File scripts\bench\run_r2.ps1 -Phase after   # 只跑某一段（中斷後接續用）
#
# 階段：
#   before  修正前的 ZeroTouch（D:\zt-demo 原本的程式）盲測 1 輪；Claude Code 盲測 2 輪（Claude 不會因修正而改變，只跑這一次）
#   update  把這次的修正套用到 D:\zt-demo（r2_fix.patch＋新檔案），重建知識庫索引
#   after   修正後的 ZeroTouch：原本 22 題 × 3 輪、盲測 × 2 輪
#   grade   用 gemini-pro-latest 依題目的 pass_if 評分，再算統計
# 跑之前會停掉 5050 的網站（它的 Healer 會修所有 namespace，干擾實驗），結束後重新啟動。
param([ValidateSet('all', 'before', 'update', 'after', 'grade')][string]$Phase = 'all')

# Continue：PowerShell 5.1 會把 python 寫到 stderr 的警告當成錯誤，設 Stop 會讓整場實驗中斷；成敗改看結束碼
$ErrorActionPreference = 'Continue'
$repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$zt = 'D:\zt-demo'
$r2 = Join-Path $repo 'docs\bench_20261107_r2'
$bench = Join-Path $repo 'scripts\bench'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUNBUFFERED = '1'
New-Item -ItemType Directory -Force (Join-Path $r2 'before'), (Join-Path $r2 'after'), (Join-Path $r2 'logs') | Out-Null
$runLog = Join-Path $r2 'logs\run_r2.log'

function Log($m) { $l = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $m"; Write-Host $l; Add-Content -Path $runLog -Value $l -Encoding UTF8 }

function Stop-Web($pattern) {
    Get-CimInstance Win32_Process -Filter "Name like 'python%'" | Where-Object { $_.CommandLine -match $pattern } |
        ForEach-Object { Log "停止 PID $($_.ProcessId)：$($_.CommandLine)"; Stop-Process -Id $_.ProcessId -Force -Confirm:$false }
}

function Start-ZT($tag) {
    Stop-Web 'zt-demo\\web_demo\.py'
    # PUBLIC_BASE_URL：zt-demo 的 .env 設成 https 網址時，登入 cookie 會被標成 Secure，測試程式走 http 就帶不回去，
    # 每個請求都變成「Not authenticated」（2026-11-07 冒煙測試發現）。環境變數優先於 .env。
    $env:WEB_PORT = '5051'; $env:CUSTOMER_CLUSTER_MODE = '0'; $env:HEALER_ONLY_NAMESPACES = 'user-bench-zt'; $env:PUBLIC_BASE_URL = 'http://127.0.0.1:5051'
    Start-Process -FilePath python -ArgumentList (Join-Path $zt 'web_demo.py') -WorkingDirectory $zt -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $r2 "logs\web5051_$tag.log") -RedirectStandardError (Join-Path $r2 "logs\web5051_$tag.err")
    Remove-Item Env:WEB_PORT, Env:CUSTOMER_CLUSTER_MODE, Env:HEALER_ONLY_NAMESPACES, Env:PUBLIC_BASE_URL
    for ($i = 0; $i -lt 90; $i++) {
        Start-Sleep -Seconds 2
        try { if ((Invoke-WebRequest -UseBasicParsing 'http://127.0.0.1:5051/' -TimeoutSec 3).StatusCode -eq 200) { Log "5051 測試站已啟動（$tag）"; return } } catch {}
    }
    throw "5051 測試站啟動失敗，看 $r2\logs\web5051_$tag.err"
}

function Py($script, $out, $systems, $argsList, $logName) {
    $env:BENCH_OUT = Join-Path $r2 $out
    $env:BENCH_SNAP = "D:\bench\snapshots_r2_$out"
    $env:BENCH_SYSTEMS = $systems
    Log "執行 $script $argsList（$systems → $out）"
    & python (Join-Path $bench $script) @argsList 2>&1 | Tee-Object -FilePath (Join-Path $r2 "logs\$logName.log") -Append
    $code = $LASTEXITCODE
    if ($code -eq 3) { throw 'Claude 額度用完，停止。額度重置後用 -Phase 接續（已完成的會保留）。' }
    if ($code -ne 0) { throw "$script 結束碼 $code" }
}

function Ensure-Model {
    try { $h = Invoke-RestMethod 'http://127.0.0.1:8765/health' -TimeoutSec 5; if ($h.deploy_loaded) { return } } catch {}
    throw 'Model Server（8765）沒有在跑，先執行 scripts\start_services.ps1'
}

try {
    Ensure-Model
    Stop-Web 'LLM-enabled-Zero-Touch-Kubernetes-Service-Deployment-Platform\\web_demo\.py'

    if ($Phase -in 'all', 'before') {
        Log '===== 階段 before：修正前盲測 ====='
        Start-ZT 'before'
        Py 'bench_blind.py' 'before' 'zerotouch' @('1') 'before_zerotouch'
        Py 'bench_blind.py' 'before' 'claude' @('2') 'before_claude'
    }
    if ($Phase -in 'all', 'update') {
        Log '===== 階段 update：套用修正到 D:\zt-demo ====='
        Stop-Web 'zt-demo\\web_demo\.py'
        git -C $zt apply --check (Join-Path $bench 'r2_fix.patch')
        if ($LASTEXITCODE -eq 0) { git -C $zt apply (Join-Path $bench 'r2_fix.patch'); Log '修補檔已套用' }
        else { Log '修補檔無法套用（可能已經套用過），檢查 deploy_fill 是否存在'; if (-not (Test-Path "$zt\agents\deploy_fill.py")) { throw '修補失敗' } }
        Copy-Item (Join-Path $repo 'agents\deploy_fill.py') "$zt\agents\deploy_fill.py" -Force
        Copy-Item (Join-Path $repo 'rag\k8s_docs\pod_status_explained.md') "$zt\rag\k8s_docs\pod_status_explained.md" -Force
        Push-Location $zt; & python rag\build_index.py --rebuild --deploy 2>&1 | Select-Object -Last 2; Pop-Location
        Log '知識庫索引已重建'
    }
    if ($Phase -in 'all', 'after') {
        Log '===== 階段 after：修正後 ====='
        if (-not (Test-Path "$zt\agents\deploy_fill.py")) { throw '測試站還沒套用修正，先跑 -Phase update' }
        Start-ZT 'after'
        Py 'bench_deploy.py' 'after' 'zerotouch' @('3') 'after_original22'
        Py 'bench_blind.py' 'after' 'zerotouch' @('2') 'after_blind'
    }
    if ($Phase -in 'all', 'grade') {
        Log '===== 階段 grade：評分與統計 ====='
        foreach ($d in 'before', 'after') {
            $env:BENCH_OUT = Join-Path $r2 $d
            & python (Join-Path $bench 'grade_blind.py') 2>&1 | Tee-Object -FilePath (Join-Path $r2 "logs\grade_$d.log") -Append | Select-Object -Last 1
            & python (Join-Path $bench 'analyze_r2.py') 2>&1 | Tee-Object -FilePath (Join-Path $r2 "logs\analyze_$d.log") -Append
        }
    }
    Log '===== 全部完成 ====='
}
catch { Log "中斷：$_" }
finally {
    Stop-Web 'zt-demo\\web_demo\.py'
    Log '重新啟動 5050 網站'
    & powershell -ExecutionPolicy Bypass -File (Join-Path $repo 'scripts\start_services.ps1')
}
