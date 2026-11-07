import csv
import io

import pytest
from conftest import ClassificationFactory, TrackFactory
from hypothesis import given
from hypothesis import strategies as st

from volumio_playlist_studio.config import EvaluationDefaults
from volumio_playlist_studio.evaluation import (
    assign_splits,
    compute_engine_metrics,
    export_csv,
    initial_verdict,
    playlist_feedback_metrics,
    rating_to_unit,
    select_evaluation_sample,
)
from volumio_playlist_studio.models import Attribute, Genre, Mood
from volumio_playlist_studio.storage.evaluations import EvaluationLabel, Split, Verdict
from volumio_playlist_studio.storage.playlists import PlaylistEntry, SavedPlaylist


@given(ids=st.lists(st.integers(), unique=True, max_size=100), seed=st.integers(0, 10_000))
def test_assign_splits_is_deterministic_disjoint_and_balanced(ids: list[int], seed: int) -> None:
    splits = assign_splits(ids, seed, 0.5)

    assert set(splits) == set(ids)
    assert splits == assign_splits(ids, seed, 0.5)
    tuning = sum(1 for s in splits.values() if s == Split.TUNING)
    assert tuning == round(len(ids) * 0.5)


def test_select_evaluation_sample_excludes_already_selected(make_track: TrackFactory) -> None:
    tracks = [make_track() for _ in range(60)]
    already = {t.id for t in tracks[:25]}

    picked = select_evaluation_sample(tracks, already, 30, seed=3, defaults=EvaluationDefaults())

    assert len(picked) == 30
    assert not set(picked) & already
    assert sum(1 for s in picked.values() if s == Split.HOLDOUT) == 15
    assert select_evaluation_sample(tracks, already, 30, seed=3, defaults=EvaluationDefaults()) == picked


def test_select_evaluation_sample_with_small_library_takes_all(make_track: TrackFactory) -> None:
    tracks = [make_track() for _ in range(10)]

    assert len(select_evaluation_sample(tracks, set(), 40, seed=0, defaults=EvaluationDefaults())) == 10


@pytest.mark.parametrize("size", [29, 51])
def test_select_evaluation_sample_rejects_out_of_range_size(make_track: TrackFactory, size: int) -> None:
    with pytest.raises(ValueError, match="sample size"):
        select_evaluation_sample([make_track()], set(), size, seed=0, defaults=EvaluationDefaults())


@pytest.mark.parametrize(("rating", "expected"), [(1, 0.0), (3, 0.5), (5, 1.0)])
def test_rating_to_unit(rating: int, expected: float) -> None:
    assert rating_to_unit(rating) == expected


def test_engine_metrics(make_classification: ClassificationFactory) -> None:
    labels = {
        # ジャンル正解・ムード不正解
        1: EvaluationLabel(1, Split.HOLDOUT, genre_verdict=Verdict.CORRECT, genre_label=Genre.JAZZ,
                           mood_verdict=Verdict.INCORRECT, mood_label=Mood.FOCUS,
                           ratings={Attribute.RELAXATION: 5, Attribute.ENERGY: 1}),
        # ジャンル不正解（ロックをポップと判定）
        2: EvaluationLabel(2, Split.HOLDOUT, genre_verdict=Verdict.INCORRECT, genre_label=Genre.ROCK,
                           mood_verdict=Verdict.CORRECT, mood_label=Mood.ENERGETIC,
                           ratings={Attribute.RELAXATION: 1}),
        # 分類エンジンはジャンルを判定保留にした
        3: EvaluationLabel(3, Split.HOLDOUT, genre_verdict=Verdict.INCORRECT, genre_label=Genre.CLASSICAL,
                           mood_verdict=Verdict.PENDING),
        # 人も判断できなかった曲は一致率に含めない
        4: EvaluationLabel(4, Split.HOLDOUT, genre_verdict=Verdict.PENDING, mood_verdict=Verdict.PENDING),
        # 調整用は検証用の指標に含めない
        5: EvaluationLabel(5, Split.TUNING, genre_verdict=Verdict.CORRECT, genre_label=Genre.POP),
        # 未評価
        6: EvaluationLabel(6, Split.HOLDOUT),
    }  # fmt: skip
    classifications = {
        1: make_classification(genre=Genre.JAZZ, mood=Mood.RELAX, relaxation=0.75, energy=0.5),
        2: make_classification(genre=Genre.POP, mood=Mood.ENERGETIC, relaxation=None),
        3: make_classification(genre=None, mood=None),
        4: make_classification(genre=Genre.POP),
        5: make_classification(genre=Genre.POP),
        6: make_classification(genre=Genre.POP),
    }

    metrics = compute_engine_metrics("rules", labels, classifications, Split.HOLDOUT)

    assert metrics.evaluated == 4
    assert (metrics.genre.labeled, metrics.genre.decided, metrics.genre.correct) == (3, 2, 1)
    assert metrics.genre.accuracy == 0.5
    assert metrics.genre.pending_rate == pytest.approx(1 / 3)
    assert metrics.genre.confusions == [("rock", "pop", 1)]
    assert (metrics.mood.labeled, metrics.mood.decided, metrics.mood.correct) == (2, 2, 1)
    assert metrics.mood.confusions == [("focus", "relax", 1)]
    # 人手 5 → 1.0 と 0.75 の差、人手 1 → 0.0 と 0.5 の差。判定保留の属性は比較しない
    assert metrics.attribute_mae == {Attribute.RELAXATION: (0.25, 1), Attribute.ENERGY: (0.5, 1)}

    everything = compute_engine_metrics("rules", labels, classifications, None)
    assert everything.evaluated == 5
    assert everything.genre.correct == 2


