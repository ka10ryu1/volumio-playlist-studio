"""プレイリスト作成画面のうち、雰囲気マップ・円で選ぶ生成・自由記述の雰囲気のテスト。

自由記述の雰囲気を作るボタンは押さない（実際の API とは通信しない）。"""

import json
import sqlite3

import pytest
from streamlit.dataframe_util import convert_arrow_bytes_to_pandas_df
from streamlit.testing.v1 import AppTest
from test_gui import click, run_view

from volumio_playlist_studio.models import (
    Attribute,
    AttributeScore,
    ClassificationResult,
    Genre,
    Mood,
)
from volumio_playlist_studio.playlist import CreationMethod, GenreMode
from volumio_playlist_studio.sample_data import DEMO_LIBRARY_ROOT
from volumio_playlist_studio.storage.analysis import save_classification
from volumio_playlist_studio.storage.custom_moods import CustomMoodDraft, list_custom_moods, save_custom_mood
from volumio_playlist_studio.storage.database import transaction
from volumio_playlist_studio.storage.playlists import list_playlists
from volumio_playlist_studio.storage.tracks import list_tracks


def mood_map_values(at: AppTest) -> list[dict[str, object]]:
    """雰囲気マップの各レイヤーに渡したデータを、まとめて返す。"""
    [chart] = [e for e in at.main if e.type == "vega_lite_chart"]
    spec = json.loads(chart.proto.spec)
    # Streamlit は各レイヤーのデータを Arrow 形式に変換し、名前で参照させる
    datasets = {d.name: convert_arrow_bytes_to_pandas_df(d.data.data) for d in chart.proto.datasets}
    rows: list[dict[str, object]] = []
    for layer in spec["layer"]:
        rows.extend(datasets[layer["data"]["name"]].to_dict("records"))

    return rows


# テーマ（.streamlit/config.toml）の主色。選択中の雰囲気はこの色で描く
PRIMARY_COLOR = "#c7354f"


def mood_markers(at: AppTest) -> dict[str, tuple[str, str]]:
    """雰囲気マップに描いた雰囲気の印を、名前ごとに（形, 色）で返す。"""
    [chart] = [e for e in at.main if e.type == "vega_lite_chart"]
    datasets = {d.name: convert_arrow_bytes_to_pandas_df(d.data.data) for d in chart.proto.datasets}
    markers: dict[str, tuple[str, str]] = {}
    for layer in json.loads(chart.proto.spec)["layer"]:
        mark = layer["mark"]
        if mark["type"] != "point":
            continue

        for row in datasets[layer["data"]["name"]].to_dict("records"):
            markers[row["雰囲気"]] = (mark["shape"], mark["color"])

    return markers


def save_jev_positions(conn: sqlite3.Connection, positions: list[tuple[float, float]]) -> None:
    """デモの曲の先頭から順に、Jev で判定した明るさ・エネルギー感を保存する。"""
    tracks = list_tracks(conn, DEMO_LIBRARY_ROOT)
    with transaction(conn):
        for track, (brightness, energy) in zip(tracks, positions, strict=False):
            jev = ClassificationResult(
                engine="jev", engine_version="jev-test", model=None, genre=Genre.ROCK, genre_confidence=None,
                genre_probabilities=None, mood=None,
                attributes={Attribute.BRIGHTNESS: AttributeScore(brightness), Attribute.ENERGY: AttributeScore(energy)},
            )  # fmt: skip
            save_classification(conn, track.id, jev)


def save_custom(conn: sqlite3.Connection, name: str, x: float, y: float, expressible: float = 0.9) -> int:
    """自由記述の雰囲気（架空）を保存する。"""
    with transaction(conn):
        return save_custom_mood(
            conn,
            CustomMoodDraft(
                name=name, description=f"{name}の説明", x=x, y=y,
                attributes={Attribute.BRIGHTNESS: AttributeScore(x, 0.8),
                            Attribute.WORK_BGM: AttributeScore(None, 0.3)},
                expressible=expressible, question_version="jev-mood-test", model="jev-test",
            ),
        )  # fmt: skip


