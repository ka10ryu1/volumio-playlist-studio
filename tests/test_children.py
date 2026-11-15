"""子供向けのアルバムの判定と、選曲での扱いのテスト。

Jev とは通信せず、HTTP の送受信だけを httpx2.MockTransport で差し替える。
"""

import sqlite3
import threading
from dataclasses import replace

import httpx2
import pytest
from conftest import ClassificationFactory, TrackFactory
from test_jev import NO_RETRY, Handler, Recorder, ok, status

from volumio_playlist_studio.albums import album_key, group_albums
from volumio_playlist_studio.catalog import judged_albums, load_candidates
from volumio_playlist_studio.classify.children import (
    CHILDREN_QUESTION,
    MAX_TRACK_TITLES,
    AlbumJudgment,
    build_album_questions,
    build_album_state,
    judge_album,
)
from volumio_playlist_studio.classify.jev import JevResponseError, JevUnavailableError, create_client
from volumio_playlist_studio.config import JEV_ALBUM_QUESTION_VERSION, JevSettings, PlaylistDefaults
from volumio_playlist_studio.jobs import JobControl
from volumio_playlist_studio.models import ClassificationSource, Mood, TitleSource, Track, TrackMetadata
from volumio_playlist_studio.mood_map import MOOD_POSITIONS, CircleRequest, select_circle_tracks
from volumio_playlist_studio.playlist import (
    REASON_CHILDREN,
    Candidate,
    ChildrenMode,
    PlaylistRequest,
    children_allowed,
    generate_playlist,
)
from volumio_playlist_studio.storage import settings_store
from volumio_playlist_studio.storage.album_judgments import (
    AlbumJudgmentRecord,
    load_children_probabilities,
    save_album_judgment,
)
from volumio_playlist_studio.storage.database import transaction
from volumio_playlist_studio.storage.jev_usage import JevUsageKind, list_usage
from volumio_playlist_studio.storage.tracks import list_tracks, upsert_track
from volumio_playlist_studio.workflows import estimate_children_albums, run_children_albums

ROOT = "/music"
DEFAULTS = PlaylistDefaults()


def album_response(probability: float = 0.9) -> dict[str, object]:
    return {
        "model": "jev-1.13.0",
        "answers": {CHILDREN_QUESTION: {"type": "noul", "noul": probability}},
        "usage": {"input_tokens": 200, "output_tokens": 10},
    }


def judge(recorder: Recorder, tracks: list[Track], settings: JevSettings | None = None) -> AlbumJudgment:
    settings = settings or JevSettings()
    with create_client(settings, transport=httpx2.MockTransport(recorder), retry=NO_RETRY) as client:
        return judge_album(client, tracks, settings)


def register(db: sqlite3.Connection, rel_path: str, album: str | None, title: str = "曲") -> int:
    meta = TrackMetadata(
        rel_path=rel_path,
        title=title,
        artist="アーティスト",
        album=album,
        genre=None,
        year=None,
        track_number=None,
        duration_sec=200.0,
        bitrate=None,
        sample_rate=None,
        channels=None,
        codec="flac",
        file_size=1,
        mtime_ns=0,
        title_source=TitleSource.TAG,
        tags_repaired=False,
        quick_hash=None,
    )
    with transaction(db):
        return upsert_track(db, ROOT, meta, None)


def register_albums(db: sqlite3.Connection, count: int, tracks_per_album: int = 2) -> None:
    for a in range(count):
        for t in range(tracks_per_album):
            register(db, f"A/アルバム{a}/{t:02d}.flac", f"アルバム{a}", f"曲{a}-{t}")


def mock(*handlers: Handler) -> tuple[Recorder, httpx2.MockTransport]:
    recorder = Recorder(list(handlers))
    return recorder, httpx2.MockTransport(recorder)


# --- アルバムのまとまり -----------------------------------------------------------


def test_tracks_in_same_folder_and_album_form_one_album(make_track: TrackFactory) -> None:
    a = make_track(rel_path="A/X/01.flac", album="X", track_number=2)
    b = make_track(rel_path="A/X/02.flac", album="X", track_number=1)
    other_album = make_track(rel_path="A/X/03.flac", album="Y")
    other_folder = make_track(rel_path="B/X/01.flac", album="X")

    albums = group_albums([a, b, other_album, other_folder])

    assert album_key(a) == album_key(b)
    assert len({album_key(t) for t in (a, other_album, other_folder)}) == 3
    # アルバム内はトラック番号順
    assert [t.id for t in albums[album_key(a)]] == [b.id, a.id]


