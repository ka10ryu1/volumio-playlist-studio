"""音響特徴量を、Jev に渡す言葉の区分に変換する。

Jev の公式ドキュメント（Model jaggedness）では、モデルは数値の大小比較が苦手なため、
数値は「コードで区分に変換して渡す」ことが推奨されている。ここで区分への変換を行い、
区分の境界を変えた場合は JEV_QUESTION_VERSION を更新する。
"""

from collections.abc import Sequence

from volumio_playlist_studio.models import AudioFeatures

# （上限, 表現）の組。値が上限未満の最初の表現を使い、どれにも当てはまらなければ最後の表現を使う
_Buckets = Sequence[tuple[float, str]]

_TEMPO: _Buckets = (
    (70.0, "very slow (under 70 BPM)"),
    (95.0, "slow (70-95 BPM)"),
    (120.0, "moderate (95-120 BPM)"),
    (145.0, "fast (120-145 BPM)"),
    (float("inf"), "very fast (over 145 BPM)"),
)
_LOUDNESS: _Buckets = (
    (-35.0, "very quiet"),
    (-25.0, "quiet"),
    (-17.0, "moderate"),
    (-11.0, "loud"),
    (float("inf"), "very loud"),
)
_TIMBRE: _Buckets = (
    (1200.0, "dark or warm (energy concentrated in low frequencies)"),
    (2500.0, "balanced"),
    (float("inf"), "bright or sharp (strong high frequencies)"),
)
_RHYTHMIC_DENSITY: _Buckets = (
    (1.0, "sparse (few note onsets)"),
    (2.5, "moderate"),
    (4.0, "dense"),
    (float("inf"), "very dense (many note onsets)"),
)
_DYNAMICS: _Buckets = (
    (2.0, "steady loudness throughout"),
    (5.0, "some loudness variation between sections"),
    (float("inf"), "large loudness variation between quiet and loud sections"),
)
_NOISINESS: _Buckets = (
    (0.05, "smooth, tonal sound"),
    (0.1, "moderate"),
    (float("inf"), "noisy or percussive sound"),
)
_TEMPO_STABILITY: _Buckets = (
    (0.1, "stable tempo"),
    (float("inf"), "tempo changes between sections"),
)


def bucket(value: float, buckets: _Buckets) -> str:
    """値を区分の表現に変換する。"""
    for upper, label in buckets:
        if value < upper:
            return label
    return buckets[-1][1]


def describe(features: AudioFeatures) -> dict[str, str]:
    """音響特徴量を Jev に渡す区分の辞書に変換する。

    MFCC やクロマのように、言葉の区分に直しにくく意味の解釈が難しい特徴量は渡さない。
    """
    return {
        "tempo": bucket(features.tempo_bpm, _TEMPO),
        "tempo_stability": bucket(features.tempo_variation, _TEMPO_STABILITY),
        "loudness": bucket(features.rms_mean_db, _LOUDNESS),
        "dynamics": bucket(features.loudness_variation_db, _DYNAMICS),
        "timbre": bucket(features.spectral_centroid_mean_hz, _TIMBRE),
        "rhythmic_density": bucket(features.onset_rate_per_sec, _RHYTHMIC_DENSITY),
        "noisiness": bucket(features.zero_crossing_rate_mean, _NOISINESS),
    }
