"""プレイリスト作成画面。条件を指定した生成、編集・保存・Volumio 登録のプレビュー、保存済みの削除を行う。"""

import logging
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
from volumio_playlist_studio.storage.playlists import (
    PlaylistDraft,
    PlaylistEntry,
    SavedPlaylist,
    delete_playlist,
    list_playlists,
    save_playlist,
)

logger = logging.getLogger(__name__)

_MOOD_KEY = "pl_mood"
_RESULT_KEY = "pl_result"
_DRAFT_KEY = "pl_draft"
_SAVED_KEY = "pl_saved_id"
_SAVED_NAME_KEY = "pl_saved_name"
_DELETED_NAME_KEY = "pl_deleted_name"
_SEED_KEY = "pl_seed"
_MAX_SEED = 1_000_000
_SECONDS_PER_MINUTE = 60
_INPUT_KEYS = (
    _MOOD_KEY,
    _SEED_KEY,
    "pl_length",
    "pl_duration",
    "pl_count",
    "pl_genre_mode",
    "pl_genres",
    "pl_novelty",
    "pl_randomness",
    "pl_artist",
    "pl_duplicates",
    "pl_smooth",
    "pl_name",
    "pl_edit_track",
)
_INPUT_VALUES_KEY = "pl_input_values"


def preserve_inputs() -> None:
    """ページ移動時に破棄される入力部品の値を、通常のセッション状態に退避する。"""
    values = st.session_state.setdefault(_INPUT_VALUES_KEY, {})
    for key in _INPUT_KEYS:
        if key in st.session_state:
            values[key] = st.session_state[key]


def render(ctx: PageContext) -> None:
    """プレイリスト作成画面を表示する。"""
    st.title("プレイリスト作成")
    if ctx.library_root is None:
        st.info("音楽フォルダが未設定です。音楽ライブラリ画面で指定するか、サンプルデータで試してください。")
        return

    # Streamlit はページ切り替えの前に入力部品を破棄するため、再表示時だけ値を戻す。
    for key, value in st.session_state.get(_INPUT_VALUES_KEY, {}).items():
        st.session_state.setdefault(key, value)

    defaults = PlaylistDefaults()
    candidates = load_candidates(ctx.conn, ctx.library_root, ctx.classification_source)
    st.caption("1 条件を選ぶ → 2 曲を編集 → 3 保存・連携")
    with st.expander("1. 選曲の条件", expanded=_RESULT_KEY not in st.session_state):
        request = _conditions_panel(defaults, ctx)

    if request is not None:
        _generate(candidates, request, defaults)
        st.rerun()

    result: GenerationResult | None = st.session_state.get(_RESULT_KEY)
    if result is not None:
        edit_tab, save_tab = st.tabs(["2. 曲を編集", "3. 保存・連携"])
        with edit_tab:
            _shortfall_notice(candidates, result, defaults)
            _draft_editor(candidates, result)

        with save_tab:
            _save_section(ctx, result)
            volumio_panel.render(ctx, st.session_state.get(_SAVED_KEY))

    _saved_playlists_section(ctx)


