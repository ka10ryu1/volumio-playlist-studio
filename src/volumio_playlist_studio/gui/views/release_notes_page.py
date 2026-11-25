"""リリースノート画面。新機能などの変更を新しい順に一覧で表示し、項目を開くと詳しい説明を表示する。"""

import logging

import streamlit as st

from volumio_playlist_studio.config import RELEASE_NOTES_PATH, REPOSITORY_URL
from volumio_playlist_studio.gui.context import PageContext
from volumio_playlist_studio.gui.widgets import page_title
from volumio_playlist_studio.release_notes import ReleaseNote, ReleaseNotesError, load_release_notes

logger = logging.getLogger(__name__)


def render(ctx: PageContext) -> None:
    """リリースノート画面を表示する。

    Args:
        ctx: 他の画面と同じ形で呼び出せるよう受け取る。ライブラリの内容は表示しないため使わない。
    """
    page_title("リリースノート")
    try:
        notes = load_release_notes(RELEASE_NOTES_PATH)
    except OSError as exc:
        # OSError のメッセージには絶対パスが含まれるため、種類だけを記録する
        logger.warning("release_notes_load_failed: error=%s", type(exc).__name__)
        _show_load_error()
        return
    except ReleaseNotesError as exc:
        logger.warning("release_notes_load_failed: error=%s detail=%s", type(exc).__name__, exc)
        _show_load_error()
        return

    if not notes:
        st.info("リリースノートはまだありません。")
        return

    st.caption("新しい順に表示しています。項目を押すと、詳しい説明を表示します。")
    for note in notes:
        _note_section(note)


def _show_load_error() -> None:
    st.error(
        "リリースノートを読み込めませんでした。"
        "アプリを最新の状態に更新しても表示されない場合は、問い合わせ（GitHub）から知らせてください。"
    )


def _note_section(note: ReleaseNote) -> None:
    with st.expander(f"{note.merged_on.isoformat()}　#{note.pr_number}　{note.title}"):
        st.markdown(note.body)
        st.markdown(f"[PR #{note.pr_number} を GitHub で見る]({REPOSITORY_URL}/pull/{note.pr_number})")
