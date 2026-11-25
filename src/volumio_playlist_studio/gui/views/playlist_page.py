"""プレイリスト作成画面。

条件を指定した生成（雰囲気と長さ、マップの円、またはアーティストで選ぶ）、雰囲気マップの表示、編集・保存を行う。
保存済みのプレイリストの一覧・削除・Volumio への登録と、自由記述の雰囲気の作成・削除はプレイリスト管理画面で行う。
"""

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime

import streamlit as st

from volumio_playlist_studio.artist_playlist import ArtistRequest, generate_artist_playlist, list_artists
from volumio_playlist_studio.catalog import load_candidates
from volumio_playlist_studio.config import PlaylistDefaults
from volumio_playlist_studio.gui.context import PageContext
from volumio_playlist_studio.gui.views import mood_map_panel
from volumio_playlist_studio.gui.views.playlist_tracks import close_player, track_list
from volumio_playlist_studio.gui.widgets import format_duration, genre_label, page_title, track_label
from volumio_playlist_studio.models import (
    CLASSIFICATION_SOURCE_LABELS,
    MOOD_LABELS,
    ClassificationSource,
    CustomMood,
    Genre,
    Mood,
)
from volumio_playlist_studio.mood_map import (
    DISPLAY_SCALE,
    MOOD_POSITIONS,
    CircleRequest,
    generate_circle_playlist,
    select_circle_tracks,
)
from volumio_playlist_studio.playlist import (
    CHILDREN_MODE_LABELS,
    CREATION_METHOD_LABELS,
    GENRE_MODE_LABELS,
    PLAYLIST_MOODS,
    Candidate,
    ChildrenMode,
    CreationMethod,
    GenerationResult,
    GenreMode,
    PlaylistRequest,
    SelectedTrack,
    add_item,
    generate_playlist,
    move_item,
    total_duration,
)
from volumio_playlist_studio.storage.custom_moods import list_custom_moods
from volumio_playlist_studio.storage.database import transaction
from volumio_playlist_studio.storage.playlists import PlaylistDraft, PlaylistEntry, save_playlist

_METHOD_KEY = "pl_method"
_MOOD_KEY = "pl_mood"
_CENTER_KEY = "pl_center"
_ARTISTS_KEY = "pl_artist_names"
_ARTIST_MOOD_KEY = "pl_artist_mood"
_RESULT_KEY = "pl_result"
_DRAFT_KEY = "pl_draft"
_SAVED_KEY = "pl_saved_id"
_SAVED_NAME_KEY = "pl_saved_name"
_SEED_KEY = "pl_seed"
# 編集中の曲を選んだときの分類結果の種類。生成後に設定を変えても、保存・除外の記録には生成時の種類を残す
_SOURCE_KEY = "pl_source"
_TABS_KEY = "playlist-tabs"
_EDIT_TAB = "2. 曲を編集"
_SAVE_TAB = "3. 保存"
# 円の中心の選択肢のうち、自由記述の雰囲気を表す値の接頭辞（既存の雰囲気は Mood の値そのもの）
_CUSTOM_CENTER_PREFIX = "custom:"
_MAX_SEED = 1_000_000
# プレイリスト名に並べるアーティストの数。残りは「ほか N 組」とする
_TITLE_ARTISTS = 2
_SECONDS_PER_MINUTE = 60
_CAP_NONE = "なし"
_CAP_DURATION = "再生時間"
_CAP_COUNT = "曲数"
_INPUT_KEYS = (
    _METHOD_KEY,
    _MOOD_KEY,
    _CENTER_KEY,
    _ARTISTS_KEY,
    _ARTIST_MOOD_KEY,
    _SEED_KEY,
    "pl_radius",
    "pl_cap",
    "pl_length",
    "pl_duration",
    "pl_count",
    "pl_genre_mode",
    "pl_genres",
    "pl_children",
    "pl_min_track_sec",
    "pl_novelty",
    "pl_randomness",
    "pl_artist",
    "pl_duplicates",
    "pl_smooth",
    "pl_name",
    "pl_edit_track",
)
_INPUT_VALUES_KEY = "pl_input_values"
_SUGGESTED_GENRE_KEY = "pl_suggested_genre"
_GENRE_APPLIED_KEY = "pl_genre_applied"

