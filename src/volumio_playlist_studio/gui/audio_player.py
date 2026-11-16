"""曲をブラウザで再生するプレーヤー。

音楽ライブラリが NAS などのネットワーク上にあると、曲のファイル（1 曲で数十 MB）を読むのに時間がかかる。
画面の操作を止めないよう、ファイルはバックグラウンドのスレッドで読み、読み終わるまでは読み込み中と表示する。
音楽ファイルは読むだけで変更しない。
"""

import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import streamlit as st

from volumio_playlist_studio.config import AUDIO_MIME_TYPES
from volumio_playlist_studio.models import Track

logger = logging.getLogger(__name__)

# 読み込み中の曲、または読み込んだ曲。セッションごとに 1 曲分だけ持つ
_STATE_KEY = "track_player_load"
# 読み込み中に、読み終わったかを確かめる間隔（秒）
_POLL_SECONDS = 0.5


@dataclass
class _AudioLoad:
    """バックグラウンドでの曲のファイルの読み込み。

    Attributes:
        key: 読み込む曲を識別する値（ライブラリのルート、相対パス、ファイルの更新日時）。
        data: 読み込んだデータ。読み込み中または失敗した場合は None。
        error: 失敗した場合に画面に出す理由。
        done: 読み込みが終わった（成功・失敗を問わない）ことを表す。
    """

    key: tuple[str, str, int]
    data: bytes | None = None
    error: str | None = None
    done: threading.Event = field(default_factory=threading.Event)


def track_player(library_root: str, track: Track) -> None:
    """曲をブラウザで再生するプレーヤーを表示する。

    同じ曲を表示している間は読み込んだデータを使い回し、画面を再実行するたびに読み直さない。
    """
    mimetype = AUDIO_MIME_TYPES.get(PurePosixPath(track.rel_path).suffix.lower())
    if mimetype is None:
        st.caption("この形式の曲は再生できません。")
        return

    # DB に記録した値だけで決め、画面のスレッドではファイルにアクセスしない
    key = (library_root, track.rel_path, track.mtime_ns)
    load = st.session_state.get(_STATE_KEY)
    if not isinstance(load, _AudioLoad) or load.key != key:
        load = _AudioLoad(key)
        st.session_state[_STATE_KEY] = load
        path = Path(library_root) / track.rel_path
        thread = threading.Thread(target=_read, args=(load, path, track.rel_path), name="track-player", daemon=True)
        thread.start()

    loading = not load.done.is_set()
    # 読み込み中だけ、この領域を定期的に更新する。読み終わった後も更新し続けると、再生中のプレーヤーを描き直すため
    st.fragment(_player_body, run_every=_POLL_SECONDS if loading else None)(load, mimetype, loading)


def _player_body(load: _AudioLoad, mimetype: str, polling: bool) -> None:
    if not load.done.is_set():
        st.caption("曲を読み込んでいます…")
        return

    if polling:
        # 定期的な更新を止めるため、画面全体を再実行してこの領域を作り直す
        st.rerun()

    if load.data is None:
        st.warning(load.error or "曲を読み込めないため再生できません。")
        return

    st.audio(load.data, format=mimetype)
    st.caption("ブラウザが対応していない形式（M4A の ALAC など）は再生できないことがあります。")


def _read(load: _AudioLoad, path: Path, rel_path: str) -> None:
    """曲のファイルを読む。バックグラウンドのスレッドの入口のため、失敗はここで記録して画面に伝える。"""
    try:
        if path.is_file():
            load.data = path.read_bytes()
        else:
            load.error = "曲のファイルが見つからないため再生できません。"
    except OSError as exc:
        # 例外のメッセージには絶対パスが含まれるため、種類だけを記録する
        logger.warning("read audio for playback failed: rel_path=%s error=%s", rel_path, type(exc).__name__)
        load.error = "曲のファイルを読めないため再生できません。"
    finally:
        load.done.set()
