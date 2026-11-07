"""mutagen による楽曲タグとファイル情報の取得。音楽ファイルは読み取りのみ行う。"""

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import mutagen

from volumio_playlist_studio.models import TitleSource, TrackMetadata

# 重複検出用ハッシュで読む先頭・末尾の大きさ。SMB 越しでも全体を読まずに済むようにする
_HASH_CHUNK_BYTES = 64 * 1024

_YEAR = re.compile(r"(?<!\d)(\d{4})(?!\d)")
_TRACK_NUMBER = re.compile(r"^\s*(\d+)")
# ファイル名先頭のトラック番号（例: "01 - ", "1-03. ", "07_"）
# 4 桁以上の数字（"2001 Space Song" の年など）はトラック番号とみなさない
_LEADING_TRACK_NUMBER = re.compile(r"^\s*(?:\d{1,2}[-.])?\d{1,3}(?!\d)\s*[-._)\]]?\s+|^\s*\d{1,3}(?!\d)[-._]\s*")
_JAPANESE_CHAR = re.compile(r"[぀-ヿ㐀-䶿一-鿿ｦ-ﾟ]")
_C1_CONTROL = re.compile(r"[\x80-\x9f]")
_LATIN1_MAX = 0xFF


@dataclass(frozen=True)
class TagReadResult:
    """タグの読み取り結果。

    Attributes:
        metadata: 取得できた範囲のメタデータ。タグを読めなくてもファイル情報とファイル名からの補完値は入る。
        error: タグを読めなかった理由。読めた場合は None。
    """

    metadata: TrackMetadata
    error: str | None


def repair_mojibake(text: str) -> tuple[str, bool]:
    """Latin-1 として誤って解釈された日本語の文字列を修復する。

    古い MP3 では、ID3 タグの文字コードが Latin-1 と宣言されたまま、実際には Shift_JIS や UTF-8 の
    バイト列が保存されていることがある。この場合 mutagen は Latin-1 として解釈するため文字化けする。
    修復後の文字列に日本語の文字が含まれる場合だけ修復結果を採用し、欧文の曲名を誤って変換しない。

    Args:
        text: タグから読んだ文字列。

    Returns:
        修復後の文字列と、修復したかどうか。
    """
    if not any("\x80" <= ch <= "\xff" for ch in text) or any(ord(ch) > _LATIN1_MAX for ch in text):
        return text, False

    raw = text.encode("latin-1")
    for encoding in ("utf-8", "cp932"):
        try:
            candidate = raw.decode(encoding)
        except UnicodeDecodeError:
            continue

        if _JAPANESE_CHAR.search(candidate) and not _C1_CONTROL.search(candidate):
            return candidate, True

    return text, False


def title_from_filename(rel_path: str) -> str | None:
    """ファイル名から曲名を補完する。先頭のトラック番号は取り除く。

    アーティスト名などはファイル名の書式が一定でないため推測しない。
    """
    stem = Path(rel_path).stem
    title = _LEADING_TRACK_NUMBER.sub("", stem, count=1).strip()
    return title or stem.strip() or None


def quick_hash(path: Path, file_size: int) -> str:
    """ファイルサイズと先頭・末尾の内容から、重複検出用のハッシュを求める。"""
    digest = hashlib.sha256(str(file_size).encode())
    with path.open("rb") as f:
        digest.update(f.read(_HASH_CHUNK_BYTES))
        if file_size > _HASH_CHUNK_BYTES * 2:
            f.seek(-_HASH_CHUNK_BYTES, 2)
            digest.update(f.read(_HASH_CHUNK_BYTES))

    return digest.hexdigest()


def read_metadata(path: Path, rel_path: str) -> TagReadResult:
    """楽曲ファイルのタグとファイル情報を読む。

    Args:
        path: ファイルの絶対パス。
        rel_path: 音楽ライブラリのルートからの相対パス。

    Returns:
        読み取り結果。タグが壊れていても例外にせず、`error` に理由を入れて返す。

    Raises:
        OSError: ファイル自体を読めない場合。
    """
    stat = path.stat()
    file_hash = quick_hash(path, stat.st_size)
    fallback_title = title_from_filename(rel_path)

    error: str | None = None
    try:
        audio = mutagen.File(path, easy=True)
    except mutagen.MutagenError as exc:
        error = f"{type(exc).__name__}: failed to read tags"
        audio = None
    else:
        if audio is None:
            error = "unsupported or unrecognized audio format"

    tags = _normalized_tags(audio)
    title, repaired_title = _repaired(tags.get("title"))
    artist, repaired_artist = _repaired(tags.get("artist"))
    album, repaired_album = _repaired(tags.get("album"))
    genre, repaired_genre = _repaired(tags.get("genre"))
    info = getattr(audio, "info", None)

    metadata = TrackMetadata(
        rel_path=rel_path,
        title=title or fallback_title,
        artist=artist,
        album=album,
        genre=genre,
        year=_parse_year(tags.get("date") or tags.get("year")),
        track_number=_parse_track_number(tags.get("tracknumber")),
        duration_sec=_positive_float(getattr(info, "length", None)),
        bitrate=_positive_int(getattr(info, "bitrate", None)),
        sample_rate=_positive_int(getattr(info, "sample_rate", None)),
        channels=_positive_int(getattr(info, "channels", None)),
        # easy=True で読むと EasyMP3 などのラッパーになるため、接頭辞を除いて形式名にする
        codec=None if audio is None else type(audio).__name__.lower().removeprefix("easy"),
        file_size=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        title_source=TitleSource.TAG if title else (TitleSource.FILENAME if fallback_title else None),
        tags_repaired=repaired_title or repaired_artist or repaired_album or repaired_genre,
        quick_hash=file_hash,
    )
    return TagReadResult(metadata=metadata, error=error)


def _normalized_tags(audio: mutagen.FileType | None) -> dict[str, str]:
    """タグを小文字のキーと、空でない最初の値の辞書にする。"""
    tags = getattr(audio, "tags", None)
    if tags is None:
        return {}

    result: dict[str, str] = {}
    for key, value in tags.items():
        values = value if isinstance(value, list) else [value]
        texts = [str(v).strip() for v in values if str(v).strip()]
        if texts:
            result[str(key).lower()] = unicodedata.normalize("NFC", texts[0])

    return result


def _repaired(value: str | None) -> tuple[str | None, bool]:
    if value is None:
        return None, False

    return repair_mojibake(value)


def _parse_year(value: str | None) -> int | None:
    if value is None:
        return None

    match = _YEAR.search(value)
    return None if match is None else int(match.group(1))


def _parse_track_number(value: str | None) -> int | None:
    if value is None:
        return None

    match = _TRACK_NUMBER.match(value)
    return None if match is None else int(match.group(1))


def _positive_float(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) and value > 0 else None


def _positive_int(value: object) -> int | None:
    return int(value) if isinstance(value, int | float) and value > 0 else None