def test_tracks_without_number_come_last(make_track: TrackFactory) -> None:
    unnumbered = make_track(rel_path="X/a.flac", album="X", track_number=None)
    numbered = make_track(rel_path="X/b.flac", album="X", track_number=5)

    assert group_albums([unnumbered, numbered])[album_key(numbered)] == [numbered, unnumbered]


# --- 送信内容と応答の解釈 ----------------------------------------------------------


def test_request_sends_album_tags_and_titles_but_no_paths(make_track: TrackFactory) -> None:
    tracks = [
        make_track(rel_path="Kids/うた/01.flac", album="こどものうた", artist="うたのおねえさん", title="ぞうさん"),
        make_track(rel_path="Kids/うた/02.flac", album="こどものうた", artist="うたのおねえさん", title="さんぽ"),
    ]
    recorder = Recorder([ok(album_response())])

    judge(recorder, tracks)

    [request] = recorder.requests
    body = request["body"]
    assert isinstance(body, dict)
    album = body["state"]["album"]
    assert album == {
        "track_count": 2,
        "title": "こどものうた",
        "artists": ["うたのおねえさん"],
        "track_titles": ["ぞうさん", "さんぽ"],
    }
    assert "Kids/うた" not in str(body)
    assert body["questions"][CHILDREN_QUESTION]["type"] == "noul"
    assert set(body["questions"]) == set(build_album_questions())


def test_titles_are_not_sent_when_disabled_and_are_capped(make_track: TrackFactory) -> None:
    tracks = [make_track(title=f"曲{i}") for i in range(MAX_TRACK_TITLES + 5)]

    without_titles = build_album_state(tracks, JevSettings(send_titles=False))["album"]
    with_titles = build_album_state(tracks, JevSettings())["album"]

    assert isinstance(without_titles, dict)
    assert isinstance(with_titles, dict)
    assert "track_titles" not in without_titles
    assert len(with_titles["track_titles"]) == MAX_TRACK_TITLES


def test_empty_album_is_rejected() -> None:
    with pytest.raises(ValueError, match="no tracks"):
        build_album_state([], JevSettings())


def test_response_gives_probability_model_and_tokens(make_track: TrackFactory) -> None:
    result = judge(Recorder([ok(album_response(0.8))]), [make_track()])

    assert result == AlbumJudgment(children_probability=0.8, model="jev-1.13.0", input_tokens=200, output_tokens=10)


def test_missing_answer_is_rejected(make_track: TrackFactory) -> None:
    body: dict[str, object] = {"model": "jev-1.13.0", "answers": {}, "usage": {}}

    with pytest.raises(JevResponseError):
        judge(Recorder([ok(body)]), [make_track()])


# --- 保存 ----------------------------------------------------------------------------


def test_judgment_round_trip_is_replaced_and_separated_by_version(db: sqlite3.Connection) -> None:
    with transaction(db):
        save_album_judgment(db, AlbumJudgmentRecord(ROOT, "k", "v1", 0.2, "m"))
        save_album_judgment(db, AlbumJudgmentRecord(ROOT, "k", "v1", 0.7, "m"))
        save_album_judgment(db, AlbumJudgmentRecord(ROOT, "k", "v2", 0.1, "m"))
        save_album_judgment(db, AlbumJudgmentRecord("/other", "k", "v1", 0.9, "m"))

    assert load_children_probabilities(db, ROOT, "v1") == {"k": 0.7}
    assert load_children_probabilities(db, ROOT, "v2") == {"k": 0.1}


@pytest.mark.parametrize("probability", [-0.01, 1.01])
def test_out_of_range_probability_is_rejected_and_nothing_saved(db: sqlite3.Connection, probability: float) -> None:
    with pytest.raises(ValueError, match="between 0 and 1"), transaction(db):
        save_album_judgment(db, AlbumJudgmentRecord(ROOT, "k", "v1", probability, None))

    assert load_children_probabilities(db, ROOT, "v1") == {}


# --- 判定の実行 ------------------------------------------------------------------------


def test_estimate_and_run_judge_each_album_once(db: sqlite3.Connection) -> None:
    register_albums(db, 3)
    recorder, transport = mock(ok(album_response(0.9)), ok(album_response(0.1)))

    estimate = estimate_children_albums(db, ROOT)
    message = run_children_albums(db, ROOT, JobControl(), transport)

    assert (estimate.pending_albums, estimate.albums_this_run, estimate.requests_this_run) == (3, 3, 3)
    # 1 アルバム 1 リクエスト（曲数の 6 ではない）
    assert len(recorder.requests) == 3
    assert "判定 3 アルバム（うち子供向け 1）" in message
    assert "未判定の残り 0 アルバム" in message
    assert estimate_children_albums(db, ROOT).pending_albums == 0

    # 判定済みのアルバムには再び問い合わせない
    run_children_albums(db, ROOT, JobControl(), transport)
    assert len(recorder.requests) == 3
    # 応答 1 件あたり入力 200・出力 10 トークン。判定するアルバムがなかった 2 回目は記録しない
    [usage] = list_usage(db)
    assert (usage.kind, usage.question_version) == (JevUsageKind.ALBUMS, JEV_ALBUM_QUESTION_VERSION)
    assert (usage.succeeded, usage.failed, usage.input_tokens, usage.output_tokens) == (3, 0, 600, 30)


