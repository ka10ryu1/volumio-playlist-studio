"""確認用レポート（diagnostics.py）と、Jev の使用量の記録のテスト。"""

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import CALM, ClassificationFactory

from volumio_playlist_studio.config import (
    ANALYSIS_VERSION,
    JEV_ALBUM_QUESTION_VERSION,
    JEV_QUESTION_VERSION,
    RULE_VERSION,
    AnalysisParams,
)
from volumio_playlist_studio.diagnostics import ReportContext, build_report, git_revision
from volumio_playlist_studio.models import Attribute, ClassificationResult, Genre, TitleSource, TrackMetadata
from volumio_playlist_studio.sample_data import DEMO_LIBRARY_ROOT
from volumio_playlist_studio.storage.album_judgments import AlbumJudgmentRecord, save_album_judgment
from volumio_playlist_studio.storage.analysis import (
    ErrorRecord,
    FeatureRun,
    record_error,
    save_classification,
    save_features,
)
from volumio_playlist_studio.storage.database import transaction
from volumio_playlist_studio.storage.jev_usage import JevUsageKind, JevUsageRecord, list_usage, record_usage
from volumio_playlist_studio.storage.tracks import ScanSummary, get_track, mark_missing, record_scan, upsert_track
from volumio_playlist_studio.storage.volumio_library import replace_uris

ROOT = "/music"
HOST = "volumio-secret.local"
# レポートに出してはいけない文字列（曲名・アーティスト名・アルバム名・パス・接続先・エラーの本文）
SECRETS = ["秘密", "消えた", HOST, ROOT, "cannot read"]


def _meta(rel_path: str) -> TrackMetadata:
    return TrackMetadata(
        rel_path=rel_path, title="秘密の曲", artist="秘密の歌手", album="秘密のアルバム", genre=None, year=None,
        track_number=None, duration_sec=200.0, bitrate=None, sample_rate=None, channels=None, codec=None,
        file_size=100, mtime_ns=1, title_source=TitleSource.TAG, tags_repaired=False, quick_hash=None,
    )  # fmt: skip


def _save_features(conn: sqlite3.Connection, track_id: int, elapsed_sec: float) -> None:
    track = get_track(conn, track_id)
    assert track is not None
    save_features(conn, track, CALM, FeatureRun(AnalysisParams(), ANALYSIS_VERSION, elapsed_sec))


@pytest.fixture
def library(db: sqlite3.Connection, make_classification: ClassificationFactory) -> sqlite3.Connection:
    """件数を手で数えられる架空のライブラリ。"""
    all_attributes = {a.value: 0.5 for a in Attribute}
    with transaction(db):
        a = upsert_track(
            db, ROOT, replace(_meta("秘密の歌手/秘密のアルバム/01 秘密の曲.flac"), tags_repaired=True), None
        )
        from_filename = replace(
            _meta("秘密の歌手/秘密のアルバム/02.flac"), title="02", title_source=TitleSource.FILENAME
        )
        b = upsert_track(db, ROOT, from_filename, "broken")
        upsert_track(db, ROOT, replace(_meta("秘密の歌手/その他/03.m4a"), title="秘密\ufffd"), None)
        d = upsert_track(db, ROOT, _meta("秘密の歌手/秘密のアルバム/04.mp3"), None)
        e = upsert_track(db, ROOT, _meta("消えた/05.flac"), None)
        mark_missing(db, [e])

        record_scan(db, ROOT, "2026-11-01T00:00:00+00:00", ScanSummary(5, 0, 0, 0, 1, 12.5))
        record_scan(db, ROOT, "2026-11-02T00:00:00+00:00", ScanSummary(0, 0, 4, 1, 0, 3725.0))

        _save_features(db, a, 4.0)
        _save_features(db, b, 6.0)
        error = ErrorRecord(
            "features", "LibsndfileError", "秘密の歌手/秘密のアルバム/04.mp3: cannot read", ANALYSIS_VERSION, 100, 1
        )
        record_error(db, d, error)

        jazz = make_classification(Genre.JAZZ, **all_attributes)
        save_classification(db, a, replace(jazz, engine_version=RULE_VERSION))
        save_classification(db, b, replace(make_classification(Genre.ROCK), engine_version=RULE_VERSION))
        jev_a = make_classification(Genre.JAZZ, engine="jev", **{**all_attributes, "energy": None})
        save_classification(db, a, replace(jev_a, engine_version=JEV_QUESTION_VERSION))
        jev_b = make_classification(None, engine="jev", **all_attributes)
        save_classification(db, b, replace(jev_b, engine_version=JEV_QUESTION_VERSION))
        save_album_judgment(db, AlbumJudgmentRecord(ROOT, "秘密のアルバム", JEV_ALBUM_QUESTION_VERSION, 0.2, None))

        replace_uris(
            db,
            HOST,
            [
                "music-library/INTERNAL/秘密の歌手/秘密のアルバム/01 秘密の曲.flac",
                "music-library/INTERNAL/秘密の歌手/秘密のアルバム/02.flac",
                "music-library/USB/秘密の歌手/秘密のアルバム/02.flac",
            ],
        )
        record_usage(db, JevUsageRecord(JevUsageKind.TRACKS, JEV_QUESTION_VERSION, "jev-test", 2, 1, 800, 140))
        record_usage(db, JevUsageRecord(JevUsageKind.ALBUMS, JEV_ALBUM_QUESTION_VERSION, "jev-test", 0, 1, 0, 0))

    return db


