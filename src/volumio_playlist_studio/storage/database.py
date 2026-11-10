"""SQLite への接続、トランザクション、スキーマのマイグレーションを扱う。

スキーマのバージョンは `PRAGMA user_version` で管理する。マイグレーションは 1 つのトランザクションで
適用し、適用前に既存の DB ファイルをバックアップする。途中で失敗しても元のデータは残る。
"""

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# 要素 i がバージョン i から i+1 への移行。既存の要素は変更せず、変更は末尾に追加する
MIGRATIONS: tuple[tuple[str, ...], ...] = (
    (
        """
        CREATE TABLE app_settings (
            key TEXT PRIMARY KEY,
            value_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE scans (
            id INTEGER PRIMARY KEY,
            library_root TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            added INTEGER NOT NULL DEFAULT 0,
            updated INTEGER NOT NULL DEFAULT 0,
            unchanged INTEGER NOT NULL DEFAULT 0,
            missing INTEGER NOT NULL DEFAULT 0,
            errors INTEGER NOT NULL DEFAULT 0,
            elapsed_sec REAL
        )
        """,
        """
        CREATE TABLE tracks (
            id INTEGER PRIMARY KEY,
            library_root TEXT NOT NULL,
            rel_path TEXT NOT NULL,
            title TEXT,
            artist TEXT,
            album TEXT,
            genre TEXT,
            year INTEGER,
            track_number INTEGER,
            duration_sec REAL,
            bitrate INTEGER,
            sample_rate INTEGER,
            channels INTEGER,
            codec TEXT,
            file_size INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL,
            title_source TEXT,
            tags_repaired INTEGER NOT NULL DEFAULT 0,
            quick_hash TEXT,
            status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'missing')),
            tag_error TEXT,
            first_seen_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (library_root, rel_path)
        )
        """,
        "CREATE INDEX idx_tracks_quick_hash ON tracks (quick_hash)",
        """
        CREATE TABLE audio_features (
            track_id INTEGER PRIMARY KEY REFERENCES tracks (id) ON DELETE CASCADE,
            analysis_version TEXT NOT NULL,
            params_json TEXT NOT NULL,
            features_json TEXT NOT NULL,
            file_size INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL,
            elapsed_sec REAL NOT NULL,
            analyzed_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE analysis_errors (
            id INTEGER PRIMARY KEY,
            track_id INTEGER REFERENCES tracks (id) ON DELETE CASCADE,
            stage TEXT NOT NULL CHECK (stage IN ('tags', 'features', 'classification')),
            error_type TEXT NOT NULL,
            message TEXT NOT NULL,
            version TEXT,
            occurred_at TEXT NOT NULL,
            resolved INTEGER NOT NULL DEFAULT 0
        )
        """,
        """
        CREATE TABLE classifications (
            id INTEGER PRIMARY KEY,
            track_id INTEGER NOT NULL REFERENCES tracks (id) ON DELETE CASCADE,
            engine TEXT NOT NULL,
            engine_version TEXT NOT NULL,
            model TEXT,
            result_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (track_id, engine, engine_version)
        )
        """,
        """
        CREATE TABLE evaluations (
            track_id INTEGER PRIMARY KEY REFERENCES tracks (id) ON DELETE CASCADE,
            split TEXT NOT NULL CHECK (split IN ('tuning', 'holdout')),
            classification_id INTEGER REFERENCES classifications (id) ON DELETE SET NULL,
            genre_verdict TEXT CHECK (genre_verdict IN ('correct', 'incorrect', 'pending')),
            genre_label TEXT,
            mood_verdict TEXT CHECK (mood_verdict IN ('correct', 'incorrect', 'pending')),
            mood_label TEXT,
            ratings_json TEXT NOT NULL DEFAULT '{}',
            note TEXT,
            added_at TEXT NOT NULL,
            evaluated_at TEXT
        )
        """,
        """
        CREATE TABLE playlists (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            conditions_json TEXT NOT NULL,
            seed INTEGER NOT NULL,
            classification_source TEXT NOT NULL,
            total_duration_sec REAL NOT NULL,
            satisfaction INTEGER CHECK (satisfaction BETWEEN 1 AND 5),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE playlist_tracks (
            playlist_id INTEGER NOT NULL REFERENCES playlists (id) ON DELETE CASCADE,
            position INTEGER NOT NULL,
            track_id INTEGER NOT NULL REFERENCES tracks (id),
            score REAL,
            added_manually INTEGER NOT NULL DEFAULT 0,
            unsuitable INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (playlist_id, position)
        )
        """,
        """
        CREATE TABLE volumio_registrations (
            id INTEGER PRIMARY KEY,
            playlist_id INTEGER NOT NULL REFERENCES playlists (id) ON DELETE CASCADE,
            volumio_playlist_name TEXT NOT NULL,
            client_kind TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('success', 'partial', 'failed')),
            error TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE volumio_registration_items (
            registration_id INTEGER NOT NULL REFERENCES volumio_registrations (id) ON DELETE CASCADE,
            position INTEGER NOT NULL,
            track_id INTEGER NOT NULL REFERENCES tracks (id),
            uri TEXT,
            status TEXT NOT NULL CHECK (status IN ('added', 'failed', 'skipped')),
            error TEXT,
            PRIMARY KEY (registration_id, position)
        )
        """,
    ),
    (
        # 評価時に表示していた分類結果の写し。classifications の行は同じエンジン・バージョンで再分類すると
        # 上書きされるため、何を見て評価したかを評価データ側に残す
        "ALTER TABLE evaluations ADD COLUMN classification_snapshot_json TEXT",
    ),
    (
        # Volumio の REST API で取得した曲の URI。照合のたびに Volumio のライブラリをたどらずに済むよう保存する
        """
        CREATE TABLE volumio_library_uris (
            host TEXT NOT NULL,
            uri TEXT NOT NULL,
            collected_at TEXT NOT NULL,
            PRIMARY KEY (host, uri)
        )
        """,
    ),
    (
        # 解析に失敗したときのファイルの状態。ファイルが変わっていない限り、同じ解析バージョンでは再試行しないために使う
        "ALTER TABLE analysis_errors ADD COLUMN file_size INTEGER",
        "ALTER TABLE analysis_errors ADD COLUMN mtime_ns INTEGER",
    ),
    (
        # プレイリストを削除した日時。満足度・不適切な曲の評価を残すため、行は消さずに一覧から隠す
        "ALTER TABLE playlists ADD COLUMN deleted_at TEXT",
    ),
)

