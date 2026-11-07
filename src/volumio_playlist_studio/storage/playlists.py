"""プレイリスト、プレイリストへのフィードバック、Volumio 登録履歴の読み書き。"""

import json
import sqlite3
from dataclasses import dataclass

from volumio_playlist_studio.config import RATING_MAX, RATING_MIN
from volumio_playlist_studio.storage.database import utc_now


@dataclass(frozen=True)
class PlaylistEntry:
    """プレイリスト内の 1 曲。"""

    track_id: int
    score: float | None
    added_manually: bool = False
    unsuitable: bool = False


@dataclass(frozen=True)
class PlaylistDraft:
    """保存するプレイリスト。

    Attributes:
        conditions: 生成条件（JSON に変換できる辞書）。
        classification_source: 生成に使った分類結果の種類。
    """

    name: str
    conditions: dict[str, object]
    seed: int
    classification_source: str
    total_duration_sec: float
    entries: list[PlaylistEntry]


@dataclass(frozen=True)
class SavedPlaylist:
    """保存済みのプレイリスト。"""

    id: int
    name: str
    conditions: dict[str, object]
    seed: int
    classification_source: str
    total_duration_sec: float
    satisfaction: int | None
    created_at: str
    entries: list[PlaylistEntry]


@dataclass(frozen=True)
class RegistrationItem:
    """Volumio への登録対象の 1 曲と、その結果。"""

    position: int
    track_id: int
    uri: str | None
    status: str
    error: str | None = None


@dataclass(frozen=True)
class RegistrationRecord:
    """Volumio への登録 1 回分の記録。"""

    playlist_id: int
    volumio_playlist_name: str
    client_kind: str
    status: str
    error: str | None
    started_at: str
    items: list[RegistrationItem]


def save_playlist(conn: sqlite3.Connection, draft: PlaylistDraft) -> int:
    """プレイリストを保存し、ID を返す。

    Raises:
        ValueError: 名前が空、または曲がない場合。
    """
    name = draft.name.strip()
    if not name:
        raise ValueError("playlist name must not be empty")
    if not draft.entries:
        raise ValueError("playlist must contain at least one track")

    now = utc_now()
    row = conn.execute(
        """
        INSERT INTO playlists (name, conditions_json, seed, classification_source, total_duration_sec,
                               created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id
        """,
        (
            name,
            json.dumps(draft.conditions, ensure_ascii=False),
            draft.seed,
            draft.classification_source,
            draft.total_duration_sec,
            now,
            now,
        ),
    ).fetchone()
    playlist_id = int(row["id"])
    conn.executemany(
        """
        INSERT INTO playlist_tracks (playlist_id, position, track_id, score, added_manually, unsuitable)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            (playlist_id, position, e.track_id, e.score, int(e.added_manually), int(e.unsuitable))
            for position, e in enumerate(draft.entries)
        ],
    )
    return playlist_id


def list_playlists(conn: sqlite3.Connection) -> list[SavedPlaylist]:
    """保存済みのプレイリストを新しい順に返す。"""
    rows = conn.execute("SELECT * FROM playlists ORDER BY id DESC").fetchall()
    return [_row_to_playlist(conn, row) for row in rows]


def get_playlist(conn: sqlite3.Connection, playlist_id: int) -> SavedPlaylist | None:
    """ID でプレイリストを取得する。"""
    row = conn.execute("SELECT * FROM playlists WHERE id = ?", (playlist_id,)).fetchone()
    return None if row is None else _row_to_playlist(conn, row)


def _row_to_playlist(conn: sqlite3.Connection, row: sqlite3.Row) -> SavedPlaylist:
    entries = conn.execute(
        "SELECT * FROM playlist_tracks WHERE playlist_id = ? ORDER BY position", (row["id"],)
    ).fetchall()
    return SavedPlaylist(
        id=row["id"],
        name=row["name"],
        conditions=json.loads(row["conditions_json"]),
        seed=row["seed"],
        classification_source=row["classification_source"],
        total_duration_sec=row["total_duration_sec"],
        satisfaction=row["satisfaction"],
        created_at=row["created_at"],
        entries=[
            PlaylistEntry(
                track_id=e["track_id"],
                score=e["score"],
                added_manually=bool(e["added_manually"]),
                unsuitable=bool(e["unsuitable"]),
            )
            for e in entries
        ],
    )


def set_feedback(
    conn: sqlite3.Connection, playlist_id: int, satisfaction: int | None, unsuitable_positions: set[int]
) -> None:
    """プレイリストへの主観的満足度（1〜5）と、不適切だった曲を記録する。

    Raises:
        ValueError: 満足度が 1〜5 の範囲外の場合。
    """
    if satisfaction is not None and not RATING_MIN <= satisfaction <= RATING_MAX:
        raise ValueError("satisfaction must be between 1 and 5")
    conn.execute(
        "UPDATE playlists SET satisfaction = ?, updated_at = ? WHERE id = ?", (satisfaction, utc_now(), playlist_id)
    )
    conn.execute("UPDATE playlist_tracks SET unsuitable = 0 WHERE playlist_id = ?", (playlist_id,))
    conn.executemany(
        "UPDATE playlist_tracks SET unsuitable = 1 WHERE playlist_id = ? AND position = ?",
        [(playlist_id, position) for position in sorted(unsuitable_positions)],
    )


def track_usage_counts(conn: sqlite3.Connection) -> dict[int, int]:
    """保存済みプレイリストに各楽曲が含まれた回数を返す。選曲の意外性の計算に使う。"""
    rows = conn.execute("SELECT track_id, COUNT(*) AS n FROM playlist_tracks GROUP BY track_id").fetchall()
    return {row["track_id"]: row["n"] for row in rows}


def save_registration(conn: sqlite3.Connection, record: RegistrationRecord) -> int:
    """Volumio への登録結果を記録し、ID を返す。"""
    row = conn.execute(
        """
        INSERT INTO volumio_registrations (playlist_id, volumio_playlist_name, client_kind, status, error,
                                           started_at, finished_at)
        VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id
        """,
        (
            record.playlist_id,
            record.volumio_playlist_name,
            record.client_kind,
            record.status,
            record.error,
            record.started_at,
            utc_now(),
        ),
    ).fetchone()
    registration_id = int(row["id"])
    conn.executemany(
        """
        INSERT INTO volumio_registration_items (registration_id, position, track_id, uri, status, error)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [(registration_id, i.position, i.track_id, i.uri, i.status, i.error) for i in record.items],
    )
    return registration_id


def list_registrations(conn: sqlite3.Connection, playlist_id: int) -> list[RegistrationRecord]:
    """プレイリストの Volumio 登録履歴を新しい順に返す。"""
    rows = conn.execute(
        "SELECT * FROM volumio_registrations WHERE playlist_id = ? ORDER BY id DESC", (playlist_id,)
    ).fetchall()
    records = []
    for row in rows:
        items = conn.execute(
            "SELECT * FROM volumio_registration_items WHERE registration_id = ? ORDER BY position", (row["id"],)
        ).fetchall()
        records.append(
            RegistrationRecord(
                playlist_id=row["playlist_id"],
                volumio_playlist_name=row["volumio_playlist_name"],
                client_kind=row["client_kind"],
                status=row["status"],
                error=row["error"],
                started_at=row["started_at"],
                items=[
                    RegistrationItem(i["position"], i["track_id"], i["uri"], i["status"], i["error"]) for i in items
                ],
            )
        )
    return records