def _report(conn: sqlite3.Connection, root: str | None = ROOT, host: str | None = HOST, tmp: Path | None = None) -> str:
    return build_report(conn, ReportContext(root, host, tmp or Path("/nonexistent-project")))


def test_report_summarizes_scan_analysis_tags_and_matching(library: sqlite3.Connection) -> None:
    report = _report(library)

    assert report.startswith("## 確認用レポート\n")
    assert "- 登録されている曲: 4 曲（見つからなくなった曲: 1 曲）" in report
    assert "- 形式: .flac 2、.m4a 1、.mp3 1" in report
    # 新しい順。1 時間を超える所要時間は時・分で表す
    assert report.index("| 0 | 0 | 4 | 1 | 0 | 1 時間 2 分 |") < report.index("| 5 | 0 | 0 | 0 | 1 | 12.5 秒 |")

    assert "- 解析済み 2 曲 / 未解析 0 曲 / 失敗 1 曲 / 未対応の形式 1 曲" in report
    assert "- 未対応の形式の内訳: .m4a 1" in report
    assert "- 1 曲あたりの解析時間: 平均 5.0 秒、最長 6.0 秒（合計 10.0 秒）" in report
    assert "| features | LibsndfileError | 1 |" in report

    assert "- タグを読めなかった曲: 1 曲" in report
    assert "- 文字化けを修復した曲: 1 曲" in report
    assert "- 曲名をファイル名から補った曲: 1 曲" in report
    assert "- 読めない文字（�）が残っている曲: 1 曲" in report

    assert "- Volumio の曲の一覧: 3 曲" in report
    assert "- ライブラリ全体の照合: 対応あり 1 曲、対応なし 2 曲、候補が複数 1 曲" in report


def test_report_summarizes_jev_results_and_usage(library: sqlite3.Connection) -> None:
    report = _report(library)

    assert "- 判定済み: 2 曲（現在の質問のバージョン）、子供向けのアルバムの判定: 1 アルバム" in report
    assert "- ジャンルが不明: 1 曲、ムードが判定保留: 2 曲" in report
    assert "- 判定保留になった属性: エネルギー感 1" in report
    # Jev がジャンル不明の曲は一致率の分母に含めない
    assert "ルールベースとジャンルが一致した曲（両方で判定済み・どちらも不明でない曲のうち）: 1 / 1 曲" in report
    assert "| ジャズ | 1 | 1 |" in report
    assert "| ロック | 1 | 0 |" in report
    assert "| 不明 | 0 | 1 |" in report
    # 成功 1 件あたりのトークン数は (800 + 140) / 2。成功がない実行は「-」
    assert "| 曲の判定 | jev-test | 2 | 1 | 800 | 140 | 470 |" in report
    assert "| 子供向けのアルバムの判定 | jev-test | 0 | 1 | 0 | 0 | - |" in report


