"""プレイリスト作成画面の「雰囲気マップ」。既存の雰囲気と曲を明るさ × エネルギー感の 2 軸に描く。

自由記述の雰囲気（▲）も描く。選択中の雰囲気は色を変えて強調する。マップの円で選ぶ場合は、円と、選ばれる曲も描く。
位置は 0〜1 で計算・保存しているが、画面では `DISPLAY_SCALE` 倍（0〜10）で表示する。
"""

import math
from dataclasses import dataclass
from typing import Literal

import altair as alt
import streamlit as st

from volumio_playlist_studio.gui.widgets import genre_label
from volumio_playlist_studio.models import ATTRIBUTE_LABELS, MOOD_LABELS, CustomMood, Mood
from volumio_playlist_studio.mood_map import (
    DISPLAY_SCALE,
    MOOD_POSITIONS,
    X_ATTRIBUTE,
    Y_ATTRIBUTE,
    CircleRequest,
    TrackMap,
    build_track_map,
)
from volumio_playlist_studio.playlist import Candidate, SelectedTrack

_X_TITLE = f"{ATTRIBUTE_LABELS[X_ATTRIBUTE]}（左: 暗い・切ない / 右: 明るい・楽しい）"
_Y_TITLE = f"{ATTRIBUTE_LABELS[Y_ATTRIBUTE]}（下: 静か / 上: 激しい）"
# 縦軸と横軸の 1 目盛りの長さを揃え、円が楕円に見えないようにする。軸の目盛り・タイトルの分を見込み、
# 描画領域がほぼ正方形（約 590 ピクセル四方）になる大きさにした。幅が足りない場合は横だけが縮んで縦長になるため、
# スマートフォンでは小さめ（約 280 ピクセル四方）に描く
_CHART_SIZE = (665, 690)
_COMPACT_CHART_SIZE = (355, 380)
# スマートフォンのブラウザの User-Agent に含まれる文字列（主要なブラウザが付ける）
_MOBILE_USER_AGENT_MARK = "Mobi"
# 雰囲気の名前は既定では点の右上に書く。図の外にはみ出したり、同じ高さの雰囲気
# （ワークアウトと元気、集中・作業とカフェ）の名前と重なったりしないよう、一部は位置を変える
_LABEL_LEFT = "left"
_LABEL_BELOW = "below"
_LABEL_RIGHT = "right"
_LABEL_POSITIONS: dict[Mood, str] = {
    Mood.MORNING: _LABEL_LEFT,
    Mood.WORKOUT: _LABEL_LEFT,
    Mood.FOCUS: _LABEL_LEFT,
    Mood.CAFE: _LABEL_BELOW,
}
# 円を描くときの頂点の数
_CIRCLE_VERTICES = 72
# 名前の位置ごとの、文字の揃え方と点からのずれ（ピクセル）
_LABEL_STYLES: dict[str, tuple[Literal["left", "center", "right"], int, int]] = {
    _LABEL_RIGHT: ("left", 8, -8),
    _LABEL_LEFT: ("right", -8, -8),
    _LABEL_BELOW: ("center", 0, 16),
}
# 名前の下に背景色の縁取りを描き、円の線や曲の点と重なっても読めるようにする（ピクセル）
_LABEL_HALO_WIDTH = 3
# 雰囲気の印の色。曲の点はテーマの既定色（青）で描くため、それと区別できる色にする。
# 選択中の雰囲気はテーマの主色で描く（取得できなければ _SELECTED_FALLBACK_COLOR）
_MOOD_COLOR = "#3f4a5a"
_CUSTOM_MOOD_COLOR = "#1e8f5a"
_SELECTED_FALLBACK_COLOR = "#c7354f"
_MARKER_SIZE = 140
_SELECTED_MARKER_SIZE = 260


type CirclePreview = tuple[CircleRequest, list[SelectedTrack]]


@dataclass(frozen=True)
class _MarkerStyle:
    """雰囲気の印と名前の描き方。text_color が None なら名前の色はテーマに任せる。"""

    shape: str
    color: str
    size: int
    text_color: str | None
    bold: bool = False


