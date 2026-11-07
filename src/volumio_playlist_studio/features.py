"""librosa による音響特徴量の抽出。

曲全体を読むと時間がかかるため、イントロ・アウトロを除いた区間から等間隔に複数の区間を切り出して解析する。
無音に近い区間は除外し、特徴量が無音部分に引きずられないようにする。
各特徴量の意味と分類への使い方は docs/audio-features.md を参照。
"""

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import librosa
import numpy as np
import soundfile
from numpy.typing import NDArray

from volumio_playlist_studio.config import AnalysisParams
from volumio_playlist_studio.models import AudioFeatures

# これより短い区間は特徴量が安定しないため使わない
_MIN_SEGMENT_SECONDS = 2.0
_N_MFCC = 13
# 振幅をデシベルに変換するときの下限。完全な無音は -100 dBFS になる
_AMPLITUDE_FLOOR = 1e-5

FloatArray = NDArray[np.float32]


class FeatureExtractionError(Exception):
    """音声から特徴量を求められない場合の例外（無音、短すぎるなど）。"""


@dataclass(frozen=True)
class Segment:
    """解析する区間。

    Attributes:
        offset: 曲の先頭からの開始位置（秒）。
        duration: 長さ（秒）。
    """

    offset: float
    duration: float


@dataclass(frozen=True)
class _SegmentFeatures:
    seconds: float
    tempo: float
    onset_count: int
    rms_db: FloatArray
    onset_strength: FloatArray
    centroid: FloatArray
    rolloff: FloatArray
    zcr: FloatArray
    chroma: FloatArray
    mfcc: FloatArray


def plan_segments(total_seconds: float, params: AnalysisParams) -> list[Segment]:
    """解析する区間を決める。

    曲の先頭・末尾の `edge_margin_ratio` を除いた範囲を区間数で等分し、それぞれの中央から切り出す。
    除外後の範囲が必要な長さに足りない短い曲では、除外後の範囲全体を 1 区間として使う。

    Args:
        total_seconds: 曲の長さ（秒）。
        params: 解析パラメータ。

    Returns:
        重ならず、開始位置の昇順に並んだ区間。

    Raises:
        FeatureExtractionError: 曲が短すぎて解析できない場合。
    """
    if total_seconds < _MIN_SEGMENT_SECONDS:
        raise FeatureExtractionError("audio is too short to analyze")

    start = total_seconds * params.edge_margin_ratio
    end = total_seconds - start
    usable = end - start
    if usable < _MIN_SEGMENT_SECONDS:
        return [Segment(0.0, total_seconds)]
    if usable <= params.segment_count * params.segment_seconds:
        return [Segment(start, usable)]

    slot = usable / params.segment_count
    return [
        Segment(start + slot * (i + 0.5) - params.segment_seconds / 2, params.segment_seconds)
        for i in range(params.segment_count)
    ]


def extract_features(path: Path, params: AnalysisParams) -> AudioFeatures:
    """音声ファイルから特徴量を抽出する。

    Args:
        path: 音声ファイルのパス。
        params: 解析パラメータ。

    Returns:
        抽出した特徴量。

    Raises:
        FeatureExtractionError: 無音または短すぎて解析できない場合。
        soundfile.LibsndfileError: 対応していない形式、または壊れたファイルの場合。
    """
    total_seconds = soundfile.info(str(path)).duration
    chunks = (
        librosa.load(path, sr=params.sample_rate, mono=True, offset=segment.offset, duration=segment.duration)[0]
        for segment in plan_segments(total_seconds, params)
    )
    return _features_from_chunks(chunks, params)


def extract_features_from_samples(samples: FloatArray, params: AnalysisParams) -> AudioFeatures:
    """読み込み済みの波形から特徴量を抽出する。区間の切り出しは `extract_features` と同じ方法で行う。

    Args:
        samples: `params.sample_rate` でサンプリングされたモノラル波形。
        params: 解析パラメータ。

    Raises:
        FeatureExtractionError: 無音または短すぎて解析できない場合。
    """
    sr = params.sample_rate
    chunks = (
        samples[int(segment.offset * sr) : int(segment.offset * sr) + int(segment.duration * sr)]
        for segment in plan_segments(len(samples) / sr, params)
    )
    return _features_from_chunks(chunks, params)


