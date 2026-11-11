"""自由記述の雰囲気。説明文から、Jev が雰囲気マップ上の位置と属性の値を決める。

説明文（例:「雨の日曜の午後、本を読みながら」）を state として Jev に 1 回だけ問い合わせ、曲の分類と同じ
7 つの属性の値を決める。マップ上の位置は明るさ × エネルギー感。円で選ぶときは 2 軸だけを使い、残りの属性は
参考として保存する。プレイリストの生成時には Jev を呼ばない。

7 つの属性では、年代・ボーカル・具体的な楽器などの要素を表せない。表せない要素を含むかを Noul で確認し、
画面で知らせる（Choice・Score は必ず値を返すため。Companion Noul パターン）。
"""

from dataclasses import dataclass

from typesafe_sdk import ContentValue, Noul, Score, SystemOneResponse, TypeSafeClient

from volumio_playlist_studio.classify.jev import ATTRIBUTE_QUESTIONS, JevResponseError
from volumio_playlist_studio.config import JevSettings
from volumio_playlist_studio.models import Attribute, AttributeScore
from volumio_playlist_studio.mood_map import X_ATTRIBUTE, Y_ATTRIBUTE, MapPoint

EXPRESSIBLE_QUESTION = "expressible"
# 名前はマップに書くため短くする。説明文は Jev に送るため、長すぎる入力を受け付けない
MAX_NAME_LENGTH = 20
MAX_DESCRIPTION_LENGTH = 200

# 属性ごとの質問文。基準（5 段階）は曲の分類と同じものを使い、同じ尺度で曲と比べられるようにする
_MOOD_INSTRUCTIONS: dict[Attribute, str] = {
    Attribute.RELAXATION: "How relaxing music that fits the described mood or scene should be",
    Attribute.ENERGY: "How energetic music that fits the described mood or scene should be",
    Attribute.BRIGHTNESS: (
        "How bright and cheerful, as opposed to dark and melancholic, "
        "music that fits the described mood or scene should be"
    ),
    Attribute.CALMNESS: (
        "How calm and gentle, with few sudden or harsh changes, music that fits the described mood or scene should be"
    ),
    Attribute.WORK_BGM: (
        "How much the described mood or scene calls for background music suitable for concentrating on work or study: "
        "unobtrusive, steady and without sudden changes"
    ),
    Attribute.PARTY: "How much the described mood or scene calls for music suitable for a party or a workout",
    Attribute.NIGHT_BGM: (
        "How much the described mood or scene calls for calm background music for a quiet night at home"
    ),
}

_STATE_NOTE = (
    "The description was written by a listener who wants a playlist for this mood or scene. "
    "It may be written in Japanese. Judge the music that would fit it, not the scene itself."
)


@dataclass(frozen=True)
class MoodEstimate:
    """Jev が説明文から決めた、雰囲気の位置と属性の値。

    Attributes:
        point: マップ上の位置（明るさ × エネルギー感）。
        attributes: 7 つの属性の値。confidence が低い属性は判定保留（値が None）。
        expressible: 説明文の要素を 7 つの属性だけで表せる確率（0〜1）。
        model: 応答したモデル名。
        input_tokens: 入力トークン数。
        output_tokens: 出力トークン数。
    """

    point: MapPoint
    attributes: dict[Attribute, AttributeScore]
    expressible: float
    model: str | None
    input_tokens: int
    output_tokens: int


class PositionUndeterminedError(Exception):
    """Jev の回答の confidence が低く、明るさかエネルギー感（マップ上の位置）を決められなかった場合の例外。"""


def validate_input(name: str, description: str) -> tuple[str, str]:
    """名前と説明文の前後の空白を除き、検証して返す。

    Raises:
        ValueError: 空、または長すぎる場合。
    """
    name = name.strip()
    description = description.strip()
    if not name or not description:
        raise ValueError("name and description must not be empty")

    if len(name) > MAX_NAME_LENGTH or len(description) > MAX_DESCRIPTION_LENGTH:
        raise ValueError("name or description is too long")

    return name, description


