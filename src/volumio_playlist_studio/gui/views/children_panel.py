"""子供向けのアルバムを Jev で判定する欄と、判定結果の一覧（音楽ライブラリ画面の中に表示する）。"""

import streamlit as st

from volumio_playlist_studio.catalog import judged_albums
from volumio_playlist_studio.config import is_typesafe_api_key_set
from volumio_playlist_studio.gui.context import PageContext, start_job
from volumio_playlist_studio.storage import settings_store
from volumio_playlist_studio.workflows import estimate_children_albums, run_children_albums


def render(ctx: PageContext, library_root: str, busy: bool) -> None:
    """判定の実行欄と、判定済みのアルバムの一覧を表示する。"""
    st.caption(
        "アルバムごとに子供向け（童謡・子供向けの番組やアニメの歌など）かを判定します。"
        "プレイリスト作成の詳細設定で、子供向けの曲を除外する・子供向けだけにする、を選べます。"
    )
    if is_typesafe_api_key_set():
        _run_panel(ctx, library_root, busy)
    else:
        st.info(
            "TYPESAFE_API_KEY が未設定のため、Jev は利用できません。未判定の曲は子供向けではないものとして扱います。"
        )

    _judged_list(ctx, library_root)


def _run_panel(ctx: PageContext, library_root: str, busy: bool) -> None:
    estimate = estimate_children_albums(ctx.conn, library_root)
    st.write(
        f"未判定のアルバム: **{estimate.pending_albums} 件**　今回判定するアルバム: **{estimate.albums_this_run} 件**"
        f"（1 回の上限は設定画面の「1 回の実行で判定する曲数の上限」と共通）"
    )
    st.write(
        f"送信するリクエスト: **{estimate.requests_this_run} 件**"
        "（1 アルバム 1 件。通信エラー時は SDK が自動で再試行します）"
    )
    st.caption(
        "送信するのはアルバム名・アーティスト名・ジャンルタグ・収録曲名だけで、音声ファイルやファイルパスは送りません。"
        "料金は TypeSafe の料金体系に従います。"
    )
    confirmed = st.checkbox("上記の件数で API を呼び出すことを確認しました", key="children-confirm")
    if st.button("子供向けか判定", disabled=busy or not confirmed or estimate.albums_this_run == 0):
        start_job(
            "子供向けの判定",
            ctx.config.database_path,
            lambda conn, control: run_children_albums(conn, library_root, control),
        )
        st.rerun()


def _judged_list(ctx: PageContext, library_root: str) -> None:
    albums = judged_albums(ctx.conn, library_root)
    if not albums:
        return

    threshold = settings_store.get_jev_settings(ctx.conn).children_threshold
    children = [a for a in albums if a.children_probability >= threshold]
    st.markdown(f"**子供向けと判定したアルバム: {len(children)} 件**（判定済み {len(albums)} 件）")
    if children:
        rows = [
            {
                "アルバム": a.title,
                "アーティスト": a.artist or "不明",
                "曲数": a.track_count,
                "確率": f"{a.children_probability:.2f}",
            }
            for a in children
        ]
        st.dataframe(rows, hide_index=True, width="stretch")
