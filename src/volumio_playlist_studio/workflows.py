"""画面の操作から呼び出す一連の処理。GUI に依存せず、バックグラウンドスレッドから実行できる。

各関数は処理結果を画面表示用の日本語の文で返す。
"""

import sqlite3
from dataclasses import dataclass
from pathlib import Path

import httpx2

from volumio_playlist_studio.analyzer import analyze_pending, classify_with_rules
from volumio_playlist_studio.classify.jev import ENGINE_NAME as JEV_ENGINE
from volumio_playlist_studio.classify.jev import JevClassifier, build_questions, create_client
from volumio_playlist_studio.classify.runner import RunLimits, classify_tracks
from volumio_playlist_studio.config import JEV_QUESTION_VERSION, MoodDecision
from volumio_playlist_studio.jobs import JobControl
from volumio_playlist_studio.scanner import scan_library
from volumio_playlist_studio.storage import analysis as analysis_store
from volumio_playlist_studio.storage import settings_store


@dataclass(frozen=True)
class JevEstimate:
    """Jev の実行前に表示する見積もり。

    Attributes:
        pending_tracks: 現在の質問定義で未判定の曲数。
        tracks_this_run: 今回の実行で判定する曲数（1 回の上限を適用後）。
        requests_this_run: 今回の実行で送るリクエスト数（1 曲 1 リクエスト。SDK の自動再試行は含まない）。
        questions_per_request: 1 リクエストあたりの質問数。
    """

    pending_tracks: int
    tracks_this_run: int
    requests_this_run: int
    questions_per_request: int


def run_scan(conn: sqlite3.Connection, root: Path, control: JobControl) -> str:
    """音楽フォルダをスキャンし、ルールベース分類を更新する。"""
    summary = scan_library(conn, root, control)
    classify_with_rules(conn, root.as_posix())
    return (
        f"追加 {summary.added} 曲、更新 {summary.updated} 曲、変更なし {summary.unchanged} 曲、"
        f"見つからなくなった曲 {summary.missing} 曲、エラー {summary.errors} 件（{summary.elapsed_sec:.1f} 秒）"
    )


def run_analysis(conn: sqlite3.Connection, root: Path, control: JobControl) -> str:
    """未解析の楽曲を音響解析する。"""
    summary = analyze_pending(conn, root, settings_store.get_analysis_params(conn), control)
    per_track = summary.seconds_per_track
    timing = "" if per_track is None else f"、1 曲あたり平均 {per_track:.1f} 秒"
    cancelled = "（中断しました）" if summary.cancelled else ""
    return (
        f"解析 {summary.analyzed} 曲、失敗 {summary.failed} 曲、未対応形式 {summary.unsupported} 曲"
        f"（合計 {summary.elapsed_sec:.1f} 秒{timing}）{cancelled}"
    )


def estimate_jev(conn: sqlite3.Connection, library_root: str) -> JevEstimate:
    """Jev による判定の対象曲数とリクエスト数を見積もる。API は呼ばない。"""
    settings = settings_store.get_jev_settings(conn)
    pending = len(analysis_store.tracks_without_classification(conn, library_root, JEV_ENGINE, JEV_QUESTION_VERSION))
    this_run = min(pending, settings.max_tracks_per_run)
    return JevEstimate(pending, this_run, this_run, len(build_questions()))


def run_jev(
    conn: sqlite3.Connection,
    library_root: str,
    control: JobControl,
    transport: httpx2.BaseTransport | None = None,
) -> str:
    """未判定の楽曲を、1 回の上限の曲数まで Jev で判定する。

    Args:
        conn: DB 接続。
        library_root: 音楽ライブラリのルート。
        control: 進捗通知と中断の受け付け。
        transport: HTTP の送受信を差し替える場合に指定する（テスト用）。

    Raises:
        JevUnavailableError: API キーが設定されていない場合。
    """
    settings = settings_store.get_jev_settings(conn)
    tracks = analysis_store.tracks_without_classification(conn, library_root, JEV_ENGINE, JEV_QUESTION_VERSION)
    features = analysis_store.load_features(conn, [t.id for t in tracks])
    with create_client(settings, transport=transport) as client:
        classifier = JevClassifier(client=client, settings=settings, mood_decision=MoodDecision())
        summary = classify_tracks(
            conn,
            classifier,
            [(t, features.get(t.id)) for t in tracks],
            RunLimits(settings.max_tracks_per_run, settings.max_consecutive_failures),
            control,
        )
    message = (
        f"判定 {summary.classified} 曲、失敗 {summary.failed} 曲、未判定の残り {summary.remaining} 曲"
        f"（入力 {classifier.input_tokens} / 出力 {classifier.output_tokens} トークン）"
    )
    if summary.stopped_reason is not None:
        message += f" {summary.stopped_reason}"
    return message
