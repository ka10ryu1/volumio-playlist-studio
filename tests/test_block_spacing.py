"""インデントが戻る箇所の空行ルール（CLAUDE.md）と、その整形ツールのテスト。"""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "insert_block_blank_lines", ROOT / "tools" / "insert_block_blank_lines.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load tools/insert_block_blank_lines.py")

    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


tool = _load_tool()


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        # ブロックの後に空行を入れる
        ("for x in y:\n    f(x)\ng()\n", "for x in y:\n    f(x)\n\ng()\n"),
        # ネストの中や、ガード節の後にも入れる
        (
            "def f(x):\n    if x:\n        return 1\n    return 2\n",
            "def f(x):\n    if x:\n        return 1\n\n    return 2\n",
        ),
        # else / elif / except / finally / case の前には入れない
        ("if a:\n    b()\nelif c:\n    d()\nelse:\n    e()\n", None),
        ("try:\n    a()\nexcept E:\n    b()\nfinally:\n    c()\n", None),
        ("match x:\n    case 1:\n        a()\n    case _:\n        b()\n", None),
        # 既に空行がある場合は変えない
        ("if a:\n    b()\n\nc()\n", None),
        # 直前のコメントは次の行の説明として、コメントの前に空行を入れる
        ("if a:\n    b()\n# 説明\nc()\n", "if a:\n    b()\n\n# 説明\nc()\n"),
    ],
)
def test_insert_block_blank_lines(source: str, expected: str | None) -> None:
    assert tool.insert_block_blank_lines(source) == (source if expected is None else expected)


def test_check_mode_reports_without_writing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target = tmp_path / "sample.py"
    target.write_text("if a:\n    b()\nc()\n", encoding="utf-8")

    assert tool.main(["--check", str(tmp_path)]) == 1
    assert target.read_text(encoding="utf-8") == "if a:\n    b()\nc()\n"
    assert tool.main([str(target)]) == 0
    assert target.read_text(encoding="utf-8") == "if a:\n    b()\n\nc()\n"
    assert "would reformat" in capsys.readouterr().out


def test_repository_follows_block_spacing_rule() -> None:
    files = tool.iter_python_files([str(ROOT / "src"), str(ROOT / "tests"), str(ROOT / "tools")])

    violations = [
        p.relative_to(ROOT).as_posix()
        for p in files
        if tool.insert_block_blank_lines(p.read_text("utf-8")) != p.read_text("utf-8")
    ]

    assert files
    assert violations == [], "uv run python tools/insert_block_blank_lines.py src tests tools で整形してください"
