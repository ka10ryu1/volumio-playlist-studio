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
    JevSettings,
)
from volumio_playlist_studio.diagnostics import ReportContext, build_report, git_revision
from volumio_playlist_studio.models import (
    Attribute,
    ClassificationResult,
    Genre,
    Mood,
    TitleSource,
    TrackMetadata,
)
from volumio_playlist_studio.sample_data import DEMO_LIBRARY_ROOT
from volumio_playlist_studio.storage import settings_store
from volumio_playlist_studio.storage.album_judgments import AlbumJudgmentRecord, save_album_judgment
from volumio_playlist_studio.storage.analysis import (
    ErrorRecord,
    FeatureRun,
    record_error,
    save_classification,
    save_features,
)
from volumio_playlist_studio.storage.database import transaction
from volumio_playlist_studio.storage.evaluations import (
    EvaluationLabel,
    Split,
    Verdict,
    add_to_evaluation_set,
    save_label,
)
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
    assert "- 判断材料が不足（genre_determinable の確率が閾値未満）: 4 曲" in section
    assert "- ジャンルの confidence が閾値未満: 1 曲" in section
    assert "- 理由を特定できない: 1 曲" in section
    determinable_table = section[section.index("genre_determinable の確率（") :]
    assert (
        "| 0.0 以上 0.1 未満 | 1 |\n| 0.1 以上 0.2 未満 | 0 |\n| 0.2 以上 0.3 未満 | 0 |\n"
        "| 0.3 以上 0.4 未満 | 2 |\n| 0.4 以上 0.5 未満 | 1 |"
    ) in determinable_table
    assert "| 0.5 以上 0.6 未満 | 0 |" in determinable_table
    assert "| 0.9 以上 1.0 以下 | 0 |" in determinable_table
    assert "- 現在の設定: genre_determinable の閾値 0.5、confidence の閾値 0.5" in section
    assert "- 0.4: 1 曲（不明 6 曲 → 5 曲）" in section
    assert "- 0.3: 2 曲（不明 6 曲 → 4 曲）" in section
    confidence_table = section[section.index("confidence が不足した曲") :]
    assert "| 0.3 以上 0.4 未満 | 0 |\n| 0.4 以上 0.5 未満 | 1 |" in confidence_table
    for secret in SECRETS:
        assert secret not in report


def test_unknown_genre_breakdown_uses_saved_jev_settings(
    db: sqlite3.Connection, make_classification: ClassificationFactory
) -> None:
    results = [
        # 判定時の confidence の閾値が 0.7 だった曲。原因は判定時の記録から決まり、現在の設定に左右されない
        _jev_unknown(make_classification, 0.6, ["ジャンルの confidence が低いため不明（0.60）"]),
        _undeterminable(make_classification, "0.32", 0.8),
        _undeterminable(make_classification, "0.31", 0.65),  # 現在の confidence の閾値 0.7 に届かない
        # 0.499 は notes に 0.50 と残る。分布から漏らさない
        _undeterminable(make_classification, "0.50", 0.9),
    ]
    with transaction(db):
        settings_store.set_jev_settings(db, JevSettings(min_confidence=0.7, genre_determinable_threshold=0.35))
        for i, result in enumerate(results):
            track_id = upsert_track(db, ROOT, _meta(f"秘密の歌手/{i:02}.flac"), None)
            save_classification(db, track_id, result)

    report = _report(db, host=None)

    assert "- ジャンルの confidence が閾値未満: 1 曲" in report
    assert "理由を特定できない" not in report
    assert "- 現在の設定: genre_determinable の閾値 0.35、confidence の閾値 0.7" in report
    assert "| 0.3 以上 0.4 未満 | 2 |" in report
    assert "| 0.5 以上 0.6 未満 | 1 |" in report
    # 現在の閾値 0.35 より高い 0.4 は試算しない
    assert "- 0.4:" not in report
    assert "confidence が現在の閾値 0.7 以上の曲に限る" in report
    assert "- 0.3: 2 曲（不明 4 曲 → 2 曲）" in report


