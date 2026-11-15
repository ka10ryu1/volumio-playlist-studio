"""起動処理（launcher）と Windows 用ランチャーのテスト。"""

import builtins
import io
import socket
import subprocess
import sys
import threading
import time
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


class FakeProcess:
    """Streamlit のプロセスの代わり。wait の結果を順に返す（例外なら送出する）。"""

    def __init__(self, command: list[str], cwd: Path, results: list[int | BaseException]) -> None:
        self.command = command
        self.cwd = cwd
        self.results = results
        self.terminated = False
        self.killed = False
        self.wait_timeouts: list[float | None] = []

    def wait(self, timeout: float | None = None) -> int:
        self.wait_timeouts.append(timeout)
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result

        return result

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


def test_main_starts_streamlit_in_project_root(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    port = _free_port()
    monkeypatch.setenv("VPS_PORT", str(port))
    monkeypatch.setenv("VPS_LAN", "1")
    monkeypatch.setenv("VPS_LAN_ADDRESS", "192.168.1.10")
    processes: list[FakeProcess] = []

    # Streamlit のプロセスは起動せず、渡されたコマンドだけを記録する（外部プロセスの境界）
    def fake_popen(command: list[str], cwd: Path) -> FakeProcess:
        processes.append(FakeProcess(command, cwd, [3]))
        return processes[-1]

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    assert launcher.main(["--no-browser"]) == 3

    [process] = processes
    assert process.cwd == ROOT
    assert process.command[process.command.index("--server.port") + 1] == str(port)
    assert process.command[process.command.index("--server.address") + 1] == launcher.ALL_INTERFACES
    assert f"http://192.168.1.10:{port}/" in capsys.readouterr().out


def test_ctrl_c_waits_for_streamlit_and_exits_normally(capsys: pytest.CaptureFixture[str]) -> None:
    process = FakeProcess([], ROOT, [KeyboardInterrupt(), -2])

    assert launcher.wait_for_streamlit(process, shutdown_timeout=5) == 0

    assert process.wait_timeouts == [None, 5]
    assert not process.terminated
    assert "終了しました" in capsys.readouterr().out


@pytest.mark.parametrize("second", [subprocess.TimeoutExpired("streamlit", 5), KeyboardInterrupt()])
def test_streamlit_is_terminated_if_it_does_not_stop(second: BaseException) -> None:
    process = FakeProcess([], ROOT, [KeyboardInterrupt(), second, -15])

    assert launcher.wait_for_streamlit(process, shutdown_timeout=5) == 0

    assert process.terminated


# --- 再起動 ----------------------------------------------------------------------------


def _stop_request(tmp_path: Path, port: int) -> Path:
    # conftest の VPS_DATA_DIR に合わせる
    return tmp_path / "app-data" / f"stop-request-{port}"


def _record_popen(monkeypatch: pytest.MonkeyPatch, results: list[int | BaseException]) -> list[FakeProcess]:
    processes: list[FakeProcess] = []

    # Streamlit のプロセスは起動せず、渡されたコマンドだけを記録する（外部プロセスの境界）
    def fake_popen(command: list[str], cwd: Path) -> FakeProcess:
        processes.append(FakeProcess(command, cwd, list(results)))
        return processes[-1]

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    return processes


def test_restart_stops_running_app_and_starts_again(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    server, port = _serve(b"ok")
    request = _stop_request(tmp_path, port)
    monkeypatch.setenv("VPS_PORT", str(port))
    processes = _record_popen(monkeypatch, [0])

    # 起動中の launcher の代わりに、停止の依頼を見つけたら依頼を消してサーバーを止める
    def running_app() -> None:
        deadline = time.monotonic() + 10
        while not request.exists() and time.monotonic() < deadline:
            time.sleep(0.05)

        request.unlink()
        server.shutdown()
        server.server_close()

    watcher = threading.Thread(target=running_app)
    watcher.start()
    code = launcher.main(["--no-browser", "--restart"])
    watcher.join()

    assert code == 0
    assert len(processes) == 1
    assert "停止しています" in capsys.readouterr().out


def test_stop_request_is_withdrawn_if_running_app_does_not_stop(fake_streamlit: int, tmp_path: Path) -> None:
    request = tmp_path / "stop-request"

    assert not launcher.request_stop(fake_streamlit, request, timeout=0.3, interval=0.1)

    # 残すと、次に起動したアプリがすぐに止まってしまう
    assert not request.exists()


class _Terminal(io.StringIO):
    def isatty(self) -> bool:
        return True


@pytest.mark.parametrize(("answer", "expected"), [("y", True), ("Yes", True), ("", False), ("n", False)])
def test_confirm_restart_asks_user(monkeypatch: pytest.MonkeyPatch, answer: str, expected: bool) -> None:
    monkeypatch.setattr(sys, "stdin", _Terminal())
    monkeypatch.setattr(builtins, "input", lambda prompt: answer)

    assert launcher.confirm_restart() is expected


def test_confirm_restart_does_not_ask_without_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(prompt: str) -> str:
        raise AssertionError("入力できない環境で確認してはいけない")

    monkeypatch.setattr(sys, "stdin", io.StringIO())
    monkeypatch.setattr(builtins, "input", fail)

    assert not launcher.confirm_restart()


def test_declining_restart_keeps_running_app(
    monkeypatch: pytest.MonkeyPatch, fake_streamlit: int, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("VPS_PORT", str(fake_streamlit))
    monkeypatch.setattr(sys, "stdin", _Terminal())
    monkeypatch.setattr(builtins, "input", lambda prompt: "")
    processes = _record_popen(monkeypatch, [0])

    assert launcher.main(["--no-browser", "--ask-restart"]) == 0

    assert processes == []
    assert not _stop_request(tmp_path, fake_streamlit).exists()
    assert "起動中の画面を開きます" in capsys.readouterr().out
    assert launcher.streamlit_running(fake_streamlit)


def test_leftover_stop_request_is_removed_before_start(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    port = _free_port()
    monkeypatch.setenv("VPS_PORT", str(port))
    request = _stop_request(tmp_path, port)
    request.parent.mkdir(parents=True)
    request.touch()
    processes = _record_popen(monkeypatch, [subprocess.TimeoutExpired("streamlit", 0.5), 0])

    # 前回の依頼が残っていても、起動直後に止めない
    assert launcher.main(["--no-browser"]) == 0
    assert not processes[0].terminated
    assert not request.exists()


def test_stop_request_stops_streamlit(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    request = tmp_path / "stop-request"
    request.touch()
    process = FakeProcess([], ROOT, [subprocess.TimeoutExpired("streamlit", 0.5), -15])

    code = launcher.wait_for_streamlit(process, shutdown_timeout=5, stop_request=request)

    assert code == launcher.STOPPED_FOR_RESTART_EXIT_CODE
    assert process.terminated
    assert not process.killed
    assert not request.exists()
    assert "再起動のため終了しました" in capsys.readouterr().out


def test_streamlit_is_killed_if_it_does_not_stop_on_request(tmp_path: Path) -> None:
    request = tmp_path / "stop-request"
    request.touch()
    timeout = subprocess.TimeoutExpired("streamlit", 0.5)
    process = FakeProcess([], ROOT, [timeout, timeout, -9])

    assert launcher.wait_for_streamlit(process, stop_request=request) == launcher.STOPPED_FOR_RESTART_EXIT_CODE
    assert process.killed


def test_streamlit_exit_code_is_returned_while_watching_stop_request(tmp_path: Path) -> None:
    process = FakeProcess([], ROOT, [subprocess.TimeoutExpired("streamlit", 0.5), 2])

    assert launcher.wait_for_streamlit(process, stop_request=tmp_path / "stop-request") == 2
    assert not process.terminated


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
    assert '$launcherArgs = @("-m", "volumio_playlist_studio.launcher")' in start
    assert 'if ($NoBrowser) { $launcherArgs += "--no-browser" }' in start
    # ダブルクリックで起動したときだけ、起動中なら再起動するかを確かめる
    assert 'if (-not $NoPause) { $launcherArgs += "--ask-restart" }' in start
    # 別のウィンドウから再起動されたら、一時停止せずにウィンドウを閉じる
    assert f"if ($appExit -eq {launcher.STOPPED_FOR_RESTART_EXIT_CODE}) {{ exit 0 }}" in start
    assert "uv run" not in start


def test_windows_ci_runs_setup_and_start_check() -> None:
    script = ROOT / "tests" / "windows" / "test_setup_and_start.ps1"
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    # Windows PowerShell 5.1 で日本語を正しく読めるよう、BOM 付き UTF-8 で保存する
    assert script.read_bytes().startswith(b"\xef\xbb\xbf")
    implementation = script.read_text(encoding="utf-8-sig")
    assert "scripts\\windows\\setup.ps1" in implementation
    assert '-m", "volumio_playlist_studio.launcher"' in implementation
    assert '"--no-browser", "--restart"' in implementation
    assert f"$process.ExitCode -eq {launcher.STOPPED_FOR_RESTART_EXIT_CODE}" in implementation
    assert "runs-on: windows-latest" in workflow
    assert "shell: powershell" in workflow
    assert ".\\tests\\windows\\test_setup_and_start.ps1" in workflow


def test_windows_ci_runs_start_cmd_and_ctrl_c_check() -> None:
    # start.cmd の動作（すぐに終了し、別のウィンドウのアプリが Ctrl+C で確認なしに止まる）は、
    # Windows の CI で実際に start.cmd を実行して確認する
    script = ROOT / "tests" / "windows" / "test_start_cmd.ps1"
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert script.read_bytes().startswith(b"\xef\xbb\xbf")
    implementation = script.read_text(encoding="utf-8-sig")
    assert "start.cmd -NoPause -NoBrowser" in implementation
    assert "GenerateConsoleCtrlEvent" in implementation
    # セットアップの後に実行する
    assert workflow.index(".\\tests\\windows\\test_setup_and_start.ps1") < workflow.index(
        ".\\tests\\windows\\test_start_cmd.ps1"
    )
