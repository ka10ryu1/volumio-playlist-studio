from conftest import ClassificationFactory, TrackFactory

from volumio_playlist_studio.models import Mood
from volumio_playlist_studio.mood_map import MOOD_POSITIONS, MapPoint, build_track_map, track_point
from volumio_playlist_studio.playlist import Candidate


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
