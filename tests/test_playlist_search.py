from __future__ import annotations

import asyncio
import json
import logging
import sys
import threading
import time

import anyio
import httpx
import pytest
from fastapi.testclient import TestClient

from djtube.app import create_app
from djtube.playlists import PlaylistStore
from djtube.search import Track
from djtube.youtube_playlist import PLAYLIST_ITEM_LIMIT, YouTubePlaylist

LIST = "PL" + "a" * 32
MUSIC = "RDCLAK5uy_" + "m" * 33
KEY = "test-playlist-search-key"
VIDEO = "abcdefghijk"
PASTED = f"https://www.youtube.com/watch?v={VIDEO}"


@pytest.fixture(autouse=True)
def _no_youtube_network(monkeypatch):
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)

    def blocked():
        raise AssertionError("playlist search tried to use the network")

    monkeypatch.setattr("djtube.youtube_playlist._open_client", blocked)

    class YoutubeDL:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("yt-dlp")

    monkeypatch.setattr("yt_dlp.YoutubeDL", YoutubeDL)

    def blocked_popen(*_args, **_kwargs):
        raise AssertionError("yt-dlp")

    monkeypatch.setattr("djtube.youtube_playlist.subprocess.Popen", blocked_popen)


def vid(number: int) -> str:
    return f"v{number:010d}"


def playlist_item(video_id: str, *, title: str = "一覧の題", private: bool = False, deleted: bool = False) -> dict:
    shown = "Deleted video" if deleted else "Private video" if private else title
    item = {
        "snippet": {
            "title": shown,
            "resourceId": {"kind": "youtube#video", "videoId": video_id},
        },
        "contentDetails": {"videoId": video_id},
    }
    if private:
        item["status"] = {"privacyStatus": "private"}
    return item


