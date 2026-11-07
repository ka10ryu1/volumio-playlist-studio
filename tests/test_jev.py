"""Jev 連携のテスト。HTTP の送受信だけを httpx2.MockTransport で差し替え、実際の API とは通信しない。

応答の形式は https://docs.typesafe.ai/introduction/quickstart.md の Raw Response に合わせている。
"""

import json
import sqlite3
from collections.abc import Callable
from compression import zstd

import httpx2
import pytest
from conftest import TrackFactory
from typesafe_sdk import RetryPolicy, TypeSafeClient

from volumio_playlist_studio.classify.jev import (
    JevClassifier,
    JevResponseError,
    JevUnavailableError,
    build_questions,
    build_state,
    create_client,
)
from volumio_playlist_studio.classify.runner import ClassificationRunSummary, RunLimits, classify_tracks
from volumio_playlist_studio.config import JEV_QUESTION_VERSION, JevSettings
from volumio_playlist_studio.jobs import JobControl
from volumio_playlist_studio.models import Attribute, AudioFeatures, Genre, Mood, TitleSource, TrackMetadata
from volumio_playlist_studio.storage.analysis import list_errors, load_classifications
from volumio_playlist_studio.storage.tracks import get_track, upsert_track
from volumio_playlist_studio.workflows import estimate_jev, run_jev

Handler = Callable[[httpx2.Request], httpx2.Response]
NO_RETRY = RetryPolicy(max_retries=0)

ATTRIBUTE_KEYS = [a.value for a in Attribute]


def score_answer(level_probabilities: list[float], confidence: float = 0.9) -> dict[str, object]:
    expected = sum(i * p for i, p in enumerate(level_probabilities))
    return {
        "type": "score",
        "score": expected,
        "confidence": confidence,
        "legend": {str(i): f"level {i}" for i in range(len(level_probabilities))},
        "probabilities": {str(i): p for i, p in enumerate(level_probabilities)},
    }


def jev_response(
    genre: str = "jazz",
    genre_confidence: float = 0.9,
    determinable: float = 0.95,
    scores: dict[str, dict[str, object]] | None = None,
) -> dict[str, object]:
    calm = score_answer([0.0, 0.0, 0.0, 0.0, 1.0])
    low = score_answer([1.0, 0.0, 0.0, 0.0, 0.0])
    answers: dict[str, object] = {
        "genre": {
            "type": "choice",
            "choice": genre,
            "confidence": genre_confidence,
            "probabilities": {genre: genre_confidence, "pop": 1 - genre_confidence},
        },
        "genre_determinable": {"type": "noul", "noul": determinable},
        "relaxation": calm,
        "energy": low,
        "brightness": score_answer([0.0, 0.0, 1.0, 0.0, 0.0]),
        "calmness": calm,
        "work_bgm": score_answer([0.0, 0.0, 0.5, 0.5, 0.0]),
        "party": low,
        "night_bgm": score_answer([0.0, 0.0, 0.0, 1.0, 0.0]),
    }
    answers.update(scores or {})
    return {"model": "jev-1.13.0", "answers": answers, "usage": {"input_tokens": 400, "output_tokens": 70}}


class Recorder:
    """送信されたリクエストを記録し、決まった応答を返す。"""

    def __init__(self, responses: list[Callable[[httpx2.Request], httpx2.Response]]) -> None:
        self.requests: list[dict[str, object]] = []
        self._responses = responses

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        content = request.content
        # SDK は既定で送信本文を zstd で圧縮する
        if request.headers.get("content-encoding") == "zstd":
            content = zstd.decompress(content)

        self.requests.append({"url": str(request.url), "headers": dict(request.headers), "body": json.loads(content)})
        responder = self._responses[min(len(self.requests), len(self._responses)) - 1]
        return responder(request)


def ok(body: dict[str, object] | None = None) -> Handler:
    return lambda _: httpx2.Response(200, json=body or jev_response())


def status(code: int) -> Handler:
    return lambda _: httpx2.Response(code, json={"error": "simulated"})


