"""README と docs/SCREENSHOTS.md に載せる画面キャプチャを撮る。

架空のデモデータ（`sample_data.load_demo_library`）を一時フォルダの DB に登録して Streamlit を起動し、
Playwright で各画面を撮影して `docs/images/` に保存する。利用者の `data/` や `.env` の接続先・API キーは使わない。

    uv run playwright install chromium   # 初回のみ
    uv run python tools/capture_screenshots.py
"""

import argparse
import logging
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from playwright.sync_api import Browser, Page, ViewportSize, sync_playwright

from volumio_playlist_studio.classify.rules import ENGINE_NAME as RULES_ENGINE
from volumio_playlist_studio.config import (
    DATA_DIR_ENV,
    LAN_ADDRESS_ENV,
    LAN_ENV,
    PORT_ENV,
    PROJECT_ROOT,
    TYPESAFE_API_KEY_ENV,
    VOLUMIO_HOST_ENV,
    EvaluationDefaults,
    load_app_config,
)
from volumio_playlist_studio.evaluation import select_evaluation_sample
from volumio_playlist_studio.launcher import LOOPBACK, build_command, streamlit_running
from volumio_playlist_studio.models import Genre, Mood
from volumio_playlist_studio.sample_data import DEMO_LIBRARY_ROOT, load_demo_library
from volumio_playlist_studio.storage import analysis as analysis_store
from volumio_playlist_studio.storage import settings_store
from volumio_playlist_studio.storage.database import connect, transaction
from volumio_playlist_studio.storage.evaluations import EvaluationLabel, Verdict, add_to_evaluation_set, save_label
from volumio_playlist_studio.storage.tracks import list_tracks

logger = logging.getLogger(__name__)

DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "docs" / "images"
# PC・タブレット縦向き・スマートフォンの幅で標準レイアウトを確認する。
DESKTOP_VIEWPORT = ViewportSize(width=1280, height=900)
TABLET_VIEWPORT = ViewportSize(width=768, height=1024)
MOBILE_VIEWPORT = ViewportSize(width=390, height=844)
DEVICE_SCALE_FACTOR = 2
STARTUP_TIMEOUT_SEC = 60.0
RENDER_TIMEOUT_MS = 30_000
# グラフ（Vega）は描画完了を DOM から判定しにくいため、スクリプトの実行完了後に少し待つ
SETTLE_MS = 1_500
PLAYLIST_SEED = 2024
PLAYLIST_NAME = "リラックス（サンプル）"
# 評価対象のうち評価済みにする曲数。未評価の曲も残し、画面の「評価済み n 曲」が分かるようにする
EVALUATED_COUNT = 24


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind((LOOPBACK, 0))
        port: int = sock.getsockname()[1]
        return port


def prepare_database(database_path: Path) -> None:
    """架空の楽曲・分類結果・人手評価を DB に登録し、サンプルデータを表示する状態にする。"""
    conn = connect(database_path)
    try:
        load_demo_library(conn)
        with transaction(conn):
            settings_store.set_value(conn, settings_store.MUSIC_ROOT_KEY, DEMO_LIBRARY_ROOT)

        _seed_evaluations(conn)
    finally:
        conn.close()


def _seed_evaluations(conn: sqlite3.Connection) -> None:
    """精度評価画面の指標が表示されるよう、決まったパターンで人手評価を登録する。"""
    defaults = EvaluationDefaults()
    tracks = list_tracks(conn, DEMO_LIBRARY_ROOT)
    assignments = select_evaluation_sample(tracks, set(), defaults.default_sample_size, 0, defaults)
    classifications = analysis_store.load_classifications(conn, RULES_ENGINE)
    genres = list(Genre)
    moods: list[Mood] = [m for m in Mood if m != Mood.OMAKASE]
    with transaction(conn):
        add_to_evaluation_set(conn, assignments)
        for index, track_id in enumerate(sorted(assignments)[:EVALUATED_COUNT]):
            stored = classifications.get(track_id)
            if stored is None:
                continue

            genre_verdict, genre = _verdict(index, 4, stored.result.genre, genres)
            mood_verdict, mood = _verdict(index, 3, stored.result.mood, moods)
            save_label(
                conn,
                EvaluationLabel(
                    track_id=track_id,
                    split=assignments[track_id],
                    classification_id=stored.id,
                    classification_snapshot=stored.result,
                    genre_verdict=genre_verdict,
                    genre_label=genre,
                    mood_verdict=mood_verdict,
                    mood_label=mood,
                ),
            )


