"""未解析の楽曲に音響解析を実行し、結果を DB に保存する。

解析済みでファイルが更新されていない楽曲は再解析しない。1 曲の解析に失敗してもエラーを記録して続行する。
解析した楽曲には、外部 API を使わないルールベース分類も続けて実行する。
"""

import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import soundfile
from librosa.util.exceptions import LibrosaError

from volumio_playlist_studio.classify.rules import RuleClassifier
from volumio_playlist_studio.classify.runner import ClassificationRunSummary, RunLimits, classify_tracks
from volumio_playlist_studio.config import ANALYSIS_VERSION, ANALYZABLE_EXTENSIONS, AnalysisParams
from volumio_playlist_studio.features import FeatureExtractionError, extract_features
from volumio_playlist_studio.jobs import JobControl
from volumio_playlist_studio.library_paths import redact_paths
from volumio_playlist_studio.models import Track
from volumio_playlist_studio.storage import analysis as analysis_store
from volumio_playlist_studio.storage.database import transaction
from volumio_playlist_studio.storage.tracks import list_tracks

logger = logging.getLogger(__name__)

# 解析で起こりうる失敗。壊れたファイル・未対応形式（soundfile）、読み取り失敗（OSError）、
# 無音・短すぎる音声（FeatureExtractionError）、librosa の引数・計算エラー
_ANALYSIS_ERRORS = (FeatureExtractionError, soundfile.LibsndfileError, OSError, LibrosaError, ValueError)


@dataclass(frozen=True)
class AnalysisRunSummary:
    """音響解析の実行結果。

    Attributes:
        unsupported: 音響解析に未対応の形式のため、解析しなかった曲数。
        cancelled: 途中で中断したか。
    """

    analyzed: int
    failed: int
    unsupported: int
    elapsed_sec: float
    cancelled: bool

    @property
    def seconds_per_track(self) -> float | None:
        """解析に成功・失敗した 1 曲あたりの平均処理時間。"""
        processed = self.analyzed + self.failed
        return None if processed == 0 else self.elapsed_sec / processed


def is_analyzable(track: Track) -> bool:
    """音響解析に対応した形式かどうか。"""
    return PurePosixPath(track.rel_path).suffix.lower() in ANALYZABLE_EXTENSIONS


def analyze_pending(
    conn: sqlite3.Connection,
    root: Path,
    params: AnalysisParams,
    control: JobControl | None = None,
) -> AnalysisRunSummary:
    """未解析の楽曲を解析する。

    Args:
        conn: DB 接続。
        root: 音楽ライブラリのルート（絶対パス）。
        params: 解析パラメータ。
        control: 進捗通知と中断の受け付け。

    Returns:
        実行結果。
    """
    control = control or JobControl()
    params.validate()
    started = time.monotonic()
    pending = analysis_store.tracks_needing_analysis(conn, root.as_posix(), ANALYSIS_VERSION)
    targets = [t for t in pending if is_analyzable(t)]
    rules = RuleClassifier()
    analyzed = failed = 0
    cancelled = False
    logger.info("analysis started: targets=%d unsupported=%d", len(targets), len(pending) - len(targets))

    for index, track in enumerate(targets):
        if control.is_cancelled():
            cancelled = True
            break

        if _analyze_one(conn, root, track, params, rules):
            analyzed += 1
        else:
            failed += 1

        control.report(index + 1, len(targets))

    summary = AnalysisRunSummary(
        analyzed=analyzed,
        failed=failed,
        unsupported=len(pending) - len(targets),
        elapsed_sec=time.monotonic() - started,
        cancelled=cancelled,
    )
    logger.info(
        "analysis finished: analyzed=%d failed=%d unsupported=%d cancelled=%s elapsed_sec=%.1f",
        summary.analyzed,
        summary.failed,
        summary.unsupported,
        summary.cancelled,
        summary.elapsed_sec,
    )
    return summary


def _analyze_one(
    conn: sqlite3.Connection, root: Path, track: Track, params: AnalysisParams, rules: RuleClassifier
) -> bool:
    track_started = time.monotonic()
    try:
        features = extract_features(root / track.rel_path, params)
    except _ANALYSIS_ERRORS as exc:
        # 外部ライブラリのメッセージには絶対パスが含まれることがあるため、ルートを取り除いてから記録する
        message = redact_paths(f"{type(exc).__name__}: {exc}", [root])
        with transaction(conn):
            analysis_store.record_error(
                conn, track.id, analysis_store.ErrorRecord("features", type(exc).__name__, message, ANALYSIS_VERSION)
            )

        logger.warning("analysis failed: path=%s error=%s", track.rel_path, type(exc).__name__)
        return False

    elapsed = time.monotonic() - track_started
    with transaction(conn):
        analysis_store.save_features(
            conn, track, features, analysis_store.FeatureRun(params, ANALYSIS_VERSION, elapsed)
        )
        analysis_store.save_classification(conn, track.id, rules.classify(track, features))

    logger.debug("analysis succeeded: path=%s elapsed_sec=%.2f", track.rel_path, elapsed)
    return True


def classify_with_rules(conn: sqlite3.Connection, library_root: str) -> ClassificationRunSummary:
    """ライブラリの全楽曲をルールベースで分類し直す。音響特徴量がない楽曲はタグ情報だけで分類する。

    外部 API を使わず短時間で終わるため、タグの更新にも追従できるよう毎回全曲を対象にする。
    """
    tracks = list_tracks(conn, library_root)
    features = analysis_store.load_features(conn, [t.id for t in tracks])
    return classify_tracks(conn, RuleClassifier(), [(t, features.get(t.id)) for t in tracks], RunLimits())
