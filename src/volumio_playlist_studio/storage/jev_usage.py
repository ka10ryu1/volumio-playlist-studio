"""Jev の実行ごとの件数とトークン数の読み書き。"""

import sqlite3
from dataclasses import dataclass
from enum import StrEnum

from volumio_playlist_studio.storage.database import utc_now


class JevUsageKind(StrEnum):
    """Jev に何を判定させたか。"""

    TRACKS = "tracks"
    ALBUMS = "albums"
    CUSTOM_MOOD = "custom_mood"


@dataclass(frozen=True)
class JevUsageRecord:
    """1 回の実行の件数とトークン数。

    Attributes:
        model: 設定画面で指定したモデル名。
        succeeded: 判定できた件数（曲・アルバム・雰囲気）。
        failed: 失敗した件数。
        finished_at: 記録した日時。保存時は無視し、現在時刻を記録する。
    """

    kind: JevUsageKind
    question_version: str
    model: str
    succeeded: int
    failed: int
    input_tokens: int
    output_tokens: int
    finished_at: str = ""


def record_usage(conn: sqlite3.Connection, record: JevUsageRecord) -> None:
    """実行の件数とトークン数を記録する。"""
    conn.execute(
        """
        INSERT INTO jev_usage (kind, question_version, model, succeeded, failed, input_tokens, output_tokens,
                               finished_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record.kind.value,
            record.question_version,
            record.model,
            record.succeeded,
            record.failed,
            record.input_tokens,
            record.output_tokens,
            utc_now(),
        ),
    )


def list_usage(conn: sqlite3.Connection, *, limit: int = 20) -> list[JevUsageRecord]:
    """記録を新しい順に返す。"""
    rows = conn.execute("SELECT * FROM jev_usage ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [
        JevUsageRecord(
            kind=JevUsageKind(row["kind"]),
            question_version=row["question_version"],
            model=row["model"],
            succeeded=row["succeeded"],
            failed=row["failed"],
            input_tokens=row["input_tokens"],
            output_tokens=row["output_tokens"],
            finished_at=row["finished_at"],
        )
        for row in rows
    ]
