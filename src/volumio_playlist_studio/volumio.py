"""Volumio との連携のインターフェース、モック、楽曲 URI の照合、プレイリスト登録処理。

実機との通信は、読み取り（プレイリスト一覧・ライブラリの参照）を REST API（volumio_rest.py）、
プレイリストの作成・曲の追加を WebSocket（Socket.io）API（volumio_socketio.py）で行う。
画面の登録は、実機での確認が済むまでモッククライアントで処理の流れを確認する（docs/volumio.md）。

PC 上のファイルパスと Volumio 上の URI は、文字列の変換で対応付けない。Volumio のライブラリを
参照して得た実際の URI の一覧と照合し、一意に対応する場合だけ登録に使う。
"""

import unicodedata
from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from volumio_playlist_studio.jobs import JobControl
from volumio_playlist_studio.models import Track
from volumio_playlist_studio.storage.playlists import RegistrationItem

# Volumio の browse 応答で、曲を表す項目の type
_TRACK_TYPES = frozenset({"song", "track"})
_FOLDER_TYPES = frozenset({"folder"})
# 楽曲 URI を収集するとき、たどる項目数の上限（ループや巨大なライブラリでの暴走防止）
_DEFAULT_MAX_BROWSE_ITEMS = 100_000
_MPD_SERVICE = "mpd"


class VolumioError(Exception):
    """Volumio との通信、または Volumio 側の処理に失敗した場合の例外。"""


class PlaylistExistsError(VolumioError):
    """同じ名前のプレイリストが既に Volumio にある場合の例外。既存のプレイリストは上書きしない。"""


def validate_playlist_name(name: str) -> str:
    """Volumio に登録するプレイリスト名を検証し、前後の空白を除いて返す。

    Volumio 3 系はプレイリスト名をそのままファイル名に使う（`/data/playlist/<名前>`）。
    別の場所のファイルを作成・変更させないよう、パスの区切りや制御文字を含む名前、`.` で始まる名前は使わない。

    Raises:
        ValueError: 名前が空、または使えない文字を含む場合。
    """
    name = name.strip()
    if not name:
        raise ValueError("playlist name must not be empty")

    # 全角スペースなどの区切り文字は使えるよう、制御文字（Unicode の C で始まる分類）だけを拒否する
    if name.startswith(".") or any(c in "/\\" or unicodedata.category(c).startswith("C") for c in name):
        raise ValueError("playlist name contains characters that cannot be used")

    return name


@dataclass(frozen=True)
class BrowseItem:
    """Volumio のライブラリを参照したときの 1 項目。"""

    type: str
    title: str
    uri: str
    service: str | None = None


class VolumioReader(Protocol):
    """Volumio からの読み取り。Volumio の状態を変更しない。"""

    @property
    def kind(self) -> str:
        """クライアントの種類（`mock`、`rest` など）。登録履歴に記録する。"""
        ...

    def list_playlists(self) -> list[str]:
        """既存のプレイリスト名の一覧を返す。"""
        ...

    def browse(self, uri: str) -> list[BrowseItem]:
        """ライブラリの指定した URI の直下の項目を返す。"""
        ...


class VolumioClient(VolumioReader, Protocol):
    """Volumio への読み取りと書き込み。実機用とモックで同じインターフェースを実装する。"""

    def create_playlist(self, name: str) -> None:
        """空のプレイリストを作成する。失敗時は VolumioError を送出する。"""
        ...

    def add_to_playlist(self, name: str, uri: str, service: str) -> None:
        """プレイリストの末尾に曲を追加する。失敗時は VolumioError を送出する。"""
        ...


