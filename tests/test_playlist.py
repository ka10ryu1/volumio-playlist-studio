import itertools
from collections import Counter
from dataclasses import replace
from typing import Any

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
from volumio_playlist_studio.playlist import (
    REASON_DUPLICATE,
    REASON_GENRE,
    REASON_NO_DURATION,
    REASON_UNCLASSIFIED,
    REASON_UNSUITABLE,
    Candidate,
    GenreMode,
    PlaylistRequest,
    add_item,
    duplicate_key,
    generate_playlist,
    move_item,
    remove_item,
    suitability,
    total_duration,
)

DEFAULTS = PlaylistDefaults()


@pytest.fixture
def relaxing_library(make_track: TrackFactory, make_classification: ClassificationFactory) -> list[Candidate]:
    """リラックス向けの曲を 5 人のアーティストで 30 曲（各 4 分）用意する。"""
    candidates = []
    for i in range(30):
        track = make_track(artist=f"アーティスト{i % 5}", duration_sec=240.0)
        relaxation = 0.7 + (i % 4) * 0.1
        classification = make_classification(
            genre=[Genre.JAZZ, Genre.AMBIENT, Genre.CLASSICAL][i % 3],
            relaxation=relaxation,
            energy=0.2 + (i % 5) * 0.05,
            calmness=0.8,
        )
        candidates.append(Candidate(track, classification, classification.genre))
    return candidates


def test_duration_target_is_met_within_tolerance(relaxing_library: list[Candidate]) -> None:
    result = generate_playlist(relaxing_library, PlaylistRequest(Mood.RELAX, duration_min=60), DEFAULTS)

    assert result.shortfall is None
    assert 60 * 60 <= result.total_duration_sec <= (60 + DEFAULTS.duration_tolerance_min) * 60


def test_track_count_is_respected(relaxing_library: list[Candidate]) -> None:
    result = generate_playlist(
        relaxing_library, PlaylistRequest(Mood.RELAX, duration_min=None, track_count=7), DEFAULTS
    )

    assert len(result.tracks) == 7
    assert result.shortfall is None


def test_same_seed_reproduces_and_different_seed_varies(relaxing_library: list[Candidate]) -> None:
    def ids(seed: int) -> list[int]:
        request = PlaylistRequest(Mood.RELAX, duration_min=30, randomness=1.0, seed=seed)
        return [t.track.id for t in generate_playlist(relaxing_library, request, DEFAULTS).tracks]

    assert ids(1) == ids(1)
    # 入力の並び順にもよらない
    shuffled = list(reversed(relaxing_library))
    request = PlaylistRequest(Mood.RELAX, duration_min=30, randomness=1.0, seed=1)
    assert [t.track.id for t in generate_playlist(shuffled, request, DEFAULTS).tracks] == ids(1)
    assert len({tuple(ids(seed)) for seed in range(5)}) > 1


def test_same_artist_is_not_consecutive(relaxing_library: list[Candidate]) -> None:
    result = generate_playlist(relaxing_library, PlaylistRequest(Mood.RELAX, duration_min=90), DEFAULTS)

    artists = [t.track.artist for t in result.tracks]
    assert all(a != b for a, b in itertools.pairwise(artists))
    assert result.warnings == []


def test_warns_when_artist_repetition_cannot_be_avoided(
    make_track: TrackFactory, make_classification: ClassificationFactory
) -> None:
    candidates = [
        Candidate(make_track(artist="同じ人"), make_classification(relaxation=0.9, energy=0.2, calmness=0.9), None)
        for _ in range(5)
    ]

    result = generate_playlist(candidates, PlaylistRequest(Mood.RELAX, duration_min=None, track_count=5), DEFAULTS)

    assert len(result.tracks) == 5
    assert result.warnings


def test_artist_repetition_is_allowed_when_disabled(
    make_track: TrackFactory, make_classification: ClassificationFactory
) -> None:
    candidates = [
        Candidate(make_track(artist="同じ人"), make_classification(relaxation=0.9, energy=0.2, calmness=0.9), None)
        for _ in range(3)
    ]
    request = PlaylistRequest(Mood.RELAX, duration_min=None, track_count=3, avoid_same_artist=False)

    assert generate_playlist(candidates, request, DEFAULTS).warnings == []


