"""実機での確認結果を GitHub の issue に貼るための、確認用レポートの作成。

開発環境のない PC でも、画面のボタン 1 つで件数・所要時間・エラーの種類などを集計し、Markdown で出力する。
曲名・アーティスト名・アルバム名・フォルダ名・ファイル名・Volumio の接続先・エラーの本文は含めない
（エラーの本文には相対パスが含まれることがあるため、種類だけを出す）。
"""

import platform
import sqlite3
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path, PurePosixPath

from volumio_playlist_studio.albums import album_key
from volumio_playlist_studio.analyzer import is_analyzable
from volumio_playlist_studio.catalog import UNKNOWN_GENRE_LABEL, dashboard_stats
from volumio_playlist_studio.classify.genre_tags import genre_from_tag
from volumio_playlist_studio.classify.jev import ENGINE_NAME as JEV_ENGINE
from volumio_playlist_studio.classify.jev import recorded_genre_determinable
from volumio_playlist_studio.classify.rules import ENGINE_NAME as RULES_ENGINE
from volumio_playlist_studio.config import (
    ANALYSIS_VERSION,
    JEV_ALBUM_QUESTION_VERSION,
    JEV_MOOD_QUESTION_VERSION,
    JEV_QUESTION_VERSION,
    RULE_VERSION,
    JevSettings,
)
from volumio_playlist_studio.models import (
    ATTRIBUTE_LABELS,
    GENRE_LABELS,
    ClassificationResult,
    ClassificationSource,
    Genre,
    TitleSource,
    Track,
)
from volumio_playlist_studio.sample_data import DEMO_LIBRARY_ROOT
from volumio_playlist_studio.storage import analysis as analysis_store
from volumio_playlist_studio.storage import settings_store
from volumio_playlist_studio.storage.database import current_version, utc_now
from volumio_playlist_studio.storage.jev_usage import JevUsageKind, list_usage
from volumio_playlist_studio.storage.tracks import list_tracks
from volumio_playlist_studio.storage.volumio_library import last_collected_at, load_uris
from volumio_playlist_studio.volumio import MatchStatus, match_tracks_to_uris

_RECENT_SCANS = 5
_RECENT_JEV_RUNS = 10
_REPLACEMENT_CHAR = "\ufffd"
_SECONDS_PER_MINUTE = 60
_MINUTES_PER_HOUR = 60
_USAGE_KIND_LABELS = {
    JevUsageKind.TRACKS: "曲の判定",
    JevUsageKind.ALBUMS: "子供向けのアルバムの判定",
    JevUsageKind.CUSTOM_MOOD: "自由記述の雰囲気",
}
# ジャンルを判断できる確率の閾値を下げた場合に、判定済みになる曲数を試算する値（高い順）
_RELAXED_DETERMINABLE_THRESHOLDS = (0.4, 0.3)
# 確率・confidence の分布を数える区間の幅
_HISTOGRAM_STEP = 0.1
_MATCH_LABELS = {
    MatchStatus.MATCHED: "対応あり",
    MatchStatus.UNMATCHED: "対応なし",
    MatchStatus.AMBIGUOUS: "候補が複数",
}


@dataclass(frozen=True)
class ReportContext:
    """レポートの対象。

    Attributes:
        library_root: 現在の音楽ライブラリのルート。未設定なら None。
        volumio_host: Volumio の接続先。レポートには出さず、保存済みの曲の一覧の検索だけに使う。
        project_root: アプリのフォルダ。Git のコミットの特定に使う。
    """

    library_root: str | None
    volumio_host: str | None
    project_root: Path


