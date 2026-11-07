"""Volumio の REST API クライアント（読み取り専用）のテスト。

Volumio の REST API を模擬するサーバーをテスト内で立てて検証し、実機とは通信しない。
応答の形式は公式ドキュメント（developers.volumio.com の REST API）の例に合わせている。
"""

import json
import socket
import sqlite3
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from volumio_playlist_studio.jobs import JobControl
from volumio_playlist_studio.storage.volumio_library import last_collected_at, load_uris
from volumio_playlist_studio.volumio import BrowseItem, VolumioError, collect_track_uris
from volumio_playlist_studio.volumio_rest import VolumioRestClient, parse_host
from volumio_playlist_studio.workflows import run_volumio_sync

# 模擬するライブラリ。フォルダの URI ごとの直下の項目
LIBRARY: dict[str, list[dict[str, object]]] = {
    "music-library": [
        {"type": "folder", "title": "INTERNAL", "uri": "music-library/INTERNAL", "service": "mpd"},
        {"type": "remdisk", "title": "USB", "uri": "music-library/USB", "service": "mpd"},
    ],
    "music-library/INTERNAL": [
        {"type": "folder", "title": "アーティスト", "uri": "music-library/INTERNAL/アーティスト", "service": "mpd"},
        {"type": "song", "title": "直下の曲", "uri": "music-library/INTERNAL/直下の曲.flac", "service": "mpd"},
        # ライブラリの外を指す項目はたどらない
        {"type": "webradio", "title": "ラジオ", "uri": "http://radio.example/stream", "service": "webradio"},
        {"type": "song", "title": "URI のない項目"},
    ],
    "music-library/INTERNAL/アーティスト": [
        {"type": "song", "title": f"曲 {i}", "uri": f"music-library/INTERNAL/アーティスト/{i:02d} 曲 [{i}].flac"}
        for i in range(1, 6)
    ],
    "music-library/USB": [],
}
PLAYLISTS = ["ゲームサントラ（オススメ）", "ボカロ"]


class FakeVolumio:
    """Volumio の REST API を模擬するサーバー。受け取ったリクエストを記録する。"""

    def __init__(
        self,
        *,
        paging: bool = True,
        status: int = 200,
        body: bytes | None = None,
        library: dict[str, list[dict[str, object]]] | None = None,
    ) -> None:
        self.requests: list[tuple[str, str]] = []
        self.library = LIBRARY if library is None else library
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                fake.requests.append(("GET", self.path))
                payload = body if body is not None else fake.respond(self.path)
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(payload)

            def do_POST(self) -> None:
                fake.requests.append(("POST", self.path))
                self.send_response(405)
                self.end_headers()

        self.paging = paging
        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def host(self) -> str:
        return f"127.0.0.1:{self.server.server_address[1]}"

    def respond(self, path: str) -> bytes:
        parts = urlsplit(path)
        if parts.path == "/api/v1/listplaylists":
            return json.dumps(PLAYLISTS).encode()

        query = parse_qs(parts.query)
        items = self.library.get(query["uri"][0], [])
        listing: dict[str, object] = {"items": items}
        if self.paging:
            offset, limit = int(query["offset"][0]), int(query["limit"][0])
            listing = {"items": items[offset : offset + limit], "count": len(items)}

        return json.dumps({"navigation": {"lists": [listing]}}).encode()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def volumio() -> Iterator[FakeVolumio]:
    fake = FakeVolumio()
    yield fake
    fake.close()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("volumio.local", ("volumio.local", 80)),
        (" 192.168.1.22:3000 ", ("192.168.1.22", 3000)),
        ("my-volumio", ("my-volumio", 80)),
    ],
)
def test_parse_host(text: str, expected: tuple[str, int]) -> None:
    assert parse_host(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "http://volumio.local",
        "volumio.local/api",
        "user@volumio.local",
        "volumio.local:0",
        "volumio.local:70000",
        "-x",
    ],
)
def test_parse_host_rejects_anything_but_host_and_port(text: str) -> None:
    # パスや認証情報を混ぜて、別の URL に接続させないようにする
    with pytest.raises(ValueError, match="invalid"):
        parse_host(text)


def test_list_playlists(volumio: FakeVolumio) -> None:
    assert VolumioRestClient(volumio.host).list_playlists() == PLAYLISTS


def test_browse_reads_all_pages_and_normalizes_types(volumio: FakeVolumio) -> None:
    client = VolumioRestClient(volumio.host, page_size=2)

    items = client.browse("music-library/INTERNAL/アーティスト")

    assert [i.title for i in items] == [f"曲 {i}" for i in range(1, 6)]
    assert len(volumio.requests) == 3  # 5 曲を 2 曲ずつ
    root = client.browse("music-library")
    # ストレージの type はバージョンによって異なりうるが、ライブラリ内の項目はたどれる項目として扱う
    assert [(i.type, i.uri) for i in root] == [("folder", "music-library/INTERNAL"), ("folder", "music-library/USB")]


def test_browse_skips_items_outside_library(volumio: FakeVolumio) -> None:
    items = VolumioRestClient(volumio.host).browse("music-library/INTERNAL")

    assert [(i.type, i.title) for i in items] == [("folder", "アーティスト"), ("song", "直下の曲")]


