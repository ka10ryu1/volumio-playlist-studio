"""精度評価画面。判定結果を人手で評価・修正し、分類エンジンごとの指標を比較する。"""

import streamlit as st

from volumio_playlist_studio.classify.jev import ENGINE_NAME as JEV_ENGINE
from volumio_playlist_studio.classify.rules import ENGINE_NAME as RULES_ENGINE
from volumio_playlist_studio.config import RATING_MAX, RATING_MIN, EvaluationDefaults
from volumio_playlist_studio.evaluation import (
    EngineMetrics,
    compute_engine_metrics,
    export_csv,
    playlist_feedback_metrics,
    select_evaluation_sample,
)
from volumio_playlist_studio.gui.context import PageContext
from volumio_playlist_studio.gui.widgets import classification_details, genre_label, mood_label, track_label
from volumio_playlist_studio.models import ATTRIBUTE_LABELS, Attribute, Genre, Mood, Track
from volumio_playlist_studio.storage import analysis as analysis_store
from volumio_playlist_studio.storage.analysis import StoredClassification
from volumio_playlist_studio.storage.database import transaction
from volumio_playlist_studio.storage.evaluations import (
    EvaluationLabel,
    Split,
    Verdict,
    add_to_evaluation_set,
    load_labels,
    save_label,
)
from volumio_playlist_studio.storage.playlists import list_playlists, set_feedback
from volumio_playlist_studio.storage.tracks import get_track, list_tracks

_ENGINE_LABELS = {RULES_ENGINE: "ルールベース（Baseline）", JEV_ENGINE: "Jev"}
_VERDICT_LABELS = {
    Verdict.CORRECT: "正しい",
    Verdict.INCORRECT: "誤り",
    Verdict.PENDING: "判定保留（人でも判断できない）",
}
_SPLIT_LABELS = {Split.TUNING: "調整用", Split.HOLDOUT: "検証用"}
_MOODS = [m for m in Mood if m != Mood.OMAKASE]


def render(ctx: PageContext) -> None:
    """精度評価画面を表示する。"""
    st.title("精度評価")
    if ctx.library_root is None:
        st.info("音楽フォルダが未設定です。")
        return

    tracks = {t.id: t for t in list_tracks(ctx.conn, ctx.library_root)}
    labels = {k: v for k, v in load_labels(ctx.conn).items() if k in tracks}
    _sample_section(ctx, list(tracks.values()), labels)
    if labels:
        _labeling_section(ctx, tracks, labels)
        _metrics_section(ctx, labels)
        _export_section(ctx, tracks, labels)

    _playlist_feedback_section(ctx)


def _sample_section(ctx: PageContext, tracks: list[Track], labels: dict[int, EvaluationLabel]) -> None:
    defaults = EvaluationDefaults()
    evaluated = sum(1 for lb in labels.values() if lb.is_evaluated)
    st.write(f"評価対象: {len(labels)} 曲（評価済み {evaluated} 曲）")
    st.caption(
        "評価対象は「調整用」と「検証用」に半分ずつ分けます。ルールを調整するときは調整用だけを参照し、"
        "改善の確認は検証用の指標で行ってください（評価データへの過学習を防ぐため）。"
    )
    with st.expander("評価対象を追加する", expanded=not labels):
        size = st.slider(
            "追加する曲数",
            defaults.min_sample_size,
            defaults.max_sample_size,
            defaults.default_sample_size,
        )
        seed = st.number_input("seed", min_value=0, value=0, key="eval-seed")
        if st.button("無作為に選んで追加"):
            assignments = select_evaluation_sample(tracks, set(labels), size, int(seed), defaults)
            with transaction(ctx.conn):
                add_to_evaluation_set(ctx.conn, assignments)

            st.success(f"{len(assignments)} 曲を評価対象に追加しました。")
            st.rerun()