def test_run_respects_limit_per_run(db: sqlite3.Connection) -> None:
    register_albums(db, 3)
    with transaction(db):
        settings_store.set_jev_settings(db, JevSettings(max_tracks_per_run=2))

    recorder, transport = mock(ok(album_response()))

    assert estimate_children_albums(db, ROOT).albums_this_run == 2
    message = run_children_albums(db, ROOT, JobControl(), transport)

    assert len(recorder.requests) == 2
    assert "未判定の残り 1 アルバム" in message


def test_authentication_error_stops_at_once_and_saves_nothing(db: sqlite3.Connection) -> None:
    register_albums(db, 3)
    recorder, transport = mock(status(401))

    message = run_children_albums(db, ROOT, JobControl(), transport)

    assert len(recorder.requests) == 1
    assert "API キーが無効" in message
    assert load_children_probabilities(db, ROOT, JEV_ALBUM_QUESTION_VERSION) == {}


def test_consecutive_failures_stop_the_run_but_keep_earlier_results(db: sqlite3.Connection) -> None:
    register_albums(db, 6)
    with transaction(db):
        settings_store.set_jev_settings(db, JevSettings(max_consecutive_failures=2))

    bad = ok({"model": "jev-1.13.0", "answers": {}, "usage": {}})
    recorder, transport = mock(ok(album_response()), bad, bad, ok(album_response()))

    message = run_children_albums(db, ROOT, JobControl(), transport)

    assert len(recorder.requests) == 3
    assert "2 回連続で失敗" in message
    assert len(load_children_probabilities(db, ROOT, JEV_ALBUM_QUESTION_VERSION)) == 1
    [usage] = list_usage(db)
    assert (usage.succeeded, usage.failed, usage.input_tokens, usage.output_tokens) == (1, 2, 200, 10)


def test_cancelled_run_sends_nothing(db: sqlite3.Connection) -> None:
    register_albums(db, 2)
    cancel = threading.Event()
    cancel.set()
    control = JobControl(cancel=cancel)
    recorder, transport = mock(ok(album_response()))

    message = run_children_albums(db, ROOT, control, transport)

    assert recorder.requests == []
    assert "中断しました" in message
    assert list_usage(db) == []


def test_run_requires_api_key(db: sqlite3.Connection) -> None:
    register_albums(db, 1)

    # conftest で TYPESAFE_API_KEY を空にしている
    with pytest.raises(JevUnavailableError):
        run_children_albums(db, ROOT, JobControl())


# --- 候補への反映 ----------------------------------------------------------------------


def test_candidates_use_album_judgment_and_threshold(db: sqlite3.Connection) -> None:
    kids = register(db, "Kids/X/01.flac", "X")
    plain = register(db, "Pop/Y/01.flac", "Y")
    unjudged = register(db, "Pop/Z/01.flac", "Z")
    tracks = {t.id: t for t in list_tracks(db, ROOT)}
    with transaction(db):
        version = JEV_ALBUM_QUESTION_VERSION
        save_album_judgment(db, AlbumJudgmentRecord(ROOT, album_key(tracks[kids]), version, 0.9, None))
        save_album_judgment(db, AlbumJudgmentRecord(ROOT, album_key(tracks[plain]), version, 0.85, None))

    def flags() -> dict[int, bool | None]:
        return {c.track.id: c.for_children for c in load_candidates(db, ROOT, ClassificationSource.RULES)}

    # 既定の閾値は 0.9 で、ちょうど 0.9 は子供向け、それ未満は子供向けではない
    assert flags() == {kids: True, plain: False, unjudged: None}

    # 閾値を変えても再判定せずに反映される
    with transaction(db):
        settings_store.set_jev_settings(db, JevSettings(children_threshold=0.8))

    assert flags() == {kids: True, plain: True, unjudged: None}

    with transaction(db):
        settings_store.set_jev_settings(db, JevSettings(children_threshold=0.95))

    assert flags() == {kids: False, plain: False, unjudged: None}