def _verdict[T: (Genre, Mood)](
    index: int, every: int, predicted: T | None, options: list[T]
) -> tuple[Verdict, T | None]:
    """`every` 曲に 1 曲を「誤り」、それ以外を「正しい」とする。判定がない曲は「判定保留」にする。"""
    if predicted is None:
        return Verdict.PENDING, None

    if index % every != every - 1:
        return Verdict.CORRECT, predicted

    return Verdict.INCORRECT, next(o for o in options if o != predicted)


@contextmanager
def run_app(data_dir: Path) -> Iterator[str]:
    """一時フォルダの DB を使って Streamlit を起動し、接続先の URL を返す。"""
    port = _free_port()
    env = dict(os.environ)
    # .env は既存の環境変数を上書きしないため、空文字を設定して利用者の接続先・API キー・LAN 設定を読ませない
    env.update(
        {
            DATA_DIR_ENV: str(data_dir),
            PORT_ENV: str(port),
            TYPESAFE_API_KEY_ENV: "",
            VOLUMIO_HOST_ENV: "",
            LAN_ENV: "",
            LAN_ADDRESS_ENV: "",
        }
    )
    # 開発者向けの「Deploy」ボタンなどは利用者の操作に関係しないため、キャプチャでは表示しない
    command = [*build_command(load_app_config(env)), "--client.toolbarMode", "viewer"]
    process = subprocess.Popen(command, cwd=PROJECT_ROOT, env=env, stdout=subprocess.DEVNULL)  # noqa: S603 - 引数は固定値と設定値のみ
    try:
        deadline = time.monotonic() + STARTUP_TIMEOUT_SEC
        while not streamlit_running(port):
            if process.poll() is not None:
                raise RuntimeError(f"streamlit exited: code={process.returncode}")

            if time.monotonic() > deadline:
                raise TimeoutError("streamlit did not start")

            time.sleep(0.5)

        logger.info("streamlit started: port=%d", port)
        yield f"http://{LOOPBACK}:{port}"
    finally:
        process.terminate()
        process.wait(timeout=15)


def wait_for_app(page: Page) -> None:
    """Streamlit のスクリプトの実行が終わり、画面の描画が落ち着くまで待つ。"""
    page.wait_for_selector(
        "[data-testid='stApp'][data-test-script-state='notRunning']", state="attached", timeout=RENDER_TIMEOUT_MS
    )
    page.wait_for_timeout(SETTLE_MS)


def save_full_page(page: Page, path: Path) -> None:
    """画面全体を撮る。

    Streamlit はページ全体ではなく本文の要素がスクロールするため、`full_page` では見えている範囲しか撮れない。
    本文の高さに合わせてビューポートを広げてから撮り、元の大きさに戻す。
    """
    # タブ切り替えや折りたたみの展開直後は、アニメーション完了後の高さで撮影する。
    wait_for_app(page)
    check_layout(page)
    viewport = page.viewport_size
    if viewport is None:
        raise RuntimeError("viewport size is not set")

    height = page.evaluate("document.querySelector(\"[data-testid='stMain']\").scrollHeight")
    page.set_viewport_size({"width": viewport["width"], "height": max(int(height), viewport["height"])})
    wait_for_app(page)
    page.screenshot(path=path)
    page.set_viewport_size(viewport)
    logger.info("screenshot saved: file=%s", path.name)