def _conditions_panel(defaults: PlaylistDefaults, ctx: PageContext) -> PlaylistRequest | None:
    # 入力は都度セッションに反映する。曲の編集やページ移動で、未生成の条件も失わないようにする。
    if _SEED_KEY not in st.session_state:
        st.session_state[_SEED_KEY] = int(datetime.now().timestamp()) % _MAX_SEED

    for key, value in {
        "pl_duration": defaults.default_duration_min,
        "pl_count": 15,
        "pl_novelty": 0.3,
        "pl_randomness": 0.3,
        "pl_artist": True,
        "pl_duplicates": True,
        "pl_smooth": True,
    }.items():
        st.session_state.setdefault(key, value)

    with st.container():
        mood = st.radio("雰囲気", list(Mood), format_func=MOOD_LABELS.__getitem__, horizontal=True, key=_MOOD_KEY)
        by_count = st.radio("長さの指定", ["再生時間", "曲数"], horizontal=True, key="pl_length") == "曲数"
        duration_col, count_col = st.columns(2)
        duration = duration_col.slider(
            "再生時間（分）",
            defaults.min_duration_min,
            defaults.max_duration_min,
            step=5,
            key="pl_duration",
        )
        count = count_col.number_input(
            "曲数",
            min_value=1,
            max_value=defaults.max_track_count,
            key="pl_count",
        )
        st.caption("「長さの指定」で選んだ方の値を使います。")
        with st.expander("詳細設定"):
            st.caption(
                f"選曲に使う分類結果: {CLASSIFICATION_SOURCE_LABELS[ctx.classification_source]}（設定画面で変更）"
            )
            genre_mode = st.radio(
                "ジャンル",
                list(GenreMode),
                format_func=lambda m: GENRE_MODE_LABELS[m],
                horizontal=True,
                key="pl_genre_mode",
            )
            genres = st.multiselect(
                "ジャンルを選ぶ（全ジャンル以外の場合）",
                list(Genre),
                format_func=genre_label,
                placeholder="ジャンルを選択",
                key="pl_genres",
                # 一覧から選ぶだけなので文字入力を受け付けず、スマートフォンでキーボードを出さない
                filter_mode=None,
            )
            novelty = st.slider("選曲の意外性（左: 定番中心 / 右: 新しい発見）", 0.0, 1.0, step=0.1, key="pl_novelty")
            randomness = st.slider("ランダム性（左: 適合度順 / 右: ランダム）", 0.0, 1.0, step=0.1, key="pl_randomness")
            avoid_same_artist = st.checkbox("同じアーティストの連続を避ける", key="pl_artist")
            avoid_duplicates = st.checkbox("同じ曲の重複を避ける", key="pl_duplicates")
            smooth_order = st.checkbox("曲順を自然につなぐ", key="pl_smooth")
            seed = st.number_input(
                "seed（同じ値なら同じ結果を再現します）",
                min_value=0,
                max_value=_MAX_SEED,
                key=_SEED_KEY,
            )

        if _RESULT_KEY in st.session_state:
            st.caption("生成すると編集中の曲一覧を置き換えます。保存済みのプレイリストは残ります。")

        submitted = st.button("プレイリストを生成", type="primary", width="stretch")

    if not submitted:
        return None

    if mood is None:
        st.error("雰囲気を選んでください。")
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


def _draft_editor(candidates: list[Candidate], result: GenerationResult) -> None:
    draft: list[SelectedTrack] = st.session_state.get(_DRAFT_KEY, [])
    relaxed = "（条件を緩和）" if result.request.relaxed else ""
    st.subheader(f"{MOOD_LABELS[result.request.mood]}のプレイリスト")
    st.write(f"{len(draft)} 曲・合計 {format_duration(total_duration(draft))}{relaxed}")
    if draft:
        st.dataframe(
            [
                {
                    "順番": i + 1,
                    "曲名": item.track.display_title,
                    "アーティスト": item.track.display_artist,
                    "時間": format_duration(item.track.duration_sec),
                    "ジャンル": genre_label(item.candidate.genre),
                }
                for i, item in enumerate(draft)
            ],
            hide_index=True,
            width="stretch",
            height=280,
            column_config={
                "順番": st.column_config.NumberColumn(width="small"),
                "曲名": st.column_config.TextColumn(width="medium"),
            },
        )
        _edit_track(draft)
    else:
        st.info("曲がありません。「曲を追加する」か、条件を変えて生成してください。")

    in_draft = {t.track.id for t in draft}
    addable = [c for c in candidates if c.track.id not in in_draft]
    with st.expander("曲を追加する"):
        choice = st.selectbox(
            "追加する曲",
            addable,
            format_func=lambda c: track_label(c.track),
            index=None,
            placeholder="曲名・アーティストで検索",
        )
        if st.button("追加", disabled=choice is None) and choice is not None:
            _update_draft(add_item(draft, choice))

    with st.expander("選曲の詳細"):
        st.caption(f"seed: {result.request.seed}")
        st.dataframe(
            [
                {"曲名": t.track.display_title, "適合度": "手動追加" if t.score is None else f"{t.score:.2f}"}
                for t in draft
            ],
            hide_index=True,
            width="stretch",
        )

    st.caption("編集が終わったら「3. 保存・連携」タブで名前を付けて保存します。")


