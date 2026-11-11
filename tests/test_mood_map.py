import json
import math
from collections.abc import Callable
from dataclasses import replace

import pytest
from conftest import ClassificationFactory, TrackFactory
from hypothesis import given, settings
from hypothesis import strategies as st

from volumio_playlist_studio.config import PlaylistDefaults
from volumio_playlist_studio.models import (
    Attribute,
    AttributeScore,
    ClassificationResult,
    Genre,
    Mood,
    TitleSource,
    Track,
)
from volumio_playlist_studio.mood_map import (
    MOOD_POSITIONS,
    CircleRequest,
    MapPoint,
    build_track_map,
    generate_circle_playlist,
    track_point,
)
from volumio_playlist_studio.playlist import Candidate, GenreMode

DEFAULTS = PlaylistDefaults()
# 円の中心にする雰囲気の位置。テストの曲はここからのずれで置く
CENTER = MOOD_POSITIONS[Mood.RELAX]

Placer = Callable[..., Candidate]


def test_every_mood_except_omakase_has_position_within_map() -> None:
    assert set(MOOD_POSITIONS) == set(Mood) - {Mood.OMAKASE}
    for point in MOOD_POSITIONS.values():
        assert 0.0 <= point.x <= 1.0
        assert 0.0 <= point.y <= 1.0


def test_mood_positions_follow_impression() -> None:
    # 横軸は明るさ、縦軸はエネルギー感
    assert MOOD_POSITIONS[Mood.MELLOW].x < MOOD_POSITIONS[Mood.MORNING].x
    assert MOOD_POSITIONS[Mood.SLEEP].y < MOOD_POSITIONS[Mood.WORKOUT].y


def test_track_is_placed_by_brightness_and_energy(
    make_track: TrackFactory, make_classification: ClassificationFactory
) -> None:
    classification = make_classification(engine="jev", brightness=0.2, energy=0.9, relaxation=0.1)
    candidate = Candidate(make_track(), classification, classification.genre)

    assert track_point(candidate) == MapPoint(0.2, 0.9)


def test_tracks_without_both_axes_are_counted_separately(
    make_track: TrackFactory, make_classification: ClassificationFactory
) -> None:
    placed = make_classification(engine="jev", brightness=0.8, energy=0.3)
    # ルールベースは明るさを判定しない
    brightness_held = make_classification(brightness=None, energy=0.5)
    brightness_missing = make_classification(energy=0.5)
    energy_held = make_classification(engine="jev", brightness=0.5, energy=None)
    candidates = [
        Candidate(make_track(), placed, placed.genre),
        Candidate(make_track(), brightness_held, brightness_held.genre),
        Candidate(make_track(), brightness_missing, brightness_missing.genre),
        Candidate(make_track(), energy_held, energy_held.genre),
        Candidate(make_track(), None, None),
    ]

    track_map = build_track_map(candidates)

    assert [p.candidate for p in track_map.points] == [candidates[0]]
    assert track_map.points[0].point == MapPoint(0.8, 0.3)
    assert track_map.held == 3
    assert track_map.unclassified == 1


def test_empty_library_has_empty_map() -> None:
    track_map = build_track_map([])

    assert track_map.points == []
    assert track_map.held == 0
    assert track_map.unclassified == 0


@pytest.fixture
def place(make_track: TrackFactory, make_classification: ClassificationFactory) -> Placer:
    """円の中心からのずれ（dx, dy）の位置に曲を置いた候補を作る。追加の引数は曲の項目を上書きする。"""

    def _place(dx: float, dy: float, genre: Genre = Genre.POP, **track_overrides: object) -> Candidate:
        classification = make_classification(genre=genre, engine="jev", brightness=CENTER.x + dx, energy=CENTER.y + dy)
        return Candidate(make_track(**track_overrides), classification, genre)

    return _place


def ids(candidates: list[Candidate]) -> set[int]:
    return {c.track.id for c in candidates}


