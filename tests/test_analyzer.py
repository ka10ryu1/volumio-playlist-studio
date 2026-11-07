import os
import sqlite3
import threading
from pathlib import Path

import numpy as np
import pytest
from conftest import AudioWriter, clicks, sine

from volumio_playlist_studio.analyzer import analyze_pending, classify_with_rules
from volumio_playlist_studio.config import ANALYSIS_VERSION, AnalysisParams
from volumio_playlist_studio.jobs import JobControl
from volumio_playlist_studio.scanner import scan_library
from volumio_playlist_studio.storage import settings_store
from volumio_playlist_studio.storage.analysis import (
    list_errors,
    load_classifications,
    load_features,
    tracks_needing_analysis,
)
from volumio_playlist_studio.storage.tracks import list_tracks
from volumio_playlist_studio.workflows import run_analysis, run_scan

PARAMS = AnalysisParams(segment_count=2, segment_seconds=5.0)


@pytest.fixture
def library(tmp_path: Path, write_audio: AudioWriter) -> Path:
    root = tmp_path / "ライブラリ"
    write_audio(root / "静か" / "01 静かな曲.flac", sine(15.0, amplitude=0.05), {"genre": "Ambient"})
    write_audio(root / "元気" / "01 元気な曲.wav", np.clip(sine(15.0) + clicks(15.0, 140), -1, 1))
    return root


def test_analysis_saves_features_and_rule_classification(db: sqlite3.Connection, library: Path) -> None:
    scan_library(db, library)

    summary = analyze_pending(db, library, PARAMS)

    assert (summary.analyzed, summary.failed, summary.unsupported, summary.cancelled) == (2, 0, 0, False)
    assert summary.seconds_per_track is not None
    tracks = list_tracks(db, library.as_posix())
    assert set(load_features(db)) == {t.id for t in tracks}
    rules = load_classifications(db, "rules")
    assert set(rules) == {t.id for t in tracks}
    assert tracks_needing_analysis(db, library.as_posix(), ANALYSIS_VERSION) == []


def test_analyzed_files_are_not_analyzed_again(db: sqlite3.Connection, library: Path) -> None:
    scan_library(db, library)
    analyze_pending(db, library, PARAMS)

    summary = analyze_pending(db, library, PARAMS)

    assert (summary.analyzed, summary.failed) == (0, 0)
    assert summary.seconds_per_track is None


def test_updated_file_is_analyzed_again(db: sqlite3.Connection, library: Path, write_audio: AudioWriter) -> None:
    scan_library(db, library)
    analyze_pending(db, library, PARAMS)
    path = write_audio(library / "静か" / "01 静かな曲.flac", sine(15.0, amplitude=0.5))
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    scan_library(db, library)

    summary = analyze_pending(db, library, PARAMS)

    assert summary.analyzed == 1


def test_broken_silent_and_unsupported_files_do_not_stop_analysis(
    db: sqlite3.Connection, library: Path, write_audio: AudioWriter
) -> None:
    write_audio(library / "無音.flac", np.zeros(22050 * 15, dtype=np.float32))
    (library / "壊れた.flac").write_bytes(b"broken" * 100)
    (library / "形式.m4a").write_bytes(b"m4a" * 100)
    scan_library(db, library)

    summary = analyze_pending(db, library, PARAMS)

    assert (summary.analyzed, summary.failed, summary.unsupported) == (2, 2, 1)
    errors = {e.rel_path: e for e in list_errors(db, library.as_posix()) if e.stage == "features"}
    assert set(errors) == {"無音.flac", "壊れた.flac"}
    assert errors["無音.flac"].error_type == "FeatureExtractionError"
    assert errors["壊れた.flac"].error_type == "LibsndfileError"
    # 外部ライブラリのメッセージに含まれる絶対パスを記録しない
    assert all(str(library) not in e.message for e in errors.values())
    assert "<library>" in errors["壊れた.flac"].message


def test_analysis_can_be_cancelled(db: sqlite3.Connection, library: Path) -> None:
    scan_library(db, library)
    cancel = threading.Event()
    cancel.set()

    summary = analyze_pending(db, library, PARAMS, JobControl(cancel=cancel))

    assert summary.cancelled
    assert summary.analyzed == 0


def test_classify_with_rules_uses_tags_without_features(db: sqlite3.Connection, library: Path) -> None:
    scan_library(db, library)

    summary = classify_with_rules(db, library.as_posix())

    assert summary.classified == 2
    results = {r.result.genre for r in load_classifications(db, "rules").values()}
    assert results == {None, "ambient"}


def test_workflow_messages(db: sqlite3.Connection, library: Path) -> None:
    settings_store.set_analysis_params(db, PARAMS)

    scan_message = run_scan(db, library, JobControl())
    analysis_message = run_analysis(db, library, JobControl())

    assert "追加 2 曲" in scan_message
    assert "解析 2 曲" in analysis_message
    assert "1 曲あたり" in analysis_message


def test_analysis_does_not_modify_music_files(db: sqlite3.Connection, library: Path) -> None:
    before = {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in library.rglob("*") if p.is_file()}
    scan_library(db, library)

    analyze_pending(db, library, PARAMS)

    after = {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in library.rglob("*") if p.is_file()}
    assert after == before
