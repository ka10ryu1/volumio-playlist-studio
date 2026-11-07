from pathlib import Path

import pytest
from conftest import AudioWriter, sine
from hypothesis import given
from hypothesis import strategies as st

from volumio_playlist_studio.models import TitleSource
from volumio_playlist_studio.tags import quick_hash, read_metadata, repair_mojibake, title_from_filename

# MPEG-1 Layer III、128 kbps、44.1 kHz、モノラルのフレームヘッダ。フレーム長は 417 バイト
_MP3_FRAME = bytes([0xFF, 0xFB, 0x90, 0xC4]) + bytes(413)


def _id3_text_frame(frame_id: str, raw_text: bytes) -> bytes:
    """ID3v2.3 のテキストフレーム。文字コードの指定（先頭 1 バイト）は 0（Latin-1）にする。"""
    body = b"\x00" + raw_text
    return frame_id.encode("ascii") + len(body).to_bytes(4, "big") + b"\x00\x00" + body


def _write_mp3_with_latin1_tags(path: Path, title: str, artist: str, encoding: str) -> None:
    """ID3 の文字コードを Latin-1 と宣言したまま、別の文字コードのバイト列を保存した MP3 を作る。

    古い MP3 タグ編集ソフトが Shift_JIS のバイト列をそのまま書き込んだ状態を再現する。
    """
    frames = _id3_text_frame("TIT2", title.encode(encoding)) + _id3_text_frame("TPE1", artist.encode(encoding))
    size = len(frames)
    # ID3v2 のヘッダのサイズは 7 ビットずつに分けた syncsafe 整数で表す
    syncsafe = bytes([(size >> 21) & 0x7F, (size >> 14) & 0x7F, (size >> 7) & 0x7F, size & 0x7F])
    path.write_bytes(b"ID3\x03\x00\x00" + syncsafe + frames + _MP3_FRAME * 40)


@pytest.mark.parametrize(
    ("original", "encoding"),
    [
        ("夜明けのテスト曲", "cp932"),
        ("ｶﾀｶﾅのテスト", "cp932"),
        ("日本語のタイトル", "utf-8"),
    ],
)
def test_repair_mojibake_restores_japanese(original: str, encoding: str) -> None:
    garbled = original.encode(encoding).decode("latin-1")

    assert repair_mojibake(garbled) == (original, True)


@pytest.mark.parametrize("text", ["Plain ASCII Title", "Café del Mar", "Ñandú", "正しい日本語", ""])
def test_repair_mojibake_keeps_normal_text(text: str) -> None:
    assert repair_mojibake(text) == (text, False)


@given(st.text())
def test_repair_mojibake_never_changes_text_with_non_latin1_chars(text: str) -> None:
    repaired, changed = repair_mojibake(text + "あ")

    assert repaired == text + "あ"
    assert not changed


@pytest.mark.parametrize(
    ("rel_path", "expected"),
    [
        ("artist/album/01 - 曲名.flac", "曲名"),
        ("01. Song Title.mp3", "Song Title"),
        ("1-03 Disc Song.flac", "Disc Song"),
        ("07_track.flac", "track"),
        ("曲名だけ.m4a", "曲名だけ"),
        ("2001 Space Song.mp3", "2001 Space Song"),
        ("05.flac", "05"),
    ],
)
def test_title_from_filename(rel_path: str, expected: str) -> None:
    assert title_from_filename(rel_path) == expected


def test_read_metadata_with_tags(tmp_path: Path, write_audio: AudioWriter) -> None:
    path = write_audio(
        tmp_path / "アーティスト" / "01 曲.flac",
        sine(3.0),
        {
            "title": "テスト曲",
            "artist": "テストアーティスト",
            "album": "テストアルバム",
            "genre": "ジャズ",
            "date": "2003-05-01",
            "tracknumber": "3/12",
        },
    )

    result = read_metadata(path, "アーティスト/01 曲.flac")

    meta = result.metadata
    assert result.error is None
    assert (meta.title, meta.artist, meta.album, meta.genre) == (
        "テスト曲",
        "テストアーティスト",
        "テストアルバム",
        "ジャズ",
    )
    assert (meta.year, meta.track_number) == (2003, 3)
    assert meta.title_source == TitleSource.TAG
    assert meta.duration_sec == pytest.approx(3.0, abs=0.01)
    assert meta.sample_rate == 22050
    assert meta.channels == 1
    assert meta.codec == "flac"
    assert meta.file_size == path.stat().st_size
    assert meta.rel_path == "アーティスト/01 曲.flac"


def test_read_metadata_without_tags_uses_filename_and_leaves_unknowns_empty(
    tmp_path: Path, write_audio: AudioWriter
) -> None:
    path = write_audio(tmp_path / "02 - 名無しの曲.flac", sine(2.0))

    meta = read_metadata(path, "02 - 名無しの曲.flac").metadata

    assert meta.title == "名無しの曲"
    assert meta.title_source == TitleSource.FILENAME
    # ファイル名からアーティストやジャンルを推測しない
    assert (meta.artist, meta.album, meta.genre, meta.year, meta.track_number) == (None, None, None, None, None)


def test_read_metadata_repairs_garbled_id3_tags(tmp_path: Path) -> None:
    path = tmp_path / "garbled.mp3"
    _write_mp3_with_latin1_tags(path, "古い日本語の曲", "昔のアーティスト", "cp932")

    result = read_metadata(path, "garbled.mp3")

    assert result.error is None
    assert result.metadata.title == "古い日本語の曲"
    assert result.metadata.artist == "昔のアーティスト"
    assert result.metadata.tags_repaired
    assert result.metadata.codec == "mp3"
    assert result.metadata.duration_sec is not None


@pytest.mark.parametrize("name", ["broken.mp3", "broken.flac", "broken.m4a"])
def test_read_metadata_of_corrupted_file_returns_error_without_raising(tmp_path: Path, name: str) -> None:
    path = tmp_path / name
    path.write_bytes(b"this is not audio data" * 20)

    result = read_metadata(path, name)

    assert result.error is not None
    assert result.metadata.title == "broken"
    assert result.metadata.title_source == TitleSource.FILENAME
    assert result.metadata.duration_sec is None
    # 例外メッセージなどに含まれうる絶対パスを記録しない
    assert str(tmp_path) not in result.error


def test_read_metadata_of_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        read_metadata(tmp_path / "missing.flac", "missing.flac")


def test_quick_hash_is_equal_for_identical_content_and_differs_otherwise(tmp_path: Path) -> None:
    large = bytes(range(256)) * 1024  # 先頭・末尾を読む方式で、末尾も読まれる大きさ
    a = tmp_path / "a.bin"
    b = tmp_path / "b.bin"
    c = tmp_path / "c.bin"
    a.write_bytes(large)
    b.write_bytes(large)
    c.write_bytes(large[:-1] + b"\x00")

    assert quick_hash(a, a.stat().st_size) == quick_hash(b, b.stat().st_size)
    assert quick_hash(a, a.stat().st_size) != quick_hash(c, c.stat().st_size)
