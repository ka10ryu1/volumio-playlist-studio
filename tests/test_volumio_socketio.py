"""Volumio の WebSocket（Socket.io）API によるプレイリストの作成・曲の追加のテスト。

WebSocket と Socket.io の最小限のサーバーをテスト内で立てて検証し、実機とは通信しない。
サーバーの振る舞いは、volumio3-backend の websocket プラグインと playlistManager.js（socket.io 2.x）と、
socket.io 3.x 以降のプロトコルの仕様に合わせている。
"""

import base64
import contextlib
import hashlib
import json
import socket
import threading
from collections.abc import Iterator

import pytest
from test_volumio_rest import FakeVolumio

from volumio_playlist_studio.volumio import PlaylistExistsError, VolumioError, register_playlist
from volumio_playlist_studio.volumio_socketio import SocketIOProtocol, SocketIOSession, VolumioLiveClient

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_OPCODE_TEXT = 0x1
_OPCODE_CLOSE = 0x8
URI_A = "music-library/INTERNAL/アーティスト/01 曲 [1].flac"
URI_B = "music-library/INTERNAL/アーティスト/02 曲 [2].flac"


def _recv_exact(conn: socket.socket, size: int) -> bytes:
    data = b""
    while len(data) < size:
        chunk = conn.recv(size - len(data))
        if not chunk:
            raise ConnectionError("closed")

        data += chunk

    return data


def _read_frame(conn: socket.socket) -> tuple[int, bytes]:
    first, second = _recv_exact(conn, 2)
    length = second & 0x7F
    if length == 126:
        length = int.from_bytes(_recv_exact(conn, 2))
    elif length == 127:
        length = int.from_bytes(_recv_exact(conn, 8))

    mask = _recv_exact(conn, 4) if second & 0x80 else b"\0\0\0\0"
    payload = bytes(b ^ mask[i % 4] for i, b in enumerate(_recv_exact(conn, length)))
    return first & 0x0F, payload


def _write_frame(conn: socket.socket, payload: bytes, opcode: int = _OPCODE_TEXT) -> None:
    header = bytes([0x80 | opcode])
    if len(payload) < 126:
        header += bytes([len(payload)])
    else:
        header += bytes([126]) + len(payload).to_bytes(2)

    conn.sendall(header + payload)


