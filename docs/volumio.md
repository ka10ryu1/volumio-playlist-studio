# Volumio 連携

## 現状

| 機能 | 状況 |
|---|---|
| プレイリスト一覧の取得（REST API） | 実装済み。設定画面の「接続テスト」で使う |
| ライブラリの曲の URI の取得（REST API） | 実装済み。取得した一覧は DB に保存し、PC のファイルとの照合に使う |
| ファイルと URI の照合、登録前のプレビュー | 実装済み（実機の一覧・モックのどちらとも照合できる） |
| プレイリストの作成・曲の追加（WebSocket API） | 実装済み（`volumio_socketio.py`）。画面の照合先で「Volumio 実機」を選ぶと、バックグラウンド処理（`workflows.run_volumio_register`）で実機に登録する。「モック（模擬）」では実機を変更せずに流れを確認できる |

読み取りは REST API（`volumio_rest.py`）、プレイリストの作成・曲の追加は WebSocket API（`volumio_socketio.py` の `VolumioLiveClient`）で行う。
テストは REST API と Socket.io を模擬するサーバーで行い、実機での確認は次のコマンドで明示的に実行する。

```bash
# 読み取りだけを確認する
VPS_TEST_VOLUMIO_HOST=volumio.local uv run pytest -m integration tests/test_volumio_live.py --no-cov
# プレイリストの作成・曲の追加も確認する（`vps-test-日時` という名前のプレイリストを新しく作る）
VPS_TEST_VOLUMIO_HOST=volumio.local VPS_TEST_VOLUMIO_WRITE=1 uv run pytest -m integration tests/test_volumio_live.py --no-cov --log-cli-level=INFO
```

作成したプレイリストはテスト後も残る。内容と再生を Volumio の画面で確認してから、画面で削除する。
判定した Socket.io のバージョンは、ログの `volumio socket.io connected: protocol=...` で確認できる。

## 実機での確認結果（2026-11-08、Volumio 3 系）

利用中の Volumio（アップデート前の旧バージョン）で、PC のブラウザから次を確認した。Volumio 4 へのアップデート後に、同じ方法で改めて確認する。

- `GET /api/v1/listplaylists` で、既存のプレイリスト名の配列が返る
- `/socket.io/?EIO=3&transport=polling` と `/socket.io/?EIO=4&transport=polling` のどちらも、Engine.IO v3 のバイナリ形式（長さの前置きのあるペイロード）で `0{"sid":...,"upgrades":["websocket"],"pingInterval":25000,"pingTimeout":60000}` が返った。EIO=4 を指定しても v3 の形式で返り、`maxPayload` もないため、socket.io 2.x（Engine.IO v3）と判断した

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

結果のイベント名はドキュメントに記載がなく、volumio3-backend のソース（`app/plugins/user_interface/websocket/index.js`）で確認した。
`app/playlistManager.js` から読み取れる、実装に関わる振る舞いは次のとおり。