def timeout() -> Handler:
    def _raise(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("simulated timeout", request=request)

    return _raise


def client_for(recorder: Recorder, settings: JevSettings | None = None) -> TypeSafeClient:
    return create_client(settings or JevSettings(), transport=httpx2.MockTransport(recorder), retry=NO_RETRY)


FEATURES = AudioFeatures(
    tempo_bpm=72.0,
    tempo_variation=0.02,
    rms_mean_db=-30.0,
    rms_std_db=2.0,
    loudness_variation_db=1.0,
    spectral_centroid_mean_hz=1000.0,
    spectral_centroid_std_hz=100.0,
    spectral_rolloff_mean_hz=2000.0,
    zero_crossing_rate_mean=0.03,
    onset_strength_mean=0.5,
    onset_strength_std=0.2,
    onset_rate_per_sec=0.7,
    chroma_mean=(0.5,) * 12,
    mfcc_mean=(0.0,) * 13,
    mfcc_std=(1.0,) * 13,
    analyzed_seconds=60.0,
    segment_count=3,
)


def test_classify_parses_typed_answers(make_track: TrackFactory) -> None:
    recorder = Recorder([ok()])
    with client_for(recorder) as client:
        classifier = JevClassifier(client=client)
        result = classifier.classify(make_track(genre="Jazz"), FEATURES)

    assert result.engine == "jev"
    assert result.engine_version == JEV_QUESTION_VERSION
    assert result.model == "jev-1.13.0"
    assert result.genre == Genre.JAZZ
    assert result.genre_confidence == 0.9
    assert result.attribute_value(Attribute.RELAXATION) == 1.0
    assert result.attribute_value(Attribute.ENERGY) == 0.0
    assert result.attribute_value(Attribute.BRIGHTNESS) == 0.5
    assert result.attribute_value(Attribute.WORK_BGM) == pytest.approx(0.625)
    assert result.attributes[Attribute.RELAXATION].probabilities == {"0": 0.0, "1": 0.0, "2": 0.0, "3": 0.0, "4": 1.0}
    assert result.mood == Mood.RELAX
    assert (classifier.input_tokens, classifier.output_tokens) == (400, 70)


def test_request_contains_only_minimal_structured_data(make_track: TrackFactory) -> None:
    recorder = Recorder([ok()])
    track = make_track(rel_path="秘密のフォルダ/曲.flac", title="曲名", artist="アーティスト", genre="Jazz", year=1999)
    with client_for(recorder) as client:
        JevClassifier(client=client).classify(track, FEATURES)

    request = recorder.requests[0]
    body = request["body"]
    assert isinstance(body, dict)
    assert request["url"] == "https://api.typesafe.ai/v1/systemone"
    assert set(body["questions"]) == {"genre", "genre_determinable", *ATTRIBUTE_KEYS}
    assert body["questions"]["genre"]["type"] == "choice"
    assert body["questions"]["genre_determinable"]["type"] == "noul"
    assert all(body["questions"][k]["type"] == "score" for k in ATTRIBUTE_KEYS)
    assert body["state"]["track"] == {
        "title": "曲名",
        "artist": "アーティスト",
        "album": "アルバム",
        "genre_tag": "Jazz",
        "release_year": "1999",
    }
    # 数値ではなく言葉の区分で送る
    assert body["state"]["audio_descriptors"]["tempo"] == "slow (70-95 BPM)"
    serialized = json.dumps(body, ensure_ascii=False)
    assert "秘密のフォルダ" not in serialized
    assert ".flac" not in serialized


def test_title_is_not_sent_when_disabled(make_track: TrackFactory) -> None:
    state = build_state(make_track(title="送らない曲名"), None, JevSettings(send_titles=False))

    track = state["track"]
    assert isinstance(track, dict)
    assert "title" not in track
    assert state["audio_descriptors"] == "not available"


def test_low_confidence_answers_are_held(make_track: TrackFactory) -> None:
    body = jev_response(genre_confidence=0.3, scores={"energy": score_answer([0.5, 0.5, 0, 0, 0], confidence=0.2)})
    with client_for(Recorder([ok(body)])) as client:
        result = JevClassifier(client=client, settings=JevSettings(min_confidence=0.5)).classify(make_track(), None)

    assert result.genre is None
    assert result.genre_confidence == 0.3
    assert result.attributes[Attribute.ENERGY].is_held
    assert result.attributes[Attribute.ENERGY].confidence == 0.2
    assert result.attributes[Attribute.RELAXATION].value == 1.0
    assert len(result.notes) == 2


def test_genre_is_unknown_when_information_is_insufficient(make_track: TrackFactory) -> None:
    with client_for(Recorder([ok(jev_response(genre="other", determinable=0.2))])) as client:
        result = JevClassifier(client=client).classify(make_track(), None)

    assert result.genre is None
    assert "判断材料が不足" in result.notes[0]


def test_other_genre_is_kept_when_determinable(make_track: TrackFactory) -> None:
    with client_for(Recorder([ok(jev_response(genre="other"))])) as client:
        result = JevClassifier(client=client).classify(make_track(), None)

    assert result.genre == Genre.OTHER


def test_build_questions_defines_all_attributes() -> None:
    questions = build_questions()

    assert set(questions) == {"genre", "genre_determinable", *ATTRIBUTE_KEYS}


def test_create_client_without_api_key_is_unavailable() -> None:
    with pytest.raises(JevUnavailableError):
        create_client(JevSettings())


def test_create_client_with_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TYPESAFE_API_KEY", "dummy-key-for-test")

    with create_client(JevSettings()) as client:
        assert isinstance(client, TypeSafeClient)


def _register_tracks(db: sqlite3.Connection, count: int) -> list[tuple[int, str]]:
    ids = []
    for i in range(count):
        meta = TrackMetadata(
            rel_path=f"曲{i}.flac", title=f"曲{i}", artist="A", album=None, genre="Jazz", year=None,
            track_number=None, duration_sec=200.0, bitrate=None, sample_rate=None, channels=None, codec="flac",
            file_size=1, mtime_ns=0, title_source=TitleSource.TAG, tags_repaired=False, quick_hash=None,
        )  # fmt: skip
        ids.append((upsert_track(db, "/music", meta, None), f"曲{i}.flac"))

    return ids


def _run(db: sqlite3.Connection, recorder: Recorder, count: int, limits: RunLimits) -> ClassificationRunSummary:
    tracks = [t for t in (get_track(db, i) for i, _ in _register_tracks(db, count)) if t is not None]
    with client_for(recorder) as client:
        return classify_tracks(db, JevClassifier(client=client), [(t, None) for t in tracks], limits)


def test_run_saves_results_and_respects_track_limit(db: sqlite3.Connection) -> None:
    recorder = Recorder([ok()])

    summary = _run(db, recorder, 5, RunLimits(max_tracks=3))

    assert len(recorder.requests) == 3
    assert summary.classified == 3
    assert summary.remaining == 2
    assert len(load_classifications(db, "jev")) == 3


@pytest.mark.parametrize(
    ("code", "reason"),
    [(401, "API キー"), (403, "API キー"), (429, "レート制限")],
)
def test_run_stops_on_errors_that_will_not_recover(db: sqlite3.Connection, code: int, reason: str) -> None:
    recorder = Recorder([status(code)])

    summary = _run(db, recorder, 4, RunLimits(max_tracks=4))

    assert len(recorder.requests) == 1
    assert summary.failed == 1
    assert reason in (summary.stopped_reason or "")
    errors = list_errors(db, "/music")
    assert [e.message for e in errors] == [f"{errors[0].error_type}: status={code}"]


def test_run_continues_after_transient_error_and_stops_after_consecutive_failures(db: sqlite3.Connection) -> None:
    recorder = Recorder([status(500), ok(), timeout(), timeout(), timeout(), ok()])

    summary = _run(db, recorder, 8, RunLimits(max_tracks=8, max_consecutive_failures=3))

    assert len(recorder.requests) == 5
    assert summary.classified == 1
    assert summary.failed == 4
    assert "3 回連続" in (summary.stopped_reason or "")
    assert [e.error_type for e in reversed(list_errors(db, "/music"))] == [
        "TypeSafeInternalServerError",
        "TypeSafeAPITimeoutError",
        "TypeSafeAPITimeoutError",
        "TypeSafeAPITimeoutError",
    ]


@pytest.mark.parametrize(
    ("body", "error_type"),
    [
        # 形式は正しいが、必要な回答がない（アプリ側で検出する）
        ({"model": "jev", "answers": {}, "usage": {}}, "JevResponseError"),
        # 必須の項目がない（SDK が検出する）
        ({"model": "jev", "answers": {}}, "TypeSafeAPIResponseValidationError"),
    ],
)
def test_invalid_response_is_recorded_as_error(
    db: sqlite3.Connection, body: dict[str, object], error_type: str
) -> None:
    recorder = Recorder([ok(body)])

    summary = _run(db, recorder, 1, RunLimits(max_tracks=1))

    assert summary.failed == 1
    assert load_classifications(db, "jev") == {}
    assert list_errors(db, "/music")[0].error_type == error_type


def test_missing_answer_raises_validation_error(make_track: TrackFactory) -> None:
    body = jev_response()
    answers = body["answers"]
    assert isinstance(answers, dict)
    del answers["energy"]
    with client_for(Recorder([ok(body)])) as client, pytest.raises(JevResponseError):
        JevClassifier(client=client).classify(make_track(), None)


def test_estimate_and_run_jev_workflow(db: sqlite3.Connection) -> None:
    _register_tracks(db, 4)

    estimate = estimate_jev(db, "/music")
    message = run_jev(db, "/music", JobControl(), httpx2.MockTransport(Recorder([ok()])))

    assert (estimate.pending_tracks, estimate.tracks_this_run, estimate.requests_this_run) == (4, 4, 4)
    assert estimate.questions_per_request == len(build_questions())
    assert "判定 4 曲" in message
    assert estimate_jev(db, "/music").pending_tracks == 0
