"""Volumio の WebSocket（Socket.io）API による、プレイリストの作成・曲の追加。

REST API にはプレイリストを作成・変更する API がないため、書き込みだけを WebSocket API で行う
（docs/volumio.md）。読み取り（プレイリスト一覧・ライブラリの参照）は REST API のクライアントに任せる。

Socket.io のクライアントは、必要な機能（イベントの送信と、結果のイベントの受信）だけを自前で実装している。
Volumio 3 系が使う socket.io 2.x（Engine.IO v3）と、socket.io 3.x 以降（Engine.IO v4）の両方に対応する。
2 つの違いは次のとおりで、接続直後のサーバーの振る舞いから判定する。

- 2.x: サーバーが既定の名前空間への接続（`40`）を自分から送る。生存確認の ping はクライアントが送る
- 3.x 以降: クライアントが `40` を送り、サーバーが応答する。ping はサーバーが送り、クライアントが pong を返す
"""

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from types import TracebackType
from typing import Self

import websocket

from volumio_playlist_studio.volumio import (
    BrowseItem,
    PlaylistExistsError,
    VolumioError,
    validate_playlist_name,
)
from volumio_playlist_studio.volumio_rest import VolumioRestClient, parse_host

logger = logging.getLogger(__name__)

# Engine.IO のパケットの種類（先頭の 1 文字）
_EIO_OPEN = "0"
_EIO_CLOSE = "1"
_EIO_PING = "2"
_EIO_PONG = "3"
_EIO_MESSAGE = "4"
# Socket.io のパケットの種類（Engine.IO の message に続く 1 文字）
_SIO_CONNECT = "0"
_SIO_DISCONNECT = "1"
_SIO_EVENT = "2"
_SIO_ERROR = "4"
_DEFAULT_NAMESPACE = "/"
# Volumio 3 系の createPlaylist が、同じ名前のプレイリストがあるときに返す理由
_PLAYLIST_EXISTS_REASON = "Playlist already exists"


class SocketIOProtocol(StrEnum):
    """接続先の Socket.io のバージョン。"""

    V2 = "socket.io-2"
    V3_OR_LATER = "socket.io-3+"


@dataclass(frozen=True)
class _Event:
    name: str
    data: object


