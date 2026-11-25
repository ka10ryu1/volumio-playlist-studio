"""プレイリストの作成中に曲を除外した記録（除外の理由）の読み書き。

「ジャンルとして不適切」は分類の誤りとして、分類の評価・改善に使う。分類結果の行は再分類で上書きされうるため、
除外したときに選曲に使っていた分類結果の写し（エンジン・バージョンを含む）を一緒に残す。
「好みの問題」は記録するだけで、選曲には使わない。
除外はプレイリストを保存する前に行うため、保存したプレイリストとは結び付けない。
"""

import json
import sqlite3
from dataclasses import dataclass
from enum import StrEnum

from volumio_playlist_studio.models import ClassificationResult, Genre
from volumio_playlist_studio.storage.database import utc_now


class ExclusionReason(StrEnum):
    """曲を除外した理由。"""

    GENRE_MISMATCH = "genre_mismatch"
    PREFERENCE = "preference"


@dataclass(frozen=True)
class TrackExclusion:
    """プレイリストの作成中に曲を除外した記録。

    Attributes:
        genre: 除外したときに選曲に使っていたジャンル。人手で修正したジャンルがあればその値になるため、
            分類結果の写しのジャンルと異なることがある。不明なら None。
        classification_source: 選曲に使った分類結果の種類（`ClassificationSource` の値）。
        classification: 選曲に使っていた分類結果の写し。未分類の曲なら None。
        conditions: プレイリストの生成条件（JSON に変換できる辞書）。
        excluded_at: 除外した日時。記録する前は None。
    """

    track_id: int
    reason: ExclusionReason
    genre: Genre | None
    classification_source: str
    classification: ClassificationResult | None
    conditions: dict[str, object]
    excluded_at: str | None = None


def record_exclusion(conn: sqlite3.Connection, exclusion: TrackExclusion) -> int:
    """除外の記録を追加し、ID を返す。同じ曲を何度除外しても、それぞれ別の記録として残す。"""
    row = conn.execute(
        """
        INSERT INTO track_exclusions (track_id, reason, genre, classification_source, classification_snapshot_json,
                                      conditions_json, excluded_at)
        VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id
        """,
        (
            exclusion.track_id,
            exclusion.reason.value,
            None if exclusion.genre is None else exclusion.genre.value,
            exclusion.classification_source,
            None
            if exclusion.classification is None
            else json.dumps(exclusion.classification.to_dict(), ensure_ascii=False),
            json.dumps(exclusion.conditions, ensure_ascii=False),
            utc_now(),
        ),
    ).fetchone()
    return int(row["id"])


def list_exclusions(conn: sqlite3.Connection) -> list[TrackExclusion]:
    """除外の記録を古い順に返す。"""
    rows = conn.execute("SELECT * FROM track_exclusions ORDER BY id").fetchall()
    return [
        TrackExclusion(
            track_id=row["track_id"],
            reason=ExclusionReason(row["reason"]),
            genre=None if row["genre"] is None else Genre(row["genre"]),
            classification_source=row["classification_source"],
            classification=None
            if row["classification_snapshot_json"] is None
            else ClassificationResult.from_dict(json.loads(row["classification_snapshot_json"])),
            conditions=json.loads(row["conditions_json"]),
            excluded_at=row["excluded_at"],
        )
        for row in rows
    ]
