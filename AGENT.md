# 起動と実装

公開サイトの操作は README。ここは起動、環境変数、デプロイ、実装のメモ。

YouTube の曲を 2 デッキで再生する。検索はサーバ側で行い、再生はサーバが中継した音声をブラウザの audio 要素で鳴らす。各デッキの音量は 0 から 1 で、クロスフェーダーはその 2 つの音の混ざりを変える。聞こえる大きさは、そのデッキの音量にクロスフェーダーのゲインを掛けた値。テンポは各デッキの再生速度を変える。

公開 URL は https://s.cympfh.cc/djtube/ 。

## 動き

- デッキ A / B、再生・一時停止、キュー、テンポ、音量、イコライザー、フィルター、クロスフェーダー、プレイリスト
- 検索語を入れると結果からデッキへ載せる
- 検索はこれまでどおり。再生は YouTube IFrame ではなく、サーバが yt-dlp で音声 URL を解決して中継する
- デッキに曲が載っているあいだ、検索結果の `thumbnail` を静止画で出す。無いときはその枠を空にする。動画 ID から絵は作らない
- 再生中のデッキは、その絵の上に薄い白い輪を出す。輪の印は長短があり、その向きが再生位置とともに変わる。`playbackRate` で速さが変わる。停止中と空のデッキは回さない。輪を上から押しているあいだは止まり、離すと、押す前に再生していたデッキだけまた回る。A と B は別々
- テンポは audio 要素の `playbackRate`。範囲は 0.5 から 2.0。スライダーと FLX4 のテンポは、その範囲の速度をそのまま渡す。キーボードの上げ下げは 0.01。画面の数値を `playbackRate` に渡す。曲を載せると、載せたデッキのテンポだけ 1.0 に戻し、そのデッキの HIGH / MID / LOW も 0 dB に戻す。音量とフィルターは戻さない
- キーボードだけで一通り操作できる
- Pioneer DDJ-FLX4 の再生、キュー、ロード、クロスフェーダー、ジョグ、ブラウズ、テンポ、イコライザー、チャンネルフェーダー、CFX のフィルターは MIDI。未実装の操作は下の一覧

YouTube の iframe は音声を Web Audio に渡せない。HIGH / MID / LOW を実際にかけるため、デッキは `/api/audio/{id}` の音声を再生する。yt-dlp は progressive な音声 URL を取るだけで、ファイルは保存しない。別のダウンロードサイトは使わない。署名付き URL はブラウザに返さない。データセンターの IP では取得に失敗することがあり、そのときはデッキに「音源を再生できませんでした」と出る。出ているあいだ、そのデッキの「再生」だけ `disabled` になり、`Q` / `W` も再生を始めない。別の曲を載せると表示は消え、また再生できる。もう一方のデッキは変えない。

イコライザーは `MediaElementAudioSourceNode` のあと、HIGH（highshelf 10 kHz）、MID（peaking 1 kHz）、LOW（lowshelf 100 Hz）の順。中央 0.5 が 0 dB、0 が -36 dB、1 が +12 dB。曲を載せると、載せたデッキの 3 バンドを 0 dB に戻す。もう一方のデッキはそのまま。グラフを作れないときはスライダーの数値とは別に「イコライザーを音声に接続できませんでした」と出す。

フィルターはイコライザーのあと、出力の前。LOW はフィルターだけに繋がり、フィルターが出力に繋がる。LOW を先に出力へ繋いでから切り離さない。音量とクロスフェーダーはこれまでどおり `audio.volume`。各デッキに 1 つ。値は 0 から 1 で、0.5 が中央。中央は peaking のゲイン 0 dB で、伝達関数が 1 になるバイパス。ローパスやハイパスを聞こえる周波数に置いたままにはしない。0.5 より左はローパスで、左端 80 Hz から中央直前 16 kHz まで対数で動く。0.5 より右はハイパスで、中央直後 40 Hz から右端 10 kHz まで対数で動く。Q はバターワース。キーボードは 0.05。画面のスライダーは 0 から 100 で、50 が中央。曲を載せてもフィルターは戻さない。グラフを作れないときは「フィルターを音声に接続できませんでした」と出す。SMART CFX や、エコー、リバーブは付けない。

