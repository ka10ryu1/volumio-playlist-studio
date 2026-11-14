"""アルバム単位の Jev の判定（子供向けである確率）の読み書き。"""

import sqlite3
from dataclasses import dataclass

from volumio_playlist_studio.storage.database import utc_now


@dataclass(frozen=True)
class AlbumJudgmentRecord:
    """保存するアルバムの判定。

    Attributes:
        album_key: `albums.album_key` で求めたアルバムのキー。
        children_probability: 子供向けのアルバムである確率（0〜1）。
        model: 応答したモデル名。
    """

    library_root: str
    album_key: str
    question_version: str
    children_probability: float
    model: str | None


def save_album_judgment(conn: sqlite3.Connection, record: AlbumJudgmentRecord) -> None:
    """アルバムの判定を保存する。同じアルバム・質問バージョンの結果があれば置き換える。

    Raises:
        ValueError: 確率が 0〜1 の範囲外の場合。
    """
    if not 0.0 <= record.children_probability <= 1.0:
        raise ValueError("probability must be between 0 and 1")

    conn.execute(
        """
        INSERT INTO album_judgments
            (library_root, album_key, question_version, model, children_probability, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (library_root, album_key, question_version) DO UPDATE SET
            model = excluded.model,
            children_probability = excluded.children_probability,
            created_at = excluded.created_at
        """,
        (
            record.library_root,
            record.album_key,
            record.question_version,
            record.model,
            record.children_probability,
            utc_now(),
        ),
    )


def load_children_probabilities(conn: sqlite3.Connection, library_root: str, question_version: str) -> dict[str, float]:
    """指定した質問バージョンで判定済みのアルバムについて、子供向けである確率をアルバムのキーごとに返す。"""
    rows = conn.execute(
        """
        SELECT album_key, children_probability FROM album_judgments
        WHERE library_root = ? AND question_version = ?
        """,
        (library_root, question_version),
    ).fetchall()
    return {str(row["album_key"]): float(row["children_probability"]) for row in rows}
