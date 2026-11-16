"""評価画面（分類結果の評価、プレイリストの満足度）のテスト。"""

import sqlite3
import threading
import time
from pathlib import Path

import pytest
import soundfile as sf
from conftest import SAMPLE_RATE, sine
from streamlit.testing.v1 import AppTest
from test_gui import click, run_view

from volumio_playlist_studio.models import (
    Attribute,
    Genre,
    TitleSource,
    TrackMetadata,
)
from volumio_playlist_studio.storage import settings_store
from volumio_playlist_studio.storage.database import transaction
from volumio_playlist_studio.storage.evaluations import Split, Verdict, add_to_evaluation_set, load_labels
from volumio_playlist_studio.storage.playlists import list_playlists
from volumio_playlist_studio.storage.tracks import upsert_track


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
    # 属性の評価は任意のため、折りたたんである
    ratings = next(e for e in at.expander if e.label.startswith("属性の評価"))
    assert ratings.label == "属性の評価（任意。入力済み 0 / 7）"
    assert not ratings.proto.expanded
    next(s for s in at.select_slider if s.label == "リラックス度").set_value(4)
    click(at, "評価を保存")
    labels = [lb for lb in load_labels(demo).values() if lb.is_evaluated]
    assert [(lb.genre_label, lb.mood_label, lb.mood_verdict) for lb in labels] == [(Genre.WORLD, None, Verdict.PENDING)]
    assert labels[0].ratings == {Attribute.RELAXATION: 4}
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
    next(s for s in at.select_slider if s.label == "リラックス度").set_value(5)
    next(s for s in at.select_slider if s.label == "エネルギー感").set_value(1)
    click(at, "評価を保存")
    [(track_id, saved)] = [(k, v) for k, v in load_labels(demo).items() if v.is_evaluated]
    assert saved.ratings == {Attribute.RELAXATION: 5, Attribute.ENERGY: 1}

    # 評価済みの曲を開き直すと、保存済みの評価が入力欄に入っている
    at = run_view("evaluation_page")
    next(c for c in at.checkbox if c.label == "未評価の曲だけ表示").uncheck().run()
    next(s for s in at.selectbox if s.label == "曲").set_value(track_id).run()
    assert next(r for r in at.radio if r.label == "ジャンルの判定は").value == Verdict.INCORRECT
    assert next(s for s in at.selectbox if s.label == "誤りの場合、正しいジャンル").value == Genre.WORLD
    assert next(r for r in at.radio if r.label == "ムードの判定は").value == Verdict.PENDING
    assert next(s for s in at.select_slider if s.label == "リラックス度").value == 5
    assert next(s for s in at.select_slider if s.label == "エネルギー感").value == 1
    assert next(s for s in at.select_slider if s.label == "明るさ").value == "-"
    assert any(e.label == "属性の評価（任意。入力済み 2 / 7）" for e in at.expander)

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
    assert updated.ratings == {Attribute.RELAXATION: 5, Attribute.ENERGY: 1}
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


def _meta(rel_path: str, title: str) -> TrackMetadata:
    return TrackMetadata(
        rel_path=rel_path, title=title, artist="歌手", album=None, genre=None, year=None, track_number=None,
        duration_sec=1.0, bitrate=None, sample_rate=None, channels=None, codec=None, file_size=100, mtime_ns=1,
        title_source=TitleSource.TAG, tags_repaired=False, quick_hash=None,
    )  # fmt: skip


@pytest.fixture
def playable(app_db: sqlite3.Connection, tmp_path: Path) -> dict[str, int]:
    """再生できる曲・ファイルがない曲・再生できない形式の曲を評価対象にした、架空のライブラリ。"""
    root = tmp_path / "music"
    (root / "album").mkdir(parents=True)
    sf.write(root / "album" / "a.wav", sine(1.0), SAMPLE_RATE)
    with transaction(app_db):
        ids = {
            title: upsert_track(app_db, str(root), _meta(rel_path, title), None)
            for title, rel_path in [("再生できる曲", "album/a.wav"), ("消えた曲", "album/b.flac"), ("未対応", "c.wma")]
        }
        add_to_evaluation_set(app_db, dict.fromkeys(ids.values(), Split.TUNING))
        settings_store.set_value(app_db, settings_store.MUSIC_ROOT_KEY, str(root))

    return ids


_LOADING = "曲を読み込んでいます"
# 曲の読み込みが終わるのを待つ上限（回数 × 間隔）
_WAIT_TRIES = 200
_WAIT_INTERVAL_SEC = 0.05


def _open_track(track_id: int) -> AppTest:
    at = run_view("evaluation_page")
    next(s for s in at.selectbox if s.label == "曲").set_value(track_id).run()
    assert not at.exception
    return at


def _is_loading(at: AppTest) -> bool:
    return any(_LOADING in c.value for c in at.caption)


def _wait_loaded(at: AppTest) -> AppTest:
    """バックグラウンドでの曲の読み込みが終わるまで、画面を再実行する。"""
    for _ in range(_WAIT_TRIES):
        if not _is_loading(at):
            return at

        time.sleep(_WAIT_INTERVAL_SEC)
        at.run()
        assert not at.exception

    raise AssertionError("track did not finish loading")


def test_evaluation_plays_selected_track(playable: dict[str, int]) -> None:
    at = _wait_loaded(_open_track(playable["再生できる曲"]))

    [player] = at.get("audio")
    assert player.proto.url

    # 曲を変えると、その曲が再生できない理由を表示する
    at = _wait_loaded(_open_track(playable["消えた曲"]))
    assert not at.get("audio")
    assert any("ファイルが見つからない" in w.value for w in at.warning)
    at = _open_track(playable["未対応"])
    assert not at.get("audio")
    assert any("この形式の曲は再生できません" in c.value for c in at.caption)


def test_slow_track_loading_does_not_block_evaluation(
    app_db: sqlite3.Connection, playable: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """ネットワーク上のライブラリなどで読み込みが遅くても、読み込み中に評価を入力・保存できる。"""
    release = threading.Event()
    read_bytes = Path.read_bytes

    def slow_read_bytes(path: Path) -> bytes:
        release.wait(timeout=30)
        return read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", slow_read_bytes)
    track_id = playable["再生できる曲"]
    at = _open_track(track_id)

    assert _is_loading(at)
    assert not at.get("audio")
    next(r for r in at.radio if r.label == "ジャンルの判定は").set_value(Verdict.PENDING)
    next(r for r in at.radio if r.label == "ムードの判定は").set_value(Verdict.PENDING)
    click(at, "評価を保存")
    assert load_labels(app_db)[track_id].is_evaluated

    # 読み終わると、評価済みの曲を開き直したときにプレーヤーが表示される
    release.set()
    next(c for c in at.checkbox if c.label == "未評価の曲だけ表示").uncheck().run()
    next(s for s in at.selectbox if s.label == "曲").set_value(track_id).run()
    at = _wait_loaded(at)
    assert len(at.get("audio")) == 1


def test_unreadable_track_shows_reason(playable: dict[str, int], monkeypatch: pytest.MonkeyPatch) -> None:
    def denied(path: Path) -> bytes:
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr(Path, "read_bytes", denied)
    at = _wait_loaded(_open_track(playable["再生できる曲"]))

    assert not at.get("audio")
    assert any("曲のファイルを読めない" in w.value for w in at.warning)