def _edit_track(draft: list[SelectedTrack]) -> None:
    ids = [item.track.id for item in draft]
    selected_id = st.selectbox(
        "編集する曲",
        ids,
        format_func=lambda track_id: f"{ids.index(track_id) + 1}. {track_label(draft[ids.index(track_id)].track)}",
        key="pl_edit_track",
        filter_mode=None,
    )
    if selected_id is None:
        return

    selected = ids.index(selected_id)
    # 曲名は標準のテキスト表示に任せ、タグ由来の文字列を HTML として解釈させない。
    st.text(track_label(draft[selected].track))
    with st.container(horizontal=True):
        if st.button("上へ", disabled=selected == 0, icon=":material/arrow_upward:"):
            _update_draft(move_item(draft, selected, -1))

        if st.button("下へ", disabled=selected == len(draft) - 1, icon=":material/arrow_downward:"):
            _update_draft(move_item(draft, selected, 1))

        if st.button("除外", help="このプレイリストから外します。音楽ファイルは変更しません。"):
            _update_draft(remove_item(draft, selected))


def _save_section(ctx: PageContext, result: GenerationResult) -> None:
    draft: list[SelectedTrack] = st.session_state.get(_DRAFT_KEY, [])
    st.subheader("名前を付けて保存")
    st.caption(f"{len(draft)} 曲・合計 {format_duration(total_duration(draft))}")
    name = st.text_input("プレイリスト名", key="pl_name")
    if st.button("プレイリストを保存", type="primary", disabled=not draft or not name.strip()):
        playlist_id = _save(ctx, name, result.request, draft)
        st.session_state[_SAVED_KEY] = playlist_id
        st.session_state[_SAVED_NAME_KEY] = name.strip()
        st.success(f"「{name.strip()}」を保存しました。下の「Volumio に登録」から Volumio に登録できます。")
    elif st.session_state.get(_SAVED_KEY) is not None and st.session_state.get(_SAVED_NAME_KEY) == name.strip():
        st.caption("保存済みです。Volumio との照合・登録は下の欄で行えます。")
    else:
        st.caption("編集中の内容は未保存です。曲を変更したら、もう一度保存してください。")


def _saved_playlists_section(ctx: PageContext) -> None:
    """保存済みのプレイリストを削除する欄。生成前でも操作できるよう、結果の有無によらず表示する。"""
    deleted_name: str | None = st.session_state.pop(_DELETED_NAME_KEY, None)
    playlists = list_playlists(ctx.conn)
    if not playlists and deleted_name is None:
        return

    with st.expander("保存済みのプレイリスト", expanded=deleted_name is not None):
        if deleted_name is not None:
            st.success(f"「{deleted_name}」を削除しました。")

        if not playlists:
            st.caption("保存したプレイリストがありません。")
            return

        playlist = st.selectbox(
            "プレイリスト",
            playlists,
            format_func=lambda p: f"{p.name}（{p.created_at}）",
            key="pl_manage",
            # 一覧から選ぶだけなので文字入力を受け付けず、スマートフォンでキーボードを出さない
            filter_mode=None,
        )
        if playlist is None:
            return

        st.caption(f"{len(playlist.entries)} 曲・合計 {format_duration(playlist.total_duration_sec)}")
        st.caption(
            "削除すると、このアプリの一覧に表示されなくなります。満足度などの評価は精度評価の集計に残ります。"
            "Volumio に登録したプレイリストは削除しません（Volumio の画面で削除してください）。"
        )
        confirmed = st.checkbox("このプレイリストを削除します", key=f"pl-delete-confirm-{playlist.id}")
        if st.button("プレイリストを削除", disabled=not confirmed, key=f"pl-delete-{playlist.id}"):
            _delete(ctx, playlist)


def _delete(ctx: PageContext, playlist: SavedPlaylist) -> None:
    try:
        with transaction(ctx.conn):
            delete_playlist(ctx.conn, playlist.id)
    except ValueError:
        # 別のタブなどで先に削除された場合。一覧を読み直して表示を合わせる
        logger.warning("playlist_delete_skipped: playlist_id=%d reason=not_found_or_deleted", playlist.id)
        st.error("このプレイリストは既に削除されています。")
        return

    logger.info("playlist_deleted: playlist_id=%d", playlist.id)
    if st.session_state.get(_SAVED_KEY) == playlist.id:
        st.session_state.pop(_SAVED_KEY, None)

    st.session_state[_DELETED_NAME_KEY] = playlist.name
    st.rerun()


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
