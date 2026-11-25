"""プレイリスト作成画面の曲一覧。

曲ごとに、ブラウザでの再生と、理由を選んで除外するボタンを置く。再生は画面を開いている端末のブラウザで行い、
Volumio の再生状態は変えない。音楽ファイルは読むだけで、除外してもファイルは変更しない。
除外の理由は評価データとして DB に記録する（`storage/exclusions.py`）。
"""

from dataclasses import dataclass

import streamlit as st

from volumio_playlist_studio.gui.audio_player import track_player
from volumio_playlist_studio.gui.context import PageContext
from volumio_playlist_studio.gui.widgets import format_duration, genre_label, track_label
from volumio_playlist_studio.models import ClassificationSource
from volumio_playlist_studio.playlist import SelectedTrack, remove_item
from volumio_playlist_studio.storage.database import transaction
from volumio_playlist_studio.storage.exclusions import ExclusionReason, TrackExclusion, record_exclusion

# プレーヤーを開いている曲の ID。再生用のデータは audio_player がセッションごとに 1 曲分だけ持つ
_PLAYING_KEY = "pl_playing_track"
# 曲がこの数を超えたら、一覧を固定の高さにして中をスクロールさせる。下の操作や「保存する」を遠ざけないため
_SCROLL_AFTER_TRACKS = 8
_LIST_HEIGHT = 480


@dataclass(frozen=True)
class _ReasonButton:
    label: str
    icon: str
    help: str


_REASON_BUTTONS = {
    ExclusionReason.GENRE_MISMATCH: _ReasonButton(
        "ジャンル違い",
        ":material/block:",
        "ジャンルとして不適切なため除外します。分類の誤りとして記録し、分類の評価・改善に使います。",
    ),
    ExclusionReason.PREFERENCE: _ReasonButton(
        "好みでない",
        ":material/thumb_down:",
        "好みの問題で除外します。記録するだけで、次回以降の選曲からは除外しません。",
    ),
}


def track_list(
    ctx: PageContext,
    draft: list[SelectedTrack],
    conditions: dict[str, object],
    classification_source: ClassificationSource,
) -> list[SelectedTrack] | None:
    """曲一覧を表示する。除外のボタンが押されたら、理由を記録して除外後の曲の並びを返す。

    Args:
        ctx: 画面の共通情報。
        draft: 編集中の曲の並び。
        conditions: プレイリストの生成条件。除外の記録に残す。
        classification_source: 曲を選んだときの分類結果の種類。生成後に設定を変えても、生成時の種類を記録する。

    Returns:
        曲を除外した場合は除外後の曲の並び。除外しなかった場合は None。
    """
    st.caption(
        "「再生」で、この画面を開いている端末のブラウザで曲を確かめられます（Volumio では再生しません）。"
        "「ジャンル違い」「好みでない」を押すと、その理由を記録して曲を除外します。"
    )
    excluded: tuple[int, ExclusionReason] | None = None
    with st.container(height=_LIST_HEIGHT if len(draft) > _SCROLL_AFTER_TRACKS else "content", key="pl-track-list"):
        for index, item in enumerate(draft):
            reason = _track_row(ctx, index, item)
            if reason is not None:
                excluded = (index, reason)

    if excluded is None:
        return None

    index, reason = excluded
    item = draft[index]
    with transaction(ctx.conn):
        record_exclusion(
            ctx.conn,
            TrackExclusion(
                track_id=item.track.id,
                reason=reason,
                genre=item.candidate.genre,
                classification_source=classification_source.value,
                classification=item.candidate.classification,
                conditions=conditions,
            ),
        )

    if st.session_state.get(_PLAYING_KEY) == item.track.id:
        st.session_state.pop(_PLAYING_KEY)

    return remove_item(draft, index)


def close_player() -> None:
    """開いているプレーヤーを閉じる。曲一覧を作り直したときに、前の一覧で開いた曲のプレーヤーを残さないために使う。"""
    st.session_state.pop(_PLAYING_KEY, None)


def _track_row(ctx: PageContext, index: int, item: SelectedTrack) -> ExclusionReason | None:
    """曲 1 行分を表示し、押された除外のボタンの理由を返す。"""
    track = item.track
    playing = st.session_state.get(_PLAYING_KEY) == track.id
    reason = None
    # 曲ごとに枠で囲み、どのボタンがどの曲のものか、ボタンが折り返す狭い画面でも分かるようにする
    with st.container(border=True, gap="small"):
        # 曲名はタグ由来のため、Markdown として解釈させない
        st.text(
            f"{index + 1}. {track_label(track)}"
            f"（{format_duration(track.duration_sec)}・{genre_label(item.candidate.genre)}）"
        )
        with st.container(horizontal=True, gap="small"):
            st.button(
                "閉じる" if playing else "再生",
                icon=":material/stop:" if playing else ":material/play_arrow:",
                key=f"pl-play-{track.id}",
                on_click=_toggle_player,
                args=(track.id,),
            )
            for candidate_reason, button in _REASON_BUTTONS.items():
                if st.button(
                    button.label, icon=button.icon, help=button.help, key=f"pl-exclude-{candidate_reason}-{track.id}"
                ):
                    reason = candidate_reason

        if playing:
            _player(ctx, item)

    return reason


def _toggle_player(track_id: int) -> None:
    if st.session_state.get(_PLAYING_KEY) == track_id:
        st.session_state.pop(_PLAYING_KEY)
    else:
        st.session_state[_PLAYING_KEY] = track_id


def _player(ctx: PageContext, item: SelectedTrack) -> None:
    # 音楽フォルダを変更した後に残っていた曲は、今の音楽フォルダのファイルとして読まない
    if ctx.library_root is None or item.track.library_root != ctx.library_root:
        st.caption("現在の音楽フォルダの曲ではないため再生できません。")
        return

    track_player(ctx.library_root, item.track)