def _features_from_chunks(chunks: Iterable[FloatArray], params: AnalysisParams) -> AudioFeatures:
    """区間ごとの波形から特徴量を求める。短すぎる区間と無音の区間は除外する。"""
    segments: list[_SegmentFeatures] = []
    for chunk in chunks:
        if len(chunk) < _MIN_SEGMENT_SECONDS * params.sample_rate:
            continue
        # 無音の区間は、重い特徴量を計算する前に除外する。フレームごとの dB の平均は無音フレームに
        # 引きずられて小さくなるため、区間全体の RMS（線形で平均してから dB に変換）で判定する
        segment_rms = float(np.sqrt(np.mean(np.square(chunk, dtype=np.float64))))
        if 20 * np.log10(max(segment_rms, _AMPLITUDE_FLOOR)) >= params.silence_threshold_db:
            segments.append(_analyze_segment(chunk, params.sample_rate))

    if not segments:
        raise FeatureExtractionError("audio is silent or too short to analyze")
    return _aggregate(segments)


def _analyze_segment(samples: FloatArray, sr: int) -> _SegmentFeatures:
    onset_env = librosa.onset.onset_strength(y=samples, sr=sr)
    tempo = librosa.feature.tempo(onset_envelope=onset_env, sr=sr)
    onsets = librosa.onset.onset_detect(onset_envelope=onset_env, sr=sr)
    return _SegmentFeatures(
        seconds=len(samples) / sr,
        tempo=float(tempo[0]),
        onset_count=len(onsets),
        rms_db=librosa.amplitude_to_db(librosa.feature.rms(y=samples)[0], ref=1.0, amin=_AMPLITUDE_FLOOR),
        onset_strength=onset_env,
        centroid=librosa.feature.spectral_centroid(y=samples, sr=sr)[0],
        rolloff=librosa.feature.spectral_rolloff(y=samples, sr=sr)[0],
        zcr=librosa.feature.zero_crossing_rate(samples)[0],
        # チューニングの推定は音程の少ない区間で不安定になるため行わず、A4 = 440 Hz を前提にする
        chroma=librosa.feature.chroma_stft(y=samples, sr=sr, tuning=0.0),
        mfcc=librosa.feature.mfcc(y=samples, sr=sr, n_mfcc=_N_MFCC),
    )


def _aggregate(segments: list[_SegmentFeatures]) -> AudioFeatures:
    """区間ごとの値を 1 曲分にまとめる。フレーム単位の値は全区間をつなげて平均・標準偏差を求める。"""
    rms_db = np.concatenate([s.rms_db for s in segments])
    onset_strength = np.concatenate([s.onset_strength for s in segments])
    centroid = np.concatenate([s.centroid for s in segments])
    chroma = np.concatenate([s.chroma for s in segments], axis=1)
    mfcc = np.concatenate([s.mfcc for s in segments], axis=1)
    tempos = np.array([s.tempo for s in segments])
    segment_loudness = np.array([float(np.mean(s.rms_db)) for s in segments])
    seconds = sum(s.seconds for s in segments)
    tempo_mean = float(np.mean(tempos))

    return AudioFeatures(
        tempo_bpm=float(np.median(tempos)),
        # 区間ごとのテンポのばらつき（変動係数）。テンポが途中で変わる曲ほど大きい
        tempo_variation=float(np.std(tempos) / tempo_mean) if tempo_mean > 0 else 0.0,
        rms_mean_db=float(np.mean(rms_db)),
        rms_std_db=float(np.std(rms_db)),
        # 区間ごとの平均音量のばらつき。曲の中で静かな部分と盛り上がる部分の差が大きいほど大きい
        loudness_variation_db=float(np.std(segment_loudness)),
        spectral_centroid_mean_hz=float(np.mean(centroid)),
        spectral_centroid_std_hz=float(np.std(centroid)),
        spectral_rolloff_mean_hz=float(np.mean(np.concatenate([s.rolloff for s in segments]))),
        zero_crossing_rate_mean=float(np.mean(np.concatenate([s.zcr for s in segments]))),
        onset_strength_mean=float(np.mean(onset_strength)),
        onset_strength_std=float(np.std(onset_strength)),
        onset_rate_per_sec=sum(s.onset_count for s in segments) / seconds,
        chroma_mean=tuple(float(v) for v in np.mean(chroma, axis=1)),
        mfcc_mean=tuple(float(v) for v in np.mean(mfcc, axis=1)),
        mfcc_std=tuple(float(v) for v in np.std(mfcc, axis=1)),
        analyzed_seconds=seconds,
        segment_count=len(segments),
    )
