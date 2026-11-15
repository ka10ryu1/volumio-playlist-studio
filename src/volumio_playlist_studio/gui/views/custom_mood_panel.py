"""プレイリスト管理画面の「雰囲気」タブ。自由記述の雰囲気の作成・一覧・削除を行う。

説明文から Jev がマップ上の位置を決め、作った雰囲気は DB に保存してプレイリスト作成画面の円の中心の候補に加える。
Jev を呼ぶのは「作成」を押したときの 1 回だけで、円の調整やプレイリストの生成では呼ばない。
"""

import logging

import streamlit as st

from volumio_playlist_studio.config import is_typesafe_api_key_set
from volumio_playlist_studio.custom_mood import MAX_DESCRIPTION_LENGTH, MAX_NAME_LENGTH
from volumio_playlist_studio.gui.context import PageContext, running_job, start_job
from volumio_playlist_studio.gui.widgets import reset_selection_if_changed
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
_TABLE_KEY = "cm_table"
_LISTED_IDS_KEY = "cm_table_ids"
_DELETED_NAMES_KEY = "cm_deleted_names"


def render(ctx: PageContext, custom_moods: list[CustomMood]) -> None:
    """自由記述の雰囲気の作成欄と、作成済みの雰囲気の一覧を表示する。"""
    st.subheader("自由記述で雰囲気を作る（Jev を使用）")
    _create_form(ctx)
    _saved_moods(ctx, custom_moods)


def _create_form(ctx: PageContext) -> None:
    st.caption(
        "説明文（例: 雨の日曜の午後、本を読みながら）から、Jev がマップ上の位置を決めます。"
        "作った雰囲気は、プレイリスト作成の「マップの円で選ぶ」で円の中心に選べます。"
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
    st.subheader("作成済みの雰囲気")
    deleted_names: list[str] | None = st.session_state.pop(_DELETED_NAMES_KEY, None)
    if deleted_names:
        st.success(f"{_quoted(deleted_names)}を削除しました。")

    if not custom_moods:
        st.caption("作成した雰囲気はありません。")
        return

    reset_selection_if_changed(_TABLE_KEY, _LISTED_IDS_KEY, [m.id for m in custom_moods])
    st.caption("表の左端で選ぶと詳細を表示します。複数選ぶと、まとめて削除できます。")
    event = st.dataframe(
        [
            {
                "名前": m.name,
                "説明文": m.description,
                "明るさ": round(m.x * DISPLAY_SCALE, 1),
                "エネルギー感": round(m.y * DISPLAY_SCALE, 1),
                "作成日時": m.created_at,
            }
            for m in custom_moods
        ],
        hide_index=True,
        width="stretch",
        on_select="rerun",
        selection_mode="multi-row",
        key=_TABLE_KEY,
    )
    selected = [custom_moods[i] for i in event.selection.rows if i < len(custom_moods)]
    if not selected:
        return

    if len(selected) == 1:
        _details(ctx, selected[0])

    _delete_form(ctx, selected)


def _details(ctx: PageContext, mood: CustomMood) -> None:
    st.markdown(f"**{mood.name}**")
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


def _delete_form(ctx: PageContext, moods: list[CustomMood]) -> None:
    st.caption("削除しても、この雰囲気で作ったプレイリストの条件には名前・説明文・位置が残ります。")
    # 選び直したときに確認のチェックを引き継がないよう、選んだ雰囲気ごとに別のキーを使う
    ids = "-".join(str(m.id) for m in moods)
    confirmed = st.checkbox(f"選んだ {len(moods)} 件の雰囲気を削除します", key=f"cm-delete-confirm-{ids}")
    if st.button("選んだ雰囲気を削除", disabled=not confirmed, key=f"cm-delete-{ids}"):
        _delete(ctx, moods)


def _delete(ctx: PageContext, moods: list[CustomMood]) -> None:
    try:
        with transaction(ctx.conn):
            for mood in moods:
                delete_custom_mood(ctx.conn, mood.id)

    except ValueError:
        # 別のタブなどで先に削除された場合。途中まで削除せず、一覧を読み直して選び直してもらう
        logger.warning("custom_mood_delete_skipped: count=%d reason=not_found", len(moods))
        st.session_state.pop(_TABLE_KEY, None)
        st.error("既に削除された雰囲気が含まれていたため、削除しませんでした。一覧を確認して選び直してください。")
        return

    logger.info("custom_moods_deleted: ids=%s", ",".join(str(m.id) for m in moods))
    st.session_state[_DELETED_NAMES_KEY] = [m.name for m in moods]
    # 削除後の一覧で、別の行が選ばれたままにならないようにする
    st.session_state.pop(_TABLE_KEY, None)
    st.rerun()


def _quoted(names: list[str]) -> str:
    return "".join(f"「{name}」" for name in names)
