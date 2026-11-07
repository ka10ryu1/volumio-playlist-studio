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

- Volumio 実機との通信は未実装。プレイリスト作成・曲追加は WebSocket（Socket.io）API にしかなく、方式をユーザーと決めてから実装する（[docs/volumio.md](docs/volumio.md)）
- Jev は公式 SDK（`typesafe-sdk`）を使う。テストでは `httpx2.MockTransport` で HTTP 層だけを差し替え、実 API とは通信しない
- 実 API を使った検証は料金が発生するため、ユーザーの指示なしに実行しない

### 起動と確認

```bash
uv run streamlit run src/volumio_playlist_studio/gui/app.py
```

GUI のテストは `streamlit.testing.v1.AppTest` で行う（`tests/test_gui.py`）。画面単位のテストは、`view_script` で 1 画面だけを表示して操作する。