@pytest.fixture
def fillable_unknowns(db: sqlite3.Connection, make_classification: ClassificationFactory) -> sqlite3.Connection:
    """ルールベースが不明の曲を、Jev やアルバムで補える曲数を手で数えられる架空のライブラリ。

    (フォルダ, アルバムタグ, ルールベース, Jev)。ルールベースの "-" は未判定、Jev の "-" は未判定、None は不明。
    """
    rows: list[tuple[str, str, Genre | str | None, Genre | str | None]] = [
        ("A", "秘密のアルバム", None, Genre.JAZZ),
        ("A", "秘密のアルバム", Genre.ROCK, "-"),
        ("A", "秘密のアルバム", None, None),
        ("B", "秘密のアルバム", None, "-"),
        ("B", "秘密のアルバム", None, Genre.POP),
        # ジャンルが複数に分かれるアルバム（コンピレーションなど）
        ("C", "秘密のアルバム", Genre.ROCK, "-"),
        ("C", "秘密のアルバム", Genre.POP, "-"),
        ("C", "秘密のアルバム", None, None),
        # フォルダが同じでもアルバムタグが違えば別のアルバム
        ("A", "秘密の別のアルバム", None, "-"),
        # ルールベースで未判定の曲は数えない
        ("E", "秘密のアルバム", "-", Genre.JAZZ),
    ]
    with transaction(db):
        for i, (folder, album, rule_genre, jev_genre) in enumerate(rows):
            meta = replace(_meta(f"秘密の歌手/{folder}/{i:02}.flac"), album=album)
            track_id = upsert_track(db, ROOT, meta, None)
            if rule_genre is None or isinstance(rule_genre, Genre):
                rule_result = replace(make_classification(rule_genre), engine_version=RULE_VERSION)
                save_classification(db, track_id, rule_result)

            if jev_genre is None or isinstance(jev_genre, Genre):
                jev_result = make_classification(jev_genre, engine="jev")
                save_classification(db, track_id, replace(jev_result, engine_version=JEV_QUESTION_VERSION))

    return db


def test_report_estimates_unknown_genres_fillable_by_jev_and_albums(fillable_unknowns: sqlite3.Connection) -> None:
    report = _report(fillable_unknowns, host=None)
    section = report[report.index("不明を補える曲の試算") :]

    assert section.startswith("不明を補える曲の試算（ルールベースが不明の 6 曲のうち。結果は変更していない）:")
    assert "- Jev がジャンルを判定済み: 2 曲（Jev も不明: 2 曲、Jev で未判定: 2 曲）" in section
    # A の 2 曲（ロックのみ）と C の 1 曲（ロックとポップ）。B は判定済みの曲がルールベースにない
    assert (
        "- 同じアルバムに、ルールベースでジャンルが決まった曲がある: 3 曲（うちアルバム内のジャンルが 1 種類: 2 曲）"
        in section
    )
    # Jev で補えない A・B・C・別のアルバムの 4 曲のうち、A（ジャズとロック）・B（ポップ）・C（ロックとポップ）の 3 曲
    assert (
        "- Jev で補えない曲のうち、同じアルバムにルールベースか Jev でジャンルが決まった曲がある: 3 曲"
        "（うちアルバム内のジャンルが 1 種類: 1 曲）"
    ) in section
    # Jev の 2 曲と、ジャンルが 1 種類の B の 1 曲
    assert "- Jev と、ジャンルが 1 種類のアルバムで補った場合: 不明 6 曲 → 3 曲" in section
    for secret in SECRETS:
        assert secret not in report


def test_unknown_genre_breakdown_omits_empty_details(library: sqlite3.Connection) -> None:
    report = _report(library)

    assert "ルールベース（判定済み 2 曲のうち不明 0 曲）:" in report
    assert "分類した後にタグが変わった可能性" not in report
    assert "不明を補える曲の試算（ルールベースが不明の 0 曲のうち。結果は変更していない）:\n\n- 不明の曲なし" in report
    # 不明の 1 曲は notes も confidence もないため、原因を特定できない
    assert "- 理由を特定できない: 1 曲" in report
    assert "閾値を下げた場合" not in report
    assert "confidence が不足した曲" not in report


def test_unknown_genre_breakdown_for_empty_library(db: sqlite3.Connection) -> None:
    report = _report(db, host=None)

    assert "ルールベース（判定済み 0 曲のうち不明 0 曲）:" in report
    assert "- ジャンルタグが空: 0 曲" in report
    assert "Jev（判定済み 0 曲のうち不明 0 曲）:\n\n- 不明の曲なし" in report


def _with_probabilities(
    result: ClassificationResult, engine_version: str, probabilities: dict[Attribute, dict[str, float]]
) -> ClassificationResult:
    attributes = {
        a: replace(score, probabilities=probabilities.get(a, score.probabilities))
        for a, score in result.attributes.items()
    }
    return replace(result, engine_version=engine_version, attributes=attributes)


