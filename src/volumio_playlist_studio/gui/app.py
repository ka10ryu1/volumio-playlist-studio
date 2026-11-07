"""Streamlit アプリのエントリポイント。

通常は start.cmd（Windows）または `uv run python -m volumio_playlist_studio.launcher` から起動する。
"""

import streamlit as st

from volumio_playlist_studio.gui.context import open_context, show_job_message, show_jobs
from volumio_playlist_studio.gui.lan import render_lan_page, require_pin, resolve_lan
from volumio_playlist_studio.gui.views import dashboard, evaluation_page, library, playlist_page, settings_page


def main() -> None:
    """画面を組み立てて表示する。"""
    st.set_page_config(page_title="Volumio Playlist Studio", page_icon="🎵", layout="centered")
    ctx = open_context()
    try:
        lan = resolve_lan(ctx.config)
        # PC 自身からの接続なら None。LAN の端末からの接続では、未認証なら PIN 入力画面だけを表示する
        remote_ip = st.context.ip_address
        if lan is not None and not require_pin(lan, remote_ip):
            return

        pages = [
            st.Page(
                lambda: dashboard.render(ctx), title="ダッシュボード", icon="📊", url_path="dashboard", default=True
            ),
            st.Page(lambda: library.render(ctx), title="音楽ライブラリ", icon="🎼", url_path="library"),
            st.Page(lambda: playlist_page.render(ctx), title="プレイリスト作成", icon="🎧", url_path="playlist"),
            st.Page(lambda: evaluation_page.render(ctx), title="精度評価", icon="✅", url_path="evaluation"),
            st.Page(lambda: settings_page.render(ctx), title="設定", icon="⚙️", url_path="settings"),
        ]
        if lan is not None and remote_ip is None:
            port = ctx.config.port
            pages.append(st.Page(lambda: render_lan_page(lan, port), title="スマホで開く", icon="📱", url_path="lan"))

        navigation = st.navigation(pages)
        show_job_message()
        show_jobs()
        navigation.run()
    finally:
        ctx.conn.close()


main()