def build_report(conn: sqlite3.Connection, context: ReportContext) -> str:
    """確認用レポートを Markdown で作成する。DB を読むだけで、外部とは通信しない。"""
    lines = [
        "## 確認用レポート",
        "",
        "曲名・アーティスト名・フォルダ名・Volumio の接続先は含めていません。日時は UTC です。",
        "",
        *_environment_section(conn, context.project_root),
    ]
    root = context.library_root
    if root is None:
        lines += ["", "音楽フォルダが未設定のため、ライブラリの集計はありません。"]
        return "\n".join(lines) + "\n"

    if root == DEMO_LIBRARY_ROOT:
        lines += ["", "**注意: サンプルデータを表示中です（実際の音楽ライブラリの集計ではありません）。**"]

    tracks = list_tracks(conn, root)
    track_ids = {t.id for t in tracks}
    rules = _results(analysis_store.load_classifications(conn, RULES_ENGINE, RULE_VERSION), track_ids)
    jev = _results(analysis_store.load_classifications(conn, JEV_ENGINE, JEV_QUESTION_VERSION), track_ids)
    for section in (
        _scan_section(conn, root, tracks),
        _analysis_section(conn, root, tracks),
        _tag_section(tracks),
        _volumio_section(conn, context.volumio_host, tracks),
        _jev_section(conn, root, rules, jev),
        _unknown_genre_section(tracks, rules, jev, settings_store.get_jev_settings(conn)),
    ):
        lines += ["", *section]

    return "\n".join(lines) + "\n"


def _environment_section(conn: sqlite3.Connection, project_root: Path) -> list[str]:
    return [
        "### 環境",
        "",
        f"- 作成日時: {utc_now()}",
        f"- アプリのコミット: {git_revision(project_root) or '不明'}",
        f"- OS: {platform.system()} {platform.release()}（{platform.machine()}）",
        f"- Python: {platform.python_version()}",
        f"- DB のスキーマ: {current_version(conn)}",
        f"- 解析・分類のバージョン: {ANALYSIS_VERSION} / {RULE_VERSION} / {JEV_QUESTION_VERSION} / "
        f"{JEV_MOOD_QUESTION_VERSION} / {JEV_ALBUM_QUESTION_VERSION}",
    ]


def git_revision(project_root: Path) -> str | None:
    """アプリのフォルダの Git のコミット（先頭 7 文字）を返す。Git で取得したフォルダでなければ None。

    利用する PC に Git のコマンドがあるとは限らないため、`.git` の中のファイルを直接読む。
    """
    git_dir = project_root / ".git"
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref: "):
            return head[:7] or None

        ref = head.removeprefix("ref: ")
        ref_path = git_dir / ref
        if ref_path.is_file():
            return ref_path.read_text(encoding="utf-8").strip()[:7] or None

        # git gc の後は、ブランチの指す先が packed-refs にだけ残る
        for line in (git_dir / "packed-refs").read_text(encoding="utf-8").splitlines():
            sha, _, name = line.partition(" ")
            if name == ref:
                return sha[:7]
    except OSError:
        return None

    return None


def _scan_section(conn: sqlite3.Connection, root: str, tracks: list[Track]) -> list[str]:
    missing = conn.execute(
        "SELECT COUNT(*) FROM tracks WHERE library_root = ? AND status = 'missing'", (root,)
    ).fetchone()[0]
    extensions = Counter(PurePosixPath(t.rel_path).suffix.lower() or "（拡張子なし）" for t in tracks)
    lines = [
        "### スキャン",
        "",
        f"- 登録されている曲: {len(tracks)} 曲（見つからなくなった曲: {missing} 曲）",
        f"- 形式: {_counts(extensions.most_common())}",
    ]
    rows = conn.execute(
        "SELECT * FROM scans WHERE library_root = ? ORDER BY id DESC LIMIT ?", (root, _RECENT_SCANS)
    ).fetchall()
    if not rows:
        return [*lines, "- スキャンの履歴: なし"]

    lines += [
        "",
        f"直近 {len(rows)} 回のスキャン（新しい順）:",
        "",
        "| 完了日時 | 追加 | 更新 | 変更なし | 見つからない | エラー | 所要時間 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    lines += [
        f"| {row['finished_at']} | {row['added']} | {row['updated']} | {row['unchanged']} | {row['missing']} "
        f"| {row['errors']} | {_duration(row['elapsed_sec'])} |"
        for row in rows
    ]
    return lines


