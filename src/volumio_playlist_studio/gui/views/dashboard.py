"""ダッシュボード画面。ライブラリと解析の状況を一覧する。"""

import streamlit as st

from volumio_playlist_studio.catalog import dashboard_stats
from volumio_playlist_studio.gui.context import PageContext
from volumio_playlist_studio.gui.widgets import music_folder_form


def render(ctx: PageContext) -> None:
    """ダッシュボードを表示する。"""
    st.title("ダッシュボード")
    if ctx.library_root is None:
        st.info("音楽フォルダが未設定です。フォルダを指定するか、サンプルデータで試してください。")
        music_folder_form(ctx)
        return

    if ctx.is_demo:
        st.caption("サンプルデータ（架空の楽曲）を表示しています。")

    stats = dashboard_stats(ctx.conn, ctx.library_root, ctx.classification_source)

    # 狭い画面では列が縦に並ぶため、1 行あたりの列数を少なめにする
    row1 = st.columns(3)
    row1[0].metric("登録楽曲数", stats.total)
    row1[1].metric("解析済み", stats.analyzed)
    row1[2].metric("未解析", stats.unanalyzed)
    row2 = st.columns(3)
    row2[0].metric("Jev 判定済み", stats.jev_classified)
    row2[1].metric("解析エラー", stats.error_count)
    row2[2].metric("解析未対応の形式", stats.unsupported, help="M4A/AAC はタグのみ取得し、音響解析は行いません。")
    st.caption(f"最終スキャン: {stats.last_scan_at or '未実施'}")

    st.subheader("ジャンル分布")
    if stats.genre_distribution:
        st.bar_chart(
            {"ジャンル": list(stats.genre_distribution), "曲数": list(stats.genre_distribution.values())},
            x="ジャンル",
            y="曲数",
            horizontal=True,
        )
    else:
        st.caption("楽曲がまだありません。音楽ライブラリ画面でスキャンしてください。")
