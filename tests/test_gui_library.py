"""音楽ライブラリの画面のテスト。Jev の実行ボタンは押さない。"""

import sqlite3
from pathlib import Path

import pytest
from test_gui import click, run_view, wait_for_jobs

from volumio_playlist_studio.albums import album_key, group_albums
from volumio_playlist_studio.config import JEV_ALBUM_QUESTION_VERSION
from volumio_playlist_studio.sample_data import generate_sample_audio
from volumio_playlist_studio.scanner import scan_library
from volumio_playlist_studio.storage import settings_store
from volumio_playlist_studio.storage.album_judgments import AlbumJudgmentRecord, save_album_judgment
from volumio_playlist_studio.storage.database import transaction
from volumio_playlist_studio.storage.tracks import list_tracks


def test_library_view_with_demo_data(demo: sqlite3.Connection) -> None:
    at = run_view("library")

    assert any("楽曲一覧（60 曲）" in s.value for s in at.subheader)
    assert any("サンプルデータではスキャン" in c.value for c in at.caption)
    at.text_input[1].input("piano").run()
    assert not at.exception
    assert any("ルールベース" in m.value for m in at.markdown)
    rows = at.dataframe[0].value
    assert len(rows) == 6
    assert set(rows["アーティスト"]) == {"Demo Quiet Piano"}


@pytest.mark.parametrize(
    ("query", "expected_titles"),
    [
        ("  PiAnO  ", [f"サンプル曲 2-{n}" for n in range(1, 7)]),
        ("サンプル曲 2-1", ["サンプル曲 2-1"]),
        ("サンプルアルバム 2", [f"サンプル曲 2-{n}" for n in range(1, 7)]),
        ("new age", [f"サンプル曲 2-{n}" for n in range(1, 7)]),
    ],
)
def test_library_search_filters_matching_tracks(
    demo: sqlite3.Connection, query: str, expected_titles: list[str]
) -> None:
    at = run_view("library")
    next(t for t in at.text_input if t.label.startswith("検索（")).input(query).run()

    assert not at.exception
    assert sorted(at.dataframe[0].value["曲名"].tolist()) == expected_titles


def test_library_search_no_matches_and_clear(demo: sqlite3.Connection) -> None:
    at = run_view("library")
    next(t for t in at.text_input if t.label.startswith("検索（")).input("該当しない検索語").run()

    assert not at.exception
    assert len(at.dataframe[0].value) == 0
    assert any("条件に合う曲がありません" in message.value for message in at.info)
    assert not any(s.label == "詳細を表示する曲" for s in at.selectbox)

    next(t for t in at.text_input if t.label.startswith("検索（")).input("   ").run()

    assert not at.exception
    assert len(at.dataframe[0].value) == 60
    assert not any("条件に合う曲がありません" in message.value for message in at.info)
    assert any(s.label == "詳細を表示する曲" for s in at.selectbox)


def test_library_view_scans_and_analyzes_real_folder(app_db: sqlite3.Connection, tmp_path: Path) -> None:
    folder = tmp_path / "Music"
    generate_sample_audio(folder, seconds=8.0)
    at = run_view("library")
    at.text_input[0].input(str(folder))
    click(at, "保存")
    assert settings_store.get_str(app_db, settings_store.MUSIC_ROOT_KEY) == folder.as_posix()

    click(at, "スキャン（追加・更新を反映）")
    wait_for_jobs(at)
    at.run()
    assert any("スキャン が完了しました" in s.value for s in at.success)
    assert any("楽曲一覧（4 曲）" in s.value for s in at.subheader)

    click(at, "未解析の曲を解析")
    wait_for_jobs(at)
    at.run()
    assert any("音響解析 が完了しました" in s.value and "解析 4 曲" in s.value for s in at.success)

    # 壊れたファイルを追加して解析すると、失敗した曲の再試行ボタンが表示される
    (folder / "壊れた.flac").write_bytes(b"broken" * 100)
    click(at, "スキャン（追加・更新を反映）")
    wait_for_jobs(at)
    at.run()
    click(at, "未解析の曲を解析")
    wait_for_jobs(at)
    at.run()
    click(at, "失敗した曲を再試行（1 曲）")
    wait_for_jobs(at)
    at.run()
    assert any("音響解析（再試行） が完了しました" in s.value and "失敗 1 曲" in s.value for s in at.success)