# ArtistRequest は PlaylistRequest の派生のため、型で分岐するときは ArtistRequest を先に判定する
type Request = PlaylistRequest | CircleRequest | ArtistRequest
type Result = GenerationResult[PlaylistRequest] | GenerationResult[CircleRequest] | GenerationResult[ArtistRequest]


@dataclass(frozen=True)
class _Options:
    """作成方法によらず共通の、詳細設定の値。"""

    genre_mode: GenreMode
    genres: tuple[Genre, ...]
    children_mode: ChildrenMode
    min_track_sec: int
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


def suggest_genre(genre: Genre | None) -> None:
    """ダッシュボードで選んだジャンルを提案として受け取り、編集中の条件は保持する。"""
    if genre is None:
        st.session_state.pop(_SUGGESTED_GENRE_KEY, None)
    else:
        st.session_state[_SUGGESTED_GENRE_KEY] = genre


def _genre_suggestion() -> bool:
    """明示的に反映したときだけジャンル条件を変更する。生成済みの曲には触れない。"""
    if st.session_state.pop(_GENRE_APPLIED_KEY, False):
        st.success("ジャンル条件を反映しました。「プレイリストを生成」で曲を選び直せます。")
        return True

    genre = st.session_state.get(_SUGGESTED_GENRE_KEY)
    if genre is None:
        return False

    with st.container(border=True):
        st.write(f"ダッシュボードで選んだジャンル: {genre_label(genre)}")
        st.caption("反映すると「指定ジャンルのみ」に変更します。ほかの条件・編集中の曲・名前は保持します。")
        with st.container(horizontal=True):
            st.button("ジャンル条件に反映", on_click=_apply_genre, args=(genre,))
            st.button("見送る", on_click=suggest_genre, args=(None,))

    return False


def _apply_genre(genre: Genre) -> None:
    # 本文描画前のコールバックで反映し、入力保持処理に古い条件が退避されるのを防ぐ。
    st.session_state["pl_genre_mode"] = GenreMode.ONLY
    st.session_state["pl_genres"] = [genre]
    st.session_state.pop(_SUGGESTED_GENRE_KEY, None)
    st.session_state[_GENRE_APPLIED_KEY] = True


def render(ctx: PageContext, *, on_manage: Callable[[], None] | None = None) -> None:
    """プレイリスト作成画面を表示する。

    Args:
        ctx: 画面の共通情報。
        on_manage: プレイリスト管理画面へ移動する処理。1 画面だけを表示するテストでは None。
    """
    page_title("プレイリスト作成")
    if ctx.library_root is None:
        st.info("音楽フォルダが未設定です。音楽ライブラリ画面で指定するか、サンプルデータで試してください。")
        return

    # Streamlit はページ切り替えの前に入力部品を破棄するため、再表示時だけ値を戻す。
    for key, value in st.session_state.get(_INPUT_VALUES_KEY, {}).items():
        st.session_state.setdefault(key, value)

    genre_applied = _genre_suggestion()
    defaults = PlaylistDefaults()
    candidates = load_candidates(ctx.conn, ctx.library_root, ctx.classification_source)
    custom_moods = list_custom_moods(ctx.conn)
    st.caption("1 条件を選ぶ → 2 曲を編集 → 3 保存（一覧・削除・Volumio への登録はプレイリスト管理画面）")
    with st.expander("1. 選曲の条件", expanded=genre_applied or _RESULT_KEY not in st.session_state):
        request = _conditions_panel(defaults, ctx, candidates, custom_moods, on_manage)

    # 円で選ぶ場合は、条件の欄の中にマップを描く。
    # アーティストで選ぶ場合は、ライブラリ全体のマップは選曲と関係が薄いため描かない
    if st.session_state.get(_METHOD_KEY) == CreationMethod.MOOD:
        with st.expander("雰囲気マップ"):
            mood_map_panel.render(candidates, custom_moods, selected=st.session_state.get(_MOOD_KEY))

    if request is not None:
        _generate(candidates, request, defaults, ctx.classification_source)
        st.rerun()

    result: Result | None = st.session_state.get(_RESULT_KEY)
    if result is not None:
        # 曲一覧の下の「保存する」から保存タブへ切り替えられるよう、選択中のタブを状態として持つ
        edit_tab, save_tab = st.tabs([_EDIT_TAB, _SAVE_TAB], key=_TABS_KEY, on_change="rerun")
        with edit_tab:
            _shortfall_notice(candidates, result, defaults, ctx.classification_source)
            _draft_editor(ctx, candidates, result)

        with save_tab:
            _save_section(ctx, result, on_manage)


