"""分類結果からプレイリストを編成する選曲エンジン。

プレイリストの生成では外部 API を呼ばず、保存済みの分類結果だけを使う。
同じ候補・条件・seed からは常に同じプレイリストを生成する。
条件に合う曲が足りない場合は自動で条件を緩めず、不足の内訳と緩和案を返して利用者に判断を委ねる。
"""

from collections import Counter
from dataclasses import asdict, dataclass, field
from enum import StrEnum

import numpy as np

from volumio_playlist_studio.config import PlaylistDefaults
from volumio_playlist_studio.models import Attribute, ClassificationResult, Genre, Mood, Track


class GenreMode(StrEnum):
    """ジャンルの指定方法。"""

    ALL = "all"
    ONLY = "only"
    MIX = "mix"


GENRE_MODE_LABELS: dict[GenreMode, str] = {
    GenreMode.ALL: "全ジャンル",
    GenreMode.ONLY: "指定ジャンルのみ",
    GenreMode.MIX: "指定ジャンルを混ぜる",
}

# 雰囲気ごとに（属性, 重み, 目標値）を定める。適合度は 1 - |値 - 目標値| の重み付き平均。
# 先頭の属性を主属性とし、主属性が判定保留の曲はその雰囲気の候補にしない
_MOOD_PROFILES: dict[Mood, tuple[tuple[Attribute, float, float], ...]] = {
    Mood.RELAX: ((Attribute.RELAXATION, 2.0, 1.0), (Attribute.ENERGY, 1.0, 0.25), (Attribute.CALMNESS, 1.0, 1.0)),
    Mood.FOCUS: ((Attribute.WORK_BGM, 2.0, 1.0), (Attribute.ENERGY, 1.0, 0.4), (Attribute.CALMNESS, 1.0, 0.7)),
    Mood.ENERGETIC: ((Attribute.ENERGY, 2.0, 1.0), (Attribute.PARTY, 1.0, 1.0), (Attribute.BRIGHTNESS, 1.0, 0.8)),
    Mood.CALM_NIGHT: ((Attribute.NIGHT_BGM, 2.0, 1.0), (Attribute.ENERGY, 1.0, 0.15), (Attribute.CALMNESS, 1.0, 1.0)),
    # 明るさが主属性の雰囲気は、明るさを判定しないルールベースの結果では候補が出ない
    Mood.MORNING: ((Attribute.BRIGHTNESS, 2.0, 1.0), (Attribute.ENERGY, 1.0, 0.55), (Attribute.CALMNESS, 1.0, 0.6)),
    Mood.CAFE: ((Attribute.RELAXATION, 2.0, 0.7), (Attribute.ENERGY, 1.0, 0.4), (Attribute.BRIGHTNESS, 1.0, 0.7)),
    Mood.DRIVE: ((Attribute.ENERGY, 2.0, 0.7), (Attribute.BRIGHTNESS, 1.0, 0.7), (Attribute.PARTY, 1.0, 0.6)),
    Mood.WORKOUT: ((Attribute.PARTY, 2.0, 1.0), (Attribute.ENERGY, 2.0, 1.0), (Attribute.RELAXATION, 1.0, 0.0)),
    Mood.MELLOW: ((Attribute.BRIGHTNESS, 2.0, 0.0), (Attribute.ENERGY, 1.0, 0.3), (Attribute.CALMNESS, 1.0, 0.8)),
    Mood.SLEEP: ((Attribute.CALMNESS, 2.0, 1.0), (Attribute.ENERGY, 1.5, 0.0), (Attribute.RELAXATION, 1.0, 1.0)),
}
# 「おまかせ」では雰囲気で絞り込まず、全曲を同じ適合度として扱う
_OMAKASE_SUITABILITY = 0.6
# 意外性・ランダム性がスコアに与える影響の最大値
_NOVELTY_WEIGHT = 0.3
_RANDOMNESS_WEIGHT = 0.3
# この回数プレイリストに入った曲を「定番」とみなす
_FAMILIAR_USAGE = 3
# 曲順を決めるとき、判定保留の属性はこの値とみなす
_NEUTRAL_VALUE = 0.5
# 最後に向けて落ち着かせる雰囲気
_WIND_DOWN_MOODS = frozenset({Mood.RELAX, Mood.CALM_NIGHT, Mood.SLEEP})

REASON_UNCLASSIFIED = "判定保留・未分類"
REASON_UNSUITABLE = "雰囲気に合わない"
REASON_GENRE = "ジャンル条件に合わない"
REASON_NO_DURATION = "再生時間が不明"
REASON_DUPLICATE = "重複"


