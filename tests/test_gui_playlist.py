"""プレイリスト作成画面のうち、雰囲気で選ぶ生成・下書きの編集・保存・削除のテスト。"""

import sqlite3

from streamlit.testing.v1 import AppTest
from test_gui import TIMEOUT, click, run_view

from volumio_playlist_studio.models import (
    Genre,
    Mood,
)
from volumio_playlist_studio.playlist import ChildrenMode, GenreMode
from volumio_playlist_studio.storage.database import transaction
from volumio_playlist_studio.storage.playlists import list_playlists, list_registrations, set_feedback


def test_playlist_excludes_children_by_default_and_can_select_only_children(demo: sqlite3.Connection) -> None:
    at = run_view("playlist_page")
    children = next(r for r in at.radio if r.label == "子供向けの曲")
    assert children.value == ChildrenMode.EXCLUDE

    # サンプルデータは未判定のため、子供向けだけにすると曲が選ばれず、判定を案内する
    children.set_value(ChildrenMode.ONLY)
    click(at, "プレイリストを生成")

    assert any("子供向けの条件に合わない" in w.value and "Jev で判定" in w.value for w in at.warning)


def test_playlist_generation_edit_save_and_register(demo: sqlite3.Connection) -> None:
    at = run_view("playlist_page")
    at.selectbox(key="pl_mood").set_value(Mood.FOCUS)
    click(at, "プレイリストを生成")
    assert any("曲・合計" in m.value for m in at.markdown)

    first_before = at.dataframe[0].value.iloc[0]["曲名"]
    click(at, "下へ")
    first_after = at.dataframe[0].value.iloc[0]["曲名"]
    assert first_after != first_before
    count_before = len(at.dataframe[0].value)
    click(at, "除外")
    assert len(at.dataframe[0].value) == count_before - 1
    next(s for s in at.selectbox if s.label == "追加する曲").select_index(0).run()
    click(at, "追加")
    assert "手動追加" in at.dataframe[1].value["適合度"].tolist()

    at.text_input(key="pl_name").input("テスト用プレイリスト").run()
    click(at, "プレイリストを保存")
    [saved] = list_playlists(demo)
    assert saved.name == "テスト用プレイリスト"
    assert any(e.added_manually for e in saved.entries)

    next(c for c in at.checkbox if c.label.startswith("この内容で")).check().run()
    click(at, "Volumio に登録")
    assert any("曲を登録しました" in s.value for s in at.success)
    assert list_registrations(demo, saved.id)[0].status == "success"

    # 同じ名前ではモックの Volumio に登録できない（既存のプレイリストを上書きしない）
    at.run()
    assert any("同じ名前のプレイリストが既にあります" in w.value for w in at.warning)
    click(at, "Volumio に登録")
    assert any("登録しませんでした" in e.value for e in at.error)


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


def test_saved_playlist_can_be_deleted_keeping_feedback(demo: sqlite3.Connection) -> None:
    at = run_view("playlist_page")
    click(at, "プレイリストを生成")
    at.text_input(key="pl_name").input("消すプレイリスト").run()
    click(at, "プレイリストを保存")
    [saved] = list_playlists(demo)
    with transaction(demo):
        set_feedback(demo, saved.id, 2, {0})

    # 生成前の画面（再表示）からも削除できる。確認のチェックを入れるまでは押せない
    at = run_view("playlist_page")
    assert next(b for b in at.button if b.label == "プレイリストを削除").disabled
    next(c for c in at.checkbox if c.label == "このプレイリストを削除します").check().run()
    click(at, "プレイリストを削除")

    assert any("「消すプレイリスト」を削除しました" in s.value for s in at.success)
    assert not any(b.label == "プレイリストを削除" for b in at.button)
    assert list_playlists(demo) == []
    [deleted] = list_playlists(demo, include_deleted=True)
    assert (deleted.satisfaction, deleted.entries[0].unsuitable) == (2, True)

    at = run_view("evaluation_page")
    assert any("保存したプレイリストがありません" in c.value for c in at.caption)
    table = next(d.value for d in at.dataframe if "平均満足度" in d.value.columns)
    assert table.iloc[0]["プレイリスト数"] == 1
    assert table.iloc[0]["平均満足度"] == "2.0"


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
    assert len(at.dataframe[0].value) == 5
    original_titles = at.dataframe[0].value["曲名"].tolist()
    click(at, "プレイリストを生成")
    assert at.dataframe[0].value["曲名"].tolist() == original_titles
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
    click(at, "除外")
    assert any("未保存" in c.value for c in at.caption)
    assert len(list_playlists(demo)[0].entries) == 5
    # 保存後は既存仕様の利用回数が選曲に影響するため、曲順でなく入力条件の保持を確認する。
    click(at, "プレイリストを生成")
    assert len(at.dataframe[0].value) == 5
    assert at.number_input(key="pl_seed").value == 42