def _mood_label(
    track_id: int, verdict: Verdict | None, mood: Mood | None, split: Split = Split.TUNING
) -> EvaluationLabel:
    return EvaluationLabel(track_id, split, mood_verdict=verdict, mood_label=mood, note="秘密のメモ")


def _evaluate(conn: sqlite3.Connection, label: EvaluationLabel) -> None:
    """評価対象に追加し、ムードの評価があれば保存する。"""
    add_to_evaluation_set(conn, {label.track_id: label.split})
    if label.is_evaluated:
        save_label(conn, label)


@pytest.fixture
def evaluated_moods(db: sqlite3.Connection, make_classification: ClassificationFactory) -> sqlite3.Connection:
    """人手でムードを評価した曲を、正解と判定の組ごとに手で数えられる架空のライブラリ。"""
    jev_version = JEV_QUESTION_VERSION
    # (人手の評価, 正解, Jev の結果, ルールベースの結果)。結果の None は未判定
    rows: list[tuple[Verdict | None, Mood | None, ClassificationResult | None, ClassificationResult | None]] = [
        # 集中・作業の曲を Jev は元気と判定し、ルールベースは集中・作業と判定した
        (
            Verdict.INCORRECT,
            Mood.FOCUS,
            _with_probabilities(
                make_classification(
                    mood=Mood.ENERGETIC, engine="jev", relaxation=0.2, energy=0.75, work_bgm=0.25, party=0.65
                ),
                jev_version,
                {Attribute.ENERGY: {"0": 0.0, "1": 0.0, "2": 0.2, "3": 0.6, "4": 0.2}},
            ),
            make_classification(mood=Mood.FOCUS, energy=0.5, work_bgm=0.75, party=0.3),
        ),
        (
            Verdict.INCORRECT,
            Mood.FOCUS,
            # 確率に含まれない段階は 0 として平均する
            _with_probabilities(
                make_classification(mood=Mood.ENERGETIC, engine="jev", energy=0.95, work_bgm=0.25, party=0.85),
                jev_version,
                {Attribute.ENERGY: {"3": 0.2, "4": 0.8}},
            ),
            make_classification(mood=Mood.FOCUS, energy=0.7, work_bgm=0.75, party=0.5),
        ),
        # リラックスと穏やかな夜の取り違えは誤りに含めない
        (
            Verdict.CORRECT,
            Mood.RELAX,
            replace(make_classification(mood=Mood.CALM_NIGHT, engine="jev", energy=0.1), engine_version=jev_version),
            make_classification(mood=Mood.RELAX),
        ),
        (
            Verdict.CORRECT,
            Mood.CALM_NIGHT,
            replace(make_classification(mood=Mood.FOCUS, engine="jev"), engine_version=jev_version),
            make_classification(mood=None),
        ),
        # Jev が判定保留にした曲。保留にした属性の確率も平均に含める
        (
            Verdict.INCORRECT,
            Mood.ENERGETIC,
            _with_probabilities(
                make_classification(mood=None, engine="jev", energy=None),
                jev_version,
                {Attribute.ENERGY: {"2": 1.0}},
            ),
            make_classification(mood=Mood.RELAX),
        ),
        # Jev で未判定の曲は、ルールベースの集計にだけ含める
        (Verdict.CORRECT, Mood.FOCUS, None, make_classification(mood=Mood.FOCUS)),
        # 人でも判断できないとした曲と、未評価の曲は数えない
        (
            Verdict.PENDING,
            None,
            replace(make_classification(mood=Mood.ENERGETIC, engine="jev"), engine_version=jev_version),
            make_classification(mood=Mood.RELAX),
        ),
        (
            None,
            None,
            replace(make_classification(mood=Mood.ENERGETIC, engine="jev"), engine_version=jev_version),
            make_classification(mood=Mood.RELAX),
        ),
    ]
    ratings = {0: {Attribute.ENERGY: 2}}
    with transaction(db):
        for i, (verdict, truth, jev_result, rule_result) in enumerate(rows):
            track_id = upsert_track(db, ROOT, _meta(f"秘密の歌手/{i:02}.flac"), None)
            _evaluate(db, replace(_mood_label(track_id, verdict, truth), ratings=ratings.get(i, {})))
            if jev_result is not None:
                save_classification(db, track_id, jev_result)

            if rule_result is not None:
                save_classification(db, track_id, replace(rule_result, engine_version=RULE_VERSION))

        # 検証用の曲は、質問や閾値を直した後の確認に残すため数えない
        holdout = upsert_track(db, ROOT, _meta("秘密の歌手/98.flac"), None)
        holdout_label = _mood_label(holdout, Verdict.INCORRECT, Mood.FOCUS, Split.HOLDOUT)
        _evaluate(db, replace(holdout_label, ratings={Attribute.ENERGY: 5}))
        holdout_jev = _with_probabilities(
            make_classification(mood=Mood.ENERGETIC, engine="jev", energy=0.0, party=0.0),
            jev_version,
            {Attribute.ENERGY: {"0": 1.0}},
        )
        save_classification(db, holdout, holdout_jev)
        holdout_rules = make_classification(mood=Mood.ENERGETIC, energy=0.0)
        save_classification(db, holdout, replace(holdout_rules, engine_version=RULE_VERSION))

        # 別の音楽フォルダの曲は数えない
        other = upsert_track(db, "/other", _meta("秘密の歌手/99.flac"), None)
        _evaluate(db, _mood_label(other, Verdict.INCORRECT, Mood.FOCUS))
        other_jev = make_classification(mood=Mood.ENERGETIC, engine="jev")
        save_classification(db, other, replace(other_jev, engine_version=jev_version))

    return db


