"""設定、パス変換、バックグラウンド処理、集計、サンプルデータのテスト。"""

import logging
import sqlite3
from pathlib import Path

import pytest

from volumio_playlist_studio.catalog import UNKNOWN_GENRE_LABEL, dashboard_stats, load_candidates
from volumio_playlist_studio.config import AnalysisParams, is_typesafe_api_key_set, load_app_config
from volumio_playlist_studio.jobs import BackgroundJob, JobControl, JobState
from volumio_playlist_studio.library_paths import LibraryPathError, redact_paths, resolve_library_input
from volumio_playlist_studio.models import ClassificationSource, Genre
from volumio_playlist_studio.sample_data import DEMO_LIBRARY_ROOT, generate_sample_audio, load_demo_library, main
from volumio_playlist_studio.scanner import scan_library
from volumio_playlist_studio.storage.database import connect
from volumio_playlist_studio.storage.evaluations import (
    EvaluationLabel,
    Split,
    Verdict,
    add_to_evaluation_set,
    save_label,
)
from volumio_playlist_studio.storage.tracks import list_tracks


def test_load_app_config_from_environment() -> None:
    config = load_app_config({"VPS_DATA_DIR": " /data/vps ", "VOLUMIO_HOST": "volumio.local"})

    assert config.data_dir == Path("/data/vps")
    assert config.database_path == Path("/data/vps/studio.sqlite3")
    assert config.log_path == Path("/data/vps/logs/app.log")
    assert config.volumio_host == "volumio.local"


def test_load_app_config_defaults() -> None:
    config = load_app_config({})

    assert config.data_dir == Path("data")
    assert config.volumio_host is None


@pytest.mark.parametrize(("value", "expected"), [("key", True), ("  ", False), ("", False)])
def test_api_key_presence(value: str, expected: bool) -> None:
    assert is_typesafe_api_key_set({"TYPESAFE_API_KEY": value}) is expected


@pytest.mark.parametrize(
    "params",
    [
        AnalysisParams(sample_rate=4000),
        AnalysisParams(segment_count=11),
        AnalysisParams(segment_seconds=1.0),
        AnalysisParams(edge_margin_ratio=0.5),
        AnalysisParams(silence_threshold_db=0.0),
    ],
)
def test_invalid_analysis_params(params: AnalysisParams) -> None:
    with pytest.raises(ValueError, match="must be between"):
        params.validate()


@pytest.mark.parametrize(
    ("text", "wsl", "expected"),
    [
        ("/mnt/volumio", False, Path("/mnt/volumio")),
        ('  "/home/user/音楽"  ', False, Path("/home/user/音楽")),
        ("C:\\Users\\me\\Music", True, Path("/mnt/c/Users/me/Music")),
        ("d:/音楽/", True, Path("/mnt/d/音楽")),
    ],
)
def test_resolve_library_input(text: str, wsl: bool, expected: Path) -> None:
    assert resolve_library_input(text, wsl=wsl) == expected


@pytest.mark.parametrize(
    ("text", "wsl", "message"),
    [
        ("", False, "入力"),
        ("\\\\volumio\\Internal Storage", True, "マウント"),
        ("//volumio/share", True, "マウント"),
        ("C:\\Music", False, "WSL"),
        ("relative/path", False, "絶対パス"),
    ],
)
def test_resolve_library_input_rejects_unusable_paths(text: str, wsl: bool, message: str) -> None:
    with pytest.raises(LibraryPathError) as exc_info:
        resolve_library_input(text, wsl=wsl)

    assert message in exc_info.value.user_message


def test_redact_paths_removes_library_root_and_home() -> None:
    home = str(Path.home())
    message = f"Error opening '/mnt/music/a/b.flac' and '{home}/secret.txt' under /mnt/music2"

    redacted = redact_paths(message, [Path("/mnt/music"), Path("/")])

    assert "/mnt/music/" not in redacted
    assert "<library>/a/b.flac" in redacted
    assert home not in redacted
    assert "~/secret.txt" in redacted


