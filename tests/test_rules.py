from dataclasses import replace

import pytest
from conftest import CALM, TrackFactory
from hypothesis import given
from hypothesis import strategies as st

from volumio_playlist_studio.classify.base import decide_mood, mood_scores
from volumio_playlist_studio.classify.genre_tags import genre_from_tag
from volumio_playlist_studio.classify.rules import RuleClassifier, normalize
from volumio_playlist_studio.config import RULE_VERSION, MoodDecision
from volumio_playlist_studio.models import (
    Attribute,
    AttributeScore,
    AudioFeatures,
    ClassificationResult,
    Genre,
    Mood,
    TitleSource,
    Track,
)

INTENSE = replace(
    CALM,
    tempo_bpm=150.0,
    rms_mean_db=-9.0,
    loudness_variation_db=6.0,
    spectral_centroid_mean_hz=3300.0,
    spectral_rolloff_mean_hz=6800.0,
    zero_crossing_rate_mean=0.14,
    onset_rate_per_sec=4.8,
)


@pytest.mark.parametrize(
    ("tag", "expected"),
    [
        ("Rock", Genre.ROCK),
        ("J-Rock", Genre.ROCK),
        ("Heavy Metal Rock", Genre.METAL),
        ("Pop Rock", Genre.ROCK),
        ("J-Pop", Genre.POP),
        ("アニメ", Genre.SOUNDTRACK),
        ("Original Soundtrack", Genre.SOUNDTRACK),
        ("ジャズ", Genre.JAZZ),
        ("Bossa Nova", Genre.JAZZ),
        ("クラシック", Genre.CLASSICAL),
        ("Hip-Hop", Genre.HIPHOP),
        ("Rap", Genre.HIPHOP),
        ("R&B", Genre.RNB_SOUL),
        ("Synthpop", Genre.ELECTRONIC),
        ("Drum & Bass", Genre.ELECTRONIC),
        ("New Age", Genre.AMBIENT),
        ("Singer-Songwriter", Genre.FOLK_ACOUSTIC),
        ("Reggae", Genre.WORLD),
        ("Therapy", None),  # "rap" を単語の一部として誤検出しない
        ("Spoken Word", None),
        ("", None),
        ("   ", None),
        (None, None),
    ],
)
def test_genre_from_tag(tag: str | None, expected: Genre | None) -> None:
    assert genre_from_tag(tag) == expected


@pytest.mark.parametrize(("value", "expected"), [(0.0, 0.0), (5.0, 0.5), (10.0, 1.0), (-3.0, 0.0), (99.0, 1.0)])
def test_normalize(value: float, expected: float) -> None:
    assert normalize(value, (0.0, 10.0)) == expected


def test_normalize_rejects_invalid_bounds() -> None:
    with pytest.raises(ValueError, match="upper bound"):
        normalize(1.0, (5.0, 5.0))


def test_without_features_all_attributes_are_held(make_track: TrackFactory) -> None:
    result = RuleClassifier().classify(make_track(genre="Jazz"), None)

    assert result.genre == Genre.JAZZ
    assert result.engine == "rules"
    assert result.engine_version == RULE_VERSION
    assert all(score.is_held for score in result.attributes.values())
    assert set(result.attributes) == set(Attribute)
    assert result.mood is None


def test_unknown_genre_tag_is_left_unknown(make_track: TrackFactory) -> None:
    result = RuleClassifier().classify(make_track(genre="Spoken Word"), CALM)

    assert result.genre is None
    assert any("ジャンル" in note for note in result.notes)


def test_calm_and_intense_tracks_are_ordered_as_expected(make_track: TrackFactory) -> None:
    classifier = RuleClassifier()
    calm = classifier.classify(make_track(), CALM)
    intense = classifier.classify(make_track(), INTENSE)

    def value(result: ClassificationResult, attribute: Attribute) -> float:
        v = result.attribute_value(attribute)
        assert v is not None
        return v

    assert value(intense, Attribute.ENERGY) > value(calm, Attribute.ENERGY)
    assert value(intense, Attribute.PARTY) > value(calm, Attribute.PARTY)
    assert value(calm, Attribute.RELAXATION) > value(intense, Attribute.RELAXATION)
    assert value(calm, Attribute.NIGHT_BGM) > value(intense, Attribute.NIGHT_BGM)
    assert intense.mood == Mood.ENERGETIC
    # 静かな曲は「リラックス」と「穏やかな夜」の両方に強く当てはまる
    top_two = sorted(mood_scores(calm.attributes).items(), key=lambda item: item[1], reverse=True)[:2]
    assert {mood for mood, _ in top_two} == {Mood.RELAX, Mood.CALM_NIGHT}