def test_duplicates_are_avoided(make_track: TrackFactory, make_classification: ClassificationFactory) -> None:
    fit = make_classification(relaxation=0.9, energy=0.2, calmness=0.9)
    original = make_track(title="同じ曲", artist="A")
    copy = make_track(title="同じ曲", artist="a", rel_path="copy/同じ曲.flac")
    other = make_track(title="別の曲", artist="B")
    candidates = [Candidate(t, fit, None) for t in (original, copy, other)]

    result = generate_playlist(candidates, PlaylistRequest(Mood.RELAX, duration_min=None, track_count=3), DEFAULTS)

    assert len(result.tracks) == 2
    assert result.shortfall is not None
    assert result.shortfall.excluded[REASON_DUPLICATE] == 1
    allowed = PlaylistRequest(Mood.RELAX, duration_min=None, track_count=3, avoid_duplicates=False)
    assert len(generate_playlist(candidates, allowed, DEFAULTS).tracks) == 3


def test_duplicate_key_falls_back_to_file_hash(make_track: TrackFactory) -> None:
    assert duplicate_key(make_track(title=None, quick_hash="abc")) == "hash:abc"
    assert duplicate_key(make_track(title=" Song ", artist="ARTIST")) == "meta:song|artist"


def test_shortfall_is_explained_without_relaxing_conditions(
    make_track: TrackFactory, make_classification: ClassificationFactory
) -> None:
    candidates = [
        Candidate(make_track(), make_classification(relaxation=0.9, energy=0.2, calmness=0.9), Genre.JAZZ),
        Candidate(make_track(), make_classification(relaxation=0.4, energy=0.6, calmness=0.4), Genre.JAZZ),
        Candidate(make_track(), make_classification(relaxation=0.9, energy=0.1, calmness=0.9), Genre.ROCK),
        Candidate(make_track(), None, Genre.JAZZ),
        Candidate(make_track(duration_sec=None), make_classification(relaxation=0.9, energy=0.1), Genre.JAZZ),
    ]
    request = PlaylistRequest(Mood.RELAX, duration_min=60, genre_mode=GenreMode.ONLY, genres=(Genre.JAZZ,))

    result = generate_playlist(candidates, request, DEFAULTS)

    assert [c.track.id for c in (t.candidate for t in result.tracks)] == [candidates[0].track.id]
    assert result.shortfall is not None
    assert result.shortfall.unit == "seconds"
    assert result.shortfall.requested == 3600
    assert result.shortfall.achieved == 240
    assert result.shortfall.excluded == {
        REASON_GENRE: 1,
        REASON_UNSUITABLE: 1,
        REASON_UNCLASSIFIED: 1,
        REASON_NO_DURATION: 1,
    }
    assert any("全ジャンル" in s for s in result.shortfall.suggestions)
    assert any("緩和" in s for s in result.shortfall.suggestions)
    assert any("解析" in s for s in result.shortfall.suggestions)


def test_relaxed_request_accepts_less_suitable_tracks(
    make_track: TrackFactory, make_classification: ClassificationFactory
) -> None:
    # 適合度 (2 * 0.4 + 0.65 + 0.4) / 4 = 0.4625 の曲は、通常の基準（0.55）では除外され、緩和後の基準（0.4）では選ばれる
    candidates = [Candidate(make_track(), make_classification(relaxation=0.4, energy=0.6, calmness=0.4), None)]
    strict = PlaylistRequest(Mood.RELAX, duration_min=None, track_count=1)

    assert generate_playlist(candidates, strict, DEFAULTS).tracks == []
    relaxed = generate_playlist(candidates, replace(strict, relaxed=True), DEFAULTS)
    assert len(relaxed.tracks) == 1
    assert relaxed.shortfall is None


def test_mixed_genres_are_interleaved(relaxing_library: list[Candidate]) -> None:
    request = PlaylistRequest(
        Mood.RELAX,
        duration_min=None,
        track_count=6,
        genre_mode=GenreMode.MIX,
        genres=(Genre.JAZZ, Genre.AMBIENT),
        smooth_order=False,
        avoid_same_artist=False,
    )

    result = generate_playlist(relaxing_library, request, DEFAULTS)

    assert Counter(t.candidate.genre for t in result.tracks) == {Genre.JAZZ: 3, Genre.AMBIENT: 3}


