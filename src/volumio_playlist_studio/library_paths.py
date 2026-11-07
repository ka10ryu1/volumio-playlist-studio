import os
from pathlib import Path


def to_library_relative_path(path: Path, library_root: Path) -> str:
    """音楽ライブラリのルートからの相対パスを、ログ出力用の文字列として返す。

    ログにユーザー名などを含む絶対パスを出さないために使う。
    ファイルシステムには触れず、パスの文字列だけで判定する。

    Args:
        path: 対象ファイルの絶対パス。
        library_root: 音楽ライブラリのルートの絶対パス。

    Returns:
        `/` 区切りの相対パス。`path` がルート自身の場合は `.`。

    Raises:
        ValueError: いずれかが相対パスの場合、または `path` がルートの外にある場合。
    """
    if not path.is_absolute() or not library_root.is_absolute():
        # 例外メッセージはログに残りうるため、パス自体は含めない
        raise ValueError("path and library_root must be absolute")

    # `..` を含むパスがルートの外を指していても通過しないよう、先に正規化する
    normalized_path = Path(os.path.normpath(path))
    normalized_root = Path(os.path.normpath(library_root))

    if not normalized_path.is_relative_to(normalized_root):
        raise ValueError("path is outside of library_root")

    return normalized_path.relative_to(normalized_root).as_posix()
