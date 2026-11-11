"""雰囲気と曲を、明るさ × エネルギー感の 2 軸のマップに配置し、マップ上の円で選曲する。

横軸は明るさ（0: 暗い・切ない 〜 1: 明るい・楽しい）、縦軸はエネルギー感（0: 静か 〜 1: 激しい）。
感情の円環モデル（快・不快 × 覚醒度）に当たる。
円の中心には、既存の雰囲気のほか、説明文から Jev が位置を決めた自由記述の雰囲気（`custom_mood.py`）も使える。

円で選ぶときは、マップで見たとおりに選ばれることを優先し、円の内外を 2 軸の位置だけで判定する。
作業BGM適性など他の属性は使わないため、同じ雰囲気でも「雰囲気と長さで選ぶ」とは選ばれる曲が変わる。
"""

import math
from dataclasses import dataclass, fields

import numpy as np

from volumio_playlist_studio.config import PlaylistDefaults
from volumio_playlist_studio.models import Attribute, CustomMood, Genre, Mood
from volumio_playlist_studio.playlist import (
    Candidate,
    CreationMethod,
    GenerationResult,
    GenreMode,
    SelectedTrack,
    arrange,
    duplicate_key,
    preference_bonus,
    ranked_order,
)

X_ATTRIBUTE = Attribute.BRIGHTNESS
Y_ATTRIBUTE = Attribute.ENERGY


@dataclass(frozen=True)
class MapPoint:
    """マップ上の位置。x は明るさ、y はエネルギー感（いずれも 0〜1）。"""

    x: float
    y: float


# 明るさ・エネルギー感の目標値を持つ雰囲気は、選曲エンジン（playlist.py）の目標値と同じ位置に置く。
# 明るさの目標値を持たない雰囲気は、エネルギー感の目標値を縦の位置とし、横の位置は雰囲気の印象から定めた。
# 「おまかせ」は雰囲気で絞り込まないため、マップには置かない（円の中心にもできない）。
# 穏やかな夜はプレイリスト生成で選べない（`playlist.PLAYLIST_MOODS`）ため、マップにも置かない
MOOD_POSITIONS: dict[Mood, MapPoint] = {
    Mood.RELAX: MapPoint(0.6, 0.25),
    Mood.FOCUS: MapPoint(0.5, 0.4),
    Mood.ENERGETIC: MapPoint(0.8, 1.0),
    Mood.MORNING: MapPoint(1.0, 0.55),
    Mood.CAFE: MapPoint(0.7, 0.4),
    Mood.DRIVE: MapPoint(0.7, 0.7),
    Mood.WORKOUT: MapPoint(0.6, 1.0),
    Mood.MELLOW: MapPoint(0.0, 0.3),
    Mood.SLEEP: MapPoint(0.5, 0.0),
    Mood.NIGHT_CITY: MapPoint(0.2, 0.5),
    Mood.TENSE: MapPoint(0.1, 0.95),
}


# 円周上の曲を、浮動小数点の誤差で円の外と判定しないための許容幅
_BOUNDARY_TOLERANCE = 1e-9


