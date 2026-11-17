import json

import pytest
from conftest import ClassificationFactory, TrackFactory
from hypothesis import given, settings
from hypothesis import strategies as st

from volumio_playlist_studio.artist_playlist import (
    ArtistRequest,
    ArtistSummary,
    generate_artist_playlist,
    list_artists,
)
from volumio_playlist_studio.config import PlaylistDefaults
from volumio_playlist_studio.models import Genre, Mood, TitleSource, Track
from volumio_playlist_studio.playlist import Candidate, ChildrenMode, GenreMode

DEFAULTS = PlaylistDefaults()


def candidate(make_track: TrackFactory, artist: str | None, **overrides: object) -> Candidate:
    """未分類の曲の候補。"""
    return Candidate(make_track(artist=artist, **overrides), None, None)


def test_artists_are_listed_by_name_with_track_counts(make_track: TrackFactory) -> None:
    candidates = [
        candidate(make_track, "Beta"),
        candidate(make_track, "alpha"),
        candidate(make_track, "Alpha"),
        candidate(make_track, " Alpha "),
        candidate(make_track, None),
        candidate(make_track, "   "),
    ]

    # 大文字・小文字と前後の空白の違いはまとめ、最も多い表記で表示する。不明なアーティストは含めない
    assert list_artists(candidates) == [ArtistSummary("Alpha", 3), ArtistSummary("Beta", 1)]


def test_artist_name_spelling_tie_is_resolved_deterministically(make_track: TrackFactory) -> None:
    forward = [candidate(make_track, "abc"), candidate(make_track, "ABC")]

    assert list_artists(forward) == list_artists(list(reversed(forward))) == [ArtistSummary("ABC", 2)]


def test_only_selected_artists_are_used(make_track: TrackFactory) -> None:
    candidates = [candidate(make_track, artist) for artist in ["A", "a ", "B", "C", "A feat. B", None]]
    request = ArtistRequest(artists=("A", "B"), duration_min=None, track_count=10)

    result = generate_artist_playlist(candidates, request, DEFAULTS)

    # 未分類の曲も、雰囲気が「おまかせ」なら選べる。「A feat. B」は別のアーティスト
    assert sorted(t.track.artist or "" for t in result.tracks) == ["A", "B", "a "]


def test_mood_narrows_tracks_of_selected_artist(
    make_track: TrackFactory, make_classification: ClassificationFactory
) -> None:
    calm = make_classification(relaxation=0.9, energy=0.2, calmness=0.9)
    loud = make_classification(relaxation=0.1, energy=0.9, calmness=0.1)
    relaxing = Candidate(make_track(artist="A"), calm, calm.genre)
    candidates = [
        relaxing,
        Candidate(make_track(artist="A"), loud, loud.genre),
        candidate(make_track, "A"),
        Candidate(make_track(artist="B"), calm, calm.genre),
    ]
    request = ArtistRequest(artists=("A",), mood=Mood.RELAX, duration_min=None, track_count=3)

    result = generate_artist_playlist(candidates, request, DEFAULTS)

    assert [t.track.id for t in result.tracks] == [relaxing.track.id]
    assert result.shortfall is not None
    assert result.shortfall.suggestions[:2] == [
        "アーティストを追加する",
        "雰囲気を「おまかせ」にする（雰囲気で絞り込まない）",
    ]
    # 指定外のアーティストの曲は、除外した曲として数えない
    assert sum(result.shortfall.excluded.values()) == 2


def test_shortfall_suggests_including_children_tracks(make_track: TrackFactory) -> None:
    candidates = [Candidate(make_track(artist="A"), None, None, for_children=True) for _ in range(3)]
    request = ArtistRequest(artists=("A",), duration_min=None, track_count=3)

    result = generate_artist_playlist(candidates, request, DEFAULTS)

    assert result.tracks == []
    assert result.shortfall is not None
    assert "子供向けの曲を「含める」にする" in result.shortfall.suggestions

    included = generate_artist_playlist(
        candidates,
        ArtistRequest(artists=("A",), duration_min=None, track_count=3, children_mode=ChildrenMode.INCLUDE),
        DEFAULTS,
    )
    assert len(included.tracks) == 3
    assert included.shortfall is None


