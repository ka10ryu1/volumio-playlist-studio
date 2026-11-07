"""Streamlit の画面のテスト。AppTest で画面を実行し、操作と表示を確認する。

実際の外部 API・Volumio とは通信しない（Jev の実行ボタンは押さない。Volumio はモッククライアントのみ）。
"""

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from volumio_playlist_studio.jobs import BackgroundJob
from volumio_playlist_studio.models import ClassificationSource, Genre
from volumio_playlist_studio.playlist import GenreMode
from volumio_playlist_studio.sample_data import DEMO_LIBRARY_ROOT, generate_sample_audio, load_demo_library
from volumio_playlist_studio.scanner import scan_library
from volumio_playlist_studio.storage import settings_store
from volumio_playlist_studio.storage.database import connect, transaction
from volumio_playlist_studio.storage.evaluations import Verdict, load_labels
from volumio_playlist_studio.storage.playlists import list_playlists, list_registrations

APP = str(Path(__file__).parents[1] / "src" / "volumio_playlist_studio" / "gui" / "app.py")
TIMEOUT = 60


def view_script(view: str) -> None:
    """1 つの画面だけを表示するスクリプト。AppTest がこの関数のソースを実行する。"""
    import importlib

    from volumio_playlist_studio.gui.context import open_context, show_job_message, show_jobs

    ctx = open_context()
    try:
        show_job_message()
        show_jobs()
        importlib.import_module(f"volumio_playlist_studio.gui.views.{view}").render(ctx)
    finally:
        ctx.conn.close()


def lan_script(remote_ip: str | None) -> None:
    """LAN モードの PIN 入力画面だけを表示するスクリプト。テストから PIN を読めるよう session_state に入れる。"""
    import streamlit as st

    from volumio_playlist_studio.gui.lan import render_lan_page, require_pin
    from volumio_playlist_studio.lan_access import LanAccess, PinPolicy

    if "lan" not in st.session_state:
        st.session_state["lan"] = LanAccess("192.168.1.10", PinPolicy(max_failures=2))

    lan = st.session_state["lan"]
    st.session_state["test_pin"] = lan.pin
    if require_pin(lan, remote_ip):
        if remote_ip is None:
            render_lan_page(lan, 8501)
        else:
            st.write("ライブラリの画面")


@pytest.fixture
def app_db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    conn = connect(tmp_path / "app-data" / "studio.sqlite3")
    yield conn
    conn.close()


@pytest.fixture
def demo(app_db: sqlite3.Connection) -> sqlite3.Connection:
    load_demo_library(app_db)
    with transaction(app_db):
        settings_store.set_value(app_db, settings_store.MUSIC_ROOT_KEY, DEMO_LIBRARY_ROOT)

    return app_db


def run_view(view: str) -> AppTest:
    at = AppTest.from_function(view_script, args=(view,), default_timeout=TIMEOUT)
    at.run()
    assert not at.exception
    return at


def click(at: AppTest, label: str) -> AppTest:
    next(b for b in at.button if b.label == label).click().run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def wait_for_jobs(at: AppTest) -> None:
    jobs: dict[str, BackgroundJob] = at.session_state["jobs"]
    for job in jobs.values():
        job.join(timeout=TIMEOUT)


def test_app_starts_without_library_and_loads_demo() -> None:
    at = AppTest.from_file(APP, default_timeout=TIMEOUT)
    at.run()

    assert not at.exception
    assert any("未設定" in i.value for i in at.info)
    click(at, "サンプルデータで試す")

    metrics = {m.label: m.value for m in at.metric}
    assert metrics["登録楽曲数"] == "60"
    assert metrics["未解析"] == "0"


def test_library_view_with_demo_data(demo: sqlite3.Connection) -> None:
    at = run_view("library")

    assert any("楽曲一覧（60 曲）" in s.value for s in at.subheader)
    assert any("サンプルデータではスキャン" in c.value for c in at.caption)
    at.text_input[1].input("piano").run()
    assert not at.exception
    assert any("ルールベース" in m.value for m in at.markdown)


def test_library_view_scans_and_analyzes_real_folder(app_db: sqlite3.Connection, tmp_path: Path) -> None:
    folder = tmp_path / "Music"
    generate_sample_audio(folder, seconds=8.0)
    at = run_view("library")
    at.text_input[0].input(str(folder))
    click(at, "保存")
    assert settings_store.get_str(app_db, settings_store.MUSIC_ROOT_KEY) == folder.as_posix()

    click(at, "スキャン（追加・更新を反映）")
    wait_for_jobs(at)
    at.run()
    assert any("スキャン が完了しました" in s.value for s in at.success)
    assert any("楽曲一覧（4 曲）" in s.value for s in at.subheader)

    click(at, "未解析の曲を解析")
    wait_for_jobs(at)
    at.run()
    assert any("音響解析 が完了しました" in s.value and "解析 4 曲" in s.value for s in at.success)


