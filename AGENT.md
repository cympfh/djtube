# 起動と実装

公開サイトの操作は README。ここは起動、環境変数、デプロイ、実装のメモ。

YouTube の曲を 2 デッキで再生する。検索はサーバ側で行い、再生はサーバが中継した音声をブラウザの audio 要素で鳴らす。クロスフェーダーはその 2 つの音量を変え、テンポは各デッキの再生速度を変える。

公開 URL は https://s.cympfh.cc/djtube/ 。

## 動き

- デッキ A / B、再生・一時停止、キュー、テンポ、イコライザー、クロスフェーダー、プレイリスト
- 検索語を入れると結果からデッキへ載せる
- 検索はこれまでどおり。再生は YouTube IFrame ではなく、サーバが yt-dlp で音声 URL を解決して中継する
- デッキに曲が載っているあいだ、検索結果の `thumbnail` を静止画で出す。無いときはその枠を空にする。動画 ID から絵は作らない
- テンポは audio 要素の `playbackRate`。範囲は 0.5 から 2.0、操作は 0.25 刻み。画面の数値を `playbackRate` に渡す。曲を載せると、載せたデッキのテンポだけ 1.0 に戻し、そのデッキの HIGH / MID / LOW も 0 dB に戻す
- キーボードだけで一通り操作できる
- Pioneer DDJ-FLX4 の再生、キュー、ロード、クロスフェーダー、ジョグ、ブラウズは MIDI。それ以外の操作は未実装

YouTube の iframe は音声を Web Audio に渡せない。HIGH / MID / LOW を実際にかけるため、デッキは `/api/audio/{id}` の音声を再生する。yt-dlp は progressive な音声 URL を取るだけで、ファイルは保存しない。別のダウンロードサイトは使わない。署名付き URL はブラウザに返さない。データセンターの IP では取得に失敗することがあり、そのときはデッキにエラーが出る。

イコライザーは `MediaElementAudioSourceNode` のあと、HIGH（highshelf 10 kHz）、MID（peaking 1 kHz）、LOW（lowshelf 100 Hz）の順。中央 0.5 が 0 dB、0 が -36 dB、1 が +12 dB。曲を載せると、載せたデッキの 3 バンドを 0 dB に戻す。もう一方のデッキはそのまま。グラフを作れないときはスライダーの数値とは別に「イコライザーを音声に接続できませんでした」と出す。

## パス

nginx は `/djtube/` を外してコンテナへ渡す。コンテナはポート 8098 で、中のパスは `/` から始まる。

ブラウザから見える URL は公開パスのまま。

| ブラウザ | コンテナ |
| --- | --- |
| `/djtube/` | `/` |
| `/djtube/static/app.js` | `/static/app.js` |
| `/djtube/api/search` | `/api/search` |
| `/djtube/api/playlists` | `/api/playlists` |

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

## プレイリスト

プレイリストは名前の付いた曲の並びです。曲は検索結果と同じ項目（動画 ID、タイトル、チャンネル、長さ、サムネイル）です。ブラウザを閉じても、別のブラウザで開いても、同じサーバの一覧を見ます。ログインはなく、このサーバを開ける人は同じプレイリストを共有します。

保存先は JSON ファイルです。既定は、起動したディレクトリ（コンテナの中では `/app`）の `data/playlists.json`。環境変数 `DJTUBE_PLAYLISTS` にファイルのパスを渡すと、そこへ書きます。

ファイルに書けたときは、同じファイルシステムが残っていればプロセスを入れ直しても読めます。書けないときは、そのプロセスが生きているあいだだけメモリに残し、ログに警告を出します。

公開手順の `docker run --rm -p 8098:8098 -e YOUTUBE_API_KEY djtube` はボリュームを付けません。`--rm` でコンテナを消すと、コンテナの中に書いたファイルも消えます。この起動のしかたには、コンテナを作り直したあとも残る場所はありません。

`compose.yaml` は名前付きボリューム `djtube-data` を `/app/data` に付け、`DJTUBE_PLAYLISTS=/app/data/playlists.json` を渡します。`docker compose` でコンテナを作り直しても、このボリュームのプレイリストは残ります。

`docker run` で残したいときは、同じボリュームを付けます。

```bash
docker run --rm -p 8098:8098 -e YOUTUBE_API_KEY -e DJTUBE_PLAYLISTS=/app/data/playlists.json -v djtube-data:/app/data djtube
```

イメージは `/app/data` を `appuser` の所有で作ります。名前付きボリュームを初めて付けるとき、この所有者が使われます。API は `/api/playlists` です。プロセスは 1 つを想定しています。

## 検索と API キー

環境変数名は `YOUTUBE_API_KEY`。YouTube Data API v3 のキーを、実行時だけ渡します。

