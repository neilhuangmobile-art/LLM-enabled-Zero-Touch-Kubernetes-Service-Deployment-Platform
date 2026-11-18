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

# 讀設定：直接問 core/config.py（Python 端也用同一份），不在這裡重寫一套 .env 解析與預設值
# （2026-11-18 code review：兩邊讀法不同時，重複的鍵會讓腳本和模型伺服器各用各的設定）
$cfgJson = & python -c "import json,sys; sys.path.insert(0, r'$root'); from core import config as c; print(json.dumps({k: getattr(c, k) for k in ['LLM_ENGINE','LLM_ENGINE_ERROR','LLAMACPP_DIR','GGUF_DIR','LLAMACPP_DEPLOY_URL','LLAMACPP_MONITOR_URL','LLAMACPP_REASON_URL','LLAMACPP_DEPLOY_GGUF','LLAMACPP_MONITOR_GGUF','LLAMACPP_REASON_GGUF','LLAMACPP_DEVICE']}))" 2>$null | Select-Object -Last 1
try { $cfg = $cfgJson | ConvertFrom-Json } catch { $cfg = $null }
if (-not $cfg) {
    Log "讀不到 core/config.py 的設定（python 執行失敗），這次用 transformers 引擎。請確認 python 可以執行、專案資料夾完整。"
    $engine = "transformers"
} else {
    $engine = $cfg.LLM_ENGINE
    if ($cfg.LLM_ENGINE_ERROR) { Log $cfg.LLM_ENGINE_ERROR }
}

