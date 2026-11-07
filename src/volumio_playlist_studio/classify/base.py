"""分類エンジンの共通インターフェースと、エンジンによらない共通処理。

分類エンジン（ルールベース、Jev、将来の Essentia など）は `Classifier` を実装する。
プレイリスト生成は分類結果（`ClassificationResult`）だけを使い、どのエンジンで作られたかに依存しない。
"""

from typing import Protocol

from volumio_playlist_studio.config import MoodDecision
from volumio_playlist_studio.models import (
    Attribute,
    AttributeScore,
    AudioFeatures,
    ClassificationResult,
    Mood,
    Track,
)


class Classifier(Protocol):
    """楽曲 1 曲を分類するエンジン。"""

    @property
    def engine(self) -> str:
        """エンジン名。DB の分類結果の識別に使う。"""
        ...

    @property
    def engine_version(self) -> str:
        """分類ルール・質問定義のバージョン。"""
        ...

    def classify(self, track: Track, features: AudioFeatures | None) -> ClassificationResult:
        """楽曲を分類する。

        Args:
            track: 対象の楽曲。
            features: 音響特徴量。未解析なら None。

        Returns:
            判定結果。判断できない属性は判定保留（None）にする。
        """
        ...


# ムードごとに、その適合度を表す属性。複数ある場合は平均する
MOOD_COMPONENTS: dict[Mood, tuple[Attribute, ...]] = {
    Mood.RELAX: (Attribute.RELAXATION,),
    Mood.FOCUS: (Attribute.WORK_BGM,),
    Mood.ENERGETIC: (Attribute.ENERGY, Attribute.PARTY),
    Mood.CALM_NIGHT: (Attribute.NIGHT_BGM,),
}


def mood_scores(attributes: dict[Attribute, AttributeScore]) -> dict[Mood, float]:
    """属性スコアからムードごとの適合度を求める。必要な属性が判定保留のムードは含めない。"""
    scores: dict[Mood, float] = {}
    for mood, components in MOOD_COMPONENTS.items():
        values = [attributes[a].value for a in components if a in attributes]
        known = [v for v in values if v is not None]
        if len(known) == len(components):
            scores[mood] = sum(known) / len(known)
    return scores


def decide_mood(attributes: dict[Attribute, AttributeScore], decision: MoodDecision) -> Mood | None:
    """属性スコアからムードを 1 つ決める。

    最も適合度が高いムードを選ぶが、適合度が低い場合や 2 位との差が小さい場合は判定保留（None）にする。
    """
    ranked = sorted(mood_scores(attributes).items(), key=lambda item: item[1], reverse=True)
    if not ranked:
        return None
    best_mood, best_score = ranked[0]
    if best_score < decision.min_score:
        return None
    if len(ranked) > 1 and best_score - ranked[1][1] < decision.min_margin:
        return None
    return best_mood
