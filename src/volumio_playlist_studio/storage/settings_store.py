"""GUI で変更する設定値の読み書き。API キーなどの秘密情報はここに保存しない。"""

import json
import sqlite3
from dataclasses import asdict, fields

from volumio_playlist_studio.config import AnalysisParams, JevSettings
from volumio_playlist_studio.storage.database import utc_now

MUSIC_ROOT_KEY = "music_root"
ANALYSIS_PARAMS_KEY = "analysis_params"
JEV_SETTINGS_KEY = "jev_settings"
CLASSIFICATION_SOURCE_KEY = "classification_source"
VOLUMIO_HOST_KEY = "volumio_host"


def get_value(conn: sqlite3.Connection, key: str) -> object:
    """JSON として保存した設定値を返す。未設定なら None。"""
    row = conn.execute("SELECT value_json FROM app_settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        return None
    value: object = json.loads(row["value_json"])
    return value


def set_value(conn: sqlite3.Connection, key: str, value: object) -> None:
    """設定値を保存する。"""
    conn.execute(
        """
        INSERT INTO app_settings (key, value_json, updated_at) VALUES (?, ?, ?)
        ON CONFLICT (key) DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at
        """,
        (key, json.dumps(value, ensure_ascii=False), utc_now()),
    )


def get_str(conn: sqlite3.Connection, key: str) -> str | None:
    """文字列の設定値を返す。未設定または文字列でなければ None。"""
    value = get_value(conn, key)
    return value if isinstance(value, str) and value else None


def get_analysis_params(conn: sqlite3.Connection) -> AnalysisParams:
    """保存済みの解析パラメータを返す。未設定の項目は既定値を使う。"""
    return _load_dataclass(conn, ANALYSIS_PARAMS_KEY, AnalysisParams)


def set_analysis_params(conn: sqlite3.Connection, params: AnalysisParams) -> None:
    """解析パラメータを検証して保存する。

    Raises:
        ValueError: 範囲外の値がある場合。
    """
    params.validate()
    set_value(conn, ANALYSIS_PARAMS_KEY, asdict(params))


def get_jev_settings(conn: sqlite3.Connection) -> JevSettings:
    """保存済みの Jev 設定を返す。未設定の項目は既定値を使う。"""
    return _load_dataclass(conn, JEV_SETTINGS_KEY, JevSettings)


def set_jev_settings(conn: sqlite3.Connection, settings: JevSettings) -> None:
    """Jev 設定を保存する。"""
    set_value(conn, JEV_SETTINGS_KEY, asdict(settings))


def _load_dataclass[T: (AnalysisParams, JevSettings)](conn: sqlite3.Connection, key: str, cls: type[T]) -> T:
    stored = get_value(conn, key)
    if not isinstance(stored, dict):
        return cls()
    # 古いバージョンで保存した未知のキーは無視する
    known = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in stored.items() if k in known})
