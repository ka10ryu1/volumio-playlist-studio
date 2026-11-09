"""設定画面。音楽フォルダ・Volumio 接続先・Jev・解析パラメータ・分類エンジンの設定と、ログの確認を行う。"""

from collections import deque
from dataclasses import replace

import streamlit as st

from volumio_playlist_studio.config import (
    TYPESAFE_API_KEY_ENV,
    VOLUMIO_HOST_ENV,
    AnalysisParams,
    is_typesafe_api_key_set,
)
from volumio_playlist_studio.gui.context import PageContext
from volumio_playlist_studio.gui.widgets import music_folder_form
from volumio_playlist_studio.models import CLASSIFICATION_SOURCE_LABELS, ClassificationSource
from volumio_playlist_studio.storage import analysis as analysis_store
from volumio_playlist_studio.storage import settings_store
from volumio_playlist_studio.storage.database import transaction
from volumio_playlist_studio.volumio import VolumioError
from volumio_playlist_studio.volumio_rest import VolumioRestClient, parse_host

_LOG_TAIL_LINES = 200
_VOLUMIO_SAVED_KEY = "volumio_host_saved"
_SAMPLE_RATES = [16000, 22050, 32000, 44100]


def render(ctx: PageContext) -> None:
    """設定画面を表示する。"""
    st.title("設定")

    general, advanced, logs = st.tabs(["一般", "解析・AI", "ログ"])
    with general:
        st.subheader("音楽フォルダ")
        st.caption(f"現在: {'サンプルデータ' if ctx.is_demo else ctx.library_root or '未設定'}")
        music_folder_form(ctx)
        _volumio_section(ctx)
        _engine_section(ctx)

    with advanced:
        st.caption("解析・判定の調整用設定です。変更を反映するには各フォームで保存してください。")
        with st.expander("Jev API の設定"):
            _jev_section(ctx)

        with st.expander("高度な音響解析設定"):
            _analysis_section(ctx)

    with logs:
        _log_section(ctx)


def _volumio_section(ctx: PageContext) -> None:
    st.subheader("Volumio 接続先")
    saved = settings_store.get_str(ctx.conn, settings_store.VOLUMIO_HOST_KEY)
    current = ctx.volumio_host
    st.caption(f"現在: {current or '未設定'}（環境変数 {VOLUMIO_HOST_ENV} が設定されていればそちらを優先）")
    st.info(
        "Volumio からは、プレイリスト一覧と曲の一覧を読み取るだけです（Volumio は変更しません）。"
        "Volumio へのプレイリストの登録は未実装で、モックでのみ確認できます。"
    )
    with st.form("volumio-host"):
        host = st.text_input("ホスト名または IP アドレス", value=saved or "", placeholder="volumio.local")
        if st.form_submit_button("保存"):
            _save_volumio_host(ctx, host.strip())

    if st.session_state.pop(_VOLUMIO_SAVED_KEY, False):
        st.success("保存しました。")

    if current is not None and st.button("接続テスト（プレイリスト一覧を読み取る）"):
        _test_volumio_connection(current)


def _save_volumio_host(ctx: PageContext, host: str) -> None:
    if host:
        try:
            parse_host(host)
        except ValueError:
            st.error("ホスト名または IP アドレスの形式が正しくありません（例: volumio.local、192.168.1.22）。")
            return

    with transaction(ctx.conn):
        settings_store.set_value(ctx.conn, settings_store.VOLUMIO_HOST_KEY, host)

    # 保存した接続先で「現在」の表示と接続テストのボタンを出し直すため、画面を描き直す
    st.session_state[_VOLUMIO_SAVED_KEY] = True
    st.rerun()


def _test_volumio_connection(host: str) -> None:
    try:
        # 1 回のリクエストで終わるため、短いタイムアウトで画面の処理として実行する
        with st.spinner("Volumio に接続しています…"):
            playlists = VolumioRestClient(host, timeout=5.0).list_playlists()
    except VolumioError, ValueError:
        st.error(
            "Volumio に接続できませんでした。接続先と、Volumio が同じネットワークで起動しているかを確認してください。"
        )
        return

    names = "、".join(playlists) if playlists else "（なし）"
    st.success(f"接続できました。既存のプレイリスト {len(playlists)} 件: {names}")