def video_item(video_id: str, *, title: str = "夜", channel: str = "人", duration: str = "PT4M2S") -> dict:
    return {
        "id": video_id,
        "snippet": {
            "title": title,
            "channelTitle": channel,
            "thumbnails": {"medium": {"url": f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg"}},
        },
        "contentDetails": {"duration": duration},
    }


def _install(monkeypatch, handler):
    transport = httpx.MockTransport(handler)

    def open_client():
        return httpx.Client(transport=transport)

    monkeypatch.setattr("djtube.youtube_playlist._open_client", open_client)
    monkeypatch.setenv("YOUTUBE_API_KEY", KEY)
    return transport


def _client() -> TestClient:
    return TestClient(create_app())


def _track(video_id: str = VIDEO, title: str = "夜") -> Track:
    return Track(video_id, title, "人", 90, None)


def _forbid_search(monkeypatch):
    def fail(*_args, **_kwargs):
        raise AssertionError("keyword search")

    monkeypatch.setattr("djtube.app.search_tracks", fail)


def _forbid_playlist(monkeypatch):
    def fail(*_args, **_kwargs):
        raise AssertionError("playlist fetch")

    monkeypatch.setattr("djtube.app.fetch_youtube_playlist", fail)


def _fake_search(monkeypatch, seen: list):
    def fake(query: str, music: bool = True):
        seen.append((query, music))
        return [_track()], "youtube"

    monkeypatch.setattr("djtube.app.search_tracks", fake)


def _fake_playlist(monkeypatch, seen: list):
    def fake(source: str, **kwargs):
        seen.append((source, kwargs))
        return YouTubePlaylist(LIST, [_track(title="中の曲")], 0, 0)

    monkeypatch.setattr("djtube.app.fetch_youtube_playlist", fake)


def test_a_playlist_url_returns_its_tracks_instead_of_search(monkeypatch):
    _forbid_search(monkeypatch)
    seen: list = []
    _fake_playlist(monkeypatch, seen)
    client = _client()
    response = client.get("/api/search", params={"q": f"https://www.youtube.com/playlist?list={LIST}"})
    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "ytdlp"
    assert [track["title"] for track in body["tracks"]] == ["中の曲"]
    assert seen[0][0] == f"https://www.youtube.com/playlist?list={LIST}"
    assert "limit" not in seen[0][1]
    assert "music" not in seen[0][1]


def test_watch_url_with_a_list_prefers_the_playlist(monkeypatch):
    _forbid_search(monkeypatch)
    seen: list = []
    _fake_playlist(monkeypatch, seen)
    client = _client()
    query = f"https://www.youtube.com/watch?v={VIDEO}&list={LIST}"
    response = client.get("/api/search", params={"q": query, "music": "false"})
    assert response.status_code == 200
    assert response.json()["tracks"][0]["id"] == VIDEO
    assert seen[0][0] == query
    again = client.get("/api/search", params={"q": f"https://music.youtube.com/playlist?list={LIST}", "music": "true"})
    assert again.status_code == 200
    assert len(seen) == 1


def test_a_bare_playlist_id_and_a_music_playlist_are_playlists(monkeypatch):
    _forbid_search(monkeypatch)
    seen: list = []
    _fake_playlist(monkeypatch, seen)
    client = _client()
    assert client.get("/api/search", params={"q": f"  {LIST}  "}).status_code == 200
    assert client.get("/api/search", params={"q": MUSIC}).status_code == 200
    assert seen[0][0] == f"  {LIST}  "
    assert seen[1][0] == MUSIC


def test_a_single_video_and_a_keyword_still_search(monkeypatch):
    _forbid_playlist(monkeypatch)
    seen: list = []
    _fake_search(monkeypatch, seen)
    client = _client()
    video = client.get("/api/search", params={"q": PASTED})
    assert video.status_code == 200
    assert video.json()["tracks"][0]["title"] == "夜"
    word = client.get("/api/search", params={"q": "city pop", "music": "false"})
    assert word.status_code == 200
    bare = client.get("/api/search", params={"q": VIDEO})
    assert bare.status_code == 200
    assert seen == [(PASTED, True), ("city pop", False), (VIDEO, True)]
    for sample in ("UL" + "u" * 22, "PLAYSTATION5", "FLOWER_DANCE"):
        assert client.get("/api/search", params={"q": sample}).status_code == 200
    assert [item[0] for item in seen[-3:]] == ["UL" + "u" * 22, "PLAYSTATION5", "FLOWER_DANCE"]


def test_unimportable_lists_are_rejected_before_youtube(monkeypatch):
    _forbid_search(monkeypatch)
    _forbid_playlist(monkeypatch)
    client = _client()
    samples = [
        "WL",
        "LL",
        "LM",
        "RDMM",
        "RD" + VIDEO,
        f"https://www.youtube.com/playlist?list=WL",
        f"https://www.youtube.com/watch?v={VIDEO}&list=RDMM",
        f"https://music.youtube.com/watch?v={VIDEO}&list=LL",
    ]
    for sample in samples:
        response = client.get("/api/search", params={"q": sample})
        assert response.status_code == 400, sample
        assert response.json()["detail"] == "取り込めない種類のリストです"


def test_a_long_keyword_is_still_cut_to_120_characters(monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", KEY)
    seen = {}

    def fake(query: str, key: str, client=None, *, music: bool = False):
        seen["query"] = query
        seen["music"] = music
        return [_track()]

    monkeypatch.setattr("djtube.search.search_youtube_api", fake)
    client = _client()
    response = client.get("/api/search", params={"q": "あ" * 121})
    assert response.status_code == 200
    assert seen["query"] == "あ" * 120
    assert seen["music"] is True
    too_long = client.get("/api/search", params={"q": "あ" * 2001})
    assert too_long.status_code == 422


def test_a_playlist_url_longer_than_120_characters_still_lists_tracks(monkeypatch):
    _forbid_search(monkeypatch)
    seen: list = []
    _fake_playlist(monkeypatch, seen)
    query = f"https://www.youtube.com/watch?v={VIDEO}&list={LIST}&si={'s' * 40}"
    assert len(query) > 120
    client = _client()
    response = client.get("/api/search", params={"q": query})
    assert response.status_code == 200
    assert seen[0][0] == query


def test_private_and_deleted_videos_are_omitted(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params.get("key") == KEY
        if request.url.path.endswith("/playlistItems"):
            return httpx.Response(
                200,
                json={
                    "items": [
                        playlist_item(vid(0), title="残す"),
                        playlist_item(vid(1), private=True),
                        playlist_item(vid(2), deleted=True),
                        playlist_item(vid(3), title="もう一曲"),
                    ],
                    "pageInfo": {"totalResults": 4},
                },
            )
        ids = request.url.params.get("id", "").split(",")
        assert vid(1) not in ids
        assert vid(2) not in ids
        return httpx.Response(
            200,
            json={"items": [video_item(video_id, title=f"曲{video_id}") for video_id in ids if video_id]},
        )

    _install(monkeypatch, handler)
    client = _client()
    response = client.get("/api/search", params={"q": f"https://www.youtube.com/playlist?list={LIST}"})
    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "youtube"
    assert [track["id"] for track in body["tracks"]] == [vid(0), vid(3)]
    assert KEY not in response.text


def test_display_stops_at_the_scan_cap(monkeypatch):
    monkeypatch.setattr("djtube.youtube_playlist.PLAYLIST_ITEM_LIMIT", 2)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/playlistItems"):
            assert request.url.params.get("maxResults") == "2"
            return httpx.Response(
                200,
                json={
                    "items": [playlist_item(vid(index), title=f"曲{index}") for index in range(5)],
                    "pageInfo": {"totalResults": 5},
                },
            )
        ids = request.url.params.get("id", "").split(",")
        return httpx.Response(200, json={"items": [video_item(video_id) for video_id in ids if video_id]})

    _install(monkeypatch, handler)
    client = _client()
    response = client.get("/api/search", params={"q": LIST})
    assert response.status_code == 200
    assert [track["id"] for track in response.json()["tracks"]] == [vid(0), vid(1)]


def test_a_playlist_longer_than_a_search_page_is_not_cut_to_fifty(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/playlistItems"):
            page = request.url.params.get("pageToken")
            if not page:
                return httpx.Response(
                    200,
                    json={
                        "items": [playlist_item(vid(index)) for index in range(50)],
                        "nextPageToken": "page-2",
                        "pageInfo": {"totalResults": 60},
                    },
                )
            assert page == "page-2"
            return httpx.Response(
                200,
                json={
                    "items": [playlist_item(vid(index)) for index in range(50, 60)],
                    "pageInfo": {"totalResults": 60},
                },
            )
        ids = request.url.params.get("id", "").split(",")
        return httpx.Response(200, json={"items": [video_item(video_id) for video_id in ids if video_id]})

    _install(monkeypatch, handler)
    client = _client()
    response = client.get("/api/search", params={"q": LIST})
    assert response.status_code == 200
    ids = [track["id"] for track in response.json()["tracks"]]
    assert len(ids) == 60
    assert ids[0] == vid(0)
    assert ids[-1] == vid(59)
    assert PLAYLIST_ITEM_LIMIT == 500


def test_quota_and_missing_playlist_use_the_import_messages(monkeypatch, caplog):
    caplog.set_level(logging.INFO)

    def quota(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={"error": {"code": 403, "message": KEY, "errors": [{"reason": "quotaExceeded"}]}},
        )

    _install(monkeypatch, quota)
    client = _client()
    response = client.get("/api/search", params={"q": LIST})
    assert response.status_code == 429
    assert response.json()["detail"] == "YouTube API の上限に達しました"
    assert KEY not in response.text
    assert KEY not in caplog.text

    def missing(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            json={"error": {"code": 404, "message": KEY, "errors": [{"reason": "playlistNotFound"}]}},
        )

    _install(monkeypatch, missing)
    client = _client()
    response = client.get("/api/search", params={"q": LIST})
    assert response.status_code == 404
    assert response.json()["detail"] == "そのプレイリストは見つかりませんでした"
    assert KEY not in response.text

    def broken(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"message": KEY}})

    _install(monkeypatch, broken)
    client = _client()
    response = client.get("/api/search", params={"q": LIST})
    assert response.status_code == 502
    assert response.json()["detail"] == "プレイリストを取れませんでした"
    assert KEY not in response.text


def test_an_empty_playlist_is_an_empty_result(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/playlistItems"):
            return httpx.Response(200, json={"items": [playlist_item(vid(0), private=True)], "pageInfo": {"totalResults": 1}})
        return httpx.Response(200, json={"items": []})

    _install(monkeypatch, handler)
    client = _client()
    response = client.get("/api/search", params={"q": LIST})
    assert response.status_code == 200
    assert response.json() == {"source": "youtube", "tracks": []}


def test_without_a_key_search_uses_the_same_ytdlp_playlist(monkeypatch):
    seen = {}

    def popen(cmd, stdout=None, stderr=None, stdin=None, start_new_session=False, **kwargs):
        seen["cmd"] = list(cmd)
        seen["start_new_session"] = start_new_session
        end = int(cmd[cmd.index("--playlist-end") + 1])
        seen["end"] = end
        payload = {
            "entries": [
                {"id": VIDEO, "title": "夜", "channel": "人", "duration": 90},
                {"id": vid(1), "title": "[Private video]", "availability": "private"},
                {"id": vid(2), "title": "[Deleted video]"},
            ],
            "playlist_count": 3,
        }
        if stdout is not None:
            stdout.write(json.dumps(payload).encode())
            stdout.seek(0)

        class Proc:
            returncode = 0
            pid = 424242

            def poll(self):
                return 0

            def wait(self, timeout=None):
                return 0

            def kill(self):
                return None

        return Proc()

    monkeypatch.setattr("djtube.youtube_playlist.subprocess.Popen", popen)
    monkeypatch.setattr("djtube.youtube_playlist.os.killpg", lambda *_args: None)
    client = _client()
    response = client.get("/api/search", params={"q": f"https://music.youtube.com/playlist?list={LIST}"})
    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "ytdlp"
    assert [track["id"] for track in body["tracks"]] == [VIDEO]
    assert seen["cmd"][0] == sys.executable
    assert seen["cmd"][1:4] == ["-m", "yt_dlp", "--ignore-config"]
    assert "--flat-playlist" in seen["cmd"]
    assert seen["end"] == PLAYLIST_ITEM_LIMIT
    assert seen["start_new_session"] is True
    assert f"https://www.youtube.com/playlist?list={LIST}" in seen["cmd"]


def test_playlist_search_shares_the_import_rate_limit(tmp_path, monkeypatch):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if request.url.path.endswith("/playlistItems"):
            return httpx.Response(
                200,
                json={"items": [playlist_item(vid(0))], "pageInfo": {"totalResults": 1}},
            )
        return httpx.Response(200, json={"items": [video_item(vid(0))]})

    _install(monkeypatch, handler)
    client = TestClient(create_app(PlaylistStore(tmp_path / "playlists.json")))
    clock = {"t": 2_000.0}
    client.app.state.import_slot.now = lambda: clock["t"]
    created = client.post("/api/playlists", json={"name": "夜"}).json()
    for index in range(5):
        clock["t"] = 2_000.0 + index
        response = client.post("/api/playlists/import", json={"url": LIST, "playlist_id": created["id"]})
        assert response.status_code == 200, response.text
    listed = client.get("/api/search", params={"q": LIST})
    assert listed.status_code == 200
    blocked = client.get("/api/search", params={"q": MUSIC})
    assert blocked.status_code == 429
    assert blocked.json()["detail"] == "取り込みの回数が多いです"
    clock["t"] = 2_000.0 + 60.0
    again = client.get("/api/search", params={"q": MUSIC})
    assert again.status_code == 200


def test_a_cached_playlist_does_not_call_youtube_or_spend_a_slot(monkeypatch):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if request.url.path.endswith("/playlistItems"):
            return httpx.Response(
                200,
                json={"items": [playlist_item(vid(0), title="夜")], "pageInfo": {"totalResults": 1}},
            )
        return httpx.Response(200, json={"items": [video_item(vid(0), title="夜")]})

    _install(monkeypatch, handler)
    client = _client()
    clock = {"t": 8_000.0}
    client.app.state.playlist_search_cache.now = lambda: clock["t"]
    first = client.get("/api/search", params={"q": f"https://youtu.be/{VIDEO}?list={LIST}"})
    assert first.status_code == 200
    assert calls["n"] == 2
    slot = client.app.state.import_slot
    stamp = slot.now()
    slot._minute.clear()
    for _index in range(6):
        slot._minute.append(stamp)
    cached = client.get("/api/search", params={"q": LIST, "music": "false"})
    assert cached.status_code == 200
    assert cached.json()["tracks"][0]["title"] == "夜"
    assert calls["n"] == 2
    clock["t"] = 8_000.0 + 599.999
    still = client.get("/api/search", params={"q": LIST})
    assert still.status_code == 200
    assert calls["n"] == 2
    clock["t"] = 8_000.0 + 600.0
    slot._minute.clear()
    refreshed = client.get("/api/search", params={"q": LIST})
    assert refreshed.status_code == 200
    assert calls["n"] == 4
    stored = json.dumps(list(client.app.state.playlist_search_cache._items.values()), default=str)
    assert KEY not in stored


def test_errors_are_not_cached(monkeypatch):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                404,
                json={"error": {"code": 404, "errors": [{"reason": "playlistNotFound"}]}},
            )
        if request.url.path.endswith("/playlistItems"):
            return httpx.Response(
                200,
                json={"items": [playlist_item(vid(0))], "pageInfo": {"totalResults": 1}},
            )
        return httpx.Response(200, json={"items": [video_item(vid(0))]})

    _install(monkeypatch, handler)
    client = _client()
    missing = client.get("/api/search", params={"q": LIST})
    assert missing.status_code == 404
    found = client.get("/api/search", params={"q": LIST})
    assert found.status_code == 200
    assert calls["n"] == 3


def test_the_cache_drops_the_oldest_playlist_past_32(monkeypatch):
    alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz_-"
    ids = ["PL" + "b" * 31 + alphabet[index] for index in range(33)]
    calls: dict[str, int] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/playlistItems"):
            playlist_id = request.url.params.get("playlistId")
            calls[playlist_id] = calls.get(playlist_id, 0) + 1
            return httpx.Response(
                200,
                json={"items": [playlist_item(vid(0))], "pageInfo": {"totalResults": 1}},
            )
        return httpx.Response(200, json={"items": [video_item(vid(0))]})

    _install(monkeypatch, handler)
    client = _client()
    slot_clock = {"t": 20_000.0}
    client.app.state.import_slot.now = lambda: slot_clock["t"]
    client.app.state.playlist_search_cache.now = lambda: 1.0
    for index, playlist_id in enumerate(ids):
        slot_clock["t"] += 61
        response = client.get(
            "/api/search",
            params={"q": playlist_id},
            headers={"X-Real-IP": f"203.0.113.{index + 1}"},
        )
        assert response.status_code == 200, response.text
    slot_clock["t"] += 61
    again = client.get("/api/search", params={"q": ids[0]}, headers={"X-Real-IP": "198.51.100.1"})
    assert again.status_code == 200
    assert calls[ids[0]] == 2
    kept = client.get("/api/search", params={"q": ids[2]}, headers={"X-Real-IP": "198.51.100.2"})
    assert kept.status_code == 200
    assert calls[ids[2]] == 1


def test_one_playlist_search_at_a_time_leaves_the_event_loop_free(tmp_path, monkeypatch):
    started = threading.Event()
    release = threading.Event()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/playlistItems"):
            started.set()
            assert release.wait(5)
            return httpx.Response(
                200,
                json={"items": [playlist_item(vid(0))], "pageInfo": {"totalResults": 1}},
            )
        return httpx.Response(200, json={"items": [video_item(vid(0))]})

    _install(monkeypatch, handler)
    app = create_app(PlaylistStore(tmp_path / "playlists.json"))

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            task = asyncio.create_task(http.get("/api/search", params={"q": LIST}))
            assert await asyncio.to_thread(started.wait, 2)
            health_at = time.monotonic()
            health = await asyncio.wait_for(http.get("/api/health"), 1)
            assert health.status_code == 200
            assert time.monotonic() - health_at < 0.8
            second = await asyncio.wait_for(http.get("/api/search", params={"q": MUSIC}), 1)
            assert second.status_code == 429
            assert second.json()["detail"] == "プレイリストを取得中です"
            blocked = await asyncio.wait_for(
                http.post("/api/playlists/import", json={"url": LIST, "name": "朝"}),
                1,
            )
            assert blocked.status_code == 429
            assert blocked.json()["detail"] == "プレイリストを取得中です"
            release.set()
            first = await asyncio.wait_for(task, 2)
            assert first.status_code == 200
            assert first.json()["tracks"][0]["id"] == vid(0)

    anyio.run(scenario)


def test_a_playlist_search_waits_out_an_import(tmp_path, monkeypatch):
    started = threading.Event()
    release = threading.Event()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/playlistItems"):
            started.set()
            assert release.wait(5)
            return httpx.Response(
                200,
                json={"items": [playlist_item(vid(0))], "pageInfo": {"totalResults": 1}},
            )
        return httpx.Response(200, json={"items": [video_item(vid(0))]})

    _install(monkeypatch, handler)
    app = create_app(PlaylistStore(tmp_path / "playlists.json"))

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            task = asyncio.create_task(http.post("/api/playlists/import", json={"url": LIST, "name": "朝"}))
            assert await asyncio.to_thread(started.wait, 2)
            searched = await asyncio.wait_for(http.get("/api/search", params={"q": LIST}), 1)
            assert searched.status_code == 429
            assert searched.json()["detail"] == "取り込み中です"
            release.set()
            first = await asyncio.wait_for(task, 2)
            assert first.status_code == 200

    anyio.run(scenario)


def test_keyword_search_does_not_spend_the_import_budget(monkeypatch):
    _forbid_playlist(monkeypatch)
    _fake_search(monkeypatch, [])
    client = _client()
    clock = {"t": 3_000.0}
    client.app.state.import_slot.now = lambda: clock["t"]
    for _index in range(7):
        response = client.get("/api/search", params={"q": "city pop"})
        assert response.status_code == 200
    rejected = client.get("/api/search", params={"q": "WL"})
    assert rejected.status_code == 400
    assert len(client.app.state.import_slot._minute) == 0