@dataclass
class MockVolumioClient:
    """実機の代わりに使うモック。ライブラリとプレイリストをメモリ上に持つ。

    Attributes:
        library: フォルダの URI ごとの直下の項目。
        playlists: プレイリスト名ごとの曲の URI。
        fail_on_add: この回数目（1 始まり）の曲追加で失敗させる（テスト・動作確認用）。
        fail_on_create: プレイリスト作成で失敗させる（テスト・動作確認用）。
    """

    library: dict[str, list[BrowseItem]] = field(default_factory=dict)
    playlists: dict[str, list[str]] = field(default_factory=dict)
    fail_on_add: int | None = None
    fail_on_create: bool = False
    _add_count: int = 0

    @property
    def kind(self) -> str:
        """クライアントの種類。"""
        return "mock"

    @classmethod
    def from_track_uris(cls, uris: list[str], root_uri: str = "music-library") -> MockVolumioClient:
        """曲の URI の一覧から、フォルダ構造を持つモックのライブラリを作る。"""
        library: dict[str, list[BrowseItem]] = {}
        for uri in uris:
            parts = uri.removeprefix(root_uri + "/").split("/")
            parent = root_uri
            for depth, part in enumerate(parts):
                child = f"{parent}/{part}"
                is_track = depth == len(parts) - 1
                item = BrowseItem("song" if is_track else "folder", part, child, _MPD_SERVICE if is_track else None)
                siblings = library.setdefault(parent, [])
                if item not in siblings:
                    siblings.append(item)

                parent = child

        return cls(library=library)

    def list_playlists(self) -> list[str]:
        """既存のプレイリスト名の一覧を返す。"""
        return sorted(self.playlists)

    def browse(self, uri: str) -> list[BrowseItem]:
        """ライブラリの指定した URI の直下の項目を返す。"""
        return list(self.library.get(uri, []))

    def create_playlist(self, name: str) -> None:
        """空のプレイリストを作成する。"""
        if self.fail_on_create:
            raise VolumioError("mock: failed to create playlist")

        if name in self.playlists:
            raise PlaylistExistsError("playlist already exists")

        self.playlists[name] = []

    def add_to_playlist(self, name: str, uri: str, service: str) -> None:
        """プレイリストの末尾に曲を追加する。"""
        self._add_count += 1
        if self.fail_on_add is not None and self._add_count >= self.fail_on_add:
            raise VolumioError("mock: failed to add track")

        if name not in self.playlists:
            raise VolumioError("playlist does not exist")

        if service != _MPD_SERVICE:
            raise VolumioError("unsupported service")

        self.playlists[name].append(uri)


def collect_track_uris(
    client: VolumioReader,
    root_uri: str = "music-library",
    max_items: int = _DEFAULT_MAX_BROWSE_ITEMS,
    control: JobControl | None = None,
) -> list[str]:
    """Volumio のライブラリをたどり、曲の URI を収集する。

    Args:
        client: Volumio からの読み取り。
        root_uri: たどり始める URI。
        max_items: たどる項目数の上限。
        control: 進捗通知（たどったフォルダの数, 見つかったフォルダの数）と中断の受け付け。

    Raises:
        VolumioError: 項目数が上限を超えた場合、中断した場合、または通信に失敗した場合。
    """
    control = control or JobControl()
    uris: list[str] = []
    queue = deque([root_uri])
    visited = {root_uri}
    count = 0
    while queue:
        if control.is_cancelled():
            raise VolumioError("cancelled")

        for item in client.browse(queue.popleft()):
            count += 1
            if count > max_items:
                raise VolumioError("too many items in Volumio library")

            if item.type in _TRACK_TYPES:
                uris.append(item.uri)
            elif item.type in _FOLDER_TYPES and item.uri not in visited:
                visited.add(item.uri)
                queue.append(item.uri)

        control.report(len(visited) - len(queue), len(visited))

    # 最後のフォルダの取得中に中断された場合も、不完全な一覧を返さない
    if control.is_cancelled():
        raise VolumioError("cancelled")

    return uris


class MatchStatus(StrEnum):
    """ファイルと Volumio の URI の照合結果。"""

    MATCHED = "matched"
    UNMATCHED = "unmatched"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class UriMatch:
    """1 曲分の照合結果。uri は一意に対応した場合だけ設定する。"""

    track_id: int
    status: MatchStatus
    uri: str | None
    candidates: list[str]


def _normalized_parts(path: str) -> tuple[str, ...]:
    # macOS が SMB 共有に作るファイル名は NFD のことがあるため、NFC にそろえてから比較する
    return tuple(unicodedata.normalize("NFC", p) for p in path.split("/") if p)


