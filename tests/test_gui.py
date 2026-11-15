"""Streamlit の画面のテストのうち、アプリ全体や複数の画面にまたがるもの。

画面ごとのテストは test_gui_<画面>.py に置き、AppTest で画面を操作するヘルパー（run_view、click など）は
このファイルから import する。実際の外部 API・Volumio とは通信しない。
"""

import sqlite3
from pathlib import Path

import pytest
from streamlit.proto.SelectWidgetFilterMode_pb2 import SelectWidgetFilterMode
from streamlit.testing.v1 import AppTest

from volumio_playlist_studio.jobs import BackgroundJob
from volumio_playlist_studio.models import Mood
from volumio_playlist_studio.sample_data import DEMO_LIBRARY_ROOT, load_demo_library
from volumio_playlist_studio.storage import settings_store
from volumio_playlist_studio.storage.database import transaction

APP = str(Path(__file__).parents[1] / "src" / "volumio_playlist_studio" / "gui" / "app.py")
TIMEOUT = 60


def view_script(view: str) -> None:
    """1 つの画面だけを表示するスクリプト。AppTest がこの関数のソースを実行する。"""
    import importlib

    from volumio_playlist_studio.gui.context import open_context, show_job_message, show_jobs

    ctx = open_context()
    try:
        show_job_message()
        show_jobs()
        importlib.import_module(f"volumio_playlist_studio.gui.views.{view}").render(ctx)
    finally:
        ctx.conn.close()


def run_view(view: str) -> AppTest:
    at = AppTest.from_function(view_script, args=(view,), default_timeout=TIMEOUT)
    at.run()
    assert not at.exception
    return at


def click(at: AppTest, label: str) -> AppTest:
    next(b for b in at.button if b.label == label).click().run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def navigate(at: AppTest, url_path: str) -> None:
    """本体で登録されたページへ、サイドバーで選んだ場合と同じ要求を送る。"""
    # AppTest.switch_page はファイルのページしか扱えないため、関数で定義したページでは
    # AppTest 内部の遷移先指定を使う。ページの登録・実行と入力保持は本体に任せる。
    at._page_hash = next(
        page_hash for page_hash, page in at._registered_pages.items() if page.get("url_pathname") == url_path
    )
    at.run()
    assert not at.exception, [e.value for e in at.exception]


def submit(at: AppTest, form_id: str) -> None:
    """フォームの送信ボタンを押す。同じ「保存」ボタンが複数あるため、フォームの ID で区別する。"""
    next(b for b in at.button if b.form_id == form_id).click().run()
    assert not at.exception, [e.value for e in at.exception]


def wait_for_jobs(at: AppTest) -> None:
    jobs: dict[str, BackgroundJob] = at.session_state["jobs"]
    for job in jobs.values():
        job.join(timeout=TIMEOUT)


def show_job_result(at: AppTest) -> None:
    """バックグラウンド処理の終了を待ち、結果のメッセージを表示させる。

    処理がすぐに終わると、ボタンを押した後の再実行の中で結果が表示済みになる。
    もう一度実行するとメッセージが消えるため、結果をまだ表示していない場合だけ再実行する。
    """
    wait_for_jobs(at)
    if at.session_state["jobs"]:
        at.run()
        assert not at.exception


def test_app_starts_without_library_and_loads_demo() -> None:
    at = AppTest.from_file(APP, default_timeout=TIMEOUT)
    at.run()

    assert not at.exception
    assert any("未設定" in i.value for i in at.info)
    click(at, "サンプルデータで試す")

    metrics = {m.label: m.value for m in at.metric}
    assert metrics["登録楽曲数"] == "60"
    assert metrics["未解析"] == "0"


def test_refresh_button_shows_latest_data(app_db: sqlite3.Connection) -> None:
    at = AppTest.from_file(APP, default_timeout=TIMEOUT)
    at.run()
    assert any("未設定" in i.value for i in at.info)

    # 別の画面やバックグラウンド処理で DB が更新された状態にする
    load_demo_library(app_db)
    with transaction(app_db):
        settings_store.set_value(app_db, settings_store.MUSIC_ROOT_KEY, DEMO_LIBRARY_ROOT)

    click(at, "更新")

    metrics = {m.label: m.value for m in at.metric}
    assert metrics["登録楽曲数"] == "60"


