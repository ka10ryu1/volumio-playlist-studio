"""プレイリスト作成画面。条件を指定して生成し、編集・保存・Volumio 登録のプレビューを行う。"""

from dataclasses import replace
from datetime import datetime

import streamlit as st

from volumio_playlist_studio.catalog import load_candidates
from volumio_playlist_studio.config import PlaylistDefaults
from volumio_playlist_studio.gui.context import PageContext
from volumio_playlist_studio.gui.views import volumio_panel
from volumio_playlist_studio.gui.widgets import format_duration, genre_label, track_label
from volumio_playlist_studio.models import CLASSIFICATION_SOURCE_LABELS, MOOD_LABELS, Genre, Mood
from volumio_playlist_studio.playlist import (
    GENRE_MODE_LABELS,
    Candidate,
    GenerationResult,
    GenreMode,
    PlaylistRequest,
    SelectedTrack,
    add_item,
    generate_playlist,
    move_item,
    remove_item,
    total_duration,
)
from volumio_playlist_studio.storage.database import transaction
from volumio_playlist_studio.storage.playlists import PlaylistDraft, PlaylistEntry, save_playlist

_MOOD_CARDS: dict[Mood, tuple[str, str]] = {
    Mood.RELAX: ("🛋️", "ゆったりくつろぐ"),
    Mood.FOCUS: ("💻", "作業や勉強のお供に"),
    Mood.ENERGETIC: ("⚡", "気分を上げたいときに"),
    Mood.CALM_NIGHT: ("🌙", "静かな夜に"),
    Mood.OMAKASE: ("🎲", "雰囲気を問わず幅広く"),
}
_MOOD_KEY = "pl_mood"
_RESULT_KEY = "pl_result"
_DRAFT_KEY = "pl_draft"
_SAVED_KEY = "pl_saved_id"
_SEED_KEY = "pl_seed"
_MAX_SEED = 1_000_000
_SECONDS_PER_MINUTE = 60


def render(ctx: PageContext) -> None:
    """プレイリスト作成画面を表示する。"""
    st.title("プレイリスト作成")
    if ctx.library_root is None:
        st.info("音楽フォルダが未設定です。音楽ライブラリ画面で指定するか、サンプルデータで試してください。")
        return

    defaults = PlaylistDefaults()
    candidates = load_candidates(ctx.conn, ctx.library_root, ctx.classification_source)
    st.caption(f"選曲に使う分類結果: {CLASSIFICATION_SOURCE_LABELS[ctx.classification_source]}（設定画面で変更）")

    mood = _mood_cards()
    request = _conditions_form(mood, defaults)
    if request is not None:
        _generate(candidates, request, defaults)

    result: GenerationResult | None = st.session_state.get(_RESULT_KEY)
    if result is None:
        return

    _shortfall_notice(candidates, result, defaults)
    _draft_editor(ctx, candidates, result)
    volumio_panel.render(ctx, st.session_state.get(_SAVED_KEY))


def _mood_cards() -> Mood:
    st.subheader("1. 雰囲気を選ぶ")
    selected: Mood = st.session_state.get(_MOOD_KEY, Mood.RELAX)
    columns = st.columns(len(_MOOD_CARDS))
    for column, (mood, (icon, caption)) in zip(columns, _MOOD_CARDS.items(), strict=True):
        with column.container(border=True):
            st.markdown(f"### {icon}")
            st.markdown(f"**{MOOD_LABELS[mood]}**")
            st.caption(caption)
            label = "選択中" if mood == selected else "選ぶ"
            if st.button(label, key=f"mood-{mood.value}", type="primary" if mood == selected else "secondary"):
                st.session_state[_MOOD_KEY] = mood
                st.rerun()

    return selected


def _conditions_form(mood: Mood, defaults: PlaylistDefaults) -> PlaylistRequest | None:
    st.subheader("2. 条件を決める")
    # seed の初期値は画面を開いたときに 1 回だけ決める（再実行のたびに変わると入力が消えるため）
    if _SEED_KEY not in st.session_state:
        st.session_state[_SEED_KEY] = int(datetime.now().timestamp()) % _MAX_SEED

    with st.form("playlist-conditions"):
        by_count = st.radio("長さの指定", ["再生時間", "曲数"], horizontal=True) == "曲数"
        duration = st.slider(
            "再生時間（分）",
            defaults.min_duration_min,
            defaults.max_duration_min,
            defaults.default_duration_min,
            step=5,
        )
        count = st.number_input("曲数", min_value=1, max_value=defaults.max_track_count, value=15)
        genre_mode = st.radio("ジャンル", list(GenreMode), format_func=lambda m: GENRE_MODE_LABELS[m], horizontal=True)
        genres = st.multiselect("ジャンルを選ぶ（全ジャンル以外の場合）", list(Genre), format_func=genre_label)
        novelty = st.slider("選曲の意外性（左: 定番中心 / 右: 新しい発見）", 0.0, 1.0, 0.3, step=0.1)
        randomness = st.slider("ランダム性（左: 適合度順 / 右: ランダム）", 0.0, 1.0, 0.3, step=0.1)
        avoid_same_artist = st.checkbox("同じアーティストの連続を避ける", value=True)
        avoid_duplicates = st.checkbox("同じ曲の重複を避ける", value=True)
        smooth_order = st.checkbox("曲順を自然につなぐ", value=True)
        seed = st.number_input(
            "seed（同じ値なら同じ結果を再現します）",
            min_value=0,
            max_value=_MAX_SEED,
            key=_SEED_KEY,
        )
        submitted = st.form_submit_button("プレイリストを生成", type="primary")

    if not submitted:
        return None

    genre_mode = genre_mode or GenreMode.ALL
    if genre_mode != GenreMode.ALL and not genres:
        st.error("ジャンルを 1 つ以上選んでください。")
        return None

    return PlaylistRequest(
        mood=mood,
        duration_min=None if by_count else int(duration),
        track_count=int(count) if by_count else None,
        genre_mode=genre_mode,
        genres=tuple(genres),
        novelty=novelty,
        randomness=randomness,
        avoid_same_artist=avoid_same_artist,
        avoid_duplicates=avoid_duplicates,
        smooth_order=smooth_order,
        seed=int(seed),
    )