def test_background_job_reports_result_and_progress() -> None:
    job = BackgroundJob(name="テスト")

    def work(control: JobControl) -> str:
        control.report(1, 2)
        control.report(2, 2)
        return "完了"

    job.start(work)
    job.join(timeout=5)

    assert job.state == JobState.SUCCEEDED
    assert job.result == "完了"
    assert job.fraction == 1.0


def test_background_job_failure_is_logged_and_reported(caplog: pytest.LogCaptureFixture) -> None:
    job = BackgroundJob(name="失敗する処理")

    def work(control: JobControl) -> str:
        raise RuntimeError("boom")

    with caplog.at_level(logging.ERROR):
        job.start(work)
        job.join(timeout=5)

    assert job.state == JobState.FAILED
    assert job.error is not None
    assert "ログ" in job.error
    assert "background job failed" in caplog.text


def test_background_job_can_be_cancelled() -> None:
    job = BackgroundJob(name="中断")
    job.cancel_event.set()

    assert job.control.is_cancelled()
    assert job.fraction == 0.0
    assert not JobControl().is_cancelled()


def test_demo_library_and_dashboard(db: sqlite3.Connection) -> None:
    count = load_demo_library(db)
    assert load_demo_library(db) == count  # 2 回読み込んでも重複しない

    stats = dashboard_stats(db, DEMO_LIBRARY_ROOT, ClassificationSource.JEV_THEN_RULES)

    assert stats.total == count == 60
    assert (stats.analyzed, stats.unanalyzed, stats.unsupported, stats.jev_classified) == (60, 0, 0, 0)
    assert stats.last_scan_at is not None
    assert sum(stats.genre_distribution.values()) == 60
    assert stats.genre_distribution[UNKNOWN_GENRE_LABEL] == 6  # ジャンルタグのない架空のアーティスト


def test_candidates_prefer_human_corrected_genre(db: sqlite3.Connection) -> None:
    load_demo_library(db)
    track = list_tracks(db, DEMO_LIBRARY_ROOT)[0]
    add_to_evaluation_set(db, {track.id: Split.TUNING})
    save_label(db, EvaluationLabel(track.id, Split.TUNING, genre_verdict=Verdict.INCORRECT, genre_label=Genre.WORLD))

    candidates = {c.track.id: c for c in load_candidates(db, DEMO_LIBRARY_ROOT, ClassificationSource.RULES)}

    assert candidates[track.id].genre == Genre.WORLD
    assert all(c.classification is not None for c in candidates.values())
    assert all(c.classification is None for c in load_candidates(db, DEMO_LIBRARY_ROOT, ClassificationSource.JEV))


def test_generated_sample_audio_can_be_scanned(db: sqlite3.Connection, tmp_path: Path) -> None:
    paths = generate_sample_audio(tmp_path / "samples", seconds=3.0)

    summary = scan_library(db, tmp_path / "samples")

    assert summary.added == len(paths) == 4
    assert {t.artist for t in list_tracks(db, (tmp_path / "samples").as_posix())} == {
        "架空の静かな楽団",
        "Demo Beat Makers",
    }


def test_sample_audio_is_not_written_into_non_empty_folder(tmp_path: Path) -> None:
    (tmp_path / "existing.flac").write_bytes(b"user data")

    with pytest.raises(FileExistsError):
        generate_sample_audio(tmp_path)

    assert [p.name for p in tmp_path.iterdir()] == ["existing.flac"]


def test_sample_data_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["audio", str(tmp_path / "audio")]) == 0
    assert main(["demo", str(tmp_path / "demo.sqlite3")]) == 0

    output = capsys.readouterr().out
    assert "4 個のサンプル音声" in output
    assert "60 曲" in output
    conn = connect(tmp_path / "demo.sqlite3")
    assert len(list_tracks(conn, DEMO_LIBRARY_ROOT)) == 60
    conn.close()