def _conditions_panel(
    defaults: PlaylistDefaults,
    ctx: PageContext,
    candidates: list[Candidate],
    custom_moods: list[CustomMood],
    on_manage: Callable[[], None] | None,
) -> Request | None:
    # 入力は都度セッションに反映する。曲の編集やページ移動で、未生成の条件も失わないようにする。
    if _SEED_KEY not in st.session_state:
        st.session_state[_SEED_KEY] = int(datetime.now().timestamp()) % _MAX_SEED

    for key, value in {
        _METHOD_KEY: CreationMethod.MOOD,
        _ARTIST_MOOD_KEY: Mood.OMAKASE,
        "pl_duration": defaults.default_duration_min,
        "pl_count": 15,
        # 半径は画面の表示（0〜10）に合わせた値で持つ
        "pl_radius": defaults.default_circle_radius * DISPLAY_SCALE,
        "pl_children": ChildrenMode.EXCLUDE,
        "pl_min_track_sec": 0,
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
            return _circle_conditions(defaults, ctx, candidates, custom_moods, on_manage)

        if method == CreationMethod.ARTIST:
            return _artist_conditions(defaults, ctx, candidates)

        return _mood_conditions(defaults, ctx)


def _mood_conditions(defaults: PlaylistDefaults, ctx: PageContext) -> PlaylistRequest | None:
    mood = st.selectbox("雰囲気", PLAYLIST_MOODS, format_func=MOOD_LABELS.__getitem__, key=_MOOD_KEY, filter_mode=None)
    by_count = st.radio("長さの指定", ["再生時間", "曲数"], horizontal=True, key="pl_length") == "曲数"
    duration, count = _length_inputs(defaults)
    st.caption("「長さの指定」で選んだ方の値を使います。")
    options = _detail_options(defaults, ctx, ranking="適合度順", preference_note=None)
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
        children_mode=options.children_mode,
        min_track_sec=options.min_track_sec,
        novelty=options.novelty,
        randomness=options.randomness,
        avoid_same_artist=options.avoid_same_artist,
        avoid_duplicates=options.avoid_duplicates,
        smooth_order=options.smooth_order,
        seed=options.seed,
    )


def _circle_conditions(
    defaults: PlaylistDefaults,
    ctx: PageContext,
    candidates: list[Candidate],
    custom_moods: list[CustomMood],
    on_manage: Callable[[], None] | None,
) -> CircleRequest | None:
    centers = _center_options(custom_moods)
    # 削除した自由記述の雰囲気が選択されたまま残らないようにする
    if st.session_state.get(_CENTER_KEY) not in centers:
        st.session_state.pop(_CENTER_KEY, None)

    selected = st.selectbox(
        "円の中心（雰囲気）",
        list(centers),
        format_func=lambda key: _center_label(centers[key]),
        key=_CENTER_KEY,
        filter_mode=None,
    )
    center = None if selected is None else centers[selected]
    with st.container(horizontal=True, vertical_alignment="center"):
        st.caption(
            "説明文から雰囲気を作る（Jev を使用）・作った雰囲気を削除するには、"
            "プレイリスト管理画面の「雰囲気」タブを開きます。"
        )
        _manage_button(on_manage, key="pl-manage-moods")

    shown_radius = st.slider(
        "円の大きさ（半径）",
        defaults.min_circle_radius * DISPLAY_SCALE,
        defaults.max_circle_radius * DISPLAY_SCALE,
        step=0.5,
        format="%.1f",
        key="pl_radius",
        help=f"マップの縦横はそれぞれ 0〜{DISPLAY_SCALE} です。円の中の曲を、明るさとエネルギー感の位置で選びます。",
    )
    # 表示の倍率で割ったときの誤差で、範囲の検証に失敗しないよう丸める
    radius = round(shown_radius / DISPLAY_SCALE, 4)
    # 上限などの入力を読んでから、選ばれる曲を描く
    map_area = st.container()
    cap = st.radio("上限", [_CAP_NONE, _CAP_DURATION, _CAP_COUNT], horizontal=True, key="pl_cap")
    duration, count = _length_inputs(defaults)
    st.caption("上限を付けると、円の中の曲から中心に近い順に、上限を超えない範囲で選びます。")
    options = _detail_options(
        defaults,
        ctx,
        ranking="中心に近い順",
        preference_note=None if cap != _CAP_NONE else "上限を付けたときだけ使います。",
    )
    request = None
    if center is not None and (options.genre_mode == GenreMode.ALL or options.genres):
        request = CircleRequest(
            mood=center,
            radius=radius,
            duration_min=duration if cap == _CAP_DURATION else None,
            track_count=count if cap == _CAP_COUNT else None,
            genre_mode=options.genre_mode,
            genres=options.genres,
            children_mode=options.children_mode,
            min_track_sec=options.min_track_sec,
            novelty=options.novelty,
            randomness=options.randomness,
            avoid_same_artist=options.avoid_same_artist,
            avoid_duplicates=options.avoid_duplicates,
            smooth_order=options.smooth_order,
            seed=options.seed,
        )

    with map_area:
        if request is None:
            mood_map_panel.render(candidates, custom_moods, selected=center)
        else:
            # 入力のたびに再実行されるため、曲順の調整は行わず、選ばれる曲だけを求める
            chosen = select_circle_tracks(candidates, request, defaults)
            st.markdown(f"**選ばれる曲: {len(chosen)} 曲・合計 {format_duration(total_duration(chosen))}**")
            mood_map_panel.render(candidates, custom_moods, (request, chosen), selected=center)

    if not _generate_button():
        return None

    if center is None:
        st.error("円の中心にする雰囲気を選んでください。")
        return None

    if not _genres_are_valid(options):
        return None

    return request


