"""TypeSafe Jev API による楽曲分類。

API 仕様は https://docs.typesafe.ai/ を参照。1 曲につき 1 リクエストで、全質問をまとめて送る。
送信するのは正規化したタグ情報と、音響特徴量を言葉の区分に変換したものだけで、
音声ファイルやファイルパスは送らない。

Jev の confidence は分類の正解率ではない。confidence が低い回答は判定保留として扱い、
値の妥当性は精度評価の画面で人手評価と比較して確認する。
"""

from dataclasses import dataclass, field

import httpx2
from typesafe_sdk import Choice, ContentValue, Noul, RetryPolicy, Score, SystemOneResponse, TypeSafeClient
from typesafe_sdk.constants import DEFAULT_BASE_URL

from volumio_playlist_studio.classify.base import decide_mood
from volumio_playlist_studio.classify.descriptors import describe
from volumio_playlist_studio.config import JEV_QUESTION_VERSION, JevSettings, MoodDecision, is_typesafe_api_key_set
from volumio_playlist_studio.models import (
    Attribute,
    AttributeScore,
    AudioFeatures,
    ClassificationResult,
    Genre,
    Track,
)

ENGINE_NAME = "jev"

GENRE_QUESTION = "genre"
GENRE_DETERMINABLE_QUESTION = "genre_determinable"

# Choice の各候補の説明。Jev は書かれた文言どおりに判断するため、境界が分かる説明を付ける
_GENRE_CRITERIA: dict[str, str] = {
    Genre.ROCK: "Rock, including punk, indie rock and alternative rock",
    Genre.POP: "Pop music, including J-pop, K-pop and idol pop",
    Genre.JAZZ: "Jazz, including swing, bossa nova and jazz fusion",
    Genre.CLASSICAL: "Western classical music, including orchestral, chamber, opera and solo classical works",
    Genre.ELECTRONIC: "Electronic dance or electronic music, such as techno, house, trance, EDM and synth-based music",
    Genre.HIPHOP: "Hip hop and rap",
    Genre.RNB_SOUL: "R&B, soul, funk and gospel",
    Genre.FOLK_ACOUSTIC: "Folk, country, singer-songwriter and acoustic music",
    Genre.METAL: "Heavy metal and its subgenres",
    Genre.AMBIENT: "Ambient, new age and healing music",
    Genre.SOUNDTRACK: "Film, TV, anime or video game soundtracks and scores",
    Genre.WORLD: "World music, such as Latin, reggae, Celtic, African or traditional folk music of a region",
    Genre.OTHER: "A genre that is not covered by any of the other options",
}

# 属性ごとの質問文と 5 段階の基準。Score の回答（0〜4）を 0〜1 に変換して使う。
# 基準は自由記述の雰囲気の質問（custom_mood.py）でも使うため、変えた場合は JEV_MOOD_QUESTION_VERSION も更新する
ATTRIBUTE_QUESTIONS: dict[Attribute, tuple[str, list[str]]] = {
    Attribute.RELAXATION: (
        "How relaxing this track is likely to feel to a listener",
        [
            "Not relaxing: tense, aggressive or very busy",
            "Slightly relaxing",
            "Moderately relaxing",
            "Quite relaxing",
            "Very relaxing: soothing, gentle and unhurried",
        ],
    ),
    Attribute.ENERGY: (
        "How energetic this track feels",
        [
            "Very low energy: still and quiet",
            "Low energy",
            "Moderate energy",
            "High energy",
            "Very high energy: intense, driving and powerful",
        ],
    ),
    Attribute.BRIGHTNESS: (
        "How bright and cheerful, as opposed to dark and melancholic, the mood of this track is",
        [
            "Dark, sad or melancholic",
            "Somewhat dark",
            "Neutral",
            "Somewhat bright",
            "Bright, happy and cheerful",
        ],
    ),
    Attribute.CALMNESS: (
        "How calm and gentle this track is, with few sudden or harsh changes",
        [
            "Not calm: harsh, agitated or full of sudden changes",
            "Slightly calm",
            "Moderately calm",
            "Quite calm",
            "Very calm and gentle throughout",
        ],
    ),
    Attribute.WORK_BGM: (
        "How suitable this track is as background music while concentrating on work or study: "
        "unobtrusive, steady and without sudden changes",
        [
            "Unsuitable: distracting or attention-grabbing",
            "Mostly unsuitable",
            "Acceptable",
            "Suitable",
            "Very suitable: unobtrusive and steady",
        ],
    ),
    Attribute.PARTY: (
        "How suitable this track is for a party or a workout",
        [
            "Unsuitable: too slow or quiet",
            "Mostly unsuitable",
            "Acceptable",
            "Suitable",
            "Very suitable: upbeat and driving",
        ],
    ),
    Attribute.NIGHT_BGM: (
        "How suitable this track is as calm background music for a quiet night at home",
        [
            "Unsuitable: loud, intense or agitating",
            "Mostly unsuitable",
            "Acceptable",
            "Suitable",
            "Very suitable: quiet, soft and soothing",
        ],
    ),
}

