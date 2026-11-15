import pytest
from streamlit.testing.v1 import AppTest

from volumio_playlist_studio.jobs import BackgroundJob, JobState


def job_panel_script() -> None:
    """本文の再実行回数を数え、完了通知が入力画面を再実行しないことを検証する。"""
    import streamlit as st

    from volumio_playlist_studio.gui.context import show_job_message, show_jobs

    st.session_state["runs"] = st.session_state.get("runs", 0) + 1
    show_job_message()
    show_jobs()
    st.text_input("編集中の名前", key="draft-name")


@pytest.mark.parametrize("state", [JobState.SUCCEEDED, JobState.FAILED])
def test_job_completion_waits_for_explicit_refresh(state: JobState) -> None:
    at = AppTest.from_function(job_panel_script)
    job = BackgroundJob(name="解析", state=state, result="3 曲", error="テスト用のエラー")
    at.session_state["jobs"] = {job.name: job}
    at.session_state["draft-name"] = "入力中の名前"
    at.run()

    assert not at.exception
    assert at.session_state["runs"] == 1
    assert at.text_input[0].value == "入力中の名前"
    assert at.session_state["jobs"] == {job.name: job}
    if state == JobState.FAILED:
        assert [message.value for message in at.error] == ["解析: テスト用のエラー"]
    else:
        assert [message.value for message in at.success] == ["解析 が完了しました。3 曲"]

    at.button(key="apply-解析").click().run()
    assert not at.exception
    assert at.session_state["jobs"] == {}
    assert at.text_input[0].value == "入力中の名前"
    assert not any(button.label == "結果を画面に反映" for button in at.button)


def test_cancel_remains_available_until_job_finishes() -> None:
    at = AppTest.from_function(job_panel_script)
    job = BackgroundJob(name="解析", done=1, total=3)
    at.session_state["jobs"] = {job.name: job}
    at.run()
    assert not at.exception
    assert not any(button.label == "結果を画面に反映" for button in at.button)

    at.button(key="cancel-解析").click().run()
    assert job.cancel_event.is_set()
    assert at.session_state["jobs"] == {job.name: job}


def test_multiple_completions_can_be_acknowledged_separately() -> None:
    at = AppTest.from_function(job_panel_script)
    at.session_state["jobs"] = {
        name: BackgroundJob(name=name, state=JobState.SUCCEEDED) for name in ("スキャン", "解析")
    }
    at.run()
    assert not at.exception
    assert at.session_state["runs"] == 1
    at.button(key="apply-スキャン").click().run()
    assert set(at.session_state["jobs"]) == {"解析"}
    at.button(key="apply-解析").click().run()
    assert at.session_state["jobs"] == {}
