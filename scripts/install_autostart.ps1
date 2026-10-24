# 開機自動啟動 ZeroTouch（模型伺服器與網站）。
#
# 做法：在 Windows「啟動」資料夾放一個捷徑，登入後在背景執行 start_services.ps1 -AtBoot。
# 不需要系統管理員權限；Docker Desktop 沒開時腳本會幫忙開，並等 Kubernetes 就緒才啟動網站。
# 為什麼需要（2026-10-11）：電腦重新開機後三個服務全部停掉，網站、Healer、對外網址都沒有回應。
#
# 用法（在專案根目錄）：
#   powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1          # 設定開機自動啟動
#   powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1 -Remove  # 取消
#   -LinkDir <資料夾>：把捷徑放到別的資料夾（測試用，預設是 Windows「啟動」資料夾）
#
# 2026-10-24 修正：WScript.Shell 的 CreateShortcut().Save() 用中文檔名時，在部分電腦上不會報錯、
# 檔案卻沒有建立（學校電腦 10/21 實測），腳本照樣印「已設定」。改成先用英文檔名建立再改名，
# 並確認檔案真的存在，失敗就印出原因並回傳錯誤碼。
param([switch]$Remove, [string]$LinkDir = "")

$startup = if ($LinkDir) { $LinkDir } else { [Environment]::GetFolderPath("Startup") }
$link = Join-Path $startup "ZeroTouch 平台.lnk"
$tmpLink = Join-Path $startup "ZeroTouch-autostart.lnk"

if ($Remove) {
    if (Test-Path $link) { Remove-Item $link; Write-Host "已取消開機自動啟動（刪除 $link）" }
    else { Write-Host "本來就沒有設定開機自動啟動" }
    return
}

$root = Split-Path -Parent $PSScriptRoot
$script = Join-Path $root "scripts\start_services.ps1"
$shell = New-Object -ComObject WScript.Shell
$s = $shell.CreateShortcut($tmpLink)
$s.TargetPath = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
$s.Arguments = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$script`" -AtBoot"
$s.WorkingDirectory = $root
$s.WindowStyle = 7   # 最小化
$s.Description = "登入後自動啟動 ZeroTouch 模型伺服器與網站"
$s.Save()
if (-not (Test-Path -LiteralPath $tmpLink)) {
    Write-Host "設定失敗：捷徑沒有建立成功（$tmpLink）。請確認這個資料夾可以寫入。" -ForegroundColor Red
    exit 1
}
if (Test-Path -LiteralPath $link) { Remove-Item -LiteralPath $link }
Rename-Item -LiteralPath $tmpLink -NewName (Split-Path -Leaf $link)
if (-not (Test-Path -LiteralPath $link)) {
    Write-Host "設定失敗：捷徑改名成「$(Split-Path -Leaf $link)」沒有成功，暫存捷徑留在 $tmpLink。" -ForegroundColor Red
    exit 1
}
Write-Host "已設定開機自動啟動：$link"
Write-Host "下次登入後會在背景啟動，過程記在 logs\startup.log。要取消請加 -Remove 再執行一次。"
