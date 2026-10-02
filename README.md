# DJTUBE

YouTube の曲を 2 デッキで再生する、ブラウザの DJ おもちゃです。検索はサーバ側で行い、再生はブラウザ上の YouTube プレーヤーが 2 つ鳴ります。クロスフェーダーはその 2 つの音量を変えます。

公開 URL は [https://s.cympfh.cc/djtube/](https://s.cympfh.cc/djtube/) です。

## 動き

- デッキ A / B、再生・一時停止、キュー、クロスフェーダー
- 検索語を入れると結果からデッキへ載せる
- 各デッキは YouTube IFrame Player API。音は見ているブラウザが YouTube から直接鳴らす
- キーボードだけで一通り操作できる
- Pioneer DDJ-FLX4 の MIDI 割り当ては未実装。受信口だけある

サーバで yt-dlp を使ってファイルを落として配る方式は採っていません。YouTube がデータセンターの IP を bot 確認で止めることがあり、その場合サーバ再生は鳴りません。埋め込みプレーヤーなら、音は利用者のブラウザから出ます。

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

- キーがあるとき、検索はサーバが Data API を呼ぶ
- キーが無い、または API が失敗したときは yt-dlp の検索に落ちる
- キーはクライアントの JS / HTML に埋め込まない。レスポンスにも載せない

## キーボード

画面下にも同じ割り当てが出ます。検索欄にフォーカスがあるあいだ、文字キーは検索語になります。`Enter`、`↑` / `↓`、`Esc` だけが操作になります。

| キー | 操作 |
| --- | --- |
| `/` | 検索欄へフォーカス |
| `Enter` | 検索欄では検索。同じ語で結果があるときは、もう一度押すとロード先デッキへ載せる。欄の外ではロード |
| `Esc` | 検索欄から抜ける |
| `↑` / `K` | 結果を上へ（検索欄の中でも） |
| `↓` / `J` | 結果を下へ（検索欄の中でも） |
| `A` | 選択中の曲をデッキ A へ |
| `B` | 選択中の曲をデッキ B へ |
| `T` | 次のロード先を A / B で切り替え |
| `Q` | デッキ A の再生 / 一時停止 |
| `W` | デッキ B の再生 / 一時停止 |
| `Z` | デッキ A のキュー。再生中ならキュー位置へ戻して停止。停止中でキュー位置にいるなら再生 |
| `X` | デッキ B のキュー。動きは Z と同じ |
| `Shift+Z` / `Shift+X` | 今の再生位置をそのデッキのキュー位置にする |
| `←` / `,` | クロスフェーダーを A 側へ |
| `→` / `.` | クロスフェーダーを B 側へ |
| `Shift+←` / `Shift+→` | フェーダーを大きく動かす |
| `Home` / `End` | フェーダーを A 端 / B 端へ |

クロスフェーダーは等パワー（中央で両方とも約 71%）。画面の「音量」がその値です。動画の上には透明な覆いがあり、クリックしてもフォーカスはページに残ります。

## DDJ-FLX4

割り当ては未実装です。コントローラ用の入口は `djtube/static/controller.js` の `FLX4_MAP` と `dispatchControllerEvent` だけです。

マップは空です。Web MIDI は「MIDI を開く」を押したときだけ接続します。来た信号は、マップに無いのでデッキを動かさず、件数だけ数えます。

あとからノート番号や CC を `FLX4_MAP` に書くと、キーボードと同じ actions（再生、キュー、ロード、フェーダー）が呼ばれます。デッキ側の書き換えは要りません。チャンネルは 0–15。フェーダー用の CC は `setCrossfaderFromController` に `passValue: true` を渡し、値は MIDI の 0–127。実機の番号はここには書いていません。

## 開発

```bash
uv run pytest -q
node --test tests/client/player.test.mjs
uv run black --line-length 120 .
```