_STATE_NOTE = (
    "The audio descriptors were computed automatically from short excerpts and may be inaccurate. "
    "Estimated tempo may be half or double the true tempo. Lyrics and vocals were not analyzed."
)


class JevUnavailableError(Exception):
    """Jev を利用できない場合の例外（API キー未設定など）。"""


class JevResponseError(Exception):
    """Jev の応答に、必要な回答がない、または想定外の値がある場合の例外。"""


def build_questions() -> dict[str, Choice | Score | Noul]:
    """1 曲分の質問を作る。質問を変えた場合は JEV_QUESTION_VERSION を更新する。"""
    questions: dict[str, Choice | Score | Noul] = {
        GENRE_QUESTION: Choice(
            instructions="Which music genre best describes this track",
            criteria=dict(_GENRE_CRITERIA),
        ),
        # Choice は必ずどれかを選ぶため、判断材料が足りるかを別の Noul で確認する（Companion Noul パターン）
        GENRE_DETERMINABLE_QUESTION: Noul(
            instructions=(
                "The provided information (artist, album, title, genre tag and audio descriptors) "
                "is enough to identify the music genre of this track"
            ),
        ),
    }
    for attribute, (instructions, criteria) in ATTRIBUTE_QUESTIONS.items():
        questions[attribute.value] = Score(instructions=instructions, criteria=list(criteria))

    return questions


def build_state(track: Track, features: AudioFeatures | None, settings: JevSettings) -> dict[str, ContentValue]:
    """Jev に送る state を作る。ファイルパスなど、判定に不要な情報は含めない。"""
    tags = {
        "title": track.title if settings.send_titles else None,
        "artist": track.artist,
        "album": track.album,
        "genre_tag": track.genre,
        "release_year": None if track.year is None else str(track.year),
    }
    return {
        "track": {key: value for key, value in tags.items() if value},
        "audio_descriptors": "not available" if features is None else describe(features),
        "note": _STATE_NOTE,
    }


def parse_response(
    response: SystemOneResponse, settings: JevSettings, mood_decision: MoodDecision
) -> ClassificationResult:
    """Jev の応答を分類結果に変換する。

    confidence が `min_confidence` 未満の回答は判定保留にする。ジャンルは、判断材料が足りるかの Noul が
    閾値未満の場合も不明とする。保留にした場合も confidence と確率は保存し、評価で確認できるようにする。

    Raises:
        JevResponseError: 必要な回答がない、または候補にない値が返された場合。
    """
    try:
        return _parse(response, settings, mood_decision)
    except (KeyError, ValueError) as exc:
        # 応答の内容はログに残さず、どの回答が不正だったかだけを伝える
        raise JevResponseError(f"missing or invalid answer: {exc.__class__.__name__}") from exc