def test_music_folder_form_rejects_invalid_paths(app_db: sqlite3.Connection, tmp_path: Path) -> None:
    at = run_view("library")

    at.text_input[0].input("relative/path")
    click(at, "保存")
    assert any("絶対パス" in e.value for e in at.error)
    at.text_input[0].input(str(tmp_path / "missing"))
    click(at, "保存")
    assert any("見つかりません" in e.value for e in at.error)
    assert settings_store.get_str(app_db, settings_store.MUSIC_ROOT_KEY) is None


def test_jev_panel_shows_estimate_before_calling_api(
    app_db: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "Music"
    generate_sample_audio(folder, seconds=3.0)
    scan_library(app_db, folder)
    with transaction(app_db):
        settings_store.set_value(app_db, settings_store.MUSIC_ROOT_KEY, folder.as_posix())

    at = run_view("library")
    assert any("TYPESAFE_API_KEY が未設定" in i.value for i in at.info)

    monkeypatch.setenv("TYPESAFE_API_KEY", "dummy-key-for-test")
    at = run_view("library")
    assert any("未判定の曲: **4 曲**" in m.value for m in at.markdown)
    assert any("リクエスト: **4 件**" in m.value for m in at.markdown)
    assert next(b for b in at.button if b.label == "Jev で判定").disabled  # 確認前は押せない


def test_playlist_generation_edit_save_and_register(demo: sqlite3.Connection) -> None:
    at = run_view("playlist_page")
    click(at, "選ぶ")  # 2 番目の雰囲気（集中・作業）を選ぶ
    click(at, "プレイリストを生成")
    assert any("曲・合計" in m.value for m in at.markdown)

    first_before = at.markdown[next(i for i, m in enumerate(at.markdown) if m.value.startswith("1. "))].value
    click(at, "↓")
    first_after = next(m.value for m in at.markdown if m.value.startswith("1. "))
    assert first_after != first_before
    count_before = sum(1 for b in at.button if b.label == "✕")
    click(at, "✕")
    assert sum(1 for b in at.button if b.label == "✕") == count_before - 1
    at.selectbox[0].select_index(0).run()
    click(at, "追加")
    assert any("手動追加" in m.value for m in at.markdown)

    at.text_input(key="pl_name").input("テスト用プレイリスト").run()
    click(at, "プレイリストを保存")
    [saved] = list_playlists(demo)
    assert saved.name == "テスト用プレイリスト"
    assert any(e.added_manually for e in saved.entries)

    next(c for c in at.checkbox if c.label.startswith("この内容で")).check().run()
    click(at, "Volumio に登録")
    assert any("曲を登録しました" in s.value for s in at.success)
    assert list_registrations(demo, saved.id)[0].status == "success"

    # 同じ名前ではモックの Volumio に登録できない（既存のプレイリストを上書きしない）
    at.run()
    assert any("同じ名前のプレイリストが既にあります" in w.value for w in at.warning)
    click(at, "Volumio に登録")
    assert any("登録しませんでした" in e.value for e in at.error)


def test_playlist_shortfall_offers_relaxation(demo: sqlite3.Connection) -> None:
    at = run_view("playlist_page")
    next(r for r in at.radio if r.label == "ジャンル").set_value(GenreMode.ONLY)
    at.multiselect[0].select(Genre.CLASSICAL)
    next(s for s in at.slider if s.label == "再生時間（分）").set_value(180)
    click(at, "プレイリストを生成")

    assert any("条件に合う曲が足りません" in w.value for w in at.warning)
    click(at, "条件を緩和して再生成")
    assert any("条件を緩和" in m.value for m in at.markdown)


def test_playlist_requires_genres_when_filtering(demo: sqlite3.Connection) -> None:
    at = run_view("playlist_page")
    next(r for r in at.radio if r.label == "ジャンル").set_value(GenreMode.ONLY)

    click(at, "プレイリストを生成")

    assert any("ジャンルを 1 つ以上" in e.value for e in at.error)


def test_evaluation_flow(demo: sqlite3.Connection) -> None:
    at = run_view("evaluation_page")
    click(at, "無作為に選んで追加")
    assert any("評価対象: 40 曲" in m.value for m in at.markdown)

    next(r for r in at.radio if r.label == "ジャンルの判定は").set_value(Verdict.INCORRECT)
    next(s for s in at.selectbox if s.label == "誤りの場合、正しいジャンル").set_value(Genre.WORLD)
    next(r for r in at.radio if r.label == "ムードの判定は").set_value(Verdict.PENDING)
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


def test_playlist_feedback(demo: sqlite3.Connection) -> None:
    at = run_view("playlist_page")
    click(at, "プレイリストを生成")
    click(at, "プレイリストを保存")
    at = run_view("evaluation_page")

    next(s for s in at.slider if s.label.startswith("満足度")).set_value(5)
    at.multiselect[0].select(0)
    click(at, "評価を保存")

    [saved] = list_playlists(demo)
    assert saved.satisfaction == 5
    assert saved.entries[0].unsuitable


def test_settings_view_saves_values(demo: sqlite3.Connection) -> None:
    at = run_view("settings_page")
    assert any("API キー: 未設定" in w.value for w in at.warning)

    next(t for t in at.text_input if t.label == "ホスト名または IP アドレス").input("volumio.local")
    at.button[2].click().run()  # Volumio 接続先の保存
    assert settings_store.get_str(demo, settings_store.VOLUMIO_HOST_KEY) == "volumio.local"

    next(n for n in at.number_input if n.label == "1 回の実行で判定する曲数の上限").set_value(5)
    at.button[3].click().run()
    assert settings_store.get_jev_settings(demo).max_tracks_per_run == 5

    next(n for n in at.number_input if n.label == "解析する区間の数").set_value(4)
    at.button[4].click().run()
    assert settings_store.get_analysis_params(demo).segment_count == 4

    next(r for r in at.radio if r.label == "選曲に使う分類結果").set_value(ClassificationSource.RULES)
    at.button[5].click().run()
    assert settings_store.get_str(demo, settings_store.CLASSIFICATION_SOURCE_KEY) == "rules"
    assert not at.exception


def test_pc_itself_does_not_need_pin_and_sees_qr_code() -> None:
    at = AppTest.from_function(lan_script, args=(None,), default_timeout=TIMEOUT)
    at.run()

    assert not at.exception
    assert any(m.label == "PIN" and m.value == at.session_state["test_pin"] for m in at.metric)
    assert any("http://192.168.1.10:8501/" in c.value for c in at.code)
    assert len(at.get("image")) == 1


def test_lan_device_must_enter_pin() -> None:
    at = AppTest.from_function(lan_script, args=("192.168.1.20",), default_timeout=TIMEOUT)
    at.run()
    assert any(t.value == "PIN の入力" for t in at.title)
    assert not any("ライブラリの画面" in m.value for m in at.markdown)

    at.text_input[0].input("wrong")
    at.button[0].click().run()
    assert any("PIN が違います" in e.value for e in at.error)

    at.text_input[0].input(at.session_state["test_pin"])
    at.button[0].click().run()
    assert any("ライブラリの画面" in m.value for m in at.markdown)
    # 認証済みのセッションでは、再実行しても PIN を求めない
    at.run()
    assert any("ライブラリの画面" in m.value for m in at.markdown)


def test_pin_is_regenerated_after_repeated_failures() -> None:
    at = AppTest.from_function(lan_script, args=("192.168.1.20",), default_timeout=TIMEOUT)
    at.run()
    original = at.session_state["test_pin"]
    wrong = "0000" if original != "0000" else "1111"

    at.text_input[0].input(wrong)
    at.button[0].click().run()
    at.text_input[0].input(wrong)
    at.button[0].click().run()
    assert any("作り直しました" in e.value for e in at.error)

    at.text_input[0].input(at.session_state["test_pin"])
    at.button[0].click().run()
    assert any("一時的に入力を受け付けていません" in e.value for e in at.error)


def test_app_without_lan_mode_has_no_lan_page() -> None:
    at = AppTest.from_file(APP, default_timeout=TIMEOUT)
    at.run()

    assert not at.exception
    assert not any(t.value == "PIN の入力" for t in at.title)


def test_app_in_lan_mode_requires_pin_before_any_page(
    monkeypatch: pytest.MonkeyPatch, demo: sqlite3.Connection
) -> None:
    monkeypatch.setenv("VPS_LAN", "1")
    monkeypatch.setenv("VPS_LAN_ADDRESS", "192.168.1.30")
    at = AppTest.from_file(APP, default_timeout=TIMEOUT)
    at.run()

    # AppTest の接続は localhost と判定されないため、LAN の端末からの接続として扱われる
    assert not at.exception
    assert [t.value for t in at.title] == ["PIN の入力"]
    assert not at.metric


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
