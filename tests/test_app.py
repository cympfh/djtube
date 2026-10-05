from __future__ import annotations

import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from djtube.app import create_app
from djtube.paths import PUBLIC_PREFIX, STATIC_DIR
from djtube.search import Track

ROOT = Path(__file__).resolve().parents[1]
VIDEO_ID = "abcdefghijk"


def test_health_and_public_prefix():
    client = TestClient(create_app())
    direct = client.get("/api/health")
    prefixed = client.get(f"{PUBLIC_PREFIX}/api/health")
    assert direct.status_code == 200
    assert prefixed.json() == direct.json()
    body = direct.json()
    assert body["ok"] is True
    assert body["prefix"] == "/djtube"
    assert body["flx4"] == "mapped"
    assert body["playback"] == "ytdlp-stream"
    assert body["search"] in {"youtube", "ytdlp"}


def test_index_uses_public_asset_prefix():
    client = TestClient(create_app())
    html = client.get("/djtube/").text
    assert "__PUBLIC_PREFIX__" not in html
    assert 'href="/djtube/static/app.css?v=' in html
    assert 'src="/djtube/static/app.js?v=' in html
    assert 'id="player-A"' in html
    assert 'id="player-B"' in html
    assert 'id="picture-A"' in html
    assert 'id="picture-B"' in html
    assert 'id="disc-A"' in html
    assert 'id="disc-B"' in html
    assert 'class="deck-disc-ring"' in html
    assert 'class="deck-disc-mark"' in html
    assert 'fill-rule="evenodd"' in html
    assert 'id="yt-A"' not in html
    assert "<iframe" not in html
    assert "iframe_api" not in html
    assert "イコライザー" in html
    assert 'id="eq-high-A"' in html
    assert 'id="eq-low-B"' in html
    assert "クロスフェーダー" in html
    assert 'id="tab-search"' in html
    assert 'id="tab-playlist"' in html
    assert 'id="search-panel"' in html
    assert "hidden" in html
    assert 'id="playlist-name"' in html
    assert 'id="playlist-tracks"' in html
    assert 'id="playlist-select"' in html
    assert 'id="playlist-edit"' in html
    assert ">編集</button>" in html
    assert html.index('id="playlist-select"') < html.index('id="playlist-edit"')
    assert 'id="playlist-edit-dialog"' in html
    assert 'id="playlist-rename"' in html
    assert ">リネーム</button>" in html
    assert 'id="playlist-delete"' in html
    assert ">プレイリスト削除</button>" in html
    assert ">変える</button>" not in html
    assert 'id="playlist-add-search"' not in html
    assert "プレイリスト" in html
    assert "デッキ A" in html
    assert "テンポ" in html
    assert 'id="rate-A" type="range" min="50" max="200" step="1"' in html
    assert 'id="rate-B" type="range" min="50" max="200" step="1"' in html
    for deck in ("A", "B"):
        head = html.split(f'id="rate-readout-{deck}"', 1)[1].split(f'id="rate-{deck}"', 1)[0]
        assert f'id="bpm-readout-{deck}"' in head
        assert "aria-live" not in head
        assert head.index(f'id="bpm-readout-{deck}"') < head.index(f'id="rate-reset-{deck}"')
        assert ">– BPM</span>" in head
    assert 'id="volume-A" type="range" min="0" max="100" step="1"' in html
    assert 'id="volume-B" type="range" min="0" max="100" step="1"' in html
    assert 'id="volume-reset-A"' in html
    assert 'id="volume-reset-B"' in html
    assert 'id="filter-A" type="range" min="0" max="100" step="1"' in html
    assert 'id="filter-B" type="range" min="0" max="100" step="1"' in html
    assert 'id="filter-reset-A"' in html
    assert 'id="filter-reset-B"' in html
    assert "中央に戻す" in html
    assert "フェード" in html
    assert 'step="25"' not in html
    assert 'id="midi-button"' in html
    assert 'data-midi="off"' in html
    assert 'id="midi-status"' in html
    assert "MIDI未接続" in html
    assert "T で切り替え" not in html
    assert "次のロード先" not in html
    assert 'id="target-A"' not in html
    assert 'id="target-B"' not in html
    assert "Enter で作る" not in html
    js = client.get("/djtube/static/player.js")
    assert js.status_code == 200
    assert "createMediaElementSource" in js.text
    assert "connectEqGraph" in js.text
    assert "playbackRate" in js.text
    assert "iframe_api" not in js.text
    assert "YOUTUBE_API_KEY" not in js.text
    assert client.get("/djtube/static/youtube.js").status_code == 404


