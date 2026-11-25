"""プレイリスト作成画面のうち、雰囲気で選ぶ生成・下書きの編集（再生・除外を含む）・保存のテスト。"""

import sqlite3
from pathlib import Path

import pytest
import soundfile as sf
from conftest import SAMPLE_RATE, sine
from streamlit.testing.v1 import AppTest
from test_gui import click, click_row, draft_rows, draft_titles, run_view, wait_track_loaded

from volumio_playlist_studio.config import PlaylistDefaults
from volumio_playlist_studio.models import (
    GENRE_LABELS,
    ClassificationResult,
    ClassificationSource,
    Genre,
    Mood,
    TitleSource,
    TrackMetadata,
)
from volumio_playlist_studio.playlist import ChildrenMode, CreationMethod, GenreMode
from volumio_playlist_studio.sample_data import DEMO_LIBRARY_ROOT
from volumio_playlist_studio.storage import settings_store
from volumio_playlist_studio.storage.analysis import save_classification
from volumio_playlist_studio.storage.database import transaction
from volumio_playlist_studio.storage.exclusions import ExclusionReason, list_exclusions
from volumio_playlist_studio.storage.playlists import list_playlists
from volumio_playlist_studio.storage.tracks import list_tracks, upsert_track

DEFAULTS = PlaylistDefaults()


def test_playlist_excludes_children_by_default_and_can_select_only_children(demo: sqlite3.Connection) -> None:
    at = run_view("playlist_page")
    children = next(r for r in at.radio if r.label == "子供向けの曲")
    assert children.value == ChildrenMode.EXCLUDE

    # サンプルデータは未判定のため、子供向けだけにすると曲が選ばれず、判定を案内する
    children.set_value(ChildrenMode.ONLY)
    click(at, "プレイリストを生成")

    assert any("子供向けの条件に合わない" in w.value and "Jev で判定" in w.value for w in at.warning)


def test_playlist_excludes_short_tracks_only_when_requested(demo: sqlite3.Connection) -> None:
    at = run_view("playlist_page")
    min_track_sec = next(n for n in at.number_input if n.key == "pl_min_track_sec")
    assert min_track_sec.value == 0

    # サンプルデータの曲はすべて 150 秒以上のため、それより長い秒数を指定すると短い曲として除外される
    min_track_sec.set_value(DEFAULTS.max_min_track_sec)
    click(at, "プレイリストを生成")

    assert any("再生時間が短い" in w.value and "短い曲の秒数を下げる" in w.value for w in at.warning)


def test_playlist_generation_edit_and_save(demo: sqlite3.Connection) -> None:
    at = run_view("playlist_page")
    at.selectbox(key="pl_mood").set_value(Mood.FOCUS)
    click(at, "プレイリストを生成")
    assert any("曲・合計" in m.value for m in at.markdown)

    first_before = draft_titles(at)[0]
    click(at, "下へ")
    first_after = draft_titles(at)[0]
    assert first_after != first_before
    count_before = len(draft_rows(at))
    click(at, "好みでない")
    assert len(draft_rows(at)) == count_before - 1
    next(s for s in at.selectbox if s.label == "追加する曲").select_index(0).run()
    click(at, "追加")
    details = next(d.value for d in at.dataframe if "適合度" in d.value.columns)
    assert "手動追加" in details["適合度"].tolist()

    at.text_input(key="pl_name").input("テスト用プレイリスト").run()
    click(at, "プレイリストを保存")
    [saved] = list_playlists(demo)
    assert saved.name == "テスト用プレイリスト"
    assert any(e.added_manually for e in saved.entries)
    # Volumio への登録・削除はプレイリスト管理画面で行う
    assert any("プレイリスト管理画面から Volumio に登録できます" in s.value for s in at.success)
    assert not any(b.label in ("Volumio に登録", "選んだプレイリストを削除") for b in at.button)


def test_save_button_below_track_list_opens_save_tab(demo: sqlite3.Connection) -> None:
    at = run_view("playlist_page")
    click(at, "プレイリストを生成")
    assert at.session_state["playlist-tabs"] == "2. 曲を編集"

    # 曲一覧の下から保存タブへ進み、そのまま名前を付けて保存できる
    click(at, "保存する")
    assert at.session_state["playlist-tabs"] == "3. 保存"
    at.text_input(key="pl_name").input("下のボタンから保存").run()
    click(at, "プレイリストを保存")
    assert [playlist.name for playlist in list_playlists(demo)] == ["下のボタンから保存"]
    # 保存後も保存タブのまま、結果のメッセージを表示する
    assert at.session_state["playlist-tabs"] == "3. 保存"
    assert any("を保存しました" in s.value for s in at.success)