def test_omakase_uses_unclassified_tracks(make_track: TrackFactory) -> None:
    candidates = [Candidate(make_track(), None, None) for _ in range(3)]

    result = generate_playlist(candidates, PlaylistRequest(Mood.OMAKASE, duration_min=None, track_count=3), DEFAULTS)

    assert len(result.tracks) == 3


def test_energetic_playlist_starts_low_and_relaxing_playlist_ends_low(
    make_track: TrackFactory, make_classification: ClassificationFactory
) -> None:
    energies = [0.6, 0.95, 0.7, 0.85, 0.75]
    energetic = [
        Candidate(make_track(), make_classification(energy=e, party=0.9, brightness=0.8), None) for e in energies
    ]
    result = generate_playlist(energetic, PlaylistRequest(Mood.ENERGETIC, duration_min=None, track_count=5), DEFAULTS)
    ordered = [
        t.candidate.classification.attribute_value(Attribute.ENERGY)
        for t in result.tracks
        if t.candidate.classification
    ]
    assert ordered == sorted(energies)

    calm = [
        Candidate(make_track(), make_classification(night_bgm=0.9, energy=e / 4, calmness=0.9), None) for e in energies
    ]
    result = generate_playlist(calm, PlaylistRequest(Mood.CALM_NIGHT, duration_min=None, track_count=5), DEFAULTS)
    ordered = [
        t.candidate.classification.attribute_value(Attribute.ENERGY)
        for t in result.tracks
        if t.candidate.classification
    ]
    assert ordered == sorted((e / 4 for e in energies), reverse=True)


@pytest.mark.parametrize(
    ("mood", "attributes", "expected"),
    [
        (Mood.RELAX, {"relaxation": 1.0, "energy": 0.25, "calmness": 1.0}, 1.0),
        (Mood.RELAX, {"relaxation": 0.0, "energy": 1.0, "calmness": 0.0}, (0 + 0.25 + 0) / 4),
        # 主属性以外が判定保留なら、判定できた属性だけで求める
        (Mood.FOCUS, {"work_bgm": 0.5}, 0.5),
        # 主属性が判定保留なら候補にしない
        (Mood.ENERGETIC, {"party": 1.0, "brightness": 1.0}, None),
        (Mood.OMAKASE, {}, 0.6),
    ],
)
def test_suitability(
    make_classification: ClassificationFactory, mood: Mood, attributes: dict[str, float], expected: float | None
) -> None:
    value = suitability(make_classification(**attributes), mood)

    if expected is None:
        assert value is None
    else:
        assert value == pytest.approx(expected)


def test_suitability_of_unclassified_track_is_none() -> None:
    assert suitability(None, Mood.RELAX) is None


@pytest.mark.parametrize(
    "request_kwargs",
    [
        {"duration_min": None, "track_count": None},
        {"duration_min": 60, "track_count": 10},
        {"duration_min": 10},
        {"duration_min": 200},
        {"duration_min": None, "track_count": 0},
        {"genre_mode": GenreMode.ONLY, "genres": ()},
        {"novelty": 1.5},
        {"randomness": -0.1},
    ],
)
def test_invalid_requests_are_rejected(request_kwargs: dict[str, Any]) -> None:
    request = replace(PlaylistRequest(Mood.RELAX), **request_kwargs)

    with pytest.raises(ValueError, match=r"must|range|exactly"):
        generate_playlist([], request, DEFAULTS)


def test_request_to_dict_is_json_friendly() -> None:
    data = PlaylistRequest(Mood.FOCUS, genre_mode=GenreMode.ONLY, genres=(Genre.JAZZ,)).to_dict()

    assert data["mood"] == "focus"
    assert data["genre_mode"] == "only"
    assert data["genres"] == ["jazz"]