def _parse(response: SystemOneResponse, settings: JevSettings, mood_decision: MoodDecision) -> ClassificationResult:
    notes: list[str] = []
    genre_answer = response.choices[GENRE_QUESTION]
    determinable = response.nouls[GENRE_DETERMINABLE_QUESTION].noul
    genre: Genre | None = Genre(genre_answer.choice)
    if determinable < settings.genre_determinable_threshold:
        genre = None
        notes.append(f"判断材料が不足しているためジャンルは不明（確率 {determinable:.2f}）")
    elif genre_answer.confidence < settings.min_confidence:
        genre = None
        notes.append(f"ジャンルの confidence が低いため不明（{genre_answer.confidence:.2f}）")

    attributes: dict[Attribute, AttributeScore] = {}
    for attribute, (_, criteria) in ATTRIBUTE_QUESTIONS.items():
        answer = response.scores[attribute.value]
        held = answer.confidence < settings.min_confidence
        attributes[attribute] = AttributeScore(
            value=None if held else round(answer.score / (len(criteria) - 1), 4),
            confidence=answer.confidence,
            probabilities={str(level): p for level, p in answer.probabilities.items()},
        )
        if held:
            notes.append(f"{attribute.value} は confidence が低いため判定保留（{answer.confidence:.2f}）")

    return ClassificationResult(
        engine=ENGINE_NAME,
        engine_version=JEV_QUESTION_VERSION,
        model=response.model,
        genre=genre,
        genre_confidence=genre_answer.confidence,
        genre_probabilities=dict(genre_answer.probabilities),
        mood=decide_mood(attributes, mood_decision),
        attributes=attributes,
        notes=notes,
    )


def create_client(
    settings: JevSettings,
    *,
    transport: httpx2.BaseTransport | None = None,
    retry: RetryPolicy | None = None,
) -> TypeSafeClient:
    """TypeSafe のクライアントを作る。API キーは環境変数 TYPESAFE_API_KEY から SDK が読む。

    Args:
        settings: Jev の利用設定。
        transport: HTTP の送受信を差し替える場合に指定する（テスト用）。
        retry: リトライ方針。省略時は SDK の既定（429・529 などを指数バックオフで再試行）。

    Raises:
        JevUnavailableError: API キーが設定されていない場合。
    """
    if transport is None and not is_typesafe_api_key_set():
        raise JevUnavailableError("TYPESAFE_API_KEY is not set")

    if transport is not None:
        # テストでは環境変数に依存しないよう、ダミーのキーと既定の接続先を使う
        return TypeSafeClient(
            api_key="test-key",
            model=settings.model,
            timeout=settings.timeout_sec,
            transport=transport,
            retry=retry,
            base_url=DEFAULT_BASE_URL,
        )

    return TypeSafeClient(model=settings.model, timeout=settings.timeout_sec, retry=retry)


@dataclass
class JevClassifier:
    """Jev による分類エンジン。

    Attributes:
        input_tokens: このインスタンスで送信した入力トークン数の合計。
        output_tokens: このインスタンスで受け取った出力トークン数の合計。
    """

    client: TypeSafeClient
    settings: JevSettings = field(default_factory=JevSettings)
    mood_decision: MoodDecision = field(default_factory=MoodDecision)
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def engine(self) -> str:
        """エンジン名。"""
        return ENGINE_NAME

    @property
    def engine_version(self) -> str:
        """質問定義のバージョン。"""
        return JEV_QUESTION_VERSION

    def classify(self, track: Track, features: AudioFeatures | None) -> ClassificationResult:
        """Jev に問い合わせて楽曲を分類する。

        Raises:
            typesafe_sdk.TypeSafeError: 通信・認証・レート制限・応答形式のエラー。
            JevResponseError: 必要な回答がない、または候補にない値が返された場合。
        """
        response = self.client.system_one(
            state=build_state(track, features, self.settings),
            questions=build_questions(),
            model=self.settings.model,
        )
        self.input_tokens += response.usage.input_tokens or 0
        self.output_tokens += response.usage.output_tokens or 0
        return parse_response(response, self.settings, self.mood_decision)