- `createPlaylist` は失敗時も例外にせず、`{"success": false, "reason": "Playlist already exists"}` などを返す
- `addToPlaylist` の結果（`pushAddToPlaylist`）は、成功時はお気に入りかどうかの情報、保存の失敗時は `{"success": false}`。曲の一覧の取得に失敗した場合などは**応答が返らない**ため、待ち時間を区切って失敗として扱う
- `addToPlaylist` は、存在しないプレイリスト名を指定すると空のプレイリストを作る。そのため、必ず `createPlaylist` で作成してから曲を追加する
- プレイリスト名はそのままファイル名（`/data/playlist/<名前>`）に使われ、検証されない。`/`・`\`・制御文字を含む名前と `.` で始まる名前は、アプリ側で登録しない
- 結果のイベントは、要求を送った接続にだけ返る。`pushListPlaylist` は作成・追加のたびに順不同で届くため、プレイリスト一覧の取得には使わず REST API で取得するvolumio3-backend は socket.io **2.x** を使っており、Python から接続するには socket.io 2.x と互換のクライアント（python-socketio の旧バージョンなど）が必要になる可能性が高い。Volumio 4 で同じかは未確認。

## URI の対応付け

PC 上のファイルパス（SMB 共有のマウント先からの相対パス）と Volumio 上の URI は、文字列の変換で対応付けない。

1. Volumio のライブラリを `browse` でたどり、実際の楽曲 URI の一覧を取得する（`volumio.collect_track_uris`）
2. ライブラリの相対パスの**全階層**が URI の末尾と一致する候補を探す（`volumio.match_tracks_to_uris`）。URI の先頭（`music-library/INTERNAL` など）は推測しない
3. 候補が 1 つなら対応あり、0 なら対応なし、複数（同じ相対パスが複数のストレージにある）なら曖昧とし、曖昧・対応なしの曲は登録しない
4. ファイル名の Unicode 正規化（NFC/NFD）の違いは吸収する

## 登録時の安全策

- 同じ名前のプレイリストが既にある場合は登録しない（既存のプレイリストを上書き・削除しない）
- 書き込みは画面で確認のチェックを入れ、ボタンを押したときだけ行う
- 曲の追加に失敗したら、または「中断」を押したら（曲の追加の間で受け付ける）そこで止め、曲ごとの結果（追加済み・失敗・未実行）を登録履歴に保存する
- 実機への登録は、Volumio から最後に取得した曲の一覧との照合結果を使う。照合できた曲がない場合は登録ボタンを押せない
- 登録しなかった場合（同じ名前がある、名前が不正、通信できない）は登録履歴に残さず、理由を画面に表示する。プレイリストの作成後に一部の曲しか登録できなかった場合は、失敗として表示する

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

**採用した方式（2026-11）**: 案 B。WebSocket の送受信には websocket-client を使い、Socket.io の部分（3 つのイベントに必要な範囲）を自前で実装した。Volumio 3 で検証してから 4 にアップデートする予定のため、socket.io 2.x と 3.x 以降の両方に対応させている。違いは次の 2 点で、接続直後にサーバーが名前空間への接続（`40`）を自分から送るかどうかで判定する。

| | socket.io 2.x（Volumio 3） | socket.io 3.x 以降 |
|---|---|---|
| 名前空間への接続 | サーバーが自分から `40` を送る | クライアントが `40` を送り、サーバーが応答する |
| 生存確認 | クライアントが ping（`2`）を送る | サーバーが ping を送り、クライアントが pong（`3`）を返す |

接続の URL は `ws://<接続先>/socket.io/?EIO=4&transport=websocket`。socket.io 2.x のサーバーは EIO の指定によらず Engine.IO v3 で応答する（実機で確認済み）。

なお、Volumio が内部で使っている MPD（音楽再生エンジン）のプロトコルでもプレイリストを作れるが、Volumio の画面のプレイリストは Volumio 独自の管理のため、作ったものが画面に表示されない可能性が高く、採用しない。

### Volumio 4 の socket.io のバージョンの確かめ方（未検証の手順）

PC のブラウザで次の URL を開き、表示された内容を確認する。Volumio の WebSocket が別のポート（例: 3000）で待ち受けている場合は、`volumio.local:3000` のようにポートを付けて試す。

1. `http://volumio.local/socket.io/?EIO=3&transport=polling` — `{"sid":...}` を含む応答なら socket.io 2.x と互換（案 A・B）
2. `http://volumio.local/socket.io/?EIO=4&transport=polling` — 1 が「Unsupported protocol version」などのエラーで、2 が `0{"sid":...}` で始まる応答なら socket.io 3.x 以降（案 C）

## 未決定の事項

1. 実機の曲の URI が、PC のファイルと一意に対応するか（「Volumio から曲の一覧を取得」した後のプレビューで確認できる）

## 実機での確認項目

- [ ] REST API でプレイリスト一覧とライブラリを取得できる
- [ ] 取得した URI とスキャンしたファイルが、日本語のファイル名を含めて一意に対応する
- [ ] WebSocket でプレイリストを作成し、曲を追加できる（結果のイベントを受け取れる）
- [ ] 作成したプレイリストが Volumio の画面（iPhone の Safari）に表示され、再生できる
- [ ] 既存のプレイリストと同じ名前では登録されない
- [ ] 途中で失敗した場合に、登録済みの曲が履歴と一致する
