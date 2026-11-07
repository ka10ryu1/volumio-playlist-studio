import sqlite3
from dataclasses import replace
from typing import Any

import pytest
from conftest import CALM, ClassificationFactory

from volumio_playlist_studio.config import AnalysisParams, JevSettings
from volumio_playlist_studio.models import Attribute, Genre, Mood, TitleSource, TrackMetadata
from volumio_playlist_studio.storage import settings_store
from volumio_playlist_studio.storage.analysis import (
    ErrorRecord,
    FeatureRun,
    classification_versions,
    failed_track_ids,
    list_errors,
    load_classifications,
    load_features,
    record_error,
    save_classification,
    save_features,
    tracks_needing_analysis,
    tracks_without_classification,
)
from volumio_playlist_studio.storage.evaluations import (
    EvaluationLabel,
    Split,
    Verdict,
    add_to_evaluation_set,
    load_labels,
    remove_from_evaluation_set,
    save_label,
)
from volumio_playlist_studio.storage.playlists import (
    PlaylistDraft,
    PlaylistEntry,
    RegistrationItem,
    RegistrationRecord,
    get_playlist,
    list_playlists,
    list_registrations,
    save_playlist,
    save_registration,
    set_feedback,
    track_usage_counts,
)
from volumio_playlist_studio.storage.tracks import get_track, upsert_track

ROOT = "/music"


_BASE_META = TrackMetadata(
    rel_path="", title="曲", artist="A", album=None, genre=None, year=None, track_number=None, duration_sec=200.0,
    bitrate=None, sample_rate=None, channels=None, codec="flac", file_size=10, mtime_ns=1,
    title_source=TitleSource.TAG, tags_repaired=False, quick_hash=None,
)  # fmt: skip


def _meta(rel_path: str, **overrides: Any) -> TrackMetadata:
    return replace(_BASE_META, rel_path=rel_path, **overrides)


def _add(db: sqlite3.Connection, rel_path: str, **overrides: Any) -> int:
    return upsert_track(db, ROOT, _meta(rel_path, **overrides), None)


def test_settings_default_and_roundtrip(db: sqlite3.Connection) -> None:
    assert settings_store.get_analysis_params(db) == AnalysisParams()
    assert settings_store.get_jev_settings(db) == JevSettings()
    assert settings_store.get_str(db, settings_store.MUSIC_ROOT_KEY) is None

    settings_store.set_analysis_params(db, AnalysisParams(segment_count=5))
    settings_store.set_jev_settings(db, JevSettings(max_tracks_per_run=7))
    settings_store.set_value(db, settings_store.MUSIC_ROOT_KEY, "/mnt/音楽")

    assert settings_store.get_analysis_params(db).segment_count == 5
    assert settings_store.get_jev_settings(db).max_tracks_per_run == 7
    assert settings_store.get_str(db, settings_store.MUSIC_ROOT_KEY) == "/mnt/音楽"


def test_settings_ignore_unknown_keys_from_older_versions(db: sqlite3.Connection) -> None:
    settings_store.set_value(db, settings_store.JEV_SETTINGS_KEY, {"model": "jev-x", "removed_option": 1})

    assert settings_store.get_jev_settings(db).model == "jev-x"


def test_invalid_analysis_params_are_not_saved(db: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="segment_count"):
        settings_store.set_analysis_params(db, AnalysisParams(segment_count=0))

    assert settings_store.get_analysis_params(db) == AnalysisParams()


def test_features_mark_track_as_analyzed_until_file_changes(db: sqlite3.Connection) -> None:
    track_id = _add(db, "a.flac")
    track = get_track(db, track_id)
    assert track is not None
    assert [t.id for t in tracks_needing_analysis(db, ROOT, "v1")] == [track_id]

    save_features(db, track, CALM, FeatureRun(AnalysisParams(), "v1", 1.5))

    assert tracks_needing_analysis(db, ROOT, "v1") == []
    assert load_features(db) == {track_id: CALM}
    # 解析ロジックのバージョンが変わったら再解析の対象にする
    assert [t.id for t in tracks_needing_analysis(db, ROOT, "v2")] == [track_id]
    # ファイルが更新されたら再解析の対象にする
    _add(db, "a.flac", mtime_ns=2)
    assert [t.id for t in tracks_needing_analysis(db, ROOT, "v1")] == [track_id]