## パス

nginx は `/djtube/` を外してコンテナへ渡す。コンテナはポート 8098 で、中のパスは `/` から始まる。

ブラウザから見える URL は公開パスのまま。

| ブラウザ | コンテナ |
| --- | --- |
| `/djtube/` | `/` |
| `/djtube/static/app.js` | `/static/app.js` |
| `/djtube/api/search` | `/api/search` |
| `/djtube/api/playlists` | `/api/playlists` |
| `/djtube/api/cookies` | `/api/cookies` |

フロントの基準パスは `/djtube/`。直に `http://127.0.0.1:8098/djtube/` を開いても、同じプレフィックスをコンテナ側で剥がすので動く。

フロントのファイルは `djtube/static/` にあり、本番ビルドで別の `dist/` は作りません。配布単位は Docker イメージです。HTML は `Cache-Control: no-cache`。`app.css` と `app.js` の URL、および各モジュールの相対 import には同じ `?v=` が付く。`v` は `templates/index.html` と `static/` の内容のハッシュです。再読み込みは HTML を取り直し、その `v` の `app.js` と、そこから import される `actions.js` や `player.js` や `filter.js` を取ります。以前のモジュールは別の URL なので、一つの読み込みに混ざりません。

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

## Cookie

YouTube がボット確認で音源取得を止めるときは、画面から Netscape 形式の Cookie をアップロードするか、同じ形式の中身を貼り付けて保存する。ファイルが無いあいだ、yt-dlp には `cookiefile` を渡さず、player client も JS ランタイムも指定しない。保存したあとの音源取得だけ、そのファイルを渡す。そのときは player client を `web_embedded` と `web_safari` にする。`tv_downgraded` は使わない。同じ取得で、イメージの `/opt/djtube/deno` があればそれだけを JS ランタイムとして渡す。このバイナリは `PATH` に入っていない。差し替えは同じ画面で上書きする。画面上部の丸い Cookie の印から、失敗を待たずにその画面を開ける。ファイル欄と貼り付け欄は、保存してある中身では埋めない。

保存先は環境変数 `DJTUBE_COOKIES`。既定は `data/cookies.txt`。イメージと compose では `/app/data/cookies.txt`。名前付きボリューム `djtube-data` の `/app/data` に載るので、コンテナを作り直しても残る。プレイリストと同じボリューム。ボリュームの無い `docker run` では、プレイリストと同じく消える。中身はログに出さない。リポジトリには置かない。

`GET /api/cookies` は入っているかどうかだけを返す。`POST /api/cookies` が、ファイルのアップロードと、貼り付けた中身の保存。どちらも同じファイルを上書きする。中身は返さない。

音源が取れず、サーバが今回の失敗を認証か Cookie と判断したときも、同じ Cookie の案内を出す。それ以外の失敗では出さない。保存のあとに再生は自動ではやり直さない。ブラウザは yt-dlp の文言を分類しない。`GET /api/audio/{id}/cause` は `{"cookies": true}` か `{"cookies": false}` だけを返す。

## 検索と API キー

環境変数名は `YOUTUBE_API_KEY`。YouTube Data API v3 のキーを、実行時だけ渡します。

- キーがあるとき、検索はサーバが Data API を呼ぶ。`safeSearch` は `none`。既定の moderate は、YouTube が年齢確認にする語を 0 件にする
- 結果は 50 件まで取る。`search.list` は 1 ページ最大 50 件で、それより少ないときは `nextPageToken` で次を足す（最大 3 ページ）。yt-dlp 側は `ytsearch50`
- 「音楽に限る」は最初オン。オンのとき Data API には `videoCategoryId=10`（Music）と `type=video` を付ける。タイトルで後から絞ることはしない。`topicId`（`/m/04rlf`）は別の絞り込みなので重ねない
- キーが無い、API が失敗した、または API が 0 件のときは yt-dlp に落ちる。音楽オンなら `music.youtube.com` の曲検索（`#songs`）。オフなら `ytsearch50` のまま、音楽では絞らない
- キーはクライアントの JS / HTML に埋め込まない。レスポンスにも載せない
- 検索語全体が動画 URL または 11 文字の動画 ID のときは、その 1 件だけを返す。URL は `watch?v=`、`youtu.be`、`/shorts/`、`/embed/`、`/live/`、`/v/`、`music.youtube.com` の `watch?v=`。ホストの付いていない文中の `v=` や `youtu.be` はキーワードのまま。`videos.list` で取り、キーが無い、失敗、または 0 件のときは YouTube の oEmbed、それも取れなければ yt-dlp でその動画だけ取る。oEmbed には長さが無い。キーワード検索には落とさない。どれも取れないときは「その動画は見つかりませんでした」。この取得に `videoCategoryId` は付けない