def test_mood_map_without_brightness_explains_jev(demo: sqlite3.Connection) -> None:
    # デモデータはルールベースの判定だけで、明るさが判定保留のため、曲はマップに載らない
    at = run_view("playlist_page")

    total = len(list_tracks(demo, DEMO_LIBRARY_ROOT))
    assert any(f"マップに載っている曲: 0 曲。載っていない曲: 明るさかエネルギー感が判定保留 {total} 曲" in c.value
               for c in at.caption)  # fmt: skip
    assert any("Jev で判定する" in i.value for i in at.info)
    # 曲がなくても雰囲気の位置は描く
    moods = {row["雰囲気"]: (row["x"], row["y"]) for row in mood_map_values(at) if "雰囲気" in row}
    # 位置は 0〜10 で表示する
    assert moods["しっとり・切ない"] == (0.0, 3.0)
    assert moods["緊迫・スリリング"] == (1.0, 9.5)
    assert "穏やかな夜" not in moods
    assert not any("曲名" in row for row in mood_map_values(at))


def test_mood_map_labels_have_background_halo(demo: sqlite3.Connection) -> None:
    # 名前は、円の線や曲の点と重なっても読めるよう、背景色の縁取りの上に書く
    at = run_view("playlist_page")
    [chart] = [e for e in at.main if e.type == "vega_lite_chart"]
    text_marks = [layer["mark"] for layer in json.loads(chart.proto.spec)["layer"] if layer["mark"]["type"] == "text"]

    halos = [m for m in text_marks if "stroke" in m]
    assert halos
    assert {m["stroke"] for m in halos} == {"#ffffff"}
    # 縁取りと名前は対になっている
    assert len(text_marks) == 2 * len(halos)


def test_mood_map_highlights_selected_mood_and_colors_custom_moods(demo: sqlite3.Connection) -> None:
    save_custom(demo, "雨の午後", 0.3, 0.3)
    at = run_view("playlist_page")
    at.selectbox(key="pl_mood").set_value(Mood.NIGHT_CITY).run()

    markers = mood_markers(at)
    assert markers["夜の街・クール"] == ("diamond", PRIMARY_COLOR)
    # 選択していない既存の雰囲気は同じ色、自由記述の雰囲気はそれとも選択中とも違う色で描く
    preset_colors = {
        color for name, (shape, color) in markers.items() if shape == "diamond" and name != "夜の街・クール"
    }
    assert len(preset_colors) == 1
    custom_shape, custom_color = markers["雨の午後"]
    assert custom_shape == "triangle-up"
    assert custom_color not in preset_colors | {PRIMARY_COLOR}
    assert any("選択中の雰囲気は色を変えて" in c.value for c in at.caption)

    # おまかせはマップにないため、どれも強調しない
    at.selectbox(key="pl_mood").set_value(Mood.OMAKASE).run()
    assert PRIMARY_COLOR not in {color for _, color in mood_markers(at).values()}
    assert not any("選択中の雰囲気は色を変えて" in c.value for c in at.caption)


def test_mood_map_highlights_custom_mood_selected_as_circle_center(demo: sqlite3.Connection) -> None:
    mood_id = save_custom(demo, "嵐の夜", 0.1, 0.9)
    at = run_view("playlist_page")
    at.radio(key="pl_method").set_value(CreationMethod.CIRCLE).run()
    at.selectbox(key="pl_center").set_value(f"custom:{mood_id}").run()

    markers = mood_markers(at)
    # 形は自由記述の雰囲気のまま、色だけを変える
    assert markers["嵐の夜"] == ("triangle-up", PRIMARY_COLOR)
    assert [name for name, (_, color) in markers.items() if color == PRIMARY_COLOR] == ["嵐の夜"]


def test_mood_map_shows_tracks_classified_by_jev(demo: sqlite3.Connection) -> None:
    track = list_tracks(demo, DEMO_LIBRARY_ROOT)[0]
    jev = ClassificationResult(
        engine="jev", engine_version="jev-test", model=None, genre=Genre.ROCK, genre_confidence=None,
        genre_probabilities=None, mood=None,
        attributes={Attribute.BRIGHTNESS: AttributeScore(0.9), Attribute.ENERGY: AttributeScore(0.1)},
    )  # fmt: skip
    with transaction(demo):
        save_classification(demo, track.id, jev)

    at = run_view("playlist_page")

    assert any(c.value.startswith("マップに載っている曲: 1 曲。") for c in at.caption)
    assert not at.info
    [placed] = [row for row in mood_map_values(at) if "曲名" in row]
    assert placed["曲名"] == track.display_title
    assert (placed["x"], placed["y"]) == (9.0, 1.0)


