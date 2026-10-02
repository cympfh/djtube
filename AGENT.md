# 起動と実装

公開サイトの操作は README。ここは起動、環境変数、デプロイ、実装のメモ。

YouTube の曲を 2 デッキで再生する。検索はサーバ側で行い、再生はサーバが中継した音声をブラウザの audio 要素で鳴らす。クロスフェーダーはその 2 つの音量を変え、テンポは各デッキの再生速度を変える。

公開 URL は https://s.cympfh.cc/djtube/ 。

## 動き

- デッキ A / B、再生・一時停止、キュー、テンポ、イコライザー、クロスフェーダー
- 検索語を入れると結果からデッキへ載せる
- 検索はこれまでどおり。再生は YouTube IFrame ではなく、サーバが yt-dlp で音声 URL を解決して中継する
- テンポは audio 要素の `playbackRate`。範囲は 0.5 から 2.0、操作は 0.25 刻み。画面の数値を `playbackRate` に渡す。曲を載せると、載せたデッキだけ 1.0 に戻す
- キーボードだけで一通り操作できる
- Pioneer DDJ-FLX4 の MIDI 割り当ては未実装。受信口だけある

YouTube の iframe は音声を Web Audio に渡せない。HIGH / MID / LOW を実際にかけるため、デッキは `/api/audio/{id}` の音声を再生する。yt-dlp は progressive な音声 URL を取るだけで、ファイルは保存しない。別のダウンロードサイトは使わない。署名付き URL はブラウザに返さない。データセンターの IP では取得に失敗することがあり、そのときはデッキにエラーが出る。

イコライザーは `MediaElementAudioSourceNode` のあと、HIGH（highshelf 10 kHz）、MID（peaking 1 kHz）、LOW（lowshelf 100 Hz）の順。中央 0.5 が 0 dB、0 が -36 dB、1 が +12 dB。グラフを作れないときはスライダーの数値とは別に「イコライザーを音声に接続できませんでした」と出す。

## パス

nginx は `/djtube/` を外してコンテナへ渡す。コンテナはポート 8098 で、中のパスは `/` から始まる。

ブラウザから見える URL は公開パスのまま。

| ブラウザ | コンテナ |
| --- | --- |
| `/djtube/` | `/` |
| `/djtube/static/app.js` | `/static/app.js` |
| `/djtube/api/search` | `/api/search` |

フロントの基準パスは `/djtube/`。直に `http://127.0.0.1:8098/djtube/` を開いても、同じプレフィックスをコンテナ側で剥がすので動く。

フロントのファイルは `djtube/static/` にあり、本番ビルドで別の `dist/` は作りません。配布単位は Docker イメージです。

## ローカル起動

Python 3.12 以上と [uv](https://docs.astral.sh/uv/) を使う。

```bash
uv sync
uv run uvicorn main:app --host 127.0.0.1 --port 8098
```

ブラウザは http://127.0.0.1:8098/djtube/ を開く。

`YOUTUBE_API_KEY` が無いときは検索を yt-dlp（`ytsearch`）で行います。再生自体にこのキーは要りません。

## Docker

ホストは `main` を `/home/ubuntu/git/djtube` に clone して、リポジトリ直下の Dockerfile をビルドする。

```bash
docker build -t djtube .
docker run --rm -p 8098:8098 -e YOUTUBE_API_KEY djtube
```

compose でも同じです。`compose.yaml` は `8098:8098` を公開し、環境変数 `YOUTUBE_API_KEY` をコンテナへ渡します。

```bash
docker compose up --build
```

キーの値はリポジトリに置きません。

## 検索と API キー

環境変数名は `YOUTUBE_API_KEY`。YouTube Data API v3 のキーを、実行時だけ渡します。

- キーがあるとき、検索はサーバが Data API を呼ぶ。`safeSearch` は `none`。既定の moderate は、YouTube が年齢確認にする語を 0 件にする
- 結果は 50 件まで取る。`search.list` は 1 ページ最大 50 件で、それより少ないときは `nextPageToken` で次を足す（最大 3 ページ）。yt-dlp 側は `ytsearch50`
- 「音楽に限る」は最初オン。オンのとき Data API には `videoCategoryId=10`（Music）と `type=video` を付ける。タイトルで後から絞ることはしない。`topicId`（`/m/04rlf`）は別の絞り込みなので重ねない
- キーが無い、API が失敗した、または API が 0 件のときは yt-dlp に落ちる。音楽オンなら `music.youtube.com` の曲検索（`#songs`）。オフなら `ytsearch50` のまま、音楽では絞らない
- キーはクライアントの JS / HTML に埋め込まない。レスポンスにも載せない

## 操作の実装

クロスフェーダーは等パワー（中央で両方とも約 71%）。画面の「音量」がその値です。ジョグ（`[ ]` と `; '`）は再生位置だけを動かし、キュー位置とクロスフェーダーは変えません。長さが分かっているときはその範囲に収め、プレーヤーが準備完了でないときは何もしません。

画面下の凡例は `djtube/static/keys.js` の `BINDINGS` が出す。

## DDJ-FLX4

割り当ては未実装です。コントローラ用の入口は `djtube/static/controller.js` の `FLX4_MAP` と `dispatchControllerEvent` だけです。

マップは空です。Web MIDI は「MIDI を開く」を押したときだけ接続します。来た信号は、マップに無いのでデッキを動かさず、件数だけ数えます。

あとからノート番号や CC を `FLX4_MAP` に書くと、キーボードと同じ actions（再生、キュー、ジョグ、テンポ、イコライザー、ロード、フェーダー）が呼ばれます。ジョグは `jog` で、引数はデッキ（`"A"` か `"B"`）と秒数です。テンポは `setRate`、`nudgeRate`、`resetRate`、`setRateFromController` です。`setRateFromController` は MIDI の 0–127 を受け、64 が 1.0、0 が 0.5、127 が 2.0 です。イコライザーは `setEq`、`nudgeEq`、`resetEq`、`setEqFromController` です。`setEqFromController` はバンド名と MIDI 0–127 を受け、64 が 0 dB、0 がカット、127 がブーストです。デッキ側の書き換えは要りません。チャンネルは 0–15。フェーダー用の CC は `setCrossfaderFromController` に `passValue: true` を渡し、値は MIDI の 0–127。実機の番号はここには書いていません。マップは空のままです。

## 開発

```bash
uv run pytest -q
node --test tests/client/player.test.mjs
uv run black --line-length 120 .
```