def _jev_unknown(
    make_classification: ClassificationFactory, genre_confidence: float | None, notes: list[str]
) -> ClassificationResult:
    result = make_classification(None, engine="jev")
    return replace(result, engine_version=JEV_QUESTION_VERSION, genre_confidence=genre_confidence, notes=notes)


def _undeterminable(
    make_classification: ClassificationFactory, p: str, genre_confidence: float
) -> ClassificationResult:
    # 保存済みの結果と同じ形の notes（classify/jev.py が判断材料の不足で不明にしたときの文言）
    note = f"判断材料が不足しているためジャンルは不明（確率 {p}）"
    return _jev_unknown(make_classification, genre_confidence, [note])


@pytest.fixture
def unknown_genres(db: sqlite3.Connection, make_classification: ClassificationFactory) -> sqlite3.Connection:
    """ジャンルが不明になった原因を手で数えられる架空のライブラリ。"""
    tags = [None, "  ", "秘密のジャンル", " 秘密のジャンル ", "Jazz", "Rock", None]
    rules: list[Genre | None] = [None, None, None, None, None, Genre.ROCK, None]
    jev = [
        _undeterminable(make_classification, "0.45", 0.8),
        _undeterminable(make_classification, "0.30", 0.9),  # 閾値 0.3 ちょうど
        _undeterminable(make_classification, "0.35", 0.2),  # 閾値を下げても confidence が足りない
        _undeterminable(make_classification, "0.05", 0.9),
        _jev_unknown(make_classification, 0.42, ["ジャンルの confidence が低いため不明（0.42）"]),
        _jev_unknown(make_classification, None, []),
        replace(make_classification(Genre.JAZZ, engine="jev"), engine_version=JEV_QUESTION_VERSION),
    ]
    with transaction(db):
        for i, (tag, rule_genre, jev_result) in enumerate(zip(tags, rules, jev, strict=True)):
            track_id = upsert_track(db, ROOT, replace(_meta(f"秘密の歌手/{i:02}.flac"), genre=tag), None)
            # 最後の曲はルールベースで未判定
            if i < len(tags) - 1:
                rule_result = replace(make_classification(rule_genre), engine_version=RULE_VERSION)
                save_classification(db, track_id, rule_result)

            save_classification(db, track_id, jev_result)

    return db


def test_report_breaks_down_unknown_genres(unknown_genres: sqlite3.Connection) -> None:
    report = _report(unknown_genres, host=None)
    section = report[report.index("### ジャンルが不明の内訳") :]

    assert "ルールベース（判定済み 6 曲のうち不明 5 曲）:" in section
    assert "- ジャンルタグが空: 2 曲" in section
    # 前後の空白だけが違う表記は同じ種類として数える
    assert "- タグはあるが候補に当てはまらない: 2 曲（表記 1 種類）" in section
    assert "- タグは候補に当てはまる（分類した後にタグが変わった可能性）: 1 曲" in section

    assert "Jev（判定済み 7 曲のうち不明 6 曲）:" in section
    assert "- 判断材料が不足（genre_determinable の確率が 0.5 未満）: 4 曲" in section
    assert "- ジャンルの confidence が 0.5 未満: 1 曲" in section
    assert "- 理由を特定できない: 1 曲" in section
    determinable_table = section[section.index("genre_determinable の確率（") :]
    assert (
        "| 0.0 以上 0.1 未満 | 1 |\n| 0.1 以上 0.2 未満 | 0 |\n| 0.2 以上 0.3 未満 | 0 |\n"
        "| 0.3 以上 0.4 未満 | 2 |\n| 0.4 以上 0.5 未満 | 1 |"
    ) in determinable_table
    assert "- 0.4: 1 曲（不明 6 曲 → 5 曲）" in section
    assert "- 0.3: 2 曲（不明 6 曲 → 4 曲）" in section
    confidence_table = section[section.index("confidence が不足した曲") :]
    assert "| 0.3 以上 0.4 未満 | 0 |\n| 0.4 以上 0.5 未満 | 1 |" in confidence_table
    for secret in SECRETS:
        assert secret not in report