@dataclass(frozen=True)
class PlaylistRequest:
    """プレイリストの生成条件。

    Attributes:
        duration_min: 目標の再生時間（分）。曲数を指定する場合は None。
        track_count: 曲数。再生時間を指定する場合は None。
        novelty: 0 で定番（過去のプレイリストによく入った曲）中心、1 で新しい発見（入ったことのない曲）中心。
        randomness: 0 で適合度順、1 で最もランダム。
        relaxed: 利用者が条件の緩和を選んだ場合 True。適合度の基準を下げる。
    """

    mood: Mood
    duration_min: int | None = 60
    track_count: int | None = None
    genre_mode: GenreMode = GenreMode.ALL
    genres: tuple[Genre, ...] = ()
    novelty: float = 0.3
    randomness: float = 0.3
    avoid_same_artist: bool = True
    avoid_duplicates: bool = True
    smooth_order: bool = True
    seed: int = 0
    relaxed: bool = False

    def validate(self, defaults: PlaylistDefaults) -> None:
        """条件を検証する。

        Raises:
            ValueError: 条件が不正な場合。
        """
        if (self.duration_min is None) == (self.track_count is None):
            raise ValueError("exactly one of duration_min and track_count must be set")

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
        """保存用の辞書に変換する。"""
        data = asdict(self)
        data["mood"] = self.mood.value
        data["genre_mode"] = self.genre_mode.value
        data["genres"] = [g.value for g in self.genres]
        return data


@dataclass(frozen=True)
class Candidate:
    """選曲の候補。

    Attributes:
        classification: 生成に使う分類結果。未分類なら None。
        genre: 選曲に使うジャンル。人手で修正したジャンルがあればそれを優先する。
        usage_count: 保存済みプレイリストに入った回数。
    """

    track: Track
    classification: ClassificationResult | None
    genre: Genre | None
    usage_count: int = 0


@dataclass(frozen=True)
class SelectedTrack:
    """プレイリストに選ばれた曲。score は雰囲気への適合度（手動で追加した曲は None）。"""

    candidate: Candidate
    score: float | None
    added_manually: bool = False

    @property
    def track(self) -> Track:
        """楽曲。"""
        return self.candidate.track


@dataclass(frozen=True)
class Shortfall:
    """条件に合う曲が足りなかった場合の説明。

    Attributes:
        requested: 要求した再生時間（秒）または曲数。
        achieved: 実際に集まった再生時間（秒）または曲数。
        unit: `seconds` または `tracks`。
        excluded: 除外した理由ごとの曲数。
        suggestions: 利用者に提示する緩和案。
    """

    requested: float
    achieved: float
    unit: str
    excluded: dict[str, int]
    suggestions: list[str]


@dataclass(frozen=True)
class GenerationResult:
    """プレイリストの生成結果。"""

    request: PlaylistRequest
    tracks: list[SelectedTrack]
    shortfall: Shortfall | None
    warnings: list[str] = field(default_factory=list)

    @property
    def total_duration_sec(self) -> float:
        """再生時間の合計（再生時間が不明な曲は 0 として数える）。"""
        return total_duration(self.tracks)


def total_duration(tracks: list[SelectedTrack]) -> float:
    """再生時間の合計を返す。再生時間が不明な曲は 0 として数える。"""
    return sum(t.track.duration_sec or 0.0 for t in tracks)


def suitability(classification: ClassificationResult | None, mood: Mood) -> float | None:
    """分類結果の、雰囲気への適合度（0〜1）を返す。主属性が判定保留なら None。"""
    if mood == Mood.OMAKASE:
        return _OMAKASE_SUITABILITY

    if classification is None:
        return None

    profile = _MOOD_PROFILES[mood]
    primary = classification.attribute_value(profile[0][0])
    if primary is None:
        return None

    weighted = total_weight = 0.0
    for attribute, weight, target in profile:
        value = classification.attribute_value(attribute)
        if value is not None:
            weighted += weight * (1.0 - abs(value - target))
            total_weight += weight

    return weighted / total_weight


def _primary_attribute(mood: Mood) -> Attribute | None:
    """雰囲気の主属性。「おまかせ」は None。"""
    profile = _MOOD_PROFILES.get(mood)
    return None if profile is None else profile[0][0]


def duplicate_key(track: Track) -> str:
    """同じ曲とみなすためのキー。曲名とアーティストが分かればそれを、なければファイル内容のハッシュを使う。"""
    if track.title and track.artist:
        return f"meta:{track.title.casefold().strip()}|{track.artist.casefold().strip()}"

    return f"hash:{track.quick_hash or track.rel_path}"


