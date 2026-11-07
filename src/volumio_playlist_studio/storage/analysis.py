"""音響特徴量、分類結果、解析エラーの読み書き。"""

import json
import sqlite3
from dataclasses import asdict, dataclass

from volumio_playlist_studio.config import AnalysisParams
from volumio_playlist_studio.models import AudioFeatures, ClassificationResult, Track
from volumio_playlist_studio.storage.database import utc_now
from volumio_playlist_studio.storage.tracks import row_to_track


@dataclass(frozen=True)
class StoredClassification:
    """DB に保存済みの分類結果。"""

    id: int
    track_id: int
    created_at: str
    result: ClassificationResult


@dataclass(frozen=True)
class FeatureRun:
    """特徴量を求めたときの条件。"""

    params: AnalysisParams
    analysis_version: str
    elapsed_sec: float


@dataclass(frozen=True)
class ErrorRecord:
    """記録する解析エラー。message には絶対パスや機密情報を含めないこと。"""

    stage: str
    error_type: str
    message: str
    version: str | None


@dataclass(frozen=True)
class AnalysisError:
    """記録済みの解析エラー。"""

    id: int
    track_id: int | None
    rel_path: str | None
    stage: str
    error_type: str
    message: str
    version: str | None
    occurred_at: str


def save_features(conn: sqlite3.Connection, track: Track, features: AudioFeatures, run: FeatureRun) -> None:
    """音響特徴量を保存する。解析時点のファイルサイズ・更新日時も記録し、ファイル更新を検出できるようにする。"""
    conn.execute(
        """
        INSERT INTO audio_features (track_id, analysis_version, params_json, features_json, file_size, mtime_ns,
                                    elapsed_sec, analyzed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (track_id) DO UPDATE SET
            analysis_version = excluded.analysis_version, params_json = excluded.params_json,
            features_json = excluded.features_json, file_size = excluded.file_size, mtime_ns = excluded.mtime_ns,
            elapsed_sec = excluded.elapsed_sec, analyzed_at = excluded.analyzed_at
        """,
        (
            track.id,
            run.analysis_version,
            json.dumps(asdict(run.params)),
            json.dumps(features.to_dict()),
            track.file_size,
            track.mtime_ns,
            run.elapsed_sec,
            utc_now(),
        ),
    )
    # 同じ段階の古いエラーは解決済みにする
    conn.execute(
        "UPDATE analysis_errors SET resolved = 1 WHERE track_id = ? AND stage = 'features'",
        (track.id,),
    )


def load_features(conn: sqlite3.Connection, track_ids: list[int] | None = None) -> dict[int, AudioFeatures]:
    """保存済みの音響特徴量を楽曲 ID ごとに返す。バージョンを問わず最新の保存値を返す。"""
    rows = conn.execute("SELECT track_id, features_json FROM audio_features").fetchall()
    wanted = None if track_ids is None else set(track_ids)
    return {
        row["track_id"]: AudioFeatures.from_dict(json.loads(row["features_json"]))
        for row in rows
        if wanted is None or row["track_id"] in wanted
    }


def tracks_needing_analysis(conn: sqlite3.Connection, library_root: str, analysis_version: str) -> list[Track]:
    """未解析、ファイル更新後に未解析、または解析バージョンが古い楽曲を返す。"""
    rows = conn.execute(
        """
        SELECT t.* FROM tracks t
        LEFT JOIN audio_features f ON f.track_id = t.id
        WHERE t.library_root = ? AND t.status = 'active'
          AND (f.track_id IS NULL OR f.analysis_version != ? OR f.file_size != t.file_size
               OR f.mtime_ns != t.mtime_ns)
        ORDER BY t.rel_path
        """,
        (library_root, analysis_version),
    ).fetchall()
    return [row_to_track(row) for row in rows]