def match_tracks_to_uris(tracks: list[Track], uris: list[str]) -> list[UriMatch]:
    """ライブラリの相対パスと、Volumio から取得した URI の末尾を階層単位で照合する。

    URI の先頭（`music-library/INTERNAL` など、Volumio 上の保存場所）は推測せず、相対パスの全階層が
    URI の末尾と一致する候補を探す。候補が 1 つだけなら対応ありとし、複数ある場合は曖昧として登録に使わない。
    """
    index: dict[tuple[str, ...], list[str]] = {}
    for uri in uris:
        parts = _normalized_parts(uri)
        for start in range(len(parts)):
            index.setdefault(parts[start:], []).append(uri)

    matches = []
    for track in tracks:
        candidates = index.get(_normalized_parts(track.rel_path), [])
        if len(candidates) == 1:
            matches.append(UriMatch(track.id, MatchStatus.MATCHED, candidates[0], candidates))
        elif candidates:
            matches.append(UriMatch(track.id, MatchStatus.AMBIGUOUS, None, sorted(candidates)))
        else:
            matches.append(UriMatch(track.id, MatchStatus.UNMATCHED, None, []))

    return matches


@dataclass(frozen=True)
class RegistrationOutcome:
    """プレイリスト登録の結果。

    Attributes:
        status: `success`（全曲登録）、`partial`（一部のみ登録）、`failed`（1 曲も登録できない）。
        error: 失敗の内容（画面表示用）。
        items: 曲ごとの結果。
    """

    status: str
    error: str | None
    items: list[RegistrationItem]


def register_playlist(client: VolumioClient, name: str, entries: list[tuple[int, str | None]]) -> RegistrationOutcome:
    """Volumio に新しいプレイリストを作成し、曲を順に追加する。

    同じ名前のプレイリストがある場合は何もせずに例外を送出し、既存のプレイリストを変更しない。
    URI が対応しない曲は追加せず `skipped` とする。追加に失敗したらそこで止め、
    どの曲まで登録されたかを結果に残す。

    Args:
        client: Volumio のクライアント。
        name: 作成するプレイリスト名。
        entries: （楽曲 ID, URI）の並び。URI が対応しない曲は None。

    Raises:
        ValueError: 名前が空か使えない文字を含む場合、または登録できる曲が 1 曲もない場合。
        PlaylistExistsError: 同じ名前のプレイリストが既にある場合。
        VolumioError: 既存のプレイリスト一覧を取得できない場合。
    """
    name = validate_playlist_name(name)
    if not any(uri for _, uri in entries):
        raise ValueError("no track has a matched Volumio URI")

    if name in client.list_playlists():
        raise PlaylistExistsError("playlist already exists")

    try:
        client.create_playlist(name)
    except VolumioError as exc:
        skipped = [RegistrationItem(i, track_id, uri, "skipped") for i, (track_id, uri) in enumerate(entries)]
        return RegistrationOutcome("failed", f"プレイリストを作成できませんでした: {exc}", skipped)

    items, error = _add_tracks(client, name, entries)
    added = sum(1 for i in items if i.status == "added")
    if added == len(items):
        status = "success"
    elif added == 0:
        status = "failed"
    else:
        status = "partial"

    return RegistrationOutcome(status, error, items)


def _add_tracks(
    client: VolumioClient, name: str, entries: list[tuple[int, str | None]]
) -> tuple[list[RegistrationItem], str | None]:
    """曲を順に追加する。失敗したらそれ以降の曲は追加せず `skipped` にする。"""
    items: list[RegistrationItem] = []
    error: str | None = None
    for position, (track_id, uri) in enumerate(entries):
        if error is not None or uri is None:
            reason = None if uri is not None else "Volumio 上の URI と対応していません"
            items.append(RegistrationItem(position, track_id, uri, "skipped", reason))
            continue

        try:
            client.add_to_playlist(name, uri, _MPD_SERVICE)
        except VolumioError as exc:
            error = f"{position + 1} 曲目の追加に失敗しました: {exc}"
            items.append(RegistrationItem(position, track_id, uri, "failed", str(exc)))
        else:
            items.append(RegistrationItem(position, track_id, uri, "added"))

    return items, error