SCHEMA_VERSION = len(MIGRATIONS)


def utc_now() -> str:
    """現在時刻を ISO 8601 形式（UTC）の文字列で返す。"""
    return datetime.now(UTC).isoformat(timespec="seconds")


def connect(path: Path) -> sqlite3.Connection:
    """DB に接続し、必要ならマイグレーションを適用する。

    トランザクションは `transaction()` で明示的に管理するため、autocommit モードで接続する。
    GUI とバックグラウンド処理が同時に読み書きできるよう WAL モードを使う。
    スレッドごとに別の接続を使うこと。

    Args:
        path: DB ファイルのパス。親ディレクトリがなければ作成する。

    Returns:
        マイグレーション済みの接続。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, autocommit=True, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        migrate(conn, path)
    except BaseException:
        conn.close()
        raise

    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """ブロック内の変更を 1 つのトランザクションにまとめる。例外時はロールバックする。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise

    conn.execute("COMMIT")


def current_version(conn: sqlite3.Connection) -> int:
    """DB のスキーマバージョンを返す。"""
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0])


def migrate(conn: sqlite3.Connection, path: Path | None = None) -> None:
    """未適用のマイグレーションを適用する。

    Args:
        conn: 対象の接続。
        path: DB ファイルのパス。指定され、既存のデータがある場合は適用前にバックアップする。

    Raises:
        RuntimeError: DB のバージョンがアプリより新しい場合。
    """
    version = current_version(conn)
    if version > SCHEMA_VERSION:
        raise RuntimeError(f"database schema version {version} is newer than supported {SCHEMA_VERSION}")

    if version == SCHEMA_VERSION:
        return

    if path is not None and version > 0:
        _backup(conn, path.with_name(f"{path.name}.bak-v{version}"))

    with transaction(conn):
        for statements in MIGRATIONS[version:]:
            for statement in statements:
                conn.execute(statement)

        # PRAGMA にはパラメータを渡せないため、整数に変換した値を埋め込む
        conn.execute(f"PRAGMA user_version = {int(SCHEMA_VERSION)}")

    logger.info("database migrated: from=%d to=%d", version, SCHEMA_VERSION)


def _backup(conn: sqlite3.Connection, backup_path: Path) -> None:
    with closing(sqlite3.connect(backup_path)) as backup:
        conn.backup(backup)

    logger.info("database backed up before migration: file=%s", backup_path.name)
