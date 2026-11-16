"""選曲を始め、保存したプレイリストとライブラリの状態を確認するホーム画面。"""

from collections.abc import Callable

import altair as alt
import streamlit as st

from volumio_playlist_studio.catalog import UNKNOWN_GENRE_LABEL, DashboardStats, dashboard_stats
from volumio_playlist_studio.gui.context import PageContext
from volumio_playlist_studio.gui.widgets import format_duration, music_folder_form, page_title
from volumio_playlist_studio.models import CLASSIFICATION_SOURCE_LABELS, GENRE_LABELS, Genre
from volumio_playlist_studio.storage.playlists import list_playlists


def render(
    ctx: PageContext,
    *,
    on_playlist: Callable[[Genre | None], None] | None = None,
    on_library: Callable[[], None] | None = None,
    on_manage: Callable[[int | None], None] | None = None,
    on_evaluation: Callable[[], None] | None = None,
) -> None:
    """主要操作、ライブラリ概要、最近のプレイリストの順に表示する。"""
    page_title("ダッシュボード")
    if ctx.library_root is None:
        st.info("音楽フォルダが未設定です。フォルダを指定するか、サンプルデータで試してください。")
        music_folder_form(ctx)
        return

    if ctx.is_demo:
        st.caption("サンプルデータ（架空の楽曲）を表示しています。")

    stats = dashboard_stats(ctx.conn, ctx.library_root, ctx.classification_source)
    if stats.total == 0:
        st.info("楽曲がまだありません。音楽ライブラリでスキャンして、楽曲を登録しましょう。")
        st.button("音楽ライブラリを開く", type="primary", on_click=on_library, disabled=on_library is None)
        return

    actions, summary = st.columns([1.1, 1], gap="medium")
    with actions:
        _actions(on_playlist, on_manage)

    with summary:
        _library_summary(stats, on_library)

    _recent_playlists(ctx, on_manage)
    _genres(ctx, stats, on_playlist)
    _maintenance(stats, on_library, on_evaluation)


def _actions(
    on_playlist: Callable[[Genre | None], None] | None,
    on_manage: Callable[[int | None], None] | None,
) -> None:
    with st.container(border=True):
        st.subheader("今日はどんな音楽にしますか？")
        st.caption("雰囲気やジャンルから、気分に合う曲を。")
        st.button(
            "プレイリストを作成",
            type="primary",
            width="stretch",
            on_click=on_playlist,
            args=(None,),
            disabled=on_playlist is None,
        )
        st.button(
            "保存したプレイリストを見る",
            width="stretch",
            on_click=on_manage,
            args=(None,),
            disabled=on_manage is None,
        )


def _library_summary(stats: DashboardStats, on_library: Callable[[], None] | None) -> None:
    with st.container(border=True):
        st.subheader("ライブラリの状況")
        st.metric("登録楽曲数", f"{stats.total:,}")
        st.markdown(f"音響解析済み **{stats.analyzed:,} 曲**　／　未解析 **{stats.unanalyzed:,} 曲**")
        st.caption(f"解析失敗 {stats.failed:,} 曲　／　解析未対応 {stats.unsupported:,} 曲")
        st.button("音楽ライブラリを開く", on_click=on_library, disabled=on_library is None)


def _recent_playlists(ctx: PageContext, on_manage: Callable[[int | None], None] | None) -> None:
    st.subheader("最近保存したプレイリスト")
    playlists = list_playlists(ctx.conn)[:3]
    if not playlists:
        st.caption("保存すると、ここから曲目を確認できます。まずはプレイリストを作ってみましょう。")
        return

    for playlist in playlists:
        with st.container(border=True):
            st.button(
                playlist.name,
                key=f"dashboard-playlist-{playlist.id}",
                width="stretch",
                on_click=on_manage,
                args=(playlist.id,),
                disabled=on_manage is None,
            )
            st.caption(f"{len(playlist.entries)} 曲 ・ {format_duration(playlist.total_duration_sec)}")