def _analysis_section(conn: sqlite3.Connection, root: str, tracks: list[Track]) -> list[str]:
    stats = dashboard_stats(conn, root, ClassificationSource.RULES)
    timing = conn.execute(
        """
        SELECT COUNT(*) AS n, SUM(f.elapsed_sec) AS total, AVG(f.elapsed_sec) AS mean, MAX(f.elapsed_sec) AS longest,
               MIN(f.analyzed_at) AS first_at, MAX(f.analyzed_at) AS last_at
        FROM audio_features f JOIN tracks t ON t.id = f.track_id
        WHERE t.library_root = ? AND t.status = 'active' AND f.analysis_version = ?
        """,
        (root, ANALYSIS_VERSION),
    ).fetchone()
    unsupported = Counter(
        PurePosixPath(t.rel_path).suffix.lower() or "（拡張子なし）" for t in tracks if not is_analyzable(t)
    )
    lines = [
        "### 音響解析",
        "",
        f"- 解析済み {stats.analyzed} 曲 / 未解析 {stats.unanalyzed} 曲 / 失敗 {stats.failed} 曲 / "
        f"未対応の形式 {stats.unsupported} 曲",
    ]
    if unsupported:
        lines.append(f"- 未対応の形式の内訳: {_counts(unsupported.most_common())}")

    if timing["n"]:
        lines += [
            f"- 1 曲あたりの解析時間: 平均 {_duration(timing['mean'])}、最長 {_duration(timing['longest'])}"
            f"（合計 {_duration(timing['total'])}）",
            f"- 解析した期間: {timing['first_at']} 〜 {timing['last_at']}",
        ]

    errors = conn.execute(
        """
        SELECT e.stage, e.error_type, COUNT(*) AS n FROM analysis_errors e
        LEFT JOIN tracks t ON t.id = e.track_id
        WHERE e.resolved = 0 AND (t.library_root = ? OR e.track_id IS NULL)
        GROUP BY e.stage, e.error_type ORDER BY n DESC, e.stage, e.error_type
        """,
        (root,),
    ).fetchall()
    if not errors:
        return [*lines, "- 未解決のエラー: なし"]

    lines += ["", "未解決のエラーの種類:", "", "| 段階 | 種類 | 件数 |", "|---|---|---:|"]
    lines += [f"| {row['stage']} | {row['error_type']} | {row['n']} |" for row in errors]
    return lines


def _tag_section(tracks: list[Track]) -> list[str]:
    def has_replacement_char(track: Track) -> bool:
        return any(_REPLACEMENT_CHAR in (v or "") for v in (track.title, track.artist, track.album, track.genre))

    return [
        "### タグ",
        "",
        f"- タグを読めなかった曲: {sum(1 for t in tracks if t.tag_error is not None)} 曲",
        f"- 文字化けを修復した曲: {sum(1 for t in tracks if t.tags_repaired)} 曲",
        f"- 曲名をファイル名から補った曲: {sum(1 for t in tracks if t.title_source == TitleSource.FILENAME)} 曲",
        f"- 読めない文字（�）が残っている曲: {sum(1 for t in tracks if has_replacement_char(t))} 曲",
    ]


def _volumio_section(conn: sqlite3.Connection, host: str | None, tracks: list[Track]) -> list[str]:
    lines = ["### Volumio との照合", ""]
    if host is None:
        return [*lines, "- 接続先: 未設定"]

    uris = load_uris(conn, host)
    if not uris:
        return [*lines, "- 接続先: 設定済み", "- Volumio の曲の一覧: 未取得"]

    statuses = Counter(m.status for m in match_tracks_to_uris(tracks, uris))
    lines += [
        "- 接続先: 設定済み",
        f"- Volumio の曲の一覧: {len(uris)} 曲（取得日時 {last_collected_at(conn, host)}）",
        "- ライブラリ全体の照合: " + "、".join(f"{label} {statuses[s]} 曲" for s, label in _MATCH_LABELS.items()),
    ]
    registrations = conn.execute(
        "SELECT client_kind, status, COUNT(*) AS n FROM volumio_registrations GROUP BY client_kind, status"
    ).fetchall()
    if registrations:
        lines.append(
            "- プレイリストの登録の履歴: "
            + "、".join(f"{row['client_kind']} {row['status']} {row['n']} 回" for row in registrations)
        )

    return lines


