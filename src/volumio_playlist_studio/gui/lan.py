"""LAN モードの PIN 入力画面と「スマホで開く」画面。

LAN モード（.env の VPS_LAN=1）のときだけ使う。PC 自身（localhost）からの接続は PIN 不要で、
「スマホで開く」画面（QR コードと PIN）は PC 自身からしか開けない。
"""

import logging

import streamlit as st

from volumio_playlist_studio.config import AppConfig
from volumio_playlist_studio.lan_access import LanAccess, PinResult, lan_ip, qr_png

logger = logging.getLogger(__name__)

_AUTH_KEY = "lan_auth"
_PIN_REFRESH_SECONDS = 15.0


@st.cache_resource
def _shared_lan_access(address: str | None) -> LanAccess:
    """全セッションで共有する LanAccess を返す。PIN はアプリを起動している間だけ有効。"""
    return LanAccess(address)


def resolve_lan(config: AppConfig) -> LanAccess | None:
    """LAN モードなら共有の LanAccess を返す。LAN モードでなければ None。"""
    if not config.lan:
        return None

    return _shared_lan_access(config.lan_address or lan_ip())


def require_pin(lan: LanAccess, remote_ip: str | None) -> bool:
    """LAN の端末から未認証で接続された場合に PIN 入力画面を表示する。

    Args:
        lan: 共有の LanAccess。
        remote_ip: 接続元の IP アドレス。PC 自身からの接続なら None。

    Returns:
        画面の表示を続けてよければ True。PIN 入力画面を表示した場合は False。
    """
    if remote_ip is None or lan.is_authenticated(st.session_state.get(_AUTH_KEY)):
        return True

    st.title("PIN の入力")
    st.write("PC の「スマホで開く」画面に表示されている PIN を入力してください。")
    with st.form("lan-login"):
        pin = st.text_input("PIN", max_chars=8)
        submitted = st.form_submit_button("開く", type="primary")

    if not submitted:
        return False

    result = lan.verify(pin)
    if result == PinResult.OK:
        st.session_state[_AUTH_KEY] = lan.session_token
        logger.info("lan login succeeded: remote=%s", remote_ip)
        st.rerun()

    if result == PinResult.LOCKED:
        st.error(
            f"PIN を続けて間違えたため、一時的に入力を受け付けていません。約 {lan.locked_seconds()} 秒後に、"
            "PC の「スマホで開く」画面に表示される新しい PIN を入力してください。"
        )
    elif result == PinResult.REGENERATED:
        st.error("PIN が違います。間違いが続いたため PIN を作り直しました。PC の画面で新しい PIN を確認してください。")
    else:
        st.error("PIN が違います。")

    return False


def render_lan_page(lan: LanAccess, port: int) -> None:
    """「スマホで開く」画面を表示する（PC 自身からの接続のときだけ、ナビゲーションに出す）。"""
    st.title("スマホで開く")
    if lan.address is None:
        st.error(
            "PC の LAN の IP アドレスを取得できませんでした。Wi-Fi への接続を確認するか、"
            ".env の VPS_LAN_ADDRESS に IP アドレスを設定して再起動してください。"
        )
        return

    url = f"http://{lan.address}:{port}/"
    st.write("自宅 Wi-Fi につないだスマホのカメラで QR コードを読み取り、下の PIN を入力してください。")
    st.image(qr_png(url), width=240)
    st.code(url, language=None)
    _pin_panel(lan)
    st.caption(
        "PIN は起動ごとに作り直します。正しい PIN を入力した端末は、そのブラウザのタブを開いている間は"
        "再入力不要です（再読み込みやアプリの再起動の後は、もう一度入力してください）。"
    )
    st.caption("同じ Wi-Fi の端末からは PIN 入力画面まで開けます。来客にはゲスト用の Wi-Fi を使ってもらってください。")


@st.fragment(run_every=_PIN_REFRESH_SECONDS)
def _pin_panel(lan: LanAccess) -> None:
    """PIN を表示する。間違いが続いて PIN が作り直された場合に備え、定期的に表示を更新する。"""
    st.metric("PIN", lan.pin)
    locked = lan.locked_seconds()
    if locked:
        st.warning(f"PIN を続けて間違えた端末があったため、約 {locked} 秒間は入力を受け付けません。")