def test_novelty_prefers_tracks_never_used_before(
    make_track: TrackFactory, make_classification: ClassificationFactory
) -> None:
    fit = make_classification(relaxation=0.9, energy=0.25, calmness=0.9)
    familiar = Candidate(make_track(), fit, None, usage_count=5)
    fresh = Candidate(make_track(), fit, None, usage_count=0)

    def first(novelty: float) -> int:
        request = PlaylistRequest(Mood.RELAX, duration_min=None, track_count=1, novelty=novelty, randomness=0.0)
        return generate_playlist([familiar, fresh], request, DEFAULTS).tracks[0].track.id

    assert first(1.0) == fresh.track.id
    assert first(0.0) == familiar.track.id


def test_edit_operations(relaxing_library: list[Candidate]) -> None:
    tracks = generate_playlist(
        relaxing_library, PlaylistRequest(Mood.RELAX, duration_min=None, track_count=3), DEFAULTS
    ).tracks
    ids = [t.track.id for t in tracks]

    assert [t.track.id for t in move_item(tracks, 0, 1)] == [ids[1], ids[0], ids[2]]
    assert [t.track.id for t in move_item(tracks, 0, -5)] == ids
    assert [t.track.id for t in move_item(tracks, 2, 10)] == ids
    assert [t.track.id for t in remove_item(tracks, 1)] == [ids[0], ids[2]]
    extra = next(c for c in relaxing_library if c.track.id not in ids)
    added = add_item(tracks, extra)
    assert added[-1].added_manually
    assert added[-1].score is None
    assert total_duration(added) == 4 * 240
    assert [t.track.id for t in tracks] == ids  # 元のリストは変更しない
    with pytest.raises(ValueError, match="already"):
        add_item(tracks, tracks[0].candidate)
    with pytest.raises(IndexError):
        move_item(tracks, 3, 1)
    with pytest.raises(IndexError):
        remove_item(tracks, -1)


def _hypothesis_track(track_id: int, artist: int, duration: float | None) -> Track:
    return Track(
        id=track_id, library_root="/m", rel_path=f"{track_id}.flac", title=f"t{track_id}", artist=f"a{artist}",
        album=None, genre=None, year=None, track_number=None, duration_sec=duration, bitrate=None, sample_rate=None,
        channels=None, codec=None, file_size=1, mtime_ns=0, title_source=TitleSource.TAG, tags_repaired=False,
        quick_hash=None, status="active", tag_error=None,
    )  # fmt: skip


@st.composite
def random_candidates(draw: st.DrawFn) -> list[Candidate]:
    count = draw(st.integers(min_value=0, max_value=40))
    candidates = []
    for i in range(count):
        duration = draw(st.one_of(st.none(), st.floats(min_value=30, max_value=900)))
        values = {
            attr: draw(st.one_of(st.none(), st.floats(min_value=0, max_value=1)))
            for attr in (Attribute.RELAXATION, Attribute.ENERGY, Attribute.CALMNESS)
        }
        classification = ClassificationResult(
            engine="rules", engine_version="v", model=None, genre=None, genre_confidence=None,
            genre_probabilities=None, mood=None, attributes={a: AttributeScore(v) for a, v in values.items()},
        )  # fmt: skip
        track = _hypothesis_track(i + 1, draw(st.integers(0, 3)), duration)
        candidates.append(Candidate(track, classification, None, draw(st.integers(0, 5))))
    return candidates


@settings(max_examples=60)
@given(
    candidates=random_candidates(),
    duration=st.integers(min_value=30, max_value=180),
    seed=st.integers(min_value=0, max_value=1000),
    randomness=st.floats(min_value=0, max_value=1),
)
def test_generated_playlist_invariants(
    candidates: list[Candidate], duration: int, seed: int, randomness: float
) -> None:
    request = PlaylistRequest(Mood.RELAX, duration_min=duration, seed=seed, randomness=randomness)

    result = generate_playlist(candidates, request, DEFAULTS)

    ids = [t.track.id for t in result.tracks]
    assert len(ids) == len(set(ids))
    assert result.total_duration_sec <= (duration + DEFAULTS.duration_tolerance_min) * 60
    for item in result.tracks:
        assert item.score is not None
        assert item.score >= DEFAULTS.suitability_threshold
    if result.shortfall is None:
        assert result.total_duration_sec >= (duration - DEFAULTS.duration_tolerance_min) * 60
    assert generate_playlist(candidates, request, DEFAULTS).tracks == result.tracks
