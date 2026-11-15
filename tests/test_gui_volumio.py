"""Volumio との連携を操作する画面のテスト（設定画面の接続テスト、プレイリストの登録パネル）。

実機とは通信せず、テスト内で立てた模擬サーバーかモッククライアントを使う。"""

import sqlite3
from collections.abc import Iterator

import pytest
from test_gui import click, run_view, show_job_result, submit
from test_volumio_rest import FakeVolumio
from test_volumio_socketio import FakeSocketIO

from volumio_playlist_studio.sample_data import DEMO_LIBRARY_ROOT
from volumio_playlist_studio.storage import settings_store
from volumio_playlist_studio.storage.database import transaction
from volumio_playlist_studio.storage.playlists import list_playlists, list_registrations
from volumio_playlist_studio.storage.tracks import list_tracks
from volumio_playlist_studio.storage.volumio_library import replace_uris


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
    # 取得がすぐに終わると、ボタンを押した後の再実行の中で完了メッセージが表示済みになる。
    # 無条件に再実行するとメッセージが消えるため、まだ表示していない場合だけ再実行する
    show_job_result(at)
    assert any("6 曲の情報を取得しました" in s.value for s in at.success), (
        f"success={[s.value for s in at.success]} error={[e.value for e in at.error]} "
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