def test_client_sources_do_not_carry_the_api_key():
    banned = ("YOUTUBE_API_KEY", "AIza", "googleapis.com")
    files = list(STATIC_DIR.rglob("*")) + list((ROOT / "djtube" / "templates").rglob("*"))
    text = "\n".join(path.read_text(encoding="utf-8") for path in files if path.is_file())
    for token in banned:
        assert token not in text


def test_search_route_defaults_to_music_and_can_turn_it_off(monkeypatch):
    seen = {}

    def fake(query: str, music: bool = True):
        seen["query"] = query
        seen["music"] = music
        return [Track(VIDEO_ID, "曲", "人", 12, None)], "youtube"

    monkeypatch.setattr("djtube.app.search_tracks", fake)
    client = TestClient(create_app())
    assert client.get("/api/search", params={"q": "同人誌"}).status_code == 200
    assert seen == {"query": "同人誌", "music": True}
    assert client.get("/api/search", params={"q": "同人誌", "music": "false"}).status_code == 200
    assert seen["music"] is False


def test_search_route_does_not_echo_key(monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "test-key")

    def fake(_query: str, music: bool = True):
        assert music is True
        return [Track(VIDEO_ID, "曲", "人", 12, "https://i.ytimg.com/a.jpg")], "youtube"

    monkeypatch.setattr("djtube.app.search_tracks", fake)
    client = TestClient(create_app())
    response = client.get("/api/search", params={"q": "city pop"})
    assert response.status_code == 200
    assert "test-key" not in response.text
    assert response.json()["tracks"][0]["id"] == VIDEO_ID


def test_playlist_rows_load_onto_a_deck_and_remove_is_red():
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    body = js[js.index("function renderPlaylists()") : js.index("function renderAddNote()")]
    assert "actions.loadPlaylistTrack(deck)" in body
    assert "`${deck}へ`" in body
    assert 'remove.className = "is-remove"' in body
    assert 'remove.textContent = confirming ? "本当に削除？" : "削除"' in body
    click = body[body.index('remove.addEventListener("click"') : body.index("buttons.append(remove)")]
    assert "if (!confirming)" in click
    assert click.index("return") < click.index("actions.removePlaylistTrack()")
    assert "actions.removePlaylistTrack()" in click
    assert "actions.movePlaylistTrack" not in body
    assert "actions.placePlaylistTrack" not in body
    assert "trackGripElement()" in body
    assert "bindTrackReorder(grip, li, index, track.id)" in body
    assert body.index("trackGripElement()") < body.index('className = "thumb"')
    assert "img.draggable = false" in body
    assert "draggable = true" not in js
    assert 'className = "track-grip"' in js
    assert "actions.placePlaylistTrack(fromIndex, order, trackId)" in js
    assert 'grip.addEventListener("pointerdown"' in js
    assert '"上"' not in body
    assert '"下"' not in body
    rule = css.split(".result-actions button.is-remove", 1)[1].split("}", 1)[0]
    assert "#ff4d3a" in rule
    assert "background:" in rule
    actions = css.split(".result-actions {", 1)[1].split("}", 1)[0]
    assert "flex-direction: row" in actions
    assert "column" not in actions
    tail = css.split(".result-actions button:last-child", 1)[1].split("}", 1)[0]
    assert "margin-left: auto" in tail
    row = css.split(".results li {", 1)[1].split("}", 1)[0]
    assert "52px minmax(0, 1fr)" in row
    assert '"actions actions"' in row
    playlist_row = css.split("#playlist-tracks > li {", 1)[1].split("}", 1)[0]
    assert "28px 52px minmax(0, 1fr)" in playlist_row
    assert '"grip thumb copy"' in playlist_row
    assert '"actions actions actions"' in playlist_row
    grip_rule = css.split("#playlist-tracks > li > .track-grip {", 1)[1].split("}", 1)[0]
    assert "grid-area: grip" in grip_rule
    assert "cursor: grab" in grip_rule


