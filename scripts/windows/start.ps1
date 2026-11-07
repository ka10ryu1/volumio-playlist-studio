# Volumio Playlist Studio の起動処理（ルートの start.cmd から呼び出す内部スクリプト）
param([switch]$NoPause)

$ErrorActionPreference = "Stop"
# Resolve-Path は UNC パス (\\wsl.localhost\...) に "Microsoft.PowerShell.Core\FileSystem::" を付け ".." も残すため GetFullPath で正規化する
$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
# \\wsl.localhost\... など UNC 上のフォルダは WSL 側の .venv（Linux 用）と共有されるため、Windows 用の環境は別名にする
$venvName = if ($projectRoot -like "\\*") { ".venv-windows" } else { ".venv" }
Set-Location -LiteralPath $projectRoot

function Finish([int]$code) {
    if (-not $NoPause) {
        Write-Host ""
        Read-Host "Enter キーを押すとこのウィンドウを閉じます" | Out-Null
    }
    exit $code
}

function Fail([string]$message) {
    Write-Host ""
    Write-Host "エラー: $message" -ForegroundColor Red
    Finish 1
}

try {
    $venvPython = Join-Path $projectRoot "$venvName\Scripts\python.exe"
    if (-not (Test-Path $venvPython)) {
        Fail "仮想環境が見つかりません。先に setup.cmd を実行してください。"
    }

    if (-not (Test-Path ".env")) {
        Copy-Item ".env.example" ".env"
        Write-Host ".env がなかったため .env.example から作成しました。" -ForegroundColor Yellow
    }

    if ($projectRoot -like "\\*") {
        # \\wsl.localhost\... などネットワーク上のフォルダでは、ライブラリの読み込みが極端に遅くなる
        Write-Host "注意: ネットワーク上のフォルダ（$projectRoot）から起動しています。起動に数分かかることがあります。Windows のローカルディスクに置くと速く起動します。" -ForegroundColor Yellow
    }

    $env:PYTHONUTF8 = "1"
    # Streamlit は stderr にも出力するため、実行中は Stop にしない
    $ErrorActionPreference = "Continue"
    # 起動後に既定のブラウザで画面を開く。終了はこのウィンドウで Ctrl+C
    & $venvPython -m volumio_playlist_studio.launcher
    $appExit = $LASTEXITCODE
    $ErrorActionPreference = "Stop"
    # Ctrl+C で止めた場合も 0 以外になるため、ログの確認を案内するだけにする
    if ($appExit -ne 0) {
        Write-Host "アプリが終了コード $appExit で停止しました。Ctrl+C で止めた場合は問題ありません。それ以外は上のメッセージと data\logs\app.log を確認してください。" -ForegroundColor Yellow
    }
    Finish 0
} catch {
    Fail "予期しないエラー: $($_.Exception.Message)"
}