def _jev_section(
    conn: sqlite3.Connection,
    root: str,
    rules: dict[int, ClassificationResult],
    jev: dict[int, ClassificationResult],
) -> list[str]:
    albums = conn.execute(
        "SELECT COUNT(*) FROM album_judgments WHERE library_root = ? AND question_version = ?",
        (root, JEV_ALBUM_QUESTION_VERSION),
    ).fetchone()[0]
    lines = [
        "### Jev",
        "",
        f"- 判定済み: {len(jev)} 曲（現在の質問のバージョン）、子供向けのアルバムの判定: {albums} アルバム",
    ]
    if jev:
        held = Counter(a for r in jev.values() for a in ATTRIBUTE_LABELS if r.attribute_value(a) is None)
        lines += [
            f"- ジャンルが不明: {sum(1 for r in jev.values() if r.genre is None)} 曲、"
            f"ムードが判定保留: {sum(1 for r in jev.values() if r.mood is None)} 曲",
            "- 判定保留になった属性: "
            + (_counts((ATTRIBUTE_LABELS[a], n) for a, n in held.most_common()) if held else "なし"),
            *_genre_comparison(rules, jev),
        ]

    return [*lines, *_usage_table(conn)]


def _results(
    stored: dict[int, analysis_store.StoredClassification], track_ids: set[int]
) -> dict[int, ClassificationResult]:
    return {track_id: c.result for track_id, c in stored.items() if track_id in track_ids}


def _genre_comparison(rules: dict[int, ClassificationResult], jev: dict[int, ClassificationResult]) -> list[str]:
    both = [(rules[i].genre, jev[i].genre) for i in jev.keys() & rules.keys()]
    known = [(r, j) for r, j in both if r is not None and j is not None]
    agreement = f"{sum(1 for r, j in known if r == j)} / {len(known)} 曲" if known else "比較できる曲なし"
    rule_counts = Counter(r for r, _ in both)
    jev_counts = Counter(j for _, j in both)
    genres: list[Genre | None] = [*Genre, None]
    lines = [
        f"- ルールベースとジャンルが一致した曲（両方で判定済み・どちらも不明でない曲のうち）: {agreement}",
        "",
        f"両方で判定済みの {len(both)} 曲のジャンルの分布:",
        "",
        "| ジャンル | ルールベース | Jev |",
        "|---|---:|---:|",
    ]
    lines += [
        f"| {UNKNOWN_GENRE_LABEL if g is None else GENRE_LABELS[g]} | {rule_counts[g]} | {jev_counts[g]} |"
        for g in genres
        if rule_counts[g] or jev_counts[g]
    ]
    return lines


def _unknown_genre_section(
    tracks: list[Track],
    rules: dict[int, ClassificationResult],
    jev: dict[int, ClassificationResult],
    settings: JevSettings,
) -> list[str]:
    """ジャンルが不明になった原因の内訳。保存済みの結果から計算し、Jev には問い合わせない。

    タグの表記そのものはライブラリの内容にあたるため出さず、件数と表記の種類の数だけを出す。
    """
    return [
        "### ジャンルが不明の内訳",
        "",
        *_rules_unknown_breakdown(tracks, rules),
        "",
        *_jev_unknown_breakdown(jev, settings),
        "",
        *_fill_estimate(tracks, rules, jev),
    ]


def _rules_unknown_breakdown(tracks: list[Track], rules: dict[int, ClassificationResult]) -> list[str]:
    unknown_ids = {i for i, r in rules.items() if r.genre is None}
    lines = [f"ルールベース（判定済み {len(rules)} 曲のうち不明 {len(unknown_ids)} 曲）:", ""]
    tags = [(t.genre or "").strip() for t in tracks if t.id in unknown_ids]
    empty = sum(1 for tag in tags if not tag)
    unmatched = [tag for tag in tags if tag and genre_from_tag(tag) is None]
    lines += [
        f"- ジャンルタグが空: {empty} 曲",
        f"- タグはあるが候補に当てはまらない: {len(unmatched)} 曲（表記 {len(set(unmatched))} 種類）",
    ]
    # 分類した後にタグが変わり、まだ分類し直していない曲
    stale = len(tags) - empty - len(unmatched)
    if stale:
        lines.append(f"- タグは候補に当てはまる（分類した後にタグが変わった可能性）: {stale} 曲")

    return lines


