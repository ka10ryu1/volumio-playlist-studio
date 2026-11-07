import sqlite3
from pathlib import Path

import pytest

from volumio_playlist_studio.storage import database
from volumio_playlist_studio.storage.database import SCHEMA_VERSION, connect, current_version, transaction


def test_new_database_is_migrated_to_latest_version(tmp_path: Path) -> None:
    conn = connect(tmp_path / "nested" / "db.sqlite3")

    assert current_version(conn) == SCHEMA_VERSION
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"tracks", "audio_features", "classifications", "evaluations", "playlists", "analysis_errors"} <= tables
    conn.close()


def test_data_persists_after_reopening(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite3"
    conn = connect(path)
    with transaction(conn):
        conn.execute("INSERT INTO app_settings (key, value_json, updated_at) VALUES ('k', '\"値\"', 'now')")

    conn.close()

    reopened = connect(path)

    assert reopened.execute("SELECT value_json FROM app_settings").fetchone()[0] == '"値"'
    reopened.close()


def test_transaction_rolls_back_on_error(db: sqlite3.Connection) -> None:
    def insert_then_fail() -> None:
        with transaction(db):
            db.execute("INSERT INTO app_settings (key, value_json, updated_at) VALUES ('k', '1', 'now')")
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        insert_then_fail()

    assert db.execute("SELECT COUNT(*) FROM app_settings").fetchone()[0] == 0


def test_newer_schema_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite3"
    conn = connect(path)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.close()

    with pytest.raises(RuntimeError, match="newer"):
        connect(path)


def test_migration_backs_up_existing_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "db.sqlite3"
    conn = connect(path)
    with transaction(conn):
        conn.execute("INSERT INTO app_settings (key, value_json, updated_at) VALUES ('k', '1', 'now')")

    conn.close()
    # スキーマの変更を追加した次のバージョンのアプリを模擬する
    monkeypatch.setattr(database, "MIGRATIONS", (*database.MIGRATIONS, ("CREATE TABLE extra (x INTEGER)",)))
    monkeypatch.setattr(database, "SCHEMA_VERSION", SCHEMA_VERSION + 1)

    upgraded = connect(path)

    assert current_version(upgraded) == SCHEMA_VERSION + 1
    assert upgraded.execute("SELECT COUNT(*) FROM extra").fetchone()[0] == 0
    upgraded.close()
    backup = sqlite3.connect(tmp_path / f"db.sqlite3.bak-v{SCHEMA_VERSION}")
    assert backup.execute("SELECT key FROM app_settings").fetchall() == [("k",)]
    backup.close()


def test_failed_migration_keeps_existing_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "db.sqlite3"
    conn = connect(path)
    with transaction(conn):
        conn.execute("INSERT INTO app_settings (key, value_json, updated_at) VALUES ('k', '1', 'now')")

    conn.close()
    broken = ("CREATE TABLE half_done (x INTEGER)", "THIS IS NOT SQL")
    monkeypatch.setattr(database, "MIGRATIONS", (*database.MIGRATIONS, broken))
    monkeypatch.setattr(database, "SCHEMA_VERSION", SCHEMA_VERSION + 1)

    with pytest.raises(sqlite3.OperationalError):
        connect(path)

    check = sqlite3.connect(path)
    assert check.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert check.execute("SELECT key FROM app_settings").fetchall() == [("k",)]
    tables = {row[0] for row in check.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "half_done" not in tables
    check.close()
