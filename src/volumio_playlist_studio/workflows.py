"""画面の操作から呼び出す一連の処理。GUI に依存せず、バックグラウンドスレッドから実行できる。

各関数は処理結果を画面表示用の日本語の文で返す。
"""

import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import httpx2
from typesafe_sdk import TypeSafeError

from volumio_playlist_studio.analyzer import analyze_pending, classify_with_rules
from volumio_playlist_studio.classify.jev import ENGINE_NAME as JEV_ENGINE
from volumio_playlist_studio.classify.jev import JevClassifier, JevResponseError, build_questions, create_client
from volumio_playlist_studio.classify.runner import RunLimits, classify_tracks, error_summary, fatal_reason
from volumio_playlist_studio.config import JEV_MOOD_QUESTION_VERSION, JEV_QUESTION_VERSION, MoodDecision
from volumio_playlist_studio.custom_mood import PositionUndeterminedError, estimate_mood, validate_input
from volumio_playlist_studio.jobs import JobControl, JobError
from volumio_playlist_studio.mood_map import DISPLAY_SCALE
from volumio_playlist_studio.scanner import scan_library
from volumio_playlist_studio.storage import analysis as analysis_store
from volumio_playlist_studio.storage import settings_store
from volumio_playlist_studio.storage.custom_moods import (
    CustomMoodDraft,
    DuplicateNameError,
    list_custom_moods,
    save_custom_mood,
)
from volumio_playlist_studio.storage.database import transaction, utc_now
from volumio_playlist_studio.storage.playlists import RegistrationRecord, save_registration
from volumio_playlist_studio.storage.volumio_library import replace_uris
from volumio_playlist_studio.volumio import PlaylistExistsError, VolumioError, collect_track_uris, register_playlist
from volumio_playlist_studio.volumio_rest import VolumioRestClient
from volumio_playlist_studio.volumio_socketio import VolumioLiveClient

logger = logging.getLogger(__name__)


# 自由記述の雰囲気が、7 つの属性で表せない要素を含む可能性が高い場合の注意（画面表示用）
UNEXPRESSIBLE_NOTE = (
    "説明文には、年代・ボーカル・楽器など、明るさやエネルギー感などの属性では表せない要素が含まれている"
    "可能性があります。それらは選曲に反映されません。"
)


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


@dataclass(frozen=True)
class VolumioRegistrationRequest:
    """Volumio 実機へのプレイリスト登録の依頼。

    Attributes:
        host: Volumio の接続先。
        playlist_id: 登録するアプリ側のプレイリストの ID。
        name: Volumio に作成するプレイリスト名。
        entries: （楽曲 ID, URI）の並び。URI が対応しない曲は None。
    """

    host: str
    playlist_id: int
    name: str
    entries: list[tuple[int, str | None]]


def run_scan(conn: sqlite3.Connection, root: Path, control: JobControl) -> str:
    """音楽フォルダをスキャンし、ルールベース分類を更新する。"""
    summary = scan_library(conn, root, control)
    classify_with_rules(conn, root.as_posix())
    message = (
        f"追加 {summary.added} 曲、更新 {summary.updated} 曲、変更なし {summary.unchanged} 曲、"
        f"見つからなくなった曲 {summary.missing} 曲、エラー {summary.errors} 件（{summary.elapsed_sec:.1f} 秒）"
    )
    if summary.unreadable_dirs:
        message += (
            f" 読めないフォルダが {summary.unreadable_dirs} 件あったため、見つからなくなった曲の判定は行っていません。"
            "共有フォルダの接続と権限を確認して、もう一度スキャンしてください。"
        )

    return message


def run_analysis(conn: sqlite3.Connection, root: Path, control: JobControl, *, retry_failed: bool = False) -> str:
    """未解析の楽曲を音響解析する。retry_failed を指定すると、前回失敗した楽曲も再試行する。"""
    summary = analyze_pending(conn, root, settings_store.get_analysis_params(conn), control, retry_failed=retry_failed)
    per_track = summary.seconds_per_track
    timing = "" if per_track is None else f"、1 曲あたり平均 {per_track:.1f} 秒"
    cancelled = "（中断しました）" if summary.cancelled else ""
    message = (
        f"解析 {summary.analyzed} 曲、失敗 {summary.failed} 曲、未対応形式 {summary.unsupported} 曲"
        f"（合計 {summary.elapsed_sec:.1f} 秒{timing}）{cancelled}"
    )
    if summary.skipped_failed:
        message += (
            f" 前回失敗した {summary.skipped_failed} 曲は再試行していません（「失敗した曲を再試行」で再試行できます）。"
        )

    return message


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


