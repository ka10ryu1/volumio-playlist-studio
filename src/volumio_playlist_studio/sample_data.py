"""動作確認用のサンプルデータ。

- `load_demo_library`: 架空の楽曲と合成した音響特徴量を DB に登録する。音声ファイルがなくても GUI を操作できる。
- `generate_sample_audio`: 正弦波・クリック音などの短い合成音声ファイルを作る。実際のスキャン・解析を試せる。

どちらも架空のデータだけを使い、実在の楽曲や利用者の音楽ファイルは使わない。
"""

import argparse
import logging
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

import mutagen
import numpy as np
import soundfile

from volumio_playlist_studio.classify.rules import RuleClassifier
from volumio_playlist_studio.config import ANALYSIS_VERSION, AnalysisParams
from volumio_playlist_studio.models import AudioFeatures, TitleSource, TrackMetadata
from volumio_playlist_studio.storage import analysis as analysis_store
from volumio_playlist_studio.storage.database import connect, transaction, utc_now
from volumio_playlist_studio.storage.tracks import ScanSummary, get_track, record_scan, upsert_track

logger = logging.getLogger(__name__)

DEMO_LIBRARY_ROOT = "sample://demo"
_SAMPLE_RATE = 22050


@dataclass(frozen=True)
class _Style:
    """架空の楽曲の作風。音響特徴量を生成するときの中心値。"""

    genre_tag: str
    tempo: float
    rms_db: float
    centroid: float
    onset_rate: float
    loudness_variation: float


_STYLES: tuple[tuple[str, _Style], ...] = (
    ("サンプル弦楽団", _Style("Classical", 72, -28, 1300, 0.9, 5.5)),
    ("Demo Quiet Piano", _Style("New Age", 66, -30, 1100, 0.7, 2.0)),
    ("架空のアンビエント工房", _Style("Ambient", 80, -32, 1000, 0.5, 1.5)),
    ("Example Jazz Trio", _Style("Jazz", 110, -20, 1900, 2.4, 3.5)),
    ("テスト・ポップス", _Style("J-Pop", 124, -12, 2600, 3.6, 2.5)),
    ("Placeholder Rockers", _Style("Rock", 140, -10, 3000, 4.3, 3.0)),
    ("ダミー電子音楽隊", _Style("Techno", 128, -9, 2900, 4.8, 1.5)),
    ("Fictional Folk Duo", _Style("Acoustic", 96, -22, 1700, 1.8, 2.5)),
    ("サンプル・ビーツ", _Style("Hip Hop", 92, -11, 2200, 3.0, 1.8)),
    ("ジャンル不明の演奏家", _Style("", 105, -18, 2000, 2.2, 2.8)),
)
_TRACKS_PER_ARTIST = 6


def _demo_features(style: _Style, rng: np.random.Generator) -> AudioFeatures:
    def jitter(center: float, spread: float) -> float:
        return float(center + rng.normal(0, spread))

    return AudioFeatures(
        tempo_bpm=max(40.0, jitter(style.tempo, 8)),
        tempo_variation=abs(jitter(0.04, 0.03)),
        rms_mean_db=jitter(style.rms_db, 2.5),
        rms_std_db=abs(jitter(4.0, 1.0)),
        loudness_variation_db=abs(jitter(style.loudness_variation, 0.8)),
        spectral_centroid_mean_hz=max(300.0, jitter(style.centroid, 250)),
        spectral_centroid_std_hz=abs(jitter(400, 100)),
        spectral_rolloff_mean_hz=max(600.0, jitter(style.centroid * 2.0, 400)),
        zero_crossing_rate_mean=max(0.005, jitter(style.centroid / 30000, 0.01)),
        onset_strength_mean=abs(jitter(1.2, 0.3)),
        onset_strength_std=abs(jitter(0.9, 0.2)),
        onset_rate_per_sec=max(0.1, jitter(style.onset_rate, 0.4)),
        chroma_mean=tuple(float(v) for v in rng.uniform(0.2, 0.8, 12)),
        mfcc_mean=tuple(float(v) for v in rng.normal(0, 20, 13)),
        mfcc_std=tuple(float(v) for v in rng.uniform(5, 30, 13)),
        analyzed_seconds=60.0,
        segment_count=3,
    )


