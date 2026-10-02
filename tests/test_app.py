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
    assert 'href="/djtube/static/app.css"' in html
    assert 'src="/djtube/static/app.js"' in html
    assert 'id="player-A"' in html
    assert 'id="player-B"' in html
    assert 'id="picture-A"' in html
    assert 'id="picture-B"' in html
    assert 'id="yt-A"' not in html
    assert "<iframe" not in html
    assert "iframe_api" not in html
    assert "イコライザー" in html
    assert 'id="eq-high-A"' in html
    assert 'id="eq-low-B"' in html
    assert "クロスフェーダー" in html
    assert 'id="playlist-name"' in html
    assert 'id="playlist-tracks"' in html
    assert "プレイリスト" in html
    assert "デッキ A" in html
    assert "テンポ" in html
    assert 'id="rate-A" type="range" min="50" max="200" step="1"' in html
    assert 'id="rate-B" type="range" min="50" max="200" step="1"' in html
    assert 'step="25"' not in html
    assert 'id="midi-status"' in html
    assert "未接続 — MIDI を開く（要 HTTPS）" in html
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


def test_readme_and_docker_contract():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "https://s.cympfh.cc/djtube/" in readme
    for token in ("おもちゃ", "toy", "YOUTUBE_API_KEY", "8098", "docker", "uv sync", "127.0.0.1", "yt-dlp", "DDJ-FLX4"):
        assert token not in readme
    agent = (ROOT / "AGENT.md").read_text(encoding="utf-8")
    for token in ("YOUTUBE_API_KEY", "8098", "/djtube/", "DDJ-FLX4", "docker run", "uv sync", "yt-dlp"):
        assert token in agent
    assert "プレイリスト" in readme
    assert "Shift+A" in readme
    assert "DJTUBE_PLAYLISTS" not in readme
    assert "data/playlists.json" not in readme
    assert "AIza" not in readme
    assert "AIza" not in agent
    assert "DJTUBE_PLAYLISTS" in agent
    assert "djtube-data" in agent
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


def test_client_unit_tests():
    subprocess.run(["node", "--test", "tests/client/player.test.mjs"], cwd=ROOT, check=True)