def build_mood_questions() -> dict[str, Score | Noul]:
    """自由記述の雰囲気の質問を作る。質問を変えた場合は JEV_MOOD_QUESTION_VERSION を更新する。"""
    questions: dict[str, Score | Noul] = {
        EXPRESSIBLE_QUESTION: Noul(
            instructions=(
                "Everything the description asks for can be expressed only by how relaxing, energetic, bright, "
                "calm, suitable as work background music, suitable for a party and suitable for a quiet night "
                "the music is. It does not ask for anything else, such as a release era, a genre, vocals or lyrics, "
                "a language, specific artists or specific instruments"
            ),
        ),
    }
    for attribute, instructions in _MOOD_INSTRUCTIONS.items():
        _, criteria = ATTRIBUTE_QUESTIONS[attribute]
        questions[attribute.value] = Score(instructions=instructions, criteria=list(criteria))

    return questions


def build_mood_state(description: str) -> dict[str, ContentValue]:
    """Jev に送る state を作る。送るのは説明文だけで、名前やライブラリの情報は含めない。"""
    return {"mood_description": description, "note": _STATE_NOTE}


def parse_mood_response(response: SystemOneResponse, settings: JevSettings) -> MoodEstimate:
    """Jev の応答を、雰囲気の位置と属性の値に変換する。

    Raises:
        JevResponseError: 必要な回答がない、または想定外の値が返された場合。
        PositionUndeterminedError: 明るさかエネルギー感の confidence が `min_confidence` 未満の場合。
    """
    try:
        attributes = _parse_attributes(response, settings)
        expressible = float(response.nouls[EXPRESSIBLE_QUESTION].noul)
    except (KeyError, ValueError) as exc:
        # 応答の内容はログに残さず、どの回答が不正だったかだけを伝える
        raise JevResponseError(f"missing or invalid answer: {exc.__class__.__name__}") from exc

    x = attributes[X_ATTRIBUTE].value
    y = attributes[Y_ATTRIBUTE].value
    if x is None or y is None:
        raise PositionUndeterminedError("brightness or energy was held due to low confidence")

    return MoodEstimate(
        point=MapPoint(x, y),
        attributes=attributes,
        expressible=expressible,
        model=response.model,
        input_tokens=response.usage.input_tokens or 0,
        output_tokens=response.usage.output_tokens or 0,
    )


def _parse_attributes(response: SystemOneResponse, settings: JevSettings) -> dict[Attribute, AttributeScore]:
    attributes: dict[Attribute, AttributeScore] = {}
    for attribute in _MOOD_INSTRUCTIONS:
        _, criteria = ATTRIBUTE_QUESTIONS[attribute]
        answer = response.scores[attribute.value]
        held = answer.confidence < settings.min_confidence
        value = round(answer.score / (len(criteria) - 1), 4)
        if not 0.0 <= value <= 1.0:
            raise ValueError(f"score out of range: {attribute.value}")

        attributes[attribute] = AttributeScore(
            value=None if held else value,
            confidence=answer.confidence,
            probabilities={str(level): p for level, p in answer.probabilities.items()},
        )

    return attributes


def estimate_mood(client: TypeSafeClient, description: str, settings: JevSettings) -> MoodEstimate:
    """説明文を Jev に送り、雰囲気の位置と属性の値を決める。リクエストは 1 回。

    Raises:
        typesafe_sdk.TypeSafeError: 通信・認証・レート制限・応答形式のエラー。
        JevResponseError: 必要な回答がない、または想定外の値が返された場合。
        PositionUndeterminedError: 明るさかエネルギー感を決められなかった場合。
    """
    response = client.system_one(
        state=build_mood_state(description),
        questions=build_mood_questions(),
        model=settings.model,
    )
    return parse_mood_response(response, settings)