def save_viewport(page: Page, path: Path) -> None:
    """見えている範囲だけを撮る。"""
    check_layout(page)
    page.screenshot(path=path)
    logger.info("screenshot saved: file=%s", path.name)


def open_page(page: Page, base_url: str, url_path: str) -> None:
    """画面を開き、描画が終わるまで待つ。"""
    page.goto(f"{base_url}/{url_path}")
    wait_for_app(page)


def fill_seed(page: Page) -> None:
    """選曲の seed を固定する。初期値は画面を開いた時刻で変わるため、撮り直しても同じ画像になるようにする。"""
    page.get_by_text("詳細設定", exact=True).click()
    page.get_by_label("seed（同じ値なら同じ結果を再現します）").fill(str(PLAYLIST_SEED))
    page.get_by_text("詳細設定", exact=True).click()


def create_playlist(page: Page) -> None:
    """プレイリスト作成画面で、決まった seed でプレイリストを生成・保存する。"""
    fill_seed(page)
    page.get_by_role("button", name="プレイリストを生成").click()
    wait_for_app(page)
    page.get_by_role("tab", name="3. 保存・連携").click()
    page.get_by_label("プレイリスト名", exact=True).fill(PLAYLIST_NAME)
    page.get_by_label("プレイリスト名", exact=True).press("Enter")
    wait_for_app(page)
    page.get_by_role("button", name="プレイリストを保存").click()
    wait_for_app(page)


def check_layout(page: Page) -> None:
    """本文全体の横あふれ・タイトルの折り返し・画面内の例外を検出する。表内のスクロールは許容する。"""
    if page.locator("[data-testid='stException']").count():
        raise RuntimeError("app rendered an exception")

    layout = page.evaluate("""() => {
        const main = document.querySelector('[data-testid="stMain"]');
        const title = document.querySelector('h1');
        const style = getComputedStyle(title);
        return {width: main.clientWidth, scrollWidth: main.scrollWidth,
                titleHeight: title.getBoundingClientRect().height
                    - parseFloat(style.paddingTop) - parseFloat(style.paddingBottom),
                titleLineHeight: parseFloat(style.lineHeight)};
    }""")
    if layout["scrollWidth"] > layout["width"]:
        raise RuntimeError(f"page overflow: {layout}")

    if layout["titleHeight"] > layout["titleLineHeight"] * 1.5:
        raise RuntimeError(f"title wraps: {layout}")

    logger.info("layout checked: width=%d scroll_width=%d", layout["width"], layout["scrollWidth"])


def capture_pages(page: Page, base_url: str, output_dir: Path, suffix: str) -> None:
    """各画面と、プレイリストの条件・編集・保存の状態を撮る。"""
    for route, name in (
        ("", "dashboard"),
        ("library", "library"),
        ("evaluation", "evaluation"),
        ("settings", "settings"),
    ):
        open_page(page, base_url, route)
        save_full_page(page, output_dir / f"{name}{suffix}.png")

    open_page(page, base_url, "playlist")
    save_full_page(page, output_dir / f"playlist-conditions{suffix}.png")
    save_viewport(page, output_dir / ("playlist-top.png" if not suffix else f"playlist{suffix}.png"))
    generate_button = page.get_by_role("button", name="プレイリストを生成").bounding_box()
    viewport = page.viewport_size
    if (
        generate_button is None
        or viewport is None
        or generate_button["y"] + generate_button["height"] > viewport["height"]
    ):
        raise RuntimeError("generate button is outside the initial viewport")

    create_playlist(page)
    save_full_page(page, output_dir / f"playlist-save{suffix}.png")
    page.get_by_role("tab", name="2. 曲を編集").click()
    save_full_page(page, output_dir / ("playlist.png" if not suffix else f"playlist-edit{suffix}.png"))

    if not suffix:
        check_navigation_state(page)
        open_page(page, base_url, "evaluation")
        page.get_by_role("tab", name="集計・CSV").click()
        save_full_page(page, output_dir / "evaluation-metrics.png")
        page.get_by_role("tab", name="プレイリスト", exact=True).click()
        save_full_page(page, output_dir / "evaluation-playlist.png")
        open_page(page, base_url, "settings")
        page.get_by_role("tab", name="解析・AI").click()
        page.get_by_text("Jev API の設定", exact=True).click()
        page.get_by_text("高度な音響解析設定", exact=True).click()
        save_full_page(page, output_dir / "settings-advanced.png")