def test_playlist_by_circle_previews_and_saves_conditions(demo: sqlite3.Connection) -> None:
    # リラックスの位置（0.6, 0.25）からの距離が 0、0.1、0.35 の 3 曲
    save_jev_positions(demo, [(0.6, 0.25), (0.7, 0.25), (0.6, 0.6)])
    at = run_view("playlist_page")
    at.radio(key="pl_method").set_value(CreationMethod.CIRCLE).run()
    at.selectbox(key="pl_center").set_value(Mood.RELAX).run()

    # 既定の半径 0.25 では 2 曲。マップは条件の欄の中の 1 つだけを描く
    assert any(m.value.startswith("**選ばれる曲: 2 曲・合計") for m in at.markdown)
    assert sum(1 for row in mood_map_values(at) if row.get("chosen") is True) == 2
    assert any("order" in row for row in mood_map_values(at))
    # 上限がなければ円の中を全部選ぶため、意外性・ランダム性は使わない
    assert at.slider(key="pl_novelty").disabled

    # 半径は 0〜10 の目盛りで指定し、保存する条件は 0〜1 のまま
    at.slider(key="pl_radius").set_value(4.0).run()
    assert any(m.value.startswith("**選ばれる曲: 3 曲・合計") for m in at.markdown)

    at.radio(key="pl_cap").set_value("曲数").run()
    at.number_input(key="pl_count").set_value(1).run()
    assert any(m.value.startswith("**選ばれる曲: 1 曲・合計") for m in at.markdown)
    assert not at.slider(key="pl_novelty").disabled

    click(at, "プレイリストを生成")
    assert any(h.value == "リラックス周辺のプレイリスト" for h in at.subheader)
    assert len(at.dataframe[0].value) == 1
    assert "中心への近さ" in at.dataframe[1].value.columns

    click(at, "プレイリストを保存")
    [saved] = list_playlists(demo)
    assert saved.name.startswith("リラックス周辺 ")
    assert saved.conditions["method"] == "circle"
    assert saved.conditions["center"] == {"x": 0.6, "y": 0.25}
    assert saved.conditions["radius"] == 0.4
    assert saved.conditions["track_count"] == 1
    assert saved.conditions["duration_min"] is None


def test_playlist_by_circle_around_custom_mood(demo: sqlite3.Connection) -> None:
    # 自由記述の雰囲気の位置（0.1, 0.9）の近くに 1 曲、リラックスの位置に 1 曲
    save_jev_positions(demo, [(0.15, 0.9), (0.6, 0.25)])
    mood_id = save_custom(demo, "嵐の夜", 0.1, 0.9)
    at = run_view("playlist_page")
    at.radio(key="pl_method").set_value(CreationMethod.CIRCLE).run()

    center = at.selectbox(key="pl_center")
    assert "嵐の夜" in center.options
    center.set_value(f"custom:{mood_id}").run()

    assert any(m.value.startswith("**選ばれる曲: 1 曲・合計") for m in at.markdown)
    custom_rows = [row for row in mood_map_values(at) if row.get("説明文") == "嵐の夜の説明"]
    # ▲ と名前（縁取りを含む）を、Jev が決めた位置に描く
    assert custom_rows
    assert {(row["x"], row["y"]) for row in custom_rows} == {(1.0, 9.0)}
    assert any("▲ は自由記述で作った雰囲気" in c.value for c in at.caption)

    click(at, "プレイリストを生成")
    assert any(h.value == "嵐の夜周辺のプレイリスト" for h in at.subheader)
    click(at, "プレイリストを保存")
    [saved] = list_playlists(demo)
    assert saved.name.startswith("嵐の夜周辺 ")
    assert saved.conditions["mood"] is None
    assert saved.conditions["center"] == {"x": 0.1, "y": 0.9}
    custom = saved.conditions["custom_mood"]
    assert isinstance(custom, dict)
    assert (custom["name"], custom["description"]) == ("嵐の夜", "嵐の夜の説明")


def test_custom_mood_panel_without_api_key_only_explains(demo: sqlite3.Connection) -> None:
    at = run_view("playlist_page")
    at.radio(key="pl_method").set_value(CreationMethod.CIRCLE).run()

    assert any("TYPESAFE_API_KEY が未設定" in i.value for i in at.info)
    assert not any(b.label == "Jev で位置を決めて作成" for b in at.button)