def test_readme_and_docker_contract():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "https://s.cympfh.cc/djtube/" in readme
    for token in (
        "おもちゃ",
        "toy",
        "YOUTUBE_API_KEY",
        "8098",
        "docker",
        "uv sync",
        "127.0.0.1",
        "yt-dlp",
        "DDJ-FLX4",
        "tv_downgraded",
        "web_embedded",
        "web_safari",
        "/opt/djtube/deno",
    ):
        assert token not in readme
    agent = (ROOT / "AGENT.md").read_text(encoding="utf-8")
    for token in ("YOUTUBE_API_KEY", "8098", "/djtube/", "DDJ-FLX4", "docker run", "uv sync", "yt-dlp"):
        assert token in agent
    assert "プレイリスト" in readme
    assert "Shift+A" in readme
    assert "各曲の「Aへ」「Bへ」で、そのデッキに載せる" in readme
    assert "「削除」を一度押すとボタンが「本当に削除？」になり、もう一度押すとその曲だけ外れる" in readme
    assert "曲の絵の左を掴んでドラッグすると、離した位置にその曲の順番が移る" in readme
    assert "`A` で選択中の曲をデッキ A へ" in readme
    assert "`B` で選択中の曲をデッキ B へ" in readme
    assert "`Enter` で検索する" in readme
    assert "中央はフィルターなし" in readme
    assert "`T` / `Shift+T`" in readme
    assert "`Shift+Q` / `Shift+W`" in readme
    assert "名前欄で `Enter` を押すと作る" in readme
    for gone in ("T で切り替え", "次のロード先", "ロード先を切り替え", "検索 / ロード", "検索欄の外で `Enter`"):
        assert gone not in readme
    assert "サインイン" in readme
    assert "書き出す" in readme
    assert "アップロード" in readme
    assert "貼り付け" in readme
    assert "画面上部の丸い Cookie の印" in readme
    assert "画面上部の「Cookie」" not in readme
    assert "再生が失敗する前でも" in readme
    assert "曲を載せても、そのデッキの音量は戻らない" in readme
    assert "曲を載せると、そのデッキの音量は 100% に戻る" not in readme
    assert "クロスフェーダーとは別" in readme
    assert "DJTUBE_PLAYLISTS" not in readme
    assert "DJTUBE_COOKIES" not in readme
    assert "data/playlists.json" not in readme
    assert "data/cookies.txt" not in readme
    assert "cookiefile" not in readme
    assert "AIza" not in readme
    assert "AIza" not in agent
    assert "DJTUBE_PLAYLISTS" in agent
    assert "DJTUBE_COOKIES" in agent
    assert "cookiefile" in agent
    assert "djtube-data" in agent
    assert "djtube.audio" in agent
    assert "Bot判定" not in agent
    assert "同意画面" not in agent
    assert "djtube.audio" not in readme
    assert "音源のログ" not in readme
    assert "Bot判定" not in readme
    assert "ボリュームを付けません" in agent
    assert "未実装" in agent
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "8098" in dockerfile
    assert "YOUTUBE_API_KEY=" not in dockerfile
    assert "ffmpeg" not in dockerfile
    assert "/app/data" in dockerfile
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    assert "8098:8098" in compose
    assert "YOUTUBE_API_KEY" in compose
    assert "djtube-data:/app/data" in compose
    assert "DJTUBE_COOKIES=/app/data/cookies.txt" in dockerfile
    assert "DJTUBE_COOKIES: /app/data/cookies.txt" in compose
    assert "/opt/djtube/deno" in dockerfile
    assert "/opt/djtube" not in dockerfile.split("ENV PATH=", 1)[1].split("\n", 1)[0]


def test_client_unit_tests():
    subprocess.run(["node", "--test", "tests/client/player.test.mjs"], cwd=ROOT, check=True)