def check_navigation_state(page: Page) -> None:
    """同一セッションのページ移動で、保存名・条件・編集結果が維持されることを実ブラウザでも確認する。"""
    page.get_by_role("button", name="除外", exact=True).click()
    wait_for_app(page)
    selection = page.get_by_test_id("stSelectbox").filter(has=page.get_by_text("編集する曲", exact=True))
    before = selection.inner_text()
    page.get_by_role("tab", name="3. 保存・連携").click()
    page.get_by_label("プレイリスト名", exact=True).fill("移動後も保持する名前")
    page.get_by_label("プレイリスト名", exact=True).press("Enter")
    wait_for_app(page)
    page.get_by_role("link", name="ダッシュボード").click()
    wait_for_app(page)
    page.get_by_role("button", name="プレイリストを作成").click()
    wait_for_app(page)
    if selection.inner_text() != before:
        raise RuntimeError("playlist changed after navigation")

    page.get_by_role("tab", name="3. 保存・連携").click()
    if page.get_by_label("プレイリスト名", exact=True).input_value() != "移動後も保持する名前":
        raise RuntimeError("playlist name was lost after navigation")

    page.get_by_text("1. 選曲の条件", exact=True).click()
    page.get_by_text("詳細設定", exact=True).click()
    if page.get_by_label("seed（同じ値なら同じ結果を再現します）").input_value() != str(PLAYLIST_SEED):
        raise RuntimeError("playlist seed was lost after navigation")


def capture_desktop(browser: Browser, base_url: str, output_dir: Path) -> None:
    """PC の画面幅で各画面を撮る。"""
    context = browser.new_context(viewport=DESKTOP_VIEWPORT, device_scale_factor=DEVICE_SCALE_FACTOR, locale="ja-JP")
    try:
        capture_pages(context.new_page(), base_url, output_dir, "")
    finally:
        context.close()


def capture_mobile(browser: Browser, base_url: str, output_dir: Path, target: str = "all") -> None:
    """スマートフォン・タブレットの画面幅で各画面を撮る。"""
    for viewport, suffix in ((MOBILE_VIEWPORT, "-mobile"), (TABLET_VIEWPORT, "-tablet")):
        if target != "all" and suffix != f"-{target}":
            continue

        context = browser.new_context(
            viewport=viewport,
            device_scale_factor=DEVICE_SCALE_FACTOR,
            is_mobile=True,
            has_touch=True,
            locale="ja-JP",
        )
        try:
            capture_pages(context.new_page(), base_url, output_dir, suffix)
        finally:
            context.close()


def main(argv: list[str] | None = None) -> int:
    """デモデータで各画面を撮影する。"""
    parser = argparse.ArgumentParser(description="架空のデモデータで画面キャプチャを撮る")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_DIR, help="PNG の保存先")
    parser.add_argument(
        "--viewport",
        choices=["all", "desktop", "mobile", "tablet"],
        default="all",
        help="撮影する画面幅（既定は全サイズ）",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    output_dir: Path = args.output
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="vps-screenshots-") as tmp:
        data_dir = Path(tmp)
        prepare_database(load_app_config({DATA_DIR_ENV: str(data_dir)}).database_path)
        with run_app(data_dir) as base_url, sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            try:
                if args.viewport in {"all", "desktop"}:
                    capture_desktop(browser, base_url, output_dir)

                if args.viewport != "desktop":
                    capture_mobile(browser, base_url, output_dir, args.viewport)
            finally:
                browser.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