def test_playlist_shortfall_offers_relaxation(demo: sqlite3.Connection) -> None:
    at = run_view("playlist_page")
    next(r for r in at.radio if r.label == "ジャンル").set_value(GenreMode.ONLY)
    at.multiselect[0].select(Genre.CLASSICAL)
    next(s for s in at.slider if s.label == "再生時間（分）").set_value(180)
    click(at, "プレイリストを生成")

    assert any("条件に合う曲が足りません" in w.value for w in at.warning)
    click(at, "条件を緩和して再生成")
    assert any("条件を緩和" in m.value for m in at.markdown)


def test_playlist_offers_eleven_moods_and_suggests_jev_for_morning(demo: sqlite3.Connection) -> None:
    at = run_view("playlist_page")
    options = at.selectbox(key="pl_mood").options
    assert len(options) == 12  # 11 種類の雰囲気と「おまかせ」
    assert {"夜の街・クール", "緊迫・スリリング"} <= set(options)
    # 穏やかな夜は分類結果のムードとしてだけ使う
    assert "穏やかな夜" not in options

    # デモデータはルールベースの判定だけで明るさが判定保留のため、朝・爽やかの候補がない
    at.selectbox(key="pl_mood").set_value(Mood.MORNING)
    click(at, "プレイリストを生成")

    assert any("条件に合う曲が足りません" in w.value and "Jev で判定する" in w.value for w in at.warning)


def test_playlist_requires_genres_when_filtering(demo: sqlite3.Connection) -> None:
    at = run_view("playlist_page")
    next(r for r in at.radio if r.label == "ジャンル").set_value(GenreMode.ONLY)

    click(at, "プレイリストを生成")

    assert any("ジャンルを 1 つ以上" in e.value for e in at.error)


def test_playlist_conditions_survive_editing_and_save(demo: sqlite3.Connection) -> None:
    at = run_view("playlist_page")
    at.selectbox(key="pl_mood").set_value(Mood.OMAKASE)
    at.radio(key="pl_length").set_value("曲数")
    at.number_input(key="pl_count").set_value(5)
    at.number_input(key="pl_seed").set_value(42)
    at.slider(key="pl_novelty").set_value(0.7)
    at.slider(key="pl_randomness").set_value(0.6)
    at.checkbox(key="pl_smooth").uncheck()
    click(at, "プレイリストを生成")
    assert len(draft_rows(at)) == 5
    original_titles = draft_titles(at)
    click(at, "プレイリストを生成")
    assert draft_titles(at) == original_titles
    at.text_input(key="pl_name").input("編集中の名前").run()
    click(at, "下へ")
    assert at.text_input(key="pl_name").value == "編集中の名前"
    assert at.number_input(key="pl_count").value == 5
    assert at.selectbox(key="pl_mood").value == Mood.OMAKASE
    assert at.slider(key="pl_novelty").value == 0.7
    click(at, "プレイリストを保存")
    [saved] = list_playlists(demo)
    assert saved.seed == 42
    assert saved.conditions["track_count"] == 5
    assert saved.conditions["randomness"] == 0.6
    assert saved.conditions["smooth_order"] is False
    click(at, "好みでない")
    assert any("未保存" in c.value for c in at.caption)
    assert len(list_playlists(demo)[0].entries) == 5
    # 保存後は既存仕様の利用回数が選曲に影響するため、曲順でなく入力条件の保持を確認する。
    click(at, "プレイリストを生成")
    assert len(draft_rows(at)) == 5
    assert at.number_input(key="pl_seed").value == 42


def test_playlist_empty_draft_can_be_restored(demo: sqlite3.Connection) -> None:
    at = run_view("playlist_page")
    at.radio(key="pl_length").set_value("曲数")
    at.number_input(key="pl_count").set_value(1)
    click(at, "プレイリストを生成")
    assert next(b for b in at.button if b.label == "上へ").disabled
    assert next(b for b in at.button if b.label == "下へ").disabled
    click(at, "好みでない")
    assert any("曲がありません" in i.value for i in at.info)
    assert next(b for b in at.button if b.label == "プレイリストを保存").disabled
    assert next(b for b in at.button if b.label == "保存する").disabled
    next(s for s in at.selectbox if s.label == "追加する曲").select_index(0).run()
    click(at, "追加")
    assert len(draft_rows(at)) == 1
    assert not next(b for b in at.button if b.label == "プレイリストを保存").disabled
    assert not next(b for b in at.button if b.label == "保存する").disabled