@dataclass(frozen=True)
class CircleRequest:
    """マップの円で選ぶ作成方法の条件。

    円の中の曲をすべて選ぶ。上限（再生時間か曲数）を付けた場合は、中心に近い順に上限まで選ぶ。

    Attributes:
        mood: 円の中心にする雰囲気（既存の雰囲気か、自由記述の雰囲気）。中心はこの雰囲気のマップ上の位置。
        radius: 円の半径（マップの座標での長さ）。
        duration_min: 再生時間の上限（分）。上限を付けない場合は None。
        track_count: 曲数の上限。上限を付けない場合は None。再生時間の上限と同時には指定できない。
        novelty: 上限を付けたときだけ使う。0 で定番中心、1 で新しい発見中心。
        randomness: 上限を付けたときだけ使う。0 で中心に近い順、1 で最もランダム。
    """

    mood: Mood | CustomMood
    radius: float
    duration_min: int | None = None
    track_count: int | None = None
    genre_mode: GenreMode = GenreMode.ALL
    genres: tuple[Genre, ...] = ()
    novelty: float = 0.3
    randomness: float = 0.3
    avoid_same_artist: bool = True
    avoid_duplicates: bool = True
    smooth_order: bool = True
    seed: int = 0

    @property
    def center(self) -> MapPoint:
        """円の中心。"""
        if isinstance(self.mood, CustomMood):
            return MapPoint(self.mood.x, self.mood.y)

        return MOOD_POSITIONS[self.mood]

    @property
    def capped(self) -> bool:
        """上限を付けたかどうか。"""
        return self.duration_min is not None or self.track_count is not None

    def validate(self, defaults: PlaylistDefaults) -> None:
        """条件を検証する。

        Raises:
            ValueError: 条件が不正な場合。
        """
        if isinstance(self.mood, CustomMood):
            if not (0.0 <= self.mood.x <= 1.0 and 0.0 <= self.mood.y <= 1.0):
                raise ValueError("custom mood position is out of range")

        elif self.mood not in MOOD_POSITIONS:
            raise ValueError("mood must have a position on the map")

        if not defaults.min_circle_radius <= self.radius <= defaults.max_circle_radius:
            raise ValueError("radius is out of range")

        if self.duration_min is not None and self.track_count is not None:
            raise ValueError("duration_min and track_count must not be set together")

        if self.duration_min is not None and not (
            defaults.min_duration_min <= self.duration_min <= defaults.max_duration_min
        ):
            raise ValueError("duration_min is out of range")

        if self.track_count is not None and not 1 <= self.track_count <= defaults.max_track_count:
            raise ValueError("track_count is out of range")

        if self.genre_mode != GenreMode.ALL and not self.genres:
            raise ValueError("genres must be specified unless genre_mode is all")

        if not (0.0 <= self.novelty <= 1.0 and 0.0 <= self.randomness <= 1.0):
            raise ValueError("novelty and randomness must be between 0 and 1")

    def to_dict(self) -> dict[str, object]:
        """保存用の辞書に変換する。

        雰囲気の位置を後から変えたり、自由記述の雰囲気を削除したりしても条件を再現できるよう、中心の座標も記録する。
        自由記述の雰囲気は、`mood` を None とし、名前・説明文・質問定義のバージョンを `custom_mood` に記録する。
        """
        # 自由記述の雰囲気は属性の値などを含むため、asdict で丸ごと変換せず、必要な項目だけを記録する
        data: dict[str, object] = {f.name: getattr(self, f.name) for f in fields(self) if f.name != "mood"}
        data["method"] = CreationMethod.CIRCLE.value
        if isinstance(self.mood, CustomMood):
            data["mood"] = None
            data["custom_mood"] = {
                "id": self.mood.id,
                "name": self.mood.name,
                "description": self.mood.description,
                "question_version": self.mood.question_version,
                "model": self.mood.model,
            }
        else:
            data["mood"] = self.mood.value

        data["center"] = {"x": self.center.x, "y": self.center.y}
        data["genre_mode"] = self.genre_mode.value
        data["genres"] = [g.value for g in self.genres]
        return data


@dataclass(frozen=True)
class TrackPoint:
    """マップに描く曲。"""

    candidate: Candidate
    point: MapPoint


@dataclass(frozen=True)
class TrackMap:
    """マップに描く曲と、描けなかった曲の内訳。

    Attributes:
        points: 明るさとエネルギー感がともに判定済みの曲。
        held: 分類済みだが、明るさかエネルギー感が判定保留（または未判定）の曲数。
        unclassified: 分類結果がない曲数。
    """

    points: list[TrackPoint]
    held: int
    unclassified: int


def track_point(candidate: Candidate) -> MapPoint | None:
    """曲のマップ上の位置を返す。明るさかエネルギー感が判定保留・未分類なら None。"""
    classification = candidate.classification
    if classification is None:
        return None

    x = classification.attribute_value(X_ATTRIBUTE)
    y = classification.attribute_value(Y_ATTRIBUTE)
    if x is None or y is None:
        return None

    return MapPoint(x, y)


def build_track_map(candidates: list[Candidate]) -> TrackMap:
    """候補の曲をマップに配置する。"""
    points = []
    held = 0
    unclassified = 0
    for candidate in candidates:
        if candidate.classification is None:
            unclassified += 1
            continue

        point = track_point(candidate)
        if point is None:
            held += 1
            continue

        points.append(TrackPoint(candidate, point))

    return TrackMap(points, held, unclassified)


