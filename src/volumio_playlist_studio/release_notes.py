"""リリースノート（docs/release-notes.md）の読み込み。

リリースノートは、利用者から見える変更を PR ごとに 1 項目ずつ Markdown で書く。各項目は次の形の見出しで始め、
次の見出しまでを詳しい説明とする。最初の見出しより前（ファイルの題名や書き方の説明）は項目に含めない。

    ## 2026-11-25 #60 曲の編集タブの下に「保存する」ボタンを追加

    詳しい説明（Markdown）。

日付は PR のマージ日（YYYY-MM-DD）。説明の中で見出しを使う場合は `###` 以下にする。
"""

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

_ENTRY_PREFIX = "## "
_HEADING = re.compile(r"## (?P<date>\d{4}-\d{2}-\d{2}) #(?P<number>[1-9]\d*) (?P<title>\S.*)")


class ReleaseNotesError(ValueError):
    """リリースノートの書式が正しくない。"""


@dataclass(frozen=True)
class ReleaseNote:
    """リリースノートの 1 項目（1 つの PR）。"""

    merged_on: date
    pr_number: int
    title: str
    body: str


def load_release_notes(path: Path) -> list[ReleaseNote]:
    """リリースノートのファイルを読み、新しい順に並べた項目を返す。

    Raises:
        OSError: ファイルを読めない場合。
        ReleaseNotesError: 書式が正しくない場合。
    """
    return parse_release_notes(path.read_text(encoding="utf-8"))


def parse_release_notes(text: str) -> list[ReleaseNote]:
    """リリースノートの Markdown を項目に分け、マージ日・PR 番号の新しい順に並べて返す。

    Raises:
        ReleaseNotesError: 見出しの形式や日付が正しくない、説明が空、または同じ PR の項目が複数ある場合。
    """
    notes: list[ReleaseNote] = []
    heading: re.Match[str] | None = None
    heading_line = 0
    body: list[str] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.startswith(_ENTRY_PREFIX):
            body.append(line)
            continue

        if heading is not None:
            notes.append(_build_note(heading, heading_line, body))

        heading = _HEADING.fullmatch(line.rstrip())
        if heading is None:
            raise ReleaseNotesError(f"line {line_number}: heading must be '## YYYY-MM-DD #<PR number> <title>'")

        heading_line = line_number
        body = []

    if heading is not None:
        notes.append(_build_note(heading, heading_line, body))

    _check_unique_pr_numbers(notes)
    return sorted(notes, key=lambda note: (note.merged_on, note.pr_number), reverse=True)


def _build_note(heading: re.Match[str], line_number: int, body: list[str]) -> ReleaseNote:
    try:
        merged_on = date.fromisoformat(heading["date"])
    except ValueError as exc:
        raise ReleaseNotesError(f"line {line_number}: invalid date") from exc

    text = "\n".join(body).strip()
    if not text:
        raise ReleaseNotesError(f"line {line_number}: description is empty")

    return ReleaseNote(merged_on=merged_on, pr_number=int(heading["number"]), title=heading["title"], body=text)


def _check_unique_pr_numbers(notes: list[ReleaseNote]) -> None:
    seen: set[int] = set()
    for note in notes:
        if note.pr_number in seen:
            raise ReleaseNotesError(f"duplicate entry for PR #{note.pr_number}")

        seen.add(note.pr_number)
