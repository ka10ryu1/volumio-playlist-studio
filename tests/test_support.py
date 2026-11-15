"""設定、パス変換、バックグラウンド処理、集計、サンプルデータのテスト。"""

import logging
import os
import sqlite3
from pathlib import Path, PureWindowsPath

import pytest
from conftest import ClassificationFactory, TrackFactory

from volumio_playlist_studio.catalog import (
    UNKNOWN_GENRE_LABEL,
    GenreTagCount,
    dashboard_stats,
    effective_classifications,
    genre_tag_counts,
    load_candidates,
)
from volumio_playlist_studio.config import AnalysisParams, is_typesafe_api_key_set, load_app_config, load_env_file
from volumio_playlist_studio.jobs import BackgroundJob, JobControl, JobError, JobState
from volumio_playlist_studio.library_paths import (
    HostPlatform,
    LibraryPathError,
    detect_platform,
    redact_paths,
    resolve_library_input,
)
from volumio_playlist_studio.models import Attribute, ClassificationSource, Genre, TitleSource, TrackMetadata
from volumio_playlist_studio.sample_data import DEMO_LIBRARY_ROOT, generate_sample_audio, load_demo_library, main
from volumio_playlist_studio.scanner import scan_library
from volumio_playlist_studio.storage.analysis import save_classification
from volumio_playlist_studio.storage.database import connect, transaction
from volumio_playlist_studio.storage.evaluations import (
    EvaluationLabel,
    Split,
    Verdict,
    add_to_evaluation_set,
    save_label,
)
from volumio_playlist_studio.storage.tracks import list_tracks, upsert_track


def test_load_app_config_from_environment() -> None:
    config = load_app_config({"VPS_DATA_DIR": " /data/vps ", "VOLUMIO_HOST": "volumio.local"})

    assert config.data_dir == Path("/data/vps")
    assert config.database_path == Path("/data/vps/studio.sqlite3")
    assert config.log_path == Path("/data/vps/logs/app.log")
    assert config.volumio_host == "volumio.local"


def test_load_app_config_defaults_are_relative_to_project_root() -> None:
    # ダブルクリックで起動してもカレントフォルダに依存しないよう、プロジェクト直下を基準にする
    root = Path(__file__).resolve().parents[1]

    config = load_app_config({})
    relative = load_app_config({"VPS_DATA_DIR": "mydata"})

    assert config.data_dir == root / "data"
    assert relative.data_dir == root / "mydata"
    assert config.volumio_host is None
    assert (config.port, config.lan, config.lan_address) == (8501, False, None)


@pytest.mark.parametrize(
    ("env", "port", "lan"),
    [
        ({"VPS_PORT": "8600", "VPS_LAN": "1"}, 8600, True),
        ({"VPS_PORT": " ", "VPS_LAN": "true"}, 8501, True),
        ({"VPS_PORT": "abc", "VPS_LAN": "0"}, 8501, False),
        ({"VPS_PORT": "80", "VPS_LAN": ""}, 8501, False),
        ({"VPS_PORT": "70000"}, 8501, False),
    ],
)
def test_port_and_lan_settings(env: dict[str, str], port: int, lan: bool) -> None:
    config = load_app_config(env)

    assert (config.port, config.lan) == (port, lan)


def test_lan_address_setting() -> None:
    assert load_app_config({"VPS_LAN_ADDRESS": " 192.168.1.10 "}).lan_address == "192.168.1.10"


def test_load_env_file_does_not_override_existing_variables(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("VOLUMIO_HOST=from-file\nVPS_TEST_ONLY=loaded\n", encoding="utf-8")
    monkeypatch.setenv("VOLUMIO_HOST", "from-env")
    monkeypatch.delenv("VPS_TEST_ONLY", raising=False)

    load_env_file(env_file)

    assert os.environ["VOLUMIO_HOST"] == "from-env"
    assert os.environ["VPS_TEST_ONLY"] == "loaded"
    monkeypatch.delenv("VPS_TEST_ONLY")


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
    ("text", "host", "expected"),
    [
        ("/mnt/volumio", HostPlatform.POSIX, "/mnt/volumio"),
        ('  "/home/user/音楽"  ', HostPlatform.WSL, "/home/user/音楽"),
        ("C:\\Users\\me\\Music", HostPlatform.WSL, "/mnt/c/Users/me/Music"),
        ("d:/音楽/", HostPlatform.WSL, "/mnt/d/音楽"),
        # Windows ではドライブのパスも UNC パス（SMB 共有）もそのまま開ける
        ("C:\\Users\\me\\Music", HostPlatform.WINDOWS, "C:\\Users\\me\\Music"),
        ("\\\\volumio.local\\Internal Storage", HostPlatform.WINDOWS, "\\\\volumio.local\\Internal Storage"),
    ],
)
def test_resolve_library_input(text: str, host: HostPlatform, expected: str) -> None:
    assert str(resolve_library_input(text, host=host)) == expected


