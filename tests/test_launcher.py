"""起動処理（launcher）と Windows 用ランチャーのテスト。"""

import socket
import subprocess
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from volumio_playlist_studio import launcher
from volumio_playlist_studio.config import AppConfig

ROOT = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _serve(body: bytes) -> tuple[HTTPServer, int]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200 if self.path == "/_stcore/health" else 404)
            self.end_headers()
            self.wfile.write(body)

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, int(server.server_address[1])


@pytest.fixture
def fake_streamlit() -> Iterator[int]:
    server, port = _serve(b"ok")
    yield port
    server.shutdown()
    server.server_close()


@pytest.fixture
def other_program() -> Iterator[int]:
    server, port = _serve(b"<html>not streamlit</html>")
    yield port
    server.shutdown()
    server.server_close()


def test_port_is_free_detects_listening_server(fake_streamlit: int) -> None:
    assert not launcher.port_is_free("127.0.0.1", fake_streamlit)
    assert launcher.port_is_free("127.0.0.1", _free_port())


def test_streamlit_running(fake_streamlit: int, other_program: int) -> None:
    assert launcher.streamlit_running(fake_streamlit)
    assert not launcher.streamlit_running(other_program)
    assert not launcher.streamlit_running(_free_port(), timeout=0.5)


@pytest.mark.parametrize(("lan", "address"), [(False, launcher.LOOPBACK), (True, launcher.ALL_INTERFACES)])
def test_build_command(lan: bool, address: str) -> None:
    command = launcher.build_command(AppConfig(data_dir=Path("data"), port=8600, lan=lan))

    assert command[1:5] == ["-m", "streamlit", "run", str(ROOT / "src" / "volumio_playlist_studio" / "gui" / "app.py")]
    assert command[command.index("--server.port") + 1] == "8600"
    assert command[command.index("--server.address") + 1] == address
    assert command[command.index("--server.headless") + 1] == "true"


def test_open_when_ready_opens_browser_after_server_responds(fake_streamlit: int) -> None:
    opened: list[str] = []

    assert launcher.open_when_ready("http://localhost/x", fake_streamlit, timeout=5, opener=opened.append)
    assert opened == ["http://localhost/x"]


def test_open_when_ready_gives_up_after_timeout() -> None:
    opened: list[str] = []

    assert not launcher.open_when_ready(
        "http://localhost/", _free_port(), timeout=0.3, interval=0.1, opener=opened.append
    )
    assert opened == []


def test_main_does_not_start_twice(
    monkeypatch: pytest.MonkeyPatch, fake_streamlit: int, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("VPS_PORT", str(fake_streamlit))

    assert launcher.main(["--no-browser"]) == 0
    assert "既に起動しています" in capsys.readouterr().out


def test_main_refuses_port_used_by_other_program(
    monkeypatch: pytest.MonkeyPatch, other_program: int, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("VPS_PORT", str(other_program))

    assert launcher.main(["--no-browser"]) == 1
    assert "VPS_PORT" in capsys.readouterr().out


def test_main_starts_streamlit_in_project_root(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    port = _free_port()
    monkeypatch.setenv("VPS_PORT", str(port))
    monkeypatch.setenv("VPS_LAN", "1")
    monkeypatch.setenv("VPS_LAN_ADDRESS", "192.168.1.10")
    calls: list[tuple[list[str], Path]] = []

    # Streamlit のプロセスは起動せず、渡されたコマンドだけを記録する（外部プロセスの境界）
    def fake_run(command: list[str], cwd: Path, check: bool) -> subprocess.CompletedProcess[bytes]:
        calls.append((command, cwd))
        return subprocess.CompletedProcess(command, 3)

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert launcher.main(["--no-browser"]) == 3

    [(command, cwd)] = calls
    assert cwd == ROOT
    assert command[command.index("--server.port") + 1] == str(port)
    assert command[command.index("--server.address") + 1] == launcher.ALL_INTERFACES
    assert f"http://192.168.1.10:{port}/" in capsys.readouterr().out


@pytest.mark.parametrize(("launcher_name", "script_name"), [("setup.cmd", "setup.ps1"), ("start.cmd", "start.ps1")])
def test_windows_launcher_calls_internal_powershell(launcher_name: str, script_name: str) -> None:
    command = (ROOT / launcher_name).read_bytes()
    script = (ROOT / "scripts" / "windows" / script_name).read_bytes()

    text = command.decode("ascii")
    assert f"%~dp0scripts\\windows\\{script_name}" in text
    assert b"\r\n" in command
    # Windows PowerShell 5.1 で日本語を正しく読めるよう、BOM 付き UTF-8 で保存する
    assert script.startswith(b"\xef\xbb\xbf")
    implementation = script.decode("utf-8-sig")
    assert 'Join-Path $PSScriptRoot "..\\.."' in implementation
    assert "Set-Location -LiteralPath $projectRoot" in implementation
    # WSL 上のフォルダ（UNC）では WSL 側の .venv と衝突しないよう、別名の仮想環境を使う
    assert '$venvName = if ($projectRoot -like "\\\\*") { ".venv-windows" } else { ".venv" }' in implementation


def test_setup_uses_locked_uv_environment() -> None:
    setup = (ROOT / "scripts" / "windows" / "setup.ps1").read_text(encoding="utf-8-sig")

    assert "sync --locked --no-dev" in setup
    assert "$env:UV_PROJECT_ENVIRONMENT = Join-Path $projectRoot $venvName" in setup
    assert "install --id=astral-sh.uv -e" in setup
    assert "pip install" not in setup
    assert ".venv-windows/" in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()


def test_start_runs_launcher_with_synced_venv() -> None:
    start = (ROOT / "scripts" / "windows" / "start.ps1").read_text(encoding="utf-8-sig")

    assert 'Join-Path $projectRoot "$venvName\\Scripts\\python.exe"' in start
    assert "-m volumio_playlist_studio.launcher" in start
    assert "uv run" not in start