def test_playlist_empty_name_is_rejected_without_losing_draft(demo: sqlite3.Connection) -> None:
    at = run_view("playlist_page")
    click(at, "プレイリストを生成")
    before = draft_titles(at)
    at.text_input(key="pl_name").input("   ")
    click(at, "プレイリストを保存")
    assert any("プレイリスト名を入力" in message.value for message in at.error)
    assert list_playlists(demo) == []
    assert draft_titles(at) == before
    at.text_input(key="pl_name").input("名前を入力して保存")
    click(at, "プレイリストを保存")
    assert [playlist.name for playlist in list_playlists(demo)] == ["名前を入力して保存"]


def test_changing_conditions_does_not_replace_draft(demo: sqlite3.Connection) -> None:
    at = run_view("playlist_page")
    click(at, "プレイリストを生成")
    before = draft_titles(at)
    at.selectbox(key="pl_mood").set_value(Mood.FOCUS).run()
    at.radio(key="pl_length").set_value("曲数").run()
    at.number_input(key="pl_count").set_value(3).run()
    assert draft_titles(at) == before
    click(at, "下へ")
    assert draft_titles(at)[:2] == [before[1], before[0]]
    assert at.number_input(key="pl_count").value == 3
    assert at.selectbox(key="pl_mood").value == Mood.FOCUS
    # 移動後も同じ曲を選択したまま、続けて移動できる。
    click(at, "下へ")
    assert draft_titles(at)[2] == before[0]
    click(at, "プレイリストを生成")
    assert len(draft_rows(at)) == 3


def test_playlist_by_artist_generates_and_saves_conditions(demo: sqlite3.Connection) -> None:
    at = run_view("playlist_page")
    at.radio(key="pl_method").set_value(CreationMethod.ARTIST).run()
    artists = at.multiselect(key="pl_artist_names")
    # サンプルデータは 10 組のアーティストが 6 曲ずつ
    assert len(artists.options) == 10
    assert "Example Jazz Trio（6 曲）" in artists.options
    # 雰囲気は既定で絞り込まない
    assert at.selectbox(key="pl_artist_mood").value == Mood.OMAKASE

    click(at, "プレイリストを生成")
    assert any("アーティストを 1 つ以上選んでください" in e.value for e in at.error)

    artists.set_value(["Example Jazz Trio", "テスト・ポップス"]).run()
    at.radio(key="pl_length").set_value("曲数").run()
    at.number_input(key="pl_count").set_value(20).run()
    click(at, "プレイリストを生成")

    assert any(h.value == "Example Jazz Trio・テスト・ポップスのプレイリスト" for h in at.subheader)
    rows = draft_rows(at)
    assert {row["artist"] for row in rows} == {"Example Jazz Trio", "テスト・ポップス"}
    # 2 組で 12 曲しかないため、アーティストの追加を提案する
    assert len(rows) == 12
    assert any("アーティストを追加する" in w.value for w in at.warning)

    click(at, "プレイリストを保存")
    [saved] = list_playlists(demo)
    assert saved.name.startswith("Example Jazz Trio・テスト・ポップス ")
    assert saved.conditions["method"] == "artist"
    assert saved.conditions["artists"] == ["Example Jazz Trio", "テスト・ポップス"]
    assert saved.conditions["mood"] == "omakase"
    assert saved.conditions["track_count"] == 20


def test_playlist_by_artist_with_mood_shows_mood_in_title(demo: sqlite3.Connection) -> None:
    at = run_view("playlist_page")
    at.radio(key="pl_method").set_value(CreationMethod.ARTIST).run()
    at.multiselect(key="pl_artist_names").set_value(["サンプル弦楽団", "Demo Quiet Piano", "Example Jazz Trio"]).run()
    at.selectbox(key="pl_artist_mood").set_value(Mood.RELAX).run()
    click(at, "プレイリストを生成")

    assert any(
        h.value == "サンプル弦楽団・Demo Quiet Piano ほか 1 組（リラックス）のプレイリスト" for h in at.subheader
    )
    chosen = {row["artist"] for row in draft_rows(at)}
    assert chosen
    assert chosen <= {"サンプル弦楽団", "Demo Quiet Piano", "Example Jazz Trio"}


