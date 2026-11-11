"""モジュール間で受け渡すデータ型を定義する。"""

import math
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any, Self


class TitleSource(StrEnum):
    """曲名をどこから得たか。"""

    TAG = "tag"
    FILENAME = "filename"


class Genre(StrEnum):
    """分類に使うジャンルの候補。タグのジャンル表記はこの候補に正規化してから比較する。"""

    ROCK = "rock"
    POP = "pop"
    JAZZ = "jazz"
    CLASSICAL = "classical"
    ELECTRONIC = "electronic"
    HIPHOP = "hiphop"
    RNB_SOUL = "rnb_soul"
    FOLK_ACOUSTIC = "folk_acoustic"
    METAL = "metal"
    AMBIENT = "ambient"
    SOUNDTRACK = "soundtrack"
    WORLD = "world"
    OTHER = "other"


GENRE_LABELS: dict[Genre, str] = {
    Genre.ROCK: "ロック",
    Genre.POP: "ポップ",
    Genre.JAZZ: "ジャズ",
    Genre.CLASSICAL: "クラシック",
    Genre.ELECTRONIC: "エレクトロニック",
    Genre.HIPHOP: "ヒップホップ",
    Genre.RNB_SOUL: "R&B・ソウル",
    Genre.FOLK_ACOUSTIC: "フォーク・アコースティック",
    Genre.METAL: "メタル",
    Genre.AMBIENT: "アンビエント",
    Genre.SOUNDTRACK: "サウンドトラック",
    Genre.WORLD: "ワールド",
    Genre.OTHER: "その他",
}


class Attribute(StrEnum):
    """0〜1 のスコアで表す楽曲の属性。"""

    RELAXATION = "relaxation"
    ENERGY = "energy"
    BRIGHTNESS = "brightness"
    CALMNESS = "calmness"
    WORK_BGM = "work_bgm"
    PARTY = "party"
    NIGHT_BGM = "night_bgm"


ATTRIBUTE_LABELS: dict[Attribute, str] = {
    Attribute.RELAXATION: "リラックス度",
    Attribute.ENERGY: "エネルギー感",
    Attribute.BRIGHTNESS: "明るさ",
    Attribute.CALMNESS: "穏やかさ",
    Attribute.WORK_BGM: "作業BGM適性",
    Attribute.PARTY: "パーティー・運動向け",
    Attribute.NIGHT_BGM: "夜のBGM適性",
}


class Mood(StrEnum):
    """プレイリストの雰囲気。

    分類結果のムード（1 曲に 1 つ決めるもの）に使うのは `classify.base.MOOD_COMPONENTS` にあるものだけ。
    それ以外はプレイリスト生成でのみ選べる。似た雰囲気どうしを曲ごとに判定すると差がつかず判定保留が増えるため、
    分類結果のムードは大まかな区分にとどめている。OMAKASE は雰囲気で絞り込まないことを表す。
    """

    RELAX = "relax"
    FOCUS = "focus"
    ENERGETIC = "energetic"
    CALM_NIGHT = "calm_night"
    MORNING = "morning"
    CAFE = "cafe"
    DRIVE = "drive"
    WORKOUT = "workout"
    MELLOW = "mellow"
    SLEEP = "sleep"
    OMAKASE = "omakase"


MOOD_LABELS: dict[Mood, str] = {
    Mood.RELAX: "リラックス",
    Mood.FOCUS: "集中・作業",
    Mood.ENERGETIC: "元気",
    Mood.CALM_NIGHT: "穏やかな夜",
    Mood.MORNING: "朝・爽やか",
    Mood.CAFE: "カフェ・ほっと一息",
    Mood.DRIVE: "ドライブ",
    Mood.WORKOUT: "ワークアウト",
    Mood.MELLOW: "しっとり・切ない",
    Mood.SLEEP: "入眠",
    Mood.OMAKASE: "おまかせ",
}