class SocketIOSession:
    """Socket.io サーバーとの 1 本の接続。イベントの送信と、指定したイベントの受信を行う。

    受信を待つ間に届いた他のイベント（Volumio が送る再生状態の通知など）は読み捨てる。
    """

    def __init__(self, host: str, timeout: float = 10.0, namespace_wait: float = 2.0) -> None:
        """接続先を設定する。通信は open() を呼んだときに行う。

        Args:
            host: 接続先（`volumio.local` や `192.168.1.22:3000`）。
            timeout: 接続と、接続直後のハンドシェイクのタイムアウト（秒）。
            namespace_wait: 接続直後に、サーバーから名前空間への接続（socket.io 2.x の振る舞い）が
                届くのを待つ時間（秒）。届かなければ socket.io 3.x 以降として扱う。

        Raises:
            ValueError: 接続先の形式が不正な場合。
        """
        self._host, self._port = parse_host(host)
        self._timeout = timeout
        self._namespace_wait = namespace_wait
        self._ws: websocket.WebSocket | None = None
        self._protocol: SocketIOProtocol | None = None
        self._ping_interval = 25.0
        self._last_ping = 0.0

    @property
    def protocol(self) -> SocketIOProtocol | None:
        """判定した Socket.io のバージョン。接続前は None。"""
        return self._protocol

    def __enter__(self) -> Self:
        self.open()
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        self.close()

    def open(self) -> None:
        """接続し、既定の名前空間に参加する。接続済みなら何もしない。

        Raises:
            VolumioError: 接続またはハンドシェイクに失敗した場合。
        """
        if self._ws is not None:
            return

        url = f"ws://{self._host}:{self._port}/socket.io/?EIO=4&transport=websocket"
        try:
            # LAN 内の機器に直接つなぐため、環境変数のプロキシ設定は使わない
            self._ws = websocket.create_connection(url, timeout=self._timeout, http_no_proxy=[self._host])
            self._handshake()
        except (websocket.WebSocketException, OSError) as exc:
            self.close()
            logger.warning("volumio socket.io connect failed: error=%s", type(exc).__name__)
            raise VolumioError(f"cannot connect to Volumio WebSocket: {type(exc).__name__}") from exc
        except VolumioError:
            self.close()
            raise

        logger.info("volumio socket.io connected: protocol=%s", self._protocol)

    def close(self) -> None:
        """接続を閉じる。未接続なら何もしない。"""
        ws, self._ws = self._ws, None
        if ws is None:
            return

        try:
            ws.close(timeout=1)
        except (websocket.WebSocketException, OSError) as exc:
            # 閉じる処理の失敗は、送受信の結果に影響しないため記録だけ行う
            logger.debug("volumio socket.io close failed: error=%s", type(exc).__name__)

    def emit(self, event: str, data: object) -> None:
        """イベントを送信する。

        Raises:
            VolumioError: 未接続、または送信に失敗した場合。
        """
        payload = json.dumps([event, data], ensure_ascii=False)
        self._send(f"{_EIO_MESSAGE}{_SIO_EVENT}{payload}")

    def wait_for(self, event: str, timeout: float) -> object:
        """指定した名前のイベントを受信するまで待ち、最初の引数を返す。

        Raises:
            VolumioError: 時間内に受信できない場合、接続が切れた場合、または未接続の場合。
        """
        deadline = time.monotonic() + timeout
        while True:
            received = self._receive_until(deadline)
            if received is None:
                raise VolumioError(f"no response from Volumio: event={event}")

            if received.name == event:
                return received.data

            logger.debug("volumio socket.io event ignored: event=%s", received.name)

    def _handshake(self) -> None:
        first = self._recv_text(time.monotonic() + self._timeout)
        if first is None or not first.startswith(_EIO_OPEN):
            raise VolumioError("unexpected Socket.io handshake")

        try:
            info = json.loads(first[1:])
        except ValueError as exc:
            raise VolumioError("unexpected Socket.io handshake") from exc

        interval = info.get("pingInterval") if isinstance(info, dict) else None
        if isinstance(interval, int) and interval > 0:
            self._ping_interval = interval / 1000

        self._last_ping = time.monotonic()
        # socket.io 2.x は既定の名前空間への接続を自分から通知する。待っても届かなければ 3.x 以降として接続を要求する
        if self._wait_namespace(time.monotonic() + self._namespace_wait):
            self._protocol = SocketIOProtocol.V2
            return

        self._protocol = SocketIOProtocol.V3_OR_LATER
        self._send(f"{_EIO_MESSAGE}{_SIO_CONNECT}")
        if not self._wait_namespace(time.monotonic() + self._timeout):
            raise VolumioError("Socket.io namespace connection timed out")

    def _wait_namespace(self, deadline: float) -> bool:
        while (packet := self._recv_text(deadline)) is not None:
            if packet.startswith(_EIO_MESSAGE + _SIO_CONNECT):
                return True

            self._handle_control(packet)

        return False

    def _receive_until(self, deadline: float) -> _Event | None:
        """期限までに届いた最初のイベントを返す。届かなければ None。"""
        while (packet := self._recv_text(deadline)) is not None:
            self._handle_control(packet)
            event = _parse_event(packet)
            if event is not None:
                return event

        return None

    def _handle_control(self, packet: str) -> None:
        """イベント以外のパケット（生存確認・切断）を処理する。"""
        if packet == _EIO_PING:
            self._send(_EIO_PONG)
        elif packet.startswith(_EIO_CLOSE) or packet.startswith(_EIO_MESSAGE + _SIO_DISCONNECT):
            raise VolumioError("Volumio closed the WebSocket connection")
        elif packet.startswith(_EIO_MESSAGE + _SIO_ERROR):
            raise VolumioError("Volumio returned a Socket.io error")

    def _recv_text(self, deadline: float) -> str | None:
        """期限までに届いたテキストのパケットを 1 つ返す。届かなければ None。

        socket.io 2.x では、待っている間もクライアントから定期的に ping を送り、接続を保つ。
        """
        ws = self._require_ws()
        while (remaining := deadline - time.monotonic()) > 0:
            wait = remaining
            if self._protocol == SocketIOProtocol.V2:
                until_ping = self._last_ping + self._ping_interval - time.monotonic()
                if until_ping <= 0:
                    self._send(_EIO_PING)
                    self._last_ping = time.monotonic()
                    continue

                wait = min(wait, until_ping)

            ws.settimeout(wait)
            try:
                message = ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            except (websocket.WebSocketException, OSError) as exc:
                raise VolumioError(f"WebSocket receive failed: {type(exc).__name__}") from exc

            # バイナリのパケット（添付ファイル）は使わないため読み捨てる
            if isinstance(message, str):
                return message

        return None

    def _send(self, packet: str) -> None:
        ws = self._require_ws()
        try:
            ws.send(packet)
        except (websocket.WebSocketException, OSError) as exc:
            raise VolumioError(f"WebSocket send failed: {type(exc).__name__}") from exc

    def _require_ws(self) -> websocket.WebSocket:
        if self._ws is None:
            raise VolumioError("WebSocket is not connected")

        return self._ws


