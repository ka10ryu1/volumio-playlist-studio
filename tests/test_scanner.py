import os
import shutil
import sqlite3
import threading
from pathlib import Path

import pytest
from conftest import AudioWriter, sine

from volumio_playlist_studio.jobs import JobControl
from volumio_playlist_studio.scanner import find_audio_files, scan_library
from volumio_playlist_studio.storage.analysis import list_errors
from volumio_playlist_studio.storage.tracks import duplicate_groups, last_scan_at, list_tracks
from volumio_playlist_studio.workflows import run_scan


@pytest.fixture
def library(tmp_path: Path, write_audio: AudioWriter) -> Path:
    root = tmp_path / "Music"
    write_audio(root / "アーティストA" / "アルバム" / "01 朝の曲.flac", sine(2.0), {"title": "朝の曲", "artist": "A"})
    write_audio(
        root / "アーティストA" / "アルバム" / "02 夜の曲.flac", sine(2.0, 330), {"title": "夜の曲", "artist": "A"}
    )
    write_audio(root / "Artist B" / "song.wav", sine(2.0, 550))
    return root


def test_find_audio_files_ignores_hidden_unsupported_and_nas_folders(library: Path) -> None:
    (library / "notes.txt").write_text("not music")
    (library / "._01 朝の曲.flac").write_bytes(b"appledouble")
    (library / "@eaDir").mkdir()
    shutil.copy(library / "Artist B" / "song.wav", library / "@eaDir" / "thumb.wav")
    (library / ".hidden").mkdir()
    shutil.copy(library / "Artist B" / "song.wav", library / ".hidden" / "x.wav")
    shutil.copy(library / "Artist B" / "song.wav", library / "Artist B" / "UPPER.WAV")

    listing = find_audio_files(library)
    names = [p.relative_to(library).as_posix() for p in listing.files]

    assert names == [
        "Artist B/UPPER.WAV",
        "Artist B/song.wav",
        "アーティストA/アルバム/01 朝の曲.flac",
        "アーティストA/アルバム/02 夜の曲.flac",
    ]
    assert listing.unreadable_dirs == []


def test_find_audio_files_rejects_non_directory(tmp_path: Path) -> None:
    with pytest.raises(NotADirectoryError):
        find_audio_files(tmp_path / "missing")


def test_initial_scan_registers_tracks(db: sqlite3.Connection, library: Path) -> None:
    summary = scan_library(db, library)

    assert (summary.added, summary.updated, summary.unchanged, summary.missing, summary.errors) == (3, 0, 0, 0, 0)
    tracks = list_tracks(db, library.as_posix())
    assert [t.rel_path for t in tracks] == [
        "Artist B/song.wav",
        "アーティストA/アルバム/01 朝の曲.flac",
        "アーティストA/アルバム/02 夜の曲.flac",
    ]
    assert tracks[1].title == "朝の曲"
    assert last_scan_at(db, library.as_posix()) is not None


def test_rescan_without_changes_does_not_reread(db: sqlite3.Connection, library: Path) -> None:
    scan_library(db, library)

    summary = scan_library(db, library)

    assert (summary.added, summary.updated, summary.unchanged, summary.missing) == (0, 0, 3, 0)


