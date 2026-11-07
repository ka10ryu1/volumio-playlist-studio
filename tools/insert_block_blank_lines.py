"""ブロックの終了後（インデントが戻る箇所）に空行を挿入する整形ツール。

CLAUDE.md の「インデントが戻る場所には空行を入れる」ルールを機械的に適用する。
else / elif / except / finally / case など、同じ構文の続きを表すキーワードの前には入れない。

    uv run python tools/insert_block_blank_lines.py src tests tools
    uv run python tools/insert_block_blank_lines.py --check src tests tools
"""

import argparse
import io
import sys
import tokenize
from pathlib import Path

CONTINUATION_KEYWORDS = ("else", "elif", "except", "finally", "case")


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def insert_block_blank_lines(source: str, continuation_keywords: tuple[str, ...] = CONTINUATION_KEYWORDS) -> str:
    """ブロックの終了後の行の直前に空行を挿入したソースを返す。

    Args:
        source: Python のソースコード。
        continuation_keywords: 直前に空行を入れないキーワード。

    Returns:
        整形後のソース。変更がなければ入力と同じ文字列。
    """
    lines = source.splitlines(keepends=True)
    insert_before: set[int] = set()
    skip_types = {tokenize.NL, tokenize.NEWLINE, tokenize.COMMENT, tokenize.INDENT, tokenize.ENDMARKER}
    pending_dedent = False
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.DEDENT:
            pending_dedent = True
            continue

        if token.type in skip_types or not pending_dedent:
            continue

        pending_dedent = False
        if token.string in continuation_keywords:
            continue

        target = token.start[0] - 1
        indent = _indent_of(lines[target])
        # 直前の同じインデントのコメント行は、次の行の説明としてまとめて扱う
        while target > 0 and lines[target - 1].strip().startswith("#") and _indent_of(lines[target - 1]) == indent:
            target -= 1

        if target > 0 and lines[target - 1].strip():
            insert_before.add(target)

    if not insert_before:
        return source

    output: list[str] = []
    for index, line in enumerate(lines):
        if index in insert_before:
            output.append("\n")

        output.append(line)

    return "".join(output)


def iter_python_files(paths: list[str]) -> list[Path]:
    """引数のパス（ファイルまたはフォルダ）から .py ファイルを列挙する。仮想環境のフォルダは除く。"""
    files: list[Path] = []
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            files.extend(sorted(p for p in path.rglob("*.py") if not any(part.startswith(".venv") for part in p.parts)))
        elif path.suffix == ".py":
            files.append(path)

    return files


def main(argv: list[str] | None = None) -> int:
    """コマンドラインから実行する。

    Returns:
        終了コード。--check で整形が必要なファイルがあれば 1。
    """
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="+")
    parser.add_argument("--check", action="store_true", help="書き換えずに、整形が必要なファイルがあるかだけ確認する")
    args = parser.parse_args(argv)

    changed: list[Path] = []
    for path in iter_python_files(args.paths):
        original = path.read_text(encoding="utf-8")
        formatted = insert_block_blank_lines(original)
        if formatted == original:
            continue

        changed.append(path)
        if not args.check:
            path.write_text(formatted, encoding="utf-8")

    for path in changed:
        sys.stdout.write(f"{'would reformat' if args.check else 'reformatted'}: {path}\n")

    return 1 if args.check and changed else 0


if __name__ == "__main__":
    raise SystemExit(main())
