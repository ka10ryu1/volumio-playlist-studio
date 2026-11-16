"""ダッシュボードの状態表示と、選曲・保存済みプレイリストへの導線を検証する。"""

import sqlite3

import pytest
from streamlit.testing.v1 import AppTest
from test_gui import APP, TIMEOUT, click, navigate, run_view
from test_gui_manage import save_demo_playlist

from volumio_playlist_studio.config import ANALYSIS_VERSION
from volumio_playlist_studio.models import ClassificationSource, Genre, Mood, TitleSource, TrackMetadata
from volumio_playlist_studio.playlist import GenreMode
from volumio_playlist_studio.sample_data import DEMO_LIBRARY_ROOT
from volumio_playlist_studio.storage import settings_store
from volumio_playlist_studio.storage.analysis import ErrorRecord, record_error
from volumio_playlist_studio.storage.database import transaction
from volumio_playlist_studio.storage.evaluations import Split, add_to_evaluation_set
from volumio_playlist_studio.storage.playlists import delete_playlist
from volumio_playlist_studio.storage.tracks import list_tracks, upsert_track


def test_empty_library_leads_to_scan(app_db: sqlite3.Connection) -> None:
    with transaction(app_db):
        settings_store.set_value(app_db, settings_store.MUSIC_ROOT_KEY, "/empty-music")

    at = AppTest.from_file(APP, default_timeout=TIMEOUT).run()

    assert not at.exception
    assert any("スキャンして" in message.value for message in at.info)
    assert not any(button.label == "プレイリストを作成" for button in at.button)
    click(at, "音楽ライブラリを開く")
    assert at.title[0].value == "音楽ライブラリ"


def test_dashboard_shows_empty_recent_list_and_opens_management(demo: sqlite3.Connection) -> None:
    at = AppTest.from_file(APP, default_timeout=TIMEOUT).run()

    assert not at.exception
    assert any("保存すると、ここから曲目を確認" in caption.value for caption in at.caption)
    click(at, "保存したプレイリストを見る")
    assert at.title[0].value == "プレイリスト管理"
    assert any("保存したプレイリストがありません" in caption.value for caption in at.caption)


def test_recent_playlists_are_latest_three_excluding_deleted(demo: sqlite3.Connection) -> None:
    for name in ("古いリスト", "朝のリスト", "昼のリスト", "夜のリスト"):
        save_demo_playlist(demo, name)

    deleted = save_demo_playlist(demo, "削除したリスト")
    with transaction(demo):
        delete_playlist(demo, deleted)

    at = AppTest.from_file(APP, default_timeout=TIMEOUT).run()

    assert not at.exception
    recent = [button.label for button in at.button if button.key and button.key.startswith("dashboard-playlist-")]
    assert recent == ["夜のリスト", "昼のリスト", "朝のリスト"]
    assert [caption.value for caption in at.caption].count("3 曲 ・ 9:00") == 3
    click(at, "昼のリスト")
    assert at.title[0].value == "プレイリスト管理"
    assert any(header.value == "昼のリスト" for header in at.subheader)
    assert len(at.dataframe[0].value) == 3
    click(at, "更新")
    assert any(header.value == "昼のリスト" for header in at.subheader)
    click(at, "一覧に戻る")
    assert at.dataframe[0].value["名前"].tolist() == ["夜のリスト", "昼のリスト", "朝のリスト", "古いリスト"]


def test_recent_playlist_deleted_before_open_is_explained(demo: sqlite3.Connection) -> None:
    playlist_id = save_demo_playlist(demo, "消えるリスト")
    at = AppTest.from_file(APP, default_timeout=TIMEOUT).run()
    with transaction(demo):
        delete_playlist(demo, playlist_id)

    click(at, "消えるリスト")

    assert any("削除されています" in message.value for message in at.info)
    assert not any(header.value == "消えるリスト" for header in at.subheader)
    assert any("保存したプレイリストがありません" in caption.value for caption in at.caption)


def test_dashboard_evaluation_count_and_navigation(demo: sqlite3.Connection) -> None:
    tracks = list_tracks(demo, DEMO_LIBRARY_ROOT)
    with transaction(demo):
        add_to_evaluation_set(demo, {tracks[0].id: Split.TUNING, tracks[1].id: Split.HOLDOUT})

    at = AppTest.from_file(APP, default_timeout=TIMEOUT).run()

    assert not at.exception
    assert any("評価対象のうち未評価 2 曲" in text.value for text in at.markdown)
    click(at, "精度評価を開く")
    assert at.title[0].value == "精度評価"
    assert any("未評価 2 曲" in text.value for text in at.markdown)


