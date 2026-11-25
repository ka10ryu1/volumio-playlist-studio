"""Streamlit アプリのエントリポイント。

通常は start.cmd（Windows）または `uv run python -m volumio_playlist_studio.launcher` から起動する。
"""

import streamlit as st

from volumio_playlist_studio.config import ISSUES_URL
from volumio_playlist_studio.gui.context import open_context, show_job_message, show_jobs
from volumio_playlist_studio.gui.lan import render_lan_page, require_pin, resolve_lan
from volumio_playlist_studio.gui.views import (
    dashboard,
    evaluation_page,
    library,
    manage_page,
    playlist_page,
    release_notes_page,
    settings_page,
)
from volumio_playlist_studio.gui.widgets import volumio_link_button
from volumio_playlist_studio.models import Genre


def main() -> None:
    """画面を組み立てて表示する。"""
    # 右上のメニューにも問い合わせ先を出す。PIN の入力前でも、ライブラリの内容を含まないため表示してよい
    st.set_page_config(
        page_title="Volumio Playlist Studio",
        page_icon="🎵",
        layout="centered",
        menu_items={"Get help": ISSUES_URL, "Report a bug": ISSUES_URL},
    )
    ctx = open_context()
    try:
        lan = resolve_lan(ctx.config)
        # PC 自身からの接続なら None。LAN の端末からの接続では、未認証なら PIN 入力画面だけを表示する
        remote_ip = st.context.ip_address
        if lan is not None and not require_pin(lan, remote_ip):
            return

        playlist_page.preserve_inputs()

        def open_playlist(genre: Genre | None) -> None:
            playlist_page.suggest_genre(genre)
            st.switch_page(pages[2])

        def open_manage(playlist_id: int | None) -> None:
            manage_page.select_playlist(playlist_id)
            st.switch_page(pages[3])

        pages = [
            st.Page(
                lambda: dashboard.render(
                    ctx,
                    on_playlist=open_playlist,
                    on_library=lambda: st.switch_page(pages[1]),
                    on_manage=open_manage,
                    on_evaluation=lambda: st.switch_page(pages[4]),
                ),
                title="ダッシュボード",
                icon="📊",
                url_path="dashboard",
                default=True,
            ),
            st.Page(lambda: library.render(ctx), title="音楽ライブラリ", icon="🎼", url_path="library"),
            st.Page(
                lambda: playlist_page.render(ctx, on_manage=lambda: st.switch_page(pages[3])),
                title="プレイリスト作成",
                icon="🎧",
                url_path="playlist",
            ),
            st.Page(lambda: manage_page.render(ctx), title="プレイリスト管理", icon="🗂️", url_path="manage"),
            st.Page(lambda: evaluation_page.render(ctx), title="精度評価", icon="✅", url_path="evaluation"),
            st.Page(lambda: settings_page.render(ctx), title="設定", icon="⚙️", url_path="settings"),
            st.Page(
                lambda: release_notes_page.render(ctx), title="リリースノート", icon="📰", url_path="release-notes"
            ),
        ]
        if lan is not None and remote_ip is None:
            port = ctx.config.port
            pages.append(st.Page(lambda: render_lan_page(lan, port), title="スマホで開く", icon="📱", url_path="lan"))

        navigation = st.navigation(pages)
        with st.sidebar:
            st.link_button("問い合わせ（GitHub）", ISSUES_URL, icon=":material/help:", width="stretch")
            volumio_link_button(ctx.volumio_host)

        with st.container(key="job-notifications"):
            show_job_message()
            show_jobs()

        # 通知の出現・消失で本文の要素の位置（識別子）がずれないようにする。
        with st.container(key="page-content"):
            navigation.run()
    finally:
        ctx.conn.close()


main()
