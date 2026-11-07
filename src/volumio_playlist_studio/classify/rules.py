"""タグ情報と音響特徴量による単純なルールベース分類（Baseline）。

外部 API を使わずに動作し、Jev による分類の改善効果を比較する基準にする。
単一の特徴量から属性を断定しないよう、各属性は複数の特徴量を 0〜1 に正規化した値の平均で求める。
明るさ（曲調の明暗）は調性や和声の判定が必要で、基本的な特徴量からは信頼できる値を出せないため判定保留とする。
"""

from dataclasses import dataclass, field

from volumio_playlist_studio.classify.base import decide_mood
from volumio_playlist_studio.classify.genre_tags import genre_from_tag
from volumio_playlist_studio.config import RULE_VERSION, MoodDecision, RuleParameters
from volumio_playlist_studio.models import (
    Attribute,
    AttributeScore,
    AudioFeatures,
    ClassificationResult,
    Track,
)

ENGINE_NAME = "rules"

# テンポの変動係数がこの値以上なら「テンポが安定しない」とみなす
_TEMPO_VARIATION_SCALE = 0.2
# 作業 BGM に適するエネルギー感の中心
_WORK_ENERGY_CENTER = 0.4


def normalize(value: float, bounds: tuple[float, float]) -> float:
    """値を範囲の下限で 0、上限で 1 になるよう線形に変換し、0〜1 に収める。"""
    low, high = bounds
    if high <= low:
        raise ValueError("upper bound must be greater than lower bound")
    return min(1.0, max(0.0, (value - low) / (high - low)))


def _mean(*values: float) -> float:
    return sum(values) / len(values)


@dataclass(frozen=True)
class RuleClassifier:
    """タグと音響特徴量によるルールベースの分類エンジン。"""

    params: RuleParameters = field(default_factory=RuleParameters)
    mood_decision: MoodDecision = field(default_factory=MoodDecision)

    @property
    def engine(self) -> str:
        """エンジン名。"""
        return ENGINE_NAME

    @property
    def engine_version(self) -> str:
        """ルールのバージョン。"""
        return RULE_VERSION

    def classify(self, track: Track, features: AudioFeatures | None) -> ClassificationResult:
        """タグからジャンルを、音響特徴量から属性スコアを求める。"""
        notes: list[str] = []
        genre = genre_from_tag(track.genre)
        if genre is None:
            notes.append("ジャンルタグがないか、候補に当てはまらないためジャンルは不明")

        if features is None:
            attributes = {attr: AttributeScore(value=None) for attr in Attribute}
            notes.append("音響特徴量がないため属性は判定保留")
        else:
            attributes = self._attributes(features)

        return ClassificationResult(
            engine=self.engine,
            engine_version=self.engine_version,
            model=None,
            genre=genre,
            genre_confidence=None,
            genre_probabilities=None,
            mood=decide_mood(attributes, self.mood_decision),
            attributes=attributes,
            notes=notes,
        )

    def _attributes(self, f: AudioFeatures) -> dict[Attribute, AttributeScore]:
        p = self.params
        loud = normalize(f.rms_mean_db, p.rms_db)
        tempo = normalize(f.tempo_bpm, p.tempo_bpm)
        density = normalize(f.onset_rate_per_sec, p.onset_rate_per_sec)
        bright_timbre = _mean(
            normalize(f.spectral_centroid_mean_hz, p.spectral_centroid_hz),
            normalize(f.spectral_rolloff_mean_hz, p.spectral_rolloff_hz),
        )
        noisy = normalize(f.zero_crossing_rate_mean, p.zero_crossing_rate)
        dynamics = normalize(f.loudness_variation_db, p.loudness_variation_db)
        steadiness = 1.0 - min(1.0, f.tempo_variation / _TEMPO_VARIATION_SCALE)

        energy = _mean(loud, tempo, density, bright_timbre)
        relaxation = _mean(1 - loud, 1 - density, 1 - bright_timbre, 1 - dynamics)
        calmness = _mean(1 - density, 1 - dynamics, 1 - noisy)
        moderate_energy = 1.0 - min(1.0, abs(energy - _WORK_ENERGY_CENTER) / _WORK_ENERGY_CENTER)

        values = {
            Attribute.ENERGY: energy,
            Attribute.RELAXATION: relaxation,
            Attribute.CALMNESS: calmness,
            Attribute.WORK_BGM: _mean(moderate_energy, 1 - dynamics, steadiness),
            Attribute.PARTY: _mean(loud, tempo, density),
            Attribute.NIGHT_BGM: _mean(relaxation, calmness, 1 - bright_timbre),
        }
        attributes = {attr: AttributeScore(value=round(v, 4)) for attr, v in values.items()}
        attributes[Attribute.BRIGHTNESS] = AttributeScore(value=None)
        return attributes
