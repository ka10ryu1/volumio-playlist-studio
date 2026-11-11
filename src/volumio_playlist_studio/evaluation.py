"""分類結果の精度評価。

人手評価（正解データ）と各分類エンジンの結果を比較し、一致率や判定保留の割合を求める。
評価データは追加時に「調整用（tuning）」と「検証用（holdout）」に分け、ルールの調整には調整用だけを使う。
検証用の指標で改善を確認することで、評価データへの過学習を防ぐ。
"""

import csv
import io
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass

import numpy as np

from volumio_playlist_studio.config import RATING_MAX, RATING_MIN, EvaluationDefaults
from volumio_playlist_studio.models import Attribute, ClassificationResult, Genre, Mood, Track
from volumio_playlist_studio.playlist import CreationMethod
from volumio_playlist_studio.storage.evaluations import EvaluationLabel, Split, Verdict
from volumio_playlist_studio.storage.playlists import SavedPlaylist

# CSV を表計算ソフトで開いたとき、数式として解釈される先頭文字
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
_TOP_CONFUSIONS = 5


@dataclass(frozen=True)
class LabelAgreement:
    """ジャンルまたはムードの一致の集計。

    Attributes:
        labeled: 人手の正解がある曲数（人手でも判断できなかった曲は除く）。
        decided: そのうち分類エンジンが判定保留にしなかった曲数。
        correct: 分類エンジンの判定が正解と一致した曲数。
        confusions: 誤判定の（正解, 判定）の組と件数。多い順。
    """

    labeled: int
    decided: int
    correct: int
    confusions: list[tuple[str, str, int]]

    @property
    def accuracy(self) -> float | None:
        """判定した曲のうち正解した割合。"""
        return None if self.decided == 0 else self.correct / self.decided

    @property
    def pending_rate(self) -> float | None:
        """正解がある曲のうち、分類エンジンが判定保留にした割合。"""
        return None if self.labeled == 0 else 1 - self.decided / self.labeled


@dataclass(frozen=True)
class EngineMetrics:
    """1 つの分類エンジン・バージョンの評価指標。

    Attributes:
        evaluated: 人手評価があり、このエンジンの分類結果もある曲数。
        attribute_mae: 属性ごとの（人手評価との平均絶対誤差, 比較した曲数）。人手評価は 0〜1 に換算して比較する。
    """

    engine: str
    split: Split | None
    evaluated: int
    genre: LabelAgreement
    mood: LabelAgreement
    attribute_mae: dict[Attribute, tuple[float, int]]


@dataclass(frozen=True)
class PlaylistFeedbackMetrics:
    """保存済みプレイリストへのフィードバックの集計。

    Attributes:
        source: 選曲に使った分類結果の種類。
        method: 作成方法（`CreationMethod` の値）。
    """

    source: str
    method: str
    playlists: int
    tracks: int
    unsuitable: int
    mean_satisfaction: float | None

    @property
    def unsuitable_rate(self) -> float | None:
        """フィードバック済みのプレイリストに含まれた曲のうち、不適切とされた割合。"""
        return None if self.tracks == 0 else self.unsuitable / self.tracks


def assign_splits(track_ids: list[int], seed: int, tuning_ratio: float) -> dict[int, Split]:
    """楽曲を調整用と検証用に分ける。同じ入力と seed からは常に同じ結果になる。"""
    rng = np.random.default_rng(seed)
    shuffled = [track_ids[i] for i in rng.permutation(len(track_ids))]
    tuning_count = round(len(shuffled) * tuning_ratio)
    return {
        track_id: Split.TUNING if index < tuning_count else Split.HOLDOUT for index, track_id in enumerate(shuffled)
    }