def test_single_artist_does_not_warn_about_consecutive_tracks(make_track: TrackFactory) -> None:
    candidates = [candidate(make_track, "A") for _ in range(5)] + [candidate(make_track, "B")]

    single = generate_artist_playlist(
        candidates, ArtistRequest(artists=("A",), duration_min=None, track_count=5), DEFAULTS
    )
    assert len(single.tracks) == 5
    assert single.warnings == []

    # 複数のアーティストなら、連続を避けきれないことを伝える
    both = generate_artist_playlist(
        candidates, ArtistRequest(artists=("A", "B"), duration_min=None, track_count=6), DEFAULTS
    )
    assert both.warnings
    assert both.request.avoid_same_artist


def test_genre_condition_applies_to_selected_artist(
    make_track: TrackFactory, make_classification: ClassificationFactory
) -> None:
    jazz = make_classification(genre=Genre.JAZZ)
    rock = make_classification(genre=Genre.ROCK)
    candidates = [
        Candidate(make_track(artist="A"), jazz, jazz.genre),
        Candidate(make_track(artist="A"), rock, rock.genre),
    ]
    request = ArtistRequest(
        artists=("A",), duration_min=None, track_count=2, genre_mode=GenreMode.ONLY, genres=(Genre.JAZZ,)
    )

    result = generate_artist_playlist(candidates, request, DEFAULTS)

    assert [t.candidate.genre for t in result.tracks] == [Genre.JAZZ]


@pytest.mark.parametrize("artists", [(), ("  ",)])
def test_request_without_artist_is_rejected(make_track: TrackFactory, artists: tuple[str, ...]) -> None:
    request = ArtistRequest(artists=artists, duration_min=None, track_count=1)

    with pytest.raises(ValueError, match="artist"):
        generate_artist_playlist([candidate(make_track, "A")], request, DEFAULTS)


def test_request_inherits_validation_of_length() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        ArtistRequest(artists=("A",), duration_min=30, track_count=5).validate(DEFAULTS)


def test_conditions_are_saved_with_method_and_artists() -> None:
    request = ArtistRequest(artists=("A", "B"), mood=Mood.CAFE, duration_min=30, genres=(Genre.JAZZ,))

    data = request.to_dict()

    assert data["method"] == "artist"
    assert data["artists"] == ["A", "B"]
    assert data["mood"] == "cafe"
    assert data["genres"] == ["jazz"]
    # DB に JSON で保存できる
    assert json.loads(json.dumps(data)) == data


def _hypothesis_candidate(track_id: int, artist: str | None) -> Candidate:
    track = Track(
        id=track_id, library_root="/m", rel_path=f"{track_id}.flac", title=f"t{track_id}", artist=artist,
        album=None, genre=None, year=None, track_number=None, duration_sec=200.0, bitrate=None, sample_rate=None,
        channels=None, codec=None, file_size=1, mtime_ns=0, title_source=TitleSource.TAG, tags_repaired=False,
        quick_hash=None, status="active", tag_error=None,
    )  # fmt: skip
    return Candidate(track, None, None)


@settings(max_examples=50, deadline=None)
@given(
    artists=st.lists(st.sampled_from(["A", "a", "B", "C", None]), min_size=1, max_size=20),
    selected=st.sets(st.sampled_from(["A", "B", "C"]), min_size=1),
    count=st.integers(min_value=1, max_value=20),
    seed=st.integers(min_value=0, max_value=1000),
)
def test_selected_tracks_always_belong_to_selected_artists(
    artists: list[str | None], selected: set[str], count: int, seed: int
) -> None:
    candidates = [_hypothesis_candidate(i + 1, artist) for i, artist in enumerate(artists)]
    request = ArtistRequest(
        artists=tuple(sorted(selected)), duration_min=None, track_count=count, randomness=1.0, seed=seed
    )

    first = generate_artist_playlist(candidates, request, DEFAULTS)
    second = generate_artist_playlist(list(reversed(candidates)), request, DEFAULTS)

    keys = {s.casefold() for s in selected}
    assert all((t.track.artist or "").casefold() in keys for t in first.tracks)
    assert len(first.tracks) == min(count, sum(1 for a in artists if a is not None and a.casefold() in keys))
    # 同じ条件・seed なら、候補の並びによらず同じ結果になる
    assert [t.track.id for t in first.tracks] == [t.track.id for t in second.tracks]