## 操作の実装

クロスフェーダーは等パワー（中央で両方とも約 71%）。画面の「フェード」がその値です。各デッキの音量は 0 から 1 で、画面の「音量」がその百分率です。audio の `volume` は、その音量にクロスフェーダーのゲインを掛けた値です。両方 1 なら中央でも両方鳴り、0 ならクロスフェーダーの位置に関係なくそのデッキは鳴りません。キーボードの上げ下げは 0.05。ジョグ（`[ ]` と `; '`。デッキ B の 10 秒戻しは `Shift+;` と `+`）は再生位置を動かし、キュー位置とクロスフェーダーは変えません。戻しているあいだはスクラッチ（キュルキュル）が鳴り、進めているあいだはその曲が回した速さで鳴ります。手が止まると、そのデッキのテンポに戻り、動かした位置から続きます。戻しているあいだ再生位置は進みません。手を止めた位置は指示したシークの合計で、離すまでの惰性は含みません。プレーヤーがまだジョグ前の位置にいるあいだは、直前に指示した位置へ足す。再生がその位置を離れて指示した位置のほうへ進んだときに記憶は消える。0.05 秒の指示に再生位置が届いたときも消える。ジョグ前の位置のまま、または逆方向に動いただけでは消えない。キューしたとき、時間のバーをクリックしたとき、曲を読み込んだときは、位置を読む前とシークする前に消える。長さが分かっているときはその範囲に収め、プレーヤーが準備完了でないときは何もしません。

画面下の凡例は `djtube/static/keys.js` の `BINDINGS` が出す。

## DDJ-FLX4

`djtube/static/controller.js` の `FLX4_MAP` が、キーボードと同じ actions を呼ぶ。番号は Pioneer の DDJ-FLX4 MIDI Message List（E1）と Mixxx の Pioneer-DDJ-FLX4。チャンネルは 0 始まり。デッキ 1 は 0、デッキ 2 は 1、ミキサーとブラウズは 6。

Web MIDI は安全なページで、「MIDI を開く」を押したときだけ接続する。公開サイトは https://s.cympfh.cc/djtube/ 。画面上部に未接続か、接続したデバイス名が出る。