def _fill_estimate(
    tracks: list[Track], rules: dict[int, ClassificationResult], jev: dict[int, ClassificationResult]
) -> list[str]:
    """ルールベースが不明の曲のうち、Jev の判定や同じアルバムの曲のジャンルで補える曲数を試算する。

    補う方法を決めるための数字で、結果は変更しない。アルバムで補う場合は、アルバム内の判定済みの曲の
    ジャンルが 1 種類か（コンピレーションなどで複数に分かれていないか）も数え、誤判定の起きやすさの目安にする。
    """
    unknown = [t for t in tracks if t.id in rules and rules[t.id].genre is None]
    lines = [f"不明を補える曲の試算（ルールベースが不明の {len(unknown)} 曲のうち。結果は変更していない）:", ""]
    if not unknown:
        return [*lines, "- 不明の曲なし"]

    jev_genres = {i: r.genre for i, r in jev.items() if r.genre is not None}
    rule_genres = {i: r.genre for i, r in rules.items() if r.genre is not None}
    # ルールベースを優先し、不明なら Jev の判定で補ったジャンル
    combined_genres = {**jev_genres, **rule_genres}
    by_jev = [t for t in unknown if t.id in jev_genres]
    jev_unknown = sum(1 for t in unknown if t.id in jev and t.id not in jev_genres)
    rest = [t for t in unknown if t.id not in jev_genres]
    lines += [
        f"- Jev がジャンルを判定済み: {len(by_jev)} 曲"
        f"（Jev も不明: {jev_unknown} 曲、Jev で未判定: {len(unknown) - len(by_jev) - jev_unknown} 曲）",
        *_album_fill_lines("同じアルバムに、ルールベースでジャンルが決まった曲がある", unknown, tracks, rule_genres),
        *_album_fill_lines(
            "Jev で補えない曲のうち、同じアルバムにルールベースか Jev でジャンルが決まった曲がある",
            rest,
            tracks,
            combined_genres,
        ),
    ]
    # ジャンルが複数に分かれるアルバムは、どれで補うか決まらないため含めない
    filled = len(by_jev) + _album_fill_counts(rest, tracks, combined_genres)[1]
    lines.append(
        f"- Jev と、ジャンルが 1 種類のアルバムで補った場合: 不明 {len(unknown)} 曲 → {len(unknown) - filled} 曲"
    )
    return lines


def _album_fill_lines(label: str, targets: list[Track], tracks: list[Track], known: dict[int, Genre]) -> list[str]:
    fillable, single = _album_fill_counts(targets, tracks, known)
    return [f"- {label}: {fillable} 曲（うちアルバム内のジャンルが 1 種類: {single} 曲）"]


def _album_fill_counts(targets: list[Track], tracks: list[Track], known: dict[int, Genre]) -> tuple[int, int]:
    """同じアルバムにジャンルが決まった曲がある曲数と、そのうちアルバム内のジャンルが 1 種類の曲数。"""
    album_genres: dict[str, set[Genre]] = {}
    for track in tracks:
        if track.id in known:
            album_genres.setdefault(album_key(track), set()).add(known[track.id])

    genres = [album_genres.get(album_key(t), set()) for t in targets]
    return sum(1 for g in genres if g), sum(1 for g in genres if len(g) == 1)


def _jev_unknown_breakdown(jev: dict[int, ClassificationResult], settings: JevSettings) -> list[str]:
    """Jev でジャンルが不明になった原因の内訳。

    閾値は設定画面で変えられ、判定した時点の値は結果に残っていない。そのため原因は、判定時に残した
    notes と confidence だけで分け、現在の設定は閾値を下げた場合の試算にだけ使う。
    """
    unknown = [r for r in jev.values() if r.genre is None]
    lines = [f"Jev（判定済み {len(jev)} 曲のうち不明 {len(unknown)} 曲）:", ""]
    if not unknown:
        return [*lines, "- 不明の曲なし"]

    # 判断材料が不足した曲の確率と confidence。confidence は不明にした場合も保存している
    undeterminable: list[tuple[float, float | None]] = []
    low_confidence: list[float] = []
    for r in unknown:
        determinable = recorded_genre_determinable(r)
        if determinable is not None:
            undeterminable.append((determinable, r.genre_confidence))
        elif r.genre_confidence is not None:
            # 判断材料が足りたのに不明になるのは、confidence が閾値未満だった場合だけ
            low_confidence.append(r.genre_confidence)

    lines += [
        f"- 判断材料が不足（genre_determinable の確率が閾値未満）: {len(undeterminable)} 曲",
        f"- ジャンルの confidence が閾値未満: {len(low_confidence)} 曲",
    ]
    unexplained = len(unknown) - len(undeterminable) - len(low_confidence)
    if unexplained:
        lines.append(f"- 理由を特定できない: {unexplained} 曲")

    lines.append(
        f"- 現在の設定: genre_determinable の閾値 {settings.genre_determinable_threshold}、"
        f"confidence の閾値 {settings.min_confidence}（判定した時点の設定とは異なる場合がある）"
    )
    if undeterminable:
        lines += [
            "",
            "判断材料が不足した曲の genre_determinable の確率（notes に残っている小数 2 桁の値。"
            "丸めにより、閾値ちょうどの値は閾値未満の値を含む）:",
            "",
            *_histogram([p for p, _ in undeterminable]),
            *_relaxed_thresholds(undeterminable, len(unknown), settings),
        ]

    if low_confidence:
        lines += ["", "confidence が不足した曲のジャンルの confidence:", "", *_histogram(low_confidence)]

    return lines


