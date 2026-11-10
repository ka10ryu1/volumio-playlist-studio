"""音楽ライブラリ画面。スキャン・解析・Jev 判定の実行と、楽曲の検索・詳細表示を行う。"""

from pathlib import Path

import streamlit as st

from volumio_playlist_studio.analyzer import is_analyzable
from volumio_playlist_studio.catalog import effective_classifications
from volumio_playlist_studio.classify.jev import ENGINE_NAME as JEV_ENGINE
from volumio_playlist_studio.classify.rules import ENGINE_NAME as RULES_ENGINE
from volumio_playlist_studio.config import ANALYSIS_VERSION, is_typesafe_api_key_set
from volumio_playlist_studio.gui.context import PageContext, running_job, start_job
from volumio_playlist_studio.gui.widgets import (
    classification_details,
    format_duration,
    genre_label,
    mood_label,
    music_folder_form,
    track_label,
)
from volumio_playlist_studio.models import ClassificationResult, Track
from volumio_playlist_studio.storage import analysis as analysis_store
from volumio_playlist_studio.storage.tracks import duplicate_groups, list_tracks
from volumio_playlist_studio.workflows import estimate_jev, run_analysis, run_jev, run_scan


def render(ctx: PageContext) -> None:
    """音楽ライブラリ画面を表示する。"""
    st.title("音楽ライブラリ")
    with st.expander("音楽フォルダ", expanded=ctx.library_root is None):
        music_folder_form(ctx)

    if ctx.library_root is None:
        return

    with st.expander("スキャン・解析", expanded=not ctx.is_demo):
        _actions(ctx, ctx.library_root)

    tracks = list_tracks(ctx.conn, ctx.library_root)
    _track_table(ctx, tracks)
    _errors(ctx, ctx.library_root)
    _duplicates(ctx, ctx.library_root, {t.id: t for t in tracks})


def _actions(ctx: PageContext, library_root: str) -> None:
    busy = running_job() is not None
    if ctx.is_demo:
        st.caption("サンプルデータではスキャン・解析は行えません。音楽フォルダを指定すると実行できます。")
        return

    root = Path(library_root)
    cols = st.columns(2)
    if cols[0].button("スキャン（追加・更新を反映）", disabled=busy, width="stretch"):
        start_job("スキャン", ctx.config.database_path, lambda conn, control: run_scan(conn, root, control))
        st.rerun()

    if cols[1].button("未解析の曲を解析", disabled=busy, width="stretch"):
        start_job("音響解析", ctx.config.database_path, lambda conn, control: run_analysis(conn, root, control))
        st.rerun()

    failed = analysis_store.failed_track_ids(ctx.conn, library_root, ANALYSIS_VERSION)
    if failed and st.button(f"失敗した曲を再試行（{len(failed)} 曲）", disabled=busy):
        start_job(
            "音響解析（再試行）",
            ctx.config.database_path,
            lambda conn, control: run_analysis(conn, root, control, retry_failed=True),
        )
        st.rerun()

    with st.expander("Jev で判定する（API を使用）"):
        _jev_panel(ctx, library_root, busy)


def _jev_panel(ctx: PageContext, library_root: str, busy: bool) -> None:
    if not is_typesafe_api_key_set():
        st.info("TYPESAFE_API_KEY が未設定のため、Jev は利用できません。ルールベースの分類で選曲できます。")
        return

    estimate = estimate_jev(ctx.conn, library_root)
    st.write(
        f"未判定の曲: **{estimate.pending_tracks} 曲**　今回判定する曲: **{estimate.tracks_this_run} 曲**"
        f"（1 回の上限は設定画面で変更できます）"
    )
    st.write(
        f"送信するリクエスト: **{estimate.requests_this_run} 件**"
        f"（1 件あたり {estimate.questions_per_request} 問。通信エラー時は SDK が自動で再試行します）"
    )
    st.caption(
        "送信するのはタグ情報と音響特徴量の区分だけで、音声ファイルやファイルパスは送りません。"
        "料金は TypeSafe の料金体系に従います。"
    )
    confirmed = st.checkbox("上記の件数で API を呼び出すことを確認しました")
    if st.button("Jev で判定", disabled=busy or not confirmed or estimate.tracks_this_run == 0):
        start_job("Jev 判定", ctx.config.database_path, lambda conn, control: run_jev(conn, library_root, control))
        st.rerun()


def _matches(track: Track, query: str) -> bool:
    fields = (track.title, track.artist, track.album, track.genre, track.rel_path)
    return any(query in f.casefold() for f in fields if f)