ジョグは `actions.jog(deck, seconds)`。準備完了のデッキだけ、再生位置を秒数ぶん動かす。戻しはスクラッチで、そのあいだ再生位置は進まない。進めは回転の速さに合わせた再生で、手を止めるとシークした位置からそのデッキのテンポに戻る。離すまでの惰性は位置に含まない。プラッター上面のタッチは `actions.pressDisc(deck, down)`。押しているあいだだけ止まり、離すと、押す前に再生していたときだけ続く。止まっていたデッキは離しても始まらない。回転のシークはそのまま。テンポはキーボードの `setRate`、`nudgeRate`、`resetRate` と、MIDI 値を受ける `setRateFromController`（0–127、64 が 1.0、0 が 0.5、127 が 2.0）。イコライザーは `setEq`、`nudgeEq`、`resetEq`、`setEqFromController`。`setEqFromController` はバンド名と MIDI 0–127 を受け、64 が 0 dB、0 がカット、127 がブーストです。音量は `setVolume`、`nudgeVolume`、`resetVolume` と、MIDI 値を受ける `setVolumeFromController`（0–127 が 0–1）。フィルターは `setFilter`、`nudgeFilter`、`resetFilter` と、MIDI 値を受ける `setFilterFromController`（0–127、64 がバイパス、0 がローパスの左端、127 がハイパスの右端）。FLX4 のテンポはチャンネル 0 がデッキ A、チャンネル 1 がデッキ B の CC 0（MSB）。その CC 値は 0.5 から 2.0 の直線上の `playbackRate` になり、0.25 には丸めない。EQ は HI が CC 7、MID が CC 11、LOW が CC 15。チャンネルフェーダーはチャンネル 0 がデッキ A、チャンネル 1 がデッキ B の CC 19（MSB、0x13）。公式の MIDI Message List（E1）と Mixxx の Pioneer-DDJ-FLX4 では、この MSB がチャンネルの音量で、LSB は CC 51（0x33）です。LSB はマップしていません。フェーダーを底から動かしたときの再生・キュー（ノート 102 / 82）は音量ではないのでマップしていません。EQ とテンポとクロスフェーダーの LSB もマップしていません。CFX はミキサーのチャンネル 6。公式の MIDI Message List（E1）の図 3-5 では、デッキ 1 が CC 23（0x17、MSB）、デッキ 2 が CC 24（0x18、MSB）で、ステータスは 0xB6。Mixxx の Pioneer-DDJ-FLX4 は同じバイトを FILTER CH1 / FILTER CH2 にしている。LSB は CC 55（0x37）と CC 56（0x38）で、マップしていません。曲を載せると、載せたデッキのテンポだけ 1.0 に戻り、そのデッキの HIGH / MID / LOW は 0 dB に戻る。音量とフィルターは、画面のスライダーでもノブでも、載せたあともそのまま。

割り当てている操作:

- PLAY/PAUSE（ch 0/1、ノート 11）: 再生 / 一時停止
- CUE（ch 0/1、ノート 12）: キュー
- LOAD（ch 6、ノート 70 / 71）: 開いているタブで選んでいる曲をデッキ A / B へ。検索なら検索結果、プレイリストならその曲。`loadOpenSelection` がタブを見て、プレイリストのときは `loadPlaylistTrack` と同じ載せ方
- クロスフェーダー MSB（ch 6、CC 31）: クロスフェーダー。値は 0–127
- ジョグ側面・プラッター（ch 0/1、CC 33 / 34 / 35）: 1 目盛り約 0.05 秒。戻すとスクラッチ、進めるとその速さで曲
- ジョグのタッチ（ch 0/1、ノート 54）: 上面を押しているあいだだけ再生を止める。離すと、押す前に再生していたときだけ再開する。止まっていたデッキは再開しない。Shift+タッチ（ノート 103）ではない
- Shift+プラッター（ch 0/1、CC 41）: 1 目盛り約 0.5 秒。向きは同じ
- BROWSE 回転（ch 6、CC 64）: 開いているタブの曲を上下。右回りが下（J）、左回りが上（K）。検索もプレイリストも同じ。`moveSelection` がタブを見て、プレイリストのときは `movePlaylistSelection` と同じ移動をする
- テンポスライダー MSB（ch 0/1、CC 0）: そのデッキの再生速度
- EQ HI / MID / LOW MSB（ch 0/1、CC 7 / 11 / 15）: そのデッキの HIGH / MID / LOW
- チャンネルフェーダー MSB（ch 0/1、CC 19）: そのデッキの音量。0 が 0%、127 が 100%
- CFX MSB（ch 6、CC 23 / 24）: デッキ A / B のフィルター。64 が中央（なし）。左がローパス、右がハイパス

未実装の操作:

- Shift+PLAY/PAUSE、Shift+CUE
- Shift+ジョグのタッチ（ノート 103）
- テンポと EQ の LSB、TRIM
- CFX の LSB（CC 55 / 56）
- チャンネルフェーダーの LSB（CC 51）、クロスフェーダーの LSB
- チャンネルフェーダー開始の再生・キュー（ノート 102 / 82）。音量ではない
- ヘッドホン、MASTER、マイク
- パッド、ループ、BEAT SYNC、エフェクト、SMART CFX、SMART FADER
- BROWSE の押し込みと Shift+BROWSE

## 音源のログ

再生できないときは、サーバの標準エラーに出る `djtube.audio` を見る。

## 開発

```bash
uv run pytest -q
node --test tests/client/player.test.mjs
uv run black --line-length 120 .
```