- キーがあるとき、検索はサーバが Data API を呼ぶ。`safeSearch` は `none`。既定の moderate は、YouTube が年齢確認にする語を 0 件にする
- 結果は 50 件まで取る。`search.list` は 1 ページ最大 50 件で、それより少ないときは `nextPageToken` で次を足す（最大 3 ページ）。yt-dlp 側は `ytsearch50`
- 「音楽に限る」は最初オン。オンのとき Data API には `videoCategoryId=10`（Music）と `type=video` を付ける。タイトルで後から絞ることはしない。`topicId`（`/m/04rlf`）は別の絞り込みなので重ねない
- キーが無い、API が失敗した、または API が 0 件のときは yt-dlp に落ちる。音楽オンなら `music.youtube.com` の曲検索（`#songs`）。オフなら `ytsearch50` のまま、音楽では絞らない
- キーはクライアントの JS / HTML に埋め込まない。レスポンスにも載せない

## 操作の実装

クロスフェーダーは等パワー（中央で両方とも約 71%）。画面の「音量」がその値です。ジョグ（`[ ]` と `; '`。デッキ B の 10 秒戻しは `Shift+;` と `+`）は再生位置だけを動かし、キュー位置とクロスフェーダーは変えません。プレーヤーがまだジョグ前の位置にいるあいだは、直前に指示した位置へ足す。再生がその位置を離れて指示した位置のほうへ進んだときに記憶は消える。0.05 秒の指示に再生位置が届いたときも消える。ジョグ前の位置のまま、または逆方向に動いただけでは消えない。キューしたとき、時間のバーをクリックしたとき、曲を読み込んだときは、位置を読む前とシークする前に消える。長さが分かっているときはその範囲に収め、プレーヤーが準備完了でないときは何もしません。

画面下の凡例は `djtube/static/keys.js` の `BINDINGS` が出す。

## DDJ-FLX4

`djtube/static/controller.js` の `FLX4_MAP` が、キーボードと同じ actions を呼ぶ。番号は Pioneer の DDJ-FLX4 MIDI Message List（E1）と Mixxx の Pioneer-DDJ-FLX4。チャンネルは 0 始まり。デッキ 1 は 0、デッキ 2 は 1、ミキサーとブラウズは 6。

Web MIDI は安全なページで、「MIDI を開く」を押したときだけ接続する。公開サイトは https://s.cympfh.cc/djtube/ 。画面上部に未接続か、接続したデバイス名が出る。

ジョグは `actions.jog(deck, seconds)`。準備完了のデッキだけ、再生位置を秒数ぶん動かす。テンポはキーボードの `setRate`、`nudgeRate`、`resetRate` と、MIDI 値を受ける `setRateFromController`（0–127、64 が 1.0、0 が 0.5、127 が 2.0）。イコライザーは `setEq`、`nudgeEq`、`resetEq`、`setEqFromController`。`setEqFromController` はバンド名と MIDI 0–127 を受け、64 が 0 dB、0 がカット、127 がブーストです。FLX4 のテンポは各デッキの CC 0（MSB）、EQ は HI が CC 7、MID が CC 11、LOW が CC 15。LSB はマップしていない。曲を載せると、載せたデッキのテンポだけ 1.0 に戻り、そのデッキの HIGH / MID / LOW は 0 dB に戻る。

割り当てている操作:

- PLAY/PAUSE（ch 0/1、ノート 11）: 再生 / 一時停止
- CUE（ch 0/1、ノート 12）: キュー
- LOAD（ch 6、ノート 70 / 71）: 選択中の曲をデッキ A / B へ
- クロスフェーダー MSB（ch 6、CC 31）: クロスフェーダー。値は 0–127
- ジョグ側面・プラッター（ch 0/1、CC 33 / 34 / 35）: 1 目盛り約 0.05 秒
- Shift+プラッター（ch 0/1、CC 41）: 1 目盛り約 0.5 秒
- BROWSE 回転（ch 6、CC 64）: 検索結果を上下
- テンポスライダー MSB（ch 0/1、CC 0）: そのデッキの再生速度
- EQ HI / MID / LOW MSB（ch 0/1、CC 7 / 11 / 15）: そのデッキの HIGH / MID / LOW

未実装の操作:

- Shift+PLAY/PAUSE、Shift+CUE
- ジョグのタッチ
- テンポと EQ の LSB、TRIM、フィルター
- チャンネルフェーダーとその LSB、クロスフェーダーの LSB
- ヘッドホン、MASTER、マイク
- パッド、ループ、BEAT SYNC、エフェクト、SMART CFX、SMART FADER
- BROWSE の押し込みと Shift+BROWSE

## 開発

```bash
uv run pytest -q
node --test tests/client/player.test.mjs
uv run black --line-length 120 .
```