def test_playlist_by_artist_drops_artists_missing_from_library(demo: sqlite3.Connection) -> None:
    at = run_view("playlist_page")
    at.radio(key="pl_method").set_value(CreationMethod.ARTIST).run()
    # ライブラリの変更で消えたアーティストが選択に残っていても、画面はエラーにならない
    at.session_state["pl_artist_names"] = ["存在しないアーティスト", "Example Jazz Trio"]
    at.run()

    assert not at.exception
    assert at.multiselect(key="pl_artist_names").value == ["Example Jazz Trio"]


def test_excluding_tracks_records_reason_without_changing_next_selection(demo: sqlite3.Connection) -> None:
    at = run_view("playlist_page")
    at.radio(key="pl_length").set_value("曲数").run()
    at.number_input(key="pl_count").set_value(5).run()
    click(at, "プレイリストを生成")
    original = draft_rows(at)
    titles = [row["title"] for row in original]
    ids = {t.display_title: t.id for t in list_tracks(demo, DEMO_LIBRARY_ROOT)}

    # 曲一覧の各行のボタン 1 回で、理由を付けて除外できる
    click_row(at, "ジャンル違い", 1)
    assert draft_titles(at) == titles[:1] + titles[2:]
    click_row(at, "好みでない", 0)
    assert draft_titles(at) == titles[2:]
    assert any("未保存" in c.value for c in at.caption)

    first, second = list_exclusions(demo)
    assert (first.track_id, first.reason) == (ids[titles[1]], ExclusionReason.GENRE_MISMATCH)
    assert (second.track_id, second.reason) == (ids[titles[0]], ExclusionReason.PREFERENCE)
    # 分類の誤りとして評価できるよう、画面に表示していたジャンルと、選曲に使った分類結果を記録する
    assert ("不明" if first.genre is None else GENRE_LABELS[first.genre]) == original[1]["genre"]
    assert first.classification_source == "jev_then_rules"
    assert first.classification is not None
    assert first.classification.engine == "rules"
    assert first.classification.genre == first.genre
    assert (first.conditions["method"], first.conditions["track_count"]) == ("mood", 5)
    # 除外の記録は保存したプレイリストとは別で、プレイリストは保存されない
    assert list_playlists(demo) == []

    # 除外の記録は次回以降の選曲に影響しない（同じ条件・seed なら同じ曲が選ばれる）
    click(at, "プレイリストを生成")
    assert draft_titles(at) == titles


def test_exclusion_and_save_keep_source_used_for_generation(demo: sqlite3.Connection) -> None:
    at = run_view("playlist_page")
    click(at, "プレイリストを生成")

    # 生成した後に、設定画面で選曲に使う分類結果を変えても、記録には生成時の種類を残す
    with transaction(demo):
        settings_store.set_value(demo, settings_store.CLASSIFICATION_SOURCE_KEY, ClassificationSource.RULES.value)

    click_row(at, "ジャンル違い", 0)
    click(at, "プレイリストを保存")

    [exclusion] = list_exclusions(demo)
    assert exclusion.classification_source == ClassificationSource.JEV_THEN_RULES.value
    [saved] = list_playlists(demo)
    assert saved.classification_source == ClassificationSource.JEV_THEN_RULES.value

    # 生成し直すと、その時点の設定の種類を記録する
    click(at, "プレイリストを生成")
    click_row(at, "ジャンル違い", 0)
    assert list_exclusions(demo)[-1].classification_source == ClassificationSource.RULES.value


def test_added_track_uses_classification_source_of_generation(demo: sqlite3.Connection) -> None:
    with transaction(demo):
        settings_store.set_value(demo, settings_store.CLASSIFICATION_SOURCE_KEY, ClassificationSource.RULES.value)

    at = run_view("playlist_page")
    click(at, "プレイリストを生成")

    # 生成した後に設定を Jev 優先に変え、すべての曲に Jev の判定がある状態で曲を追加する
    with transaction(demo):
        settings_store.set_value(
            demo, settings_store.CLASSIFICATION_SOURCE_KEY, ClassificationSource.JEV_THEN_RULES.value
        )
        for track in list_tracks(demo, DEMO_LIBRARY_ROOT):
            jev = ClassificationResult(
                engine="jev", engine_version="jev-test", model=None, genre=Genre.ROCK, genre_confidence=None,
                genre_probabilities=None, mood=None, attributes={},
            )  # fmt: skip
            save_classification(demo, track.id, jev)

    next(s for s in at.selectbox if s.label == "追加する曲").select_index(0).run()
    click(at, "追加")
    click_row(at, "ジャンル違い", -1)

    # 追加した曲も生成時と同じ分類結果（ルールベース）で選ばれ、その種類と写しが一致して記録される
    [exclusion] = list_exclusions(demo)
    assert exclusion.classification_source == ClassificationSource.RULES.value
    assert exclusion.classification is not None
    assert exclusion.classification.engine == "rules"