def _between(text: str, start: str, end: str) -> str:
    begin = text.index(start)
    return text[begin : text.index(end, begin)]


def test_report_compares_jev_moods_with_human_labels(evaluated_moods: sqlite3.Connection) -> None:
    report = _report(evaluated_moods, host=None)
    section = _between(report, "### Jev のムードの偏り", "### ジャンルが不明の内訳")

    assert (
        "- ムードの正解がある調整用の曲: 6 曲（人でも判断できないとした曲を除く）。"
        "検証用の 1 曲は、質問や閾値を直した後の確認に残すため含めない"
    ) in section
    assert (
        "- Jev: 判定済み 5 曲のうち判定保留 1 曲、正解 1 曲（うちリラックスと穏やかな夜の取り違え 1 曲）、"
        "誤り 3 曲（判定の内訳: 集中・作業 1、元気 2）"
    ) in section
    assert (
        "- ルールベース: 判定済み 6 曲のうち判定保留 1 曲、正解 4 曲（うちリラックスと穏やかな夜の取り違え 0 曲）、"
        "誤り 1 曲（判定の内訳: リラックス 1）"
    ) in section

    header = "| 正解 | リラックス | 集中・作業 | 元気 | 穏やかな夜 | 判定保留 |"
    jev_table = _between(section, "正解（行）と Jev の判定（列）", "正解（行）とルールベース")
    assert (
        f"{header}\n|---|---:|---:|---:|---:|---:|\n"
        "| リラックス | 0 | 0 | 0 | 1 | 0 |\n"
        "| 集中・作業 | 0 | 0 | 2 | 0 | 0 |\n"
        "| 元気 | 0 | 0 | 0 | 0 | 1 |\n"
        "| 穏やかな夜 | 0 | 1 | 0 | 0 | 0 |\n"
    ) in jev_table
    rule_table = _between(section, "正解（行）とルールベース", "ムードの適合度")
    assert (
        f"{header}\n|---|---:|---:|---:|---:|---:|\n"
        "| リラックス | 1 | 0 | 0 | 0 | 0 |\n"
        "| 集中・作業 | 0 | 3 | 0 | 0 | 0 |\n"
        "| 元気 | 1 | 0 | 0 | 0 | 0 |\n"
        "| 穏やかな夜 | 0 | 0 | 0 | 0 | 1 |\n"
    ) in rule_table

    # 元気は energy と party の平均（Jev: 0.70 と 0.90、ルールベース: 0.40 と 0.60）
    score_table = _between(section, "ムードの適合度", "属性の値")
    assert "| 集中・作業 → 元気 | 2 | 0.20 / - | 0.25 / 0.75 | 0.80 / 0.50 | - / - |" in score_table
    # 人手の 5 段階評価の 2 は 0.25。評価を入力していない曲は平均に含めない
    value_table = _between(section, "属性の値", "Jev のリラックス度")
    assert (
        "| 集中・作業 → 元気 | 2 | 0.20 / - / - | 0.85 / 0.60 / 0.25 | - / - / - | - / - / - | 0.25 / 0.75 / - "
        "| 0.75 / 0.40 / - | - / - / - |"
    ) in value_table

    energy_table = _between(
        section, "Jev のエネルギー感（energy）の段階ごとの確率の平均（段階 0 が最も低く 4", "Jev の明るさ"
    )
    assert "| 正解 → Jev の判定 | 曲数 | 0 | 1 | 2 | 3 | 4 | 判定保留 |" in energy_table
    # 行の順は正解、判定の順で Mood の定義順
    assert (
        "| リラックス → 穏やかな夜 | 1 | - | - | - | - | - | 0 |\n"
        "| 集中・作業 → 元気 | 2 | 0.00 | 0.00 | 0.10 | 0.40 | 0.50 | 0 |\n"
        "| 元気 → 判定保留 | 1 | 0.00 | 0.00 | 1.00 | 0.00 | 0.00 | 1 |\n"
        "| 穏やかな夜 → 集中・作業 | 1 | - | - | - | - | - | 0 |\n"
    ) in energy_table
    for attribute in Attribute:
        assert f"（{attribute.value}）の段階ごとの確率の平均" in section

    for secret in [*SECRETS, "/other"]:
        assert secret not in report