@pytest.mark.parametrize(
    ("text", "host", "message"),
    [
        ("", HostPlatform.WINDOWS, "入力"),
        ("\\\\volumio\\Internal Storage", HostPlatform.WSL, "マウント"),
        ("//volumio/share", HostPlatform.WSL, "マウント"),
        ("C:\\Music", HostPlatform.POSIX, "Windows"),
        ("relative/path", HostPlatform.POSIX, "絶対パス"),
        ("Music", HostPlatform.WINDOWS, "絶対パス"),
        # ドライブ名だけで区切りのないパスは、そのドライブのカレントフォルダからの相対パス
        ("C:Music", HostPlatform.WINDOWS, "絶対パス"),
    ],
)
def test_resolve_library_input_rejects_unusable_paths(text: str, host: HostPlatform, message: str) -> None:
    with pytest.raises(LibraryPathError) as exc_info:
        resolve_library_input(text, host=host)

    assert message in exc_info.value.user_message


def test_detect_platform_returns_known_value() -> None:
    assert detect_platform() in set(HostPlatform)


def test_redact_paths_handles_both_separators() -> None:
    redacted = redact_paths("Error opening 'C:/Music/a.flac'", [PureWindowsPath("C:\\Music")])

    assert redacted == "Error opening '<library>/a.flac'"


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


def test_background_job_shows_message_of_expected_failure(caplog: pytest.LogCaptureFixture) -> None:
    job = BackgroundJob(name="想定内の失敗")

    def work(control: JobControl) -> str:
        try:
            raise ValueError("cause")
        except ValueError as exc:
            raise JobError("同じ名前のものがあるため、実行しませんでした。") from exc

    with caplog.at_level(logging.WARNING):
        job.start(work)
        job.join(timeout=5)

    assert job.state == JobState.FAILED
    assert job.error == "同じ名前のものがあるため、実行しませんでした。"
    assert "background job failed" in caplog.text
    assert "cause" in caplog.text


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


def _meta(rel_path: str) -> TrackMetadata:
    return TrackMetadata(
        rel_path=rel_path, title="曲", artist=None, album=None, genre=None, year=None, track_number=None,
        duration_sec=200.0, bitrate=None, sample_rate=None, channels=None, codec=None, file_size=100, mtime_ns=1,
        title_source=TitleSource.TAG, tags_repaired=False, quick_hash=None,
    )  # fmt: skip


def test_jev_then_rules_fills_unknown_jev_genre_with_rules_genre(
    db: sqlite3.Connection, make_classification: ClassificationFactory
) -> None:
    # (ルールベース, Jev)。"-" はそのエンジンで未判定
    rows: list[tuple[Genre | str | None, Genre | str | None]] = [
        (Genre.ROCK, None),
        (Genre.ROCK, Genre.JAZZ),
        (None, None),
        (Genre.POP, "-"),
        (None, Genre.POP),
        ("-", None),
    ]
    with transaction(db):
        ids = []
        for i, (rule_genre, jev_genre) in enumerate(rows):
            track_id = upsert_track(db, "/music", _meta(f"{i:02}.flac"), None)
            ids.append(track_id)
            if rule_genre is None or isinstance(rule_genre, Genre):
                save_classification(db, track_id, make_classification(rule_genre, energy=0.1))

            if jev_genre is None or isinstance(jev_genre, Genre):
                save_classification(db, track_id, make_classification(jev_genre, engine="jev", energy=0.9))

    merged = effective_classifications(db, ClassificationSource.JEV_THEN_RULES)

    assert [merged[i].genre if i in merged else "-" for i in ids] == [
        Genre.ROCK,  # Jev が不明なら、ルールベースのジャンルで補う
        Genre.JAZZ,  # Jev のジャンルがあれば Jev を優先する
        None,
        Genre.POP,  # Jev で未判定ならルールベースの結果
        Genre.POP,
        None,
    ]
    # ジャンルを補った曲も、属性は Jev の結果を使い、補ったことを notes に残す
    filled = merged[ids[0]]
    assert (filled.engine, filled.attribute_value(Attribute.ENERGY)) == ("jev", 0.9)
    assert any("ルールベース" in note for note in filled.notes)
    assert all("ルールベース" not in note for note in merged[ids[1]].notes)

    # 「のみ」の設定では補わない
    assert effective_classifications(db, ClassificationSource.JEV)[ids[0]].genre is None
    assert effective_classifications(db, ClassificationSource.RULES)[ids[4]].genre is None
    # 選曲の候補とダッシュボードのジャンルにも反映される
    candidates = {c.track.id: c for c in load_candidates(db, "/music", ClassificationSource.JEV_THEN_RULES)}
    assert candidates[ids[0]].genre == Genre.ROCK
    stats = dashboard_stats(db, "/music", ClassificationSource.JEV_THEN_RULES)
    assert stats.genre_distribution == {"ロック": 1, "ポップ": 2, "ジャズ": 1, UNKNOWN_GENRE_LABEL: 2}


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


def test_genre_tag_counts_groups_tags_and_applies_current_rules(make_track: TrackFactory) -> None:
    tags = ["Rock", " Rock ", "Rock", "邦楽", "邦楽", None, "  ", "Blues"]
    tracks = [make_track(genre=tag) for tag in tags]

    # 曲数の多い順。同数なら表記の順。前後の空白だけが違う表記と、空のタグはそれぞれ 1 つにまとめる
    assert genre_tag_counts(tracks) == [
        GenreTagCount("Rock", Genre.ROCK, 3),
        GenreTagCount("", None, 2),
        GenreTagCount("邦楽", None, 2),
        GenreTagCount("Blues", Genre.RNB_SOUL, 1),
    ]


def test_genre_tag_counts_for_empty_library() -> None:
    assert genre_tag_counts([]) == []