def render(
    candidates: list[Candidate],
    custom_moods: list[CustomMood],
    preview: CirclePreview | None = None,
    selected: Mood | CustomMood | None = None,
) -> None:
    """雰囲気マップを表示する。

    Args:
        candidates: 選曲の候補。
        custom_moods: 自由記述の雰囲気。
        preview: マップの円で選ぶ場合の、現在の条件と選ばれる曲。円を描き、選ばれる曲を強調する。
        selected: 選択中の雰囲気。色を変えて強調する。マップにない雰囲気（おまかせなど）は強調しない。
    """
    track_map = build_track_map(candidates)
    on_map = isinstance(selected, CustomMood) or selected in MOOD_POSITIONS
    st.caption(_summary(track_map, bool(custom_moods), preview is not None, on_map))
    if not track_map.points:
        st.info(
            "マップに載る曲がありません。明るさはルールベースでは判定しないため、"
            "音楽ライブラリ画面の「Jev で判定する」で判定した曲だけが載ります。"
        )

    width, height = _COMPACT_CHART_SIZE if _is_mobile() else _CHART_SIZE
    st.altair_chart(_chart(track_map, custom_moods, preview, selected), width=width, height=height)


def _is_mobile() -> bool:
    """スマートフォンからの表示か。画面の幅は取得できないため、User-Agent で判断する。"""
    return _MOBILE_USER_AGENT_MARK in st.context.headers.get("User-Agent", "")


def _summary(track_map: TrackMap, with_custom_moods: bool, with_circle: bool, with_selected: bool) -> str:
    excluded = []
    if track_map.held:
        excluded.append(f"明るさかエネルギー感が判定保留 {track_map.held} 曲")

    if track_map.unclassified:
        excluded.append(f"未分類 {track_map.unclassified} 曲")

    summary = f"マップに載っている曲: {len(track_map.points)} 曲。"
    if excluded:
        summary += f"載っていない曲: {'、'.join(excluded)}。"

    summary += "◆ は雰囲気の位置です。"
    if with_custom_moods:
        summary += "▲ は自由記述で作った雰囲気の位置です。"

    if with_selected:
        summary += "選択中の雰囲気は色を変えて大きく示します。"

    if with_circle:
        summary += "濃い点が選ばれる曲です（ジャンルの指定・上限・重複の除外を反映）。"

    return summary


def _chart(
    track_map: TrackMap,
    custom_moods: list[CustomMood],
    preview: CirclePreview | None,
    selected: Mood | CustomMood | None,
) -> alt.LayerChart:
    scale = alt.Scale(domain=[0, DISPLAY_SCALE], nice=False)
    # 目盛りは整数だけにする（大きく描くと、既定では 0.5 刻みになる）
    axis = alt.Axis(values=list(range(DISPLAY_SCALE + 1)))
    x = alt.X("x:Q", title=_X_TITLE, scale=scale, axis=axis)
    y = alt.Y("y:Q", title=_Y_TITLE, scale=scale, axis=axis)
    chosen = set() if preview is None else {t.track.id for t in preview[1]}
    tracks = alt.Chart(
        alt.InlineData(
            values=[
                {
                    "x": p.point.x * DISPLAY_SCALE,
                    "y": p.point.y * DISPLAY_SCALE,
                    "曲名": p.candidate.track.display_title,
                    "アーティスト": p.candidate.track.display_artist,
                    "ジャンル": genre_label(p.candidate.genre),
                    "chosen": p.candidate.track.id in chosen,
                }
                for p in track_map.points
            ]
        )
    ).encode(x, y, tooltip=["曲名:N", "アーティスト:N", "ジャンル:N"])
    if preview is None:
        tracks = tracks.mark_circle(size=30, opacity=0.35)
    else:
        # 色はテーマに任せ、選ばれる曲は不透明度と大きさで区別する
        tracks = tracks.mark_circle().encode(
            opacity=alt.condition(alt.datum.chosen, alt.value(0.9), alt.value(0.15)),
            size=alt.condition(alt.datum.chosen, alt.value(50), alt.value(25)),
        )

    preset_rows = [_mood_row(mood) for mood in MOOD_POSITIONS if mood != selected]
    custom_rows = [_custom_mood_row(mood) for mood in custom_moods if not _same_custom_mood(mood, selected)]
    layers: list[alt.Chart] = [
        tracks,
        *_marker_layers(preset_rows, x, y, _MarkerStyle("diamond", _MOOD_COLOR, _MARKER_SIZE, None)),
        *_marker_layers(
            custom_rows, x, y, _MarkerStyle("triangle-up", _CUSTOM_MOOD_COLOR, _MARKER_SIZE, _CUSTOM_MOOD_COLOR)
        ),
    ]
    selected_layers = _selected_layers(selected, x, y)
    if preview is not None:
        layers.append(_circle_layer(preview[0], x, y))

    # 選択中の雰囲気は、ほかの印や円の線に隠れないよう最後に描く
    layers += selected_layers
    return alt.LayerChart(layer=layers)


