"""アプリの起動処理。start.cmd（Windows）から呼び出し、WSL・Linux では次のように実行する。

    uv run python -m volumio_playlist_studio.launcher

プロジェクト直下の .env を読み込み、Streamlit を起動して既定のブラウザで画面を開く。
同じポートで既に起動している場合は 2 つ目を起動せず、既存の画面をブラウザで開いて終了する
（Windows では同じポートに 2 つのサーバーが同居でき、古い方が応答し続けることがあるため）。

`--restart`（または `--ask-restart` で確認して同意した場合）では、起動中のアプリを止めてから起動し直す。
Windows では別のウィンドウに Ctrl+C を送りにくいため、データフォルダに停止の依頼のファイルを置き、
起動中の launcher がそれを見つけて自分で Streamlit を止める。
"""

import argparse
import http.client
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from collections.abc import Callable
from http import HTTPStatus
from pathlib import Path
from typing import Protocol

from volumio_playlist_studio.config import PROJECT_ROOT, AppConfig, load_app_config, load_env_file
from volumio_playlist_studio.lan_access import lan_ip

logger = logging.getLogger(__name__)

APP_SCRIPT = PROJECT_ROOT / "src" / "volumio_playlist_studio" / "gui" / "app.py"
LOOPBACK = "127.0.0.1"
ALL_INTERFACES = "0.0.0.0"  # noqa: S104 - LAN モードのときだけ、利用者の明示的な設定で使う
# 再起動の依頼で止まったときの終了コード。start.ps1 はこのコードなら一時停止せずにウィンドウを閉じる
STOPPED_FOR_RESTART_EXIT_CODE = 10


def port_is_free(host: str, port: int) -> bool:
    """そのアドレスとポートで待ち受けているサーバーがないかを、bind できるかで確かめる。

    接続を試す方法では、環境によって空きポートへの接続がタイムアウトになり、使用中と誤判定することがある。
    Windows では SO_REUSEADDR を付けると待ち受け中でも bind できてしまうため、Windows 以外でだけ付ける。

    Args:
        host: bind するアドレス。
        port: ポート番号。
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            if os.name != "nt":
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

            sock.bind((host, port))
    except OSError:
        return False

    return True


def streamlit_running(port: int, host: str = LOOPBACK, timeout: float = 2.0) -> bool:
    """そのポートで Streamlit が応答するかを、ヘルスチェックのエンドポイントで確かめる。"""
    connection = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        connection.request("GET", "/_stcore/health")
        response = connection.getresponse()
        return response.status == HTTPStatus.OK and response.read().strip() == b"ok"
    except OSError, http.client.HTTPException:
        return False
    finally:
        connection.close()


def ports_are_free(port: int) -> bool:
    """そのポートで待ち受けているサーバーがないかを確かめる。

    Windows では 0.0.0.0 と 127.0.0.1 が互いに衝突しないため、両方を確かめる。
    """
    return all(port_is_free(host, port) for host in (ALL_INTERFACES, LOOPBACK))


def stop_request_path(config: AppConfig) -> Path:
    """起動中のアプリに停止を依頼するファイルのパス。ポートごとに分ける。"""
    return config.data_dir / f"stop-request-{config.port}"


def request_stop(port: int, request: Path, timeout: float = 30.0, interval: float = 0.5) -> bool:
    """起動中のアプリに停止を依頼し、ポートが空くまで待つ。

    Args:
        port: 起動中のアプリのポート番号。
        request: 停止の依頼のファイル。起動中の launcher が見つけると削除して停止する。
        timeout: 待つ上限秒数。
        interval: 確認の間隔（秒）。

    Returns:
        時間内に止まったら True。止まらなければ依頼を取り下げて False
        （依頼の仕組みを持たない古いバージョンが起動している場合など）。
    """
    request.parent.mkdir(parents=True, exist_ok=True)
    request.touch()
    logger.info("stop requested: port=%d", port)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ports_are_free(port):
            return True

        time.sleep(interval)

    # 残すと、次に起動したアプリがすぐに止まってしまう
    request.unlink(missing_ok=True)
    logger.warning("stop request timed out: port=%d", port)
    return False


def confirm_restart() -> bool:
    """起動中のアプリを再起動するかを利用者に確かめる。入力できない環境では確かめずに False を返す。"""
    if not sys.stdin.isatty():
        return False

    _say("git pull などで更新した内容を反映するには、再起動してください。")
    _say("解析や Jev の判定を実行している途中で再起動すると、その処理は中断されます。")
    try:
        answer = input("再起動しますか？ [y/N] ")
    except EOFError, KeyboardInterrupt:
        return False

    return answer.strip().lower() in {"y", "yes"}


def build_command(config: AppConfig) -> list[str]:
    """Streamlit を起動するコマンドを組み立てる。"""
    return [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(APP_SCRIPT),
        "--server.port",
        str(config.port),
        "--server.address",
        ALL_INTERFACES if config.lan else LOOPBACK,
        # headless でないと初回起動時にメールアドレスの入力を求められ、ダブルクリックでの起動が止まるため、
        # ブラウザはこのモジュールで開く
        "--server.headless",
        "true",
        "--browser.gatherUsageStats",
        "false",
    ]


def open_when_ready(
    url: str,
    port: int,
    timeout: float = 120.0,
    interval: float = 1.0,
    opener: Callable[[str], object] = webbrowser.open,
) -> bool:
    """Streamlit が応答するまで待ってからブラウザで開く。

    Args:
        url: ブラウザで開く URL。
        port: 応答を確かめるポート番号。
        timeout: 待つ上限秒数。ネットワーク上のフォルダから起動すると、ライブラリの読み込みに時間がかかる。
        interval: 確認の間隔（秒）。
        opener: URL を開く関数（テストで差し替える）。

    Returns:
        ブラウザで開いたら True。時間内に応答がなければ False。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if streamlit_running(port):
            opener(url)
            return True

        time.sleep(interval)

    return False


