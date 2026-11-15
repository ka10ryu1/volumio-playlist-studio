"""評価画面（分類結果の評価、プレイリストの満足度）のテスト。"""

import sqlite3

from test_gui import click, run_view

from volumio_playlist_studio.models import (
    Genre,
)
from volumio_playlist_studio.storage.evaluations import Verdict, load_labels
from volumio_playlist_studio.storage.playlists import list_playlists


def test_evaluation_flow(demo: sqlite3.Connection) -> None:
    at = run_view("evaluation_page")
    click(at, "無作為に選んで追加")
    assert any("評価対象: 40 曲" in m.value for m in at.markdown)

    next(r for r in at.radio if r.label == "ジャンルの判定は").set_value(Verdict.INCORRECT)
    next(s for s in at.selectbox if s.label == "誤りの場合、正しいジャンル").set_value(Genre.WORLD)
    next(r for r in at.radio if r.label == "ムードの判定は").set_value(Verdict.PENDING)
    # 正しいムードの候補は、分類結果に現れるムードだけ（プレイリスト専用の雰囲気は含めない）
    mood_options = next(s for s in at.selectbox if s.label == "誤りの場合、正しいムード").options
    assert mood_options == ["リラックス", "集中・作業", "元気", "穏やかな夜"]
    at.select_slider[0].set_value(4)
    click(at, "評価を保存")
    labels = [lb for lb in load_labels(demo).values() if lb.is_evaluated]
    assert [(lb.genre_label, lb.mood_label, lb.mood_verdict) for lb in labels] == [(Genre.WORLD, None, Verdict.PENDING)]
    assert len(at.get("download_button")) == 1

    assert any("指標" in s.value for s in at.subheader)


def test_evaluation_rejects_inconsistent_input(demo: sqlite3.Connection) -> None:
    at = run_view("evaluation_page")
    click(at, "無作為に選んで追加")
    # 「誤り」なのに正しいジャンルを選ばない
    next(r for r in at.radio if r.label == "ジャンルの判定は").set_value(Verdict.INCORRECT)
    next(r for r in at.radio if r.label == "ムードの判定は").set_value(Verdict.PENDING)

    click(at, "評価を保存")

    assert any("保存できます" in e.value for e in at.error)


def test_evaluation_requires_explicit_verdicts(demo: sqlite3.Connection) -> None:
    at = run_view("evaluation_page")
    click(at, "無作為に選んで追加")

    # 未評価の曲では判定を選んでいないため、保存できない
    assert next(r for r in at.radio if r.label == "ジャンルの判定は").value is None
    click(at, "評価を保存")

    assert any("判定を選んでください" in e.value for e in at.error)


def test_reopened_evaluation_keeps_saved_labels(demo: sqlite3.Connection) -> None:
    at = run_view("evaluation_page")
    click(at, "無作為に選んで追加")
    next(r for r in at.radio if r.label == "ジャンルの判定は").set_value(Verdict.INCORRECT)
    next(s for s in at.selectbox if s.label == "誤りの場合、正しいジャンル").set_value(Genre.WORLD)
    next(r for r in at.radio if r.label == "ムードの判定は").set_value(Verdict.PENDING)
    click(at, "評価を保存")
    [(track_id, saved)] = [(k, v) for k, v in load_labels(demo).items() if v.is_evaluated]

    # 評価済みの曲を開き直すと、保存済みの評価が入力欄に入っている
    next(c for c in at.checkbox if c.label == "未評価の曲だけ表示").uncheck().run()
    next(s for s in at.selectbox if s.label == "曲").set_value(track_id).run()
    assert next(r for r in at.radio if r.label == "ジャンルの判定は").value == Verdict.INCORRECT
    assert next(s for s in at.selectbox if s.label == "誤りの場合、正しいジャンル").value == Genre.WORLD
    assert next(r for r in at.radio if r.label == "ムードの判定は").value == Verdict.PENDING

    # メモだけを変えて保存しても、人手の正解は変わらない
    next(t for t in at.text_input if t.label == "メモ").input("メモだけ追記")
    click(at, "評価を保存")
    updated = load_labels(demo)[track_id]
    assert (updated.genre_verdict, updated.genre_label, updated.mood_verdict) == (
        saved.genre_verdict,
        saved.genre_label,
        saved.mood_verdict,
    )
    assert updated.note == "メモだけ追記"
    assert updated.classification_snapshot is not None


def test_playlist_feedback(demo: sqlite3.Connection) -> None:
    at = run_view("playlist_page")
    click(at, "プレイリストを生成")
    click(at, "プレイリストを保存")
    at = run_view("evaluation_page")

    next(s for s in at.slider if s.label.startswith("満足度")).set_value(5)
    assert at.multiselect[0].placeholder == "合わなかった曲を選択"
    at.multiselect[0].select(0)
    click(at, "評価を保存")

    [saved] = list_playlists(demo)
    assert saved.satisfaction == 5
    assert saved.entries[0].unsuitable
