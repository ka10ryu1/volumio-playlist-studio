"""自由記述の雰囲気のテスト。Jev とは通信せず、HTTP の送受信だけを httpx2.MockTransport で差し替える。"""

import json
import logging
import sqlite3
from pathlib import Path

import httpx2
import pytest
from test_jev import NO_RETRY, Handler, Recorder, ok, score_answer, status

from volumio_playlist_studio.classify.jev import (
    ATTRIBUTE_QUESTIONS,
    JevResponseError,
    JevUnavailableError,
    create_client,
)
from volumio_playlist_studio.config import JEV_MOOD_QUESTION_VERSION, JevSettings
from volumio_playlist_studio.custom_mood import (
    MAX_DESCRIPTION_LENGTH,
    MAX_NAME_LENGTH,
    MoodEstimate,
    PositionUndeterminedError,
    build_mood_questions,
    estimate_mood,
    validate_input,
)
from volumio_playlist_studio.jobs import BackgroundJob, JobControl, JobError, JobState
from volumio_playlist_studio.models import Attribute, AttributeScore
from volumio_playlist_studio.mood_map import MapPoint
from volumio_playlist_studio.storage.custom_moods import (
    CustomMoodDraft,
    DuplicateNameError,
    delete_custom_mood,
    list_custom_moods,
    save_custom_mood,
)
from volumio_playlist_studio.storage.database import connect, transaction
from volumio_playlist_studio.workflows import UNEXPRESSIBLE_NOTE, run_custom_mood

ATTRIBUTE_KEYS = {a.value for a in Attribute}
DESCRIPTION = "雨の日曜の午後、本を読みながら"


def mood_response(scores: dict[str, dict[str, object]] | None = None, expressible: float = 0.9) -> dict[str, object]:
    """自由記述の雰囲気への応答。既定では明るさ 0.25・エネルギー感 0.0 の位置になる。"""
    middle = score_answer([0.0, 0.0, 1.0, 0.0, 0.0])
    answers: dict[str, object] = {
        "expressible": {"type": "noul", "noul": expressible},
        "relaxation": score_answer([0.0, 0.0, 0.0, 0.0, 1.0]),
        "energy": score_answer([1.0, 0.0, 0.0, 0.0, 0.0]),
        "brightness": score_answer([0.0, 1.0, 0.0, 0.0, 0.0]),
        "calmness": middle,
        "work_bgm": middle,
        "party": middle,
        "night_bgm": middle,
    }
    answers.update(scores or {})
    return {"model": "jev-1.13.0", "answers": answers, "usage": {"input_tokens": 300, "output_tokens": 50}}


def transport(recorder: Recorder) -> httpx2.MockTransport:
    return httpx2.MockTransport(recorder)


def estimate(recorder: Recorder, settings: JevSettings | None = None) -> MoodEstimate:
    settings = settings or JevSettings()
    with create_client(settings, transport=transport(recorder), retry=NO_RETRY) as client:
        return estimate_mood(client, DESCRIPTION, settings)


def test_questions_cover_all_attributes_with_same_criteria_as_tracks() -> None:
    questions = build_mood_questions()

    assert set(questions) == {"expressible", *ATTRIBUTE_KEYS}
    # 曲と同じ尺度で比べられるよう、基準（5 段階）は曲の質問と同じ
    for attribute, (_, criteria) in ATTRIBUTE_QUESTIONS.items():
        assert questions[attribute.value].criteria == criteria


def test_request_sends_only_description() -> None:
    recorder = Recorder([ok(mood_response())])

    estimate(recorder)

    [request] = recorder.requests
    body = request["body"]
    assert isinstance(body, dict)
    assert request["url"] == "https://api.typesafe.ai/v1/systemone"
    assert body["state"]["mood_description"] == DESCRIPTION
    assert set(body["state"]) == {"mood_description", "note"}
    assert body["questions"]["expressible"]["type"] == "noul"
    assert all(body["questions"][k]["type"] == "score" for k in ATTRIBUTE_KEYS)


def test_response_gives_position_and_attribute_values() -> None:
    result = estimate(Recorder([ok(mood_response())]))

    # 5 段階（0〜4）の回答を 0〜1 に換算し、明るさ × エネルギー感を位置にする
    assert result.point == MapPoint(0.25, 0.0)
    assert result.attributes[Attribute.RELAXATION].value == 1.0
    assert result.attributes[Attribute.WORK_BGM].value == 0.5
    assert result.attributes[Attribute.ENERGY].confidence == 0.9
    assert result.expressible == 0.9
    assert result.model == "jev-1.13.0"
    assert (result.input_tokens, result.output_tokens) == (300, 50)


