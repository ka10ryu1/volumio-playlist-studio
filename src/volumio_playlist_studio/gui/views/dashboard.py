"""ダッシュボード画面。ライブラリと解析の状況を一覧する。"""

from collections.abc import Callable

import streamlit as st

from volumio_playlist_studio.catalog import dashboard_stats
from volumio_playlist_studio.gui.context import PageContext
from volumio_playlist_studio.gui.widgets import music_folder_form


def render(
    ctx: PageContext,
    *,
    on_playlist: Callable[[], None] | None = None,
    on_library: Callable[[], None] | None = None,
) -> None:
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

    if stats.unanalyzed or stats.total == 0:
        st.info("音楽ライブラリでスキャン・解析を行うと、選曲に使える曲が増えます。")
    else:
        st.caption("雰囲気を選んで、今日のプレイリストを作りましょう。")

    if stats.failed:
        st.warning(f"解析に失敗した曲が {stats.failed} 曲あります。音楽ライブラリで再試行できます。")

    with st.container(horizontal=True):
        st.button("プレイリストを作成", type="primary", on_click=on_playlist, disabled=on_playlist is None)
        st.button("音楽ライブラリを開く", on_click=on_library, disabled=on_library is None)

    st.subheader("ジャンル分布")
    if stats.genre_distribution:
        st.vega_lite_chart(
            [{"ジャンル": genre, "曲数": count} for genre, count in stats.genre_distribution.items()],
            {
                "mark": "bar",
                "height": 300,
                "encoding": {
                    "x": {"field": "曲数", "type": "quantitative", "axis": {"tickMinStep": 1}},
                    "y": {"field": "ジャンル", "type": "nominal", "title": None, "axis": {"labelLimit": 210}},
                    "tooltip": [{"field": "ジャンル"}, {"field": "曲数"}],
                },
            },
            width="stretch",
        )
    else:
        st.caption("楽曲がまだありません。音楽ライブラリ画面でスキャンしてください。")

    with st.expander("解析・判定の詳細"):
        row2 = st.columns(2)
        row2[0].metric("Jev 判定済み", stats.jev_classified)
        row2[1].metric("解析失敗", stats.failed)
        row3 = st.columns(2)
        row3[0].metric("解析エラー", stats.error_count, help="未解決のエラーの件数（曲ごとに最新の 1 件）。")
        row3[1].metric("解析未対応の形式", stats.unsupported, help="M4A/AAC はタグのみ取得し、音響解析は行いません。")
        st.caption(f"最終スキャン: {stats.last_scan_at or '未実施'}")
