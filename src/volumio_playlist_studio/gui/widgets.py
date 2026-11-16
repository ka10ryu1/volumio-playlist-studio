"""複数の画面で使う表示部品と書式。"""

from pathlib import Path

import streamlit as st

from volumio_playlist_studio.gui.context import PageContext
from volumio_playlist_studio.library_paths import LibraryPathError, detect_platform, resolve_library_input
from volumio_playlist_studio.models import (
    ATTRIBUTE_LABELS,
    GENRE_LABELS,
    MOOD_LABELS,
    ClassificationResult,
    Genre,
    Mood,
    Track,
)
from volumio_playlist_studio.sample_data import load_demo_library
from volumio_playlist_studio.storage import settings_store
from volumio_playlist_studio.storage.database import transaction

_SECONDS_PER_MINUTE = 60


def page_title(title: str) -> None:
    """画面のタイトルと、画面を描き直す「更新」ボタンを同じ行に表示する。

    Streamlit は画面の中に独自のスクロール領域を持ち、スマホの「引っ張って更新」が効かないため、代わりに置く。
    押すと再実行されるだけで、入力内容や LAN 接続の PIN 認証は保たれる。スマホで本文を押し下げないよう、
    タイトルと別の行にはしない。
    """
    with st.container(horizontal=True, vertical_alignment="center"):
        st.title(title, width="stretch")
        st.button("更新", icon=":material/refresh:", type="tertiary", help="画面を最新の状態に描き直します")


def reset_selection_if_changed(table_key: str, ids_key: str, ids: list[int]) -> None:
    """表の行の並びが前回の表示から変わっていたら、表の行選択を解除する。表を描く前に呼ぶ。

    行選択は行番号で保持されるため、別のタブでの削除や追加で並びが変わると、
    利用者が選んでいない行が選ばれた状態になる。それを防ぐ。

    Args:
        table_key: 表（st.dataframe）のキー。
        ids_key: 前回表示した行の ID の並びを保持するキー。
        ids: 今回表示する行の ID の並び。
    """
    if st.session_state.get(ids_key) != ids:
        st.session_state.pop(table_key, None)

    st.session_state[ids_key] = ids


def format_duration(seconds: float | None) -> str:
    """秒を「分:秒」の文字列にする。不明なら「-」。"""
    if seconds is None:
        return "-"

    minutes, secs = divmod(round(seconds), _SECONDS_PER_MINUTE)
    return f"{minutes}:{secs:02d}"


def genre_label(genre: Genre | None) -> str:
    """ジャンルの表示名。"""
    return "不明" if genre is None else GENRE_LABELS[genre]


def mood_label(mood: Mood | None) -> str:
    """ムードの表示名。"""
    return "判定保留" if mood is None else MOOD_LABELS[mood]


def track_label(track: Track) -> str:
    """選択肢などに使う、曲名とアーティスト名の表示。"""
    return f"{track.display_title} — {track.display_artist}"


def classification_details(result: ClassificationResult | None) -> None:
    """分類結果の内容を表示する。"""
    if result is None:
        st.caption("未分類")
        return

    model = f"（モデル: {result.model}）" if result.model else ""
    st.caption(f"{result.engine} / {result.engine_version}{model}")
    confidence = "" if result.genre_confidence is None else f"（確信度 {result.genre_confidence:.2f}）"
    st.write(f"ジャンル: **{genre_label(result.genre)}**{confidence}　ムード: **{mood_label(result.mood)}**")
    rows = []
    for attribute, score in result.attributes.items():
        rows.append(
            {
                "属性": ATTRIBUTE_LABELS[attribute],
                "スコア (0〜1)": "判定保留" if score.value is None else f"{score.value:.2f}",
                "確信度": "対象外" if score.confidence is None else f"{score.confidence:.2f}",
                "状態": "判定保留" if score.is_held else "判定済み",
            }
        )

    st.dataframe(rows, hide_index=True, width="stretch")
    st.caption("確信度は AI の回答への確かさで、正解率ではありません。ルールベースでは対象外です。")
    for note in result.notes:
        st.caption(f"・{note}")


def music_folder_form(ctx: PageContext) -> None:
    """音楽フォルダの指定欄と、サンプルデータの読み込みボタンを表示する。"""
    current = "" if ctx.library_root is None or ctx.is_demo else ctx.library_root
    with st.form("music-folder", enter_to_submit=False):
        text = st.text_input(
            "音楽フォルダ",
            value=current,
            placeholder="\\\\volumio.local\\Internal Storage または C:\\Users\\...\\Music",
            help=(
                "Windows では Volumio の SMB 共有を UNC パスでそのまま指定できます。"
                "WSL ではマウント先を指定します（README 参照）。"
            ),
        )
        submitted = st.form_submit_button("保存")

    if submitted:
        try:
            path = resolve_library_input(text, host=detect_platform())
        except LibraryPathError as exc:
            st.error(exc.user_message)
            return

        if not Path(path).is_dir():
            st.error("指定したフォルダが見つかりません。パスとマウント状態を確認してください。")
            return

        with transaction(ctx.conn):
            settings_store.set_value(ctx.conn, settings_store.MUSIC_ROOT_KEY, path.as_posix())

        st.success("音楽フォルダを保存しました。")
        st.rerun()

    if st.button("サンプルデータで試す", help="架空の楽曲 60 曲を登録します。音楽ファイルは不要です。"):
        count = load_demo_library(ctx.conn)
        with transaction(ctx.conn):
            settings_store.set_value(ctx.conn, settings_store.MUSIC_ROOT_KEY, "sample://demo")

        st.success(f"サンプルデータ {count} 曲を読み込みました。")
        st.rerun()
