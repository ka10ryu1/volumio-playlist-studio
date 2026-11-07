"""時間のかかる処理を GUI スレッドの外で実行するための仕組み。

Streamlit のスクリプトは操作のたびに再実行されるため、解析などの処理はバックグラウンドスレッドで
実行し、進捗をこのモジュールのオブジェクト経由で画面に伝える。
"""

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

logger = logging.getLogger(__name__)


@dataclass
class JobControl:
    """処理の進捗通知と中断の受け付け。

    Attributes:
        progress: （処理済み数, 総数）を受け取るコールバック。
        cancel: セットされたら処理を中断する。
    """

    progress: Callable[[int, int], None] | None = None
    cancel: threading.Event | None = None

    def report(self, done: int, total: int) -> None:
        """進捗を通知する。"""
        if self.progress is not None:
            self.progress(done, total)

    def is_cancelled(self) -> bool:
        """中断が要求されたかどうか。"""
        return self.cancel is not None and self.cancel.is_set()


class JobState(StrEnum):
    """バックグラウンド処理の状態。"""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass
class BackgroundJob:
    """バックグラウンドスレッドで実行する処理 1 件。

    Attributes:
        name: 画面に表示する処理名。
        result: 正常終了時の戻り値。
        error: 異常終了時の、画面に表示する日本語のメッセージ。
    """

    name: str
    state: JobState = JobState.RUNNING
    done: int = 0
    total: int = 0
    result: object = None
    error: str | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def control(self) -> JobControl:
        """処理に渡す進捗通知・中断の受け付け。"""
        return JobControl(progress=self._on_progress, cancel=self.cancel_event)

    @property
    def fraction(self) -> float:
        """進捗の割合（0〜1）。総数が不明なら 0。"""
        with self._lock:
            return 0.0 if self.total <= 0 else min(1.0, self.done / self.total)

    def _on_progress(self, done: int, total: int) -> None:
        with self._lock:
            self.done = done
            self.total = total

    def start(self, target: Callable[[JobControl], object]) -> None:
        """処理をバックグラウンドスレッドで開始する。"""
        self._thread = threading.Thread(target=self._run, args=(target,), name=f"job-{self.name}", daemon=True)
        self._thread.start()

    def join(self, timeout: float | None = None) -> None:
        """処理の終了を待つ（テスト用）。"""
        if self._thread is not None:
            self._thread.join(timeout)

    def _run(self, target: Callable[[JobControl], object]) -> None:
        # バックグラウンド処理の入口として、ここで例外をまとめて捕捉し、記録と画面への通知を行う
        try:
            result = target(self.control)
        except Exception:
            logger.exception("background job failed: name=%s", self.name)
            self.error = "処理中にエラーが発生しました。詳細は設定画面のログを確認してください。"
            self.state = JobState.FAILED
        else:
            self.result = result
            self.state = JobState.SUCCEEDED
