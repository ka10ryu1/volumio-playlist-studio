"""Streamlit の画面のテスト。AppTest で画面を実行し、操作と表示を確認する。

実際の外部 API・Volumio とは通信しない。Jev の実行ボタンは押さず、Volumio はテスト内で立てた模擬サーバーか
モッククライアントを使う。
"""

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest
from test_volumio_rest import FakeVolumio
from test_volumio_socketio import FakeSocketIO

from volumio_playlist_studio.jobs import BackgroundJob
from volumio_playlist_studio.models import ClassificationSource, Genre, Mood
from volumio_playlist_studio.playlist import GenreMode
from volumio_playlist_studio.sample_data import DEMO_LIBRARY_ROOT, generate_sample_audio, load_demo_library
from volumio_playlist_studio.scanner import scan_library
from volumio_playlist_studio.storage import settings_store
from volumio_playlist_studio.storage.database import connect, transaction
from volumio_playlist_studio.storage.evaluations import Verdict, load_labels
from volumio_playlist_studio.storage.playlists import list_playlists, list_registrations
from volumio_playlist_studio.storage.tracks import list_tracks
from volumio_playlist_studio.storage.volumio_library import replace_uris

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


def submit(at: AppTest, form_id: str) -> None:
    """フォームの送信ボタンを押す。同じ「保存」ボタンが複数あるため、フォームの ID で区別する。"""
    next(b for b in at.button if b.form_id == form_id).click().run()
    assert not at.exception, [e.value for e in at.exception]


def wait_for_jobs(at: AppTest) -> None:
    jobs: dict[str, BackgroundJob] = at.session_state["jobs"]
    for job in jobs.values():
        job.join(timeout=TIMEOUT)


def show_job_result(at: AppTest) -> None:
    """バックグラウンド処理の終了を待ち、結果のメッセージを表示させる。

    処理がすぐに終わると、ボタンを押した後の再実行の中で結果が表示済みになる。
    もう一度実行するとメッセージが消えるため、結果をまだ表示していない場合だけ再実行する。
    """
    wait_for_jobs(at)
    if at.session_state["jobs"]:
        at.run()
        assert not at.exception


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

    # 壊れたファイルを追加して解析すると、失敗した曲の再試行ボタンが表示される
    (folder / "壊れた.flac").write_bytes(b"broken" * 100)
    click(at, "スキャン（追加・更新を反映）")
    wait_for_jobs(at)
    at.run()
    click(at, "未解析の曲を解析")
    wait_for_jobs(at)
    at.run()
    click(at, "失敗した曲を再試行（1 曲）")
    wait_for_jobs(at)
    at.run()
    assert any("音響解析（再試行） が完了しました" in s.value and "失敗 1 曲" in s.value for s in at.success)


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
    at.radio(key="pl_mood").set_value(Mood.FOCUS)
    click(at, "プレイリストを生成")
    assert any("曲・合計" in m.value for m in at.markdown)

    first_before = at.dataframe[0].value.iloc[0]["曲名"]
    click(at, "下へ")
    first_after = at.dataframe[0].value.iloc[0]["曲名"]
    assert first_after != first_before
    count_before = len(at.dataframe[0].value)
    click(at, "除外")
    assert len(at.dataframe[0].value) == count_before - 1
    next(s for s in at.selectbox if s.label == "追加する曲").select_index(0).run()
    click(at, "追加")
    assert "手動追加" in at.dataframe[1].value["適合度"].tolist()

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
    assert at.multiselect[0].placeholder == "合わなかった曲を選択"
    at.multiselect[0].select(0)
    click(at, "評価を保存")

    [saved] = list_playlists(demo)
    assert saved.satisfaction == 5
    assert saved.entries[0].unsuitable


def test_settings_view_saves_values(demo: sqlite3.Connection) -> None:
    at = run_view("settings_page")
    assert any("API キー: 未設定" in w.value for w in at.warning)

    next(t for t in at.text_input if t.label == "ホスト名または IP アドレス").input("volumio.local")
    submit(at, "volumio-host")  # Volumio 接続先の保存
    assert settings_store.get_str(demo, settings_store.VOLUMIO_HOST_KEY) == "volumio.local"

    next(n for n in at.number_input if n.label == "1 回の実行で判定する曲数の上限").set_value(5)
    submit(at, "jev-settings")
    assert settings_store.get_jev_settings(demo).max_tracks_per_run == 5

    next(n for n in at.number_input if n.label == "解析する区間の数").set_value(4)
    submit(at, "analysis-params")
    assert settings_store.get_analysis_params(demo).segment_count == 4

    next(r for r in at.radio if r.label == "選曲に使う分類結果").set_value(ClassificationSource.RULES)
    submit(at, "engine")
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