def test_judged_albums_lists_most_likely_first(db: sqlite3.Connection) -> None:
    kids = register(db, "Kids/X/01.flac", "こどものうた")
    register(db, "Kids/X/02.flac", "こどものうた")
    loose = register(db, "single.flac", None)
    register(db, "Pop/Z/01.flac", "未判定")
    tracks = {t.id: t for t in list_tracks(db, ROOT)}
    with transaction(db):
        version = JEV_ALBUM_QUESTION_VERSION
        save_album_judgment(db, AlbumJudgmentRecord(ROOT, album_key(tracks[loose]), version, 0.1, None))
        save_album_judgment(db, AlbumJudgmentRecord(ROOT, album_key(tracks[kids]), version, 0.95, None))

    albums = judged_albums(db, ROOT)

    assert [(a.title, a.track_count, a.children_probability) for a in albums] == [
        ("こどものうた", 2, 0.95),
        ("（ライブラリ直下）", 1, 0.1),
    ]


# --- 選曲 ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (ChildrenMode.EXCLUDE, {False: True, None: True, True: False}),
        (ChildrenMode.INCLUDE, {False: True, None: True, True: True}),
        (ChildrenMode.ONLY, {False: False, None: False, True: True}),
    ],
)
def test_children_mode_treats_unjudged_tracks_as_not_for_children(
    make_track: TrackFactory, mode: ChildrenMode, expected: dict[bool | None, bool]
) -> None:
    for flag, allowed in expected.items():
        assert children_allowed(Candidate(make_track(), None, None, for_children=flag), mode) is allowed


@pytest.fixture
def mixed_library(make_track: TrackFactory, make_classification: ClassificationFactory) -> list[Candidate]:
    """リラックス向けの曲を、子供向け・子供向けでない・未判定の 3 種類で 10 曲ずつ用意する。"""
    candidates = []
    for flag in (True, False, None):
        for i in range(10):
            classification = make_classification(
                engine="jev", relaxation=0.9, energy=0.2, calmness=0.9, brightness=MOOD_POSITIONS[Mood.RELAX].x
            )
            track = make_track(artist=f"アーティスト{i % 5}")
            candidates.append(Candidate(track, classification, classification.genre, for_children=flag))

    return candidates


def flags_of(candidates: list[Candidate]) -> set[bool | None]:
    return {c.for_children for c in candidates}


def test_playlist_excludes_children_by_default(mixed_library: list[Candidate]) -> None:
    result = generate_playlist(mixed_library, PlaylistRequest(Mood.RELAX, duration_min=None, track_count=30), DEFAULTS)

    assert flags_of([t.candidate for t in result.tracks]) == {False, None}
    assert result.shortfall is not None
    assert result.shortfall.excluded[REASON_CHILDREN] == 10


def test_playlist_with_only_children(mixed_library: list[Candidate]) -> None:
    request = PlaylistRequest(Mood.RELAX, duration_min=None, track_count=30, children_mode=ChildrenMode.ONLY)

    result = generate_playlist(mixed_library, request, DEFAULTS)

    assert flags_of([t.candidate for t in result.tracks]) == {True}
    assert result.shortfall is not None
    assert "子供向けのアルバムを Jev で判定" in " ".join(result.shortfall.suggestions)


def test_playlist_including_children(mixed_library: list[Candidate]) -> None:
    request = PlaylistRequest(Mood.RELAX, duration_min=None, track_count=30, children_mode=ChildrenMode.INCLUDE)

    result = generate_playlist(mixed_library, request, DEFAULTS)

    assert len(result.tracks) == 30
    assert result.shortfall is None


@pytest.mark.parametrize(
    ("mode", "expected"),
    [(ChildrenMode.EXCLUDE, {False, None}), (ChildrenMode.INCLUDE, {True, False, None}), (ChildrenMode.ONLY, {True})],
)
def test_circle_follows_children_mode(
    mixed_library: list[Candidate], mode: ChildrenMode, expected: set[bool | None]
) -> None:
    request = CircleRequest(Mood.RELAX, radius=DEFAULTS.max_circle_radius, children_mode=mode)

    chosen = select_circle_tracks(mixed_library, request, DEFAULTS)

    assert flags_of([t.candidate for t in chosen]) == expected


def test_children_mode_is_saved_with_conditions() -> None:
    request = PlaylistRequest(Mood.RELAX, children_mode=ChildrenMode.ONLY)
    circle = CircleRequest(Mood.RELAX, radius=DEFAULTS.max_circle_radius)

    assert request.to_dict()["children_mode"] == "only"
    assert replace(request, children_mode=ChildrenMode.EXCLUDE).to_dict()["children_mode"] == "exclude"
    assert circle.to_dict()["children_mode"] == "exclude"
