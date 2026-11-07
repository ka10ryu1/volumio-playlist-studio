"""Volumio 実機と通信するテスト（読み取りのみ）。

通常のテストでは実行しない。実機で確認するときは、接続先を環境変数で指定して明示的に実行する。

    VPS_TEST_VOLUMIO_HOST=volumio.local uv run pytest -m integration tests/test_volumio_live.py --no-cov
"""

import os

import pytest

from volumio_playlist_studio.volumio import collect_track_uris
from volumio_playlist_studio.volumio_rest import VolumioRestClient

pytestmark = pytest.mark.integration

# テスト全体の設定（conftest）が VOLUMIO_HOST を空にするため、実機の接続先は別の環境変数で受け取る
HOST_ENV = "VPS_TEST_VOLUMIO_HOST"


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
