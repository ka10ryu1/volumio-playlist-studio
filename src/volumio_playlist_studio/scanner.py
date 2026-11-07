"""音楽フォルダを再帰的にスキャンし、楽曲メタデータを DB に登録する。

ファイルサイズと更新日時が前回と同じファイルはタグを読み直さない（差分スキャン）。
見つからなくなったファイルは削除せず `missing` にし、評価データを残す。
"""

import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from volumio_playlist_studio.config import SUPPORTED_EXTENSIONS
from volumio_playlist_studio.jobs import JobControl
from volumio_playlist_studio.library_paths import redact_paths, to_library_relative_path
from volumio_playlist_studio.storage import tracks as track_store
from volumio_playlist_studio.storage.analysis import ErrorRecord, record_error
from volumio_playlist_studio.storage.database import transaction, utc_now
from volumio_playlist_studio.tags import read_metadata

logger = logging.getLogger(__name__)

# 何ファイルごとにコミットするか。GUI から途中経過を読めるようにする
_COMMIT_INTERVAL = 50
# Synology などの NAS が作る管理用フォルダ
_IGNORED_DIRS = frozenset({"@eaDir", "#recycle", "$RECYCLE.BIN", "System Volume Information"})


@dataclass
class _Counters:
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    errors: int = 0
    seen: set[str] = field(default_factory=set)


def find_audio_files(root: Path) -> list[Path]:
    """対応する拡張子の楽曲ファイルを再帰的に探し、パス順に返す。

    隠しファイル（macOS が SMB 共有に作る `._` ファイルなど）と NAS の管理用フォルダは除外する。
    シンボリックリンクのフォルダはたどらない（ループとライブラリ外への到達を防ぐ）。

    Raises:
        NotADirectoryError: root がフォルダでない場合。
    """
    if not root.is_dir():
        raise NotADirectoryError("library root is not a directory")

    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in _IGNORED_DIRS)
        found.extend(
            Path(dirpath, name)
            for name in sorted(filenames)
            if not name.startswith(".") and Path(name).suffix.lower() in SUPPORTED_EXTENSIONS
        )

    return found


def scan_library(
    conn: sqlite3.Connection,
    root: Path,
    control: JobControl | None = None,
) -> track_store.ScanSummary:
    """音楽フォルダをスキャンし、追加・更新・消失を DB に反映する。

    個々のファイルの読み取りに失敗しても記録して処理を続ける。

    Args:
        conn: DB 接続。
        root: 音楽ライブラリのルート（絶対パス）。
        control: 進捗通知と中断の受け付け。中断時は消失の判定を行わない。

    Returns:
        スキャン結果の集計。

    Raises:
        NotADirectoryError: root がフォルダでない場合。
    """
    control = control or JobControl()
    started = time.monotonic()
    started_at = utc_now()
    library_root = root.as_posix()
    files = find_audio_files(root)
    snapshots = track_store.load_snapshots(conn, library_root)
    counters = _Counters()
    logger.info("scan started: files=%d", len(files))

    cancelled = False
    for start in range(0, len(files), _COMMIT_INTERVAL):
        if control.is_cancelled():
            cancelled = True
            break

        with transaction(conn):
            for path in files[start : start + _COMMIT_INTERVAL]:
                _scan_file(conn, root, path, snapshots, counters)

        control.report(min(start + _COMMIT_INTERVAL, len(files)), len(files))

    missing_ids = [] if cancelled else _missing_track_ids(snapshots, counters.seen)
    summary = track_store.ScanSummary(
        added=counters.added,
        updated=counters.updated,
        unchanged=counters.unchanged,
        missing=len(missing_ids),
        errors=counters.errors,
        elapsed_sec=time.monotonic() - started,
    )
    with transaction(conn):
        track_store.mark_missing(conn, missing_ids)
        track_store.record_scan(conn, library_root, started_at, summary)

    logger.info(
        "scan finished: added=%d updated=%d unchanged=%d missing=%d errors=%d cancelled=%s elapsed_sec=%.1f",
        summary.added,
        summary.updated,
        summary.unchanged,
        summary.missing,
        summary.errors,
        cancelled,
        summary.elapsed_sec,
    )
    return summary


def _scan_file(
    conn: sqlite3.Connection,
    root: Path,
    path: Path,
    snapshots: dict[str, track_store.TrackSnapshot],
    counters: _Counters,
) -> None:
    rel_path = to_library_relative_path(path, root)
    counters.seen.add(rel_path)
    snapshot = snapshots.get(rel_path)

    try:
        stat = path.stat()
        if (
            snapshot is not None
            and snapshot.status == "active"
            and snapshot.file_size == stat.st_size
            and snapshot.mtime_ns == stat.st_mtime_ns
        ):
            counters.unchanged += 1
            return

        result = read_metadata(path, rel_path)
    except OSError as exc:
        counters.errors += 1
        message = redact_paths(f"{type(exc).__name__}: cannot read file: {rel_path}", [root])
        record_error(
            conn, None if snapshot is None else snapshot.id, ErrorRecord("tags", type(exc).__name__, message, None)
        )
        logger.warning("scan file failed: path=%s error=%s", rel_path, type(exc).__name__)
        return

    track_id = track_store.upsert_track(conn, root.as_posix(), result.metadata, result.error)
    if result.error is not None:
        counters.errors += 1
        record_error(conn, track_id, ErrorRecord("tags", "TagReadError", result.error, None))
        logger.warning("tag read failed: path=%s", rel_path)

    if snapshot is None:
        counters.added += 1
    else:
        counters.updated += 1


def _missing_track_ids(snapshots: dict[str, track_store.TrackSnapshot], seen: set[str]) -> list[int]:
    return [s.id for rel_path, s in snapshots.items() if rel_path not in seen and s.status == "active"]