def test_circle_selects_tracks_inside_circle_including_boundary(
    place: Placer, make_track: TrackFactory, make_classification: ClassificationFactory
) -> None:
    inside = place(0.1, 0.0)
    on_boundary = place(0.0, 0.25)
    outside = place(0.0, -0.26)
    held_classification = make_classification(engine="jev", brightness=None, energy=CENTER.y)
    held = Candidate(make_track(), held_classification, held_classification.genre)
    unclassified = Candidate(make_track(), None, None)
    request = CircleRequest(Mood.RELAX, radius=0.25)

    result = generate_circle_playlist([inside, on_boundary, outside, held, unclassified], request, DEFAULTS)

    assert {t.track.id for t in result.tracks} == ids([inside, on_boundary])
    # スコアは中心への近さ（中心で 1、円周で 0）
    scores = {t.track.id: t.score for t in result.tracks}
    assert scores[inside.track.id] == pytest.approx(0.6)
    assert scores[on_boundary.track.id] == pytest.approx(0.0)
    assert result.shortfall is None


def test_circle_without_cap_selects_all_regardless_of_seed_and_randomness(place: Placer) -> None:
    # 利用回数が違っても（意外性が効く条件でも）、円の中は全部選ぶ
    candidates = [replace(place(0.02 * i, 0.0), usage_count=i) for i in range(8)]

    sets = [
        {t.track.id for t in generate_circle_playlist(
            candidates, CircleRequest(Mood.RELAX, radius=0.25, seed=seed, randomness=1.0, novelty=1.0), DEFAULTS
        ).tracks}
        for seed in (1, 2, 3)
    ]  # fmt: skip

    assert sets == [ids(candidates)] * 3


def test_track_count_cap_picks_tracks_nearest_to_center(place: Placer) -> None:
    far = place(0.2, 0.0)
    nearest = place(0.0, 0.05)
    middle = place(-0.1, 0.0)
    request = CircleRequest(Mood.RELAX, radius=0.25, track_count=2, randomness=0.0)

    result = generate_circle_playlist([far, nearest, middle], request, DEFAULTS)

    assert {t.track.id for t in result.tracks} == ids([nearest, middle])


def test_duration_cap_is_never_exceeded_and_filled_with_shorter_tracks(place: Placer) -> None:
    unknown_duration = place(0.0, 0.0, duration_sec=None)
    first = place(0.01, 0.0, duration_sec=1200.0)
    too_long = place(0.02, 0.0, duration_sec=900.0)
    fits = place(0.03, 0.0, duration_sec=600.0)
    request = CircleRequest(Mood.RELAX, radius=0.25, duration_min=30, randomness=0.0)

    result = generate_circle_playlist([unknown_duration, first, too_long, fits], request, DEFAULTS)

    assert {t.track.id for t in result.tracks} == ids([first, fits])
    assert result.total_duration_sec == 1800.0


def test_track_without_duration_is_selected_when_cap_is_not_by_duration(place: Placer) -> None:
    unknown_duration = place(0.0, 0.0, duration_sec=None)

    result = generate_circle_playlist([unknown_duration], CircleRequest(Mood.RELAX, radius=0.25), DEFAULTS)

    assert [t.track.id for t in result.tracks] == [unknown_duration.track.id]


def test_circle_applies_genre_filter_and_removes_duplicates(place: Placer) -> None:
    jazz = place(0.0, 0.0, genre=Genre.JAZZ, title="同じ曲", artist="同じ人")
    jazz_duplicate = place(0.05, 0.0, genre=Genre.JAZZ, title="同じ曲", artist="同じ人")
    pop = place(0.0, 0.05, genre=Genre.POP)
    request = CircleRequest(Mood.RELAX, radius=0.25, genre_mode=GenreMode.ONLY, genres=(Genre.JAZZ,))

    result = generate_circle_playlist([jazz, jazz_duplicate, pop], request, DEFAULTS)

    assert [t.track.id for t in result.tracks] == [jazz.track.id]


def test_circle_center_is_mood_position() -> None:
    assert CircleRequest(Mood.MELLOW, radius=0.25).center == MapPoint(0.0, 0.3)


