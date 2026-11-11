"""プレイリスト作成画面の「自由記述で雰囲気を作る」。説明文から Jev がマップ上の位置を決める。

作った雰囲気は DB に保存し、円の中心の候補に加える。Jev を呼ぶのは「作成」を押したときの 1 回だけで、
円の調整やプレイリストの生成では呼ばない。
"""

import logging

import streamlit as st

from volumio_playlist_studio.config import is_typesafe_api_key_set
from volumio_playlist_studio.custom_mood import MAX_DESCRIPTION_LENGTH, MAX_NAME_LENGTH
from volumio_playlist_studio.gui.context import PageContext, running_job, start_job
from volumio_playlist_studio.models import ATTRIBUTE_LABELS, CustomMood
from volumio_playlist_studio.mood_map import DISPLAY_SCALE
from volumio_playlist_studio.storage.custom_moods import delete_custom_mood
from volumio_playlist_studio.storage.database import transaction
from volumio_playlist_studio.storage.settings_store import get_jev_settings
from volumio_playlist_studio.workflows import UNEXPRESSIBLE_NOTE, run_custom_mood

logger = logging.getLogger(__name__)

_JOB_NAME = "雰囲気の作成（Jev）"
_NAME_KEY = "cm_name"
_DESCRIPTION_KEY = "cm_description"
_SELECTED_KEY = "cm_selected"
_DELETED_NAME_KEY = "cm_deleted_name"


def render(ctx: PageContext, custom_moods: list[CustomMood]) -> None:
    """自由記述の雰囲気の作成欄と、作成済みの雰囲気の一覧を表示する。"""
    _create_form(ctx)
    _saved_moods(ctx, custom_moods)


def _create_form(ctx: PageContext) -> None:
    st.caption(
        "説明文（例: 雨の日曜の午後、本を読みながら）から、Jev がマップ上の位置を決めます。"
        "作った雰囲気は円の中心に選べます。"
    )
    if not is_typesafe_api_key_set():
        st.info("TYPESAFE_API_KEY が未設定のため、Jev は利用できません。既存の雰囲気を円の中心にしてください。")
        return

    name = st.text_input("名前（マップに表示）", max_chars=MAX_NAME_LENGTH, key=_NAME_KEY)
    description = st.text_area("説明文", max_chars=MAX_DESCRIPTION_LENGTH, key=_DESCRIPTION_KEY, height=80)
    st.caption(
        "作成するたびに Jev へ 1 件リクエストを送ります（料金は TypeSafe の料金体系に従います）。"
        "送るのは説明文だけで、名前や音楽ライブラリの情報は送りません。"
        "年代・ボーカル・楽器などは、明るさやエネルギー感などの属性で表せないため反映されません。"
    )
    busy = running_job() is not None
    if st.button("Jev で位置を決めて作成", disabled=busy or not name.strip() or not description.strip()):
        start_job(
            _JOB_NAME,
            ctx.config.database_path,
            lambda conn, _control: run_custom_mood(conn, name, description),
        )
        st.rerun()


def _saved_moods(ctx: PageContext, custom_moods: list[CustomMood]) -> None:
    deleted_name: str | None = st.session_state.pop(_DELETED_NAME_KEY, None)
    if deleted_name is not None:
        st.success(f"「{deleted_name}」を削除しました。")

    if not custom_moods:
        return

    st.markdown("**作成済みの雰囲気**")
    by_id = {m.id: m for m in custom_moods}
    # 削除した雰囲気が選択されたまま残らないようにする
    if st.session_state.get(_SELECTED_KEY) not in by_id:
        st.session_state.pop(_SELECTED_KEY, None)

    selected_id = st.selectbox(
        "雰囲気",
        list(by_id),
        format_func=lambda mood_id: by_id[mood_id].name,
        key=_SELECTED_KEY,
        # 一覧から選ぶだけなので文字入力を受け付けず、スマートフォンでキーボードを出さない
        filter_mode=None,
    )
    if selected_id is None:
        return

    mood = by_id[selected_id]

    # 説明文は利用者の入力のため、Markdown として解釈させない
    st.text(f"説明文: {mood.description}")
    position = f"明るさ {mood.x * DISPLAY_SCALE:.1f}・エネルギー感 {mood.y * DISPLAY_SCALE:.1f}"
    st.caption(f"マップ上の位置: {position}（{mood.model or 'モデル不明'}）")
    if mood.expressible < get_jev_settings(ctx.conn).expressible_threshold:
        st.warning(UNEXPRESSIBLE_NOTE)

    with st.expander("属性の値（参考。円で選ぶときは明るさとエネルギー感だけを使います）"):
        st.dataframe(
            [
                {
                    "属性": ATTRIBUTE_LABELS[attribute],
                    "値": "判定保留" if score.value is None else f"{score.value:.2f}",
                    "confidence": "-" if score.confidence is None else f"{score.confidence:.2f}",
                }
                for attribute, score in mood.attributes.items()
            ],
            hide_index=True,
            width="stretch",
        )

    st.caption("削除しても、この雰囲気で作ったプレイリストの条件には名前・説明文・位置が残ります。")
    confirmed = st.checkbox("この雰囲気を削除します", key=f"cm-delete-confirm-{mood.id}")
    if st.button("雰囲気を削除", disabled=not confirmed, key=f"cm-delete-{mood.id}"):
        _delete(ctx, mood)


def _delete(ctx: PageContext, mood: CustomMood) -> None:
    try:
        with transaction(ctx.conn):
            delete_custom_mood(ctx.conn, mood.id)
    except ValueError:
        # 別のタブなどで先に削除された場合。一覧を読み直して表示を合わせる
        logger.warning("custom_mood_delete_skipped: custom_mood_id=%d reason=not_found", mood.id)
        st.error("この雰囲気は既に削除されています。")
        return

    logger.info("custom_mood_deleted: custom_mood_id=%d", mood.id)
    st.session_state[_DELETED_NAME_KEY] = mood.name
    st.rerun()
