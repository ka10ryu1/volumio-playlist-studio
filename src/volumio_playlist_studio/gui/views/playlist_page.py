"""プレイリスト作成画面。

条件を指定した生成（雰囲気と長さ、またはマップの円で選ぶ）、雰囲気マップの表示、編集・保存・Volumio 登録の
プレビュー、保存済みの削除を行う。
"""

import logging
from dataclasses import dataclass, replace
from datetime import datetime

import streamlit as st

from volumio_playlist_studio.catalog import load_candidates
from volumio_playlist_studio.config import PlaylistDefaults
from volumio_playlist_studio.gui.context import PageContext
from volumio_playlist_studio.gui.views import custom_mood_panel, mood_map_panel, volumio_panel
from volumio_playlist_studio.gui.widgets import format_duration, genre_label, track_label
from volumio_playlist_studio.models import CLASSIFICATION_SOURCE_LABELS, MOOD_LABELS, CustomMood, Genre, Mood
from volumio_playlist_studio.mood_map import (
    MOOD_POSITIONS,
    CircleRequest,
    generate_circle_playlist,
    select_circle_tracks,
)
from volumio_playlist_studio.playlist import (
    CREATION_METHOD_LABELS,
    GENRE_MODE_LABELS,
    PLAYLIST_MOODS,
    Candidate,
    CreationMethod,
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
from volumio_playlist_studio.storage.custom_moods import list_custom_moods
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

_METHOD_KEY = "pl_method"
_MOOD_KEY = "pl_mood"
_CENTER_KEY = "pl_center"
_RESULT_KEY = "pl_result"
_DRAFT_KEY = "pl_draft"
_SAVED_KEY = "pl_saved_id"
_SAVED_NAME_KEY = "pl_saved_name"
_DELETED_NAME_KEY = "pl_deleted_name"
_SEED_KEY = "pl_seed"
# 円の中心の選択肢のうち、自由記述の雰囲気を表す値の接頭辞（既存の雰囲気は Mood の値そのもの）
_CUSTOM_CENTER_PREFIX = "custom:"
_MAX_SEED = 1_000_000
_SECONDS_PER_MINUTE = 60
_CAP_NONE = "なし"
_CAP_DURATION = "再生時間"
_CAP_COUNT = "曲数"
_INPUT_KEYS = (
    _METHOD_KEY,
    _MOOD_KEY,
    _CENTER_KEY,
    _SEED_KEY,
    "pl_radius",
    "pl_cap",
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

type Request = PlaylistRequest | CircleRequest
type Result = GenerationResult[PlaylistRequest] | GenerationResult[CircleRequest]


@dataclass(frozen=True)
class _Options:
    """作成方法によらず共通の、詳細設定の値。"""

    genre_mode: GenreMode
    genres: tuple[Genre, ...]
    novelty: float
    randomness: float
    avoid_same_artist: bool
    avoid_duplicates: bool
    smooth_order: bool
    seed: int


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
    custom_moods = list_custom_moods(ctx.conn)
    st.caption("1 条件を選ぶ → 2 曲を編集 → 3 保存・連携")
    with st.expander("1. 選曲の条件", expanded=_RESULT_KEY not in st.session_state):
        request = _conditions_panel(defaults, ctx, candidates, custom_moods)

    # 円で選ぶ場合は、条件の欄の中にマップを描く
    if st.session_state.get(_METHOD_KEY) != CreationMethod.CIRCLE:
        with st.expander("雰囲気マップ"):
            mood_map_panel.render(candidates, custom_moods)

    if request is not None:
        _generate(candidates, request, defaults)
        st.rerun()

    result: Result | None = st.session_state.get(_RESULT_KEY)
    if result is not None:
        edit_tab, save_tab = st.tabs(["2. 曲を編集", "3. 保存・連携"])
        with edit_tab:
            _shortfall_notice(candidates, result, defaults)
            _draft_editor(candidates, result)

        with save_tab:
            _save_section(ctx, result)
            volumio_panel.render(ctx, st.session_state.get(_SAVED_KEY))

    _saved_playlists_section(ctx)


def _conditions_panel(
    defaults: PlaylistDefaults, ctx: PageContext, candidates: list[Candidate], custom_moods: list[CustomMood]
) -> Request | None:
    # 入力は都度セッションに反映する。曲の編集やページ移動で、未生成の条件も失わないようにする。
    if _SEED_KEY not in st.session_state:
        st.session_state[_SEED_KEY] = int(datetime.now().timestamp()) % _MAX_SEED

    for key, value in {
        _METHOD_KEY: CreationMethod.MOOD,
        "pl_duration": defaults.default_duration_min,
        "pl_count": 15,
        "pl_radius": defaults.default_circle_radius,
        "pl_novelty": 0.3,
        "pl_randomness": 0.3,
        "pl_artist": True,
        "pl_duplicates": True,
        "pl_smooth": True,
    }.items():
        st.session_state.setdefault(key, value)

    with st.container():
        method = st.radio(
            "作成方法",
            list(CreationMethod),
            format_func=CREATION_METHOD_LABELS.__getitem__,
            horizontal=True,
            key=_METHOD_KEY,
        )
        if method == CreationMethod.CIRCLE:
            return _circle_conditions(defaults, ctx, candidates, custom_moods)

        return _mood_conditions(defaults, ctx)


def _mood_conditions(defaults: PlaylistDefaults, ctx: PageContext) -> PlaylistRequest | None:
    mood = st.radio("雰囲気", PLAYLIST_MOODS, format_func=MOOD_LABELS.__getitem__, horizontal=True, key=_MOOD_KEY)
    by_count = st.radio("長さの指定", ["再生時間", "曲数"], horizontal=True, key="pl_length") == "曲数"
    duration, count = _length_inputs(defaults)
    st.caption("「長さの指定」で選んだ方の値を使います。")
    options = _detail_options(ctx, ranking="適合度順", preference_note=None)
    if not _generate_button():
        return None

    if mood is None:
        st.error("雰囲気を選んでください。")
        return None

    if not _genres_are_valid(options):
        return None

    return PlaylistRequest(
        mood=mood,
        duration_min=None if by_count else duration,
        track_count=count if by_count else None,
        genre_mode=options.genre_mode,
        genres=options.genres,
        novelty=options.novelty,
        randomness=options.randomness,
        avoid_same_artist=options.avoid_same_artist,
        avoid_duplicates=options.avoid_duplicates,
        smooth_order=options.smooth_order,
        seed=options.seed,
    )


def _circle_conditions(
    defaults: PlaylistDefaults, ctx: PageContext, candidates: list[Candidate], custom_moods: list[CustomMood]
) -> CircleRequest | None:
    centers = _center_options(custom_moods)
    # 削除した自由記述の雰囲気が選択されたまま残らないようにする
    if st.session_state.get(_CENTER_KEY) not in centers:
        st.session_state.pop(_CENTER_KEY, None)

    selected = st.radio(
        "円の中心（雰囲気）",
        list(centers),
        format_func=lambda key: _center_label(centers[key]),
        horizontal=True,
        key=_CENTER_KEY,
    )
    center = None if selected is None else centers[selected]
    with st.expander("自由記述で雰囲気を作る（Jev を使用）"):
        custom_mood_panel.render(ctx, custom_moods)

    radius = st.slider(
        "円の大きさ（半径）",
        defaults.min_circle_radius,
        defaults.max_circle_radius,
        step=0.05,
        key="pl_radius",
        help="マップの縦横はそれぞれ 0〜1 です。円の中の曲を、明るさとエネルギー感の位置だけで選びます。",
    )
    # 上限などの入力を読んでから、選ばれる曲を描く
    map_area = st.container()
    cap = st.radio("上限", [_CAP_NONE, _CAP_DURATION, _CAP_COUNT], horizontal=True, key="pl_cap")
    duration, count = _length_inputs(defaults)
    st.caption("上限を付けると、円の中の曲から中心に近い順に、上限を超えない範囲で選びます。")
    options = _detail_options(
        ctx, ranking="中心に近い順", preference_note=None if cap != _CAP_NONE else "上限を付けたときだけ使います。"
    )
    request = None
    if center is not None and (options.genre_mode == GenreMode.ALL or options.genres):
        request = CircleRequest(
            mood=center,
            radius=float(radius),
            duration_min=duration if cap == _CAP_DURATION else None,
            track_count=count if cap == _CAP_COUNT else None,
            genre_mode=options.genre_mode,
            genres=options.genres,
            novelty=options.novelty,
            randomness=options.randomness,
            avoid_same_artist=options.avoid_same_artist,
            avoid_duplicates=options.avoid_duplicates,
            smooth_order=options.smooth_order,
            seed=options.seed,
        )

    with map_area:
        if request is None:
            mood_map_panel.render(candidates, custom_moods)
        else:
            # 入力のたびに再実行されるため、曲順の調整は行わず、選ばれる曲だけを求める
            chosen = select_circle_tracks(candidates, request, defaults)
            st.markdown(f"**選ばれる曲: {len(chosen)} 曲・合計 {format_duration(total_duration(chosen))}**")
            mood_map_panel.render(candidates, custom_moods, (request, chosen))

    if not _generate_button():
        return None

    if center is None:
        st.error("円の中心にする雰囲気を選んでください。")
        return None

    if not _genres_are_valid(options):
        return None

    return request


def _center_options(custom_moods: list[CustomMood]) -> dict[str, Mood | CustomMood]:
    """円の中心の選択肢。セッションに保存する値は文字列にし、ページ移動や削除の後も比較できるようにする。"""
    options: dict[str, Mood | CustomMood] = {mood.value: mood for mood in Mood if mood in MOOD_POSITIONS}
    for mood in custom_moods:
        options[f"{_CUSTOM_CENTER_PREFIX}{mood.id}"] = mood

    return options


def _center_label(mood: Mood | CustomMood) -> str:
    return mood.name if isinstance(mood, CustomMood) else MOOD_LABELS[mood]


def _length_inputs(defaults: PlaylistDefaults) -> tuple[int, int]:
    """再生時間（分）と曲数の入力欄。どちらを使うかは呼び出し元が決める。"""
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
    return int(duration), int(count)


def _detail_options(ctx: PageContext, *, ranking: str, preference_note: str | None) -> _Options:
    """詳細設定の欄。

    Args:
        ctx: 画面の共通情報。
        ranking: ランダム性が 0 のときの選曲の順（例: 適合度順）。ランダム性の説明に使う。
        preference_note: 意外性・ランダム性を使わない場合に、入力欄を無効にして表示する説明。使う場合は None。
    """
    with st.expander("詳細設定"):
        st.caption(f"選曲に使う分類結果: {CLASSIFICATION_SOURCE_LABELS[ctx.classification_source]}（設定画面で変更）")
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
        # 使わない場合も入力欄は残し、無効にする。表示しないと Streamlit が入力済みの値を破棄するため
        disabled = preference_note is not None
        novelty = st.slider(
            "選曲の意外性（左: 定番中心 / 右: 新しい発見）", 0.0, 1.0, step=0.1, key="pl_novelty", disabled=disabled
        )
        randomness = st.slider(
            f"ランダム性（左: {ranking} / 右: ランダム）", 0.0, 1.0, step=0.1, key="pl_randomness", disabled=disabled
        )
        if preference_note is not None:
            st.caption(f"選曲の意外性・ランダム性は、{preference_note}")

        avoid_same_artist = st.checkbox("同じアーティストの連続を避ける", key="pl_artist")
        avoid_duplicates = st.checkbox("同じ曲の重複を避ける", key="pl_duplicates")
        smooth_order = st.checkbox("曲順を自然につなぐ", key="pl_smooth")
        seed = st.number_input(
            "seed（同じ値なら同じ結果を再現します）",
            min_value=0,
            max_value=_MAX_SEED,
            key=_SEED_KEY,
        )

    return _Options(
        genre_mode=genre_mode or GenreMode.ALL,
        genres=tuple(genres),
        novelty=novelty,
        randomness=randomness,
        avoid_same_artist=avoid_same_artist,
        avoid_duplicates=avoid_duplicates,
        smooth_order=smooth_order,
        seed=int(seed),
    )


def _generate_button() -> bool:
    if _RESULT_KEY in st.session_state:
        st.caption("生成すると編集中の曲一覧を置き換えます。保存済みのプレイリストは残ります。")

    return st.button("プレイリストを生成", type="primary", width="stretch")


def _genres_are_valid(options: _Options) -> bool:
    if options.genre_mode != GenreMode.ALL and not options.genres:
        st.error("ジャンルを 1 つ以上選んでください。")
        return False

    return True


def _generate(candidates: list[Candidate], request: Request, defaults: PlaylistDefaults) -> None:
    result: Result
    if isinstance(request, CircleRequest):
        result = generate_circle_playlist(candidates, request, defaults)
    else:
        result = generate_playlist(candidates, request, defaults)

    st.session_state[_RESULT_KEY] = result
    st.session_state[_DRAFT_KEY] = list(result.tracks)
    st.session_state.pop(_SAVED_KEY, None)
    st.session_state["pl_name"] = f"{_playlist_title(request)} {datetime.now():%Y-%m-%d %H:%M}"


def _playlist_title(request: Request) -> str:
    """プレイリストの既定の名前と見出しに使う、条件の短い説明。"""
    if isinstance(request, CircleRequest):
        return f"{_center_label(request.mood)}周辺"

    return MOOD_LABELS[request.mood]


def _shortfall_notice(candidates: list[Candidate], result: Result, defaults: PlaylistDefaults) -> None:
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
    request = result.request
    if isinstance(request, PlaylistRequest) and not request.relaxed and st.button("条件を緩和して再生成"):
        _generate(candidates, replace(request, relaxed=True), defaults)
        st.rerun()


def _draft_editor(candidates: list[Candidate], result: Result) -> None:
    draft: list[SelectedTrack] = st.session_state.get(_DRAFT_KEY, [])
    request = result.request
    if isinstance(request, CircleRequest):
        note = f"（マップの円・半径 {request.radius:.2f}）"
        score_label = "中心への近さ"
    else:
        note = "（条件を緩和）" if request.relaxed else ""
        score_label = "適合度"

    st.subheader(f"{_playlist_title(request)}のプレイリスト")
    st.write(f"{len(draft)} 曲・合計 {format_duration(total_duration(draft))}{note}")
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
                {"曲名": t.track.display_title, score_label: "手動追加" if t.score is None else f"{t.score:.2f}"}
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


def _save_section(ctx: PageContext, result: Result) -> None:
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


def _save(ctx: PageContext, name: str, request: Request, draft: list[SelectedTrack]) -> int:
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