def select_evaluation_sample(
    tracks: list[Track], already_selected: set[int], size: int, seed: int, defaults: EvaluationDefaults
) -> dict[int, Split]:
    """評価対象に追加する楽曲を無作為に選び、調整用と検証用に分ける。

    Raises:
        ValueError: 件数が既定の範囲外の場合。
    """
    if not defaults.min_sample_size <= size <= defaults.max_sample_size:
        raise ValueError("sample size is out of range")

    pool = sorted(t.id for t in tracks if t.id not in already_selected)
    rng = np.random.default_rng(seed)
    picked = sorted(pool[i] for i in rng.choice(len(pool), size=min(size, len(pool)), replace=False))
    return assign_splits(picked, seed, defaults.tuning_ratio)


def initial_verdict[T: (Genre, Mood)](
    stored_verdict: Verdict | None, stored_label: T | None, predicted: T | None
) -> tuple[Verdict | None, T | None]:
    """保存済みの評価から、評価の入力欄の初期値（判定の正誤, 誤りの場合の正しい値）を決める。

    評価した後に分類結果が変わっていることがあるため、保存済みの正解と現在の判定を比べて決める。
    これにより、メモだけを直して保存しても人手の正解が上書きされない。

    Args:
        stored_verdict: 保存済みの判定の正誤。未評価なら None。
        stored_label: 保存済みの正解。
        predicted: 現在の判定。

    Returns:
        正解が現在の判定と同じなら（正しい, None）、違えば（誤り, 保存済みの正解）、判定保留なら（判定保留, None）。
        未評価なら（None, None）とし、うっかり「正しい」のまま保存されるのを防ぐ。
    """
    if stored_verdict is None:
        return None, None

    if stored_verdict == Verdict.PENDING or stored_label is None:
        return stored_verdict, None

    if stored_label == predicted:
        return Verdict.CORRECT, None

    return Verdict.INCORRECT, stored_label


def rating_to_unit(rating: int) -> float:
    """5 段階の人手評価を 0〜1 に換算する。"""
    return (rating - RATING_MIN) / (RATING_MAX - RATING_MIN)


def _agreement(pairs: Iterable[tuple[str | None, str | None]]) -> LabelAgreement:
    """（正解, 判定）の組から一致を集計する。正解が None の組は数えない。"""
    labeled = decided = correct = 0
    confusions: Counter[tuple[str, str]] = Counter()
    for truth, predicted in pairs:
        if truth is None:
            continue

        labeled += 1
        if predicted is None:
            continue

        decided += 1
        if truth == predicted:
            correct += 1
        else:
            confusions[(truth, predicted)] += 1

    return LabelAgreement(
        labeled=labeled,
        decided=decided,
        correct=correct,
        confusions=[(t, p, n) for (t, p), n in confusions.most_common(_TOP_CONFUSIONS)],
    )


def _truth(verdict: Verdict | None, label: Genre | Mood | None) -> str | None:
    if verdict in {Verdict.CORRECT, Verdict.INCORRECT} and label is not None:
        return label.value

    return None


def compute_engine_metrics(
    engine: str,
    labels: dict[int, EvaluationLabel],
    classifications: dict[int, ClassificationResult],
    split: Split | None,
) -> EngineMetrics:
    """分類エンジンの結果を人手評価と比較する。

    Args:
        engine: エンジン名（表示用）。
        labels: 楽曲 ID ごとの人手評価。
        classifications: 楽曲 ID ごとの、評価するエンジン・バージョンの分類結果。
        split: 対象の用途。None なら全件。
    """
    targets = [
        (label, classifications[track_id])
        for track_id, label in sorted(labels.items())
        if track_id in classifications and label.is_evaluated and (split is None or label.split == split)
    ]
    genre = _agreement(
        (_truth(lb.genre_verdict, lb.genre_label), None if c.genre is None else c.genre.value) for lb, c in targets
    )
    mood = _agreement(
        (_truth(lb.mood_verdict, lb.mood_label), None if c.mood is None else c.mood.value) for lb, c in targets
    )

    errors: dict[Attribute, list[float]] = {}
    for label, classification in targets:
        for attribute, rating in label.ratings.items():
            predicted = classification.attribute_value(attribute)
            if predicted is not None:
                errors.setdefault(attribute, []).append(abs(predicted - rating_to_unit(rating)))

    return EngineMetrics(
        engine=engine,
        split=split,
        evaluated=len(targets),
        genre=genre,
        mood=mood,
        attribute_mae={a: (sum(v) / len(v), len(v)) for a, v in errors.items()},
    )