def test_analysis_statuses_are_distinct_and_lead_to_library(demo: sqlite3.Connection) -> None:
    with transaction(demo):
        for name in ("pending.flac", "unsupported.m4a", "failed.flac"):
            metadata = TrackMetadata(
                rel_path=name, title=None, artist=None, album=None, genre=None, year=None, track_number=None,
                duration_sec=None, bitrate=None, sample_rate=None, channels=None, codec=None,
                file_size=100, mtime_ns=1, title_source=TitleSource.FILENAME, tags_repaired=False, quick_hash=None,
            )  # fmt: skip
            track_id = upsert_track(demo, DEMO_LIBRARY_ROOT, metadata, None)
            if name == "failed.flac":
                record_error(
                    demo,
                    track_id,
                    ErrorRecord("features", "ValueError", "test failure", ANALYSIS_VERSION, 100, 1),
                )

    at = AppTest.from_file(APP, default_timeout=TIMEOUT).run()
    assert not at.exception
    assert at.metric[0].value == "63"
    assert any(text.value == "音響解析済み **60 曲**　／　未解析 **1 曲**" for text in at.markdown)
    assert any(caption.value == "解析失敗 1 曲　／　解析未対応 1 曲" for caption in at.caption)
    assert any("解析に失敗した曲が 1 曲" in message.value for message in at.warning)
    click(at, "解析状況を確認")
    assert at.title[0].value == "音楽ライブラリ"


def test_dashboard_unknown_genres_have_no_selection_shortcut(demo: sqlite3.Connection) -> None:
    with transaction(demo):
        settings_store.set_value(demo, settings_store.CLASSIFICATION_SOURCE_KEY, ClassificationSource.JEV.value)

    at = run_view("dashboard")

    assert any("ジャンルはすべて不明" in caption.value for caption in at.caption)
    assert not any(button.label == "このジャンルで選曲へ" for button in at.button)


@pytest.mark.parametrize("apply", [True, False])
def test_genre_shortcut_preserves_draft_and_other_conditions(demo: sqlite3.Connection, apply: bool) -> None:
    at = AppTest.from_file(APP, default_timeout=TIMEOUT).run()
    click(at, "プレイリストを作成")
    click(at, "プレイリストを生成")
    click(at, "除外")
    before = at.dataframe[0].value["曲名"].tolist()
    at.text_input(key="pl_name").input("編集中のリスト").run()
    at.selectbox(key="pl_mood").set_value(Mood.FOCUS).run()
    at.number_input(key="pl_count").set_value(7).run()
    at.number_input(key="pl_seed").set_value(2024).run()
    at.radio(key="pl_genre_mode").set_value(GenreMode.MIX).run()
    at.multiselect(key="pl_genres").set_value([Genre.POP]).run()
    navigate(at, "")
    at.selectbox(key="dashboard-genre").set_value(Genre.ROCK).run()
    click(at, "このジャンルで選曲へ")

    assert at.title[0].value == "プレイリスト作成"
    assert at.radio(key="pl_genre_mode").value == GenreMode.MIX
    assert at.multiselect(key="pl_genres").value == [Genre.POP]
    assert at.dataframe[0].value["曲名"].tolist() == before
    click(at, "ジャンル条件に反映" if apply else "見送る")
    navigate(at, "")
    click(at, "プレイリストを作成")

    assert at.radio(key="pl_genre_mode").value == (GenreMode.ONLY if apply else GenreMode.MIX)
    assert at.multiselect(key="pl_genres").value == ([Genre.ROCK] if apply else [Genre.POP])
    assert at.text_input(key="pl_name").value == "編集中のリスト"
    assert at.selectbox(key="pl_mood").value == Mood.FOCUS
    assert at.number_input(key="pl_count").value == 7
    assert at.number_input(key="pl_seed").value == 2024
    assert at.dataframe[0].value["曲名"].tolist() == before
    assert not any(button.label == "ジャンル条件に反映" for button in at.button)


def test_genre_shortcut_applies_before_first_generation(demo: sqlite3.Connection) -> None:
    at = AppTest.from_file(APP, default_timeout=TIMEOUT).run()
    at.selectbox(key="dashboard-genre").set_value(Genre.ROCK).run()
    click(at, "このジャンルで選曲へ")
    assert not at.dataframe
    click(at, "ジャンル条件に反映")
    assert not at.dataframe
    at.selectbox(key="pl_mood").set_value(Mood.OMAKASE).run()
    click(at, "プレイリストを生成")
    assert len(at.dataframe[0].value) > 0
    assert set(at.dataframe[0].value["ジャンル"]) == {"ロック"}