# 0. llama.cpp 模式（2026-11-18）：三顆模型各由一個 llama-server 常駐。AMD 用 Vulkan 版、NVIDIA 用 CUDA 版，
#    GGUF 檔兩台共用。model_server 只轉送請求；多代理人流程直接打 4B 的 llama-server，不啟動 8766。
#    回傳 $true 代表就緒（或本來就在執行且載的是對的檔案）。
function Start-Llama($name, $gguf, $url, $exe, $extra, $waitSec) {
    $port = ([uri]$url).Port
    $path = Join-Path $cfg.GGUF_DIR $gguf
    if (-not (Test-Path $path)) {
        Log "找不到$name的模型檔 $path。請照 docs\setup.md 6.1 節下載 GGUF 後重新執行；網站仍會啟動，但會顯示模型沒有執行。"
        return $false
    }
    if (-not (Test-Path $exe)) {
        Log "找不到 llama.cpp（$exe），$name沒有啟動。請照 docs\setup.md 6.1 節下載並解壓後重新執行。"
        return $false
    }
    $old = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($old) {
        $proc = Get-Process -Id $old.OwningProcess -ErrorAction SilentlyContinue
        if ($proc -and $proc.Name -eq "llama-server") {
            if (-not $Restart) {
                # 已經在執行：確認載的是設定要的檔案（改了 .env 卻沒加 -Restart 時會不一樣）
                $loaded = ""
                try { $loaded = Split-Path -Leaf ((Invoke-RestMethod "$url/props" -TimeoutSec 3).model_path) } catch {}
                if ($loaded -and $loaded -ne $gguf) {
                    Log "$name（port $port）目前載入的是 $loaded，設定要的是 $gguf。請加 -Restart 重新啟動。"
                    return $false
                }
                Log "$name（port $port）已經在執行，沒有重複啟動。要重新啟動請加 -Restart。"
                return $true
            }
            Stop-Process -Id $proc.Id -Force
            Start-Sleep 2
        } else {
            # 不關掉別的程式：只說明是誰占用、怎麼處理
            Log "port $port 被其他程式（$(if ($proc) { $proc.Name } else { '未知' })，PID $($old.OwningProcess)）占用，$name沒有啟動。請關掉那個程式，或在 .env 改用別的 port。"
            return $false
        }
    }
    $llamaArgs = @("-m", "`"$path`"", "-np", "1", "--host", "127.0.0.1", "--port", "$port") + $extra
    $errLog = Join-Path $logs "llama_$port.err"
    $p = Start-Process -FilePath $exe -ArgumentList $llamaArgs -WorkingDirectory $root -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $logs "llama_$port.log") -RedirectStandardError $errLog
    # 等它就緒再啟動下一個：同時啟動時顯卡記憶體會互搶，最後拿的會被放到共用記憶體、速度掉一半（11-18 實測）
    for ($i = 0; $i -lt $waitSec; $i++) {
        Start-Sleep 1
        if ($p.HasExited) {
            $tail = (Get-Content $errLog -Tail 3 -ErrorAction SilentlyContinue) -join " / "
            Log "$name 啟動失敗（程式結束，代碼 $($p.ExitCode)）。最後的紀錄：$tail（完整內容在 logs\llama_$port.err）"
            return $false
        }
        try { if ((Invoke-RestMethod "$url/health" -TimeoutSec 2).status -eq "ok") { Log "$name 已就緒（$gguf，port $port）"; return $true } } catch {}
    }
    Log "$name $waitSec 秒內沒有就緒，請看 logs\llama_$port.err"
    return $false
}

$reasonUrl = if ($cfg) { $cfg.LLAMACPP_REASON_URL } else { "http://127.0.0.1:8773" }
if ($engine -eq "llamacpp") {
    $isNvidia = [bool](Get-Command nvidia-smi -ErrorAction SilentlyContinue)
    $build = if ($isNvidia) { "cuda" } else { "vulkan" }
    $gpuExe = Join-Path $cfg.LLAMACPP_DIR "$build\llama-server.exe"
    $cpuExe = Join-Path $cfg.LLAMACPP_DIR "cpu\llama-server.exe"
    # AMD 的內顯也會被列成 Vulkan 裝置，指定第一張（獨顯）；可用 .env 的 LLAMACPP_DEVICE 改
    $device = $cfg.LLAMACPP_DEVICE
    if (-not $device -and -not $isNvidia) { $device = "Vulkan0" }
    $gpuArgs = @("-ngl", "99") + $(if ($device) { @("--device", $device) } else { @() })
    Log "推理引擎：llama.cpp（$build$(if ($device) { "，$device" })；監控模型用 CPU）"
    # 由大到小依序啟動。GPU 模型沒就緒就不啟動下一個 GPU 模型（會互搶記憶體），監控模型跑 CPU 不受影響。
    # context：多代理人與部署 16384（聊天帶歷史、部署帶 RAG 範例時 8192 可能不夠），監控 8192
    $ok = Start-Llama "多代理人模型" $cfg.LLAMACPP_REASON_GGUF $reasonUrl $gpuExe ($gpuArgs + @("-c", "16384")) 180
    if ($ok) {
        $null = Start-Llama "部署模型" $cfg.LLAMACPP_DEPLOY_GGUF $cfg.LLAMACPP_DEPLOY_URL $gpuExe ($gpuArgs + @("-c", "16384")) 180
    } else {
        Log "多代理人模型沒有就緒，先不啟動部署模型（避免顯卡記憶體互搶）。處理完上面的問題後重新執行這個腳本。"
    }
    # 監控 1.5B 跑 CPU（不占顯存，只給背景 Healer 用；8 執行緒，留 CPU 給網站）
    $null = Start-Llama "監控模型" $cfg.LLAMACPP_MONITOR_GGUF $cfg.LLAMACPP_MONITOR_URL $cpuExe @("-ngl", "0", "-t", "8", "-c", "8192") 120
}

# 1. 模型伺服器只用顯卡，不用等 Docker，先開始載入（需要 1 到 2 分鐘；llama.cpp 模式只載入 tokenizer）
Start-One "Model Server" "core\model_server.py" "core[\\/]model_server\.py" "model_server"

# 1b. 多代理人部署解析的本地模型（Qwen3.5-4B，127.0.0.1:8766，2026-11-17）。
#     要 transformers 5，用另一個 venv（路徑可用環境變數 PIPELINE_PYTHON 改）。沒有這個 venv 時網站照樣啟動，
#     部署解析會退回原本的方式，部署單上會提示。
#     2026-11-18：NVIDIA 用 4-bit，AMD 用 bf16（core/pipeline_model_server.py 自動判斷）。
#     預設依序找學校電腦（C:\zt-k8s\.venv-qwen35）與家裡 AMD 電腦（D:\zt-venvs\qwen35）的 venv。
$qwenPy = $env:PIPELINE_PYTHON
if (-not $qwenPy) {
    $qwenPy = @("C:\zt-k8s\.venv-qwen35\Scripts\python.exe", "D:\zt-venvs\qwen35\Scripts\python.exe") |
        Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $qwenPy) { $qwenPy = "C:\zt-k8s\.venv-qwen35\Scripts\python.exe" }
}
if ($engine -eq "llamacpp") {
    Log "llama.cpp 模式：多代理人流程直接使用 $reasonUrl，不啟動 8766"
} elseif (Test-Path $qwenPy) {
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
            Log "Model Server 就緒：$(if ($h.gpu) { "$($h.gpu)，" })$($h.deploy_precision)$(if ($h.problems) { "；注意：$($h.problems -join "；")" })"
            break
        }
    } catch {}
    if ($i -eq 59) {
        if ($engine -eq "llamacpp") { Log "Model Server 5 分鐘內沒有就緒，請看 logs\model_server.err 與 logs\llama_8771.err、llama_8772.err" }
        else { Log "Model Server 5 分鐘內沒有就緒，請看 logs\model_server.err" }
    }
}
$reasonHealth = if ($engine -eq "llamacpp") { "$reasonUrl/health" } else { "http://127.0.0.1:8766/health" }
for ($i = 0; $i -lt 36; $i++) {
    try {
        $p = Invoke-RestMethod $reasonHealth -TimeoutSec 3
        if ($p.loaded -or $p.status -eq "ok") { Log "多代理人模型就緒$(if ($p.model) { "：$($p.model)" })"; break }
    } catch {}
    Start-Sleep 5
    if ($i -eq 35) {
        $errLog = if ($engine -eq "llamacpp") { "logs\llama_$(([uri]$reasonUrl).Port).err" } else { "logs\pipeline_model.err" }
        Log "多代理人模型 3 分鐘內沒有就緒，請看 $errLog（部署解析會先退回原本的方式）"
    }
}
try {
    $code = (Invoke-WebRequest "http://127.0.0.1:5050/" -UseBasicParsing -TimeoutSec 10).StatusCode
    Log "網站回應 $code"
} catch { Log "網站沒有回應，請看 logs\web.err" }
Log "===== 啟動流程結束 ====="