def test_low_confidence_other_attribute_is_held_but_position_is_decided() -> None:
    body = mood_response({"work_bgm": score_answer([0.0, 0.0, 1.0, 0.0, 0.0], confidence=0.2)})

    result = estimate(Recorder([ok(body)]), JevSettings(min_confidence=0.5))

    assert result.attributes[Attribute.WORK_BGM].is_held
    assert result.attributes[Attribute.WORK_BGM].confidence == 0.2
    assert result.point == MapPoint(0.25, 0.0)


@pytest.mark.parametrize("axis", ["brightness", "energy"])
def test_low_confidence_axis_leaves_position_undetermined(axis: str) -> None:
    body = mood_response({axis: score_answer([0.0, 0.0, 1.0, 0.0, 0.0], confidence=0.49)})

    with pytest.raises(PositionUndeterminedError):
        estimate(Recorder([ok(body)]), JevSettings(min_confidence=0.5))


def test_missing_answer_is_rejected() -> None:
    body = mood_response()
    answers = body["answers"]
    assert isinstance(answers, dict)
    del answers["expressible"]

    with pytest.raises(JevResponseError):
        estimate(Recorder([ok(body)]))


@pytest.mark.parametrize(
    ("name", "description", "expected"),
    [
        ("  雨の午後 ", f" {DESCRIPTION}\n", ("雨の午後", DESCRIPTION)),
        (
            "名" * MAX_NAME_LENGTH,
            "説" * MAX_DESCRIPTION_LENGTH,
            ("名" * MAX_NAME_LENGTH, "説" * MAX_DESCRIPTION_LENGTH),
        ),
    ],
)
def test_input_is_trimmed_and_accepted_up_to_limits(name: str, description: str, expected: tuple[str, str]) -> None:
    assert validate_input(name, description) == expected