def _artist_conditions(
    defaults: PlaylistDefaults, ctx: PageContext, candidates: list[Candidate]
) -> ArtistRequest | None:
    artists = {summary.name: summary.track_count for summary in list_artists(candidates)}
    # ライブラリの変更で消えたアーティストが選択されたまま残ると、選択欄がエラーになるため外す
    selected_before = st.session_state.get(_ARTISTS_KEY, [])
    if any(name not in artists for name in selected_before):
        st.session_state[_ARTISTS_KEY] = [name for name in selected_before if name in artists]

    if not artists:
        st.info("アーティスト名のタグが付いた曲がありません。")

    selected = st.multiselect(
        "アーティスト（複数選べます）",
        list(artists),
        format_func=lambda name: f"{name}（{artists[name]} 曲）",
        placeholder="アーティスト名で検索",
        key=_ARTISTS_KEY,
    )
    st.caption(
        "タグのアーティスト名が一致する曲を選びます（「feat.」などを含む表記は別のアーティストとして扱います）。"
    )
    mood = st.selectbox(
        "雰囲気（おまかせなら雰囲気で絞り込みません）",
        PLAYLIST_MOODS,
        format_func=MOOD_LABELS.__getitem__,
        key=_ARTIST_MOOD_KEY,
        filter_mode=None,
    )
    by_count = st.radio("長さの指定", ["再生時間", "曲数"], horizontal=True, key="pl_length") == "曲数"
    duration, count = _length_inputs(defaults)
    st.caption("「長さの指定」で選んだ方の値を使います。")
    options = _detail_options(defaults, ctx, ranking="適合度順", preference_note=None)
    if not _generate_button():
        return None

    if not selected:
        st.error("アーティストを 1 つ以上選んでください。")
        return None

    if not _genres_are_valid(options):
        return None

    return ArtistRequest(
        artists=tuple(selected),
        mood=mood or Mood.OMAKASE,
        duration_min=None if by_count else duration,
        track_count=count if by_count else None,
        genre_mode=options.genre_mode,
        genres=options.genres,
        children_mode=options.children_mode,
        min_track_sec=options.min_track_sec,
        novelty=options.novelty,
        randomness=options.randomness,
        avoid_same_artist=options.avoid_same_artist,
        avoid_duplicates=options.avoid_duplicates,
        smooth_order=options.smooth_order,
        seed=options.seed,
    )


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
    with st.container(horizontal=True):
        duration = st.slider(
            "再生時間（分）",
            defaults.min_duration_min,
            defaults.max_duration_min,
            step=5,
            key="pl_duration",
        )
        count = st.number_input(
            "曲数",
            min_value=1,
            max_value=defaults.max_track_count,
            key="pl_count",
            width=140,
        )

    return int(duration), int(count)


