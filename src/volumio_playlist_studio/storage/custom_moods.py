"""自由記述の雰囲気の読み書き。"""

import json
import sqlite3
from dataclasses import asdict, dataclass

from volumio_playlist_studio.models import Attribute, AttributeScore, CustomMood
from volumio_playlist_studio.storage.database import utc_now


class DuplicateNameError(Exception):
    """同じ名前の自由記述の雰囲気が既にある場合の例外。"""


@dataclass(frozen=True)
class CustomMoodDraft:
    """保存する自由記述の雰囲気。各項目は `models.CustomMood` と同じ。"""

    name: str
    description: str
    x: float
    y: float
    attributes: dict[Attribute, AttributeScore]
    expressible: float
    question_version: str
    model: str | None


def save_custom_mood(conn: sqlite3.Connection, draft: CustomMoodDraft) -> int:
    """自由記述の雰囲気を保存し、ID を返す。

    Raises:
        DuplicateNameError: 同じ名前の雰囲気が既にある場合。
    """
    result = {
        "attributes": {attribute.value: asdict(score) for attribute, score in draft.attributes.items()},
        "expressible": draft.expressible,
    }
    try:
        row = conn.execute(
            """
            INSERT INTO custom_moods (name, description, x, y, result_json, question_version, model, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING id
            """,
            (
                draft.name,
                draft.description,
                draft.x,
                draft.y,
                json.dumps(result, ensure_ascii=False),
                draft.question_version,
                draft.model,
                utc_now(),
            ),
        ).fetchone()
    except sqlite3.IntegrityError as exc:
        if "custom_moods.name" in str(exc):
            raise DuplicateNameError("custom mood name already exists") from exc

        raise

    return int(row["id"])


def list_custom_moods(conn: sqlite3.Connection) -> list[CustomMood]:
    """保存済みの自由記述の雰囲気を作成順に返す。

    Raises:
        ValueError: 保存された内容の形式が不正な場合。
    """
    rows = conn.execute("SELECT * FROM custom_moods ORDER BY id").fetchall()
    return [_from_row(row) for row in rows]


def delete_custom_mood(conn: sqlite3.Connection, mood_id: int) -> None:
    """自由記述の雰囲気を削除する。作成済みのプレイリストの条件には名前・説明文・座標が残る。

    Raises:
        ValueError: 指定した ID の雰囲気がない場合。
    """
    cursor = conn.execute("DELETE FROM custom_moods WHERE id = ?", (mood_id,))
    if cursor.rowcount == 0:
        raise ValueError("custom mood not found")


def _from_row(row: sqlite3.Row) -> CustomMood:
    try:
        result = json.loads(row["result_json"])
        attributes = {
            Attribute(key): AttributeScore(
                value=score["value"],
                confidence=score.get("confidence"),
                probabilities=score.get("probabilities"),
            )
            for key, score in result["attributes"].items()
        }
        expressible = float(result["expressible"])
    except (KeyError, TypeError, AttributeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid custom mood data") from exc

    return CustomMood(
        id=int(row["id"]),
        name=str(row["name"]),
        description=str(row["description"]),
        x=float(row["x"]),
        y=float(row["y"]),
        attributes=attributes,
        expressible=expressible,
        question_version=str(row["question_version"]),
        model=row["model"],
        created_at=str(row["created_at"]),
    )