@pytest.mark.parametrize(
    ("name", "description", "message"),
    [
        ("", DESCRIPTION, "empty"),
        ("雨の午後", "   ", "empty"),
        ("名" * (MAX_NAME_LENGTH + 1), DESCRIPTION, "too long"),
        ("雨の午後", "説" * (MAX_DESCRIPTION_LENGTH + 1), "too long"),
    ],
)
def test_invalid_input_is_rejected(name: str, description: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        validate_input(name, description)


def draft(name: str = "雨の午後", x: float = 0.25, y: float = 0.0) -> CustomMoodDraft:
    return CustomMoodDraft(
        name=name,
        description=DESCRIPTION,
        x=x,
        y=y,
        attributes={Attribute.BRIGHTNESS: AttributeScore(x, 0.9, {"1": 1.0}), Attribute.WORK_BGM: AttributeScore(None)},
        expressible=0.4,
        question_version="jev-mood-test",
        model="jev-test",
    )


def test_custom_mood_round_trip(db: sqlite3.Connection) -> None:
    with transaction(db):
        first = save_custom_mood(db, draft("雨の午後"))
        second = save_custom_mood(db, draft("夜のドライブ", x=0.7, y=0.8))

    moods = list_custom_moods(db)

    assert [(m.id, m.name) for m in moods] == [(first, "雨の午後"), (second, "夜のドライブ")]
    mood = moods[0]
    assert mood.description == DESCRIPTION
    assert (mood.x, mood.y) == (0.25, 0.0)
    assert mood.attributes[Attribute.BRIGHTNESS] == AttributeScore(0.25, 0.9, {"1": 1.0})
    assert mood.attributes[Attribute.WORK_BGM].is_held
    assert mood.expressible == 0.4
    assert (mood.question_version, mood.model) == ("jev-mood-test", "jev-test")
    assert mood.created_at


def test_duplicate_name_is_rejected_without_changing_existing(db: sqlite3.Connection) -> None:
    with transaction(db):
        save_custom_mood(db, draft("雨の午後"))

    with pytest.raises(DuplicateNameError), transaction(db):
        save_custom_mood(db, draft("雨の午後", x=0.9))

    [mood] = list_custom_moods(db)
    assert mood.x == 0.25


def test_position_outside_map_is_rejected_by_database(db: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError), transaction(db):
        save_custom_mood(db, draft(x=1.5))

    assert list_custom_moods(db) == []


def test_delete_custom_mood(db: sqlite3.Connection) -> None:
    with transaction(db):
        keep = save_custom_mood(db, draft("残す"))
        removed = save_custom_mood(db, draft("消す"))

    with transaction(db):
        delete_custom_mood(db, removed)

    assert [m.id for m in list_custom_moods(db)] == [keep]
    with pytest.raises(ValueError, match="not found"):
        delete_custom_mood(db, removed)


def test_broken_saved_data_is_reported(db: sqlite3.Connection) -> None:
    with transaction(db):
        mood_id = save_custom_mood(db, draft())
        db.execute("UPDATE custom_moods SET result_json = ? WHERE id = ?", (json.dumps({"attributes": {}}), mood_id))

    with pytest.raises(ValueError, match="invalid custom mood"):
        list_custom_moods(db)


def test_run_custom_mood_saves_position_and_versions(db: sqlite3.Connection) -> None:
    recorder = Recorder([ok(mood_response())])

    message = run_custom_mood(db, " 雨の午後 ", DESCRIPTION, transport=transport(recorder))

    [mood] = list_custom_moods(db)
    assert (mood.name, mood.description) == ("雨の午後", DESCRIPTION)
    assert (mood.x, mood.y) == (0.25, 0.0)
    assert mood.question_version == JEV_MOOD_QUESTION_VERSION
    assert mood.model == "jev-1.13.0"
    assert len(mood.attributes) == len(Attribute)
    assert "「雨の午後」をマップに追加しました" in message
    assert "入力 300 / 出力 50 トークン" in message
    assert UNEXPRESSIBLE_NOTE not in message
    # 名前は Jev に送らない
    assert "雨の午後" not in json.dumps(recorder.requests[0]["body"], ensure_ascii=False)


def test_run_custom_mood_warns_when_description_is_not_expressible(db: sqlite3.Connection) -> None:
    recorder = Recorder([ok(mood_response(expressible=0.3))])

    message = run_custom_mood(db, "90年代の女性ボーカル", "90 年代の女性ボーカルの曲", transport=transport(recorder))

    assert UNEXPRESSIBLE_NOTE in message
    [mood] = list_custom_moods(db)
    assert mood.expressible == 0.3


@pytest.mark.parametrize(("name", "description"), [("", DESCRIPTION), ("雨の午後", "")])
def test_run_custom_mood_rejects_invalid_input_without_request(
    db: sqlite3.Connection, name: str, description: str
) -> None:
    recorder = Recorder([ok(mood_response())])

    with pytest.raises(JobError, match="名前と説明文を入力"):
        run_custom_mood(db, name, description, transport=transport(recorder))

    assert recorder.requests == []


def test_run_custom_mood_rejects_duplicate_name_without_request(db: sqlite3.Connection) -> None:
    with transaction(db):
        save_custom_mood(db, draft("雨の午後"))

    recorder = Recorder([ok(mood_response())])

    with pytest.raises(JobError, match="既にあります"):
        run_custom_mood(db, "雨の午後", "別の説明", transport=transport(recorder))

    assert recorder.requests == []
    assert len(list_custom_moods(db)) == 1


@pytest.mark.parametrize(
    ("handler", "message"),
    [
        (status(401), "API キーが無効"),
        (ok(mood_response({"energy": score_answer([0.0, 0.0, 1.0, 0.0, 0.0], confidence=0.1)})), "決められません"),
        (ok({"model": "jev-1.13.0", "answers": {}, "usage": {}}), "応答の形式"),
    ],
)
def test_run_custom_mood_failure_saves_nothing(db: sqlite3.Connection, handler: Handler, message: str) -> None:
    recorder = Recorder([handler])

    with pytest.raises(JobError, match=message):
        run_custom_mood(db, "雨の午後", DESCRIPTION, transport=transport(recorder))

    assert list_custom_moods(db) == []


def test_run_custom_mood_fails_with_connection_error(db: sqlite3.Connection) -> None:
    def fail(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("simulated", request=request)

    with pytest.raises(JobError, match="通信に失敗"):
        run_custom_mood(db, "雨の午後", DESCRIPTION, transport=transport(Recorder([fail])))

    assert list_custom_moods(db) == []


def test_run_custom_mood_requires_api_key(db: sqlite3.Connection) -> None:
    # conftest で TYPESAFE_API_KEY を空にしている
    with pytest.raises(JevUnavailableError):
        run_custom_mood(db, "雨の午後", DESCRIPTION)


def test_failed_request_does_not_log_description(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    # エラー応答の本文に説明文が含まれていても、ジョブの境界までログに出さない
    path = tmp_path / "studio.sqlite3"
    connect(path).close()
    leaky = Recorder([lambda _: httpx2.Response(400, json={"error": f"invalid state: {DESCRIPTION}"})])

    def work(_control: JobControl) -> object:
        conn = connect(path)
        try:
            return run_custom_mood(conn, "雨の午後", DESCRIPTION, transport=transport(leaky))
        finally:
            conn.close()

    job = BackgroundJob(name="雰囲気の作成")
    with caplog.at_level(logging.INFO):
        job.start(work)
        job.join(timeout=30)

    assert job.state == JobState.FAILED
    assert "status=400" in caplog.text
    assert DESCRIPTION not in caplog.text
