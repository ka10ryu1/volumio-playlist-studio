import unicodedata

import pytest
from conftest import TrackFactory

from volumio_playlist_studio.volumio import (
    BrowseItem,
    MatchStatus,
    MockVolumioClient,
    PlaylistExistsError,
    VolumioError,
    collect_track_uris,
    match_tracks_to_uris,
    register_playlist,
)

URIS = [
    "music-library/INTERNAL/アーティスト/アルバム/01 曲.flac",
    "music-library/INTERNAL/アーティスト/アルバム/02 曲.flac",
    "music-library/USB/backup/アーティスト/アルバム/02 曲.flac",
    "music-library/INTERNAL/Other/xsong.flac",
]


def test_tracks_are_matched_by_full_relative_path(make_track: TrackFactory) -> None:
    track = make_track(rel_path="アーティスト/アルバム/01 曲.flac")

    [match] = match_tracks_to_uris([track], URIS)

    assert match.status == MatchStatus.MATCHED
    assert match.uri == URIS[0]


def test_same_relative_path_in_two_storages_is_ambiguous(make_track: TrackFactory) -> None:
    [match] = match_tracks_to_uris([make_track(rel_path="アーティスト/アルバム/02 曲.flac")], URIS)

    assert match.status == MatchStatus.AMBIGUOUS
    assert match.uri is None
    assert match.candidates == sorted([URIS[1], URIS[2]])


@pytest.mark.parametrize(
    "rel_path",
    [
        "アーティスト/アルバム/03 曲.flac",
        # 階層の途中で一致するだけでは対応させない
        "song.flac",
        "Other/song.flac",
        # 拡張子が違えば別のファイル
        "アーティスト/アルバム/01 曲.mp3",
    ],
)
def test_unmatched_tracks_are_reported(make_track: TrackFactory, rel_path: str) -> None:
    [match] = match_tracks_to_uris([make_track(rel_path=rel_path)], URIS)

    assert match.status == MatchStatus.UNMATCHED
    assert match.uri is None


def test_unicode_normalization_differences_are_tolerated(make_track: TrackFactory) -> None:
    # macOS が作るファイル名は NFD のことがある（「ガ」が「カ」＋濁点になる）
    nfd = unicodedata.normalize("NFD", "ガイド/バラード.flac")
    uris = ["music-library/INTERNAL/ガイド/バラード.flac"]

    [match] = match_tracks_to_uris([make_track(rel_path=nfd)], uris)

    assert match.status == MatchStatus.MATCHED


def test_collect_track_uris_walks_folders() -> None:
    client = MockVolumioClient.from_track_uris(URIS)

    assert sorted(collect_track_uris(client)) == sorted(URIS)


def test_collect_track_uris_stops_on_cycles_and_limits() -> None:
    folder = BrowseItem("folder", "loop", "music-library/loop")
    client = MockVolumioClient(
        library={
            "music-library": [folder, BrowseItem("song", "a", "music-library/a.flac", "mpd")],
            "music-library/loop": [BrowseItem("folder", "up", "music-library"), folder],
        }
    )

    assert collect_track_uris(client) == ["music-library/a.flac"]
    with pytest.raises(VolumioError, match="too many"):
        collect_track_uris(client, max_items=2)


def test_register_playlist_adds_tracks_in_order() -> None:
    client = MockVolumioClient.from_track_uris(URIS)

    outcome = register_playlist(client, " 夜の曲 ", [(1, URIS[1]), (2, URIS[0])])

    assert outcome.status == "success"
    assert outcome.error is None
    assert client.playlists == {"夜の曲": [URIS[1], URIS[0]]}
    assert [(i.position, i.track_id, i.status) for i in outcome.items] == [(0, 1, "added"), (1, 2, "added")]


def test_existing_playlist_is_never_overwritten() -> None:
    client = MockVolumioClient(playlists={"既存": ["music-library/old.flac"]})

    with pytest.raises(PlaylistExistsError):
        register_playlist(client, "既存", [(1, URIS[0])])

    assert client.playlists == {"既存": ["music-library/old.flac"]}


def test_failure_midway_reports_which_tracks_were_added() -> None:
    client = MockVolumioClient(fail_on_add=2)

    outcome = register_playlist(client, "途中で失敗", [(1, URIS[0]), (2, URIS[1]), (3, URIS[3])])

    assert outcome.status == "partial"
    assert outcome.error is not None
    assert "2 曲目" in outcome.error
    assert [i.status for i in outcome.items] == ["added", "failed", "skipped"]
    assert client.playlists == {"途中で失敗": [URIS[0]]}


def test_failure_on_first_track_is_failed() -> None:
    client = MockVolumioClient(fail_on_add=1)

    outcome = register_playlist(client, "失敗", [(1, URIS[0])])

    assert outcome.status == "failed"
    assert [i.status for i in outcome.items] == ["failed"]


def test_failure_to_create_playlist_adds_nothing() -> None:
    client = MockVolumioClient(fail_on_create=True)

    outcome = register_playlist(client, "作れない", [(1, URIS[0])])

    assert outcome.status == "failed"
    assert [i.status for i in outcome.items] == ["skipped"]
    assert client.playlists == {}


def test_unmatched_tracks_are_skipped() -> None:
    client = MockVolumioClient()

    outcome = register_playlist(client, "一部のみ", [(1, URIS[0]), (2, None)])

    assert outcome.status == "partial"
    assert [(i.status, i.error) for i in outcome.items] == [
        ("added", None),
        ("skipped", "Volumio 上の URI と対応していません"),
    ]


@pytest.mark.parametrize(
    ("name", "entries"),
    [
        ("  ", [(1, URIS[0])]),
        ("名前", [(1, None)]),
        ("名前", []),
        # Volumio 3 系はプレイリスト名をファイル名に使うため、別の場所を指す名前は登録しない
        ("../設定", [(1, URIS[0])]),
        ("a/b", [(1, URIS[0])]),
        ("a\\b", [(1, URIS[0])]),
        (".隠し", [(1, URIS[0])]),
        ("タブ\t入り", [(1, URIS[0])]),
    ],
)
def test_invalid_registration_is_rejected(name: str, entries: list[tuple[int, str | None]]) -> None:
    client = MockVolumioClient()

    with pytest.raises(ValueError, match=r"name|URI"):
        register_playlist(client, name, entries)

    assert client.playlists == {}


@pytest.mark.parametrize("name", ["夜の曲 (2026)", "A.B", "曲名...", "ゲームサントラ（オススメ）", "夜の　曲"])
def test_playlist_names_with_ordinary_symbols_are_accepted(name: str) -> None:
    client = MockVolumioClient()

    outcome = register_playlist(client, f" {name} ", [(1, URIS[0])])

    assert outcome.status == "success"
    assert client.playlists == {name: [URIS[0]]}


def test_mock_rejects_unknown_playlist_and_service() -> None:
    client = MockVolumioClient(playlists={"p": []})

    with pytest.raises(VolumioError, match="does not exist"):
        client.add_to_playlist("missing", URIS[0], "mpd")

    with pytest.raises(VolumioError, match="service"):
        client.add_to_playlist("p", URIS[0], "spotify")

    with pytest.raises(PlaylistExistsError):
        client.create_playlist("p")

    assert client.kind == "mock"