def _generate(candidates: list[Candidate], request: PlaylistRequest, defaults: PlaylistDefaults) -> None:
    result = generate_playlist(candidates, request, defaults)
    st.session_state[_RESULT_KEY] = result
    st.session_state[_DRAFT_KEY] = list(result.tracks)
    st.session_state.pop(_SAVED_KEY, None)
    st.session_state["pl_name"] = f"{MOOD_LABELS[request.mood]} {datetime.now():%Y-%m-%d %H:%M}"


def _shortfall_notice(candidates: list[Candidate], result: GenerationResult, defaults: PlaylistDefaults) -> None:
    for warning in result.warnings:
        st.warning(warning)

    shortfall = result.shortfall
    if shortfall is None:
        return

    if shortfall.unit == "seconds":
        amount = (
            f"目標 {shortfall.requested / _SECONDS_PER_MINUTE:.0f} 分に対して "
            f"{shortfall.achieved / _SECONDS_PER_MINUTE:.0f} 分"
        )
    else:
        amount = f"目標 {shortfall.requested:.0f} 曲に対して {shortfall.achieved:.0f} 曲"

    excluded = "、".join(f"{reason} {n} 曲" for reason, n in shortfall.excluded.items() if n) or "なし"
    st.warning(
        f"条件に合う曲が足りません（{amount}）。除外した曲: {excluded}\n\n"
        + "\n".join(f"- {s}" for s in shortfall.suggestions)
    )
    if not result.request.relaxed and st.button("条件を緩和して再生成"):
        _generate(candidates, replace(result.request, relaxed=True), defaults)
        st.rerun()


def _draft_editor(ctx: PageContext, candidates: list[Candidate], result: GenerationResult) -> None:
    st.subheader("3. 確認・編集する")
    draft: list[SelectedTrack] = st.session_state.get(_DRAFT_KEY, [])
    relaxed = "（条件を緩和）" if result.request.relaxed else ""
    st.write(f"{len(draft)} 曲・合計 {format_duration(total_duration(draft))}{relaxed}　seed: {result.request.seed}")

    for index, item in enumerate(draft):
        cols = st.columns([6, 1, 1, 1])
        score = "手動追加" if item.score is None else f"適合度 {item.score:.2f}"
        cols[0].markdown(
            f"{index + 1}. **{item.track.display_title}** — {item.track.display_artist}  \n"
            f"<small>{format_duration(item.track.duration_sec)}・{genre_label(item.candidate.genre)}・{score}</small>",
            unsafe_allow_html=True,
        )
        if cols[1].button("↑", key=f"up-{index}", disabled=index == 0, help="前へ"):
            _update_draft(move_item(draft, index, -1))

        if cols[2].button("↓", key=f"down-{index}", disabled=index == len(draft) - 1, help="後ろへ"):
            _update_draft(move_item(draft, index, 1))

        if cols[3].button("✕", key=f"remove-{index}", help="除外"):
            _update_draft(remove_item(draft, index))

    in_draft = {t.track.id for t in draft}
    addable = [c for c in candidates if c.track.id not in in_draft]
    with st.expander("曲を追加する"):
        choice = st.selectbox("追加する曲", addable, format_func=lambda c: track_label(c.track), index=None)
        if st.button("追加", disabled=choice is None) and choice is not None:
            _update_draft(add_item(draft, choice))

    st.subheader("4. 保存する")
    name = st.text_input("プレイリスト名", key="pl_name")
    if st.button("プレイリストを保存", type="primary", disabled=not draft or not name.strip()):
        playlist_id = _save(ctx, name, result.request, draft)
        st.session_state[_SAVED_KEY] = playlist_id
        st.success(f"「{name.strip()}」を保存しました。下の「Volumio に登録」から Volumio に登録できます。")


def _update_draft(draft: list[SelectedTrack]) -> None:
    st.session_state[_DRAFT_KEY] = draft
    st.session_state.pop(_SAVED_KEY, None)
    st.rerun()


def _save(ctx: PageContext, name: str, request: PlaylistRequest, draft: list[SelectedTrack]) -> int:
    with transaction(ctx.conn):
        return save_playlist(
            ctx.conn,
            PlaylistDraft(
                name=name,
                conditions=request.to_dict(),
                seed=request.seed,
                classification_source=ctx.classification_source.value,
                total_duration_sec=total_duration(draft),
                entries=[PlaylistEntry(t.track.id, t.score, t.added_manually) for t in draft],
            ),
        )