def test_engine_metrics_without_data(make_classification: ClassificationFactory) -> None:
    metrics = compute_engine_metrics("jev", {}, {}, None)

    assert metrics.evaluated == 0
    assert metrics.genre.accuracy is None
    assert metrics.genre.pending_rate is None


def test_playlist_feedback_metrics() -> None:
    def playlist(pid: int, source: str, satisfaction: int | None, unsuitable: list[bool]) -> SavedPlaylist:
        entries = [PlaylistEntry(i, None, unsuitable=u) for i, u in enumerate(unsuitable)]
        return SavedPlaylist(pid, "p", {}, 0, source, 0.0, satisfaction, "now", entries)

    metrics = playlist_feedback_metrics(
        [
            playlist(1, "rules", 2, [True, False, False, False]),
            playlist(2, "rules", 4, [True, True, False, False]),
            playlist(3, "jev", None, [True]),  # 満足度が未入力のものは数えない
        ]
    )

    assert len(metrics) == 1
    assert (metrics[0].source, metrics[0].playlists, metrics[0].tracks, metrics[0].unsuitable) == ("rules", 2, 8, 3)
    assert metrics[0].unsuitable_rate == 3 / 8
    assert metrics[0].mean_satisfaction == 3.0


def test_export_csv(make_track: TrackFactory, make_classification: ClassificationFactory) -> None:
    track = make_track(title='=HYPERLINK("x")', artist="日本語の歌手", genre="ジャズ")
    labels = {
        track.id: EvaluationLabel(
            track.id, Split.HOLDOUT, genre_verdict=Verdict.CORRECT, genre_label=Genre.JAZZ,
            ratings={Attribute.ENERGY: 2}, note="+メモ",
            classification_snapshot=make_classification(genre=Genre.FOLK_ACOUSTIC, mood=Mood.RELAX),
        )
    }  # fmt: skip
    classifications = {"rules": {track.id: make_classification(genre=Genre.JAZZ, energy=0.3)}, "jev": {}}

    rows = list(csv.DictReader(io.StringIO(export_csv({track.id: track}, labels, classifications))))

    assert len(rows) == 1
    row = rows[0]
    # 表計算ソフトで数式として実行されないようにする
    assert row["title"] == '\'=HYPERLINK("x")'
    assert row["note"] == "'+メモ"
    assert row["artist"] == "日本語の歌手"
    assert row["rules_genre"] == "jazz"
    assert row["rules_energy"] == "0.3"
    assert row["jev_genre"] == ""
    # 評価時に表示していた結果と、現在の分類結果は別に出力する
    assert (row["evaluated_engine"], row["evaluated_genre"], row["evaluated_mood"]) == (
        "rules",
        "folk_acoustic",
        "relax",
    )
    assert row["genre_verdict"] == "correct"
    assert row["rating_energy"] == "2"
    assert row["rating_relaxation"] == ""


@pytest.mark.parametrize(
    ("stored_verdict", "stored_label", "predicted", "expected"),
    [
        # 未評価なら何も選ばない（うっかり「正しい」のまま保存しないように）
        (None, None, Genre.JAZZ, (None, None)),
        (Verdict.CORRECT, Genre.JAZZ, Genre.JAZZ, (Verdict.CORRECT, None)),
        (Verdict.INCORRECT, Genre.ROCK, Genre.JAZZ, (Verdict.INCORRECT, Genre.ROCK)),
        (Verdict.PENDING, None, Genre.JAZZ, (Verdict.PENDING, None)),
        # 「正しい」と評価した後に判定が変わった場合は、保存済みの正解を残すため「誤り」として開く
        (Verdict.CORRECT, Genre.JAZZ, Genre.POP, (Verdict.INCORRECT, Genre.JAZZ)),
        # 「誤り」と評価した後に判定が正解と同じになった場合は「正しい」として開く
        (Verdict.INCORRECT, Genre.ROCK, Genre.ROCK, (Verdict.CORRECT, None)),
        (Verdict.INCORRECT, Genre.ROCK, None, (Verdict.INCORRECT, Genre.ROCK)),
    ],
)
def test_initial_verdict(
    stored_verdict: Verdict | None,
    stored_label: Genre | None,
    predicted: Genre | None,
    expected: tuple[Verdict | None, Genre | None],
) -> None:
    assert initial_verdict(stored_verdict, stored_label, predicted) == expected


def test_initial_verdict_for_mood() -> None:
    assert initial_verdict(Verdict.INCORRECT, Mood.FOCUS, Mood.RELAX) == (Verdict.INCORRECT, Mood.FOCUS)