def run_custom_mood(
    conn: sqlite3.Connection,
    name: str,
    description: str,
    transport: httpx2.BaseTransport | None = None,
) -> str:
    """説明文から Jev で雰囲気マップ上の位置と属性の値を決め、自由記述の雰囲気として保存する。

    Jev へのリクエストは 1 回（SDK の自動再試行を除く）。名前の重複など、入力で分かる問題は API を呼ぶ前に確認する。

    Args:
        conn: DB 接続。
        name: マップに書く名前。
        description: 雰囲気の説明文。Jev に送るのはこの文だけ。
        transport: HTTP の送受信を差し替える場合に指定する（テスト用）。

    Raises:
        JobError: 入力が不正、名前が重複、または Jev の応答から位置を決められなかった場合（画面表示用のメッセージ）。
        JevUnavailableError: API キーが設定されていない場合。
    """
    try:
        name, description = validate_input(name, description)
    except ValueError as exc:
        raise JobError("名前と説明文を入力してください（長すぎる場合は短くしてください）。") from exc

    if any(mood.name == name for mood in list_custom_moods(conn)):
        raise JobError(f"「{name}」という名前の雰囲気は既にあります。別の名前にしてください。")

    settings = settings_store.get_jev_settings(conn)
    with create_client(settings, transport=transport) as client:
        try:
            estimate = estimate_mood(client, description, settings)
        except TypeSafeError as exc:
            # 応答本文には送信した説明文が含まれる可能性があるため、種類とステータスだけを残す
            logger.warning("custom mood request failed: error=%s", error_summary(exc))
            reason = fatal_reason(exc) or "Jev との通信に失敗しました。しばらく待ってから、もう一度試してください。"
            # ジョブの境界（jobs.BackgroundJob）は原因の例外を repr でログに出す。SDK の例外の repr には
            # 応答本文が含まれるため、原因には繋がない（種類とステータスは上でログに残した）
            raise JobError(reason) from None
        except JevResponseError as exc:
            raise JobError("Jev の応答の形式が想定と異なるため、雰囲気を作れませんでした。") from exc
        except PositionUndeterminedError as exc:
            raise JobError(
                "Jev が明るさかエネルギー感を決められませんでした（確信度が低い回答）。"
                "説明文をより具体的にして、もう一度試してください。"
            ) from exc

    try:
        with transaction(conn):
            save_custom_mood(
                conn,
                CustomMoodDraft(
                    name=name,
                    description=description,
                    x=estimate.point.x,
                    y=estimate.point.y,
                    attributes=estimate.attributes,
                    expressible=estimate.expressible,
                    question_version=JEV_MOOD_QUESTION_VERSION,
                    model=estimate.model,
                ),
            )
    except DuplicateNameError as exc:
        # 別のタブなどで、問い合わせ中に同じ名前の雰囲気が作られた場合
        raise JobError(f"「{name}」という名前の雰囲気は既にあります。別の名前にしてください。") from exc

    logger.info(
        "custom mood created: question_version=%s input_tokens=%d output_tokens=%d",
        JEV_MOOD_QUESTION_VERSION,
        estimate.input_tokens,
        estimate.output_tokens,
    )
    message = (
        f"「{name}」をマップに追加しました（明るさ {estimate.point.x * DISPLAY_SCALE:.1f}・"
        f"エネルギー感 {estimate.point.y * DISPLAY_SCALE:.1f}、"
        f"入力 {estimate.input_tokens} / 出力 {estimate.output_tokens} トークン）。"
    )
    if estimate.expressible < settings.expressible_threshold:
        message += f" {UNEXPRESSIBLE_NOTE}"

    return message


def run_volumio_sync(conn: sqlite3.Connection, host: str, control: JobControl) -> str:
    """Volumio のライブラリから曲の URI の一覧を取得して保存する。Volumio の状態は変更しない。

    Raises:
        VolumioError: 通信に失敗した場合、または中断した場合。
        ValueError: 接続先の形式が不正な場合。
    """
    uris = collect_track_uris(VolumioRestClient(host, should_stop=control.is_cancelled), control=control)
    with transaction(conn):
        replace_uris(conn, host, uris)

    return f"Volumio のライブラリから {len(uris)} 曲の情報を取得しました。"


def run_volumio_register(conn: sqlite3.Connection, request: VolumioRegistrationRequest, control: JobControl) -> str:
    """Volumio 実機に新しいプレイリストを作成して曲を追加し、結果を登録履歴に保存する。

    既存のプレイリストは変更しない。プレイリストの作成後の結果（一部のみ登録・失敗・中断を含む）は、
    曲ごとに登録履歴へ保存してから返す。

    Args:
        conn: アプリの DB 接続。
        request: 登録の依頼。
        control: 進捗通知と中断の受け付け。

    Raises:
        JobError: 登録しなかった場合、またはすべての曲を登録できなかった場合（画面表示用のメッセージ）。
        ValueError: 接続先の形式が不正な場合。
    """
    started_at = utc_now()
    # 接続先の形式の誤りは設定の問題のため、ここでは捕捉しない
    name = request.name.strip()
    client = VolumioLiveClient(request.host, should_stop=control.is_cancelled)
    try:
        with client:
            outcome = register_playlist(client, name, request.entries, control)
    except PlaylistExistsError as exc:
        raise JobError("同じ名前のプレイリストが Volumio に既にあるため、登録しませんでした。") from exc
    except ValueError as exc:
        raise JobError(
            "プレイリスト名が空か使えない文字（/ や \\ など）を含むか、登録できる曲がないため、登録しませんでした。"
        ) from exc
    except VolumioError as exc:
        raise JobError(f"Volumio との通信に失敗したため、登録しませんでした: {exc}") from exc

    with transaction(conn):
        save_registration(
            conn,
            RegistrationRecord(
                request.playlist_id, name, client.kind, outcome.status, outcome.error, started_at, outcome.items
            ),
        )

    added = sum(1 for i in outcome.items if i.status == "added")
    if outcome.status != "success":
        raise JobError(f"登録が完了しませんでした（{added}/{len(outcome.items)} 曲を登録）。{outcome.error or ''}")

    return f"Volumio にプレイリスト「{name}」を作成し、{added} 曲を登録しました。"
