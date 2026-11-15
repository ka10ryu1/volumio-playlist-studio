"""設定画面のテスト。Volumio の接続テストは test_gui_volumio.py で検証する。"""

import sqlite3

from test_gui import click, run_view, submit

from volumio_playlist_studio.models import (
    ClassificationSource,
)
from volumio_playlist_studio.storage import settings_store


def test_settings_view_saves_values(demo: sqlite3.Connection) -> None:
    at = run_view("settings_page")
    assert any("API キー: 未設定" in w.value for w in at.warning)

    next(t for t in at.text_input if t.label == "ホスト名または IP アドレス").input("volumio.local")
    submit(at, "volumio-host")  # Volumio 接続先の保存
    assert settings_store.get_str(demo, settings_store.VOLUMIO_HOST_KEY) == "volumio.local"

    children = next(s for s in at.slider if "子供向け" in s.label)
    assert children.value == 0.9
    children.set_value(0.8)
    next(n for n in at.number_input if n.label == "1 回の実行で判定する曲数の上限").set_value(5)
    submit(at, "jev-settings")
    saved = settings_store.get_jev_settings(demo)
    assert (saved.max_tracks_per_run, saved.children_threshold) == (5, 0.8)

    next(n for n in at.number_input if n.label == "解析する区間の数").set_value(4)
    submit(at, "analysis-params")
    assert settings_store.get_analysis_params(demo).segment_count == 4

    next(r for r in at.radio if r.label == "選曲に使う分類結果").set_value(ClassificationSource.RULES)
    submit(at, "engine")
    assert settings_store.get_str(demo, settings_store.CLASSIFICATION_SOURCE_KEY) == "rules"
    assert not at.exception


def test_settings_view_shows_report_for_issue(demo: sqlite3.Connection) -> None:
    at = run_view("settings_page")
    assert not [c for c in at.code if c.language == "markdown"]

    click(at, "レポートを作成")

    [report] = [c.value for c in at.code if c.language == "markdown"]
    assert report.startswith("## 確認用レポート\n")
    assert "サンプルデータを表示中" in report
    assert "- 登録されている曲: 60 曲" in report
    assert not at.exception
