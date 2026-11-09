"""Volumio 実機と通信するテスト。

通常のテストでは実行しない。実機で確認するときは、接続先を環境変数で指定して明示的に実行する。

    VPS_TEST_VOLUMIO_HOST=volumio.local uv run pytest -m integration tests/test_volumio_live.py --no-cov

プレイリストを作成するテストは、Volumio の状態を変更するため、さらに VPS_TEST_VOLUMIO_WRITE=1 を
指定したときだけ実行する。`vps-test-` で始まる名前のプレイリストを新しく作り、既存のプレイリストは変更しない。
作成したプレイリストはテスト後も残るため、内容と再生を Volumio の画面で確認してから、画面で削除する。
"""

import os
from collections import deque
from datetime import datetime

import pytest

from volumio_playlist_studio.volumio import PlaylistExistsError, VolumioReader, collect_track_uris, register_playlist
from volumio_playlist_studio.volumio_rest import VolumioRestClient
from volumio_playlist_studio.volumio_socketio import VolumioLiveClient

pytestmark = pytest.mark.integration

# テスト全体の設定（conftest）が VOLUMIO_HOST を空にするため、実機の接続先は別の環境変数で受け取る
HOST_ENV = "VPS_TEST_VOLUMIO_HOST"
WRITE_ENV = "VPS_TEST_VOLUMIO_WRITE"
# プレイリストに追加する曲数
TRACKS_TO_ADD = 3


@pytest.fixture
def client() -> VolumioRestClient:
    host = os.environ.get(HOST_ENV, "").strip()
    if not host:
        pytest.skip(f"{HOST_ENV} is not set")

    return VolumioRestClient(host)


def test_list_playlists_on_real_volumio(client: VolumioRestClient) -> None:
    playlists = client.list_playlists()

    assert all(isinstance(name, str) for name in playlists)


def test_library_uris_on_real_volumio(client: VolumioRestClient) -> None:
    uris = collect_track_uris(client)

    assert uris, "Volumio のライブラリに曲が見つかりません"
    assert all(uri.startswith("music-library/") for uri in uris)


def _first_track_uris(client: VolumioReader, count: int) -> list[str]:
    """ライブラリを先頭からたどり、最初に見つかった曲の URI を返す（ライブラリ全体はたどらない）。"""
    uris: list[str] = []
    queue = deque(["music-library"])
    while queue and len(uris) < count:
        for item in client.browse(queue.popleft()):
            if item.type == "song":
                uris.append(item.uri)
            else:
                queue.append(item.uri)

    return uris[:count]


def test_create_playlist_on_real_volumio(client: VolumioRestClient) -> None:
    if os.environ.get(WRITE_ENV) != "1":
        pytest.skip(f"{WRITE_ENV}=1 is not set (this test creates a playlist on Volumio)")

    name = f"vps-test-{datetime.now():%Y%m%d-%H%M%S}"
    with VolumioLiveClient(os.environ[HOST_ENV]) as live:
        uris = _first_track_uris(live, TRACKS_TO_ADD)
        assert uris, "Volumio のライブラリに曲が見つかりません"

        outcome = register_playlist(live, name, [(i, uri) for i, uri in enumerate(uris)])
        assert outcome.status == "success", outcome.error
        assert name in live.list_playlists()

        # 同じ名前では作成しない（アプリの確認と、Volumio 自身の応答の両方）
        with pytest.raises(PlaylistExistsError):
            register_playlist(live, name, [(0, uris[0])])

        with pytest.raises(PlaylistExistsError):
            live.create_playlist(name)

        # 判定した Socket.io のバージョンは、クライアントが INFO のログに出す（--log-cli-level=INFO で表示される）
        assert live.protocol is not None