@pytest.mark.parametrize("view", ["dashboard", "library", "playlist_page", "evaluation_page", "settings_page"])
def test_every_view_has_refresh_button(demo: sqlite3.Connection, view: str) -> None:
    at = run_view(view)

    assert [b.label for b in at.button].count("更新") == 1
    click(at, "更新")


def test_sidebar_links_to_github_issues() -> None:
    at = AppTest.from_file(APP, default_timeout=TIMEOUT)
    at.run()

    assert not at.exception
    links = [e.proto for e in at.sidebar.get("link_button")]
    assert [(link.label, link.url) for link in links] == [
        ("問い合わせ（GitHub）", "https://github.com/ka10ryu1/volumio-playlist-studio/issues")
    ]


def test_dashboard_navigates_to_playlist(demo: sqlite3.Connection) -> None:
    at = AppTest.from_file(APP, default_timeout=TIMEOUT).run()
    click(at, "プレイリストを作成")
    assert [t.value for t in at.title] == ["プレイリスト作成"]
    click(at, "プレイリストを生成")
    assert not next(e for e in at.expander if e.label == "1. 選曲の条件").proto.expanded
    assert [t.label for t in at.tabs] == ["2. 曲を編集", "3. 保存・連携"]
    assert len(at.dataframe[0].value) > 0


def test_dashboard_navigates_to_library(demo: sqlite3.Connection) -> None:
    at = AppTest.from_file(APP, default_timeout=TIMEOUT).run()
    click(at, "音楽ライブラリを開く")
    assert [t.value for t in at.title] == ["音楽ライブラリ"]


def test_select_widgets_do_not_accept_typing_except_track_search(demo: sqlite3.Connection) -> None:
    """選択欄は文字入力を受け付けない（スマートフォンでソフトウェアキーボードを出さない）。

    曲名で検索して選ぶ「追加する曲」だけは文字入力を受け付ける。
    """
    at = run_view("playlist_page")
    click(at, "プレイリストを生成")
    click(at, "プレイリストを保存")
    screens = [at, run_view("library"), run_view("settings_page")]
    at = run_view("evaluation_page")
    click(at, "無作為に選んで追加")
    screens.append(at)

    widgets = [w for screen in screens for w in [*screen.selectbox, *screen.multiselect]]
    typable = [w.label for w in widgets if w.proto.filter_mode != SelectWidgetFilterMode.FILTER_MODE_NONE]

    assert typable == ["追加する曲"]
    assert [w.label for w in widgets].count("プレイリスト") == 2
    assert {w.label for w in widgets} >= {
        "ジャンルを選ぶ（全ジャンル以外の場合）",
        "編集する曲",
        "プレイリスト",
        "登録するプレイリスト",
        "解析状況",
        "判定ジャンルで絞り込み",
        "詳細を表示する曲",
        "サンプリング周波数（Hz）",
        "曲",
        "誤りの場合、正しいジャンル",
        "誤りの場合、正しいムード",
        "雰囲気に合わなかった曲",
    }


def test_playlist_round_trip_keeps_unsaved_inputs_and_draft(demo: sqlite3.Connection) -> None:
    at = AppTest.from_file(APP, default_timeout=TIMEOUT).run()
    click(at, "プレイリストを作成")
    click(at, "プレイリストを生成")
    click(at, "除外")
    before = at.dataframe[0].value["曲名"].tolist()
    at.text_input(key="pl_name").input("戻って編集する名前").run()
    at.selectbox(key="pl_mood").set_value(Mood.FOCUS).run()
    at.number_input(key="pl_count").set_value(7).run()
    at.number_input(key="pl_seed").set_value(2024).run()
    navigate(at, "settings")
    assert [t.value for t in at.title] == ["設定"]
    assert not at.dataframe
    navigate(at, "playlist")
    assert [t.value for t in at.title] == ["プレイリスト作成"]
    assert at.text_input(key="pl_name").value == "戻って編集する名前"
    assert at.selectbox(key="pl_mood").value == Mood.FOCUS
    assert at.number_input(key="pl_count").value == 7
    assert at.number_input(key="pl_seed").value == 2024
    assert at.dataframe[0].value["曲名"].tolist() == before
