"""Volumio から取得した曲の URI の一覧の読み書き。"""

import sqlite3

from volumio_playlist_studio.storage.database import utc_now


def replace_uris(conn: sqlite3.Connection, host: str, uris: list[str]) -> None:
    """接続先の曲の URI の一覧を置き換える。呼び出し側のトランザクションの中で実行すること。"""
    now = utc_now()
    conn.execute("DELETE FROM volumio_library_uris WHERE host = ?", (host,))
    conn.executemany(
        "INSERT OR IGNORE INTO volumio_library_uris (host, uri, collected_at) VALUES (?, ?, ?)",
        [(host, uri, now) for uri in uris],
    )


def load_uris(conn: sqlite3.Connection, host: str) -> list[str]:
    """接続先の曲の URI の一覧を返す。"""
    rows = conn.execute("SELECT uri FROM volumio_library_uris WHERE host = ? ORDER BY uri", (host,)).fetchall()
    return [row["uri"] for row in rows]


def last_collected_at(conn: sqlite3.Connection, host: str) -> str | None:
    """接続先の曲の一覧を最後に取得した日時を返す。未取得なら None。"""
    row = conn.execute("SELECT MAX(collected_at) AS at FROM volumio_library_uris WHERE host = ?", (host,)).fetchone()
    return None if row is None else row["at"]