def _jev_section(ctx: PageContext) -> None:
    st.subheader("Jev API")
    if is_typesafe_api_key_set():
        st.success(f"API キー: 設定済み（環境変数 {TYPESAFE_API_KEY_ENV}）")
    else:
        st.warning(f"API キー: 未設定。環境変数 {TYPESAFE_API_KEY_ENV} または .env に設定してください。")

    st.caption("API キーの値は画面にもログにも表示しません。")

    settings = settings_store.get_jev_settings(ctx.conn)
    with st.form("jev-settings"):
        model = st.text_input("モデル", value=settings.model)
        min_confidence = st.slider(
            "この confidence 未満の回答は判定保留にする", 0.0, 1.0, settings.min_confidence, step=0.05
        )
        determinable = st.slider(
            "「ジャンルを判断できる」の確率がこれ未満ならジャンル不明にする",
            0.0,
            1.0,
            settings.genre_determinable_threshold,
            step=0.05,
        )
        send_titles = st.checkbox("曲名を送信する", value=settings.send_titles)
        max_tracks = st.number_input("1 回の実行で判定する曲数の上限", 1, 500, settings.max_tracks_per_run)
        if st.form_submit_button("保存"):
            updated = replace(
                settings,
                model=model.strip() or settings.model,
                min_confidence=min_confidence,
                genre_determinable_threshold=determinable,
                send_titles=send_titles,
                max_tracks_per_run=int(max_tracks),
            )
            with transaction(ctx.conn):
                settings_store.set_jev_settings(ctx.conn, updated)

            st.success("保存しました。判定基準の変更は、次に Jev で判定した曲から反映されます。")


def _analysis_section(ctx: PageContext) -> None:
    st.subheader("解析パラメータ")
    params = settings_store.get_analysis_params(ctx.conn)
    st.caption(
        "曲の先頭・末尾を除いた範囲から、等間隔に区間を切り出して解析します。区間を増やすと精度は上がりますが時間がかかります。"
    )
    with st.form("analysis-params"):
        segment_count = st.number_input("解析する区間の数", 1, 10, params.segment_count)
        segment_seconds = st.number_input("1 区間の長さ（秒）", 5.0, 120.0, params.segment_seconds, step=5.0)
        edge_margin = st.slider("先頭・末尾から除外する割合", 0.0, 0.45, params.edge_margin_ratio, step=0.05)
        sample_rate = st.selectbox(
            "サンプリング周波数（Hz）",
            _SAMPLE_RATES,
            index=_SAMPLE_RATES.index(params.sample_rate) if params.sample_rate in _SAMPLE_RATES else 1,
        )
        silence = st.number_input("無音とみなす音量（dBFS）", -120.0, -1.0, params.silence_threshold_db, step=5.0)
        if st.form_submit_button("保存"):
            updated = AnalysisParams(
                sample_rate=int(sample_rate),
                segment_count=int(segment_count),
                segment_seconds=float(segment_seconds),
                edge_margin_ratio=float(edge_margin),
                silence_threshold_db=float(silence),
            )
            try:
                with transaction(ctx.conn):
                    settings_store.set_analysis_params(ctx.conn, updated)
            except ValueError:
                st.error("範囲外の値があります。")
            else:
                st.success("保存しました。保存済みの解析結果は、ファイルが更新されない限り再解析されません。")


def _engine_section(ctx: PageContext) -> None:
    st.subheader("分類エンジン")
    st.caption("プレイリスト生成に使う分類結果を選びます。Essentia などは未導入です（docs/architecture.md 参照）。")
    sources = list(ClassificationSource)
    with st.form("engine"):
        source = st.radio(
            "選曲に使う分類結果",
            sources,
            index=sources.index(ctx.classification_source),
            format_func=CLASSIFICATION_SOURCE_LABELS.__getitem__,
        )
        if st.form_submit_button("保存"):
            with transaction(ctx.conn):
                settings_store.set_value(ctx.conn, settings_store.CLASSIFICATION_SOURCE_KEY, source.value)

            st.success("保存しました。")
            st.rerun()


def _log_section(ctx: PageContext) -> None:
    st.subheader("ログ・エラー")
    if ctx.library_root is not None:
        errors = analysis_store.list_errors(ctx.conn, ctx.library_root, limit=50)
        st.caption(f"未解決の解析エラー: {len(errors)} 件（詳細は音楽ライブラリ画面）")

    with st.expander("アプリのログ（最新 200 行）"):
        path = ctx.config.log_path
        if not path.exists():
            st.caption("ログはまだありません。")
            return

        with path.open(encoding="utf-8", errors="replace") as f:
            st.code("".join(deque(f, maxlen=_LOG_TAIL_LINES)), language="text")
