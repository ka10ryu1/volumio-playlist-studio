import itertools
import math
from pathlib import Path

import numpy as np
import pytest
import soundfile
from conftest import SAMPLE_RATE, AudioWriter, clicks, sine
from hypothesis import given, settings
from hypothesis import strategies as st

from volumio_playlist_studio.config import AnalysisParams
from volumio_playlist_studio.features import (
    FeatureExtractionError,
    extract_features,
    extract_features_from_samples,
    plan_segments,
)
from volumio_playlist_studio.models import AudioFeatures

PARAMS = AnalysisParams(segment_count=2, segment_seconds=6.0)


@given(
    total=st.floats(min_value=2.0, max_value=3600.0),
    count=st.integers(min_value=1, max_value=10),
    seconds=st.floats(min_value=5.0, max_value=120.0),
    margin=st.floats(min_value=0.0, max_value=0.45),
)
def test_planned_segments_stay_inside_track_and_do_not_overlap(
    total: float, count: int, seconds: float, margin: float
) -> None:
    params = AnalysisParams(segment_count=count, segment_seconds=seconds, edge_margin_ratio=margin)

    segments = plan_segments(total, params)

    assert 1 <= len(segments) <= count
    for segment in segments:
        assert segment.duration > 0
        assert segment.offset >= -1e-9
        assert segment.offset + segment.duration <= total + 1e-6
    for previous, current in itertools.pairwise(segments):
        assert previous.offset + previous.duration <= current.offset + 1e-6


def test_segments_skip_intro_and_outro_of_long_tracks() -> None:
    params = AnalysisParams(segment_count=3, segment_seconds=20.0, edge_margin_ratio=0.1)

    segments = plan_segments(300.0, params)

    assert len(segments) == 3
    assert segments[0].offset >= 30.0
    assert segments[-1].offset + segments[-1].duration <= 270.0
    assert all(s.duration == 20.0 for s in segments)


def test_short_track_is_analyzed_as_one_segment() -> None:
    segments = plan_segments(30.0, AnalysisParams(segment_count=3, segment_seconds=20.0, edge_margin_ratio=0.1))

    assert len(segments) == 1
    assert segments[0].offset == pytest.approx(3.0)
    assert segments[0].duration == pytest.approx(24.0)


def test_too_short_track_is_rejected() -> None:
    with pytest.raises(FeatureExtractionError):
        plan_segments(1.0, PARAMS)


def test_silence_is_rejected() -> None:
    with pytest.raises(FeatureExtractionError, match="silent"):
        extract_features_from_samples(np.zeros(SAMPLE_RATE * 20, dtype=np.float32), PARAMS)


def test_silent_segments_are_excluded() -> None:
    # 前半が無音、後半が音のある曲。無音の区間は除外され、音のある区間だけが解析される
    samples = np.concatenate([np.zeros(SAMPLE_RATE * 15, dtype=np.float32), sine(15.0)])

    features = extract_features_from_samples(samples, PARAMS)

    assert features.segment_count == 1
    assert features.rms_mean_db > -20


def test_tempo_of_click_track_is_close_to_true_tempo() -> None:
    features = extract_features_from_samples(clicks(20.0, bpm=120), PARAMS)

    assert features.tempo_bpm == pytest.approx(120, abs=8)
    assert features.onset_rate_per_sec == pytest.approx(2.0, abs=0.3)


def test_louder_signal_has_higher_rms() -> None:
    quiet = extract_features_from_samples(sine(20.0, amplitude=0.05), PARAMS)
    loud = extract_features_from_samples(sine(20.0, amplitude=0.5), PARAMS)

    # 振幅が 10 倍なら RMS は 20 dB 大きい
    assert loud.rms_mean_db - quiet.rms_mean_db == pytest.approx(20.0, abs=0.5)


def test_higher_frequency_has_higher_spectral_centroid_and_zero_crossing_rate() -> None:
    low = extract_features_from_samples(sine(20.0, freq=220), PARAMS)
    high = extract_features_from_samples(sine(20.0, freq=3000), PARAMS)

    assert high.spectral_centroid_mean_hz > low.spectral_centroid_mean_hz
    assert high.zero_crossing_rate_mean > low.zero_crossing_rate_mean
    # 正弦波のゼロ交差率は 2 * 周波数 / サンプリング周波数
    assert low.zero_crossing_rate_mean == pytest.approx(2 * 220 / SAMPLE_RATE, rel=0.05)


def test_extraction_is_deterministic() -> None:
    samples = sine(20.0) + clicks(20.0, bpm=100)

    assert extract_features_from_samples(samples, PARAMS) == extract_features_from_samples(samples, PARAMS)


@settings(max_examples=8, deadline=None)
@given(
    freq=st.floats(min_value=50.0, max_value=5000.0),
    amplitude=st.floats(min_value=0.01, max_value=1.0),
    bpm=st.floats(min_value=50.0, max_value=200.0),
)
def test_features_are_finite_and_within_physical_ranges(freq: float, amplitude: float, bpm: float) -> None:
    samples = np.clip(sine(14.0, freq=freq, amplitude=amplitude) + 0.3 * clicks(14.0, bpm=bpm), -1, 1)

    features = extract_features_from_samples(samples.astype(np.float32), PARAMS)

    scalars = [v for v in features.to_dict().values() if isinstance(v, float)]
    assert all(math.isfinite(v) for v in scalars)
    assert features.rms_mean_db <= 0.0
    assert 0.0 <= features.spectral_centroid_mean_hz <= SAMPLE_RATE / 2
    assert 0.0 <= features.spectral_rolloff_mean_hz <= SAMPLE_RATE / 2
    assert 0.0 <= features.zero_crossing_rate_mean <= 1.0
    assert all(0.0 <= v <= 1.0 for v in features.chroma_mean)
    assert len(features.chroma_mean) == 12
    assert len(features.mfcc_mean) == len(features.mfcc_std) == 13
    assert features.tempo_bpm >= 0.0


def test_extract_features_from_file(tmp_path: Path, write_audio: AudioWriter) -> None:
    path = write_audio(tmp_path / "日本語の曲名.flac", sine(30.0) + clicks(30.0, bpm=120))

    features = extract_features(path, PARAMS)

    assert features.segment_count == 2
    assert features.analyzed_seconds == pytest.approx(12.0, abs=0.1)
    assert features.tempo_bpm == pytest.approx(120, abs=8)


def test_extract_features_of_corrupted_file_raises(tmp_path: Path) -> None:
    path = tmp_path / "broken.flac"
    path.write_bytes(b"not a flac file" * 100)

    with pytest.raises(soundfile.LibsndfileError):
        extract_features(path, PARAMS)


def test_features_roundtrip_through_dict() -> None:
    features = extract_features_from_samples(sine(14.0), PARAMS)

    assert AudioFeatures.from_dict(features.to_dict()) == features


@pytest.mark.parametrize(
    "broken",
    [
        {},
        {"tempo_bpm": "fast"},
    ],
)
def test_features_from_invalid_dict_raises(broken: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="invalid"):
        AudioFeatures.from_dict(broken)


def test_features_from_dict_rejects_non_finite_values() -> None:
    data = extract_features_from_samples(sine(14.0), PARAMS).to_dict()
    data["tempo_bpm"] = float("nan")

    with pytest.raises(ValueError, match="finite"):
        AudioFeatures.from_dict(data)