def _labeling_section(ctx: PageContext, tracks: dict[int, Track], labels: dict[int, EvaluationLabel]) -> None:
    st.subheader("判定結果を評価する")
    engine = st.radio("評価する判定結果", list(_ENGINE_LABELS), format_func=_ENGINE_LABELS.__getitem__, horizontal=True)
    classifications = analysis_store.load_classifications(ctx.conn, engine)

    only_pending = st.checkbox("未評価の曲だけ表示", value=True)
    ids = [i for i in labels if not (only_pending and labels[i].is_evaluated)]
    if not ids:
        st.caption("表示する曲がありません。")
        return

    track_id = st.selectbox(
        "曲",
        ids,
        format_func=lambda i: (
            f"{'✓ ' if labels[i].is_evaluated else ''}{track_label(tracks[i])}（{_SPLIT_LABELS[labels[i].split]}）"
        ),
    )
    if track_id is None:
        return

    stored = classifications.get(track_id)
    with st.container(border=True):
        st.markdown(f"**{track_label(tracks[track_id])}**")
        st.caption(
            f"アルバム: {tracks[track_id].album or '（不明）'}　ジャンルタグ: {tracks[track_id].genre or '（なし）'}"
        )
        classification_details(None if stored is None else stored.result)

    _label_form(ctx, labels[track_id], stored)


def _label_form(ctx: PageContext, label: EvaluationLabel, stored: StoredClassification | None) -> None:
    predicted_genre = None if stored is None else stored.result.genre
    predicted_mood = None if stored is None else stored.result.mood
    with st.form(f"label-{label.track_id}"):
        st.markdown(f"判定ジャンル: **{genre_label(predicted_genre)}**")
        genre_verdict = st.radio(
            "ジャンルの判定は", list(Verdict), format_func=_VERDICT_LABELS.__getitem__, horizontal=True
        )
        corrected_genre = st.selectbox("誤りの場合、正しいジャンル", list(Genre), format_func=genre_label, index=None)
        st.markdown(f"判定ムード: **{mood_label(predicted_mood)}**")
        mood_verdict = st.radio(
            "ムードの判定は", list(Verdict), format_func=_VERDICT_LABELS.__getitem__, horizontal=True
        )
        corrected_mood = st.selectbox("誤りの場合、正しいムード", _MOODS, format_func=mood_label, index=None)

        st.markdown("属性の評価（評価しない属性は「-」のまま）")
        ratings: dict[Attribute, int] = {}
        for attribute in Attribute:
            value = st.select_slider(
                ATTRIBUTE_LABELS[attribute],
                options=["-", *range(RATING_MIN, RATING_MAX + 1)],
                value=label.ratings.get(attribute, "-"),
            )
            if isinstance(value, int):
                ratings[attribute] = value

        note = st.text_input("メモ", value=label.note or "")
        submitted = st.form_submit_button("評価を保存", type="primary")

    if not submitted:
        return

    if genre_verdict is None or mood_verdict is None:
        st.error("ジャンルとムードの判定を選んでください。")
        return

    genre_value = _resolve_label(genre_verdict, predicted_genre, corrected_genre)
    mood_value = _resolve_label(mood_verdict, predicted_mood, corrected_mood)
    if genre_value is False or mood_value is False:
        st.error("「正しい」は判定結果がある場合だけ、「誤り」は正しい値を選んだ場合だけ保存できます。")
        return

    with transaction(ctx.conn):
        save_label(
            ctx.conn,
            EvaluationLabel(
                track_id=label.track_id,
                split=label.split,
                classification_id=None if stored is None else stored.id,
                genre_verdict=genre_verdict,
                genre_label=genre_value if isinstance(genre_value, Genre) else None,
                mood_verdict=mood_verdict,
                mood_label=mood_value if isinstance(mood_value, Mood) else None,
                ratings=ratings,
                note=note.strip() or None,
            ),
        )

    st.success("評価を保存しました。")
    st.rerun()


def _resolve_label[T: (Genre, Mood)](verdict: Verdict, predicted: T | None, corrected: T | None) -> T | bool | None:
    """人手評価から正解の値を決める。入力が矛盾する場合は False を返す。"""
    if verdict == Verdict.CORRECT:
        return predicted if predicted is not None else False

    if verdict == Verdict.INCORRECT:
        return corrected if corrected is not None and corrected != predicted else False

    return None


