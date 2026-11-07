"""テスト全体で共有する fixture を定義する。

テスト用の音声はすべてテスト内で生成し（正弦波・無音・クリック音）、実在の楽曲は使わない。
"""

import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import mutagen
import numpy as np
import pytest
import soundfile

from volumio_playlist_studio.models import (
    Attribute,
    AttributeScore,
    AudioFeatures,
    ClassificationResult,
    Genre,
    Mood,
    TitleSource,
    Track,
)
from volumio_playlist_studio.storage.database import connect

SAMPLE_RATE = 22050

# 静かで穏やかな曲を想定した音響特徴量
CALM = AudioFeatures(
    tempo_bpm=65.0,
    tempo_variation=0.02,
    rms_mean_db=-35.0,
    rms_std_db=2.0,
    loudness_variation_db=0.8,
    spectral_centroid_mean_hz=900.0,
    spectral_centroid_std_hz=100.0,
    spectral_rolloff_mean_hz=1700.0,
    zero_crossing_rate_mean=0.03,
    onset_strength_mean=0.5,
    onset_strength_std=0.2,
    onset_rate_per_sec=0.6,
    chroma_mean=(0.5,) * 12,
    mfcc_mean=(0.0,) * 13,
    mfcc_std=(1.0,) * 13,
    analyzed_seconds=60.0,
    segment_count=3,
)

AudioWriter = Callable[..., Path]
TrackFactory = Callable[..., Track]
ClassificationFactory = Callable[..., ClassificationResult]


@pytest.fixture
def db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    conn = connect(tmp_path / "data" / "test.sqlite3")
    yield conn
    conn.close()


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # 開発者の環境変数や .env の値がテストに影響しないようにする
    monkeypatch.setenv("TYPESAFE_API_KEY", "")
    monkeypatch.setenv("TYPESAFE_BASE_URL", "")
    monkeypatch.setenv("VOLUMIO_HOST", "")
    monkeypatch.setenv("VPS_DATA_DIR", str(tmp_path / "app-data"))
    monkeypatch.setenv("VPS_PORT", "")
    monkeypatch.setenv("VPS_LAN", "")
    monkeypatch.setenv("VPS_LAN_ADDRESS", "")


def sine(seconds: float, freq: float = 440.0, amplitude: float = 0.3, sr: int = SAMPLE_RATE) -> np.ndarray:
    t = np.arange(int(seconds * sr)) / sr
    return (amplitude * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def clicks(seconds: float, bpm: float, sr: int = SAMPLE_RATE) -> np.ndarray:
    """一定のテンポのクリック音（短い減衰音）を生成する。"""
    signal = np.zeros(int(seconds * sr), dtype=np.float32)
    click = (np.hanning(400) * np.sin(2 * np.pi * 1500 * np.arange(400) / sr)).astype(np.float32)
    step = int(sr * 60 / bpm)
    for start in range(0, len(signal) - len(click), step):
        signal[start : start + len(click)] += click

    return signal


@pytest.fixture
def write_audio() -> AudioWriter:
    """音声ファイルを書き込み、必要ならタグを付ける。"""

    def _write(path: Path, samples: np.ndarray, tags: dict[str, str] | None = None) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        soundfile.write(path, samples, SAMPLE_RATE)
        if tags:
            audio = mutagen.File(path, easy=True)
            assert audio is not None
            audio.update(tags)
            audio.save()

        return path

    return _write


@pytest.fixture
def make_track() -> TrackFactory:
    counter = iter(range(1, 1_000_000))

    def _make(**overrides: Any) -> Track:  # 任意の項目を上書きするため Any で受け取る
        track_id = next(counter)
        base = Track(
            id=track_id,
            library_root="/music",
            rel_path=f"artist/album/{track_id:03d}.flac",
            title=f"曲 {track_id}",
            artist=f"アーティスト {track_id}",
            album="アルバム",
            genre=None,
            year=None,
            track_number=None,
            duration_sec=240.0,
            bitrate=None,
            sample_rate=None,
            channels=None,
            codec="flac",
            file_size=1000,
            mtime_ns=0,
            title_source=TitleSource.TAG,
            tags_repaired=False,
            quick_hash=f"hash-{track_id}",
            status="active",
            tag_error=None,
        )
        return replace(base, **overrides)

    return _make


@pytest.fixture
def make_classification() -> ClassificationFactory:
    def _make(
        genre: Genre | None = Genre.POP,
        mood: Mood | None = None,
        engine: str = "rules",
        **attributes: float | None,
    ) -> ClassificationResult:
        scores = {Attribute(k): AttributeScore(value=v) for k, v in attributes.items()}
        return ClassificationResult(
            engine=engine,
            engine_version=f"{engine}-test",
            model=None,
            genre=genre,
            genre_confidence=None,
            genre_probabilities=None,
            mood=mood,
            attributes=scores,
        )

    return _make
