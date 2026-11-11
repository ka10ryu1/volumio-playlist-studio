"""プレイリスト作成画面の「雰囲気マップ」。既存の雰囲気と曲を明るさ × エネルギー感の 2 軸に描く。

マップの円で選ぶ場合は、円と、選ばれる曲も描く。
"""

import math
from typing import Literal

import altair as alt
import streamlit as st

from volumio_playlist_studio.gui.widgets import genre_label
from volumio_playlist_studio.models import ATTRIBUTE_LABELS, MOOD_LABELS, Mood
from volumio_playlist_studio.mood_map import (
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
_CHART_HEIGHT = 420
# 雰囲気の名前は既定では点の右上に書く。スマートフォンの狭い幅でも、図の外にはみ出したり、
# 同じ高さの雰囲気（ワークアウトと元気、集中・作業とカフェ）の名前と重なったりしないよう、一部は位置を変える
_LABEL_LEFT = "left"
_LABEL_BELOW = "below"
_LABEL_POSITIONS: dict[Mood, str] = {
    Mood.MORNING: _LABEL_LEFT,
    Mood.WORKOUT: _LABEL_LEFT,
    Mood.FOCUS: _LABEL_LEFT,
    Mood.CAFE: _LABEL_BELOW,
}
_LABEL_RIGHT = "right"
# 円を描くときの頂点の数
_CIRCLE_VERTICES = 72
# 名前の位置ごとの、文字の揃え方と点からのずれ（ピクセル）
_LABEL_STYLES: dict[str, tuple[Literal["left", "center", "right"], int, int]] = {
    _LABEL_RIGHT: ("left", 8, -8),
    _LABEL_LEFT: ("right", -8, -8),
    _LABEL_BELOW: ("center", 0, 16),
}


type CirclePreview = tuple[CircleRequest, list[SelectedTrack]]


def render(candidates: list[Candidate], preview: CirclePreview | None = None) -> None:
    """雰囲気マップを表示する。

    Args:
        candidates: 選曲の候補。
        preview: マップの円で選ぶ場合の、現在の条件と選ばれる曲。円を描き、選ばれる曲を強調する。
    """
    track_map = build_track_map(candidates)
    st.caption(_summary(track_map, preview is not None))
    if not track_map.points:
        st.info(
            "マップに載る曲がありません。明るさはルールベースでは判定しないため、"
            "音楽ライブラリ画面の「Jev で判定する」で判定した曲だけが載ります。"
        )

    st.altair_chart(_chart(track_map, preview), width="stretch", height=_CHART_HEIGHT)


def _summary(track_map: TrackMap, with_circle: bool) -> str:
    excluded = []
    if track_map.held:
        excluded.append(f"明るさかエネルギー感が判定保留 {track_map.held} 曲")

    if track_map.unclassified:
        excluded.append(f"未分類 {track_map.unclassified} 曲")

    summary = f"マップに載っている曲: {len(track_map.points)} 曲。"
    if excluded:
        summary += f"載っていない曲: {'、'.join(excluded)}。"

    summary += "◆ は雰囲気の位置です。"
    if with_circle:
        summary += "濃い点が選ばれる曲です（ジャンルの指定・上限・重複の除外を反映）。"

    return summary


def _chart(track_map: TrackMap, preview: CirclePreview | None) -> alt.LayerChart:
    scale = alt.Scale(domain=[0, 1], nice=False)
    x = alt.X("x:Q", title=_X_TITLE, scale=scale)
    y = alt.Y("y:Q", title=_Y_TITLE, scale=scale)
    chosen = set() if preview is None else {t.track.id for t in preview[1]}
    tracks = alt.Chart(
        alt.InlineData(
            values=[
                {
                    "x": p.point.x,
                    "y": p.point.y,
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

    moods = alt.Chart(
        alt.InlineData(
            values=[
                {
                    "x": p.x,
                    "y": p.y,
                    "雰囲気": MOOD_LABELS[mood],
                    "名前の位置": _LABEL_POSITIONS.get(mood, _LABEL_RIGHT),
                }
                for mood, p in MOOD_POSITIONS.items()
            ]
        )
    ).encode(x, y, tooltip=["雰囲気:N"])
    # 色はテーマ（.streamlit/config.toml）に任せ、曲の点とは形・大きさ・不透明度で区別する
    markers = moods.mark_point(shape="diamond", size=140, filled=True, opacity=1.0)
    labels = moods.encode(text="雰囲気:N")
    label_layers = [
        labels.transform_filter(alt.datum["名前の位置"] == position).mark_text(align=align, dx=dx, dy=dy)
        for position, (align, dx, dy) in _LABEL_STYLES.items()
    ]
    layers: list[alt.Chart] = [tracks, markers, *label_layers]
    if preview is not None:
        layers.append(_circle_layer(preview[0], x, y))

    return alt.LayerChart(layer=layers)


def _circle_layer(request: CircleRequest, x: alt.X, y: alt.Y) -> alt.Chart:
    """円周を折れ線で描く。マップの縦横比によっては楕円に見えるが、選ばれる範囲はこの線の内側と一致する。"""
    center = request.center
    vertices = [
        {
            "x": center.x + request.radius * math.cos(2 * math.pi * i / _CIRCLE_VERTICES),
            "y": center.y + request.radius * math.sin(2 * math.pi * i / _CIRCLE_VERTICES),
            "order": i,
        }
        for i in range(_CIRCLE_VERTICES + 1)
    ]
    # Altair の型定義では mark_line の戻り値が Any になるため、Chart として受ける。マップの外にはみ出す部分は描かない
    line: alt.Chart = alt.Chart(alt.InlineData(values=vertices)).mark_line(clip=True, strokeDash=[6, 4])
    return line.encode(x, y, order="order:Q")
