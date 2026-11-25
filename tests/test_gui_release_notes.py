"""リリースノート画面のテスト。"""

from pathlib import Path

import pytest
from test_gui import run_view

from volumio_playlist_studio.gui.views import release_notes_page

NOTES = """# リリースノート

## 2026-11-08 #2 最初のバージョン

最初の説明。

## 2026-11-25 #56 リリースノート画面を追加

画面の説明。
"""


@pytest.fixture
def notes_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """画面が読むリリースノートを、テスト用のファイルに差し替える。"""
    path = tmp_path / "release-notes.md"
    monkeypatch.setattr(release_notes_page, "RELEASE_NOTES_PATH", path)
    return path


def test_release_notes_view_lists_notes_newest_first(notes_path: Path) -> None:
    notes_path.write_text(NOTES, encoding="utf-8")

    at = run_view("release_notes_page")

    assert [t.value for t in at.title] == ["リリースノート"]
    assert [e.label for e in at.expander] == [
        "2026-11-25　#56　リリースノート画面を追加",
        "2026-11-08　#2　最初のバージョン",
    ]
    # 説明は折りたたみの中に表示し、最初は閉じておく
    assert [e.proto.expanded for e in at.expander] == [False, False]
    newest = [m.value for m in at.expander[0].markdown]
    assert newest == [
        "画面の説明。",
        "[PR #56 を GitHub で見る](https://github.com/ka10ryu1/volumio-playlist-studio/pull/56)",
    ]
    assert not at.error


def test_release_notes_view_shows_message_when_empty(notes_path: Path) -> None:
    notes_path.write_text("# リリースノート\n", encoding="utf-8")

    at = run_view("release_notes_page")

    assert [i.value for i in at.info] == ["リリースノートはまだありません。"]
    assert not at.expander


@pytest.mark.parametrize(
    "content",
    [
        "## 2026-11-25 リリースノート画面を追加\n\n説明。\n",  # PR 番号がない
        None,  # ファイルがない
    ],
)
def test_release_notes_view_shows_error_when_notes_cannot_be_read(notes_path: Path, content: str | None) -> None:
    if content is not None:
        notes_path.write_text(content, encoding="utf-8")

    at = run_view("release_notes_page")

    assert [e.value for e in at.error] == [
        "リリースノートを読み込めませんでした。"
        "アプリを最新の状態に更新しても表示されない場合は、問い合わせ（GitHub）から知らせてください。"
    ]
    assert not at.expander


def test_release_notes_view_shows_repository_notes() -> None:
    at = run_view("release_notes_page")

    assert any(e.label.endswith("　リリースノート画面を追加") for e in at.expander)
    assert not at.error
