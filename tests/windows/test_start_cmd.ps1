# start.cmd での起動と、Ctrl+C での終了の確認（CI の windows-setup-and-start ジョブから、セットアップの後に実行する。
# 手元の Windows でも実行できる。先に setup.cmd か tests\windows\test_setup_and_start.ps1 でセットアップしておくこと）
#   powershell.exe -NoProfile -ExecutionPolicy Bypass -File tests\windows\test_start_cmd.ps1
# 1. start.cmd を実行し、start.cmd（cmd.exe）自身はすぐに終了することを確認する。
#    バッチが残っていなければ、Ctrl+C で「バッチ ジョブを終了しますか (Y/N)?」が出ることはない
# 2. 別のウィンドウで動く PowerShell（start.ps1）からアプリが起動し、ヘルスチェックに応答することを確認する
# 3. そのウィンドウに Ctrl+C を送り、確認なしに PowerShell とアプリが終了することを確認する
param([int]$Port = 8598, [int]$TimeoutSeconds = 180)

$ErrorActionPreference = "Stop"
$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
Set-Location -LiteralPath $projectRoot
$powershell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"

function Assert([bool]$condition, [string]$message) {
    if (-not $condition) { throw "確認に失敗しました: $message" }
}

function Test-Healthy([string]$url) {
    try {
        return (Invoke-WebRequest $url -UseBasicParsing -TimeoutSec 5).Content -eq "ok"
    } catch {
        return $false
    }
}

# 対象のコンソールに Ctrl+C を送る。送信側も同じコンソールに接続するため、このスクリプト自身が止まらないよう
# 別の PowerShell プロセスから送る（送信側は Ctrl+C を無視してから送る）
$sendCtrlC = @'
param([int]$TargetPid)
Add-Type -Namespace Win32 -Name Console -MemberDefinition @"
[DllImport("kernel32.dll", SetLastError = true)] public static extern bool FreeConsole();
[DllImport("kernel32.dll", SetLastError = true)] public static extern bool AttachConsole(uint processId);
[DllImport("kernel32.dll", SetLastError = true)] public static extern bool SetConsoleCtrlHandler(System.IntPtr handler, bool add);
[DllImport("kernel32.dll", SetLastError = true)] public static extern bool GenerateConsoleCtrlEvent(uint ctrlEvent, uint processGroupId);
"@
[Win32.Console]::FreeConsole() | Out-Null
if (-not [Win32.Console]::AttachConsole($TargetPid)) { exit 2 }
[Win32.Console]::SetConsoleCtrlHandler([System.IntPtr]::Zero, $true) | Out-Null
if (-not [Win32.Console]::GenerateConsoleCtrlEvent(0, 0)) { exit 3 }
exit 0
'@

# CI のランナーなどが Ctrl+C を無効にしてこのスクリプトを起動していると、その設定が子孫プロセスに引き継がれ、
# start.cmd の作りによらず Ctrl+C が届かない。利用者がダブルクリックした場合と同じ条件にするため、有効に戻す
Add-Type -Namespace Win32 -Name CtrlHandler -MemberDefinition @"
[DllImport("kernel32.dll", SetLastError = true)] public static extern bool SetConsoleCtrlHandler(System.IntPtr handler, bool add);
"@
[Win32.CtrlHandler]::SetConsoleCtrlHandler([System.IntPtr]::Zero, $false) | Out-Null

$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
Assert (Test-Path $venvPython) "仮想環境がありません。先にセットアップしてください"

$env:VPS_PORT = "$Port"
$env:VPS_DATA_DIR = Join-Path ([IO.Path]::GetTempPath()) ("vps-ci-" + [guid]::NewGuid().ToString("N"))
$healthUrl = "http://127.0.0.1:$Port/_stcore/health"
$senderPath = Join-Path ([IO.Path]::GetTempPath()) ("vps-send-ctrl-c-" + [guid]::NewGuid().ToString("N") + ".ps1")
Set-Content -LiteralPath $senderPath -Value $sendCtrlC -Encoding UTF8
$app = $null

try {
    # --- 1. start.cmd 自身はすぐに終了する ------------------------------------------
    $cmd = Start-Process -PassThru -FilePath "cmd.exe" -ArgumentList "/c", "start.cmd -NoPause -NoBrowser" -WorkingDirectory $projectRoot
    Assert ($cmd.WaitForExit(30000)) "start.cmd が 30 秒以内に終了しませんでした（バッチが残ると Ctrl+C で確認が出ます）"
    Assert ($cmd.ExitCode -eq 0) "start.cmd が終了コード $($cmd.ExitCode) で終了しました"

    # --- 2. 別のウィンドウの PowerShell からアプリが起動する ----------------------------
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline -and $null -eq $app) {
        $found = Get-CimInstance Win32_Process -Filter "Name = 'powershell.exe'" |
            Where-Object { $_.CommandLine -like "*scripts\windows\start.ps1*" } |
            Select-Object -First 1
        if ($found) { $app = Get-Process -Id $found.ProcessId } else { Start-Sleep -Seconds 1 }
    }

    Assert ($null -ne $app) "start.ps1 を実行する PowerShell が見つかりません"

    $healthy = $false
    while ((Get-Date) -lt $deadline -and -not $app.HasExited) {
        if (Test-Healthy $healthUrl) { $healthy = $true; break }
        Start-Sleep -Seconds 2
    }

    Assert $healthy "時間内に Streamlit のヘルスチェックが応答しませんでした"

    # --- 3. Ctrl+C で、確認なしに終了する ----------------------------------------------
    & $powershell -NoProfile -ExecutionPolicy Bypass -File $senderPath -TargetPid $app.Id
    Assert ($LASTEXITCODE -eq 0) "Ctrl+C を送れませんでした（終了コード $LASTEXITCODE）"
    # 確認が出て入力待ちになると、PowerShell は終了しない
    Assert ($app.WaitForExit(60000)) "Ctrl+C の後、60 秒以内に start.ps1 の PowerShell が終了しませんでした"
    # PowerShell が先に終わり、Streamlit の停止が少し遅れることがあるため、しばらく待つ
    $stopDeadline = (Get-Date).AddSeconds(30)
    while ((Get-Date) -lt $stopDeadline -and (Test-Healthy $healthUrl)) { Start-Sleep -Seconds 1 }
    Assert (-not (Test-Healthy $healthUrl)) "Ctrl+C の後も Streamlit が応答しています"

    Write-Host "start.cmd での起動と、Ctrl+C での終了を確認しました。" -ForegroundColor Green
} finally {
    # 失敗した場合に、起動したプロセスを残さない（Streamlit は子孫プロセスのため、ツリーごと止める）
    if ($null -ne $app -and -not $app.HasExited) { taskkill /T /F /PID $app.Id | Out-Null }
    Remove-Item -Force -ErrorAction SilentlyContinue $senderPath
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $env:VPS_DATA_DIR
}