def test_errors_are_resolved_by_later_success(
    db: sqlite3.Connection, make_classification: ClassificationFactory
) -> None:
    track_id = _add(db, "a.flac")
    track = get_track(db, track_id)
    assert track is not None
    record_error(db, track_id, ErrorRecord("features", "LibsndfileError", "broken", "v1"))
    record_error(db, track_id, ErrorRecord("classification", "Timeout", "timeout", "q1"))
    assert len(list_errors(db, ROOT)) == 2

    save_features(db, track, CALM, FeatureRun(AnalysisParams(), "v1", 1.0))
    assert [e.stage for e in list_errors(db, ROOT)] == ["classification"]
    save_classification(db, track_id, make_classification())
    assert list_errors(db, ROOT) == []


def test_classifications_are_versioned(db: sqlite3.Connection, make_classification: ClassificationFactory) -> None:
    track_id = _add(db, "a.flac")
    first = make_classification(genre=Genre.ROCK, energy=0.9)
    second = make_classification(genre=Genre.JAZZ, energy=0.1)
    second = replace(second, engine_version="rules-test-2")

    first_id = save_classification(db, track_id, first)
    assert save_classification(db, track_id, first) == first_id  # 同じバージョンは上書き
    save_classification(db, track_id, second)

    assert load_classifications(db, "rules", "rules-test")[track_id].result == first
    assert load_classifications(db, "rules")[track_id].result.genre == Genre.JAZZ  # 最新
    assert sorted(classification_versions(db)) == [("rules", "rules-test", 1), ("rules", "rules-test-2", 1)]
    assert tracks_without_classification(db, ROOT, "rules", "rules-test") == []
    assert [t.id for t in tracks_without_classification(db, ROOT, "jev", "q1")] == [track_id]


def test_playlist_roundtrip_and_feedback(db: sqlite3.Connection) -> None:
    ids = [_add(db, f"{i}.flac") for i in range(3)]
    draft = PlaylistDraft(
        name="  夜のプレイリスト ",
        conditions={"mood": "calm_night"},
        seed=42,
        classification_source="rules",
        total_duration_sec=600.0,
        entries=[PlaylistEntry(ids[2], 0.9), PlaylistEntry(ids[0], None, added_manually=True)],
    )

    playlist_id = save_playlist(db, draft)
    set_feedback(db, playlist_id, 4, {1})

    saved = get_playlist(db, playlist_id)
    assert saved is not None
    assert saved.name == "夜のプレイリスト"
    assert saved.conditions == {"mood": "calm_night"}
    assert [(e.track_id, e.added_manually, e.unsuitable) for e in saved.entries] == [
        (ids[2], False, False),
        (ids[0], True, True),
    ]
    assert saved.satisfaction == 4
    assert [p.id for p in list_playlists(db)] == [playlist_id]
    assert track_usage_counts(db) == {ids[2]: 1, ids[0]: 1}
    assert get_playlist(db, 999) is None


@pytest.mark.parametrize(("name", "entries"), [("   ", [PlaylistEntry(1, None)]), ("名前", [])])
def test_invalid_playlist_is_rejected(db: sqlite3.Connection, name: str, entries: list[PlaylistEntry]) -> None:
    _add(db, "a.flac")
    draft = PlaylistDraft(name, {}, 0, "rules", 0.0, entries)

    with pytest.raises(ValueError, match="playlist"):
        save_playlist(db, draft)


@pytest.mark.parametrize("satisfaction", [0, 6])
def test_satisfaction_must_be_between_one_and_five(db: sqlite3.Connection, satisfaction: int) -> None:
    track_id = _add(db, "a.flac")
    playlist_id = save_playlist(db, PlaylistDraft("p", {}, 0, "rules", 0.0, [PlaylistEntry(track_id, None)]))

    with pytest.raises(ValueError, match="satisfaction"):
        set_feedback(db, playlist_id, satisfaction, set())


def test_registration_history(db: sqlite3.Connection) -> None:
    track_id = _add(db, "a.flac")
    playlist_id = save_playlist(db, PlaylistDraft("p", {}, 0, "rules", 0.0, [PlaylistEntry(track_id, None)]))
    record = RegistrationRecord(
        playlist_id, "p", "mock", "partial", "2 曲目で失敗", "2026-01-01T00:00:00+00:00",
        [
            RegistrationItem(0, track_id, "music-library/a.flac", "added"),
            RegistrationItem(1, track_id, None, "skipped"),
        ],
    )  # fmt: skip

    save_registration(db, record)

    [saved] = list_registrations(db, playlist_id)
    assert saved.status == "partial"
    assert saved.items == record.items


