# volumio-playlist-studio

ローカルに保存した音楽を解析し、気分・ジャンル・テンポなどに応じたプレイリストを GUI から作成するアプリです。分類結果を人手で評価し、継続的に選曲品質を改善できるようにします。

Raspberry Pi 上の Volumio に保存した音楽を PC から SMB 共有経由で読み取り、PC 側で解析・選曲します。Volumio は音楽の保存と再生だけを担当し、Volumio 本体では解析を行いません。実行時に Claude は使わず、Python アプリと TypeSafe Jev API（任意）だけで動作します。

## 実装状況

| 機能 | 状況 |
|---|---|
| 音楽フォルダのスキャン（初回・差分、mutagen によるタグ取得、文字化け修復、重複検出） | 実装済み |
| librosa による音響特徴量の抽出（区間サンプリング、無音区間の除外、処理時間の記録） | 実装済み（MP3/FLAC/OGG/WAV/AIFF。M4A/AAC はタグのみ） |
| SQLite への保存（マイグレーション、解析ロジックのバージョン記録、再解析の抑制） | 実装済み |
| ルールベース分類（タグ＋音響特徴量、Baseline） | 実装済み |
| TypeSafe Jev による分類（件数の見積もり、上限、エラー時の打ち切り） | 実装済み。**実 API での動作は未確認**（モックした HTTP 応答でのみテスト） |
| プレイリスト生成・編集・保存（seed による再現、条件不足時の説明） | 実装済み |
| 精度評価（人手評価、調整用／検証用の分割、指標、CSV 出力、プレイリストの満足度） | 実装済み |
| Volumio へのプレイリスト登録 | **モッククライアントのみ**。実機との通信は未実装（[docs/volumio.md](docs/volumio.md)） |
| Essentia | 未導入（比較できる設計のみ。[docs/architecture.md](docs/architecture.md)） |

## 必要な環境