def generate_playlist(
    candidates: list[Candidate], request: PlaylistRequest, defaults: PlaylistDefaults
) -> GenerationResult:
    """条件に合う曲を選び、曲順を決める。

    Args:
        candidates: 選曲の候補。
        request: 生成条件。
        defaults: 生成の既定値と制約。

    Returns:
        生成結果。曲が足りない場合も、集まった曲と不足の説明を返す。

    Raises:
        ValueError: 条件が不正な場合。
    """
    request.validate(defaults)
    rng = np.random.default_rng(request.seed)
    excluded: Counter[str] = Counter()
    threshold = defaults.relaxed_suitability_threshold if request.relaxed else defaults.suitability_threshold

    scored = _score_candidates(candidates, request, threshold, rng, excluded)
    chosen = _select(scored, request, defaults, excluded)
    selected = [SelectedTrack(candidate=c, score=round(s, 4)) for c, s in chosen]

    warnings: list[str] = []
    if request.smooth_order:
        selected = _smooth_order(selected, request.mood)

    if request.avoid_same_artist and not _separate_artists(selected):
        warnings.append("同じアーティストの曲が多く、連続を避けきれない箇所があります。")

    return GenerationResult(
        request=request,
        tracks=selected,
        shortfall=_shortfall(selected, request, defaults, excluded),
        warnings=warnings,
    )


def _score_candidates(
    candidates: list[Candidate],
    request: PlaylistRequest,
    threshold: float,
    rng: np.random.Generator,
    excluded: Counter[str],
) -> list[tuple[Candidate, float, float]]:
    """候補を絞り込み、（候補, 適合度, 並べ替え用のスコア）を返す。"""
    allowed_genres = set(request.genres)
    result: list[tuple[Candidate, float, float]] = []
    # 入力の順序によらず同じ結果になるよう、ID 順に乱数を割り当てる
    for candidate in sorted(candidates, key=lambda c: c.track.id):
        noise = float(rng.uniform(-1.0, 1.0))
        if request.genre_mode != GenreMode.ALL and candidate.genre not in allowed_genres:
            excluded[REASON_GENRE] += 1
            continue

        fit = suitability(candidate.classification, request.mood)
        if fit is None:
            excluded[REASON_UNCLASSIFIED] += 1
            continue

        if fit < threshold:
            excluded[REASON_UNSUITABLE] += 1
            continue

        if request.duration_min is not None and not candidate.track.duration_sec:
            excluded[REASON_NO_DURATION] += 1
            continue

        familiarity = min(1.0, candidate.usage_count / _FAMILIAR_USAGE)
        novelty_bonus = request.novelty * (1 - familiarity) + (1 - request.novelty) * familiarity
        rank = fit + _NOVELTY_WEIGHT * novelty_bonus + _RANDOMNESS_WEIGHT * request.randomness * noise
        result.append((candidate, fit, rank))

    return result


def _ranked_order(
    scored: list[tuple[Candidate, float, float]], request: PlaylistRequest
) -> list[tuple[Candidate, float]]:
    """並べ替え用のスコア順に並べる。ジャンル混合ではジャンルごとに 1 曲ずつ交互に取り出す。"""
    ordered = sorted(scored, key=lambda item: (-item[2], item[0].track.id))
    if request.genre_mode != GenreMode.MIX:
        return [(c, fit) for c, fit, _ in ordered]

    by_genre: dict[Genre | None, list[tuple[Candidate, float]]] = {}
    for c, fit, _ in ordered:
        by_genre.setdefault(c.genre, []).append((c, fit))

    queues = [by_genre[g] for g in request.genres if g in by_genre]
    interleaved: list[tuple[Candidate, float]] = []
    while any(queues):
        for queue in queues:
            if queue:
                interleaved.append(queue.pop(0))

    return interleaved


def _select(
    scored: list[tuple[Candidate, float, float]],
    request: PlaylistRequest,
    defaults: PlaylistDefaults,
    excluded: Counter[str],
) -> list[tuple[Candidate, float]]:
    target_sec = None if request.duration_min is None else request.duration_min * 60.0
    upper_sec = None if target_sec is None else target_sec + defaults.duration_tolerance_min * 60.0
    chosen: list[tuple[Candidate, float]] = []
    seen_keys: set[str] = set()
    total = 0.0

    for candidate, fit in _ranked_order(scored, request):
        if target_sec is not None and upper_sec is not None:
            if total >= target_sec:
                break

            duration = candidate.track.duration_sec or 0.0
            if total + duration > upper_sec:
                continue
        elif request.track_count is not None and len(chosen) >= request.track_count:
            break

        key = duplicate_key(candidate.track)
        if request.avoid_duplicates and key in seen_keys:
            excluded[REASON_DUPLICATE] += 1
            continue

        seen_keys.add(key)
        chosen.append((candidate, fit))
        total += candidate.track.duration_sec or 0.0

    return chosen