def test_rescan_detects_added_updated_and_removed_files(
    db: sqlite3.Connection, library: Path, write_audio: AudioWriter
) -> None:
    scan_library(db, library)
    updated = library / "アーティストA" / "アルバム" / "01 朝の曲.flac"
    write_audio(updated, sine(3.0), {"title": "朝の曲（改）", "artist": "A"})
    stat = updated.stat()
    os.utime(updated, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    write_audio(library / "新しい曲.flac", sine(2.0))
    (library / "Artist B" / "song.wav").unlink()

    summary = scan_library(db, library)

    assert (summary.added, summary.updated, summary.unchanged, summary.missing) == (1, 1, 1, 1)
    active = {t.rel_path: t for t in list_tracks(db, library.as_posix())}
    assert active["アーティストA/アルバム/01 朝の曲.flac"].title == "朝の曲（改）"
    assert "Artist B/song.wav" not in active
    everything = {t.rel_path: t.status for t in list_tracks(db, library.as_posix(), include_missing=True)}
    assert everything["Artist B/song.wav"] == "missing"


def test_file_that_reappears_becomes_active_again(db: sqlite3.Connection, library: Path) -> None:
    backup = library.parent / "song.wav.bak"
    shutil.move(library / "Artist B" / "song.wav", backup)
    scan_library(db, library)
    shutil.move(backup, library / "Artist B" / "song.wav")
    scan_library(db, library)
    scan_library(db, library)

    statuses = {t.rel_path: t.status for t in list_tracks(db, library.as_posix(), include_missing=True)}
    assert statuses["Artist B/song.wav"] == "active"


def test_corrupted_file_is_recorded_and_scan_continues(db: sqlite3.Connection, library: Path) -> None:
    (library / "壊れた曲.mp3").write_bytes(b"\x00garbage" * 100)

    summary = scan_library(db, library)

    assert summary.added == 4
    assert summary.errors == 1
    broken = next(t for t in list_tracks(db, library.as_posix()) if t.rel_path == "壊れた曲.mp3")
    assert broken.tag_error is not None
    errors = list_errors(db, library.as_posix())
    assert [(e.stage, e.rel_path) for e in errors] == [("tags", "壊れた曲.mp3")]
    assert all(str(library) not in e.message for e in errors)


def test_unreadable_file_is_recorded_and_scan_continues(db: sqlite3.Connection, library: Path) -> None:
    unreadable = library / "読めない曲.flac"
    shutil.copy(library / "Artist B" / "song.wav", unreadable)
    unreadable.chmod(0)
    if os.access(unreadable, os.R_OK):  # root 権限では読めてしまうため検証できない
        unreadable.chmod(0o644)
        pytest.skip("cannot make file unreadable when running as root")

    try:
        summary = scan_library(db, library)
    finally:
        unreadable.chmod(0o644)

    assert summary.errors == 1
    assert summary.added == 3
    errors = list_errors(db, library.as_posix())
    assert errors[0].error_type == "PermissionError"
    assert "読めない曲.flac" in errors[0].message
    assert str(library) not in errors[0].message


def test_duplicate_files_are_detected(db: sqlite3.Connection, library: Path) -> None:
    shutil.copy(library / "Artist B" / "song.wav", library / "Artist B" / "song (copy).wav")

    scan_library(db, library)

    tracks = {t.id: t.rel_path for t in list_tracks(db, library.as_posix())}
    groups = [[tracks[i] for i in group] for group in duplicate_groups(db, library.as_posix())]
    assert groups == [["Artist B/song (copy).wav", "Artist B/song.wav"]]


def test_cancelled_scan_does_not_mark_files_missing(db: sqlite3.Connection, library: Path) -> None:
    scan_library(db, library)
    cancel = threading.Event()
    cancel.set()

    summary = scan_library(db, library, JobControl(cancel=cancel))

    assert summary.missing == 0
    assert len(list_tracks(db, library.as_posix())) == 3


def test_scan_reports_progress(db: sqlite3.Connection, library: Path) -> None:
    reports: list[tuple[int, int]] = []

    scan_library(db, library, JobControl(progress=lambda done, total: reports.append((done, total))))

    assert reports[-1] == (3, 3)


def test_scan_does_not_modify_music_files(db: sqlite3.Connection, library: Path) -> None:
    before = {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in library.rglob("*") if p.is_file()}

    scan_library(db, library)

    after = {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in library.rglob("*") if p.is_file()}
    assert after == before


def _make_unreadable(path: Path) -> None:
    path.chmod(0)
    if os.access(path, os.R_OK):  # root 権限では読めてしまうため検証できない
        path.chmod(0o755)
        pytest.skip("cannot make directory unreadable when running as root")


def test_unreadable_directory_does_not_mark_its_tracks_missing(db: sqlite3.Connection, library: Path) -> None:
    scan_library(db, library)
    folder = library / "アーティストA"
    _make_unreadable(folder)

    try:
        summary = scan_library(db, library)
        message = run_scan(db, library, JobControl())
    finally:
        folder.chmod(0o755)

    # 一時的に読めないだけかもしれないため、配下の曲を消失扱いにしない
    assert summary.missing == 0
    assert summary.unreadable_dirs == 1
    assert summary.errors == 1
    assert len(list_tracks(db, library.as_posix())) == 3
    errors = [e for e in list_errors(db, library.as_posix()) if e.error_type == "DirectoryReadError"]
    assert errors
    assert errors[0].message == "cannot read directory: アーティストA"
    assert str(library) not in errors[0].message
    assert "判定は行っていません" in message


def test_unreadable_root_does_not_mark_tracks_missing(db: sqlite3.Connection, library: Path) -> None:
    scan_library(db, library)
    _make_unreadable(library)

    try:
        summary = scan_library(db, library)
    finally:
        library.chmod(0o755)

    assert summary.missing == 0
    assert summary.unreadable_dirs == 1
    assert len(list_tracks(db, library.as_posix())) == 3