def _detail_options(
    defaults: PlaylistDefaults, ctx: PageContext, *, ranking: str, preference_note: str | None
) -> _Options:
    """詳細設定の欄。

    Args:
        defaults: 生成の既定値と制約。
        ctx: 画面の共通情報。
        ranking: ランダム性が 0 のときの選曲の順（例: 適合度順）。ランダム性の説明に使う。
        preference_note: 意外性・ランダム性を使わない場合に、入力欄を無効にして表示する説明。使う場合は None。
    """
    with st.expander("詳細設定", key="playlist-options"):
        with st.popover("選曲設定のヒント", icon=":material/info:"):
            st.markdown(
                "**意外性**は、これまでプレイリストに使っていない曲を選びやすくする設定です。\n\n"
                "**ランダム性**を上げると、適合度が高い曲以外も選ばれやすくなります。\n\n"
                "**seed** は抽選の番号です。同じ曲・分類結果・利用履歴・条件なら、同じ番号で選曲を再現できます。\n\n"
                "条件を変えただけでは編集中の曲は変わりません。「プレイリストを生成」で反映します。"
            )

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
        children_mode = st.radio(
            "子供向けの曲",
            list(ChildrenMode),
            format_func=CHILDREN_MODE_LABELS.__getitem__,
            horizontal=True,
            key="pl_children",
            help=(
                "音楽ライブラリ画面で、Jev がアルバムごとに判定した結果を使います。"
                "未判定の曲は子供向けではないものとして扱います。"
            ),
        )
        min_track_sec = st.number_input(
            "短い曲を除外（この秒数より短い曲を選ばない。0 なら除外しない）",
            min_value=0,
            max_value=defaults.max_min_track_sec,
            step=10,
            key="pl_min_track_sec",
            help="ジングルや効果音などの短い曲を外すときに使います。再生時間が不明な曲も除外します。",
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
        children_mode=children_mode or ChildrenMode.EXCLUDE,
        min_track_sec=int(min_track_sec),
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


def _generate(
    candidates: list[Candidate], request: Request, defaults: PlaylistDefaults, source: ClassificationSource
) -> None:
    """プレイリストを生成し、編集中の曲を置き換える。

    Args:
        candidates: 選曲の候補。
        request: 生成条件。
        defaults: 生成の既定値と制約。
        source: 候補の読み込みに使った分類結果の種類。
    """
    result: Result
    if isinstance(request, CircleRequest):
        result = generate_circle_playlist(candidates, request, defaults)
    elif isinstance(request, ArtistRequest):
        result = generate_artist_playlist(candidates, request, defaults)
    else:
        result = generate_playlist(candidates, request, defaults)

    st.session_state[_RESULT_KEY] = result
    st.session_state[_DRAFT_KEY] = list(result.tracks)
    st.session_state.pop(_SAVED_KEY, None)
    st.session_state[_SOURCE_KEY] = source
    close_player()
    st.session_state["pl_name"] = f"{_playlist_title(request)} {datetime.now():%Y-%m-%d %H:%M}"


def _playlist_title(request: Request) -> str:
    """プレイリストの既定の名前と見出しに使う、条件の短い説明。"""
    if isinstance(request, CircleRequest):
        return f"{_center_label(request.mood)}周辺"

    if isinstance(request, ArtistRequest):
        return _artist_title(request)

    return MOOD_LABELS[request.mood]


def _artist_title(request: ArtistRequest) -> str:
    """アーティストで選ぶ場合の短い説明。3 組以上は先頭の 2 組と残りの数で表す。"""
    names = list(request.artists)
    shown = names[:_TITLE_ARTISTS]
    title = "・".join(shown)
    if len(names) > len(shown):
        title += f" ほか {len(names) - len(shown)} 組"

    if request.mood != Mood.OMAKASE:
        title += f"（{MOOD_LABELS[request.mood]}）"

    return title


def _shortfall_notice(
    candidates: list[Candidate], result: Result, defaults: PlaylistDefaults, source: ClassificationSource
) -> None:
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
        _generate(candidates, replace(request, relaxed=True), defaults, source)
        st.rerun()


def _draft_editor(ctx: PageContext, candidates: list[Candidate], result: Result) -> None:
    draft: list[SelectedTrack] = st.session_state.get(_DRAFT_KEY, [])
    request = result.request
    if isinstance(request, CircleRequest):
        note = f"（マップの円・半径 {request.radius * DISPLAY_SCALE:.1f}）"
        score_label = "中心への近さ"
    else:
        note = "（条件を緩和）" if request.relaxed else ""
        score_label = "適合度"

    source = _generation_source(ctx)
    st.subheader(f"{_playlist_title(request)}のプレイリスト")
    st.write(f"{len(draft)} 曲・合計 {format_duration(total_duration(draft))}{note}")
    if draft:
        remaining = track_list(ctx, draft, request.to_dict(), source)
        if remaining is not None:
            _update_draft(remaining)

        _edit_track(draft)
    else:
        st.info("曲がありません。「曲を追加する」か、条件を変えて生成してください。")

    # 生成後に設定で分類結果の種類を変えても、追加する曲は生成時の種類で選ぶ。
    # 除外や保存で記録する種類と、曲ごとの分類結果の写しを食い違わせないため
    if source != ctx.classification_source and ctx.library_root is not None:
        candidates = load_candidates(ctx.conn, ctx.library_root, source)

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

    # 曲一覧を下までスクロールした位置から、上のタブに戻らずに保存へ進めるようにする
    st.caption(f"編集が終わったら「{_SAVE_TAB}」タブで名前を付けて保存します。")
    st.button(
        "保存する",
        type="primary",
        icon=":material/save:",
        disabled=not draft,
        width="stretch",
        on_click=_open_save_tab,
        key="pl-open-save-tab",
    )


def _open_save_tab() -> None:
    st.session_state[_TABS_KEY] = _SAVE_TAB


def _edit_track(draft: list[SelectedTrack]) -> None:
    ids = [item.track.id for item in draft]
    selected_id = st.selectbox(
        "並べ替える曲",
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


def _save_section(ctx: PageContext, result: Result, on_manage: Callable[[], None] | None) -> None:
    draft: list[SelectedTrack] = st.session_state.get(_DRAFT_KEY, [])
    st.subheader("名前を付けて保存")
    st.caption(f"{len(draft)} 曲・合計 {format_duration(total_duration(draft))}")
    with st.popover("保存について", icon=":material/info:"):
        st.write(
            "名前の入力や Enter キーだけでは保存しません。「プレイリストを保存」を押してください。"
            "保存するたびに新しいプレイリストを作ります。保存したプレイリストの一覧・削除・Volumio への登録は、"
            "プレイリスト管理画面で行います。"
        )

    name = st.text_input("プレイリスト名", key="pl_name", help="入力後に「プレイリストを保存」を押してください。")
    # 名前の確定前にボタンを無効化すると、キーボードを閉じた後にもう一度タップする必要がある。
    if st.button("プレイリストを保存", type="primary", disabled=not draft, width="stretch"):
        if not name.strip():
            st.error("プレイリスト名を入力してください。")
            return

        playlist_id = _save(ctx, name, result.request, draft)
        st.session_state[_SAVED_KEY] = playlist_id
        st.session_state[_SAVED_NAME_KEY] = name.strip()
        st.success(f"「{name.strip()}」を保存しました。プレイリスト管理画面から Volumio に登録できます。")
    elif st.session_state.get(_SAVED_KEY) is not None and st.session_state.get(_SAVED_NAME_KEY) == name.strip():
        st.caption("保存済みです。Volumio との照合・登録はプレイリスト管理画面で行えます。")
    else:
        st.caption("編集中の内容は未保存です。曲を変更したら、もう一度保存してください。")

    _manage_button(on_manage, key="pl-manage-playlists")


def _manage_button(on_manage: Callable[[], None] | None, *, key: str) -> None:
    st.button(
        "プレイリスト管理を開く",
        icon=":material/folder_open:",
        on_click=on_manage,
        disabled=on_manage is None,
        key=key,
    )


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
                classification_source=_generation_source(ctx).value,
                total_duration_sec=total_duration(draft),
                entries=[PlaylistEntry(t.track.id, t.score, t.added_manually) for t in draft],
            ),
        )


def _generation_source(ctx: PageContext) -> ClassificationSource:
    """編集中の曲を選んだときの分類結果の種類。記録がなければ現在の設定を使う。"""
    source = st.session_state.get(_SOURCE_KEY)
    return source if isinstance(source, ClassificationSource) else ctx.classification_source
