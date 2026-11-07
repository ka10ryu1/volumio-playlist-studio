"""自宅 LAN のスマホから接続するときの PIN 認証、PC の LAN の IP アドレスの取得、QR コードの生成。

PIN は起動ごとに secrets で生成し、プロセスのメモリにだけ置く（ファイルやログには出さない）。
総当たり対策として、続けて間違えた回数が上限に達したら PIN を作り直し、しばらく入力を受け付けない。
"""

import hmac
import io
import ipaddress
import logging
import math
import secrets
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

import qrcode
from qrcode.image.pil import PilImage

logger = logging.getLogger(__name__)


class PinResult(StrEnum):
    """PIN の照合結果。"""

    OK = "ok"
    WRONG = "wrong"
    # 間違いが上限に達したため PIN を作り直した
    REGENERATED = "regenerated"
    # 間違いが続いたため、入力を一時的に受け付けていない
    LOCKED = "locked"


@dataclass(frozen=True)
class PinPolicy:
    """PIN の桁数と総当たり対策の設定。

    Attributes:
        pin_digits: PIN の桁数。
        max_failures: この回数続けて間違えたら PIN を作り直して入力を一時停止する。
        lockout_seconds: 最初の一時停止の秒数。正しい PIN が入力されるまで、作り直すたびに倍にする。
        max_lockout_seconds: 一時停止の上限秒数。
    """

    pin_digits: int = 4
    max_failures: int = 5
    lockout_seconds: float = 30.0
    max_lockout_seconds: float = 7200.0


class LanAccess:
    """起動ごとの PIN と、PIN で認証したセッションの照合を管理する。

    Streamlit のセッション（スレッド）間で共有するため、状態はロックで保護する。

    Attributes:
        address: スマホから接続する PC の LAN の IP アドレス。取得できなければ None。
        session_token: 認証済みのセッションに保存する印。起動ごとに変わるため、
            再起動すると全端末で PIN の入力が必要になる。
    """

    def __init__(
        self,
        address: str | None = None,
        policy: PinPolicy | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """PIN を生成する。

        Args:
            address: スマホから接続する PC の LAN の IP アドレス。
            policy: PIN の桁数と総当たり対策の設定。省略時は既定値。
            clock: 経過時間の取得関数（テストで差し替える）。
        """
        self.address = address
        self._policy = policy or PinPolicy()
        self._clock = clock
        self._lock = threading.Lock()
        self._pin = self._new_pin()
        self._failures = 0
        self._next_lockout = min(self._policy.lockout_seconds, self._policy.max_lockout_seconds)
        self._locked_until = 0.0
        self.session_token = secrets.token_urlsafe(32)

    def _new_pin(self) -> str:
        digits = self._policy.pin_digits
        return f"{secrets.randbelow(10**digits):0{digits}d}"

    @property
    def pin(self) -> str:
        """現在の PIN（PC の「スマホで開く」画面に表示する）。"""
        with self._lock:
            return self._pin

    def locked_seconds(self) -> int:
        """PIN の入力を受け付けない残り秒数。受け付けるなら 0。"""
        with self._lock:
            return self._remaining_lock()

    def _remaining_lock(self) -> int:
        return max(0, math.ceil(self._locked_until - self._clock()))

    def verify(self, submitted: str) -> PinResult:
        """入力された PIN を定数時間で照合する。

        間違いの回数は全端末で共通に数える。正しい PIN が入力されたら、間違いの回数と一時停止の秒数を元に戻す。

        Args:
            submitted: 入力された PIN。

        Returns:
            照合結果。一時停止中は照合せずに LOCKED を返す。
        """
        with self._lock:
            if self._remaining_lock():
                return PinResult.LOCKED

            if hmac.compare_digest(submitted.strip().encode("utf-8"), self._pin.encode("ascii")):
                self._failures = 0
                self._next_lockout = min(self._policy.lockout_seconds, self._policy.max_lockout_seconds)
                return PinResult.OK

            self._failures += 1
            logger.warning("lan pin rejected: failures=%d", self._failures)
            if self._failures < self._policy.max_failures:
                return PinResult.WRONG

            lockout = self._next_lockout
            self._pin = self._new_pin()
            self._failures = 0
            self._next_lockout = min(lockout * 2, self._policy.max_lockout_seconds)
            self._locked_until = self._clock() + lockout
            logger.warning("lan pin regenerated: reason=too many failures lockout_sec=%d", lockout)
            return PinResult.REGENERATED

    def is_authenticated(self, token: object) -> bool:
        """セッションに保存された認証済みの印が、この起動のものかを確かめる。

        Args:
            token: セッションに保存された値。未認証なら None。
        """
        if not isinstance(token, str):
            return False

        return hmac.compare_digest(token.encode("utf-8"), self.session_token.encode("ascii"))


def lan_ip(probe: tuple[str, int] = ("8.8.8.8", 80)) -> str | None:
    """スマホから接続するための、PC の LAN の IP アドレスを返す。

    UDP ソケットを外部のアドレスに connect し、OS が選んだ送信元アドレスを読む（UDP の connect はパケットを送らない）。

    Args:
        probe: 経路の選択に使う宛先。

    Returns:
        IP アドレス。ネットワークに接続していないなどで取得できなければ None。
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(probe)
            address = str(sock.getsockname()[0])
    except OSError as exc:
        logger.warning("lan ip lookup failed: error=%s", type(exc).__name__)
        return None

    parsed = ipaddress.ip_address(address)
    if parsed.is_loopback or parsed.is_unspecified:
        return None

    return address


def qr_png(data: str) -> bytes:
    """文字列を QR コードの PNG 画像にする。

    Args:
        data: QR コードにする文字列（スマホから開く URL）。
    """
    # Pillow は Streamlit の依存関係として導入済みのため、Pillow で画像を作る
    image = qrcode.make(data, border=2, image_factory=PilImage)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()