def _track_table(ctx: PageContext, tracks: list[Track]) -> None:
    st.subheader(f"楽曲一覧（{len(tracks)} 曲）")
    query = st.text_input("検索（曲名・アーティスト・アルバム・ジャンル）").strip().casefold()
    shown = [t for t in tracks if _matches(t, query)] if query else tracks

    classifications = effective_classifications(ctx.conn, ctx.classification_source)
    library_root = ctx.library_root or ""
    pending = {t.id for t in analysis_store.tracks_needing_analysis(ctx.conn, library_root, ANALYSIS_VERSION)}
    failed = analysis_store.failed_track_ids(ctx.conn, library_root, ANALYSIS_VERSION)
    with st.expander("絞り込み・表示項目"):
        # 選択欄は一覧から選ぶだけなので文字入力を受け付けず、スマートフォンでキーボードを出さない。
        # 曲名などでの絞り込みは上の検索欄で行う
        state = st.selectbox("解析状況", ["すべて", "解析済み", "未解析", "解析失敗", "未対応形式"], filter_mode=None)
        genre = st.selectbox(
            "判定ジャンルで絞り込み",
            ["すべて", *sorted({genre_label(c.genre) for c in classifications.values()}), "未分類"],
            filter_mode=None,
        )
        detailed = st.checkbox("アルバム・タグ・ムードも一覧に表示")

    shown = [
        t
        for t in shown
        if (state == "すべて" or _analysis_state(t, pending, failed) == state)
        and (
            genre == "すべて"
            or genre == (genre_label(classifications[t.id].genre) if t.id in classifications else "未分類")
        )
    ]
    st.caption(f"{len(shown)} / {len(tracks)} 曲を表示")
    rows = []
    for track in shown:
        c = classifications.get(track.id)
        rows.append(_track_row(track, c, _analysis_state(track, pending, failed)))

    columns = ["曲名", "アーティスト", "時間", "解析", "判定ジャンル"]
    if detailed:
        columns += ["アルバム", "ジャンル（タグ）", "ムード"]

    st.dataframe(
        rows,
        hide_index=True,
        width="stretch",
        height=350,
        column_order=columns,
        column_config={
            "曲名": st.column_config.TextColumn(width="medium"),
            "アーティスト": st.column_config.TextColumn(width="medium"),
        },
    )

    if not shown:
        st.info("条件に合う曲がありません。検索語や絞り込みを変更してください。")
        return

    selected = st.selectbox("詳細を表示する曲", shown, format_func=track_label, filter_mode=None)
    if selected is not None:
        _track_detail(ctx, selected)


def _analysis_state(track: Track, pending: set[int], failed: set[int]) -> str:
    if not is_analyzable(track):
        return "未対応形式"

    if track.id in failed:
        return "解析失敗"

    return "未解析" if track.id in pending else "解析済み"


def _track_row(track: Track, classification: ClassificationResult | None, state: str) -> dict[str, str]:
    return {
        "曲名": track.display_title,
        "アーティスト": track.display_artist,
        "アルバム": track.album or "",
        "ジャンル（タグ）": track.genre or "",
        "判定ジャンル": "未分類" if classification is None else genre_label(classification.genre),
        "ムード": mood_label(None if classification is None else classification.mood),
        "時間": format_duration(track.duration_sec),
        "解析": state,
    }


def _track_detail(ctx: PageContext, track: Track) -> None:
    with st.container(border=True):
        st.markdown(f"**{track.display_title}**")
        st.caption(track.rel_path)
        st.write(
            f"アーティスト: {track.display_artist}　アルバム: {track.album or '（不明）'}　"
            f"年: {track.year or '-'}　トラック: {track.track_number or '-'}"
        )
        st.write(
            f"再生時間: {format_duration(track.duration_sec)}　ビットレート: "
            f"{'-' if track.bitrate is None else f'{track.bitrate // 1000} kbps'}　形式: {track.codec or '-'}"
        )
        if track.title_source is not None and track.title_source.value == "filename":
            st.caption("曲名はファイル名から補完しました（タグに曲名がありません）。")

        if track.tags_repaired:
            st.caption("文字化けしたタグを修復して表示しています。")

        if track.tag_error:
            st.warning(f"タグの読み取りエラー: {track.tag_error}")

        with st.expander("音響特徴量・判定の詳細"):
            features = analysis_store.load_features(ctx.conn, [track.id]).get(track.id)
            st.markdown("**音響特徴量**")
            if features is None:
                st.caption("未解析")
            else:
                st.dataframe(
                    [
                        {"特徴量": "推定 BPM", "値": round(features.tempo_bpm, 1)},
                        {"特徴量": "平均音量 (dBFS)", "値": round(features.rms_mean_db, 1)},
                        {"特徴量": "区間ごとの音量差 (dB)", "値": round(features.loudness_variation_db, 2)},
                        {"特徴量": "スペクトル重心 (Hz)", "値": round(features.spectral_centroid_mean_hz)},
                        {"特徴量": "スペクトルロールオフ (Hz)", "値": round(features.spectral_rolloff_mean_hz)},
                        {"特徴量": "ゼロ交差率", "値": round(features.zero_crossing_rate_mean, 4)},
                        {"特徴量": "発音の頻度 (回/秒)", "値": round(features.onset_rate_per_sec, 2)},
                        {"特徴量": "解析した長さ (秒)", "値": round(features.analyzed_seconds, 1)},
                    ],
                    hide_index=True,
                    width="stretch",
                )

            for engine, title in ((RULES_ENGINE, "ルールベース"), (JEV_ENGINE, "Jev")):
                stored = analysis_store.load_classifications(ctx.conn, engine).get(track.id)
                st.markdown(f"**判定結果: {title}**")
                classification_details(None if stored is None else stored.result)


def _errors(ctx: PageContext, library_root: str) -> None:
    errors = analysis_store.list_errors(ctx.conn, library_root)
    with st.expander(f"解析エラー（{len(errors)} 件）"):
        if not errors:
            st.caption("エラーはありません。")
            return

        st.dataframe(
            [
                {
                    "日時": e.occurred_at,
                    "段階": e.stage,
                    "ファイル": e.rel_path or "",
                    "種類": e.error_type,
                    "内容": e.message,
                }
                for e in errors
            ],
            hide_index=True,
            width="stretch",
        )


def _duplicates(ctx: PageContext, library_root: str, tracks: dict[int, Track]) -> None:
    groups = duplicate_groups(ctx.conn, library_root)
    with st.expander(f"重複の可能性があるファイル（{len(groups)} 組）"):
        if not groups:
            st.caption("内容が同じと推定されるファイルはありません。")

        for group in groups:
            st.write(" / ".join(tracks[i].rel_path for i in group if i in tracks))