def test_music_folder_form_rejects_invalid_paths(app_db: sqlite3.Connection, tmp_path: Path) -> None:
    at = run_view("library")

    at.text_input[0].input("relative/path")
    click(at, "保存")
    assert any("絶対パス" in e.value for e in at.error)
    at.text_input[0].input(str(tmp_path / "missing"))
    click(at, "保存")
    assert any("見つかりません" in e.value for e in at.error)
    assert settings_store.get_str(app_db, settings_store.MUSIC_ROOT_KEY) is None


def test_jev_panel_shows_estimate_before_calling_api(
    app_db: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "Music"
    generate_sample_audio(folder, seconds=3.0)
    scan_library(app_db, folder)
    with transaction(app_db):
        settings_store.set_value(app_db, settings_store.MUSIC_ROOT_KEY, folder.as_posix())

    at = run_view("library")
    assert any("TYPESAFE_API_KEY が未設定" in i.value for i in at.info)

    monkeypatch.setenv("TYPESAFE_API_KEY", "dummy-key-for-test")
    at = run_view("library")
    assert any("未判定の曲: **4 曲**" in m.value for m in at.markdown)
    assert any("リクエスト: **4 件**" in m.value for m in at.markdown)
    assert next(b for b in at.button if b.label == "Jev で判定").disabled  # 確認前は押せない


def test_children_panel_shows_estimate_and_judged_albums(
    app_db: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "Music"
    generate_sample_audio(folder, seconds=3.0)
    scan_library(app_db, folder)
    root = folder.as_posix()
    tracks = list_tracks(app_db, root)
    with transaction(app_db):
        settings_store.set_value(app_db, settings_store.MUSIC_ROOT_KEY, root)
        record = AlbumJudgmentRecord(root, album_key(tracks[0]), JEV_ALBUM_QUESTION_VERSION, 0.9, None)
        save_album_judgment(app_db, record)

    pending = len(group_albums(tracks)) - 1
    assert pending > 0
    at = run_view("library")
    # API キーがなくても、判定済みの結果は確認できる
    assert any("未判定の曲は子供向けではないものとして扱います" in i.value for i in at.info)
    assert any("子供向けと判定したアルバム: 1 件" in m.value for m in at.markdown)

    monkeypatch.setenv("TYPESAFE_API_KEY", "dummy-key-for-test")
    at = run_view("library")
    assert any(f"未判定のアルバム: **{pending} 件**" in m.value for m in at.markdown)
    assert next(b for b in at.button if b.label == "子供向けか判定").disabled  # 確認前は押せない


def test_library_filters_and_empty_results(demo: sqlite3.Connection) -> None:
    at = run_view("library")
    next(s for s in at.selectbox if s.label == "解析状況").select("未解析").run()
    assert any("条件に合う曲がありません" in i.value for i in at.info)
    next(s for s in at.selectbox if s.label == "解析状況").select("解析済み").run()
    next(s for s in at.selectbox if s.label == "判定ジャンルで絞り込み").select("ジャズ").run()
    rows = at.dataframe[0].value
    assert len(rows) > 0
    assert set(rows["判定ジャンル"]) == {"ジャズ"}
    assert set(rows["解析"]) == {"解析済み"}
    assert next(e for e in at.expander if e.label == "音響特徴量・判定の詳細").proto.expanded is False


def test_classification_details_do_not_show_none(demo: sqlite3.Connection) -> None:
    at = run_view("library")
    details = next(d.value for d in at.dataframe if "確信度" in d.value.columns)
    assert set(details["確信度"]) == {"対象外"}
    assert "判定保留" in details["スコア (0〜1)"].tolist()
    assert not details.isna().any().any()
