"""Streamlit アプリのエントリポイント。`uv run streamlit run src/volumio_playlist_studio/gui/app.py` で起動する。"""

import streamlit as st

from volumio_playlist_studio.gui.context import open_context, show_job_message, show_jobs
from volumio_playlist_studio.gui.views import dashboard, evaluation_page, library, playlist_page, settings_page


def main() -> None:
    """画面を組み立てて表示する。"""
    st.set_page_config(page_title="Volumio Playlist Studio", page_icon="🎵", layout="centered")
    ctx = open_context()
    try:
        pages = [
            st.Page(
                lambda: dashboard.render(ctx), title="ダッシュボード", icon="📊", url_path="dashboard", default=True
            ),
            st.Page(lambda: library.render(ctx), title="音楽ライブラリ", icon="🎼", url_path="library"),
            st.Page(lambda: playlist_page.render(ctx), title="プレイリスト作成", icon="🎧", url_path="playlist"),
            st.Page(lambda: evaluation_page.render(ctx), title="精度評価", icon="✅", url_path="evaluation"),
            st.Page(lambda: settings_page.render(ctx), title="設定", icon="⚙️", url_path="settings"),
        ]
        navigation = st.navigation(pages)
        show_job_message()
        show_jobs()
        navigation.run()
    finally:
        ctx.conn.close()


main()