def distance(a: MapPoint, b: MapPoint) -> float:
    """マップ上の 2 点の距離。"""
    return math.dist((a.x, a.y), (b.x, b.y))


def generate_circle_playlist(
    candidates: list[Candidate], request: CircleRequest, defaults: PlaylistDefaults
) -> GenerationResult[CircleRequest]:
    """円の中の曲を選び、曲順を決める。

    円の中の曲が少なくても条件は緩めない（円の大きさで利用者が調整する）。上限は超えない。

    Args:
        candidates: 選曲の候補。
        request: 生成条件。
        defaults: 生成の制約。

    Returns:
        生成結果。各曲のスコアは中心への近さ（中心で 1、円周で 0）。

    Raises:
        ValueError: 条件が不正な場合。
    """
    selected, warnings = arrange(
        select_circle_tracks(candidates, request, defaults),
        # 自由記述の雰囲気では、終盤に向けて落ち着かせるかを決められないため、既存の雰囲気の扱いを使わない
        None if isinstance(request.mood, CustomMood) else request.mood,
        smooth_order=request.smooth_order,
        avoid_same_artist=request.avoid_same_artist,
    )
    return GenerationResult(request=request, tracks=selected, shortfall=None, warnings=warnings)


def select_circle_tracks(
    candidates: list[Candidate], request: CircleRequest, defaults: PlaylistDefaults
) -> list[SelectedTrack]:
    """円の中から選ぶ曲だけを決める。曲順は決めない（中心に近い順に並ぶ）。

    円の大きさを変えるたびに描くプレビュー用。上限がなければ数千曲になり得るため、曲数の 2 乗の時間が
    かかる曲順の調整（`arrange`）は行わない。選ばれる曲の集合は `generate_circle_playlist` と同じ。

    Raises:
        ValueError: 条件が不正な場合。
    """
    request.validate(defaults)
    rng = np.random.default_rng(request.seed)
    allowed_genres = set(request.genres)
    scored: list[tuple[Candidate, float, float]] = []
    # 入力の順序によらず同じ結果になるよう、ID 順に乱数を割り当てる
    for candidate in sorted(candidates, key=lambda c: c.track.id):
        noise = float(rng.uniform(-1.0, 1.0))
        if request.genre_mode != GenreMode.ALL and candidate.genre not in allowed_genres:
            continue

        point = track_point(candidate)
        if point is None:
            continue

        gap = distance(point, request.center)
        if gap > request.radius + _BOUNDARY_TOLERANCE:
            continue

        # 再生時間が不明な曲は、再生時間の上限を守れるか分からないため選ばない
        if request.duration_min is not None and not candidate.track.duration_sec:
            continue

        closeness = max(0.0, 1.0 - gap / request.radius)
        # 上限がなければ円の中を全部選ぶため、意外性・ランダム性は効かない
        bonus = preference_bonus(candidate, request.novelty, request.randomness, noise) if request.capped else 0.0
        scored.append((candidate, closeness, closeness + bonus))

    chosen = _select_within_cap(ranked_order(scored, request.genre_mode, request.genres), request)
    return [SelectedTrack(candidate=c, score=round(s, 4)) for c, s in chosen]


def _select_within_cap(ranked: list[tuple[Candidate, float]], request: CircleRequest) -> list[tuple[Candidate, float]]:
    cap_sec = None if request.duration_min is None else request.duration_min * 60.0
    chosen: list[tuple[Candidate, float]] = []
    seen_keys: set[str] = set()
    total = 0.0
    for candidate, closeness in ranked:
        if request.track_count is not None and len(chosen) >= request.track_count:
            break

        duration = candidate.track.duration_sec or 0.0
        # 上限を超える曲は飛ばし、後ろの短い曲で埋められるか試す
        if cap_sec is not None and total + duration > cap_sec:
            continue

        key = duplicate_key(candidate.track)
        if request.avoid_duplicates and key in seen_keys:
            continue

        seen_keys.add(key)
        chosen.append((candidate, closeness))
        total += duration

    return chosen