def test_custom_mood_form_requires_name_and_description(
    demo: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ボタンは押さない（実際の API とは通信しない）
    monkeypatch.setenv("TYPESAFE_API_KEY", "dummy-key-for-display")
    at = run_view("playlist_page")
    at.radio(key="pl_method").set_value(CreationMethod.CIRCLE).run()

    def create_button_disabled() -> bool:
        return next(b for b in at.button if b.label == "Jev で位置を決めて作成").disabled

    assert create_button_disabled()
    at.text_input(key="cm_name").set_value("雨の午後").run()
    assert create_button_disabled()
    at.text_area(key="cm_description").set_value("雨の日曜の午後、本を読みながら").run()
    assert not create_button_disabled()
    assert any("1 件リクエストを送ります" in c.value and "名前や音楽ライブラリの情報は送りません" in c.value
               for c in at.caption)  # fmt: skip


def test_custom_mood_details_and_delete(demo: sqlite3.Connection) -> None:
    save_custom(demo, "残す", 0.3, 0.3)
    removed_id = save_custom(demo, "年代指定", 0.8, 0.6, expressible=0.2)
    at = run_view("playlist_page")
    at.radio(key="pl_method").set_value(CreationMethod.CIRCLE).run()
    at.selectbox(key="pl_center").set_value(f"custom:{removed_id}").run()
    at.selectbox(key="cm_selected").set_value(removed_id).run()

    assert any(t.value == "説明文: 年代指定の説明" for t in at.text)
    # 属性で表せない要素を含む可能性が高い雰囲気には注意を出す
    assert any("属性では表せない要素" in w.value for w in at.warning)
    details = next(d.value for d in at.dataframe if "属性" in d.value.columns)
    assert dict(zip(details["属性"], details["値"], strict=True))["作業BGM適性"] == "判定保留"

    at.checkbox(key=f"cm-delete-confirm-{removed_id}").check().run()
    click(at, "雰囲気を削除")

    assert [m.name for m in list_custom_moods(demo)] == ["残す"]
    assert any(s.value == "「年代指定」を削除しました。" for s in at.success)
    # 削除した雰囲気は円の中心の選択肢から消え、既定の中心に戻る
    assert "年代指定" not in at.selectbox(key="pl_center").options
    assert at.selectbox(key="pl_center").value == Mood.RELAX


def test_randomness_label_follows_creation_method(demo: sqlite3.Connection) -> None:
    # ランダム性 0 のときの順は、雰囲気で選ぶ場合は適合度順、円で選ぶ場合は中心に近い順
    at = run_view("playlist_page")
    at.slider(key="pl_randomness").set_value(0.8).run()
    assert at.slider(key="pl_randomness").label == "ランダム性（左: 適合度順 / 右: ランダム）"

    at.radio(key="pl_method").set_value(CreationMethod.CIRCLE).run()
    at.radio(key="pl_cap").set_value("曲数").run()
    assert at.slider(key="pl_randomness").label == "ランダム性（左: 中心に近い順 / 右: ランダム）"
    # 説明が変わっても、入力済みの値は保つ
    assert at.slider(key="pl_randomness").value == 0.8

    at.radio(key="pl_method").set_value(CreationMethod.MOOD).run()
    assert at.slider(key="pl_randomness").value == 0.8


def test_playlist_by_circle_without_jev_selects_nothing(demo: sqlite3.Connection) -> None:
    # デモデータはルールベースの判定だけで明るさが判定保留のため、円の中に曲がない
    at = run_view("playlist_page")
    at.radio(key="pl_method").set_value(CreationMethod.CIRCLE).run()

    assert any(m.value.startswith("**選ばれる曲: 0 曲") for m in at.markdown)
    assert any("Jev で判定する" in i.value for i in at.info)
    click(at, "プレイリストを生成")
    assert any("曲がありません" in i.value for i in at.info)


def test_playlist_by_circle_requires_genres_when_filtering(demo: sqlite3.Connection) -> None:
    at = run_view("playlist_page")
    at.radio(key="pl_method").set_value(CreationMethod.CIRCLE).run()
    at.radio(key="pl_genre_mode").set_value(GenreMode.ONLY).run()

    click(at, "プレイリストを生成")

    assert any("ジャンルを 1 つ以上選んでください" in e.value for e in at.error)
    assert "pl_result" not in at.session_state