class FakeSocketIO:
    """Volumio のプレイリスト操作を模擬する Socket.io サーバー。受け取ったパケットを記録する。

    Attributes:
        legacy: True なら socket.io 2.x、False なら 3.x 以降として振る舞う。
        playlists: プレイリスト名ごとの曲の URI。
        silent_uris: 追加しようとしても応答を返さない URI（Volumio 3 系で一覧の取得に失敗した場合の振る舞い）。
        failing_uris: success: false を返す URI。
        ping_interval_ms: 接続時に通知する ping の間隔。
    """

    def __init__(self, *, legacy: bool = True, ping_interval_ms: int = 25000) -> None:
        self.legacy = legacy
        self.ping_interval_ms = ping_interval_ms
        self.playlists: dict[str, list[str]] = {}
        self.silent_uris: set[str] = set()
        self.failing_uris: set[str] = set()
        self.refuse_namespace = False
        self.close_after_open = False
        self.open_packet: str | None = None
        self.received: list[str] = []
        self.paths: list[str] = []
        self._server = socket.create_server(("127.0.0.1", 0))
        threading.Thread(target=self._serve, daemon=True).start()

    @property
    def host(self) -> str:
        return f"127.0.0.1:{self._server.getsockname()[1]}"

    def close(self) -> None:
        self._server.close()

    def _serve(self) -> None:
        # WebSocket の接続中にも REST API に応答できるよう、接続ごとにスレッドを分ける
        while True:
            try:
                conn, _ = self._server.accept()
            except OSError:
                return

            threading.Thread(target=self._serve_connection, args=(conn,), daemon=True).start()

    def _serve_connection(self, conn: socket.socket) -> None:
        # テスト側が接続を切った場合などの送受信の失敗は、そのテストのアサーションで検出する
        with conn, contextlib.suppress(OSError):
            self._handle(conn)

    def _handle(self, conn: socket.socket) -> None:
        request = b""
        while b"\r\n\r\n" not in request:
            request += conn.recv(4096)

        lines = request.decode().split("\r\n")
        self.paths.append(lines[0].split(" ")[1])
        headers = {k.lower(): v.strip() for k, _, v in (line.partition(":") for line in lines[1:] if line)}
        if headers.get("upgrade", "").lower() != "websocket":
            # 同じ接続先で REST API（プレイリスト一覧）にも応答する
            body = json.dumps(sorted(self.playlists)).encode()
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n%s" % (len(body), body))
            return

        # WebSocket のハンドシェイクの仕様（RFC 6455）で決まった計算。安全性のための利用ではない
        accept = base64.b64encode(
            hashlib.sha1((headers["sec-websocket-key"] + _WS_GUID).encode(), usedforsecurity=False).digest()
        )
        conn.sendall(
            b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            b"Sec-WebSocket-Accept: " + accept + b"\r\n\r\n"
        )
        info = {"sid": "abc", "upgrades": [], "pingInterval": self.ping_interval_ms, "pingTimeout": 60000}
        self._send(conn, self.open_packet or "0" + json.dumps(info))
        if self.close_after_open:
            self._send(conn, "1")
            return

        if self.legacy:
            self._send(conn, "40")

        while True:
            opcode, payload = _read_frame(conn)
            if opcode == _OPCODE_CLOSE:
                _write_frame(conn, payload, _OPCODE_CLOSE)
                return

            packet = payload.decode()
            self.received.append(packet)
            self._on_packet(conn, packet)

    def _on_packet(self, conn: socket.socket, packet: str) -> None:
        if packet == "2" and self.legacy:
            self._send(conn, "3")
        elif packet == "40" and not self.legacy:
            if self.refuse_namespace:
                self._send(conn, '44{"message":"not authorized"}')
            else:
                self._send(conn, '40{"sid":"xyz"}')
                # 3.x 以降では ping をサーバーから送る
                self._send(conn, "2")
        elif packet.startswith("42"):
            event, data = json.loads(packet[2:])
            self._on_event(conn, event, data)

    def _on_event(self, conn: socket.socket, event: str, data: dict[str, str]) -> None:
        # 結果と関係のない通知を混ぜ、クライアントが読み捨てることを確認する
        self._emit(conn, "pushState", {"status": "stop"})
        if event == "createPlaylist":
            if data["name"] in self.playlists:
                self._emit(conn, "pushCreatePlaylist", {"success": False, "reason": "Playlist already exists"})
                return

            self.playlists[data["name"]] = []
            self._emit(conn, "pushCreatePlaylist", {"success": True})
            self._emit(conn, "pushListPlaylist", sorted(self.playlists))
        elif event == "addToPlaylist":
            if data["uri"] in self.silent_uris:
                return

            if data["uri"] in self.failing_uris:
                self._emit(conn, "pushAddToPlaylist", {"success": False})
                return

            self.playlists.setdefault(data["name"], []).append(data["uri"])
            self._emit(conn, "pushListPlaylist", sorted(self.playlists))
            self._emit(conn, "pushAddToPlaylist", {"uri": data["uri"], "service": "mpd", "favourite": False})

    def _emit(self, conn: socket.socket, event: str, data: object) -> None:
        self._send(conn, "42" + json.dumps([event, data], ensure_ascii=False))

    def _send(self, conn: socket.socket, packet: str) -> None:
        _write_frame(conn, packet.encode())


@pytest.fixture(params=[True, False], ids=["socket.io-2", "socket.io-3+"])
def fake(request: pytest.FixtureRequest) -> Iterator[FakeSocketIO]:
    server = FakeSocketIO(legacy=request.param)
    yield server
    server.close()


def live_client(fake: FakeSocketIO, response_timeout: float = 1.0) -> VolumioLiveClient:
    return VolumioLiveClient(fake.host, timeout=2.0, response_timeout=response_timeout, namespace_wait=0.3)


def test_create_playlist_and_add_tracks(fake: FakeSocketIO) -> None:
    with live_client(fake) as client:
        client.create_playlist(" 夜の曲 ")
        client.add_to_playlist("夜の曲", URI_A, "mpd")
        client.add_to_playlist("夜の曲", URI_B, "mpd")
        expected = SocketIOProtocol.V2 if fake.legacy else SocketIOProtocol.V3_OR_LATER
        assert client.protocol == expected

    assert fake.playlists == {"夜の曲": [URI_A, URI_B]}
    assert fake.paths == ["/socket.io/?EIO=4&transport=websocket"]
    events = [json.loads(p[2:]) for p in fake.received if p.startswith("42")]
    assert events == [
        ["createPlaylist", {"name": "夜の曲"}],
        ["addToPlaylist", {"name": "夜の曲", "service": "mpd", "uri": URI_A}],
        ["addToPlaylist", {"name": "夜の曲", "service": "mpd", "uri": URI_B}],
    ]


def test_namespace_connect_is_sent_only_to_socketio_3_or_later(fake: FakeSocketIO) -> None:
    with live_client(fake) as client:
        client.create_playlist("p")

    # 2.x では既定の名前空間に自動で参加するため、接続を要求すると二重の接続になる
    assert ("40" in fake.received) is not fake.legacy
    # 3.x 以降ではサーバーからの ping に pong を返す
    assert ("3" in fake.received) is not fake.legacy