def _smooth_order(tracks: list[SelectedTrack], mood: Mood) -> list[SelectedTrack]:
    """エネルギー感とリラックス度が急に変わらないよう、近い曲を順につなぐ。

    エネルギー感が最も低い曲から始めて、まだ使っていない最も近い曲を次に選ぶ（最近傍法）。
    これにより全体としてエネルギー感が徐々に上がる並びになる。リラックス・穏やかな夜・入眠では逆順にし、
    終盤に向けて落ち着いていく並びにする。
    """
    if len(tracks) <= 1:
        return list(tracks)

    def point(t: SelectedTrack) -> tuple[float, float]:
        c = t.candidate.classification
        energy = None if c is None else c.attribute_value(Attribute.ENERGY)
        relaxation = None if c is None else c.attribute_value(Attribute.RELAXATION)
        return (_NEUTRAL_VALUE if energy is None else energy, _NEUTRAL_VALUE if relaxation is None else relaxation)

    remaining = sorted(tracks, key=lambda t: (point(t)[0], t.track.id))
    ordered = [remaining.pop(0)]
    while remaining:
        last = point(ordered[-1])
        nearest = min(
            remaining,
            key=lambda t: (abs(point(t)[0] - last[0]) + abs(point(t)[1] - last[1]), t.track.id),
        )
        remaining.remove(nearest)
        ordered.append(nearest)

    if mood in _WIND_DOWN_MOODS:
        ordered.reverse()

    return ordered


def _same_artist(a: SelectedTrack, b: SelectedTrack) -> bool:
    # アーティスト不明の曲どうしは同じアーティストとみなさない
    return (
        a.track.artist is not None
        and b.track.artist is not None
        and (a.track.artist.casefold() == b.track.artist.casefold())
    )


def _separate_artists(tracks: list[SelectedTrack]) -> bool:
    """同じアーティストが連続する箇所を、後ろの曲と入れ替えて解消する。

    Returns:
        連続をすべて解消できたら True。
    """
    for i in range(1, len(tracks)):
        if not _same_artist(tracks[i - 1], tracks[i]):
            continue

        for j in range(i + 1, len(tracks)):
            if not _same_artist(tracks[i - 1], tracks[j]):
                tracks.insert(i, tracks.pop(j))
                break

    return all(not _same_artist(tracks[i - 1], tracks[i]) for i in range(1, len(tracks)))


def _shortfall(
    tracks: list[SelectedTrack], request: PlaylistRequest, defaults: PlaylistDefaults, excluded: Counter[str]
) -> Shortfall | None:
    if request.duration_min is not None:
        requested = request.duration_min * 60.0
        achieved = total_duration(tracks)
        if achieved >= requested - defaults.duration_tolerance_min * 60.0:
            return None

        unit = "seconds"
    else:
        requested = float(request.track_count or 0)
        achieved = float(len(tracks))
        if achieved >= requested:
            return None

        unit = "tracks"

    suggestions: list[str] = []
    if excluded[REASON_GENRE]:
        suggestions.append("ジャンルの指定を「全ジャンル」にするか、ジャンルを追加する")

    if excluded[REASON_UNSUITABLE] and not request.relaxed:
        suggestions.append("条件を緩和して再生成する（雰囲気への適合度の基準を下げる）")

    if excluded[REASON_UNCLASSIFIED]:
        suggestions.append("未解析・未判定の曲を解析または判定する")
        if _primary_attribute(request.mood) == Attribute.BRIGHTNESS:
            suggestions.append(
                "Jev で判定する（ルールベースは曲調の明るさを判定しないため、この雰囲気の候補にならない）"
            )

    if unit == "seconds":
        suggestions.append("再生時間を短くする")

    return Shortfall(requested, achieved, unit, dict(excluded), suggestions)


def move_item(tracks: list[SelectedTrack], index: int, offset: int) -> list[SelectedTrack]:
    """曲を前後に移動した新しいリストを返す。範囲外への移動は端で止める。"""
    if not 0 <= index < len(tracks):
        raise IndexError("index out of range")

    result = list(tracks)
    item = result.pop(index)
    result.insert(max(0, min(len(result), index + offset)), item)
    return result


def remove_item(tracks: list[SelectedTrack], index: int) -> list[SelectedTrack]:
    """曲を除外した新しいリストを返す。"""
    if not 0 <= index < len(tracks):
        raise IndexError("index out of range")

    return tracks[:index] + tracks[index + 1 :]


def add_item(tracks: list[SelectedTrack], candidate: Candidate) -> list[SelectedTrack]:
    """曲を末尾に手動で追加した新しいリストを返す。

    Raises:
        ValueError: 同じ曲が既に含まれている場合。
    """
    if any(t.track.id == candidate.track.id for t in tracks):
        raise ValueError("track is already in the playlist")

    return [*tracks, SelectedTrack(candidate=candidate, score=None, added_manually=True)]