def test_playlist_empty_draft_can_be_restored(demo: sqlite3.Connection) -> None:
    at = run_view("playlist_page")
    at.radio(key="pl_length").set_value("曲数")
    at.number_input(key="pl_count").set_value(1)
    click(at, "プレイリストを生成")
    assert next(b for b in at.button if b.label == "上へ").disabled
    assert next(b for b in at.button if b.label == "下へ").disabled
    click(at, "除外")
    assert any("曲がありません" in i.value for i in at.info)
    assert next(b for b in at.button if b.label == "プレイリストを保存").disabled
    next(s for s in at.selectbox if s.label == "追加する曲").select_index(0).run()
    click(at, "追加")
    assert len(at.dataframe[0].value) == 1
    assert not next(b for b in at.button if b.label == "プレイリストを保存").disabled


def test_playlist_empty_name_is_rejected_without_losing_draft(demo: sqlite3.Connection) -> None:
    at = run_view("playlist_page")
    click(at, "プレイリストを生成")
    before = at.dataframe[0].value["曲名"].tolist()
    at.text_input(key="pl_name").input("   ")
    click(at, "プレイリストを保存")
    assert any("プレイリスト名を入力" in message.value for message in at.error)
    assert list_playlists(demo) == []
    assert at.dataframe[0].value["曲名"].tolist() == before
    at.text_input(key="pl_name").input("名前を入力して保存")
    click(at, "プレイリストを保存")
    assert [playlist.name for playlist in list_playlists(demo)] == ["名前を入力して保存"]


def test_changing_conditions_does_not_replace_draft(demo: sqlite3.Connection) -> None:
    at = run_view("playlist_page")
    click(at, "プレイリストを生成")
    before = at.dataframe[0].value["曲名"].tolist()
    at.selectbox(key="pl_mood").set_value(Mood.FOCUS).run()
    at.radio(key="pl_length").set_value("曲数").run()
    at.number_input(key="pl_count").set_value(3).run()
    assert at.dataframe[0].value["曲名"].tolist() == before
    click(at, "下へ")
    assert at.dataframe[0].value["曲名"].tolist()[:2] == [before[1], before[0]]
    assert at.number_input(key="pl_count").value == 3
    assert at.selectbox(key="pl_mood").value == Mood.FOCUS
    # 移動後も同じ曲を選択したまま、続けて移動できる。
    click(at, "下へ")
    assert at.dataframe[0].value.iloc[2]["曲名"] == before[0]
    click(at, "プレイリストを生成")
    assert len(at.dataframe[0].value) == 3


def playlist_navigation_script() -> None:
    """AppTest が直接操作できないサイドバーの代わりにボタンで標準ページ遷移を行う。"""
    import streamlit as st

    from volumio_playlist_studio.gui.context import open_context
    from volumio_playlist_studio.gui.views import playlist_page

    ctx = open_context()
    try:

        def edit() -> None:
            playlist_page.render(ctx)
            if st.button("別画面へ"):
                st.switch_page(other_page)

        def other() -> None:
            if st.button("編集へ戻る"):
                st.switch_page(edit_page)

        edit_page = st.Page(edit, title="編集", url_path="edit", default=True)
        other_page = st.Page(other, title="別画面", url_path="other")
        playlist_page.preserve_inputs()
        st.navigation([edit_page, other_page]).run()
    finally:
        ctx.conn.close()


def test_playlist_round_trip_keeps_unsaved_inputs_and_draft(demo: sqlite3.Connection) -> None:
    at = AppTest.from_function(playlist_navigation_script, default_timeout=TIMEOUT).run()
    click(at, "プレイリストを生成")
    click(at, "除外")
    before = at.dataframe[0].value["曲名"].tolist()
    at.text_input(key="pl_name").input("戻って編集する名前").run()
    at.selectbox(key="pl_mood").set_value(Mood.FOCUS).run()
    at.number_input(key="pl_count").set_value(7).run()
    at.number_input(key="pl_seed").set_value(2024).run()
    click(at, "別画面へ")
    assert not at.dataframe
    click(at, "編集へ戻る")
    assert at.text_input(key="pl_name").value == "戻って編集する名前"
    assert at.selectbox(key="pl_mood").value == Mood.FOCUS
    assert at.number_input(key="pl_count").value == 7
    assert at.number_input(key="pl_seed").value == 2024
    assert at.dataframe[0].value["曲名"].tolist() == before