def test_mood_bias_without_mood_labels(library: sqlite3.Connection) -> None:
    report = _report(library)

    assert (
        "### Jev のムードの偏り（人手の評価との比較）\n\n"
        "- ムードの正解がある調整用の曲: 0 曲（人でも判断できないとした曲を除く）。"
        "検証用の 0 曲は、質問や閾値を直した後の確認に残すため含めない\n\n###"
    ) in report


def test_mood_bias_excludes_holdout_tracks(db: sqlite3.Connection, make_classification: ClassificationFactory) -> None:
    with transaction(db):
        track_id = upsert_track(db, ROOT, _meta("秘密の歌手/01.flac"), None)
        _evaluate(db, _mood_label(track_id, Verdict.INCORRECT, Mood.FOCUS, Split.HOLDOUT))
        jev_result = make_classification(mood=Mood.ENERGETIC, engine="jev", energy=0.75)
        save_classification(db, track_id, replace(jev_result, engine_version=JEV_QUESTION_VERSION))

    report = _report(db, host=None)
    section = _between(report, "### Jev のムードの偏り", "### ジャンルが不明の内訳")

    assert "- ムードの正解がある調整用の曲: 0 曲" in section
    assert "検証用の 1 曲は、質問や閾値を直した後の確認に残すため含めない" in section
    assert "- Jev:" not in section
    assert "正解（行）と Jev の判定" not in section


def test_mood_bias_without_jev_results(db: sqlite3.Connection, make_classification: ClassificationFactory) -> None:
    with transaction(db):
        track_id = upsert_track(db, ROOT, _meta("秘密の歌手/01.flac"), None)
        _evaluate(db, _mood_label(track_id, Verdict.CORRECT, Mood.RELAX))
        rule_result = make_classification(mood=Mood.CALM_NIGHT)
        save_classification(db, track_id, replace(rule_result, engine_version=RULE_VERSION))
        # 古い質問のバージョンの Jev の結果は比べない
        save_classification(db, track_id, make_classification(mood=Mood.ENERGETIC, engine="jev"))

    report = _report(db, host=None)

    assert (
        "- Jev: 判定済み 0 曲のうち判定保留 0 曲、正解 0 曲（うちリラックスと穏やかな夜の取り違え 0 曲）、誤り 0 曲\n"
    ) in report
    assert (
        "- ルールベース: 判定済み 1 曲のうち判定保留 0 曲、正解 1 曲（うちリラックスと穏やかな夜の取り違え 1 曲）、"
        "誤り 0 曲\n"
    ) in report
    assert "正解（行）と Jev の判定" not in report


def test_mood_bias_without_rule_results(db: sqlite3.Connection, make_classification: ClassificationFactory) -> None:
    with transaction(db):
        track_id = upsert_track(db, ROOT, _meta("秘密の歌手/01.flac"), None)
        _evaluate(db, _mood_label(track_id, Verdict.INCORRECT, Mood.FOCUS))
        jev_result = make_classification(mood=Mood.ENERGETIC, engine="jev", energy=0.75, party=0.25)
        save_classification(db, track_id, replace(jev_result, engine_version=JEV_QUESTION_VERSION))

    report = _report(db, host=None)

    assert (
        "正解（行）とルールベースの判定（列）の曲数（Jev で判定済みの曲に限らない）:\n\n- 判定済みの曲なし\n" in report
    )
    # ルールベースの値がない曲は「-」
    assert "| 集中・作業 → 元気 | 1 | - / - | - / - | 0.50 / - | - / - |" in report
    assert "| 集中・作業 → 元気 | 1 | - / - / - | 0.75 / - / - |" in report


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