def test_browse_without_paging_support_reads_once() -> None:
    fake = FakeVolumio(paging=False)
    try:
        items = VolumioRestClient(fake.host, page_size=2).browse("music-library/INTERNAL/アーティスト")
    finally:
        fake.close()

    assert len(items) == 5
    assert len(fake.requests) == 1


def test_collect_track_uris_from_rest_api_only_reads(volumio: FakeVolumio) -> None:
    reports: list[tuple[int, int]] = []

    uris = collect_track_uris(
        VolumioRestClient(volumio.host, page_size=3), control=JobControl(progress=lambda *r: reports.append(r))
    )

    assert sorted(uris) == sorted(
        ["music-library/INTERNAL/直下の曲.flac"]
        + [f"music-library/INTERNAL/アーティスト/{i:02d} 曲 [{i}].flac" for i in range(1, 6)]
    )
    assert reports[-1] == (4, 4)
    # Volumio の状態を変更しないよう、読み取りの API だけを呼ぶ
    assert {method for method, _ in volumio.requests} == {"GET"}
    assert {urlsplit(path).path for _, path in volumio.requests} == {"/api/v1/browse"}


def test_collect_track_uris_can_be_cancelled(volumio: FakeVolumio) -> None:
    cancel = threading.Event()
    cancel.set()

    with pytest.raises(VolumioError, match="cancelled"):
        collect_track_uris(VolumioRestClient(volumio.host), control=JobControl(cancel=cancel))


@pytest.mark.parametrize(
    ("status", "body", "message"),
    [
        (500, b"{}", "HTTP 500"),
        (200, b"<html>not json</html>", "invalid JSON"),
        (200, b'{"unexpected": true}', "unexpected response"),
    ],
)
def test_errors_are_reported_as_volumio_error(status: int, body: bytes, message: str) -> None:
    fake = FakeVolumio(status=status, body=body)
    try:
        client = VolumioRestClient(fake.host)
        with pytest.raises(VolumioError, match=message):
            client.list_playlists()

        with pytest.raises(VolumioError):
            client.browse("music-library")
    finally:
        fake.close()


def test_unreachable_volumio_is_reported() -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    with pytest.raises(VolumioError, match="cannot connect"):
        VolumioRestClient(f"127.0.0.1:{port}", timeout=1.0).list_playlists()


def test_sync_saves_and_replaces_uris(db: sqlite3.Connection, volumio: FakeVolumio) -> None:
    assert last_collected_at(db, volumio.host) is None

    message = run_volumio_sync(db, volumio.host, JobControl())
    run_volumio_sync(db, volumio.host, JobControl())

    assert "6 曲" in message
    assert len(load_uris(db, volumio.host)) == 6
    assert last_collected_at(db, volumio.host) is not None
    assert load_uris(db, "other-host") == []


def test_page_with_only_skipped_items_does_not_stop_paging() -> None:
    radio: dict[str, object] = {"type": "webradio", "title": "ラジオ", "uri": "http://radio.example/stream"}
    songs: list[dict[str, object]] = [
        {"type": "song", "title": f"曲{i}", "uri": f"music-library/INTERNAL/曲{i}.flac"} for i in range(3)
    ]
    fake = FakeVolumio(library={"music-library/INTERNAL": [radio, radio, *songs]})
    try:
        items = VolumioRestClient(fake.host, page_size=2).browse("music-library/INTERNAL")
    finally:
        fake.close()

    # 最初の 2 件はどちらも対象外だが、続きの曲も取得する
    assert [i.title for i in items] == ["曲0", "曲1", "曲2"]
    assert len(fake.requests) == 3


def test_browse_stops_between_pages_when_requested(volumio: FakeVolumio) -> None:
    calls = iter([False, True])
    client = VolumioRestClient(volumio.host, page_size=2, should_stop=lambda: next(calls))

    with pytest.raises(VolumioError, match="cancelled"):
        client.browse("music-library/INTERNAL/アーティスト")

    assert len(volumio.requests) == 1


class CancelDuringBrowse:
    """フォルダを取得している間に中断が要求された状況を再現する、読み取り用のクライアント。"""

    def __init__(self, cancel: threading.Event) -> None:
        self.cancel = cancel

    @property
    def kind(self) -> str:
        return "fake"

    def list_playlists(self) -> list[str]:
        return []

    def browse(self, uri: str) -> list[BrowseItem]:
        self.cancel.set()
        return [BrowseItem("song", "曲", f"{uri}/曲.flac")]


def test_cancel_during_final_folder_does_not_return_partial_result() -> None:
    cancel = threading.Event()

    # 最後（唯一）のフォルダを取得している間に中断が要求される
    with pytest.raises(VolumioError, match="cancelled"):
        collect_track_uris(CancelDuringBrowse(cancel), control=JobControl(cancel=cancel))


def test_cancelled_sync_keeps_previous_uris(db: sqlite3.Connection, volumio: FakeVolumio) -> None:
    run_volumio_sync(db, volumio.host, JobControl())
    cancel = threading.Event()
    cancel.set()

    with pytest.raises(VolumioError, match="cancelled"):
        run_volumio_sync(db, volumio.host, JobControl(cancel=cancel))

    assert len(load_uris(db, volumio.host)) == 6