@pytest.fixture
def fake_volumio() -> Iterator[FakeVolumio]:
    fake = FakeVolumio()
    yield fake
    fake.close()


def test_settings_volumio_connection_test(
    demo: sqlite3.Connection, fake_volumio: FakeVolumio, monkeypatch: pytest.MonkeyPatch
) -> None:
    at = run_view("settings_page")
    next(t for t in at.text_input if t.label == "ホスト名または IP アドレス").input("http://volumio.local/api")
    submit(at, "volumio-host")
    assert any("形式が正しくありません" in e.value for e in at.error)
    assert settings_store.get_str(demo, settings_store.VOLUMIO_HOST_KEY) is None

    next(t for t in at.text_input if t.label == "ホスト名または IP アドレス").input(fake_volumio.host)
    submit(at, "volumio-host")
    click(at, "接続テスト（プレイリスト一覧を読み取る）")
    assert any("既存のプレイリスト 2 件" in s.value for s in at.success)

    fake_volumio.close()
    click(at, "接続テスト（プレイリスト一覧を読み取る）")
    assert any("接続できませんでした" in e.value for e in at.error)


def test_volumio_panel_previews_against_real_library(demo: sqlite3.Connection, fake_volumio: FakeVolumio) -> None:
    with transaction(demo):
        settings_store.set_value(demo, settings_store.VOLUMIO_HOST_KEY, fake_volumio.host)

    at = run_view("playlist_page")
    click(at, "プレイリストを生成")
    click(at, "プレイリストを保存")
    assert next(r for r in at.radio if r.label == "照合先").value == "Volumio 実機"
    assert any("まだ曲の一覧を取得していません" in c.value for c in at.caption)
    # 曲の一覧を取得して照合するまで、登録ボタンは表示しない
    assert not any(b.label == "Volumio に登録" for b in at.button)

    click(at, "Volumio から曲の一覧を取得")
    wait_for_jobs(at)
    # まれに完了メッセージが表示されずに失敗するため、原因を切り分けられるよう失敗時の状態を残す
    jobs: dict[str, BackgroundJob] = at.session_state["jobs"]
    job_states = {name: (job.state, job.error, job.result) for name, job in jobs.items()}
    at.run()
    assert any("6 曲の情報を取得しました" in s.value for s in at.success), (
        f"jobs={job_states} success={[s.value for s in at.success]} error={[e.value for e in at.error]} "
        f"requests={len(fake_volumio.requests)}"
    )
    # サンプルデータの曲は模擬した Volumio のライブラリにないため、すべて「対応なし」になる
    assert any("Volumio 上の URI と一意に対応しない" in w.value for w in at.warning)
    assert {method for method, _ in fake_volumio.requests} == {"GET"}
    # 登録できる曲がないため、確認のチェックを入れても登録できない
    next(c for c in at.checkbox if c.label.startswith("この内容で")).check().run()
    assert next(b for b in at.button if b.label == "Volumio に登録").disabled

    next(r for r in at.radio if r.label == "照合先").set_value("モック（模擬）").run()
    assert any(b.label == "Volumio に登録" for b in at.button)