@pytest.mark.parametrize(
    ("request_", "message"),
    [
        (CircleRequest(Mood.OMAKASE, radius=0.25), "position on the map"),
        (CircleRequest(Mood.RELAX, radius=0.0), "radius"),
        (CircleRequest(Mood.RELAX, radius=0.8), "radius"),
        (CircleRequest(Mood.RELAX, radius=0.25, duration_min=60, track_count=10), "together"),
        (CircleRequest(Mood.RELAX, radius=0.25, duration_min=10), "duration_min"),
        (CircleRequest(Mood.RELAX, radius=0.25, track_count=0), "track_count"),
        (CircleRequest(Mood.RELAX, radius=0.25, genre_mode=GenreMode.ONLY), "genres"),
        (CircleRequest(Mood.RELAX, radius=0.25, novelty=1.5), "novelty"),
    ],
)
def test_invalid_circle_request_is_rejected(request_: CircleRequest, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        generate_circle_playlist([], request_, DEFAULTS)


def test_circle_request_records_method_center_and_radius() -> None:
    request = CircleRequest(Mood.SLEEP, radius=0.3, track_count=10, genre_mode=GenreMode.MIX, genres=(Genre.JAZZ,))

    data = json.loads(json.dumps(request.to_dict()))

    assert data["method"] == "circle"
    assert data["mood"] == "sleep"
    assert data["center"] == {"x": 0.5, "y": 0.0}
    assert data["radius"] == 0.3
    assert data["track_count"] == 10
    assert data["duration_min"] is None
    assert data["genre_mode"] == "mix"
    assert data["genres"] == ["jazz"]


def _hypothesis_track(track_id: int, artist: int, duration: float | None) -> Track:
    return Track(
        id=track_id, library_root="/m", rel_path=f"{track_id}.flac", title=f"t{track_id}", artist=f"a{artist}",
        album=None, genre=None, year=None, track_number=None, duration_sec=duration, bitrate=None, sample_rate=None,
        channels=None, codec=None, file_size=1, mtime_ns=0, title_source=TitleSource.TAG, tags_repaired=False,
        quick_hash=None, status="active", tag_error=None,
    )  # fmt: skip


@st.composite
def placed_candidates(draw: st.DrawFn) -> list[Candidate]:
    candidates = []
    for i in range(draw(st.integers(min_value=0, max_value=40))):
        values = {
            Attribute.BRIGHTNESS: draw(st.one_of(st.none(), st.floats(min_value=0, max_value=1))),
            Attribute.ENERGY: draw(st.one_of(st.none(), st.floats(min_value=0, max_value=1))),
        }
        classification = ClassificationResult(
            engine="jev", engine_version="v", model=None, genre=None, genre_confidence=None,
            genre_probabilities=None, mood=None, attributes={a: AttributeScore(v) for a, v in values.items()},
        )  # fmt: skip
        duration = draw(st.one_of(st.none(), st.floats(min_value=30, max_value=900)))
        track = _hypothesis_track(i + 1, draw(st.integers(0, 3)), duration)
        candidates.append(Candidate(track, classification, None, draw(st.integers(0, 5))))

    return candidates


@st.composite
def circle_requests(draw: st.DrawFn) -> CircleRequest:
    cap = draw(st.sampled_from(["none", "duration", "count"]))
    return CircleRequest(
        mood=draw(st.sampled_from(sorted(MOOD_POSITIONS))),
        radius=draw(st.floats(min_value=DEFAULTS.min_circle_radius, max_value=DEFAULTS.max_circle_radius)),
        duration_min=draw(st.integers(DEFAULTS.min_duration_min, DEFAULTS.max_duration_min))
        if cap == "duration"
        else None,
        track_count=draw(st.integers(1, 20)) if cap == "count" else None,
        seed=draw(st.integers(min_value=0, max_value=1000)),
        randomness=draw(st.floats(min_value=0, max_value=1)),
    )


@settings(max_examples=60)
@given(candidates=placed_candidates(), circle=circle_requests())
def test_circle_playlist_invariants(candidates: list[Candidate], circle: CircleRequest) -> None:
    result = generate_circle_playlist(candidates, circle, DEFAULTS)

    chosen = [t.track.id for t in result.tracks]
    assert len(chosen) == len(set(chosen))
    center = MOOD_POSITIONS[circle.mood]
    radius = circle.radius
    for item in result.tracks:
        point = track_point(item.candidate)
        assert point is not None
        assert math.hypot(point.x - center.x, point.y - center.y) <= radius + 1e-6
        assert item.score is not None
        assert 0.0 <= item.score <= 1.0

    if circle.duration_min is not None:
        assert result.total_duration_sec <= circle.duration_min * 60

    if circle.track_count is not None:
        assert len(chosen) <= circle.track_count

    if not circle.capped:
        # 上限がなければ、円の中の曲（重複を除く）はすべて選ぶ。テストの曲はすべて曲名が異なる
        inside = [
            c for c in candidates
            if (p := track_point(c)) is not None and math.hypot(p.x - center.x, p.y - center.y) < radius - 1e-6
        ]  # fmt: skip
        assert ids(inside) <= set(chosen)

    assert generate_circle_playlist(candidates, circle, DEFAULTS).tracks == result.tracks