def main(argv: list[str] | None = None) -> int:
    """設定を読み込んで Streamlit を起動する。

    Returns:
        終了コード。ポートが使えない場合は 1。
    """
    parser = argparse.ArgumentParser(description="Volumio Playlist Studio を起動する")
    parser.add_argument("--no-browser", action="store_true", help="ブラウザを開かない")
    parser.add_argument("--restart", action="store_true", help="起動中なら止めてから起動し直す")
    parser.add_argument(
        "--ask-restart", action="store_true", help="起動中なら再起動するかを確かめる（start.cmd から使う）"
    )
    args = parser.parse_args(argv)

    load_env_file()
    config = load_app_config()
    local_url = f"http://localhost:{config.port}"
    # LAN モードでは、PC のブラウザにスマホ用の QR コードと PIN を表示する
    open_url = f"{local_url}/lan" if config.lan else local_url

    stop_request = stop_request_path(config)
    if not ports_are_free(config.port):
        if not streamlit_running(config.port):
            _say(f"ポート {config.port} は別のプログラムが使用中です。.env の VPS_PORT を別の番号にしてください。")
            return 1

        _say(f"既に起動しています: {local_url}")
        if not (args.restart or (args.ask_restart and confirm_restart())):
            _say("起動中の画面を開きます（このウィンドウは閉じて構いません）。")
            if not args.no_browser:
                webbrowser.open(open_url)

            return 0

        _say("起動中のアプリを停止しています...")
        if not request_stop(config.port, stop_request):
            _say(
                "起動中のアプリを停止できませんでした。「Volumio Playlist Studio」のウィンドウで Ctrl+C を押して"
                "終了してから、もう一度起動してください。"
            )
            return 1

    # 前回の停止の依頼が残っていると、起動直後に止まってしまうため消しておく
    stop_request.unlink(missing_ok=True)

    _say(f"Volumio Playlist Studio を起動します: {local_url}  （終了は Ctrl+C）")
    if config.lan:
        address = config.lan_address or lan_ip()
        if address is None:
            _say("LAN の IP アドレスを取得できませんでした。Wi-Fi への接続を確認してください。")
        else:
            _say(f"スマホから: http://{address}:{config.port}/  （PIN は PC の「スマホで開く」画面に表示）")

    if not args.no_browser:
        threading.Thread(target=open_when_ready, args=(open_url, config.port), daemon=True).start()

    # .streamlit/config.toml を読み込ませるため、プロジェクト直下で実行する
    process = subprocess.Popen(build_command(config), cwd=PROJECT_ROOT)  # noqa: S603 - 引数は固定値と設定値のみ
    return wait_for_streamlit(process, stop_request=stop_request)


class _Process(Protocol):
    """終了を待てるプロセス（subprocess.Popen と同じ操作）。"""

    def wait(self, timeout: float | None = None) -> int:
        """終了を待ち、終了コードを返す。"""
        ...

    def terminate(self) -> None:
        """終了させる。"""
        ...

    def kill(self) -> None:
        """強制的に終了させる。"""
        ...


def wait_for_streamlit(
    process: _Process, shutdown_timeout: float = 15.0, stop_request: Path | None = None, poll_interval: float = 0.5
) -> int:
    """Streamlit の終了を待つ。Ctrl+C で止めた場合は、正常な終了として扱う。

    端末での Ctrl+C は Streamlit とこのプロセスの両方に届く。Streamlit は自分で停止処理をするため、
    ここでは終了を待つだけにし、割り込みのトレースバックを表示しない。

    Args:
        process: 起動した Streamlit のプロセス。
        shutdown_timeout: Streamlit の終了を待つ上限秒数。超えたら強制的に終了させる。
        stop_request: 停止の依頼のファイル。待つ間に現れたら、Streamlit を止めて終了する。
        poll_interval: 停止の依頼を確かめる間隔（秒）。

    Returns:
        終了コード。Ctrl+C で止めた場合は 0、停止の依頼で止めた場合は `STOPPED_FOR_RESTART_EXIT_CODE`。
    """
    try:
        if stop_request is None:
            return process.wait()

        while True:
            try:
                return process.wait(timeout=poll_interval)
            except subprocess.TimeoutExpired:
                if stop_request.exists():
                    return _stop_for_restart(process, stop_request, shutdown_timeout)

    except KeyboardInterrupt:
        try:
            process.wait(timeout=shutdown_timeout)
        except subprocess.TimeoutExpired:
            logger.warning("streamlit did not stop: action=terminate")
            process.terminate()
            process.wait()
        except KeyboardInterrupt:
            # 停止を待つ間にもう一度 Ctrl+C が押された場合は、待たずに終了させる
            process.terminate()
            process.wait()

        _say("Volumio Playlist Studio を終了しました。")
        return 0


def _stop_for_restart(process: _Process, stop_request: Path, shutdown_timeout: float) -> int:
    stop_request.unlink(missing_ok=True)
    logger.info("stopping for restart: action=terminate")
    process.terminate()
    try:
        process.wait(timeout=shutdown_timeout)
    except subprocess.TimeoutExpired:
        logger.warning("streamlit did not stop: action=kill")
        process.kill()
        process.wait()

    _say("再起動のため終了しました。")
    return STOPPED_FOR_RESTART_EXIT_CODE


def _say(message: str) -> None:
    sys.stdout.write(message + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    raise SystemExit(main())