def _parse_event(packet: str) -> _Event | None:
    """既定の名前空間のイベントのパケットを解析する。イベント以外や解析できないものは None。

    形式は `42` に続けて、省略可能な名前空間（`/xxx,`）と応答 ID（数字）、JSON の配列 `["名前", 引数...]`。
    """
    prefix = _EIO_MESSAGE + _SIO_EVENT
    if not packet.startswith(prefix):
        return None

    body = packet[len(prefix) :]
    if body.startswith("/"):
        namespace, _, body = body.partition(",")
        if namespace != _DEFAULT_NAMESPACE:
            return None

    body = body.lstrip("0123456789")
    try:
        decoded = json.loads(body)
    except ValueError:
        logger.warning("volumio socket.io event malformed")
        return None

    if not isinstance(decoded, list) or not decoded or not isinstance(decoded[0], str):
        return None

    return _Event(decoded[0], decoded[1] if len(decoded) > 1 else None)


class VolumioLiveClient:
    """Volumio 実機のクライアント。読み取りは REST API、プレイリストの作成・曲の追加は WebSocket API で行う。

    WebSocket は最初の書き込みのときに接続し、close() まで使い続ける。with 文で使う。
    Volumio 3 系の addToPlaylist は失敗時に応答を返さないことがあるため、応答の待ち時間を区切って失敗として扱う。
    """

    def __init__(
        self,
        host: str,
        timeout: float = 10.0,
        response_timeout: float = 15.0,
        should_stop: Callable[[], bool] | None = None,
        namespace_wait: float = 2.0,
    ) -> None:
        """接続先を設定する。通信はメソッドを呼んだときに行う。

        Args:
            host: Volumio の接続先（`volumio.local` など）。
            timeout: 接続のタイムアウト（秒）。
            response_timeout: 書き込みのイベントを送ってから、結果のイベントを待つ時間（秒）。
            should_stop: REST API での分割取得の間に呼び、True を返したら中断する。
            namespace_wait: Socket.io のバージョンの判定に使う待ち時間（秒）。SocketIOSession を参照。

        Raises:
            ValueError: 接続先の形式が不正な場合。
        """
        self._reader = VolumioRestClient(host, timeout=timeout, should_stop=should_stop)
        self._session = SocketIOSession(host, timeout=timeout, namespace_wait=namespace_wait)
        self._response_timeout = response_timeout

    @property
    def kind(self) -> str:
        """クライアントの種類。"""
        return "websocket"

    @property
    def protocol(self) -> SocketIOProtocol | None:
        """判定した Socket.io のバージョン。WebSocket に接続する前は None。"""
        return self._session.protocol

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        self.close()

    def close(self) -> None:
        """WebSocket の接続を閉じる。"""
        self._session.close()

    def list_playlists(self) -> list[str]:
        """既存のプレイリスト名の一覧を返す（REST API）。"""
        return self._reader.list_playlists()

    def browse(self, uri: str) -> list[BrowseItem]:
        """ライブラリの指定した URI の直下の項目を返す（REST API）。"""
        return self._reader.browse(uri)

    def create_playlist(self, name: str) -> None:
        """空のプレイリストを作成する。

        Raises:
            ValueError: 名前が空、またはファイル名に使えない文字を含む場合。
            PlaylistExistsError: 同じ名前のプレイリストが既にある場合。
            VolumioError: 通信に失敗した場合、または Volumio が作成に失敗した場合。
        """
        name = validate_playlist_name(name)
        self._session.open()
        self._session.emit("createPlaylist", {"name": name})
        result = self._session.wait_for("pushCreatePlaylist", self._response_timeout)
        if isinstance(result, dict) and result.get("success") is True:
            return

        reason = result.get("reason") if isinstance(result, dict) else None
        if reason == _PLAYLIST_EXISTS_REASON:
            raise PlaylistExistsError("playlist already exists")

        raise VolumioError(f"Volumio failed to create playlist: {reason or 'unknown reason'}")

    def add_to_playlist(self, name: str, uri: str, service: str) -> None:
        """プレイリストの末尾に曲を追加する。

        Volumio 3 系では、存在しないプレイリストを指定すると空のプレイリストが作られるため、
        create_playlist で作成したプレイリストにだけ使う。

        Raises:
            ValueError: 名前が空、またはファイル名に使えない文字を含む場合。
            VolumioError: 通信に失敗した場合、時間内に結果が届かない場合、または Volumio が失敗を返した場合。
        """
        name = validate_playlist_name(name)
        self._session.open()
        self._session.emit("addToPlaylist", {"name": name, "service": service, "uri": uri})
        result = self._session.wait_for("pushAddToPlaylist", self._response_timeout)
        # 成功時の内容（お気に入りかどうかの情報）は使わない。失敗時だけ success: false が返る
        if isinstance(result, dict) and result.get("success") is False:
            raise VolumioError("Volumio failed to add track")
