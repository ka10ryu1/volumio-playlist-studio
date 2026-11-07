"""アプリ全体で共有する設定値とバージョン定数を管理する。

複数のモジュールから参照する定数（解析ロジックのバージョン、既定値、正規化の範囲など）は
このモジュールに集約し、各モジュールに散在させない。
"""

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# リポジトリのルート（src/volumio_playlist_studio/config.py から 2 階層上）。
# ダブルクリックでの起動でもカレントフォルダに依存しないよう、.env とデータの保存先の基準にする
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 解析・分類ロジックのバージョン。出力が変わる変更では必ず更新する。
# 評価データがどのバージョンの結果に対するものかを追跡するために、DB の各結果に記録する。
ANALYSIS_VERSION = "features-1"
RULE_VERSION = "rules-1"
JEV_QUESTION_VERSION = "jev-questions-1"

# 対応する拡張子。タグ取得は mutagen、音響解析は librosa（soundfile 経由）で行う。
# M4A/AAC は libsndfile が対応していないため、タグのみ取得し音響解析はエラーとして記録する。
SUPPORTED_EXTENSIONS = frozenset({".mp3", ".flac", ".m4a", ".ogg", ".opus", ".wav", ".aif", ".aiff"})
ANALYZABLE_EXTENSIONS = frozenset({".mp3", ".flac", ".ogg", ".opus", ".wav", ".aif", ".aiff"})

# 環境変数名。接続先や認証情報はコードに直書きせず、環境変数または .env から読む
DATA_DIR_ENV = "VPS_DATA_DIR"
TYPESAFE_API_KEY_ENV = "TYPESAFE_API_KEY"  # TypeSafe SDK が読む環境変数名と同じ
VOLUMIO_HOST_ENV = "VOLUMIO_HOST"
PORT_ENV = "VPS_PORT"
LAN_ENV = "VPS_LAN"
LAN_ADDRESS_ENV = "VPS_LAN_ADDRESS"

# 人手評価（属性の評価、プレイリストの満足度）に使う 5 段階の尺度
RATING_MIN = 1
RATING_MAX = 5

DEFAULT_DATA_DIR = PROJECT_ROOT / "data"
DEFAULT_PORT = 8501
ENV_FILENAME = ".env"
DATABASE_FILENAME = "studio.sqlite3"
LOG_DIRNAME = "logs"
LOG_FILENAME = "app.log"


@dataclass(frozen=True)
class AnalysisParams:
    """librosa による音響解析のパラメータ。

    曲全体を解析すると時間がかかるため、イントロ・アウトロを除いた区間から
    等間隔に複数の区間を切り出して解析する。

    Attributes:
        sample_rate: 解析時にリサンプリングする周波数（Hz）。
        segment_count: 切り出す区間の数。
        segment_seconds: 1 区間の長さ（秒）。
        edge_margin_ratio: 曲の先頭・末尾から除外する割合（イントロ・アウトロ対策）。
        silence_threshold_db: これより小さい平均 RMS（dBFS）の区間を無音として除外する。
    """

    sample_rate: int = 22050
    segment_count: int = 3
    segment_seconds: float = 20.0
    edge_margin_ratio: float = 0.1
    silence_threshold_db: float = -60.0

    def validate(self) -> None:
        """値の範囲を検証する。

        Raises:
            ValueError: 範囲外の値がある場合。
        """
        _check_range("sample_rate", self.sample_rate, 8000, 48000)
        _check_range("segment_count", self.segment_count, 1, 10)
        _check_range("segment_seconds", self.segment_seconds, 5.0, 120.0)
        _check_range("edge_margin_ratio", self.edge_margin_ratio, 0.0, 0.45)
        _check_range("silence_threshold_db", self.silence_threshold_db, -120.0, -1.0)


def _check_range(name: str, value: float, low: float, high: float) -> None:
    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}")


@dataclass(frozen=True)
class JevSettings:
    """TypeSafe Jev API の利用設定。API キーはここに持たず、環境変数から SDK が直接読む。

    Attributes:
        model: 使用するモデル名。
        timeout_sec: 1 リクエストあたりのタイムアウト（秒）。
        min_confidence: これ未満の confidence の回答は判定保留として扱う。
        genre_determinable_threshold: 「ジャンルを判断できる」の確率がこれ未満なら、ジャンルを不明とする。
        send_titles: 曲名を Jev に送るか。
        max_tracks_per_run: 1 回の実行で判定する曲数の上限（料金の暴走防止）。
        max_consecutive_failures: 連続して通信に失敗したら実行を打ち切る回数。
    """

    model: str = "jev-latest"
    timeout_sec: float = 30.0
    min_confidence: float = 0.5
    genre_determinable_threshold: float = 0.5
    send_titles: bool = True
    max_tracks_per_run: int = 20
    max_consecutive_failures: int = 3


