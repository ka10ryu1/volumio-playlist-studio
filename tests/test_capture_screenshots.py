import importlib.util
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from volumio_playlist_studio.classify.rules import ENGINE_NAME as RULES_ENGINE
from volumio_playlist_studio.config import PROJECT_ROOT
from volumio_playlist_studio.models import Genre, Mood
from volumio_playlist_studio.sample_data import DEMO_LIBRARY_ROOT
from volumio_playlist_studio.storage import analysis as analysis_store
from volumio_playlist_studio.storage import settings_store
from volumio_playlist_studio.storage.database import connect
from volumio_playlist_studio.storage.evaluations import Verdict, load_labels
from volumio_playlist_studio.storage.tracks import list_tracks


def _load_tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "capture_screenshots", PROJECT_ROOT / "tools" / "capture_screenshots.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load tools/capture_screenshots.py")

    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


tool = _load_tool()


def test_prepare_database_registers_demo_library_and_evaluations(tmp_path: Path) -> None:
    database = tmp_path / "studio.sqlite3"
    tool.prepare_database(database)

    conn = connect(database)
    try:
        assert settings_store.get_str(conn, settings_store.MUSIC_ROOT_KEY) == DEMO_LIBRARY_ROOT
        assert len(list_tracks(conn, DEMO_LIBRARY_ROOT)) == 60

        labels = load_labels(conn)
        evaluated = [lb for lb in labels.values() if lb.is_evaluated]
        # 評価対象は既定の 40 曲。一部だけ評価済みにし、未評価の曲も残す
        assert len(labels) == 40
        assert len(evaluated) == 24

        predictions = analysis_store.load_classifications(conn, RULES_ENGINE)
        verdicts = set()
        for label in evaluated:
            predicted = predictions[label.track_id].result
            verdicts.add(label.genre_verdict)
            if label.genre_verdict == Verdict.CORRECT:
                assert label.genre_label == predicted.genre
            elif label.genre_verdict == Verdict.INCORRECT:
                assert label.genre_label is not None
                assert label.genre_label != predicted.genre

            if label.mood_verdict == Verdict.CORRECT:
                assert label.mood_label == predicted.mood
            elif label.mood_verdict == Verdict.INCORRECT:
                assert label.mood_label not in {None, predicted.mood, Mood.OMAKASE}

        # 指標と誤判定の傾向が画面に出るよう、正解と誤りの両方を含む
        assert {Verdict.CORRECT, Verdict.INCORRECT} <= verdicts
    finally:
        conn.close()


def test_prepare_database_fails_on_existing_non_database_file(tmp_path: Path) -> None:
    broken = tmp_path / "studio.sqlite3"
    broken.write_bytes(b"not a sqlite database")

    with pytest.raises(sqlite3.DatabaseError):
        tool.prepare_database(broken)


@pytest.mark.parametrize(
    ("index", "predicted", "expected"),
    [
        (0, Genre.JAZZ, (Verdict.CORRECT, Genre.JAZZ)),
        (3, Genre.JAZZ, (Verdict.INCORRECT, Genre.AMBIENT)),
        (7, Genre.AMBIENT, (Verdict.INCORRECT, Genre.JAZZ)),
        (3, None, (Verdict.PENDING, None)),
    ],
)
def test_verdict_marks_every_nth_track_incorrect(
    index: int, predicted: Genre | None, expected: tuple[Verdict, Genre | None]
) -> None:
    assert tool._verdict(index, 4, predicted, [Genre.AMBIENT, Genre.JAZZ]) == expected


class FakeProcess:
    """起動した Streamlit の代わり。終了処理が呼ばれたかを記録する。"""

    def __init__(self, exit_code: int | None = None) -> None:
        self.exit_code = exit_code
        self.returncode = exit_code
        self.terminated = False

    def poll(self) -> int | None:
        return self.exit_code

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: float | None = None) -> int:
        return 0


@pytest.fixture
def launched(monkeypatch: pytest.MonkeyPatch) -> list[tuple[list[str], dict[str, object]]]:
    """起動コマンドの記録先。起動待ちの sleep は省く。"""
    calls: list[tuple[list[str], dict[str, object]]] = []
    monkeypatch.setattr(tool.time, "sleep", lambda _seconds: None)
    return calls


def _fake_popen(
    monkeypatch: pytest.MonkeyPatch, calls: list[tuple[list[str], dict[str, object]]], process: FakeProcess
) -> None:
    def fake(command: list[str], **kwargs: object) -> FakeProcess:
        calls.append((command, kwargs))
        return process

    monkeypatch.setattr(subprocess, "Popen", fake)


def test_run_app_ignores_user_connection_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, launched: list[tuple[list[str], dict[str, object]]]
) -> None:
    # 利用者の環境変数（.env から読まれたものを含む）が設定されていても、撮影用の起動では使わない
    monkeypatch.setenv("TYPESAFE_API_KEY", "user-secret")
    monkeypatch.setenv("VOLUMIO_HOST", "volumio.local")
    monkeypatch.setenv("VPS_LAN", "1")
    monkeypatch.setenv("VPS_LAN_ADDRESS", "192.168.1.10")
    monkeypatch.setenv("VPS_DATA_DIR", "/somewhere/user-data")
    monkeypatch.setattr(tool, "streamlit_running", lambda _port: True)
    process = FakeProcess()
    _fake_popen(monkeypatch, launched, process)

    with tool.run_app(tmp_path) as url:
        assert url.startswith("http://127.0.0.1:")
        assert not process.terminated

    assert process.terminated
    [(command, kwargs)] = launched
    env = kwargs["env"]
    assert isinstance(env, dict)
    assert env["TYPESAFE_API_KEY"] == ""
    assert env["VOLUMIO_HOST"] == ""
    assert env["VPS_LAN"] == ""
    assert env["VPS_LAN_ADDRESS"] == ""
    assert env["VPS_DATA_DIR"] == str(tmp_path)
    assert url.endswith(f":{env['VPS_PORT']}")
    assert kwargs["cwd"] == PROJECT_ROOT
    # LAN 設定を読まないため、この PC からだけ接続できる
    assert command[command.index("--server.address") + 1] == "127.0.0.1"


def test_run_app_terminates_streamlit_when_capture_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, launched: list[tuple[list[str], dict[str, object]]]
) -> None:
    monkeypatch.setattr(tool, "streamlit_running", lambda _port: True)
    process = FakeProcess()
    _fake_popen(monkeypatch, launched, process)

    with pytest.raises(RuntimeError, match="capture failed"), tool.run_app(tmp_path):
        raise RuntimeError("capture failed")

    assert process.terminated


def test_run_app_fails_when_streamlit_exits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, launched: list[tuple[list[str], dict[str, object]]]
) -> None:
    monkeypatch.setattr(tool, "streamlit_running", lambda _port: False)
    process = FakeProcess(exit_code=1)
    _fake_popen(monkeypatch, launched, process)

    with pytest.raises(RuntimeError, match="code=1"), tool.run_app(tmp_path):
        pytest.fail("streamlit should not be reported as started")

    assert process.terminated


def test_run_app_gives_up_when_streamlit_does_not_respond(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, launched: list[tuple[list[str], dict[str, object]]]
) -> None:
    monkeypatch.setattr(tool, "streamlit_running", lambda _port: False)
    monkeypatch.setattr(tool, "STARTUP_TIMEOUT_SEC", -1.0)
    process = FakeProcess()
    _fake_popen(monkeypatch, launched, process)

    with pytest.raises(TimeoutError), tool.run_app(tmp_path):
        pytest.fail("streamlit should not be reported as started")

    assert process.terminated