def test_evaluation_labels(db: sqlite3.Connection) -> None:
    a, b = _add(db, "a.flac"), _add(db, "b.flac")
    add_to_evaluation_set(db, {a: Split.TUNING, b: Split.HOLDOUT})
    add_to_evaluation_set(db, {a: Split.HOLDOUT})  # 既存の用途は変えない

    save_label(
        db,
        EvaluationLabel(
            track_id=a,
            split=Split.HOLDOUT,
            genre_verdict=Verdict.INCORRECT,
            genre_label=Genre.JAZZ,
            mood_verdict=Verdict.PENDING,
            ratings={Attribute.RELAXATION: 5, Attribute.ENERGY: 1},
            note="メモ",
        ),
    )

    labels = load_labels(db)
    assert labels[a].split == Split.TUNING
    assert labels[a].genre_label == Genre.JAZZ
    assert labels[a].mood_label is None
    assert labels[a].ratings == {Attribute.RELAXATION: 5, Attribute.ENERGY: 1}
    assert labels[a].is_evaluated
    assert labels[a].evaluated_at is not None
    assert not labels[b].is_evaluated

    remove_from_evaluation_set(db, b)
    assert set(load_labels(db)) == {a}


def test_label_for_track_outside_evaluation_set_is_rejected(db: sqlite3.Connection) -> None:
    track_id = _add(db, "a.flac")

    with pytest.raises(ValueError, match="evaluation set"):
        save_label(db, EvaluationLabel(track_id=track_id, split=Split.TUNING, mood_label=Mood.RELAX))


@pytest.mark.parametrize("rating", [0, 6])
def test_rating_outside_scale_is_rejected(db: sqlite3.Connection, rating: int) -> None:
    track_id = _add(db, "a.flac")
    add_to_evaluation_set(db, {track_id: Split.TUNING})

    with pytest.raises(ValueError, match="rating"):
        save_label(db, EvaluationLabel(track_id, Split.TUNING, ratings={Attribute.ENERGY: rating}))


def test_evaluation_keeps_snapshot_of_shown_classification(
    db: sqlite3.Connection, make_classification: ClassificationFactory
) -> None:
    track_id = _add(db, "a.flac", genre="Jazz")
    tag_only = make_classification(genre=Genre.JAZZ, energy=None)
    classification_id = save_classification(db, track_id, tag_only)
    add_to_evaluation_set(db, {track_id: Split.HOLDOUT})
    save_label(
        db,
        EvaluationLabel(
            track_id,
            Split.HOLDOUT,
            classification_id=classification_id,
            classification_snapshot=tag_only,
            genre_verdict=Verdict.CORRECT,
            genre_label=Genre.JAZZ,
        ),
    )

    # 音響解析の後に、同じエンジン・バージョンで分類し直すと分類結果の行は上書きされる
    reanalyzed = make_classification(genre=Genre.ROCK, energy=0.9)
    assert save_classification(db, track_id, reanalyzed) == classification_id

    label = load_labels(db)[track_id]
    assert label.classification_snapshot == tag_only
    assert load_classifications(db, "rules")[track_id].result == reanalyzed


def test_record_error_keeps_only_latest_unresolved_error_per_track_and_stage(db: sqlite3.Connection) -> None:
    a, b = _add(db, "a.flac"), _add(db, "b.flac")
    record_error(db, a, ErrorRecord("features", "LibsndfileError", "1 回目", "v1", 10, 1))
    record_error(db, a, ErrorRecord("features", "LibsndfileError", "2 回目", "v1", 10, 1))
    record_error(db, a, ErrorRecord("classification", "Timeout", "別の段階", "q1"))
    record_error(db, b, ErrorRecord("features", "LibsndfileError", "別の曲", "v1", 10, 1))
    # 楽曲に紐づかないエラー（読めないフォルダなど）は、まとめずにすべて残す
    record_error(db, None, ErrorRecord("tags", "DirectoryReadError", "フォルダ 1", None))
    record_error(db, None, ErrorRecord("tags", "DirectoryReadError", "フォルダ 2", None))

    messages = sorted(e.message for e in list_errors(db, ROOT))

    assert messages == sorted(["2 回目", "別の段階", "別の曲", "フォルダ 1", "フォルダ 2"])


def test_failed_track_ids_follow_file_changes_and_version(db: sqlite3.Connection) -> None:
    track_id = _add(db, "a.flac", file_size=10, mtime_ns=1)
    record_error(db, track_id, ErrorRecord("features", "LibsndfileError", "broken", "v1", 10, 1))

    assert failed_track_ids(db, ROOT, "v1") == {track_id}
    # 解析ロジックのバージョンが変われば、改めて解析する
    assert failed_track_ids(db, ROOT, "v2") == set()
    # ファイルが更新されれば、改めて解析する
    _add(db, "a.flac", file_size=10, mtime_ns=2)
    assert failed_track_ids(db, ROOT, "v1") == set()
