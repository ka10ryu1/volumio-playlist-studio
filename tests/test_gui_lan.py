"""LAN モードの PIN 入力画面のテスト。"""

import sqlite3

import pytest
from streamlit.testing.v1 import AppTest
from test_gui import APP, TIMEOUT


def lan_script(remote_ip: str | None) -> None:
    """LAN モードの PIN 入力画面だけを表示するスクリプト。テストから PIN を読めるよう session_state に入れる。"""
    import streamlit as st

    from volumio_playlist_studio.gui.lan import render_lan_page, require_pin
    from volumio_playlist_studio.lan_access import LanAccess, PinPolicy

    if "lan" not in st.session_state:
        st.session_state["lan"] = LanAccess("192.168.1.10", PinPolicy(max_failures=2))

    lan = st.session_state["lan"]
    st.session_state["test_pin"] = lan.pin
    if require_pin(lan, remote_ip):
        if remote_ip is None:
            render_lan_page(lan, 8501)
        else:
            st.write("ライブラリの画面")


def test_pc_itself_does_not_need_pin_and_sees_qr_code() -> None:
    at = AppTest.from_function(lan_script, args=(None,), default_timeout=TIMEOUT)
    at.run()

    assert not at.exception
    assert any(m.label == "PIN" and m.value == at.session_state["test_pin"] for m in at.metric)
    assert any("http://192.168.1.10:8501/" in c.value for c in at.code)
    assert len(at.get("image")) == 1


def test_lan_device_must_enter_pin() -> None:
    at = AppTest.from_function(lan_script, args=("192.168.1.20",), default_timeout=TIMEOUT)
    at.run()
    assert any(t.value == "PIN の入力" for t in at.title)
    assert not any("ライブラリの画面" in m.value for m in at.markdown)

    at.text_input[0].input("wrong")
    at.button[0].click().run()
    assert any("PIN が違います" in e.value for e in at.error)

    at.text_input[0].input(at.session_state["test_pin"])
    at.button[0].click().run()
    assert any("ライブラリの画面" in m.value for m in at.markdown)
    # 認証済みのセッションでは、再実行しても PIN を求めない
    at.run()
    assert any("ライブラリの画面" in m.value for m in at.markdown)


def test_pin_is_regenerated_after_repeated_failures() -> None:
    at = AppTest.from_function(lan_script, args=("192.168.1.20",), default_timeout=TIMEOUT)
    at.run()
    original = at.session_state["test_pin"]
    wrong = "0000" if original != "0000" else "1111"

    at.text_input[0].input(wrong)
    at.button[0].click().run()
    at.text_input[0].input(wrong)
    at.button[0].click().run()
    assert any("作り直しました" in e.value for e in at.error)

    at.text_input[0].input(at.session_state["test_pin"])
    at.button[0].click().run()
    assert any("一時的に入力を受け付けていません" in e.value for e in at.error)


def test_app_without_lan_mode_has_no_lan_page() -> None:
    at = AppTest.from_file(APP, default_timeout=TIMEOUT)
    at.run()

    assert not at.exception
    assert not any(t.value == "PIN の入力" for t in at.title)


def test_app_in_lan_mode_requires_pin_before_any_page(
    monkeypatch: pytest.MonkeyPatch, demo: sqlite3.Connection
) -> None:
    monkeypatch.setenv("VPS_LAN", "1")
    monkeypatch.setenv("VPS_LAN_ADDRESS", "192.168.1.30")
    at = AppTest.from_file(APP, default_timeout=TIMEOUT)
    at.run()

    # AppTest の接続は localhost と判定されないため、LAN の端末からの接続として扱われる
    assert not at.exception
    assert [t.value for t in at.title] == ["PIN の入力"]
    assert not at.metric
