"""プレイリスト管理画面。保存済みのプレイリストと、自由記述で作った雰囲気を一覧で管理する。

プレイリストのタブでは、表で選んだプレイリストの詳細の表示・Volumio への登録・削除を行う。
複数選んだ場合は、まとめて削除できる。雰囲気のタブは `custom_mood_panel` に任せる。
"""

import logging

import streamlit as st

from volumio_playlist_studio.gui.context import PageContext
from volumio_playlist_studio.gui.views import custom_mood_panel, volumio_panel
from volumio_playlist_studio.gui.widgets import (
    format_duration,
    page_title,
    reset_selection_if_changed,
    track_label,
)
from volumio_playlist_studio.storage.custom_moods import list_custom_moods
from volumio_playlist_studio.storage.database import transaction
from volumio_playlist_studio.storage.playlists import (
    SavedPlaylist,
    delete_playlist,
    list_playlists,
    list_registrations,
)
from volumio_playlist_studio.storage.tracks import get_track

logger = logging.getLogger(__name__)

_TABLE_KEY = "manage_playlists"
_LISTED_IDS_KEY = "manage_playlists_ids"
_DELETED_NAMES_KEY = "manage_deleted_names"
_NOT_REGISTERED = "未登録"
_OPEN_PLAYLIST_KEY = "manage_open_playlist"


def select_playlist(playlist_id: int | None) -> None:
    """ダッシュボードから開くプレイリストを指定する。None は通常の一覧を開く。"""
    if playlist_id is None:
        st.session_state.pop(_OPEN_PLAYLIST_KEY, None)
    else:
        st.session_state[_OPEN_PLAYLIST_KEY] = playlist_id


def render(ctx: PageContext) -> None:
    """プレイリスト管理画面を表示する。"""
    page_title("プレイリスト管理")
    if ctx.library_root is None:
        st.info("音楽フォルダが未設定です。音楽ライブラリ画面で指定するか、サンプルデータで試してください。")
        return

    playlists_tab, moods_tab = st.tabs(["プレイリスト", "雰囲気"], key="manage-tabs")
    with playlists_tab:
        _playlists(ctx)

    with moods_tab:
        custom_mood_panel.render(ctx, list_custom_moods(ctx.conn))


def _playlists(ctx: PageContext) -> None:
    deleted_names: list[str] | None = st.session_state.pop(_DELETED_NAMES_KEY, None)
    if deleted_names:
        st.success(f"{''.join(f'「{name}」' for name in deleted_names)}を削除しました。")

    playlists = list_playlists(ctx.conn)
    if _opened_playlist(ctx, playlists):
        return

    if not playlists:
        st.caption("保存したプレイリストがありません。プレイリスト作成画面で作成・保存できます。")
        return

    reset_selection_if_changed(_TABLE_KEY, _LISTED_IDS_KEY, [p.id for p in playlists])
    st.caption("表の左端で選ぶと、曲目の確認と Volumio への登録ができます。複数選ぶと、まとめて削除できます。")
    event = st.dataframe(
        [
            {
                "名前": p.name,
                "曲数": len(p.entries),
                "合計時間": format_duration(p.total_duration_sec),
                "満足度": "-" if p.satisfaction is None else p.satisfaction,
                "Volumio": _registration_summary(ctx, p),
                "作成日時": p.created_at,
            }
            for p in playlists
        ],
        hide_index=True,
        width="stretch",
        on_select="rerun",
        selection_mode="multi-row",
        key=_TABLE_KEY,
    )
    selected = [playlists[i] for i in event.selection.rows if i < len(playlists)]
    if not selected:
        return

    _delete_form(ctx, selected)
    if len(selected) == 1:
        _details(ctx, selected[0])
    else:
        st.caption("曲目と Volumio への登録は、プレイリストを 1 件だけ選ぶと表示します。")


def _opened_playlist(ctx: PageContext, playlists: list[SavedPlaylist]) -> bool:
    playlist_id = st.session_state.get(_OPEN_PLAYLIST_KEY)
    if playlist_id is None:
        return False

    selected = next((playlist for playlist in playlists if playlist.id == playlist_id), None)
    if selected is None:
        select_playlist(None)
        st.info("選んだプレイリストは削除されています。一覧から選び直してください。")
        return False

    st.button("一覧に戻る", on_click=select_playlist, args=(None,))
    _details(ctx, selected)
    _delete_form(ctx, [selected])
    return True


def _registration_summary(ctx: PageContext, playlist: SavedPlaylist) -> str:
    """一覧に出す Volumio への登録状況。成功した登録のうち最新のものの名前を示す。"""
    for record in list_registrations(ctx.conn, playlist.id):
        if record.status == "success":
            return f"登録済み（{record.volumio_playlist_name}）"

    return _NOT_REGISTERED


def _details(ctx: PageContext, playlist: SavedPlaylist) -> None:
    st.subheader(playlist.name)
    st.caption(f"{len(playlist.entries)} 曲・合計 {format_duration(playlist.total_duration_sec)}")
    rows = []
    for position, entry in enumerate(playlist.entries):
        track = get_track(ctx.conn, entry.track_id)
        rows.append(
            {
                "No.": position + 1,
                "曲": "（ライブラリにない曲）" if track is None else track_label(track),
                "時間": format_duration(None if track is None else track.duration_sec),
            }
        )

    st.dataframe(rows, hide_index=True, width="stretch")
    volumio_panel.render(ctx, playlist)


def _delete_form(ctx: PageContext, playlists: list[SavedPlaylist]) -> None:
    st.caption(
        "削除すると、このアプリの一覧に表示されなくなります。満足度などの評価は精度評価の集計に残ります。"
        "Volumio に登録したプレイリストは削除しません（Volumio の画面で削除してください）。"
    )
    # 選び直したときに確認のチェックを引き継がないよう、選んだプレイリストごとに別のキーを使う
    ids = "-".join(str(p.id) for p in playlists)
    confirmed = st.checkbox(f"選んだ {len(playlists)} 件のプレイリストを削除します", key=f"pl-delete-confirm-{ids}")
    if st.button("選んだプレイリストを削除", disabled=not confirmed, key=f"pl-delete-{ids}"):
        _delete(ctx, playlists)


def _delete(ctx: PageContext, playlists: list[SavedPlaylist]) -> None:
    try:
        with transaction(ctx.conn):
            for playlist in playlists:
                delete_playlist(ctx.conn, playlist.id)

    except ValueError:
        # 別のタブなどで先に削除された場合。途中まで削除せず、一覧を読み直して選び直してもらう
        logger.warning("playlist_delete_skipped: count=%d reason=not_found_or_deleted", len(playlists))
        st.session_state.pop(_TABLE_KEY, None)
        st.error("既に削除されたプレイリストが含まれていたため、削除しませんでした。一覧を確認して選び直してください。")
        return

    logger.info("playlists_deleted: ids=%s", ",".join(str(p.id) for p in playlists))
    st.session_state[_DELETED_NAMES_KEY] = [p.name for p in playlists]
    # 削除後の一覧で、別の行が選ばれたままにならないようにする
    st.session_state.pop(_TABLE_KEY, None)
    st.rerun()
