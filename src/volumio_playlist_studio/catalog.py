"""DB に保存した楽曲・解析結果・評価を、画面や選曲エンジンが使う形にまとめる。"""

import sqlite3
from collections import Counter
from dataclasses import dataclass

from volumio_playlist_studio.analyzer import is_analyzable
from volumio_playlist_studio.classify.jev import ENGINE_NAME as JEV_ENGINE
from volumio_playlist_studio.classify.rules import ENGINE_NAME as RULES_ENGINE
from volumio_playlist_studio.config import ANALYSIS_VERSION
from volumio_playlist_studio.models import GENRE_LABELS, ClassificationResult, ClassificationSource
from volumio_playlist_studio.playlist import Candidate
from volumio_playlist_studio.storage import analysis as analysis_store
from volumio_playlist_studio.storage.evaluations import Verdict, load_labels
from volumio_playlist_studio.storage.playlists import track_usage_counts
from volumio_playlist_studio.storage.tracks import last_scan_at, list_tracks

UNKNOWN_GENRE_LABEL = "不明"


@dataclass(frozen=True)
class DashboardStats:
    """ダッシュボードに表示する集計値。

    Attributes:
        unanalyzed: 音響解析に対応した形式で、未解析（または解析後にファイルが更新された）曲数。
            解析に失敗した曲は含めない。
        failed: 同じ解析バージョンで解析に失敗し、その後ファイルが変わっていない曲数。
        unsupported: 音響解析に未対応の形式（M4A など）の曲数。
        genre_distribution: 選曲に使うジャンルごとの曲数。
    """

    total: int
    analyzed: int
    unanalyzed: int
    failed: int
    unsupported: int
    jev_classified: int
    error_count: int
    last_scan_at: str | None
    genre_distribution: dict[str, int]


def effective_classifications(
    conn: sqlite3.Connection, source: ClassificationSource
) -> dict[int, ClassificationResult]:
    """選曲に使う分類結果を楽曲 ID ごとに返す。各エンジンについて楽曲ごとに最新の結果を使う。"""
    rules = {k: v.result for k, v in analysis_store.load_classifications(conn, RULES_ENGINE).items()}
    if source == ClassificationSource.RULES:
        return rules

    jev = {k: v.result for k, v in analysis_store.load_classifications(conn, JEV_ENGINE).items()}
    if source == ClassificationSource.JEV:
        return jev

    return {**rules, **jev}


def load_candidates(conn: sqlite3.Connection, library_root: str, source: ClassificationSource) -> list[Candidate]:
    """選曲の候補を読み込む。人手で修正したジャンルがあれば、分類結果より優先する。"""
    classifications = effective_classifications(conn, source)
    labels = load_labels(conn)
    usage = track_usage_counts(conn)
    candidates = []
    for track in list_tracks(conn, library_root):
        classification = classifications.get(track.id)
        genre = None if classification is None else classification.genre
        label = labels.get(track.id)
        if label is not None and label.genre_verdict in {Verdict.CORRECT, Verdict.INCORRECT}:
            genre = label.genre_label

        candidates.append(Candidate(track, classification, genre, usage.get(track.id, 0)))

    return candidates


def dashboard_stats(conn: sqlite3.Connection, library_root: str, source: ClassificationSource) -> DashboardStats:
    """ダッシュボードの集計値を求める。"""
    tracks = list_tracks(conn, library_root)
    pending_ids = {t.id for t in analysis_store.tracks_needing_analysis(conn, library_root, ANALYSIS_VERSION)}
    unsupported = sum(1 for t in tracks if not is_analyzable(t))
    failed_ids = analysis_store.failed_track_ids(conn, library_root, ANALYSIS_VERSION)
    failed = sum(1 for t in tracks if t.id in pending_ids and t.id in failed_ids and is_analyzable(t))
    unanalyzed = sum(1 for t in tracks if t.id in pending_ids and t.id not in failed_ids and is_analyzable(t))
    track_ids = {t.id for t in tracks}
    jev = analysis_store.load_classifications(conn, JEV_ENGINE)
    genres = Counter(
        UNKNOWN_GENRE_LABEL if c.genre is None else GENRE_LABELS[c.genre]
        for c in load_candidates(conn, library_root, source)
    )
    return DashboardStats(
        total=len(tracks),
        analyzed=len(tracks) - unanalyzed - failed - unsupported,
        unanalyzed=unanalyzed,
        failed=failed,
        unsupported=unsupported,
        jev_classified=sum(1 for track_id in jev if track_id in track_ids),
        error_count=len(analysis_store.list_errors(conn, library_root)),
        last_scan_at=last_scan_at(conn, library_root),
        genre_distribution=dict(genres.most_common()),
    )
