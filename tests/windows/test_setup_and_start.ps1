# Windows でのセットアップと起動の確認（CI の windows-setup ジョブから実行する。手元の Windows でも実行できる）
#   powershell.exe -NoProfile -ExecutionPolicy Bypass -File tests\windows\test_setup_and_start.ps1
# 1. scripts\windows\setup.ps1 でセットアップする（uv で Python と依存パッケージを同期し、.env を作る）
# 2. start.ps1 と同じ仮想環境の Python で起動処理（launcher）を実行し、Streamlit のヘルスチェックが応答することを確認する
# 3. 2 回目の起動が二重起動として検知され、正常終了することを確認する
# 4. 起動したプロセスを止める
param([int]$Port = 8599, [int]$TimeoutSeconds = 180)

$ErrorActionPreference = "Stop"
$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
Set-Location -LiteralPath $projectRoot
$powershell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"

function Assert([bool]$condition, [string]$message) {
    if (-not $condition) { throw "確認に失敗しました: $message" }
}

# --- 1. セットアップ ---------------------------------------------------------
& $powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $projectRoot "scripts\windows\setup.ps1") -NoPause
Assert ($LASTEXITCODE -eq 0) "setup.ps1 が終了コード $LASTEXITCODE で終了しました"

$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
Assert (Test-Path $venvPython) "仮想環境の python.exe がありません"
Assert (Test-Path (Join-Path $projectRoot ".env")) ".env が作成されていません"

# --- 2. 起動とヘルスチェック -------------------------------------------------
$env:VPS_PORT = "$Port"
$env:VPS_DATA_DIR = Join-Path ([IO.Path]::GetTempPath()) ("vps-ci-" + [guid]::NewGuid().ToString("N"))
$env:PYTHONUTF8 = "1"
$healthUrl = "http://127.0.0.1:$Port/_stcore/health"
$process = Start-Process -PassThru -NoNewWindow -FilePath $venvPython -ArgumentList "-m", "volumio_playlist_studio.launcher", "--no-browser"

try {
    $healthy = $false
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline -and -not $process.HasExited) {
        try {
            $response = Invoke-WebRequest $healthUrl -UseBasicParsing -TimeoutSec 5
            if ($response.Content -eq "ok") { $healthy = $true; break }
        } catch {
            # 起動中は接続できないため、時間内は待ち続ける
        }

        Start-Sleep -Seconds 2
    }

    Assert $healthy "時間内に Streamlit のヘルスチェックが応答しませんでした"
    $page = Invoke-WebRequest "http://127.0.0.1:$Port/" -UseBasicParsing -TimeoutSec 10
    Assert ($page.StatusCode -eq 200) "トップページが HTTP $($page.StatusCode) を返しました"

    # --- 3. 二重起動の検知 ---------------------------------------------------
    & $venvPython -m volumio_playlist_studio.launcher --no-browser
    Assert ($LASTEXITCODE -eq 0) "2 回目の起動が終了コード $LASTEXITCODE で終了しました"

    # 初期設定では PC 自身からのみ接続できる（127.0.0.1 だけで待ち受ける）
    $listeners = @(Get-NetTCPConnection -LocalPort $Port -State Listen | ForEach-Object { $_.LocalAddress })
    Assert ($listeners.Count -gt 0 -and -not ($listeners | Where-Object { $_ -ne "127.0.0.1" })) "127.0.0.1 以外でも待ち受けています: $($listeners -join ', ')"

    Write-Host "Windows でのセットアップと起動を確認しました。" -ForegroundColor Green
} finally {
    # --- 4. 停止（Streamlit は launcher の子プロセスのため、プロセスツリーごと止める） ---
    if (-not $process.HasExited) { taskkill /T /F /PID $process.Id | Out-Null }
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $env:VPS_DATA_DIR
}
