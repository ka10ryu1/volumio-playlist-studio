"""Volumio の REST API による読み取り専用のクライアント。

公式ドキュメント（https://developers.volumio.com/ の REST API）にある、プレイリスト一覧（`/api/v1/listplaylists`）と
ライブラリの参照（`/api/v1/browse`）だけを使う。Volumio の状態を変更する API は呼ばない。
接続先はローカルネットワーク内の Volumio に限り、設定値（.env の VOLUMIO_HOST または設定画面）から受け取る。
"""

import http.client
import json
import logging
import re
from http import HTTPStatus
from urllib.parse import urlencode

from volumio_playlist_studio.volumio import BrowseItem, VolumioError

logger = logging.getLogger(__name__)

# ホスト名または IPv4 アドレスと、省略可能なポート番号。URL の他の部分（パスや認証情報）を混ぜられないようにする
_HOST_PATTERN = re.compile(r"^(?P<host>[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?)(?::(?P<port>\d{1,5}))?$")
_DEFAULT_PORT = 80
_MAX_PORT = 65535
_TRACK_TYPES = frozenset({"song", "track"})
_LIBRARY_PREFIX = "music-library"


def parse_host(text: str) -> tuple[str, int]:
    """設定された Volumio の接続先を、ホスト名とポート番号に分ける。

    Args:
        text: `volumio.local` や `192.168.1.22:3000` の形式の文字列。

    Returns:
        （ホスト名, ポート番号）。

    Raises:
        ValueError: 形式が不正な場合。
    """
    match = _HOST_PATTERN.match(text.strip())
    if match is None:
        raise ValueError("invalid volumio host")

    port = int(match.group("port") or _DEFAULT_PORT)
    if not 1 <= port <= _MAX_PORT:
        raise ValueError("invalid volumio port")

    return match.group("host"), port


class VolumioRestClient:
    """Volumio の REST API による読み取り専用のクライアント。"""

    def __init__(self, host: str, timeout: float = 10.0, page_size: int = 500) -> None:
        """接続先を設定する。通信はメソッドを呼んだときに行う。

        Args:
            host: Volumio の接続先（`volumio.local` など）。
            timeout: 1 回のリクエストのタイムアウト（秒）。
            page_size: ライブラリを参照するとき、1 回のリクエストで受け取る項目数。

        Raises:
            ValueError: 接続先の形式が不正な場合。
        """
        self._host, self._port = parse_host(host)
        self._timeout = timeout
        self._page_size = page_size

    @property
    def kind(self) -> str:
        """クライアントの種類。"""
        return "rest"

    def list_playlists(self) -> list[str]:
        """既存のプレイリスト名の一覧を返す。

        Raises:
            VolumioError: 通信に失敗した場合、または応答の形式が想定と異なる場合。
        """
        data = self._get_json("/api/v1/listplaylists")
        if not isinstance(data, list) or not all(isinstance(name, str) for name in data):
            raise VolumioError("unexpected response from listplaylists")

        return list(data)

    def browse(self, uri: str) -> list[BrowseItem]:
        """ライブラリの指定した URI の直下の項目を返す。項目が多い場合は分割して取得する。

        曲は `song`、たどれる項目（ストレージやフォルダ）は `folder` にそろえる。
        ライブラリの外を指す項目（Web ラジオなど）は含めない。

        Raises:
            VolumioError: 通信に失敗した場合、または応答の形式が想定と異なる場合。
        """
        items: list[BrowseItem] = []
        offset = 0
        while True:
            query = urlencode({"uri": uri, "limit": self._page_size, "offset": offset})
            page, total = _parse_browse(self._get_json(f"/api/v1/browse?{query}"))
            items.extend(page)
            offset += self._page_size
            # count がない応答は分割取得に対応していないため、1 回で終える
            if total is None or offset >= total or not page:
                return items

    def _get_json(self, path: str) -> object:
        connection = http.client.HTTPConnection(self._host, self._port, timeout=self._timeout)
        try:
            connection.request("GET", path, headers={"Accept": "application/json"})
            response = connection.getresponse()
            body = response.read()
        except (OSError, http.client.HTTPException) as exc:
            logger.warning("volumio request failed: error=%s", type(exc).__name__)
            raise VolumioError(f"cannot connect to Volumio: {type(exc).__name__}") from exc
        finally:
            connection.close()

        if response.status != HTTPStatus.OK:
            raise VolumioError(f"Volumio returned HTTP {response.status}")

        try:
            data: object = json.loads(body)
        except ValueError as exc:
            raise VolumioError("Volumio returned invalid JSON") from exc

        return data


def _parse_browse(data: object) -> tuple[list[BrowseItem], int | None]:
    """ライブラリ参照（browse）の応答から項目と総数を取り出す。総数は分割取得に対応した応答だけにある。"""
    navigation = data.get("navigation") if isinstance(data, dict) else None
    lists = navigation.get("lists") if isinstance(navigation, dict) else None
    if not isinstance(lists, list):
        raise VolumioError("unexpected response from browse")

    items: list[BrowseItem] = []
    total: int | None = None
    for entry in lists:
        if not isinstance(entry, dict):
            continue

        raw_items = entry.get("items", [])
        if isinstance(raw_items, list):
            items.extend(item for item in (_to_item(raw) for raw in raw_items) if item is not None)

        count = entry.get("count")
        if isinstance(count, int):
            total = count if total is None else max(total, count)

    return items, total


def _to_item(raw: object) -> BrowseItem | None:
    if not isinstance(raw, dict):
        return None

    uri, kind, title = raw.get("uri"), raw.get("type"), raw.get("title")
    if not isinstance(uri, str) or not isinstance(kind, str):
        return None

    service = raw.get("service") if isinstance(raw.get("service"), str) else None
    display = title if isinstance(title, str) else uri
    if kind in _TRACK_TYPES:
        return BrowseItem("song", display, uri, service)

    # ストレージ（INTERNAL・USB・NAS）やフォルダは、Volumio のバージョンによって type が異なる可能性があるため、
    # ライブラリ内の URI であればたどれる項目として扱う
    if uri.startswith(f"{_LIBRARY_PREFIX}/"):
        return BrowseItem("folder", display, uri, service)

    logger.debug("volumio browse item skipped: type=%s", kind)
    return None