def test_volumio_panel_registers_to_real_device(demo: sqlite3.Connection) -> None:
    fake = FakeSocketIO()
    fake.playlists["既存"] = []
    # サンプルデータの全曲が Volumio の内部ストレージにある想定で、取得済みの曲の一覧を用意する
    uri_by_id = {t.id: f"music-library/INTERNAL/{t.rel_path}" for t in list_tracks(demo, DEMO_LIBRARY_ROOT)}
    with transaction(demo):
        settings_store.set_value(demo, settings_store.VOLUMIO_HOST_KEY, fake.host)
        replace_uris(demo, fake.host, sorted(uri_by_id.values()))

    at = run_view("playlist_page")
    click(at, "プレイリストを生成")
    at.text_input(key="pl_name").input("夜の曲").run()
    click(at, "プレイリストを保存")
    [saved] = list_playlists(demo)
    assert not any("一意に対応しない" in w.value for w in at.warning)
    register = next(b for b in at.button if b.label == "Volumio に登録")
    assert register.disabled  # 確認のチェックを入れるまで登録できない
    assert not any(p.startswith("42") for p in fake.received)

    next(c for c in at.checkbox if c.label.startswith("この内容で")).check().run()
    click(at, "Volumio に登録")
    show_job_result(at)
    assert any("「夜の曲」を作成し" in s.value for s in at.success), [e.value for e in at.error]
    assert fake.playlists["夜の曲"] == [uri_by_id[e.track_id] for e in saved.entries]
    [record] = list_registrations(demo, saved.id)
    assert (record.status, record.client_kind) == ("success", "websocket")
    assert any("夜の曲" in c.value and "websocket" in c.value for c in at.caption)

    # 既存の名前では登録せず、既存のプレイリストを変更しない
    at.text_input(key=f"volumio-name-real-{saved.id}").input("既存").run()
    click(at, "Volumio に登録")
    show_job_result(at)
    assert any("既にあるため、登録しませんでした" in e.value for e in at.error)
    assert fake.playlists["既存"] == []
    assert len(list_registrations(demo, saved.id)) == 1
    fake.close()


def test_dashboard_navigates_to_playlist(demo: sqlite3.Connection) -> None:
    at = AppTest.from_file(APP, default_timeout=TIMEOUT).run()
    click(at, "プレイリストを作成")
    assert [t.value for t in at.title] == ["プレイリスト作成"]
    click(at, "プレイリストを生成")
    assert not next(e for e in at.expander if e.label == "1. 選曲の条件").proto.expanded
    assert [t.label for t in at.tabs] == ["2. 曲を編集", "3. 保存・連携"]
    assert len(at.dataframe[0].value) > 0


def test_dashboard_navigates_to_library(demo: sqlite3.Connection) -> None:
    at = AppTest.from_file(APP, default_timeout=TIMEOUT).run()
    click(at, "音楽ライブラリを開く")
    assert [t.value for t in at.title] == ["音楽ライブラリ"]


def test_playlist_conditions_survive_editing_and_save(demo: sqlite3.Connection) -> None:
    at = run_view("playlist_page")
    at.radio(key="pl_mood").set_value(Mood.OMAKASE)
    at.radio(key="pl_length").set_value("曲数")
    at.number_input(key="pl_count").set_value(5)
    at.number_input(key="pl_seed").set_value(42)
    at.slider(key="pl_novelty").set_value(0.7)
    at.slider(key="pl_randomness").set_value(0.6)
    at.checkbox(key="pl_smooth").uncheck()
    click(at, "プレイリストを生成")
    assert len(at.dataframe[0].value) == 5
    original_titles = at.dataframe[0].value["曲名"].tolist()
    click(at, "プレイリストを生成")
    assert at.dataframe[0].value["曲名"].tolist() == original_titles
    at.text_input(key="pl_name").input("編集中の名前").run()
    click(at, "下へ")
    assert at.text_input(key="pl_name").value == "編集中の名前"
    assert at.number_input(key="pl_count").value == 5
    assert at.radio(key="pl_mood").value == Mood.OMAKASE
    assert at.slider(key="pl_novelty").value == 0.7
    click(at, "プレイリストを保存")
    [saved] = list_playlists(demo)
    assert saved.seed == 42
    assert saved.conditions["track_count"] == 5
    assert saved.conditions["randomness"] == 0.6
    assert saved.conditions["smooth_order"] is False
    click(at, "除外")
    assert any("未保存" in c.value for c in at.caption)
    assert len(list_playlists(demo)[0].entries) == 5
    # 保存後は既存仕様の利用回数が選曲に影響するため、曲順でなく入力条件の保持を確認する。
    click(at, "プレイリストを生成")
    assert len(at.dataframe[0].value) == 5
    assert at.number_input(key="pl_seed").value == 42


def test_playlist_empty_draft_can_be_restored(demo: sqlite3.Connection) -> None:
    at = run_view("playlist_page")
    at.radio(key="pl_length").set_value("曲数")
    at.number_input(key="pl_count").set_value(1)
    click(at, "プレイリストを生成")
    assert next(b for b in at.button if b.label == "上へ").disabled
    assert next(b for b in at.button if b.label == "下へ").disabled
    click(at, "除外")
    assert any("曲がありません" in i.value for i in at.info)
    assert next(b for b in at.button if b.label == "プレイリストを保存").disabled
    next(s for s in at.selectbox if s.label == "追加する曲").select_index(0).run()
    click(at, "追加")
    assert len(at.dataframe[0].value) == 1
    assert not next(b for b in at.button if b.label == "プレイリストを保存").disabled


