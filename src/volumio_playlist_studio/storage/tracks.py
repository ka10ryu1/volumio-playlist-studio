"""楽曲メタデータとスキャン履歴の読み書き。"""

import sqlite3
from dataclasses import dataclass

from volumio_playlist_studio.models import TitleSource, Track, TrackMetadata
from volumio_playlist_studio.storage.database import utc_now


@dataclass(frozen=True)
class ScanSummary:
    """1 回のスキャンの結果。"""

    added: int
    updated: int
    unchanged: int
    missing: int
    errors: int
    elapsed_sec: float


@dataclass(frozen=True)
class TrackSnapshot:
    """差分判定に使う、DB に登録済みのファイル情報。"""

    id: int
    file_size: int
    mtime_ns: int
    status: str


def row_to_track(row: sqlite3.Row) -> Track:
    """DB の行を `Track` に変換する。"""
    return Track(
        id=row["id"],
        library_root=row["library_root"],
        rel_path=row["rel_path"],
        title=row["title"],
        artist=row["artist"],
        album=row["album"],
        genre=row["genre"],
        year=row["year"],
        track_number=row["track_number"],
        duration_sec=row["duration_sec"],
        bitrate=row["bitrate"],
        sample_rate=row["sample_rate"],
        channels=row["channels"],
        codec=row["codec"],
        file_size=row["file_size"],
        mtime_ns=row["mtime_ns"],
        title_source=None if row["title_source"] is None else TitleSource(row["title_source"]),
        tags_repaired=bool(row["tags_repaired"]),
        quick_hash=row["quick_hash"],
        status=row["status"],
        tag_error=row["tag_error"],
    )


def _metadata_values(meta: TrackMetadata) -> tuple[object, ...]:
    return (
        meta.title,
        meta.artist,
        meta.album,
        meta.genre,
        meta.year,
        meta.track_number,
        meta.duration_sec,
        meta.bitrate,
        meta.sample_rate,
        meta.channels,
        meta.codec,
        meta.file_size,
        meta.mtime_ns,
        None if meta.title_source is None else meta.title_source.value,
        int(meta.tags_repaired),
        meta.quick_hash,
    )


def upsert_track(conn: sqlite3.Connection, library_root: str, meta: TrackMetadata, tag_error: str | None) -> int:
    """楽曲を登録または更新し、ID を返す。消えていた楽曲が戻った場合は有効に戻す。"""
    now = utc_now()
    row = conn.execute(
        """
        INSERT INTO tracks (
            library_root, rel_path, title, artist, album, genre, year, track_number, duration_sec, bitrate,
            sample_rate, channels, codec, file_size, mtime_ns, title_source, tags_repaired, quick_hash,
            status, tag_error, first_seen_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
        ON CONFLICT (library_root, rel_path) DO UPDATE SET
            title = excluded.title, artist = excluded.artist, album = excluded.album, genre = excluded.genre,
            year = excluded.year, track_number = excluded.track_number, duration_sec = excluded.duration_sec,
            bitrate = excluded.bitrate, sample_rate = excluded.sample_rate, channels = excluded.channels,
            codec = excluded.codec, file_size = excluded.file_size, mtime_ns = excluded.mtime_ns,
            title_source = excluded.title_source, tags_repaired = excluded.tags_repaired,
            quick_hash = excluded.quick_hash, status = 'active', tag_error = excluded.tag_error,
            updated_at = excluded.updated_at
        RETURNING id
        """,
        (library_root, meta.rel_path, *_metadata_values(meta), tag_error, now, now),
    ).fetchone()
    return int(row["id"])


def load_snapshots(conn: sqlite3.Connection, library_root: str) -> dict[str, TrackSnapshot]:
    """ライブラリに登録済みの楽曲を、相対パスをキーにして返す。"""
    rows = conn.execute(
        "SELECT id, rel_path, file_size, mtime_ns, status FROM tracks WHERE library_root = ?",
        (library_root,),
    ).fetchall()
    return {row["rel_path"]: TrackSnapshot(row["id"], row["file_size"], row["mtime_ns"], row["status"]) for row in rows}


def mark_missing(conn: sqlite3.Connection, track_ids: list[int]) -> None:
    """ファイルが見つからなくなった楽曲を `missing` にする。評価データを残すため行は削除しない。"""
    conn.executemany(
        "UPDATE tracks SET status = 'missing', updated_at = ? WHERE id = ?",
        [(utc_now(), track_id) for track_id in track_ids],
    )


def record_scan(conn: sqlite3.Connection, library_root: str, started_at: str, summary: ScanSummary) -> None:
    """スキャン履歴を記録する。"""
    conn.execute(
        """
        INSERT INTO scans (library_root, started_at, finished_at, added, updated, unchanged, missing, errors,
                           elapsed_sec)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            library_root,
            started_at,
            utc_now(),
            summary.added,
            summary.updated,
            summary.unchanged,
            summary.missing,
            summary.errors,
            summary.elapsed_sec,
        ),
    )


def last_scan_at(conn: sqlite3.Connection, library_root: str) -> str | None:
    """最後にスキャンが完了した日時を返す。"""
    row = conn.execute(
        "SELECT MAX(finished_at) AS finished_at FROM scans WHERE library_root = ?", (library_root,)
    ).fetchone()
    return None if row is None else row["finished_at"]


def get_track(conn: sqlite3.Connection, track_id: int) -> Track | None:
    """ID で楽曲を取得する。"""
    row = conn.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()
    return None if row is None else row_to_track(row)


def list_tracks(conn: sqlite3.Connection, library_root: str, *, include_missing: bool = False) -> list[Track]:
    """ライブラリの楽曲を相対パス順に返す。"""
    if include_missing:
        query = "SELECT * FROM tracks WHERE library_root = ? ORDER BY rel_path"
    else:
        query = "SELECT * FROM tracks WHERE library_root = ? AND status = 'active' ORDER BY rel_path"
    rows = conn.execute(query, (library_root,)).fetchall()
    return [row_to_track(row) for row in rows]


def duplicate_groups(conn: sqlite3.Connection, library_root: str) -> list[list[int]]:
    """内容が同じと推定される楽曲（quick_hash が一致する）の ID のグループを返す。"""
    rows = conn.execute(
        """
        SELECT quick_hash, GROUP_CONCAT(id) AS ids FROM tracks
        WHERE library_root = ? AND status = 'active' AND quick_hash IS NOT NULL
        GROUP BY quick_hash HAVING COUNT(*) > 1
        """,
        (library_root,),
    ).fetchall()
    return [sorted(int(i) for i in row["ids"].split(",")) for row in rows]
