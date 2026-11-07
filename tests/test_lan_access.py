"""LAN モードの PIN 認証のテスト。"""

import io

import pytest
from PIL import Image

from volumio_playlist_studio.lan_access import LanAccess, PinPolicy, PinResult, lan_ip, qr_png


class FakeClock:
    """テスト用の時計。advance で時間を進める。"""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _wrong(pin: str) -> str:
    return "0000" if pin != "0000" else "1111"


def test_pin_has_configured_digits_and_correct_pin_is_accepted() -> None:
    lan = LanAccess("192.168.1.10", PinPolicy(pin_digits=6))

    assert len(lan.pin) == 6
    assert lan.pin.isdigit()
    assert lan.verify(f" {lan.pin} ") == PinResult.OK


def test_pin_changes_per_instance() -> None:
    # 10 個作って全部同じになる確率は無視できるほど小さい
    assert len({LanAccess(None, PinPolicy(pin_digits=8)).pin for _ in range(10)}) > 1


def test_repeated_failures_regenerate_pin_and_lock() -> None:
    clock = FakeClock()
    lan = LanAccess(None, PinPolicy(max_failures=3, lockout_seconds=30), clock=clock)
    original = lan.pin

    assert [lan.verify(_wrong(original)) for _ in range(2)] == [PinResult.WRONG, PinResult.WRONG]
    assert lan.verify(_wrong(original)) == PinResult.REGENERATED
    # 作り直す前の PIN は、一時停止が終わっても使えない（偶然同じ値になった場合を除く）
    regenerated = lan.pin
    assert lan.locked_seconds() == 30
    assert lan.verify(regenerated) == PinResult.LOCKED

    clock.advance(30)
    assert lan.locked_seconds() == 0
    assert lan.verify(regenerated) == PinResult.OK


def test_lockout_doubles_until_success_and_is_capped() -> None:
    clock = FakeClock()
    lan = LanAccess(None, PinPolicy(max_failures=1, lockout_seconds=30, max_lockout_seconds=100), clock=clock)

    lockouts = []
    for _ in range(4):
        assert lan.verify(_wrong(lan.pin)) == PinResult.REGENERATED
        lockouts.append(lan.locked_seconds())
        clock.advance(lockouts[-1])

    assert lockouts == [30, 60, 100, 100]
    assert lan.verify(lan.pin) == PinResult.OK
    assert lan.verify(_wrong(lan.pin)) == PinResult.REGENERATED
    assert lan.locked_seconds() == 30  # 正しい PIN の入力で元に戻る


def test_success_resets_failure_count() -> None:
    lan = LanAccess(None, PinPolicy(max_failures=2))

    assert lan.verify(_wrong(lan.pin)) == PinResult.WRONG
    assert lan.verify(lan.pin) == PinResult.OK
    assert lan.verify(_wrong(lan.pin)) == PinResult.WRONG


@pytest.mark.parametrize("token", [None, 123, "", "forged-token"])
def test_only_this_launchs_session_token_is_authenticated(token: object) -> None:
    lan = LanAccess(None)

    assert lan.is_authenticated(lan.session_token)
    assert not lan.is_authenticated(token)
    assert not lan.is_authenticated(LanAccess(None).session_token)  # 再起動前の印は使えない


def test_lan_ip_returns_none_when_route_lookup_fails() -> None:
    # 存在しないアドレスへの connect は OSError になる
    assert lan_ip(("256.256.256.256", 80)) is None


def test_lan_ip_ignores_loopback() -> None:
    assert lan_ip(("127.0.0.1", 80)) is None


def test_qr_png_is_a_png_image() -> None:
    data = qr_png("http://192.168.1.10:8501/")

    image = Image.open(io.BytesIO(data))
    assert image.format == "PNG"
    assert image.size[0] == image.size[1]
