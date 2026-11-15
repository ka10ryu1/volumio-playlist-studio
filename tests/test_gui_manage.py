"""プレイリスト管理画面（保存済みのプレイリスト・自由記述の雰囲気の一覧と削除、Volumio への登録）のテスト。

Volumio はモッククライアントを使い、自由記述の雰囲気を作るボタンは押さない（実際の外部サービスとは通信しない）。
"""

import sqlite3

import pytest
from streamlit.testing.v1 import AppTest
from test_gui import click, run_view, select_rows
from test_gui_mood_map import save_custom

from volumio_playlist_studio.models import Mood
from volumio_playlist_studio.playlist import CreationMethod
from volumio_playlist_studio.sample_data import DEMO_LIBRARY_ROOT
from volumio_playlist_studio.storage.custom_moods import delete_custom_mood, list_custom_moods
from volumio_playlist_studio.storage.database import transaction
from volumio_playlist_studio.storage.playlists import (
    PlaylistDraft,
    PlaylistEntry,
    delete_playlist,
    list_playlists,
    list_registrations,
    set_feedback,
)
from volumio_playlist_studio.storage.playlists import save_playlist as store_playlist
from volumio_playlist_studio.storage.tracks import list_tracks

_PLAYLISTS = "manage_playlists"
_MOODS = "cm_table"


def save_demo_playlist(conn: sqlite3.Connection, name: str, track_count: int = 3) -> int:
    """サンプルデータの先頭の曲で、プレイリストを保存する。"""
    tracks = list_tracks(conn, DEMO_LIBRARY_ROOT)[:track_count]
    with transaction(conn):
        return store_playlist(
            conn,
            PlaylistDraft(
                name=name, conditions={"mood": "relax"}, seed=1, classification_source="rules",
                total_duration_sec=180.0 * len(tracks), entries=[PlaylistEntry(t.id, 0.5) for t in tracks],
            ),
        )  # fmt: skip


def button_labels(at: AppTest) -> list[str]:
    return [b.label for b in at.button]


def test_manage_page_without_playlists_explains(demo: sqlite3.Connection) -> None:
    at = run_view("manage_page")

    assert [t.label for t in at.tabs] == ["プレイリスト", "雰囲気"]
    assert any("保存したプレイリストがありません" in c.value for c in at.caption)
    assert any("作成した雰囲気はありません" in c.value for c in at.caption)


def test_manage_page_lists_playlists_newest_first(demo: sqlite3.Connection) -> None:
    first = save_demo_playlist(demo, "朝の曲", track_count=2)
    save_demo_playlist(demo, "夜の曲", track_count=4)
    with transaction(demo):
        set_feedback(demo, first, 4, set())

    at = run_view("manage_page")

    table = at.dataframe[0].value
    assert table["名前"].tolist() == ["夜の曲", "朝の曲"]
    assert table["曲数"].tolist() == [4, 2]
    assert table["合計時間"].tolist() == ["12:00", "6:00"]
    assert table["満足度"].astype(str).tolist() == ["-", "4"]
    assert table["Volumio"].tolist() == ["未登録", "未登録"]
    # 選ぶまでは削除・登録の操作を出さない
    assert not {"選んだプレイリストを削除", "Volumio に登録"} & set(button_labels(at))


def test_manage_page_deletes_selected_playlists_at_once_keeping_feedback(demo: sqlite3.Connection) -> None:
    kept = save_demo_playlist(demo, "残す")
    rated = save_demo_playlist(demo, "消す1")
    save_demo_playlist(demo, "消す2")
    with transaction(demo):
        set_feedback(demo, rated, 2, {0})

    # 一覧は新しい順（消す2、消す1、残す）
    at = select_rows(run_view("manage_page"), _PLAYLISTS, [0, 1])
    # 複数選んだ場合、曲目と登録欄は出さない
    assert any("1 件だけ選ぶと表示します" in c.value for c in at.caption)
    assert "Volumio に登録" not in button_labels(at)
    # 確認のチェックを入れるまでは押せない
    assert next(b for b in at.button if b.label == "選んだプレイリストを削除").disabled
    next(c for c in at.checkbox if c.label == "選んだ 2 件のプレイリストを削除します").check().run()
    click(at, "選んだプレイリストを削除")

    assert any(s.value == "「消す2」「消す1」を削除しました。" for s in at.success)
    assert [p.id for p in list_playlists(demo)] == [kept]
    assert at.dataframe[0].value["名前"].tolist() == ["残す"]
    # 削除後は選択を解除し、残ったプレイリストを選んだ状態にしない
    assert "選んだプレイリストを削除" not in button_labels(at)
    deleted = next(p for p in list_playlists(demo, include_deleted=True) if p.id == rated)
    assert (deleted.satisfaction, deleted.entries[0].unsuitable) == (2, True)

    at = run_view("evaluation_page")
    table = next(d.value for d in at.dataframe if "平均満足度" in d.value.columns)
    assert table.iloc[0]["プレイリスト数"] == 1
    assert table.iloc[0]["平均満足度"] == "2.0"


def test_manage_page_clears_selection_when_list_changes_elsewhere(demo: sqlite3.Connection) -> None:
    first = save_demo_playlist(demo, "先に消える")
    second = save_demo_playlist(demo, "残るはず")
    third = save_demo_playlist(demo, "選んでいない")
    # 一覧は新しい順（選んでいない、残るはず、先に消える）。下の 2 件を選ぶ
    at = select_rows(run_view("manage_page"), _PLAYLISTS, [1, 2])
    next(c for c in at.checkbox if c.label == "選んだ 2 件のプレイリストを削除します").check().run()
    # 別のタブなどで 1 件が先に削除され、行の並びが変わった
    with transaction(demo):
        delete_playlist(demo, first)

    at.run()

    # 行番号がずれて、選んでいないプレイリストが選ばれた状態にならない
    assert "選んだプレイリストを削除" not in button_labels(at)
    assert [p.id for p in list_playlists(demo)] == [third, second]


