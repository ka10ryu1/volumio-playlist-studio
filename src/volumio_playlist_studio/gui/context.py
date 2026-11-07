"""各画面で共有する、設定・DB 接続・バックグラウンド処理の扱い。"""

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

from volumio_playlist_studio.config import AppConfig, load_app_config
from volumio_playlist_studio.jobs import BackgroundJob, JobControl, JobState
from volumio_playlist_studio.models import ClassificationSource
from volumio_playlist_studio.sample_data import DEMO_LIBRARY_ROOT
from volumio_playlist_studio.storage import settings_store
from volumio_playlist_studio.storage.database import connect

_JOBS_KEY = "jobs"
_LOG_MAX_BYTES = 1_000_000
_LOG_BACKUPS = 3


@dataclass(frozen=True)
class PageContext:
    """画面の描画に必要な情報。

    Attributes:
        conn: この再実行の間だけ使う DB 接続。
        library_root: 現在の音楽ライブラリのルート。未設定なら None。
    """

    config: AppConfig
    conn: sqlite3.Connection
    library_root: str | None

    @property
    def is_demo(self) -> bool:
        """サンプルデータを表示しているか。"""
        return self.library_root == DEMO_LIBRARY_ROOT

    @property
    def classification_source(self) -> ClassificationSource:
        """選曲に使う分類結果の種類。"""
        value = settings_store.get_str(self.conn, settings_store.CLASSIFICATION_SOURCE_KEY)
        try:
            return ClassificationSource(value) if value else ClassificationSource.JEV_THEN_RULES
        except ValueError:
            return ClassificationSource.JEV_THEN_RULES


def configure_logging(log_path: Path) -> None:
    """ファイルへのログ出力を設定する。同じパスに対して何度呼んでもハンドラを重複させない。"""
    root = logging.getLogger()
    if any(getattr(h, "baseFilename", None) == str(log_path.resolve()) for h in root.handlers):
        return
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(log_path, maxBytes=_LOG_MAX_BYTES, backupCount=_LOG_BACKUPS, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root.addHandler(handler)
    root.setLevel(logging.INFO)


def open_context() -> PageContext:
    """環境変数を読み込み、DB に接続して画面の描画に必要な情報をまとめる。"""
    # .env の値は既存の環境変数を上書きしない
    load_dotenv(override=False)
    config = load_app_config()
    configure_logging(config.log_path)
    conn = connect(config.database_path)
    return PageContext(config, conn, settings_store.get_str(conn, settings_store.MUSIC_ROOT_KEY))


def jobs() -> dict[str, BackgroundJob]:
    """このセッションのバックグラウンド処理。"""
    if _JOBS_KEY not in st.session_state:
        st.session_state[_JOBS_KEY] = {}
    result: dict[str, BackgroundJob] = st.session_state[_JOBS_KEY]
    return result


def running_job() -> BackgroundJob | None:
    """実行中のバックグラウンド処理を返す。"""
    return next((j for j in jobs().values() if j.state == JobState.RUNNING), None)


def start_job(name: str, database_path: Path, work: Callable[[sqlite3.Connection, JobControl], object]) -> None:
    """バックグラウンド処理を開始する。処理は専用の DB 接続を使う。"""

    def target(control: JobControl) -> object:
        conn = connect(database_path)
        try:
            return work(conn, control)
        finally:
            conn.close()

    job = BackgroundJob(name=name)
    jobs()[name] = job
    job.start(target)


@st.fragment(run_every=1.0)
def job_status_panel() -> None:
    """バックグラウンド処理の進捗を 1 秒ごとに更新して表示する。終了したら画面全体を更新する。"""
    for name, job in list(jobs().items()):
        if job.state == JobState.RUNNING:
            st.progress(job.fraction, text=f"{name} を実行中… {job.done}/{job.total or '?'}")
            if st.button("中断", key=f"cancel-{name}"):
                job.cancel_event.set()
            continue

        del jobs()[name]
        if job.state == JobState.FAILED:
            st.session_state["job_message"] = ("error", f"{name}: {job.error}")
        else:
            summary = job.result if isinstance(job.result, str) else ""
            st.session_state["job_message"] = ("success", f"{name} が完了しました。{summary}")
        st.rerun()


def show_jobs() -> None:
    """バックグラウンド処理があれば進捗を表示する。"""
    if jobs():
        job_status_panel()


def show_job_message() -> None:
    """直前に終了したバックグラウンド処理の結果を表示する。"""
    message = st.session_state.pop("job_message", None)
    if message is None:
        return
    kind, text = message
    if kind == "error":
        st.error(text)
    else:
        st.success(text)