def _metrics_section(ctx: PageContext, labels: dict[int, EvaluationLabel]) -> None:
    st.subheader("指標")
    split_choice = st.radio(
        "対象",
        [Split.HOLDOUT, Split.TUNING, None],
        format_func=lambda s: "全件" if s is None else _SPLIT_LABELS[s],
        horizontal=True,
    )
    metrics = [
        compute_engine_metrics(
            _ENGINE_LABELS[engine],
            labels,
            {k: v.result for k, v in analysis_store.load_classifications(ctx.conn, engine).items()},
            split_choice,
        )
        for engine in _ENGINE_LABELS
    ]
    st.dataframe([_metrics_row(m) for m in metrics], hide_index=True, width="stretch")
    st.caption(
        "一致率は分類エンジンが判定した曲のうち正解した割合、保留率は正解がある曲のうち判定保留にした割合です。"
        "誤差は人手の 5 段階評価を 0〜1 に換算したときの平均絶対誤差です。確信度は正解率ではありません。"
    )
    for m in metrics:
        confusions = m.genre.confusions + m.mood.confusions
        if confusions:
            st.markdown(f"**{m.engine} の誤判定の傾向**")
            st.dataframe(
                [{"正解": t, "判定": p, "件数": n} for t, p, n in confusions], hide_index=True, width="stretch"
            )


def _ratio(value: float | None) -> str:
    return "-" if value is None else f"{value:.0%}"


def _metrics_row(m: EngineMetrics) -> dict[str, object]:
    row: dict[str, object] = {
        "分類エンジン": m.engine,
        "評価曲数": m.evaluated,
        "ジャンル一致率": _ratio(m.genre.accuracy),
        "ジャンル保留率": _ratio(m.genre.pending_rate),
        "ムード一致率": _ratio(m.mood.accuracy),
        "ムード保留率": _ratio(m.mood.pending_rate),
    }
    for attribute in Attribute:
        mae = m.attribute_mae.get(attribute)
        row[f"{ATTRIBUTE_LABELS[attribute]} 誤差"] = "-" if mae is None else f"{mae[0]:.2f} (n={mae[1]})"

    return row


def _export_section(ctx: PageContext, tracks: dict[int, Track], labels: dict[int, EvaluationLabel]) -> None:
    classifications = {
        engine: {k: v.result for k, v in analysis_store.load_classifications(ctx.conn, engine).items()}
        for engine in _ENGINE_LABELS
    }
    st.download_button(
        "評価結果を CSV で保存",
        data=export_csv(tracks, labels, classifications).encode("utf-8-sig"),
        file_name="evaluation.csv",
        mime="text/csv",
    )


def _optional_track_label(track: Track | None) -> str:
    return "（不明）" if track is None else track_label(track)


def _playlist_feedback_section(ctx: PageContext) -> None:
    st.subheader("プレイリストの評価")
    playlists = list_playlists(ctx.conn)
    if not playlists:
        st.caption("保存したプレイリストがありません。")
        return

    playlist = st.selectbox("プレイリスト", playlists, format_func=lambda p: f"{p.name}（{p.created_at}）")
    if playlist is None:
        return

    tracks = [get_track(ctx.conn, e.track_id) for e in playlist.entries]
    options = list(range(len(tracks)))
    with st.form(f"feedback-{playlist.id}"):
        satisfaction = st.slider("満足度（1: 不満 〜 5: 満足）", RATING_MIN, RATING_MAX, playlist.satisfaction or 3)
        unsuitable = st.multiselect(
            "雰囲気に合わなかった曲",
            options,
            default=[i for i, e in enumerate(playlist.entries) if e.unsuitable],
            format_func=lambda i: f"{i + 1}. {_optional_track_label(tracks[i])}",
        )
        if st.form_submit_button("評価を保存"):
            with transaction(ctx.conn):
                set_feedback(ctx.conn, playlist.id, satisfaction, set(unsuitable))

            st.success("プレイリストの評価を保存しました。")
            st.rerun()

    feedback = playlist_feedback_metrics(playlists)
    if feedback:
        st.dataframe(
            [
                {
                    "分類結果": f.source,
                    "プレイリスト数": f.playlists,
                    "不適切な曲の割合": _ratio(f.unsuitable_rate),
                    "平均満足度": "-" if f.mean_satisfaction is None else f"{f.mean_satisfaction:.1f}",
                }
                for f in feedback
            ],
            hide_index=True,
            width="stretch",
        )
