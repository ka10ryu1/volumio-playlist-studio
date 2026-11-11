"""分類エンジンを複数の楽曲に対して実行し、結果を DB に保存する。

1 曲の失敗では処理を止めず、エラーを記録して次の曲に進む。ただし、認証エラーやレート制限のように
続けても成功しない失敗、または連続した失敗が続いた場合は、無駄なリクエストを避けるため打ち切る。
"""

import logging
import sqlite3
import time
from dataclasses import dataclass

from typesafe_sdk import (
    TypeSafeAPIError,
    TypeSafeAuthenticationError,
    TypeSafeError,
    TypeSafePermissionDeniedError,
    TypeSafeRateLimitError,
)

from volumio_playlist_studio.classify.base import Classifier
from volumio_playlist_studio.classify.jev import JevResponseError
from volumio_playlist_studio.jobs import JobControl
from volumio_playlist_studio.models import AudioFeatures, Track
from volumio_playlist_studio.storage.analysis import ErrorRecord, record_error, save_classification
from volumio_playlist_studio.storage.database import transaction

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RunLimits:
    """1 回の実行の上限。

    Attributes:
        max_tracks: 処理する曲数の上限。None なら上限なし（外部 API を使わないエンジン向け）。
        max_consecutive_failures: 連続してこの回数失敗したら打ち切る。
    """

    max_tracks: int | None = None
    max_consecutive_failures: int = 3


@dataclass(frozen=True)
class ClassificationRunSummary:
    """分類の実行結果。

    Attributes:
        stopped_reason: 途中で打ち切った場合の理由（画面表示用の日本語）。最後まで処理した場合は None。
    """

    classified: int
    failed: int
    remaining: int
    stopped_reason: str | None
    elapsed_sec: float


def fatal_reason(exc: Exception) -> str | None:
    """続けても成功しない失敗なら、画面に表示する理由を返す。"""
    if isinstance(exc, TypeSafeAuthenticationError | TypeSafePermissionDeniedError):
        return "API キーが無効、または権限がありません。設定を確認してください。"

    if isinstance(exc, TypeSafeRateLimitError):
        return "レート制限に達しました。しばらく待ってから再実行してください。"

    return None


def error_summary(exc: Exception) -> str:
    """ログ・エラー記録用に、例外の種類と HTTP ステータスだけを返す。"""
    # 応答本文には送信した state が含まれる可能性があるため、記録するのは種類とステータスだけにする
    if isinstance(exc, TypeSafeAPIError):
        return f"{type(exc).__name__}: status={exc.status}"

    return type(exc).__name__


def classify_tracks(
    conn: sqlite3.Connection,
    classifier: Classifier,
    items: list[tuple[Track, AudioFeatures | None]],
    limits: RunLimits,
    control: JobControl | None = None,
) -> ClassificationRunSummary:
    """楽曲を分類し、結果を DB に保存する。

    Args:
        conn: DB 接続。
        classifier: 分類エンジン。
        items: 対象の楽曲と音響特徴量の組。特徴量がない楽曲はタグ情報だけで分類する。
        limits: 実行の上限。
        control: 進捗通知と中断の受け付け。

    Returns:
        実行結果。
    """
    control = control or JobControl()
    started = time.monotonic()
    targets = items if limits.max_tracks is None else items[: limits.max_tracks]
    classified = failed = consecutive_failures = 0
    stopped_reason: str | None = None

    for index, (track, features) in enumerate(targets):
        if control.is_cancelled():
            stopped_reason = "ユーザーが中断しました。"
            break

        try:
            result = classifier.classify(track, features)
        except (TypeSafeError, JevResponseError) as exc:
            failed += 1
            consecutive_failures += 1
            error = ErrorRecord("classification", type(exc).__name__, error_summary(exc), classifier.engine_version)
            with transaction(conn):
                record_error(conn, track.id, error)

            logger.warning(
                "classification failed: engine=%s track_id=%d error=%s", classifier.engine, track.id, error.message
            )
            stopped_reason = fatal_reason(exc)
            if stopped_reason is None and consecutive_failures >= limits.max_consecutive_failures:
                stopped_reason = (
                    f"{consecutive_failures} 回連続で失敗したため中断しました。通信状況を確認してください。"
                )

            if stopped_reason is not None:
                break
        else:
            consecutive_failures = 0
            classified += 1
            with transaction(conn):
                save_classification(conn, track.id, result)

        control.report(index + 1, len(targets))

    summary = ClassificationRunSummary(
        classified=classified,
        failed=failed,
        remaining=len(items) - classified,
        stopped_reason=stopped_reason,
        elapsed_sec=time.monotonic() - started,
    )
    logger.info(
        "classification finished: engine=%s classified=%d failed=%d remaining=%d stopped=%s",
        classifier.engine,
        summary.classified,
        summary.failed,
        summary.remaining,
        stopped_reason is not None,
    )
    return summary