def test_manage_page_shows_tracks_and_registers_to_mock_volumio(demo: sqlite3.Connection) -> None:
    playlist_id = save_demo_playlist(demo, "テスト用プレイリスト", track_count=3)
    at = select_rows(run_view("manage_page"), _PLAYLISTS, [0])

    assert any(h.value == "テスト用プレイリスト" for h in at.subheader)
    tracks = next(d.value for d in at.dataframe if "曲" in d.value.columns and "No." in d.value.columns)
    assert tracks["No."].tolist() == [1, 2, 3]
    # Volumio が未設定のため、照合先はモックだけ
    assert next(r for r in at.radio if r.label == "照合先").options == ["モック（模擬）"]

    next(c for c in at.checkbox if c.label.startswith("この内容で")).check().run()
    click(at, "Volumio に登録")
    assert any("3 曲を登録しました" in s.value for s in at.success)
    assert list_registrations(demo, playlist_id)[0].status == "success"

    # 登録の操作で画面を再実行しても、選択と登録欄を保つ。一覧には登録状況を出す
    at.run()
    assert at.dataframe[0].value["Volumio"].tolist() == ["登録済み（テスト用プレイリスト）"]
    assert any("同じ名前のプレイリストが既にあります" in w.value for w in at.warning)
    click(at, "Volumio に登録")
    assert any("登録しませんでした" in e.value for e in at.error)


def test_moods_tab_without_api_key_only_explains(demo: sqlite3.Connection) -> None:
    at = run_view("manage_page")

    assert any("TYPESAFE_API_KEY が未設定" in i.value for i in at.info)
    assert "Jev で位置を決めて作成" not in button_labels(at)


def test_moods_tab_form_requires_name_and_description(
    demo: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ボタンは押さない（実際の API とは通信しない）
    monkeypatch.setenv("TYPESAFE_API_KEY", "dummy-key-for-display")
    at = run_view("manage_page")

    def create_button_disabled() -> bool:
        return next(b for b in at.button if b.label == "Jev で位置を決めて作成").disabled

    assert create_button_disabled()
    at.text_input(key="cm_name").set_value("雨の午後").run()
    assert create_button_disabled()
    at.text_area(key="cm_description").set_value("雨の日曜の午後、本を読みながら").run()
    assert not create_button_disabled()
    assert any("1 件リクエストを送ります" in c.value and "名前や音楽ライブラリの情報は送りません" in c.value
               for c in at.caption)  # fmt: skip


def test_moods_tab_shows_details_of_selected_mood(demo: sqlite3.Connection) -> None:
    save_custom(demo, "穏やか", 0.3, 0.3)
    save_custom(demo, "年代指定", 0.8, 0.6, expressible=0.2)
    at = run_view("manage_page")
    table = next(d.value for d in at.dataframe if "説明文" in d.value.columns)
    assert table["名前"].tolist() == ["穏やか", "年代指定"]
    assert table["明るさ"].tolist() == [3.0, 8.0]

    select_rows(at, _MOODS, [1])

    assert any(t.value == "説明文: 年代指定の説明" for t in at.text)
    # 属性で表せない要素を含む可能性が高い雰囲気には注意を出す
    assert any("属性では表せない要素" in w.value for w in at.warning)
    details = next(d.value for d in at.dataframe if "属性" in d.value.columns)
    assert dict(zip(details["属性"], details["値"], strict=True))["作業BGM適性"] == "判定保留"


def test_moods_tab_deletes_selected_moods_at_once(demo: sqlite3.Connection) -> None:
    save_custom(demo, "残す", 0.3, 0.3)
    removed_id = save_custom(demo, "消す1", 0.8, 0.6)
    save_custom(demo, "消す2", 0.1, 0.9)
    at = run_view("playlist_page")
    at.radio(key="pl_method").set_value(CreationMethod.CIRCLE).run()
    at.selectbox(key="pl_center").set_value(f"custom:{removed_id}").run()

    at_manage = select_rows(run_view("manage_page"), _MOODS, [1, 2])
    assert not any(t.value.startswith("説明文:") for t in at_manage.text)
    assert next(b for b in at_manage.button if b.label == "選んだ雰囲気を削除").disabled
    next(c for c in at_manage.checkbox if c.label == "選んだ 2 件の雰囲気を削除します").check().run()
    click(at_manage, "選んだ雰囲気を削除")

    assert [m.name for m in list_custom_moods(demo)] == ["残す"]
    assert any(s.value == "「消す1」「消す2」を削除しました。" for s in at_manage.success)
    assert "選んだ雰囲気を削除" not in button_labels(at_manage)

    # 削除した雰囲気は円の中心の選択肢から消え、既定の中心に戻る
    at.run()
    assert "消す1" not in at.selectbox(key="pl_center").options
    assert at.selectbox(key="pl_center").value == Mood.RELAX


def test_moods_tab_clears_selection_when_list_changes_elsewhere(demo: sqlite3.Connection) -> None:
    first = save_custom(demo, "先に消える", 0.3, 0.3)
    save_custom(demo, "選んだ", 0.8, 0.6)
    save_custom(demo, "選んでいない", 0.1, 0.9)
    at = select_rows(run_view("manage_page"), _MOODS, [0, 1])
    with transaction(demo):
        delete_custom_mood(demo, first)

    at.run()

    assert "選んだ雰囲気を削除" not in button_labels(at)
    assert not any(t.value.startswith("説明文:") for t in at.text)
