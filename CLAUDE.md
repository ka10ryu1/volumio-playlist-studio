# CLAUDE.md

このリポジトリの開発規約は [AGENTS.md](AGENTS.md) にまとめています。作業前に必ず読み、規約に従ってください。

@AGENTS.md

## コードスタイル（AGENTS.md への追加）

- インデントが戻る場所（if / for / while / with / try / match などのブロックの終了後）には空行を入れる。ネストの深さは問わず、ループの中やガード節（早期の return / continue / raise）の後にも入れる。ただし else / elif / except / finally / case など、同じ構文の続きを表すキーワードの前には入れない
  - `uv run python tools/insert_block_blank_lines.py src tests tools` で機械的に挿入できる（`--check` で確認のみ）
  - `tests/test_block_spacing.py` がリポジトリ全体を検査するため、守られていないと `uv run pytest` が失敗する
  - `ruff format` の後に実行する（ruff format はこの空行を削除しない）

## このプロジェクト固有の注意点

### 構成

- `src/volumio_playlist_studio/` 直下に解析・分類・選曲などのロジック、`storage/` に SQLite の読み書き、`gui/` に Streamlit の画面を置く。責務の分け方は [docs/architecture.md](docs/architecture.md) を参照
- 画面から呼ぶ一連の処理は `workflows.py` にまとめ、GUI に依存させない（バックグラウンドスレッドから実行し、テストもしやすくするため）
- 画面のモジュールは `gui/views/` に置く。`gui/pages/` という名前にすると、Streamlit が旧方式のマルチページとして扱い `st.navigation` と衝突する

### バージョンの更新

`config.py` の次の定数は、出力が変わる変更をしたら必ず更新する。評価データがどの結果に対するものかを追跡するために DB に記録している。

- `ANALYSIS_VERSION`: 音響特徴量の抽出方法を変えたとき（更新すると全曲が再解析の対象になる）
- `RULE_VERSION`: ルールベース分類の計算を変えたとき
- `JEV_QUESTION_VERSION`: Jev の質問文・候補・特徴量の区分を変えたとき

### 外部サービス

- Volumio 実機とは、読み取りを REST API（`volumio_rest.py`）、プレイリストの作成・曲の追加を WebSocket（Socket.io）API（`volumio_socketio.py`）で行う。Socket.io は socket.io 2.x（Volumio 3）と 3.x 以降の両方に対応させている（[docs/volumio.md](docs/volumio.md)）。Volumio の状態を変更する通信は、ユーザーの承認なしに追加しない
- Jev は公式 SDK（`typesafe-sdk`）を使う。テストでは `httpx2.MockTransport` で HTTP 層だけを差し替え、実 API とは通信しない
- 実 API を使った検証は料金が発生するため、ユーザーの指示なしに実行しない

### 動作環境と起動

- 開発は WSL、実際の利用は Windows に直接クローンして `setup.cmd` → `start.cmd`（ダブルクリック）で起動する想定。Windows 固有の処理（パス、ランチャー）を変えるときは両方の環境を意識する
- `.env` とデータの保存先はプロジェクト直下を基準にする（`config.PROJECT_ROOT`）。ダブルクリック起動ではカレントフォルダに依存できないため
- 起動処理は `launcher.py` にまとめ、`scripts/windows/*.ps1` からはそれを呼ぶだけにする。`.ps1` は Windows PowerShell 5.1 で日本語を読めるよう BOM 付き UTF-8・CRLF で保存する
- CI の `windows-setup-and-start` ジョブが、`tests/windows/test_setup_and_start.ps1`（セットアップ → 起動 → ヘルスチェック → 停止）を Windows PowerShell 5.1 で実行する。Windows 用のスクリプトや起動処理を変えたら、手元の Windows でもこのスクリプトを実行して確認する

```bash
uv run python -m volumio_playlist_studio.launcher   # WSL・Linux
```

- LAN モード（`VPS_LAN=1`）では、localhost と判定できない接続に PIN を求める（`gui/lan.py`）。PIN 画面より前に、ライブラリの内容を表示する処理を置かない

GUI のテストは `streamlit.testing.v1.AppTest` で行う（`tests/test_gui.py`）。画面単位のテストは、`view_script` で 1 画面だけを表示して操作する。