- Windows 11 + WSL2（Ubuntu など）。Linux でも動作します
- [uv](https://docs.astral.sh/uv/)
- Python 3.14（uv が `.python-version` に従って用意します）

## セットアップ

```bash
git clone <このリポジトリ>
cd volumio-playlist-studio
uv sync
cp .env.example .env   # Jev を使う場合は TYPESAFE_API_KEY を設定する
```

API キーは `.env` または環境変数で設定します。キーは画面にもログにも表示されません。未設定でも、ルールベースの分類で全機能（Jev 判定を除く）を使えます。

## 起動

```bash
uv run streamlit run src/volumio_playlist_studio/gui/app.py
```

Windows のブラウザで <http://localhost:8501> を開きます。初期設定ではこの PC からのみ接続できます（`.streamlit/config.toml`）。

音楽ファイルがなくても、画面の「サンプルデータで試す」で架空の楽曲 60 曲を読み込み、選曲から保存までの流れを試せます。

## 音楽フォルダの指定

画面の「音楽フォルダ」に、WSL から見えるフォルダの絶対パスを入力します。

- WSL 上のフォルダ: `/home/<ユーザー>/Music`
- Windows のフォルダ: `C:\Users\<ユーザー>\Music` と入力すると `/mnt/c/Users/<ユーザー>/Music` に変換されます
- Volumio の SMB 共有: UNC パス（`\\volumio.local\...`）は WSL から直接開けないため、先に読み取り専用でマウントします

```bash
# 共有名はエクスプローラーで \\volumio.local を開いて確認する（例: "Internal Storage"）
sudo mkdir -p /mnt/volumio
sudo mount -t drvfs '\\volumio.local\Internal Storage' /mnt/volumio -o ro
```

この手順は Volumio 実機では未確認です。マウントできない場合は、Windows でネットワークドライブ（例: `Z:`）に割り当ててから `sudo mount -t drvfs Z: /mnt/volumio -o ro` を試してください。

マウント後、`/mnt/volumio` を音楽フォルダとして指定します。アプリは音楽ファイルを読み取るだけで、タグの書き換えやファイルの移動・削除は行いません。

## 操作手順

1. **音楽ライブラリ**: 「スキャン」で楽曲を登録し、「未解析の曲を解析」で音響特徴量を求めます。2 回目以降は追加・更新されたファイルだけを処理します。解析中も画面は操作でき、進捗が表示されます
2. （任意）**音楽ライブラリ → Jev で判定する**: 対象曲数とリクエスト数を確認してから実行します。1 回の上限（既定 20 曲）は設定画面で変更できます
3. **プレイリスト作成**: 雰囲気をカードで選び、再生時間・ジャンル・意外性などを指定して生成します。曲の並び替え・除外・追加、名前の編集をして保存します。条件に合う曲が足りない場合は、理由と緩和案が表示され、「条件を緩和して再生成」を選べます
4. **精度評価**: 30〜50 曲を評価対象に追加し、判定結果の正誤やジャンル・ムードの修正、属性の 5 段階評価を入力します。ルールベースと Jev の一致率などを比較し、CSV で保存できます。保存したプレイリストの満足度と不適切な曲も記録できます
5. **設定**: 解析パラメータ、Jev の判定基準、選曲に使う分類結果、ログを確認・変更します

評価の進め方は [docs/evaluation.md](docs/evaluation.md) を参照してください。

## Jev（TypeSafe）の利用について

- 送信するのは、正規化したタグ情報（曲名・アーティスト・アルバム・ジャンルタグ・年）と、音響特徴量を言葉の区分（「slow (70-95 BPM)」など）に変換したものだけです。音声ファイルやファイルパスは送りません。曲名を送らない設定もできます
- 1 曲につき 1 リクエストです。料金は TypeSafe の料金体系に従います。実行前に画面で件数を確認してください
- 認証エラー・レート制限では即座に、通信エラーが連続した場合も中断し、無駄なリクエストを抑えます
- Jev の confidence は分類の正解率ではありません。精度は精度評価画面で確認してください

詳細は [docs/jev.md](docs/jev.md) を参照してください。

## サンプルデータ

```bash
# 架空の楽曲を DB に登録する（GUI の「サンプルデータで試す」と同じ）
uv run python -m volumio_playlist_studio.sample_data demo data/studio.sqlite3

# 合成音声（正弦波・クリック音）の FLAC を空のフォルダに作る。スキャンと解析を試せる
uv run python -m volumio_playlist_studio.sample_data audio /tmp/sample-music
```

## LAN からのアクセス（iPhone など）

初期設定では外部から接続できません。アプリには認証機能がないため、LAN に公開すると同じネットワークの誰でもライブラリの閲覧や設定の変更ができます。信頼できる自宅 LAN でのみ、一時的に次のように起動してください。

```bash
uv run streamlit run src/volumio_playlist_studio/gui/app.py --server.address 0.0.0.0
```

WSL2 では Windows 側のポート転送やファイアウォールの設定も必要です。インターネットには公開しないでください。

## データの保存場所

解析結果・評価・プレイリストは `data/studio.sqlite3`、ログは `data/logs/app.log` に保存します（`VPS_DATA_DIR` で変更可能）。どちらも Git 管理対象外です。スキーマを更新するときは、既存の DB を `studio.sqlite3.bak-v<バージョン>` にバックアップしてから移行します。

## 開発

開発規約は [AGENTS.md](AGENTS.md) を参照してください。

| 用途 | コマンド |
|---|---|
| フォーマット | `uv run ruff format .` |
| リント | `uv run ruff check .` |
| 型検査 | `uv run mypy` |
| テスト（カバレッジ 90% 未満で失敗） | `uv run pytest` |

テストは外部サービスと通信しません（Jev は HTTP 層をモック、Volumio はモッククライアント）。テスト用の音声はテスト内で生成します。

## ドキュメント

- [docs/architecture.md](docs/architecture.md): アーキテクチャ、データフロー、データモデル、実装タスク
- [docs/audio-features.md](docs/audio-features.md): 音響特徴量の意味と分類への使い方
- [docs/jev.md](docs/jev.md): Jev による分類の設計
- [docs/evaluation.md](docs/evaluation.md): 精度評価の手順と指標
- [docs/volumio.md](docs/volumio.md): Volumio 連携の調査結果と未確認事項