@dataclass(frozen=True)
class TrackMetadata:
    """mutagen とファイル情報から得た楽曲のメタデータ。不明な値は None とする。

    Attributes:
        rel_path: 音楽ライブラリのルートからの相対パス（`/` 区切り）。
        title_source: 曲名をタグから得たか、ファイル名から補完したか。
        tags_repaired: 文字化けしたタグを修復したか。
        quick_hash: 重複検出用に、ファイルサイズと先頭・末尾から求めたハッシュ。
    """

    rel_path: str
    title: str | None
    artist: str | None
    album: str | None
    genre: str | None
    year: int | None
    track_number: int | None
    duration_sec: float | None
    bitrate: int | None
    sample_rate: int | None
    channels: int | None
    codec: str | None
    file_size: int
    mtime_ns: int
    title_source: TitleSource | None
    tags_repaired: bool
    quick_hash: str | None


@dataclass(frozen=True)
class Track(TrackMetadata):
    """DB に登録済みの楽曲。"""

    id: int
    library_root: str
    status: str
    tag_error: str | None

    @property
    def display_title(self) -> str:
        """画面表示用の曲名。タグもファイル名もなければ相対パスを使う。"""
        return self.title or self.rel_path

    @property
    def display_artist(self) -> str:
        """画面表示用のアーティスト名。"""
        return self.artist or "（不明）"