def test_library_filters_and_empty_results(demo: sqlite3.Connection) -> None:
    at = run_view("library")
    next(s for s in at.selectbox if s.label == "解析状況").select("未解析").run()
    assert any("条件に合う曲がありません" in i.value for i in at.info)
    next(s for s in at.selectbox if s.label == "解析状況").select("解析済み").run()
    next(s for s in at.selectbox if s.label == "判定ジャンルで絞り込み").select("ジャズ").run()
    rows = at.dataframe[0].value
    assert len(rows) > 0
    assert set(rows["判定ジャンル"]) == {"ジャズ"}
    assert set(rows["解析"]) == {"解析済み"}
    assert next(e for e in at.expander if e.label == "音響特徴量・判定の詳細").proto.expanded is False


def test_classification_details_do_not_show_none(demo: sqlite3.Connection) -> None:
    at = run_view("library")
    details = next(d.value for d in at.dataframe if "確信度" in d.value.columns)
    assert set(details["確信度"]) == {"対象外"}
    assert "判定保留" in details["スコア (0〜1)"].tolist()
    assert not details.isna().any().any()


def test_changing_conditions_does_not_replace_draft(demo: sqlite3.Connection) -> None:
    at = run_view("playlist_page")
    click(at, "プレイリストを生成")
    before = at.dataframe[0].value["曲名"].tolist()
    at.radio(key="pl_mood").set_value(Mood.FOCUS).run()
    at.radio(key="pl_length").set_value("曲数").run()
    at.number_input(key="pl_count").set_value(3).run()
    assert at.dataframe[0].value["曲名"].tolist() == before
    click(at, "下へ")
    assert at.dataframe[0].value["曲名"].tolist()[:2] == [before[1], before[0]]
    assert at.number_input(key="pl_count").value == 3
    assert at.radio(key="pl_mood").value == Mood.FOCUS
    # 移動後も同じ曲を選択したまま、続けて移動できる。
    click(at, "下へ")
    assert at.dataframe[0].value.iloc[2]["曲名"] == before[0]
    click(at, "プレイリストを生成")
    assert len(at.dataframe[0].value) == 3


def playlist_navigation_script() -> None:
    """AppTest が直接操作できないサイドバーの代わりにボタンで標準ページ遷移を行う。"""
    import streamlit as st

    from volumio_playlist_studio.gui.context import open_context
    from volumio_playlist_studio.gui.views import playlist_page

    ctx = open_context()
    try:

        def edit() -> None:
            playlist_page.render(ctx)
            if st.button("別画面へ"):
                st.switch_page(other_page)

        def other() -> None:
            if st.button("編集へ戻る"):
                st.switch_page(edit_page)

        edit_page = st.Page(edit, title="編集", url_path="edit", default=True)
        other_page = st.Page(other, title="別画面", url_path="other")
        playlist_page.preserve_inputs()
        st.navigation([edit_page, other_page]).run()
    finally:
        ctx.conn.close()


def test_playlist_round_trip_keeps_unsaved_inputs_and_draft(demo: sqlite3.Connection) -> None:
    at = AppTest.from_function(playlist_navigation_script, default_timeout=TIMEOUT).run()
    click(at, "プレイリストを生成")
    click(at, "除外")
    before = at.dataframe[0].value["曲名"].tolist()
    at.text_input(key="pl_name").input("戻って編集する名前").run()
    at.radio(key="pl_mood").set_value(Mood.FOCUS).run()
    at.number_input(key="pl_count").set_value(7).run()
    at.number_input(key="pl_seed").set_value(2024).run()
    click(at, "別画面へ")
    assert not at.dataframe
    click(at, "編集へ戻る")
    assert at.text_input(key="pl_name").value == "戻って編集する名前"
    assert at.radio(key="pl_mood").value == Mood.FOCUS
    assert at.number_input(key="pl_count").value == 7
    assert at.number_input(key="pl_seed").value == 2024
    assert at.dataframe[0].value["曲名"].tolist() == before
