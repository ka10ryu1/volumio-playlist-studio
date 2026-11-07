"""人手評価（正解データ）の読み書き。"""

import json
import sqlite3
from dataclasses import dataclass, field
from enum import StrEnum

from volumio_playlist_studio.config import RATING_MAX, RATING_MIN
from volumio_playlist_studio.models import Attribute, Genre, Mood
from volumio_playlist_studio.storage.database import utc_now


class Split(StrEnum):
    """評価データの用途。TUNING だけをルール調整に使い、HOLDOUT は検証専用にする。"""

    TUNING = "tuning"
    HOLDOUT = "holdout"


class Verdict(StrEnum):
    """判定結果に対する人手の評価。"""

    CORRECT = "correct"
    INCORRECT = "incorrect"
    PENDING = "pending"


@dataclass(frozen=True)
class EvaluationLabel:
    """1 曲分の人手評価。

    Attributes:
        classification_id: 評価時に画面に表示していた分類結果。どのバージョンの結果を見て評価したかを追跡する。
        genre_label: 人が正しいと判断したジャンル。判断できない場合は None。
        mood_label: 人が正しいと判断したムード。判断できない場合は None。
        ratings: 属性ごとの 5 段階評価。
    """

    track_id: int
    split: Split
    classification_id: int | None = None
    genre_verdict: Verdict | None = None
    genre_label: Genre | None = None
    mood_verdict: Verdict | None = None
    mood_label: Mood | None = None
    ratings: dict[Attribute, int] = field(default_factory=dict)
    note: str | None = None
    evaluated_at: str | None = None

    @property
    def is_evaluated(self) -> bool:
        """ジャンルかムードの評価が入力済みかどうか。"""
        return self.genre_verdict is not None or self.mood_verdict is not None


def add_to_evaluation_set(conn: sqlite3.Connection, assignments: dict[int, Split]) -> None:
    """楽曲を評価対象に追加する。既に評価対象の楽曲は、用途も評価内容も変更しない。"""
    now = utc_now()
    conn.executemany(
        "INSERT INTO evaluations (track_id, split, added_at) VALUES (?, ?, ?) ON CONFLICT (track_id) DO NOTHING",
        [(track_id, split.value, now) for track_id, split in assignments.items()],
    )


def save_label(conn: sqlite3.Connection, label: EvaluationLabel) -> None:
    """人手評価を保存する。用途（split）は追加時のものを維持する。

    Raises:
        ValueError: 評価対象に追加されていない楽曲、または 5 段階の範囲外の評価の場合。
    """
    for attribute, rating in label.ratings.items():
        if not RATING_MIN <= rating <= RATING_MAX:
            raise ValueError(f"rating for {attribute.value} must be between {RATING_MIN} and {RATING_MAX}")

    cursor = conn.execute(
        """
        UPDATE evaluations SET classification_id = ?, genre_verdict = ?, genre_label = ?, mood_verdict = ?,
            mood_label = ?, ratings_json = ?, note = ?, evaluated_at = ?
        WHERE track_id = ?
        """,
        (
            label.classification_id,
            _value_or_none(label.genre_verdict),
            _value_or_none(label.genre_label),
            _value_or_none(label.mood_verdict),
            _value_or_none(label.mood_label),
            json.dumps({attr.value: rating for attr, rating in label.ratings.items()}),
            label.note,
            utc_now(),
            label.track_id,
        ),
    )
    if cursor.rowcount == 0:
        raise ValueError("track is not in the evaluation set")


def load_labels(conn: sqlite3.Connection) -> dict[int, EvaluationLabel]:
    """評価対象の全楽曲の人手評価を返す。"""
    rows = conn.execute("SELECT * FROM evaluations ORDER BY track_id").fetchall()
    return {row["track_id"]: _row_to_label(row) for row in rows}


def _row_to_label(row: sqlite3.Row) -> EvaluationLabel:
    ratings = json.loads(row["ratings_json"])
    return EvaluationLabel(
        track_id=row["track_id"],
        split=Split(row["split"]),
        classification_id=row["classification_id"],
        genre_verdict=None if row["genre_verdict"] is None else Verdict(row["genre_verdict"]),
        genre_label=None if row["genre_label"] is None else Genre(row["genre_label"]),
        mood_verdict=None if row["mood_verdict"] is None else Verdict(row["mood_verdict"]),
        mood_label=None if row["mood_label"] is None else Mood(row["mood_label"]),
        ratings={Attribute(k): int(v) for k, v in ratings.items()},
        note=row["note"],
        evaluated_at=row["evaluated_at"],
    )


def remove_from_evaluation_set(conn: sqlite3.Connection, track_id: int) -> None:
    """楽曲を評価対象から外す。"""
    conn.execute("DELETE FROM evaluations WHERE track_id = ?", (track_id,))


def _value_or_none(value: StrEnum | None) -> str | None:
    return None if value is None else value.value
