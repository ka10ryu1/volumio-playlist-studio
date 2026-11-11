"""雰囲気と曲を、明るさ × エネルギー感の 2 軸のマップに配置する。

横軸は明るさ（0: 暗い・切ない 〜 1: 明るい・楽しい）、縦軸はエネルギー感（0: 静か 〜 1: 激しい）。
感情の円環モデル（快・不快 × 覚醒度）に当たる。現時点では表示のみで、選曲には使わない。
"""

from dataclasses import dataclass

from volumio_playlist_studio.models import Attribute, Mood
from volumio_playlist_studio.playlist import Candidate

X_ATTRIBUTE = Attribute.BRIGHTNESS
Y_ATTRIBUTE = Attribute.ENERGY


@dataclass(frozen=True)
class MapPoint:
    """マップ上の位置。x は明るさ、y はエネルギー感（いずれも 0〜1）。"""

    x: float
    y: float


# 明るさ・エネルギー感の目標値を持つ雰囲気は、選曲エンジン（playlist.py）の目標値と同じ位置に置く。
# 明るさの目標値を持たない雰囲気は、エネルギー感の目標値を縦の位置とし、横の位置は雰囲気の印象から定めた。
# 「おまかせ」は雰囲気で絞り込まないため、マップには置かない
MOOD_POSITIONS: dict[Mood, MapPoint] = {
    Mood.RELAX: MapPoint(0.6, 0.25),
    Mood.FOCUS: MapPoint(0.5, 0.4),
    Mood.ENERGETIC: MapPoint(0.8, 1.0),
    Mood.CALM_NIGHT: MapPoint(0.4, 0.15),
    Mood.MORNING: MapPoint(1.0, 0.55),
    Mood.CAFE: MapPoint(0.7, 0.4),
    Mood.DRIVE: MapPoint(0.7, 0.7),
    Mood.WORKOUT: MapPoint(0.6, 1.0),
    Mood.MELLOW: MapPoint(0.0, 0.3),
    Mood.SLEEP: MapPoint(0.5, 0.0),
}


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