def _meta(rel_path: str, title: str) -> TrackMetadata:
    return TrackMetadata(
        rel_path=rel_path, title=title, artist="歌手", album=None, genre=None, year=None, track_number=None,
        duration_sec=1.0, bitrate=None, sample_rate=None, channels=None, codec=None, file_size=100, mtime_ns=1,
        title_source=TitleSource.TAG, tags_repaired=False, quick_hash=None,
    )  # fmt: skip


@pytest.fixture
def playable(app_db: sqlite3.Connection, tmp_path: Path) -> None:
    """再生できる曲と、DB の相対パスが音楽フォルダの外を指す曲がある、架空のライブラリ。"""
    root = tmp_path / "music"
    (root / "album").mkdir(parents=True)
    sf.write(root / "album" / "a.wav", sine(1.0), SAMPLE_RATE)
    # 音楽フォルダの外にあるファイル。DB の値が壊れていても、ブラウザに送ってはいけない
    sf.write(tmp_path / "outside.wav", sine(1.0), SAMPLE_RATE)
    with transaction(app_db):
        for title, rel_path in [("再生できる曲", "album/a.wav"), ("外を指す曲", "../outside.wav")]:
            upsert_track(app_db, str(root), _meta(rel_path, title), None)

        settings_store.set_value(app_db, settings_store.MUSIC_ROOT_KEY, str(root))


def _artist_playlist() -> AppTest:
    """未解析の曲でも選べるよう、アーティストで選んでプレイリストを作る。"""
    at = run_view("playlist_page")
    at.radio(key="pl_method").set_value(CreationMethod.ARTIST).run()
    at.multiselect(key="pl_artist_names").set_value(["歌手"]).run()
    click(at, "プレイリストを生成")
    return at


@pytest.mark.usefixtures("playable")
def test_play_button_plays_track_in_browser() -> None:
    at = _artist_playlist()
    titles = draft_titles(at)
    assert sorted(titles) == ["再生できる曲", "外を指す曲"]
    assert not at.get("audio")

    click_row(at, "再生", titles.index("再生できる曲"))
    wait_track_loaded(at)
    [player] = at.get("audio")
    assert player.proto.url
    # 再生中の曲のボタンは「閉じる」になり、押すとプレーヤーを閉じる
    click(at, "閉じる")
    assert not at.get("audio")
    assert [b.label for b in at.button].count("再生") == 2

    # 生成し直すと、前の曲一覧で開いたプレーヤーは閉じる
    click_row(at, "再生", titles.index("再生できる曲"))
    wait_track_loaded(at)
    assert len(at.get("audio")) == 1
    click(at, "プレイリストを生成")
    assert not at.get("audio")
    assert draft_titles(at) == titles

    # 再生中の曲を除外すると、プレーヤーも閉じる
    click_row(at, "再生", titles.index("再生できる曲"))
    wait_track_loaded(at)
    assert len(at.get("audio")) == 1
    click_row(at, "好みでない", titles.index("再生できる曲"))
    assert draft_titles(at) == ["外を指す曲"]
    assert not at.get("audio")


@pytest.mark.usefixtures("playable")
def test_track_outside_library_is_not_sent_to_browser() -> None:
    at = _artist_playlist()

    click_row(at, "再生", draft_titles(at).index("外を指す曲"))
    wait_track_loaded(at)

    assert not at.get("audio")
    assert any("音楽フォルダの外" in w.value for w in at.warning)


def test_track_from_previous_music_folder_is_not_played(
    app_db: sqlite3.Connection, playable: None, tmp_path: Path
) -> None:
    at = _artist_playlist()
    other = tmp_path / "other-music"
    other.mkdir()
    # 生成した後に音楽フォルダを変えると、編集中の曲は前の音楽フォルダの曲として残る
    with transaction(app_db):
        settings_store.set_value(app_db, settings_store.MUSIC_ROOT_KEY, str(other))

    at.run()
    click_row(at, "再生", draft_titles(at).index("再生できる曲"))
    wait_track_loaded(at)

    assert not at.get("audio")
    assert any("現在の音楽フォルダの曲ではない" in c.value for c in at.caption)