def test_brightness_is_always_held_by_rules(make_track: TrackFactory) -> None:
    # 曲調の明暗は基本的な特徴量からは判断しない
    for features in (CALM, INTENSE):
        assert RuleClassifier().classify(make_track(), features).attributes[Attribute.BRIGHTNESS].is_held


def test_single_feature_does_not_decide_energy(make_track: TrackFactory) -> None:
    # テンポだけが速い静かな曲は、エネルギー感が最大にはならない
    fast_but_quiet = replace(CALM, tempo_bpm=180.0)

    energy = RuleClassifier().classify(make_track(), fast_but_quiet).attribute_value(Attribute.ENERGY)

    assert energy is not None
    assert energy < 0.5


finite = st.floats(min_value=-1e4, max_value=1e4, allow_nan=False, allow_infinity=False)
positive = st.floats(min_value=0.0, max_value=1e4, allow_nan=False, allow_infinity=False)

# Hypothesis のテストでは関数スコープの fixture を使えないため、固定の楽曲を使う
TRACK = Track(
    id=1,
    library_root="/music",
    rel_path="a.flac",
    title="曲",
    artist="アーティスト",
    album=None,
    genre=None,
    year=None,
    track_number=None,
    duration_sec=200.0,
    bitrate=None,
    sample_rate=None,
    channels=None,
    codec=None,
    file_size=1,
    mtime_ns=0,
    title_source=TitleSource.TAG,
    tags_repaired=False,
    quick_hash=None,
    status="active",
    tag_error=None,
)


@st.composite
def arbitrary_features(draw: st.DrawFn) -> AudioFeatures:
    return replace(
        CALM,
        tempo_bpm=draw(positive),
        tempo_variation=draw(positive),
        rms_mean_db=draw(finite),
        loudness_variation_db=draw(positive),
        spectral_centroid_mean_hz=draw(positive),
        spectral_rolloff_mean_hz=draw(positive),
        zero_crossing_rate_mean=draw(st.floats(min_value=0.0, max_value=1.0)),
        onset_rate_per_sec=draw(positive),
    )


@given(arbitrary_features())
def test_rule_attributes_are_always_between_zero_and_one(features: AudioFeatures) -> None:
    result = RuleClassifier().classify(TRACK, features)

    for score in result.attributes.values():
        assert score.value is None or 0.0 <= score.value <= 1.0


def _scores(**values: float | None) -> dict[Attribute, AttributeScore]:
    return {Attribute(k): AttributeScore(value=v) for k, v in values.items()}


def test_mood_scores_skip_moods_with_held_attributes() -> None:
    scores = mood_scores(_scores(relaxation=0.8, energy=0.4, party=None, work_bgm=0.5, night_bgm=None))

    assert scores == {Mood.RELAX: 0.8, Mood.FOCUS: 0.5}


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ({"relaxation": 0.9, "work_bgm": 0.5, "energy": 0.2, "party": 0.2, "night_bgm": 0.6}, Mood.RELAX),
        # 最高でも基準未満なら判定保留
        ({"relaxation": 0.55, "work_bgm": 0.5, "energy": 0.2, "party": 0.2, "night_bgm": 0.5}, None),
        # 1 位と 2 位の差が小さければ判定保留
        ({"relaxation": 0.85, "work_bgm": 0.5, "energy": 0.2, "party": 0.2, "night_bgm": 0.8}, None),
        ({}, None),
        ({"energy": 0.9, "party": 0.8}, Mood.ENERGETIC),
    ],
)
def test_decide_mood(values: dict[str, float], expected: Mood | None) -> None:
    assert decide_mood(_scores(**values), MoodDecision()) == expected