def _relaxed_thresholds(
    undeterminable: list[tuple[float, float | None]], unknown: int, settings: JevSettings
) -> list[str]:
    """genre_determinable の閾値を現在の設定より下げた場合に、判定済みになる曲数を試算する。"""
    thresholds = [t for t in _RELAXED_DETERMINABLE_THRESHOLDS if t < settings.genre_determinable_threshold]
    if not thresholds:
        return []

    lines = [
        "",
        "genre_determinable の閾値を下げた場合に判定済みになる曲（confidence が現在の閾値 "
        f"{settings.min_confidence} 以上の曲に限る）:",
        "",
    ]
    for threshold in thresholds:
        recovered = sum(
            1 for p, c in undeterminable if p >= threshold and c is not None and c >= settings.min_confidence
        )
        lines.append(f"- {threshold}: {recovered} 曲（不明 {unknown} 曲 → {unknown - recovered} 曲）")

    return lines


def _histogram(values: list[float]) -> list[str]:
    """0〜1 の値を、幅 `_HISTOGRAM_STEP` の区間ごとに数えた表を返す。最後の区間は 1 を含む。"""
    # 0.1 * 3 のような誤差で 0.3 ちょうどの値が下の区間に入らないよう、小数 2 桁に丸める
    edges = [round(i * _HISTOGRAM_STEP, 2) for i in range(round(1 / _HISTOGRAM_STEP) + 1)]
    lines = ["| 範囲 | 曲数 |", "|---|---:|"]
    for low, high in pairwise(edges):
        is_last = high == edges[-1]
        count = sum(1 for v in values if low <= v < high or (is_last and v == high))
        lines.append(f"| {low:.1f} 以上 {high:.1f} {'以下' if is_last else '未満'} | {count} |")

    return lines


def _usage_table(conn: sqlite3.Connection) -> list[str]:
    records = list_usage(conn, limit=_RECENT_JEV_RUNS)
    if not records:
        return []

    lines = [
        "",
        f"直近 {len(records)} 回の Jev の実行（新しい順）:",
        "",
        "| 完了日時 | 種類 | モデル | 成功 | 失敗 | 入力トークン | 出力トークン | 成功 1 件あたり |",
        "|---|---|---|---:|---:|---:|---:|---:|",
    ]
    for r in records:
        per_item = f"{(r.input_tokens + r.output_tokens) / r.succeeded:.0f}" if r.succeeded else "-"
        lines.append(
            f"| {r.finished_at} | {_USAGE_KIND_LABELS[r.kind]} | {r.model} | {r.succeeded} | {r.failed} "
            f"| {r.input_tokens} | {r.output_tokens} | {per_item} |"
        )

    return lines


def _counts(items: Iterable[tuple[str, int]]) -> str:
    text = "、".join(f"{label} {n}" for label, n in items)
    return text or "なし"


def _duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"

    if seconds < _SECONDS_PER_MINUTE:
        return f"{seconds:.1f} 秒"

    minutes, sec = divmod(round(seconds), _SECONDS_PER_MINUTE)
    if minutes < _MINUTES_PER_HOUR:
        return f"{minutes} 分 {sec} 秒"

    hours, minutes = divmod(minutes, _MINUTES_PER_HOUR)
    return f"{hours} 時間 {minutes} 分"