def test_unknown_genre_breakdown_omits_empty_details(library: sqlite3.Connection) -> None:
    report = _report(library)

    assert "ルールベース（判定済み 2 曲のうち不明 0 曲）:" in report
    assert "分類した後にタグが変わった可能性" not in report
    # 不明の 1 曲は notes も confidence もないため、原因を特定できない
    assert "- 理由を特定できない: 1 曲" in report
    assert "閾値を下げた場合" not in report
    assert "confidence が不足した曲" not in report


def test_unknown_genre_breakdown_for_empty_library(db: sqlite3.Connection) -> None:
    report = _report(db, host=None)

    assert "ルールベース（判定済み 0 曲のうち不明 0 曲）:" in report
    assert "- ジャンルタグが空: 0 曲" in report
    assert "Jev（判定済み 0 曲のうち不明 0 曲）:\n\n- 不明の曲なし" in report


def test_report_does_not_contain_names_paths_host_or_error_messages(library: sqlite3.Connection) -> None:
    report = _report(library)

    for secret in SECRETS:
        assert secret not in report


def test_report_without_library_has_only_environment(db: sqlite3.Connection) -> None:
    report = _report(db, root=None, host=None)

    assert "### 環境" in report
    assert "- アプリのコミット: 不明" in report
    assert f"{ANALYSIS_VERSION} / {RULE_VERSION} / {JEV_QUESTION_VERSION}" in report
    assert "音楽フォルダが未設定のため" in report
    assert "### スキャン" not in report


def test_report_for_empty_library_and_volumio_states(db: sqlite3.Connection) -> None:
    no_host = _report(db, host=None)
    not_collected = _report(db)

    assert "- 登録されている曲: 0 曲（見つからなくなった曲: 0 曲）" in no_host
    assert "- スキャンの履歴: なし" in no_host
    assert "- 未解決のエラー: なし" in no_host
    assert "- 接続先: 未設定" in no_host
    assert "- 判定済み: 0 曲" in no_host
    assert "Jev の実行" not in no_host
    assert "- Volumio の曲の一覧: 未取得" in not_collected


def test_report_warns_when_showing_sample_data(demo: sqlite3.Connection) -> None:
    report = _report(demo, root=DEMO_LIBRARY_ROOT, host=None)

    assert "サンプルデータを表示中" in report


SHA = "0123456789abcdef0123456789abcdef01234567"


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        ({"HEAD": "ref: refs/heads/main\n", "refs/heads/main": f"{SHA}\n"}, "0123456"),
        ({"HEAD": "ref: refs/heads/main\n", "packed-refs": f"# pack-refs\n{SHA} refs/heads/main\n"}, "0123456"),
        ({"HEAD": f"{SHA}\n"}, "0123456"),
        ({"HEAD": "ref: refs/heads/main\n", "packed-refs": f"{SHA} refs/heads/other\n"}, None),
        ({"HEAD": "ref: refs/heads/main\n"}, None),
        ({}, None),
    ],
)
def test_git_revision(tmp_path: Path, files: dict[str, str], expected: str | None) -> None:
    for name, content in files.items():
        path = tmp_path / ".git" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    assert git_revision(tmp_path) == expected


def test_report_shows_git_revision(db: sqlite3.Connection, tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text(f"{SHA}\n", encoding="utf-8")

    assert "- アプリのコミット: 0123456" in _report(db, root=None, tmp=tmp_path)


def test_usage_is_listed_newest_first_and_limited(db: sqlite3.Connection) -> None:
    with transaction(db):
        for i in range(3):
            record_usage(db, JevUsageRecord(JevUsageKind.TRACKS, "v", "m", i, 0, 10 * i, i))

    records = list_usage(db, limit=2)

    assert [r.succeeded for r in records] == [2, 1]
    assert records[0].finished_at != ""


def test_negative_usage_is_rejected(db: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError), transaction(db):
        record_usage(db, JevUsageRecord(JevUsageKind.TRACKS, "v", "m", -1, 0, 0, 0))

    assert list_usage(db) == []