def playlist_feedback_metrics(playlists: list[SavedPlaylist]) -> list[PlaylistFeedbackMetrics]:
    """分類結果の種類と作成方法の組ごとに、プレイリストへのフィードバックを集計する。

    満足度の入力がないプレイリストは除く。作成方法を記録する前に保存したプレイリストは、
    当時唯一の作成方法だった「雰囲気と長さで選ぶ」として数える。
    """
    groups: dict[tuple[str, str], list[SavedPlaylist]] = {}
    for playlist in playlists:
        if playlist.satisfaction is not None:
            method = str(playlist.conditions.get("method", CreationMethod.MOOD.value))
            groups.setdefault((playlist.classification_source, method), []).append(playlist)

    result = []
    for (source, method), items in sorted(groups.items()):
        satisfactions = [p.satisfaction for p in items if p.satisfaction is not None]
        result.append(
            PlaylistFeedbackMetrics(
                source=source,
                method=method,
                playlists=len(items),
                tracks=sum(len(p.entries) for p in items),
                unsuitable=sum(1 for p in items for e in p.entries if e.unsuitable),
                mean_satisfaction=sum(satisfactions) / len(satisfactions),
            )
        )

    return result


def _csv_safe(value: object) -> str:
    """表計算ソフトで数式として実行されないよう、危険な先頭文字の前に `'` を付ける。"""
    text = "" if value is None else str(value)
    return f"'{text}" if text.startswith(_FORMULA_PREFIXES) else text


def export_csv(
    tracks: dict[int, Track],
    labels: dict[int, EvaluationLabel],
    classifications: dict[str, dict[int, ClassificationResult]],
) -> str:
    """評価結果を CSV 文字列にする。

    Args:
        tracks: 楽曲 ID ごとの楽曲。
        labels: 楽曲 ID ごとの人手評価。
        classifications: エンジン名ごとの、楽曲 ID ごとの分類結果。
    """
    engines = sorted(classifications)
    attributes = list(Attribute)
    header = ["track_id", "split", "title", "artist", "album", "genre_tag"]
    for engine in engines:
        header += [f"{engine}_version", f"{engine}_genre", f"{engine}_mood"]
        header += [f"{engine}_{a.value}" for a in attributes]

    # 評価時に表示していた分類結果（現在の分類結果とは異なる場合がある）
    header += ["evaluated_engine", "evaluated_engine_version", "evaluated_genre", "evaluated_mood"]
    header += ["genre_verdict", "genre_label", "mood_verdict", "mood_label"]
    header += [f"rating_{a.value}" for a in attributes]
    header += ["note", "evaluated_at"]

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header)
    for track_id, label in sorted(labels.items()):
        track = tracks.get(track_id)
        row: list[object] = [
            track_id,
            label.split.value,
            None if track is None else track.title,
            None if track is None else track.artist,
            None if track is None else track.album,
            None if track is None else track.genre,
        ]
        for engine in engines:
            c = classifications[engine].get(track_id)
            if c is None:
                row += [None] * (3 + len(attributes))
                continue

            row += [c.engine_version, c.genre, c.mood]
            row += [c.attribute_value(a) for a in attributes]

        shown = label.classification_snapshot
        if shown is None:
            row += [None] * 4
        else:
            row += [shown.engine, shown.engine_version, shown.genre, shown.mood]

        row += [label.genre_verdict, label.genre_label, label.mood_verdict, label.mood_label]
        row += [label.ratings.get(a) for a in attributes]
        row += [label.note, label.evaluated_at]
        writer.writerow([_csv_safe(v) for v in row])

    return buffer.getvalue()
