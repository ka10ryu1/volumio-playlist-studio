from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from volumio_playlist_studio.library_paths import to_library_relative_path

LIBRARY_ROOT = Path("/home/user/Music")

# パスの 1 階層分の名前。区切り文字・NUL・カレント／親ディレクトリを表す名前は除く
path_segments = st.text(
    alphabet=st.characters(exclude_characters="/\x00", exclude_categories=["Cs"]),
    min_size=1,
).filter(lambda s: s not in {".", ".."})


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (Path("/home/user/Music/artist/album/01.flac"), "artist/album/01.flac"),
        (Path("/home/user/Music/01.flac"), "01.flac"),
        (Path("/home/user/Music"), "."),
        (Path("/home/user/Music/artist/./album//01.flac"), "artist/album/01.flac"),
        (Path("/home/user/Music/artist/../other/01.flac"), "other/01.flac"),
        (Path("/home/user/Music/アーティスト/曲 名.mp3"), "アーティスト/曲 名.mp3"),
    ],
)
def test_returns_path_relative_to_library_root(path: Path, expected: str) -> None:
    assert to_library_relative_path(path, LIBRARY_ROOT) == expected


def test_accepts_library_root_with_trailing_slash() -> None:
    assert to_library_relative_path(Path("/home/user/Music/01.flac"), Path("/home/user/Music/")) == "01.flac"


@pytest.mark.parametrize(
    "path",
    [
        Path("/home/user/Documents/memo.txt"),
        # 文字列の前方一致ではなく、階層単位で判定すること
        Path("/home/user/Music2/01.flac"),
        # `..` でルートの外に出るパス
        Path("/home/user/Music/../Documents/memo.txt"),
        Path("/home/user/Music/artist/../../Documents/memo.txt"),
        Path("/home/user"),
    ],
)
def test_rejects_path_outside_library_root(path: Path) -> None:
    with pytest.raises(ValueError, match="outside of library_root"):
        to_library_relative_path(path, LIBRARY_ROOT)


@pytest.mark.parametrize(
    ("path", "library_root"),
    [
        (Path("artist/01.flac"), LIBRARY_ROOT),
        (Path("/home/user/Music/01.flac"), Path("Music")),
    ],
)
def test_rejects_relative_paths(path: Path, library_root: Path) -> None:
    with pytest.raises(ValueError, match="must be absolute"):
        to_library_relative_path(path, library_root)


def test_error_message_does_not_contain_path() -> None:
    with pytest.raises(ValueError, match="outside of library_root") as exc_info:
        to_library_relative_path(Path("/home/secret-user/Documents/memo.txt"), LIBRARY_ROOT)

    message = str(exc_info.value)
    assert "secret-user" not in message
    assert "/home/user/Music" not in message


@given(segments=st.lists(path_segments, min_size=1, max_size=5))
def test_joined_segments_are_returned_as_relative_path(segments: list[str]) -> None:
    path = LIBRARY_ROOT.joinpath(*segments)

    assert to_library_relative_path(path, LIBRARY_ROOT) == "/".join(segments)


def _resolve_segments(segments: list[str]) -> list[str] | None:
    """`.` と `..` を 1 階層ずつ辿り、ルートからの階層を返す。ルートの外に出たら None を返す。

    ルートの名前（Music）は候補に含めないため、一度外に出たらルートの中には戻らない。
    """
    stack: list[str] = []
    for segment in segments:
        if segment == "..":
            if not stack:
                return None
            stack.pop()
        elif segment != ".":
            stack.append(segment)
    return stack


@given(segments=st.lists(st.sampled_from(["a", "b", ".", ".."]), max_size=8))
def test_dot_segments_are_resolved_or_rejected(segments: list[str]) -> None:
    path = LIBRARY_ROOT.joinpath(*segments)
    expected = _resolve_segments(segments)

    if expected is None:
        with pytest.raises(ValueError, match="outside of library_root"):
            to_library_relative_path(path, LIBRARY_ROOT)
    else:
        assert to_library_relative_path(path, LIBRARY_ROOT) == ("/".join(expected) or ".")
