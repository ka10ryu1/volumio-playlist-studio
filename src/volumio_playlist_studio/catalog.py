"""DB に保存した楽曲・解析結果・評価を、画面や選曲エンジンが使う形にまとめる。"""

import sqlite3
from collections import Counter
from dataclasses import dataclass

from volumio_playlist_studio.albums import album_key, group_albums
from volumio_playlist_studio.analyzer import is_analyzable
from volumio_playlist_studio.classify.genre_tags import genre_from_tag
from volumio_playlist_studio.classify.jev import ENGINE_NAME as JEV_ENGINE
from volumio_playlist_studio.classify.rules import ENGINE_NAME as RULES_ENGINE
from volumio_playlist_studio.config import ANALYSIS_VERSION, JEV_ALBUM_QUESTION_VERSION
from volumio_playlist_studio.models import GENRE_LABELS, ClassificationResult, ClassificationSource, Genre, Track
from volumio_playlist_studio.playlist import Candidate
from volumio_playlist_studio.storage import analysis as analysis_store
from volumio_playlist_studio.storage import settings_store
from volumio_playlist_studio.storage.album_judgments import load_children_probabilities
from volumio_playlist_studio.storage.evaluations import Verdict, load_labels
from volumio_playlist_studio.storage.playlists import track_usage_counts
from volumio_playlist_studio.storage.tracks import last_scan_at, list_tracks

UNKNOWN_GENRE_LABEL = "不明"


@dataclass(frozen=True)
class GenreTagCount:
    """ジャンルタグの表記 1 つと、それを持つ曲数。

    Attributes:
        tag: 前後の空白を除いた表記。タグが空の曲は空文字列。
        genre: 現在の照合規則で当てはまるジャンル。当てはまらなければ None。
    """

    tag: str
    genre: Genre | None
    tracks: int


def genre_tag_counts(tracks: list[Track]) -> list[GenreTagCount]:
    """ジャンルタグの表記ごとに、当てはまるジャンルと曲数を、曲数の多い順に返す。

    保存済みの分類結果ではなく現在の照合規則で求めるため、規則を変えた直後でも、スキャンし直す前に
    どの表記が当てはまるようになったかを確かめられる。
    """
    counts = Counter((t.genre or "").strip() for t in tracks)
    return [
        GenreTagCount(tag, genre_from_tag(tag), n) for tag, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]


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
    """選曲の候補を読み込む。人手で修正したジャンルがあれば、分類結果より優先する。

    子供向けかどうかは、現在の質問バージョンでのアルバム単位の判定を、設定の閾値と比べて決める。
    """
    classifications = effective_classifications(conn, source)
    labels = load_labels(conn)
    usage = track_usage_counts(conn)
    children = load_children_probabilities(conn, library_root, JEV_ALBUM_QUESTION_VERSION)
    threshold = settings_store.get_jev_settings(conn).children_threshold
    candidates = []
    for track in list_tracks(conn, library_root):
        classification = classifications.get(track.id)
        genre = None if classification is None else classification.genre
        label = labels.get(track.id)
        if label is not None and label.genre_verdict in {Verdict.CORRECT, Verdict.INCORRECT}:
            genre = label.genre_label

        probability = children.get(album_key(track))
        for_children = None if probability is None else probability >= threshold
        candidates.append(Candidate(track, classification, genre, usage.get(track.id, 0), for_children))

    return candidates


@dataclass(frozen=True)
class JudgedAlbum:
    """Jev で判定済みのアルバム。画面で判定結果を確認するために使う。

    Attributes:
        title: アルバム名。タグがなければフォルダ名。
        artist: 収録曲で最も多いアーティスト名。不明なら None。
        children_probability: 子供向けのアルバムである確率（0〜1）。
    """

    title: str
    artist: str | None
    track_count: int
    children_probability: float


def judged_albums(conn: sqlite3.Connection, library_root: str) -> list[JudgedAlbum]:
    """現在の質問バージョンで判定済みのアルバムを、子供向けである確率の高い順に返す。"""
    probabilities = load_children_probabilities(conn, library_root, JEV_ALBUM_QUESTION_VERSION)
    result = []
    for key, tracks in group_albums(list_tracks(conn, library_root)).items():
        probability = probabilities.get(key)
        if probability is None:
            continue

        first = tracks[0]
        artists = Counter(t.artist for t in tracks if t.artist)
        result.append(
            JudgedAlbum(
                title=first.album or first.rel_path.rpartition("/")[0] or "（ライブラリ直下）",
                artist=artists.most_common(1)[0][0] if artists else None,
                track_count=len(tracks),
                children_probability=probability,
            )
        )

    return sorted(result, key=lambda a: (-a.children_probability, a.title))


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