def _mood_row(mood: Mood) -> dict[str, object]:
    point = MOOD_POSITIONS[mood]
    return {
        "x": point.x * DISPLAY_SCALE,
        "y": point.y * DISPLAY_SCALE,
        "雰囲気": MOOD_LABELS[mood],
        "名前の位置": _LABEL_POSITIONS.get(mood, _LABEL_RIGHT),
    }


def _custom_mood_row(mood: CustomMood) -> dict[str, object]:
    """自由記述の雰囲気。名前は点の右上に書き、説明文はカーソルを合わせたときに表示する。"""
    return {
        "x": mood.x * DISPLAY_SCALE,
        "y": mood.y * DISPLAY_SCALE,
        "雰囲気": mood.name,
        "説明文": mood.description,
        "名前の位置": _LABEL_RIGHT,
    }


def _same_custom_mood(mood: CustomMood, selected: Mood | CustomMood | None) -> bool:
    return isinstance(selected, CustomMood) and selected.id == mood.id


def _selected_layers(selected: Mood | CustomMood | None, x: alt.X, y: alt.Y) -> list[alt.Chart]:
    """選択中の雰囲気を、テーマの主色で大きめに描く。形は既存の雰囲気（◆）か自由記述（▲）かで変えない。"""
    if isinstance(selected, CustomMood):
        row, shape = _custom_mood_row(selected), "triangle-up"
    elif selected in MOOD_POSITIONS:
        row, shape = _mood_row(selected), "diamond"
    else:
        return []

    primary = st.get_option("theme.primaryColor")
    color = primary if isinstance(primary, str) else _SELECTED_FALLBACK_COLOR
    return _marker_layers([row], x, y, _MarkerStyle(shape, color, _SELECTED_MARKER_SIZE, color, bold=True))


def _marker_layers(rows: list[dict[str, object]], x: alt.X, y: alt.Y, style: _MarkerStyle) -> list[alt.Chart]:
    """雰囲気の印と名前のレイヤーを返す。行がなければ何も描かない。"""
    if not rows:
        return []

    tooltip = ["雰囲気:N", "説明文:N"] if any("説明文" in row for row in rows) else ["雰囲気:N"]
    chart = alt.Chart(alt.InlineData(values=rows)).encode(x, y, tooltip=tooltip)
    markers = chart.mark_point(shape=style.shape, size=style.size, filled=True, opacity=1.0, color=style.color)
    labels = chart.encode(text="雰囲気:N")
    positions = dict.fromkeys(str(row["名前の位置"]) for row in rows)
    label_layers = [
        layer
        for position in positions
        for layer in _label_layers(
            labels.transform_filter(alt.datum["名前の位置"] == position), _LABEL_STYLES[position], style
        )
    ]
    return [markers, *label_layers]


def _circle_layer(request: CircleRequest, x: alt.X, y: alt.Y) -> alt.Chart:
    """円周を折れ線で描く。選ばれる範囲はこの線の内側と一致する。"""
    center = request.center
    vertices = [
        {
            "x": (center.x + request.radius * math.cos(2 * math.pi * i / _CIRCLE_VERTICES)) * DISPLAY_SCALE,
            "y": (center.y + request.radius * math.sin(2 * math.pi * i / _CIRCLE_VERTICES)) * DISPLAY_SCALE,
            "order": i,
        }
        for i in range(_CIRCLE_VERTICES + 1)
    ]
    # Altair の型定義では mark_line の戻り値が Any になるため、Chart として受ける。マップの外にはみ出す部分は描かない
    line: alt.Chart = alt.Chart(alt.InlineData(values=vertices)).mark_line(clip=True, strokeDash=[6, 4])
    return line.encode(x, y, order="order:Q")


def _label_layers(
    labels: alt.Chart, position: tuple[Literal["left", "center", "right"], int, int], style: _MarkerStyle
) -> list[alt.Chart]:
    """名前を書くレイヤーを返す。テーマの背景色が分かる場合は、その色の縁取りを下に重ねる。"""
    align, dx, dy = position
    weight: Literal["bold", "normal"] = "bold" if style.bold else "normal"
    if style.text_color is None:
        text = labels.mark_text(align=align, dx=dx, dy=dy, fontWeight=weight)
    else:
        text = labels.mark_text(align=align, dx=dx, dy=dy, fontWeight=weight, color=style.text_color)

    background = st.get_option("theme.backgroundColor")
    if not isinstance(background, str):
        return [text]

    halo = labels.mark_text(
        align=align,
        dx=dx,
        dy=dy,
        fontWeight=weight,
        stroke=background,
        strokeWidth=_LABEL_HALO_WIDTH,
        strokeJoin="round",
    )
    return [halo, text]
