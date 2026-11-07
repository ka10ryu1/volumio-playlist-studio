# Volumio 連携

## 現状

実機と通信するクライアントは**未実装**。インターフェース（`volumio.VolumioClient`）、モッククライアント、URI の照合、登録処理と履歴の保存までを実装し、モックでテストしている。画面の「Volumio に登録」もモッククライアントを使い、実機の Volumio は変更しない。

AGENTS.md の方針（連携方式は実装前にユーザーと確認する）に従い、通信方式を決めてから実機クライアントを実装する。

## 公式資料の調査結果（2026-11 時点）

公式の開発者ドキュメント（<https://developers.volumio.com/>、ソースは GitHub の volumio/volumio-developers-docs）と、公開されているバックエンドのソース（volumio/volumio3-backend）を確認した。Volumio 4 のバックエンドのソースと、Volumio 4 向けの API 資料は確認できなかった。

### REST API（読み取り中心）

| 用途 | エンドポイント |
|---|---|
| プレイリスト一覧 | `GET /api/v1/listplaylists` |
| ライブラリの参照 | `GET /api/v1/browse?uri=<uri>`（`limit`・`offset` あり。ルートは `music-library` など） |
| キューへの追加 | `POST /api/v1/addToQueue` |

ドキュメントの例では、楽曲の URI は `music-library/NAS/...` や `mnt/NAS/...` の形式。REST API には**プレイリストの作成・曲の追加はない**。

### WebSocket API（Socket.io）

プレイリストの作成・曲の追加は WebSocket API にのみある。

| イベント（送信） | 入力 | 結果のイベント |
|---|---|---|
| `listPlaylist` | なし | `pushListPlaylist`（名前の配列） |
| `createPlaylist` | `{"name": "..."}` | `pushCreatePlaylist`（`{"success": bool, "reason": "..."}`） |
| `addToPlaylist` | `{"name": "...", "service": "mpd", "uri": "..."}` | `pushAddToPlaylist` |

結果のイベント名はドキュメントに記載がなく、volumio3-backend のソース（`app/plugins/user_interface/websocket/index.js`）で確認した。volumio3-backend は socket.io **2.x** を使っており、Python から接続するには socket.io 2.x と互換のクライアント（python-socketio の旧バージョンなど）が必要になる可能性が高い。Volumio 4 で同じかは未確認。

## URI の対応付け

PC 上のファイルパス（SMB 共有のマウント先からの相対パス）と Volumio 上の URI は、文字列の変換で対応付けない。

1. Volumio のライブラリを `browse` でたどり、実際の楽曲 URI の一覧を取得する（`volumio.collect_track_uris`）
2. ライブラリの相対パスの**全階層**が URI の末尾と一致する候補を探す（`volumio.match_tracks_to_uris`）。URI の先頭（`music-library/INTERNAL` など）は推測しない
3. 候補が 1 つなら対応あり、0 なら対応なし、複数（同じ相対パスが複数のストレージにある）なら曖昧とし、曖昧・対応なしの曲は登録しない
4. ファイル名の Unicode 正規化（NFC/NFD）の違いは吸収する

## 登録時の安全策

- 同じ名前のプレイリストが既にある場合は登録しない（既存のプレイリストを上書き・削除しない）
- 書き込みは画面で確認のチェックを入れ、ボタンを押したときだけ行う
- 曲の追加に失敗したらそこで止め、曲ごとの結果（追加済み・失敗・未実行）を登録履歴に保存して画面に表示する

## 通信方式の選択肢

「通信方式」とは、このアプリが Volumio にどのような手段で命令を送るかのこと。Volumio には次の 2 つの公式 API がある。

| | REST API | WebSocket API（Socket.io） |
|---|---|---|
| 仕組み | ブラウザで URL を開くのと同じ、1 回ごとの HTTP のやり取り | 接続を張ったまま、イベント名とデータを送受信する。Volumio の画面自体もこれを使う |
| できること | プレイリスト一覧、ライブラリの参照、キューへの追加・再生 | 上記に加えて、プレイリストの作成・曲の追加・削除 |
| Python から使う方法 | 標準ライブラリや httpx で簡単に呼べる | Socket.io のプロトコル（通常の WebSocket の上に独自の形式を重ねたもの）に対応したクライアントが必要 |

プレイリストを作るには WebSocket API が必須になる。Socket.io はサーバーとクライアントのバージョンをそろえる必要があり、公開されている Volumio 3 のバックエンドは socket.io 2.x を使っている。Python の主要なクライアント（python-socketio）は、最新版が socket.io 3.x 以降にしか対応しないため、次のどちらかになる。

- **案 A**: socket.io 2.x に対応する python-socketio の旧版（4.x 系）を依存関係に加える。保守が終わっている古いライブラリで、新しい Python での動作は未確認
- **案 B**: 必要な 3 つのイベント（`listPlaylist`、`createPlaylist`、`addToPlaylist`）だけを、WebSocket ライブラリの上に自前で実装する。依存は小さいが、プロトコルの細部を自分で扱う

どちらの場合も、ライブラリの参照（URI 一覧の取得）は REST API で行い、書き込みだけを WebSocket で行う構成を想定している。Volumio 4 のバックエンドが socket.io 3.x 以降に更新されていれば、最新の python-socketio をそのまま使える（案 C）。

なお、Volumio が内部で使っている MPD（音楽再生エンジン）のプロトコルでもプレイリストを作れるが、Volumio の画面のプレイリストは Volumio 独自の管理のため、作ったものが画面に表示されない可能性が高く、採用しない。

### Volumio 4 の socket.io のバージョンの確かめ方（未検証の手順）

PC のブラウザで次の URL を開き、表示された内容を確認する。Volumio の WebSocket が別のポート（例: 3000）で待ち受けている場合は、`volumio.local:3000` のようにポートを付けて試す。

1. `http://volumio.local/socket.io/?EIO=3&transport=polling` — `{"sid":...}` を含む応答なら socket.io 2.x と互換（案 A・B）
2. `http://volumio.local/socket.io/?EIO=4&transport=polling` — 1 が「Unsupported protocol version」などのエラーで、2 が `0{"sid":...}` で始まる応答なら socket.io 3.x 以降（案 C）

## ユーザーに確認したいこと

1. 上記の URL の表示結果（Volumio 4 の socket.io のバージョンの判断材料）
2. 案 A・B・C のどれで実装するか。案 A・C の場合は依存関係の追加の可否
3. REST API の `browse` で URI 一覧を取得する部分（読み取りのみ）を先に実装してよいか
4. ブラウザで `http://volumio.local/api/v1/listplaylists` を開いて、プレイリスト一覧が表示されるか
5. 音楽ファイルの保存場所（microSD の内部ストレージ）が、Volumio の画面で `music-library/INTERNAL/...` として見えるか

## 実機での確認項目

- [ ] REST API でプレイリスト一覧とライブラリを取得できる
- [ ] 取得した URI とスキャンしたファイルが、日本語のファイル名を含めて一意に対応する
- [ ] WebSocket でプレイリストを作成し、曲を追加できる（結果のイベントを受け取れる）
- [ ] 作成したプレイリストが Volumio の画面（iPhone の Safari）に表示され、再生できる
- [ ] 既存のプレイリストと同じ名前では登録されない
- [ ] 途中で失敗した場合に、登録済みの曲が履歴と一致する