def _genres(
    ctx: PageContext,
    stats: DashboardStats,
    on_playlist: Callable[[Genre | None], None] | None,
) -> None:
    st.subheader("ジャンルから探す")
    with st.popover("ジャンル分布について", icon=":material/info:"):
        st.write("曲数の多い順に表示しています。棒の右側の数値は曲数です。人手で修正したジャンルを優先します。")
        st.write(f"選曲に使う分類: {CLASSIFICATION_SOURCE_LABELS[ctx.classification_source]}")
        st.write("「不明」はジャンルが決まっていない曲です。ジャンルを指定した選曲の対象には含めません。")

    # 名前は棒の上に置き、狭い画面でも長いジャンル名が棒の幅を奪わないようにする。
    distribution = sorted(stats.genre_distribution.items(), key=lambda item: (-item[1], item[0]))
    base = alt.Chart(alt.InlineData(values=[{"ジャンル": name, "曲数": count} for name, count in distribution])).encode(
        y=alt.Y("ジャンル:N", sort=[name for name, _ in distribution], axis=None),
        x=alt.X("曲数:Q", axis=None, scale=alt.Scale(domain=[0, max(stats.genre_distribution.values()) * 1.15])),
        tooltip=[alt.Tooltip("ジャンル:N"), alt.Tooltip("曲数:Q", format=",")],
    )
    bars = base.mark_bar(size=8, cornerRadiusEnd=3).encode(
        color=alt.condition(alt.datum["ジャンル"] == UNKNOWN_GENRE_LABEL, alt.value("#b6bbc5"), alt.value("#677786")),
    )
    names = base.mark_text(align="left", dy=-15, fontSize=12, color="#262730").encode(
        x=alt.value(0),
        text="ジャンル:N",
    )
    counts = base.mark_text(align="left", dx=6, fontSize=12, color="#262730").encode(text="曲数:Q")
    st.altair_chart(
        (bars + names + counts).properties(height=alt.Step(44)).configure_view(stroke=None), width="stretch"
    )
    choices = [genre for name, _ in distribution for genre, label in GENRE_LABELS.items() if label == name]
    if not choices:
        st.caption("ジャンルはすべて不明です。音楽ライブラリで分類結果を確認できます。")
        return

    genre = st.selectbox(
        "選曲に使うジャンル",
        choices,
        format_func=GENRE_LABELS.__getitem__,
        filter_mode=None,
        key="dashboard-genre",
    )
    st.button(
        "このジャンルで選曲へ",
        on_click=on_playlist,
        args=(genre,),
        disabled=on_playlist is None or genre is None,
        width="stretch",
    )
    st.caption("作成画面でジャンル条件に反映できます。曲の生成は、その画面で行います。")


def _maintenance(
    stats: DashboardStats,
    on_library: Callable[[], None] | None,
    on_evaluation: Callable[[], None] | None,
) -> None:
    with st.container(border=True):
        st.subheader("ライブラリを整える")
        if stats.unanalyzed:
            st.write(f"未解析 {stats.unanalyzed:,} 曲 — 音響解析でテンポや曲調を選曲に使えます。")

        if stats.failed:
            st.warning(f"解析に失敗した曲が {stats.failed:,} 曲あります。音楽ライブラリで再試行できます。")

        if stats.unanalyzed or stats.failed:
            st.button("解析状況を確認", on_click=on_library, disabled=on_library is None)

        if stats.pending_evaluations:
            st.write(f"評価対象のうち未評価 {stats.pending_evaluations:,} 曲 — 判定が合っているか確認しましょう。")
        else:
            st.caption("判定を確かめたい曲を評価対象に追加すると、選曲の改善に役立てられます。")

        st.button("精度評価を開く", on_click=on_evaluation, disabled=on_evaluation is None)

    with st.expander("解析・判定の詳細"):
        st.caption("音響解析の件数です。選曲に使える曲数とは異なります。")
        st.write(f"Jev 判定済み: {stats.jev_classified:,} 曲")
        st.write(f"解析エラー: {stats.error_count:,} 件（未解決のエラーを曲ごとに最新の 1 件で集計）")
        st.write(f"解析未対応: {stats.unsupported:,} 曲（M4A/AAC はタグのみ取得）")
        st.caption(f"最終スキャン: {stats.last_scan_at or '未実施'}")