def record_error(conn: sqlite3.Connection, track_id: int | None, error: ErrorRecord) -> None:
    """解析エラーを記録する。"""
    conn.execute(
        """
        INSERT INTO analysis_errors (track_id, stage, error_type, message, version, occurred_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (track_id, error.stage, error.error_type, error.message, error.version, utc_now()),
    )


def list_errors(conn: sqlite3.Connection, library_root: str, *, limit: int = 200) -> list[AnalysisError]:
    """未解決の解析エラーを新しい順に返す。"""
    rows = conn.execute(
        """
        SELECT e.*, t.rel_path FROM analysis_errors e
        LEFT JOIN tracks t ON t.id = e.track_id
        WHERE e.resolved = 0 AND (t.library_root = ? OR e.track_id IS NULL)
        ORDER BY e.id DESC LIMIT ?
        """,
        (library_root, limit),
    ).fetchall()
    return [
        AnalysisError(
            id=row["id"],
            track_id=row["track_id"],
            rel_path=row["rel_path"],
            stage=row["stage"],
            error_type=row["error_type"],
            message=row["message"],
            version=row["version"],
            occurred_at=row["occurred_at"],
        )
        for row in rows
    ]


def save_classification(conn: sqlite3.Connection, track_id: int, result: ClassificationResult) -> int:
    """分類結果を保存し、ID を返す。同じエンジン・バージョンの結果は上書きする。"""
    row = conn.execute(
        """
        INSERT INTO classifications (track_id, engine, engine_version, model, result_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (track_id, engine, engine_version) DO UPDATE SET
            model = excluded.model, result_json = excluded.result_json, created_at = excluded.created_at
        RETURNING id
        """,
        (
            track_id,
            result.engine,
            result.engine_version,
            result.model,
            json.dumps(result.to_dict(), ensure_ascii=False),
            utc_now(),
        ),
    ).fetchone()
    conn.execute(
        "UPDATE analysis_errors SET resolved = 1 WHERE track_id = ? AND stage = 'classification'",
        (track_id,),
    )
    return int(row["id"])


def load_classifications(
    conn: sqlite3.Connection, engine: str, engine_version: str | None = None
) -> dict[int, StoredClassification]:
    """エンジンの分類結果を楽曲 ID ごとに返す。バージョン省略時は楽曲ごとに最新のものを返す。"""
    if engine_version is None:
        rows = conn.execute(
            """
            SELECT * FROM classifications c
            WHERE engine = ? AND id = (
                SELECT id FROM classifications c2 WHERE c2.track_id = c.track_id AND c2.engine = c.engine
                ORDER BY created_at DESC, id DESC LIMIT 1
            )
            """,
            (engine,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM classifications WHERE engine = ? AND engine_version = ?", (engine, engine_version)
        ).fetchall()
    return {
        row["track_id"]: StoredClassification(
            id=row["id"],
            track_id=row["track_id"],
            created_at=row["created_at"],
            result=ClassificationResult.from_dict(json.loads(row["result_json"])),
        )
        for row in rows
    }


def classification_versions(conn: sqlite3.Connection) -> list[tuple[str, str, int]]:
    """保存済みの（エンジン、バージョン、件数）の一覧を返す。"""
    rows = conn.execute(
        "SELECT engine, engine_version, COUNT(*) AS n FROM classifications GROUP BY engine, engine_version"
    ).fetchall()
    return [(row["engine"], row["engine_version"], row["n"]) for row in rows]


def tracks_without_classification(
    conn: sqlite3.Connection, library_root: str, engine: str, engine_version: str
) -> list[Track]:
    """指定したエンジン・バージョンの分類結果がない楽曲を返す。"""
    rows = conn.execute(
        """
        SELECT t.* FROM tracks t
        WHERE t.library_root = ? AND t.status = 'active' AND NOT EXISTS (
            SELECT 1 FROM classifications c
            WHERE c.track_id = t.id AND c.engine = ? AND c.engine_version = ?
        )
        ORDER BY t.rel_path
        """,
        (library_root, engine, engine_version),
    ).fetchall()
    return [row_to_track(row) for row in rows]
