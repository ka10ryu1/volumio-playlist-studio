# Jev による分類

TypeSafe の Jev（System One モデル）で楽曲を分類する設計をまとめる。API 仕様は公式ドキュメント（<https://docs.typesafe.ai/>、2026-11 時点）を確認して実装した。

## 使用する API

- 公式 Python SDK `typesafe-sdk` の `TypeSafeClient.system_one()` を使う（`POST https://api.typesafe.ai/v1/systemone`）
- API キーは環境変数 `TYPESAFE_API_KEY` から SDK が読む。アプリの DB や画面・ログには保存・表示しない
- モデルは既定で `jev-latest`。応答に含まれる実際のモデル名（例: `jev-1.13.0`）を分類結果に記録する
- 429（レート制限）・529（過負荷）・接続エラーなどは SDK の既定のリトライ方針（指数バックオフ）で再試行される
- SDK は送信本文を zstd で圧縮する（Python 3.14 では標準ライブラリで対応）

## 送信するデータ（state）

1 曲につき 1 リクエストで、次の JSON だけを送る。音声ファイルとファイルパスは送らない。

```json
{
  "track": {"title": "...", "artist": "...", "album": "...", "genre_tag": "...", "release_year": "1999"},
  "audio_descriptors": {
    "tempo": "slow (70-95 BPM)",
    "tempo_stability": "stable tempo",
    "loudness": "quiet",
    "dynamics": "steady loudness throughout",
    "timbre": "dark or warm (energy concentrated in low frequencies)",
    "rhythmic_density": "sparse (few note onsets)",
    "noisiness": "smooth, tonal sound"
  },
  "note": "The audio descriptors were computed automatically from short excerpts and may be inaccurate. ..."
}
```

- 値が不明なタグは送らない。設定で曲名を送らないようにできる
- 公式ドキュメント（Model jaggedness）では、Jev は数値の比較が苦手なため、数値はコードで名前付きの区分に変換して渡すことが推奨されている。そのため特徴量は `classify/descriptors.py` で言葉の区分に変換する
- MFCC やクロマのように言葉の区分にしにくい特徴量は送らない
- 音響特徴量が未解析の曲は `"audio_descriptors": "not available"` として、タグ情報だけで判定する

## 質問（questions）

| 質問 | 型 | 内容 |
|---|---|---|
| `genre` | Choice | 13 のジャンル候補（`other` を含む）から 1 つ。各候補に境界が分かる説明を付ける |
| `genre_determinable` | Noul | 与えた情報でジャンルを判断できるか（Companion Noul パターン。Choice は必ずどれかを選ぶため） |
| `relaxation`, `energy`, `brightness`, `calmness`, `work_bgm`, `party`, `night_bgm` | Score（5 段階） | 各属性。回答（0〜4 の期待値）を 0〜1 に換算する |

質問文と基準は `classify/jev.py` に定義している。質問・候補・区分を変えた場合は `config.JEV_QUESTION_VERSION` を更新する。同じ曲でもバージョンが違えば別の結果として保存され、評価データで比較できる。

## 判定保留の扱い

- Score・Choice の confidence が `min_confidence`（既定 0.5）未満の回答は判定保留にする
- `genre_determinable` の確率が閾値（既定 0.5）未満なら、ジャンルを不明にする
- 保留にした場合も confidence と各段階の確率は保存し、精度評価で確認できるようにする
- ムードは属性スコアから `classify/base.py` で決める（1 位のスコアが低い、または 2 位との差が小さい場合は判定保留）
- confidence は分類の正解率ではない。正解率は人手評価との比較で確認する
- 歌詞の内容、ボーカルの有無は質問しない。タグと基本的な特徴量からは信頼できる判定ができないため

## 料金とリクエストの抑制

- 実行前に、未判定の曲数、今回判定する曲数、リクエスト数を画面に表示し、確認のチェックを入れないと実行できない
- 1 回の実行で判定する曲数に上限（既定 20 曲、設定画面で変更可能）を設ける
- 現在の質問バージョンで判定済みの曲は再判定しない。プレイリストの生成時には Jev を呼ばない
- 認証エラー（401・403）とレート制限（SDK の再試行後も 429）では即座に中断する。その他の失敗が 3 回連続した場合も中断する
- エラーは種類と HTTP ステータスだけを記録する（応答本文には送信した内容が含まれる可能性があるため）

## API キーがない場合

Jev のパネルは案内のみを表示し、ルールベースの分類で選曲・評価を行う。テストは `httpx2.MockTransport` で HTTP 層だけを差し替えて行い、実 API とは通信しない。

## 未確認事項

- 実 API での動作（応答形式は公式ドキュメントの例に合わせてテストしている）
- 楽曲分類での Jev の精度。まず 5〜10 曲で応答を確認し、30〜50 曲の評価データでルールベースと比較する
- 1 リクエストあたりのトークン数と料金（画面に入力・出力トークン数の合計を表示する）