def load_demo_library(conn: sqlite3.Connection, seed: int = 0) -> int:
    """架空の楽曲を DB に登録し、ルールベース分類まで実行する。既に登録済みの曲は上書きする。

    Returns:
        登録した曲数。
    """
    rng = np.random.default_rng(seed)
    rules = RuleClassifier()
    started_at = utc_now()
    count = 0
    with transaction(conn):
        for artist_index, (artist, style) in enumerate(_STYLES):
            for number in range(1, _TRACKS_PER_ARTIST + 1):
                meta = TrackMetadata(
                    rel_path=f"{artist}/サンプルアルバム {artist_index + 1}/{number:02d} サンプル曲 {number}.flac",
                    title=f"サンプル曲 {artist_index + 1}-{number}",
                    artist=artist,
                    album=f"サンプルアルバム {artist_index + 1}",
                    genre=style.genre_tag or None,
                    year=2000 + artist_index,
                    track_number=number,
                    duration_sec=float(rng.integers(150, 420)),
                    bitrate=900_000,
                    sample_rate=44100,
                    channels=2,
                    codec="flac",
                    file_size=int(rng.integers(5_000_000, 40_000_000)),
                    mtime_ns=0,
                    title_source=TitleSource.TAG,
                    tags_repaired=False,
                    quick_hash=f"demo-{artist_index}-{number}",
                )
                track_id = upsert_track(conn, DEMO_LIBRARY_ROOT, meta, None)
                track = get_track(conn, track_id)
                if track is None:  # 直前に登録した行が見つからないのは DB の異常
                    raise RuntimeError("inserted demo track was not found")
                features = _demo_features(style, rng)
                analysis_store.save_features(
                    conn, track, features, analysis_store.FeatureRun(AnalysisParams(), ANALYSIS_VERSION, 0.0)
                )
                analysis_store.save_classification(conn, track_id, rules.classify(track, features))
                count += 1
        record_scan(conn, DEMO_LIBRARY_ROOT, started_at, ScanSummary(count, 0, 0, 0, 0, 0.0))
    logger.info("demo library loaded: tracks=%d", count)
    return count


def _synth(kind: str, seconds: float, rng: np.random.Generator) -> np.ndarray:
    t = np.arange(int(seconds * _SAMPLE_RATE)) / _SAMPLE_RATE
    if kind == "calm":
        signal = 0.1 * np.sin(2 * np.pi * 220 * t) + 0.05 * np.sin(2 * np.pi * 330 * t)
    else:
        signal = 0.3 * np.sin(2 * np.pi * 440 * t)
        clicks = np.zeros_like(t)
        clicks[:: int(_SAMPLE_RATE * 60 / 140)] = 1.0
        signal = signal + 0.6 * np.convolve(clicks, np.hanning(200), mode="same")
        signal = signal + 0.05 * rng.normal(size=len(t))
    return np.clip(signal, -1.0, 1.0).astype(np.float32)


def generate_sample_audio(directory: Path, seconds: float = 30.0, seed: int = 0) -> list[Path]:
    """合成音声の FLAC ファイルを作り、タグを書き込む。

    利用者の音楽フォルダを変更しないよう、存在しないか空のフォルダにだけ作成する。

    Raises:
        FileExistsError: フォルダが空でない場合。
    """
    if directory.exists() and any(directory.iterdir()):
        raise FileExistsError("sample audio directory must be empty")

    rng = np.random.default_rng(seed)
    specs = [
        ("架空の静かな楽団", "静かなサンプル", "calm", "Ambient"),
        ("架空の静かな楽団", "夜のサンプル", "calm", "New Age"),
        ("Demo Beat Makers", "Upbeat Sample", "upbeat", "Electronic"),
        ("Demo Beat Makers", "Driving Sample", "upbeat", "Rock"),
    ]
    paths = []
    for index, (artist, title, kind, genre) in enumerate(specs, start=1):
        path = directory / artist / f"{index:02d} {title}.flac"
        path.parent.mkdir(parents=True, exist_ok=True)
        soundfile.write(path, _synth(kind, seconds, rng), _SAMPLE_RATE)
        audio = mutagen.File(path, easy=True)
        if audio is None:  # 直前に書き込んだ FLAC を読めないのは異常
            raise RuntimeError("generated sample audio is not readable")
        audio.update(
            {"title": title, "artist": artist, "album": "サンプル音源", "genre": genre, "tracknumber": str(index)}
        )
        audio.save()
        paths.append(path)
    return paths


def main(argv: list[str] | None = None) -> int:
    """コマンドラインからサンプルデータを作る。"""
    parser = argparse.ArgumentParser(description="動作確認用のサンプルデータを作成する")
    sub = parser.add_subparsers(dest="command", required=True)
    audio = sub.add_parser("audio", help="合成音声のサンプルファイルを作る")
    audio.add_argument("directory", type=Path)
    demo = sub.add_parser("demo", help="架空の楽曲を DB に登録する")
    demo.add_argument("database", type=Path)
    args = parser.parse_args(argv)

    if args.command == "audio":
        paths = generate_sample_audio(args.directory)
        sys.stdout.write(f"{len(paths)} 個のサンプル音声を作成しました\n")
    else:
        conn = connect(args.database)
        try:
            count = load_demo_library(conn)
        finally:
            conn.close()
        sys.stdout.write(f"{count} 曲のサンプルデータを登録しました\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