@dataclass(frozen=True)
class AudioFeatures:
    """librosa で抽出した音響特徴量。各値の意味は docs/audio-features.md を参照。"""

    tempo_bpm: float
    tempo_variation: float
    rms_mean_db: float
    rms_std_db: float
    loudness_variation_db: float
    spectral_centroid_mean_hz: float
    spectral_centroid_std_hz: float
    spectral_rolloff_mean_hz: float
    zero_crossing_rate_mean: float
    onset_strength_mean: float
    onset_strength_std: float
    onset_rate_per_sec: float
    chroma_mean: tuple[float, ...]
    mfcc_mean: tuple[float, ...]
    mfcc_std: tuple[float, ...]
    analyzed_seconds: float
    segment_count: int

    def to_dict(self) -> dict[str, Any]:
        """JSON に保存できる辞書に変換する。"""
        data = asdict(self)
        for key in ("chroma_mean", "mfcc_mean", "mfcc_std"):
            data[key] = list(data[key])

        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """`to_dict` の出力から復元する。

        Raises:
            ValueError: 必要なキーがない、または値が有限の数値でない場合。
        """
        try:
            scalars = {key: float(data[key]) for key in _FEATURE_SCALAR_KEYS}
            vectors = {key: tuple(float(v) for v in data[key]) for key in ("chroma_mean", "mfcc_mean", "mfcc_std")}
            segment_count = int(data["segment_count"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("invalid audio features data") from exc

        if not all(math.isfinite(v) for v in scalars.values()):
            raise ValueError("audio features must be finite")

        return cls(
            tempo_bpm=scalars["tempo_bpm"],
            tempo_variation=scalars["tempo_variation"],
            rms_mean_db=scalars["rms_mean_db"],
            rms_std_db=scalars["rms_std_db"],
            loudness_variation_db=scalars["loudness_variation_db"],
            spectral_centroid_mean_hz=scalars["spectral_centroid_mean_hz"],
            spectral_centroid_std_hz=scalars["spectral_centroid_std_hz"],
            spectral_rolloff_mean_hz=scalars["spectral_rolloff_mean_hz"],
            zero_crossing_rate_mean=scalars["zero_crossing_rate_mean"],
            onset_strength_mean=scalars["onset_strength_mean"],
            onset_strength_std=scalars["onset_strength_std"],
            onset_rate_per_sec=scalars["onset_rate_per_sec"],
            chroma_mean=vectors["chroma_mean"],
            mfcc_mean=vectors["mfcc_mean"],
            mfcc_std=vectors["mfcc_std"],
            analyzed_seconds=scalars["analyzed_seconds"],
            segment_count=segment_count,
        )


_FEATURE_SCALAR_KEYS = (
    "tempo_bpm",
    "tempo_variation",
    "rms_mean_db",
    "rms_std_db",
    "loudness_variation_db",
    "spectral_centroid_mean_hz",
    "spectral_centroid_std_hz",
    "spectral_rolloff_mean_hz",
    "zero_crossing_rate_mean",
    "onset_strength_mean",
    "onset_strength_std",
    "onset_rate_per_sec",
    "analyzed_seconds",
)


@dataclass(frozen=True)
class AttributeScore:
    """1 つの属性の判定結果。

    Attributes:
        value: 0〜1 のスコア。判定保留なら None。
        confidence: 判定元が返した確信度（分類の正解率ではない）。なければ None。
        probabilities: 各段階の確率。なければ None。
    """

    value: float | None
    confidence: float | None = None
    probabilities: dict[str, float] | None = None

    @property
    def is_held(self) -> bool:
        """判定保留かどうか。"""
        return self.value is None


@dataclass(frozen=True)
class ClassificationResult:
    """分類エンジン 1 つによる楽曲 1 曲の判定結果。

    Attributes:
        engine: 分類エンジン名（`rules`、`jev` など）。
        engine_version: 分類ルール・質問定義のバージョン。
        model: 外部モデルを使った場合、実際に応答したモデル名。
        genre: 判定したジャンル。判断できなければ None（不明）。
        mood: 属性スコアから決めたムード。判定保留なら None。
        attributes: 属性ごとのスコア。
        notes: 判定時の補足（判定保留の理由など）。
    """

    engine: str
    engine_version: str
    model: str | None
    genre: Genre | None
    genre_confidence: float | None
    genre_probabilities: dict[str, float] | None
    mood: Mood | None
    attributes: dict[Attribute, AttributeScore]
    notes: list[str] = field(default_factory=list)

    def attribute_value(self, attribute: Attribute) -> float | None:
        """属性のスコアを返す。未判定・判定保留なら None。"""
        score = self.attributes.get(attribute)
        return None if score is None else score.value

    def to_dict(self) -> dict[str, Any]:
        """JSON に保存できる辞書に変換する。"""
        return {
            "engine": self.engine,
            "engine_version": self.engine_version,
            "model": self.model,
            "genre": None if self.genre is None else self.genre.value,
            "genre_confidence": self.genre_confidence,
            "genre_probabilities": self.genre_probabilities,
            "mood": None if self.mood is None else self.mood.value,
            "attributes": {attr.value: asdict(score) for attr, score in self.attributes.items()},
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """`to_dict` の出力から復元する。

        Raises:
            ValueError: 形式が不正な場合。
        """
        try:
            attributes = {
                Attribute(key): AttributeScore(
                    value=score["value"],
                    confidence=score.get("confidence"),
                    probabilities=score.get("probabilities"),
                )
                for key, score in data["attributes"].items()
            }
            return cls(
                engine=str(data["engine"]),
                engine_version=str(data["engine_version"]),
                model=data.get("model"),
                genre=None if data.get("genre") is None else Genre(data["genre"]),
                genre_confidence=data.get("genre_confidence"),
                genre_probabilities=data.get("genre_probabilities"),
                mood=None if data.get("mood") is None else Mood(data["mood"]),
                attributes=attributes,
                notes=list(data.get("notes", [])),
            )
        except (KeyError, TypeError, AttributeError) as exc:
            raise ValueError("invalid classification data") from exc


@dataclass(frozen=True)
class CustomMood:
    """保存済みの自由記述の雰囲気。

    Attributes:
        id: DB の ID。
        name: マップに書く名前。
        description: 利用者が書いた説明文。
        x: 雰囲気マップの横の位置（明るさ、0〜1）。Jev が説明文から決めた値。
        y: 雰囲気マップの縦の位置（エネルギー感、0〜1）。Jev が説明文から決めた値。
        attributes: 7 つの属性の値（参考として表示する。円で選ぶときは使わない）。
        expressible: 説明文の要素を 7 つの属性だけで表せる確率（0〜1）。
        question_version: 位置を決めたときの質問定義のバージョン。
        model: 応答したモデル名。
        created_at: 作成日時。
    """

    id: int
    name: str
    description: str
    x: float
    y: float
    attributes: dict[Attribute, AttributeScore]
    expressible: float
    question_version: str
    model: str | None
    created_at: str


class ClassificationSource(StrEnum):
    """プレイリスト生成に使う分類結果の種類。"""

    RULES = "rules"
    JEV = "jev"
    JEV_THEN_RULES = "jev_then_rules"


CLASSIFICATION_SOURCE_LABELS: dict[ClassificationSource, str] = {
    ClassificationSource.RULES: "ルールベースのみ（タグ＋音響特徴量）",
    ClassificationSource.JEV: "Jev のみ（未判定の曲は使わない）",
    ClassificationSource.JEV_THEN_RULES: "Jev を優先し、未判定の曲はルールベース",
}
