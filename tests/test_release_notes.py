"""リリースノートの読み込みのテスト。"""

from datetime import date
from pathlib import Path

import pytest

from volumio_playlist_studio.config import RELEASE_NOTES_PATH
from volumio_playlist_studio.release_notes import (
    ReleaseNote,
    ReleaseNotesError,
    load_release_notes,
    parse_release_notes,
)

TEXT = """# リリースノート

書き方の説明。項目には含めない。

## 2026-11-08 #2 最初のバージョン

最初の説明。

- 箇条書き

### 補足

補足の説明。

## 2026-11-25 #60 「保存する」ボタンを追加

ボタンの説明。

## 2026-11-25 #56 リリースノート画面を追加
リリースノートの説明。
"""


def test_parse_returns_notes_newest_first() -> None:
    notes = parse_release_notes(TEXT)

    assert notes == [
        ReleaseNote(date(2026, 11, 25), 60, "「保存する」ボタンを追加", "ボタンの説明。"),
        ReleaseNote(date(2026, 11, 25), 56, "リリースノート画面を追加", "リリースノートの説明。"),
        ReleaseNote(date(2026, 11, 8), 2, "最初のバージョン", "最初の説明。\n\n- 箇条書き\n\n### 補足\n\n補足の説明。"),
    ]


@pytest.mark.parametrize("text", ["", "# リリースノート\n\n項目はまだない。\n"])
def test_parse_returns_empty_list_without_entries(text: str) -> None:
    assert parse_release_notes(text) == []


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("## 2026-11-25 リリースノート\n\n説明。\n", "line 1: heading"),  # PR 番号がない
        ("# 題名\n\n## #56 リリースノート\n\n説明。\n", "line 3: heading"),  # 日付がない
        ("## 2026/11/25 #56 リリースノート\n\n説明。\n", "line 1: heading"),
        ("## 2026-11-25 #56\n\n説明。\n", "line 1: heading"),  # 見出しがない
        ("## 2026-11-25 #0 リリースノート\n\n説明。\n", "line 1: heading"),
        ("## 2026-13-01 #56 リリースノート\n\n説明。\n", "line 1: invalid date"),
        ("## 2026-11-25 #56 リリースノート\n\n## 2026-11-26 #57 次\n\n説明。\n", "line 1: description is empty"),
        ("## 2026-11-25 #56 リリースノート\n   \n", "line 1: description is empty"),
        ("## 2026-11-25 #56 A\n\n説明。\n\n## 2026-11-26 #56 B\n\n説明。\n", "duplicate entry for PR #56"),
    ],
)
def test_parse_rejects_malformed_notes(text: str, message: str) -> None:
    with pytest.raises(ReleaseNotesError, match=message):
        parse_release_notes(text)


def test_load_reads_utf8_file(tmp_path: Path) -> None:
    path = tmp_path / "release-notes.md"
    path.write_text("## 2026-11-25 #56 リリースノート画面を追加\n\n説明。\n", encoding="utf-8")

    assert load_release_notes(path) == [ReleaseNote(date(2026, 11, 25), 56, "リリースノート画面を追加", "説明。")]


def test_load_fails_when_file_is_missing(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_release_notes(tmp_path / "missing.md")


def test_repository_release_notes_are_valid() -> None:
    """リポジトリのリリースノートが読み込め、この画面を追加した PR の項目を含む。"""
    notes = load_release_notes(RELEASE_NOTES_PATH)

    assert "リリースノート画面を追加" in [note.title for note in notes]


def test_repository_release_notes_are_written_newest_first() -> None:
    """ファイルでの項目の並びが、画面に表示する新しい順と一致する（新しい項目を先頭に追加している）。"""
    text = RELEASE_NOTES_PATH.read_text(encoding="utf-8")
    written = [int(line.split()[2].removeprefix("#")) for line in text.splitlines() if line.startswith("## ")]

    assert written == [note.pr_number for note in load_release_notes(RELEASE_NOTES_PATH)]