@dataclass(frozen=True)
class RuleParameters:
    """ルールベース分類で特徴量を 0〜1 に正規化するときの範囲。

    単一の特徴量で判断しないよう、各属性は複数の特徴量の平均として求める。
    範囲は一般的なポピュラー音楽の値の分布を目安にした初期値であり、評価結果に基づいて調整する。
    """

    tempo_bpm: tuple[float, float] = (60.0, 160.0)
    rms_db: tuple[float, float] = (-40.0, -8.0)
    spectral_centroid_hz: tuple[float, float] = (800.0, 3500.0)
    spectral_rolloff_hz: tuple[float, float] = (1500.0, 7000.0)
    zero_crossing_rate: tuple[float, float] = (0.02, 0.15)
    onset_rate_per_sec: tuple[float, float] = (0.5, 5.0)
    loudness_variation_db: tuple[float, float] = (0.5, 8.0)


@dataclass(frozen=True)
class MoodDecision:
    """属性スコアからムードを 1 つに決めるときの閾値。

    Attributes:
        min_score: 最も高いムードのスコアがこれ未満なら判定保留にする。
        min_margin: 1 位と 2 位の差がこれ未満なら判定保留にする。
    """

    min_score: float = 0.6
    min_margin: float = 0.1


@dataclass(frozen=True)
class PlaylistDefaults:
    """プレイリスト生成の既定値と制約。"""

    min_duration_min: int = 30
    max_duration_min: int = 180
    default_duration_min: int = 60
    # 目標の再生時間をこの分数まで超えることを許す
    duration_tolerance_min: float = 5.0
    # 雰囲気への適合度がこれ以上の曲を候補にする
    suitability_threshold: float = 0.55
    # 「条件を緩和して再生成」を選んだときの適合度の閾値
    relaxed_suitability_threshold: float = 0.4
    max_track_count: int = 200


@dataclass(frozen=True)
class EvaluationDefaults:
    """精度評価の既定値。"""

    min_sample_size: int = 30
    max_sample_size: int = 50
    default_sample_size: int = 40
    # 評価データのうちルール調整に使ってよい割合。残りは検証専用にする
    tuning_ratio: float = 0.5


@dataclass(frozen=True)
class AppConfig:
    """起動時に環境変数から決まる設定。

    Attributes:
        data_dir: SQLite やログを保存するディレクトリ。
        volumio_host: Volumio の接続先（未設定なら None）。
        port: GUI のポート番号。
        lan: True なら LAN 内の他の端末（スマホ）からの接続を受け付け、PIN を求める。
        lan_address: スマホから接続する PC の IP アドレス。None なら起動時に自動で取得する。
    """

    data_dir: Path
    volumio_host: str | None = None
    port: int = DEFAULT_PORT
    lan: bool = False
    lan_address: str | None = None
    analysis: AnalysisParams = field(default_factory=AnalysisParams)
    jev: JevSettings = field(default_factory=JevSettings)

    @property
    def database_path(self) -> Path:
        """SQLite データベースのパス。"""
        return self.data_dir / DATABASE_FILENAME

    @property
    def log_path(self) -> Path:
        """ログファイルのパス。"""
        return self.data_dir / LOG_DIRNAME / LOG_FILENAME


def load_env_file(path: Path | None = None) -> None:
    """プロジェクト直下の .env を環境変数に読み込む。既に設定されている環境変数は上書きしない。

    Args:
        path: 読み込む .env のパス。省略時はプロジェクト直下の .env。
    """
    load_dotenv(path or PROJECT_ROOT / ENV_FILENAME, override=False)


def load_app_config(environ: dict[str, str] | None = None) -> AppConfig:
    """環境変数から設定を読み込む。

    Args:
        environ: 参照する環境変数。省略時は `os.environ`。

    Returns:
        読み込んだ設定。
    """
    env = dict(os.environ) if environ is None else environ
    data_dir = Path(env.get(DATA_DIR_ENV, "").strip() or DEFAULT_DATA_DIR)
    if not data_dir.is_absolute():
        data_dir = PROJECT_ROOT / data_dir

    return AppConfig(
        data_dir=data_dir,
        volumio_host=env.get(VOLUMIO_HOST_ENV, "").strip() or None,
        port=_env_port(env.get(PORT_ENV, "")),
        lan=env.get(LAN_ENV, "").strip().lower() in {"1", "true", "yes"},
        lan_address=env.get(LAN_ADDRESS_ENV, "").strip() or None,
    )


def _env_port(raw: str, minimum: int = 1024, maximum: int = 65535) -> int:
    """ポート番号を読む。未設定なら既定値、範囲外や数値でなければ警告を出して既定値を使う。"""
    if not raw.strip():
        return DEFAULT_PORT

    try:
        port = int(raw)
    except ValueError:
        port = 0

    if not minimum <= port <= maximum:
        logger.warning("invalid port setting: name=%s fallback=%d", PORT_ENV, DEFAULT_PORT)
        return DEFAULT_PORT

    return port


def is_typesafe_api_key_set(environ: dict[str, str] | None = None) -> bool:
    """TypeSafe の API キーが設定されているかを返す。キーの値自体は返さない。"""
    env = os.environ if environ is None else environ
    return bool(env.get(TYPESAFE_API_KEY_ENV, "").strip())
