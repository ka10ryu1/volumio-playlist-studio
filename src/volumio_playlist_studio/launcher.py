"""アプリの起動処理。start.cmd（Windows）から呼び出し、WSL・Linux では次のように実行する。

    uv run python -m volumio_playlist_studio.launcher

プロジェクト直下の .env を読み込み、Streamlit を起動して既定のブラウザで画面を開く。
同じポートで既に起動している場合は 2 つ目を起動せず、既存の画面をブラウザで開いて終了する
（Windows では同じポートに 2 つのサーバーが同居でき、古い方が応答し続けることがあるため）。
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
from typing import Protocol

from volumio_playlist_studio.config import PROJECT_ROOT, AppConfig, load_app_config, load_env_file
from volumio_playlist_studio.lan_access import lan_ip

logger = logging.getLogger(__name__)

APP_SCRIPT = PROJECT_ROOT / "src" / "volumio_playlist_studio" / "gui" / "app.py"
LOOPBACK = "127.0.0.1"
ALL_INTERFACES = "0.0.0.0"  # noqa: S104 - LAN モードのときだけ、利用者の明示的な設定で使う


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
    args = parser.parse_args(argv)

    load_env_file()
    config = load_app_config()
    local_url = f"http://localhost:{config.port}"
    # LAN モードでは、PC のブラウザにスマホ用の QR コードと PIN を表示する
    open_url = f"{local_url}/lan" if config.lan else local_url

    # Windows では 0.0.0.0 と 127.0.0.1 が互いに衝突しないため、両方を確かめる
    if not all(port_is_free(host, config.port) for host in (ALL_INTERFACES, LOOPBACK)):
        if streamlit_running(config.port):
            _say(f"既に起動しています: {local_url}  （このウィンドウは閉じて構いません）")
            if not args.no_browser:
                webbrowser.open(open_url)

            return 0

        _say(f"ポート {config.port} は別のプログラムが使用中です。.env の VPS_PORT を別の番号にしてください。")
        return 1

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
    return wait_for_streamlit(process)


class _Process(Protocol):
    """終了を待てるプロセス（subprocess.Popen と同じ操作）。"""

    def wait(self, timeout: float | None = None) -> int:
        """終了を待ち、終了コードを返す。"""
        ...

    def terminate(self) -> None:
        """終了させる。"""
        ...


def wait_for_streamlit(process: _Process, shutdown_timeout: float = 15.0) -> int:
    """Streamlit の終了を待つ。Ctrl+C で止めた場合は、正常な終了として扱う。

    端末での Ctrl+C は Streamlit とこのプロセスの両方に届く。Streamlit は自分で停止処理をするため、
    ここでは終了を待つだけにし、割り込みのトレースバックを表示しない。

    Args:
        process: 起動した Streamlit のプロセス。
        shutdown_timeout: Ctrl+C の後、Streamlit の終了を待つ上限秒数。超えたら強制的に終了させる。

    Returns:
        終了コード。Ctrl+C で止めた場合は 0。
    """
    try:
        return process.wait()
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


def _say(message: str) -> None:
    sys.stdout.write(message + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    raise SystemExit(main())
