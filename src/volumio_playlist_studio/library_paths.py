import os
import platform
import re
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


class LibraryPathError(ValueError):
    """音楽フォルダとして指定されたパスが使えない場合の例外。

    Attributes:
        user_message: 画面に表示する日本語の説明。
    """

    def __init__(self, reason: str, user_message: str) -> None:
        super().__init__(reason)
        self.user_message = user_message


_WINDOWS_DRIVE = re.compile(r"^(?P<drive>[A-Za-z]):[\\/](?P<rest>.*)$")


def is_wsl() -> bool:
    """WSL 上で動作しているかを返す。"""
    return "microsoft" in platform.uname().release.lower()


def resolve_library_input(text: str, *, wsl: bool) -> Path:
    r"""GUI で入力された音楽フォルダのパスを、この環境で開けるパスに変換する。

    WSL 上では `C:\Music` のような Windows のドライブパスを `/mnt/c/Music` に変換する。
    UNC パス（`\\volumio\share`）は WSL から直接開けないため、マウント手順を案内する例外を送出する。
    ファイルシステムには触れず、存在確認は呼び出し側で行う。

    Args:
        text: 入力されたパス。前後の空白と引用符は無視する。
        wsl: WSL 上で動作しているか。

    Returns:
        絶対パス。

    Raises:
        LibraryPathError: 空、相対パス、UNC パス、または WSL 以外での Windows パスの場合。
    """
    cleaned = text.strip().strip("\"'").strip()
    if not cleaned:
        raise LibraryPathError("empty path", "音楽フォルダのパスを入力してください。")

    if cleaned.startswith(("\\\\", "//")):
        raise LibraryPathError(
            "unc path is not supported",
            "UNC パス（\\\\サーバー名\\共有名）は直接開けません。SMB 共有を WSL にマウントしてから、"
            "マウント先（例: /mnt/volumio）を指定してください。手順は README を参照してください。",
        )

    drive_match = _WINDOWS_DRIVE.match(cleaned)
    if drive_match is not None:
        if not wsl:
            raise LibraryPathError("windows path outside wsl", "Windows 形式のパスは WSL 上でのみ指定できます。")

        rest = drive_match.group("rest").replace("\\", "/")
        return Path("/mnt", drive_match.group("drive").lower(), *[p for p in rest.split("/") if p])

    path = Path(cleaned).expanduser()
    if not path.is_absolute():
        raise LibraryPathError("relative path", "音楽フォルダは絶対パスで指定してください。")

    return path


def redact_paths(message: str, roots: list[Path]) -> str:
    """ログや画面に出すメッセージから、ライブラリのルートとホームディレクトリの絶対パスを取り除く。

    外部ライブラリの例外メッセージには絶対パスが含まれることがあるため、記録する前にこの関数を通す。

    Args:
        message: 元のメッセージ。
        roots: 取り除くルートのパス。

    Returns:
        ルートを `<library>`、ホームディレクトリを `~` に置き換えたメッセージ。
    """
    result = message
    # 長いパスから置き換え、ルートの一部だけが置き換わるのを防ぐ
    for root in sorted({str(r) for r in roots}, key=len, reverse=True):
        if root and root != "/":
            result = result.replace(root, "<library>")

    home = str(Path.home())
    if home and home != "/":
        result = result.replace(home, "~")

    return result