def test_existing_playlist_name_is_rejected(fake: FakeSocketIO) -> None:
    fake.playlists["既存"] = [URI_A]
    with live_client(fake) as client, pytest.raises(PlaylistExistsError):
        client.create_playlist("既存")

    assert fake.playlists == {"既存": [URI_A]}


def test_add_failure_reported_by_volumio(fake: FakeSocketIO) -> None:
    fake.failing_uris.add(URI_B)
    with live_client(fake) as client:
        client.create_playlist("p")
        with pytest.raises(VolumioError, match="failed to add"):
            client.add_to_playlist("p", URI_B, "mpd")


def test_add_without_response_times_out(fake: FakeSocketIO) -> None:
    fake.silent_uris.add(URI_B)
    with live_client(fake, response_timeout=0.3) as client:
        client.create_playlist("p")
        with pytest.raises(VolumioError, match="no response"):
            client.add_to_playlist("p", URI_B, "mpd")


def test_register_playlist_stops_at_unanswered_track(fake: FakeSocketIO) -> None:
    fake.silent_uris.add(URI_B)
    fake.playlists["既存"] = []
    with live_client(fake, response_timeout=0.3) as client:
        outcome = register_playlist(client, "途中で応答なし", [(1, URI_A), (2, URI_B), (3, URI_A)])
        with pytest.raises(PlaylistExistsError):
            register_playlist(client, "既存", [(1, URI_A)])

    assert outcome.status == "partial"
    assert [i.status for i in outcome.items] == ["added", "failed", "skipped"]
    assert fake.playlists == {"既存": [], "途中で応答なし": [URI_A]}
    # 既存の名前の確認は REST API で行い、作成の要求は送らない
    assert "/api/v1/listplaylists" in fake.paths
    assert sum(1 for p in fake.received if p.startswith('42["createPlaylist"')) == 1


@pytest.mark.parametrize("name", ["", "  ", "../etc", "a/b", "a\\b", ".hidden", "改行\nあり"])
def test_invalid_playlist_name_is_not_sent(fake: FakeSocketIO, name: str) -> None:
    with live_client(fake) as client:
        with pytest.raises(ValueError, match="playlist name"):
            client.create_playlist(name)

        with pytest.raises(ValueError, match="playlist name"):
            client.add_to_playlist(name, URI_A, "mpd")

    # 名前を検証してから接続するため、何も送らない
    assert fake.received == []
    assert fake.paths == []


def test_legacy_client_sends_ping_while_waiting() -> None:
    server = FakeSocketIO(legacy=True, ping_interval_ms=50)
    server.silent_uris.add(URI_A)
    try:
        with (
            VolumioLiveClient(server.host, timeout=2.0, response_timeout=0.3, namespace_wait=0.3) as client,
            pytest.raises(VolumioError, match="no response"),
        ):
            client.add_to_playlist("p", URI_A, "mpd")
    finally:
        server.close()

    assert server.received.count("2") >= 2


def test_namespace_refused_by_server() -> None:
    server = FakeSocketIO(legacy=False)
    server.refuse_namespace = True
    try:
        with pytest.raises(VolumioError, match=r"Socket\.io error"):
            SocketIOSession(server.host, timeout=2.0, namespace_wait=0.1).open()
    finally:
        server.close()


@pytest.mark.parametrize("open_packet", ["hello", "0{broken", "1"])
def test_unexpected_handshake(open_packet: str) -> None:
    server = FakeSocketIO()
    server.open_packet = open_packet
    try:
        with pytest.raises(VolumioError, match="handshake"):
            SocketIOSession(server.host, timeout=2.0, namespace_wait=0.1).open()
    finally:
        server.close()


def test_server_closing_connection() -> None:
    server = FakeSocketIO()
    server.close_after_open = True
    try:
        with pytest.raises(VolumioError, match="closed"):
            SocketIOSession(server.host, timeout=2.0, namespace_wait=0.3).open()
    finally:
        server.close()


def test_connection_refused() -> None:
    with socket.create_server(("127.0.0.1", 0)) as unused:
        port = unused.getsockname()[1]

    with pytest.raises(VolumioError, match="cannot connect"):
        SocketIOSession(f"127.0.0.1:{port}", timeout=1.0).open()


def test_session_requires_connection() -> None:
    session = SocketIOSession("127.0.0.1:1")
    with pytest.raises(VolumioError, match="not connected"):
        session.emit("listPlaylist", None)

    assert session.protocol is None
    session.close()


def test_reads_use_rest_api() -> None:
    volumio = FakeVolumio()
    try:
        client = VolumioLiveClient(volumio.host)
        assert client.kind == "websocket"
        assert client.list_playlists() == ["ゲームサントラ（オススメ）", "ボカロ"]
        assert [i.uri for i in client.browse("music-library/USB")] == []
        assert client.protocol is None
    finally:
        volumio.close()
