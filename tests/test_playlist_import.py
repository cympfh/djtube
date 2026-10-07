from __future__ import annotations

import asyncio
import json
import logging
import signal
import subprocess
import sys
import threading
import time

_REAL_POPEN = subprocess.Popen

import anyio
import httpx
import pytest
from fastapi.testclient import TestClient

from djtube.app import create_app
from djtube.playlists import PlaylistStore
from djtube.search import Track
from djtube.youtube_playlist import (
    PLAYLIST_ITEM_LIMIT,
    ImportCancelled,
    PlaylistLookupError,
    _unavailable_item,
    fetch_youtube_playlist,
    parse_playlist_id,
    rejected_playlist_message,
)

LIST = "PL" + "a" * 32
ALBUM = "OLAK5uy_l1m0thk3g31NmIIz_vMIbWtyv7eZixlH0"
KEY = "test-playlist-key"
VIDEO = "abcdefghijk"
OTHER = "bbbbbbbbbbb"
THIRD = "ccccccccccc"


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


@pytest.fixture(autouse=True)
def _no_youtube_network(monkeypatch):
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)

    def blocked():
        raise AssertionError("youtube playlist lookup tried to use the network")

    monkeypatch.setattr("djtube.youtube_playlist._open_client", blocked)

    class YoutubeDL:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("yt-dlp")

    monkeypatch.setattr("yt_dlp.YoutubeDL", YoutubeDL)

    def blocked_popen(*_args, **_kwargs):
        raise AssertionError("yt-dlp")

    monkeypatch.setattr("djtube.youtube_playlist.subprocess.Popen", blocked_popen)


def _install(monkeypatch, handler):
    transport = httpx.MockTransport(handler)

    def open_client():
        return httpx.Client(transport=transport)

    monkeypatch.setattr("djtube.youtube_playlist._open_client", open_client)
    monkeypatch.setenv("YOUTUBE_API_KEY", KEY)
    return transport


def _app(tmp_path, **limits):
    store = PlaylistStore(tmp_path / "playlists.json", **limits)
    return TestClient(create_app(store)), store


def test_parse_playlist_id_accepts_urls_and_a_bare_id():
    bare = LIST
    album = ALBUM
    samples = {
        bare: bare,
        f"  {bare}  ": bare,
        f"https://www.youtube.com/playlist?list={bare}": bare,
        f"https://youtube.com/playlist?list={bare}&si=tracking": bare,
        f"http://m.youtube.com/playlist?list={bare}": bare,
        f"https://music.youtube.com/playlist?list={bare}": bare,
        f"https://www.youtube.com/watch?v={VIDEO}&list={bare}": bare,
        f"https://www.youtube.com/watch?list={bare}&v={VIDEO}": bare,
        f"https://youtu.be/{VIDEO}?list={bare}": bare,
        f"https://www.youtube-nocookie.com/embed/videoseries?list={bare}": bare,
        f"youtube.com/playlist?list={bare}": bare,
        f"//www.youtube.com/playlist?list={bare}": bare,
        "PL" + "c" * 16: "PL" + "c" * 16,
        "UU" + "b" * 22: "UU" + "b" * 22,
        "FL" + "d" * 22: "FL" + "d" * 22,
        "EC" + "e" * 32: "EC" + "e" * 32,
        album: album,
        f"https://www.youtube.com/playlist?list={album}": album,
        "RDCLAK5uy_" + "m" * 33: "RDCLAK5uy_" + "m" * 33,
    }
    for sample, expected in samples.items():
        assert parse_playlist_id(sample) == expected


def test_parse_playlist_id_rejects_anything_that_is_not_a_playlist():
    rejected = [
        "",
        "   ",
        VIDEO,
        "PL",
        "PL" + "a" * 9,
        "PL" + "a" * 10,
        "PL" + "a" * 15,
        "PL" + "a" * 17,
        "PL" + "a" * 31,
        "PL" + "a" * 33,
        "PL" + "a" * 65,
        "pl" + "a" * 32,
        "PLAYSTATION5",
        "FLOWER_DANCE",
        "TLC_waterfalls",
        "WL",
        "LL",
        "LM",
        "RDMM",
        "RD" + VIDEO,
        "RDCLAK5uy_" + "m" * 32,
        "RDCLAK5uy_" + "m" * 34,
        "UL" + "u" * 22,
        "PU" + "p" * 22,
        "TL" + "t" * 22,
        "UU" + "b" * 21,
        "UU" + "b" * 23,
        "FL" + "d" * 10,
        f"https://www.youtube.com/watch?v={VIDEO}",
        f"https://youtu.be/{VIDEO}",
        f"https://evil.example/playlist?list={LIST}",
        f"https://www.youtube.com.evil.example/playlist?list={LIST}",
        f"https://www.googleapis.com/youtube/v3/playlistItems?playlistId={LIST}",
        f"https://user:secret@www.youtube.com/playlist?list={LIST}",
        "https://www.youtube.com/playlist?list=not-a-list",
        "javascript:alert(1)",
        f"{LIST[:2]}\n{LIST[2:]}",
        f"https://www.youtube.com/playlist?list={LIST}/../x",
        None,
        12,
        "OLAK5uy_short",
    ]
    for sample in rejected:
        assert parse_playlist_id(sample) is None


def test_pagination_keeps_playlist_order_when_video_details_come_back_reversed(monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", KEY)
    calls = {"items": 0, "videos": []}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "www.googleapis.com"
        if request.url.path.endswith("/playlistItems"):
            calls["items"] += 1
            page = request.url.params.get("pageToken")
            if not page:
                return httpx.Response(
                    200,
                    json={
                        "items": [playlist_item(vid(0)), playlist_item(vid(1))],
                        "nextPageToken": "page-2",
                    },
                )
            assert page == "page-2"
            return httpx.Response(200, json={"items": [playlist_item(vid(2))]})
        requested = request.url.params["id"].split(",")
        calls["videos"].append(requested)
        reversed_items = [video_item(video_id, title=f"曲{video_id}") for video_id in reversed(requested)]
        return httpx.Response(200, json={"items": reversed_items})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        fetched = fetch_youtube_playlist(f"https://www.youtube.com/watch?v={VIDEO}&list={LIST}", client=client)
    assert calls["items"] == 2
    assert calls["videos"] == [[vid(0), vid(1), vid(2)]]
    assert [track.id for track in fetched.tracks] == [vid(0), vid(1), vid(2)]
    assert fetched.tracks[0].title == f"曲{vid(0)}"
    assert fetched.tracks[0].channel == "人"
    assert fetched.tracks[0].duration == 242
    assert fetched.unavailable == 0
    assert fetched.repeated == 0


def test_private_and_deleted_videos_are_skipped_and_not_looked_up(monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", KEY)
    seen: list[str] = []
    hidden = "abcde123451"
    titled = "abcde123452"
    japanese_private = "abcde123453"
    japanese_deleted = "abcde123454"
    bracket_private = "abcde123455"
    bracket_deleted = "abcde123456"
    public = "abcde123457"
    spaced = "abcde123458"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/playlistItems"):
            private_title = playlist_item(hidden, title="普通の題", private=True)
            private_title["snippet"]["title"] = "普通の題"
            public_item = playlist_item(public, title="公開")
            public_item["status"] = {"privacyStatus": "public"}
            return httpx.Response(
                200,
                json={
                    "items": [
                        playlist_item(VIDEO, title="残る"),
                        private_title,
                        playlist_item(OTHER, private=True),
                        playlist_item(THIRD, deleted=True),
                        {"snippet": {"title": "Private video", "resourceId": {"videoId": titled}}},
                        {"snippet": {"title": "非公開の動画", "resourceId": {"videoId": japanese_private}}},
                        {"snippet": {"title": "削除された動画", "resourceId": {"videoId": japanese_deleted}}},
                        {"snippet": {"title": "[Private video]", "resourceId": {"videoId": bracket_private}}},
                        {"snippet": {"title": "[Deleted video]", "resourceId": {"videoId": bracket_deleted}}},
                        {"snippet": {"title": " Private video ", "resourceId": {"videoId": spaced}}},
                        public_item,
                        {"snippet": {"title": "空"}},
                    ]
                },
            )
        requested = request.url.params["id"].split(",")
        seen.extend(requested)
        return httpx.Response(
            200,
            json={"items": [video_item(video_id, title="残る" if video_id == VIDEO else "公開") for video_id in requested]},
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        fetched = fetch_youtube_playlist(LIST, client=client)
    assert seen == [VIDEO, public]
    assert hidden not in seen
    assert spaced not in seen
    assert [track.id for track in fetched.tracks] == [VIDEO, public]
    assert fetched.unavailable == 10
    assert fetched.repeated == 0
    assert _unavailable_item({"status": {"privacyStatus": "private"}, "snippet": {"title": "普通の題"}}) is True
    assert _unavailable_item({"status": {"privacyStatus": "public"}, "snippet": {"title": "公開"}}) is False
    assert _unavailable_item({"snippet": {"title": " Private video "}}) is True
    assert _unavailable_item({"snippet": {"title": "夜"}}) is False


def test_a_video_missing_from_videos_list_counts_as_unavailable_including_repeats(monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", KEY)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/playlistItems"):
            return httpx.Response(
                200,
                json={"items": [playlist_item(VIDEO), playlist_item(OTHER), playlist_item(OTHER)]},
            )
        assert request.url.params["id"] == f"{VIDEO},{OTHER}"
        return httpx.Response(200, json={"items": [video_item(VIDEO)]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        fetched = fetch_youtube_playlist(LIST, client=client)
    assert [track.id for track in fetched.tracks] == [VIDEO]
    assert fetched.unavailable == 2
    assert fetched.repeated == 0


def test_a_repeated_public_video_is_counted_once(monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", KEY)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/playlistItems"):
            return httpx.Response(
                200, json={"items": [playlist_item(VIDEO), playlist_item(OTHER), playlist_item(VIDEO)]}
            )
        return httpx.Response(
            200, json={"items": [video_item(video_id) for video_id in request.url.params["id"].split(",")]}
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        fetched = fetch_youtube_playlist(LIST, client=client)
    assert [track.id for track in fetched.tracks] == [VIDEO, OTHER]
    assert fetched.repeated == 1
    assert fetched.unavailable == 0


def test_item_cap_stops_at_500_and_does_not_follow_another_page(monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", KEY)
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "www.googleapis.com"
        if request.url.path.endswith("/playlistItems"):
            page = int(request.url.params.get("pageToken") or "0")
            calls.append(int(request.url.params["maxResults"]))
            start = page * 50
            items = [playlist_item(vid(start + offset)) for offset in range(50)]
            return httpx.Response(200, json={"items": items, "nextPageToken": str(page + 1)})
        requested = request.url.params["id"].split(",")
        return httpx.Response(200, json={"items": [video_item(video_id) for video_id in requested]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        fetched = fetch_youtube_playlist(LIST, client=client, limit=10_000)
    assert calls == [50] * 10
    assert len(fetched.tracks) == PLAYLIST_ITEM_LIMIT
    assert fetched.tracks[0].id == vid(0)
    assert fetched.tracks[-1].id == vid(499)


def test_a_short_page_does_not_keep_items_past_the_requested_limit(monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", KEY)
    calls = {"n": 0, "max": None}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/videos"):
            requested = request.url.params["id"].split(",")
            return httpx.Response(200, json={"items": [video_item(video_id) for video_id in requested]})
        calls["n"] += 1
        calls["max"] = int(request.url.params["maxResults"])
        items = [playlist_item(vid(offset)) for offset in range(5)]
        return httpx.Response(200, json={"items": items})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        fetched = fetch_youtube_playlist(LIST, client=client, limit=2)
    assert calls["n"] == 1
    assert calls["max"] == 50
    assert [track.id for track in fetched.tracks] == [vid(0), vid(1)]
    assert fetched.overflow == 3


def test_deadline_stops_before_the_next_page(monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", KEY)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            200,
            json={"items": [playlist_item(VIDEO, private=True)], "nextPageToken": "next"},
        )

    times = iter([0, 1, 1000])

    def now():
        return next(times)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(PlaylistLookupError) as caught:
            fetch_youtube_playlist(LIST, client=client, now=now)
    assert caught.value.status == 502
    assert str(caught.value) == "プレイリストを取れませんでした"
    assert calls["n"] == 1


def test_api_failure_and_timeout_are_errors_and_do_not_echo_the_key(monkeypatch, caplog):
    monkeypatch.setenv("YOUTUBE_API_KEY", KEY)
    caplog.set_level(logging.WARNING)

    def broken(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"code": 500, "message": KEY}})

    with httpx.Client(transport=httpx.MockTransport(broken)) as client:
        with pytest.raises(PlaylistLookupError) as caught:
            fetch_youtube_playlist(LIST, client=client)
    assert caught.value.status == 502
    assert KEY not in str(caught.value)
    assert KEY not in caplog.text

    def missing(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            json={"error": {"code": 404, "message": KEY, "errors": [{"reason": "playlistNotFound"}]}},
        )

    with httpx.Client(transport=httpx.MockTransport(missing)) as client:
        with pytest.raises(PlaylistLookupError) as caught:
            fetch_youtube_playlist(LIST, client=client)
    assert caught.value.status == 404
    assert str(caught.value) == "そのプレイリストは見つかりませんでした"
    assert KEY not in str(caught.value)

    def timeout(_request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out")

    with httpx.Client(transport=httpx.MockTransport(timeout)) as client:
        with pytest.raises(PlaylistLookupError) as caught:
            fetch_youtube_playlist(LIST, client=client)
    assert caught.value.status == 502
    assert KEY not in caplog.text


def test_invalid_input_does_not_call_the_api():
    with pytest.raises(PlaylistLookupError) as caught:
        fetch_youtube_playlist(f"https://evil.example/playlist?list={LIST}")
    assert caught.value.status == 400
    assert str(caught.value) == "プレイリストのURLを入れてください"


class _Proc:
    def __init__(self, returncode=0):
        self.returncode = returncode
        self.killed = False
        self.reaped = threading.Event()
        self._dead = threading.Event()
        self.pid = 424242

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.reaped.is_set():
            return self.returncode
        if self.killed:
            time.sleep(0.3)
            self.reaped.set()
            return self.returncode
        if self.returncode is not None:
            self.reaped.set()
            return self.returncode
        if not self._dead.wait(3 if timeout is None else timeout):
            if self.returncode is not None:
                return self.returncode
            raise subprocess.TimeoutExpired("yt-dlp", timeout or 0)
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9
        self._dead.set()


def _install_ytdlp(monkeypatch, entries, *, playlist_count=None, proc=None, err_bytes=b"", ready=None):
    seen = {"kills": []}

    def popen(cmd, stdout=None, stderr=None, stdin=None, start_new_session=False, **kwargs):
        seen["cmd"] = list(cmd)
        seen["start_new_session"] = start_new_session
        end = int(cmd[cmd.index("--playlist-end") + 1])
        chosen = list(entries)[:end]
        seen["end"] = end
        seen["returned"] = len(chosen)
        payload = {
            "entries": chosen,
            "playlist_count": len(entries) if playlist_count is None else playlist_count,
        }
        if stdout is not None:
            stdout.write(json.dumps(payload).encode())
            stdout.seek(0)
        if stderr is not None and err_bytes:
            stderr.write(err_bytes)
            stderr.seek(0)
        seen["proc"] = proc if proc is not None else _Proc(0)
        if ready is not None:
            ready.set()
        return seen["proc"]

    def killpg(pid, sig):
        seen["kills"].append((pid, sig))
        current = seen.get("proc")
        if current is not None and pid == getattr(current, "pid", None):
            current.kill()

    monkeypatch.setattr("djtube.youtube_playlist.subprocess.Popen", popen)
    monkeypatch.setattr("djtube.youtube_playlist.os.killpg", killpg)
    return seen


def test_without_a_key_flat_playlist_skips_private_and_repeats(monkeypatch):
    entries = [
        {
            "id": VIDEO,
            "title": "夜",
            "channel": "人",
            "duration": 90,
            "thumbnail": f"https://i.ytimg.com/vi/{VIDEO}/mqdefault.jpg",
        },
        {"id": OTHER, "title": "[Private video]", "availability": "private"},
        {"id": "eeeeeeeeeee", "title": "普通の題", "availability": "private", "duration": 10, "channel": "人"},
        {"id": VIDEO, "title": "夜", "duration": 90},
        {"id": THIRD, "title": "[Deleted video]"},
        None,
    ]
    seen = _install_ytdlp(monkeypatch, entries)
    fetched = fetch_youtube_playlist(f"https://music.youtube.com/playlist?list={LIST}")
    command = seen["cmd"]
    assert command[0] == sys.executable
    assert command[1:4] == ["-m", "yt_dlp", "--ignore-config"]
    assert "-J" in command
    assert "--flat-playlist" in command
    assert seen["start_new_session"] is True
    assert seen["end"] == PLAYLIST_ITEM_LIMIT
    assert seen["returned"] == len(entries)
    assert f"https://www.youtube.com/playlist?list={LIST}" in command
    assert "music.youtube.com" not in " ".join(command)
    assert [track.id for track in fetched.tracks] == [VIDEO]
    assert fetched.tracks[0].channel == "人"
    assert fetched.tracks[0].duration == 90
    assert "eeeeeeeeeee" not in [track.id for track in fetched.tracks]
    assert fetched.repeated == 1
    assert fetched.unavailable == 4


def test_ytdlp_failure_is_an_error_without_the_message(monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    _install_ytdlp(monkeypatch, [], proc=_Proc(1), err_bytes=f"boom {KEY}".encode())
    with pytest.raises(PlaylistLookupError) as caught:
        fetch_youtube_playlist(LIST)
    assert caught.value.status == 502
    assert KEY not in str(caught.value)
    assert KEY not in caplog.text


def _track(video_id: str, title: str) -> dict:
    return {"id": video_id, "title": title, "channel": "人", "duration": 90, "thumbnail": None}


def test_import_appends_to_an_existing_playlist_and_skips_duplicates(tmp_path, monkeypatch):
    client, store = _app(tmp_path)
    kept = client.post("/api/playlists", json={"name": "残す"}).json()
    client.post(f"/api/playlists/{kept['id']}/tracks", json=_track(THIRD, "別"))
    destination = client.post("/api/playlists", json={"name": "夜"}).json()
    client.post(f"/api/playlists/{destination['id']}/tracks", json=_track("ddddddddddd", "先"))
    client.post(f"/api/playlists/{destination['id']}/tracks", json=_track(VIDEO, "既"))
    before = store.path.read_text(encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/playlistItems"):
            return httpx.Response(
                200,
                json={
                    "items": [
                        playlist_item(VIDEO, title="一覧"),
                        playlist_item(OTHER, title="一覧"),
                        playlist_item(VIDEO, title="一覧"),
                        playlist_item("eeeeeeeeeee", private=True),
                    ]
                },
            )
        requested = request.url.params["id"].split(",")
        assert "eeeeeeeeeee" not in requested
        return httpx.Response(
            200,
            json={"items": [video_item(video_id, title="本当の題", channel="店") for video_id in requested]},
        )

    _install(monkeypatch, handler)
    response = client.post(
        "/api/playlists/import",
        json={
            "url": f"https://www.youtube.com/watch?v={VIDEO}&list={LIST}",
            "playlist_id": destination["id"],
            "limit": 1,
        },
    )
    assert response.status_code == 200
    assert KEY not in response.text
    body = response.json()
    assert body["added"] == 1
    assert body["duplicates"] == 2
    assert body["unavailable"] == 1
    assert body["playlist"]["id"] == destination["id"]
    assert [item["id"] for item in body["playlist"]["tracks"]] == ["ddddddddddd", VIDEO, OTHER]
    added = body["playlist"]["tracks"][2]
    assert added["title"] == "本当の題"
    assert added["channel"] == "店"
    assert added["duration"] == 242
    listed = client.get("/api/playlists").json()["playlists"]
    assert listed[0]["id"] == kept["id"]
    assert [item["id"] for item in listed[0]["tracks"]] == [THIRD]
    assert json.loads(store.path.read_text(encoding="utf-8"))["playlists"] == listed
    assert before != store.path.read_text(encoding="utf-8")


def test_import_can_create_a_new_playlist_without_touching_the_others(tmp_path, monkeypatch):
    client, store = _app(tmp_path)
    kept = client.post("/api/playlists", json={"name": "残す"}).json()
    client.post(f"/api/playlists/{kept['id']}/tracks", json=_track(THIRD, "別"))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/playlistItems"):
            assert request.url.params["playlistId"] == LIST
            return httpx.Response(200, json={"items": [playlist_item(OTHER), playlist_item(VIDEO)]})
        return httpx.Response(
            200,
            json={"items": [video_item(video_id) for video_id in request.url.params["id"].split(",")]},
        )

    _install(monkeypatch, handler)
    response = client.post("/api/playlists/import", json={"url": LIST, "name": "  夜の\nセット  "})
    assert response.status_code == 200
    body = response.json()
    assert body["added"] == 2
    assert body["duplicates"] == 0
    assert body["unavailable"] == 0
    assert body["playlist"]["name"] == "夜の セット"
    assert [item["id"] for item in body["playlist"]["tracks"]] == [OTHER, VIDEO]
    listed = client.get("/api/playlists").json()["playlists"]
    assert [item["name"] for item in listed] == ["残す", "夜の セット"]
    assert [item["id"] for item in listed[0]["tracks"]] == [THIRD]
    reloaded = PlaylistStore(store.path).list_playlists()
    assert reloaded == listed


def test_invalid_import_input_is_400_and_does_not_write(tmp_path, monkeypatch):
    client, store = _app(tmp_path)
    created = client.post("/api/playlists", json={"name": "夜"}).json()
    client.post(f"/api/playlists/{created['id']}/tracks", json=_track(VIDEO, "既"))
    before = store.path.read_text(encoding="utf-8")
    cases = [
        ({}, "プレイリストのURLを入れてください"),
        ({"url": f"https://www.youtube.com/watch?v={VIDEO}", "name": "朝"}, "プレイリストのURLを入れてください"),
        ({"url": f"https://evil.example/playlist?list={LIST}", "name": "朝"}, "プレイリストのURLを入れてください"),
        ({"url": VIDEO, "name": "朝"}, "プレイリストのURLを入れてください"),
        ({"url": LIST}, "追加先を選んでください"),
        ({"url": LIST, "name": "朝", "playlist_id": created["id"]}, "追加先を一つ選んでください"),
        ({"url": LIST, "name": "   "}, "名前を入れてください"),
        ({"url": LIST, "playlist_id": 12}, "追加先を選んでください"),
    ]
    for payload, detail in cases:
        response = client.post("/api/playlists/import", json=payload)
        assert response.status_code == 400
        assert response.json()["detail"] == detail
    empty = client.post("/api/playlists/import", content=b"", headers={"content-type": "application/json"})
    assert empty.status_code == 400
    array = client.post("/api/playlists/import", json=["nope"])
    assert array.status_code == 400
    assert store.path.read_text(encoding="utf-8") == before
    assert client.get("/api/playlists").json()["playlists"][0]["tracks"][0]["id"] == VIDEO


def test_missing_destination_playlist_is_404(tmp_path):
    client, store = _app(tmp_path)
    created = client.post("/api/playlists", json={"name": "夜"}).json()
    before = store.path.read_text(encoding="utf-8")
    missing = client.post("/api/playlists/import", json={"url": LIST, "playlist_id": "b" * 32})
    assert missing.status_code == 404
    assert missing.json()["detail"] == "プレイリストが見つかりません"
    malformed = client.post("/api/playlists/import", json={"url": LIST, "playlist_id": "not-an-id"})
    assert malformed.status_code == 404
    assert store.path.read_text(encoding="utf-8") == before
    assert client.get("/api/playlists").json()["playlists"][0]["id"] == created["id"]


def test_track_cap_and_playlist_cap_write_nothing(tmp_path, monkeypatch):
    client, store = _app(tmp_path, playlist_limit=1, track_limit=1)
    created = client.post("/api/playlists", json={"name": "夜"}).json()
    client.post(f"/api/playlists/{created['id']}/tracks", json=_track(VIDEO, "既"))
    before = store.path.read_text(encoding="utf-8")
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if request.url.path.endswith("/playlistItems"):
            return httpx.Response(200, json={"items": [playlist_item(OTHER), playlist_item(THIRD)]})
        return httpx.Response(
            200,
            json={"items": [video_item(video_id) for video_id in request.url.params["id"].split(",")]},
        )

    _install(monkeypatch, handler)
    full = client.post("/api/playlists/import", json={"url": LIST, "playlist_id": created["id"]})
    assert full.status_code == 400
    assert full.json()["detail"] == "曲数が多すぎます"
    another = client.post("/api/playlists/import", json={"url": LIST, "name": "朝"})
    assert another.status_code == 400
    assert another.json()["detail"] == "プレイリストが多すぎます"
    assert calls["n"] == 0
    assert store.path.read_text(encoding="utf-8") == before
    assert [item["id"] for item in store.list_playlists()] == [created["id"]]


def test_api_failure_on_the_endpoint_leaves_playlists_unchanged(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    client, store = _app(tmp_path)
    created = client.post("/api/playlists", json={"name": "夜"}).json()
    client.post(f"/api/playlists/{created['id']}/tracks", json=_track(VIDEO, "既"))
    before = store.path.read_text(encoding="utf-8")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"code": 500, "message": KEY}})

    _install(monkeypatch, handler)
    response = client.post("/api/playlists/import", json={"url": LIST, "playlist_id": created["id"]})
    assert response.status_code == 502
    assert response.json()["detail"] == "プレイリストを取れませんでした"
    assert KEY not in response.text
    assert KEY not in caplog.text
    assert store.path.read_text(encoding="utf-8") == before


def test_endpoint_without_a_key_uses_the_flat_playlist(tmp_path, monkeypatch):
    seen = _install_ytdlp(
        monkeypatch,
        [{"id": OTHER, "title": "昼", "channel": "店", "duration": 12}],
    )
    client, _store = _app(tmp_path)
    response = client.post(
        "/api/playlists/import",
        json={"url": f"https://www.youtube.com/watch?v={VIDEO}&list={LIST}", "name": "朝"},
    )
    assert response.status_code == 200
    assert "--flat-playlist" in seen["cmd"]
    assert f"https://www.youtube.com/playlist?list={LIST}" in seen["cmd"]
    body = response.json()
    assert body["added"] == 1
    assert body["playlist"]["tracks"][0]["id"] == OTHER
    assert body["playlist"]["tracks"][0]["title"] == "昼"


def test_direct_import_of_track_objects_appends_in_order(tmp_path):
    store = PlaylistStore(tmp_path / "playlists.json")
    first = store.create("残す")
    store.add_track(first["id"], _track(THIRD, "別"))
    destination = store.create("夜")
    store.add_track(destination["id"], _track(VIDEO, "既"))
    imported = store.import_tracks(
        [Track(VIDEO, "重", "人", 10, None), Track(OTHER, "新", "店", 12, None)],
        playlist_id=destination["id"],
        repeated=1,
        unavailable=2,
    )
    assert imported["added"] == 1
    assert imported["duplicates"] == 2
    assert imported["unavailable"] == 2
    assert [item["id"] for item in imported["playlist"]["tracks"]] == [VIDEO, OTHER]
    assert imported["overflow"] == 0
    assert [item["id"] for item in store.list_playlists()[0]["tracks"]] == [THIRD]


def test_unimportable_lists_are_rejected_before_the_api(tmp_path, monkeypatch):
    music = "RDCLAK5uy_" + "m" * 33
    assert rejected_playlist_message(music) is None
    assert rejected_playlist_message(f"https://music.youtube.com/playlist?list={music}") is None
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(str(request.url))

    _install(monkeypatch, handler)
    samples = [
        "WL",
        "LL",
        "LM",
        "RDMM",
        "RD" + VIDEO,
        "https://www.youtube.com/playlist?list=WL",
        f"https://www.youtube.com/watch?v={VIDEO}&list=LL",
        f"https://www.youtube.com/playlist?list=RD{VIDEO}",
    ]
    for sample in samples:
        with pytest.raises(PlaylistLookupError) as caught:
            fetch_youtube_playlist(sample)
        assert caught.value.status == 400
        assert str(caught.value) == "取り込めない種類のリストです"
    client, store = _app(tmp_path)
    before = store.path.read_text(encoding="utf-8") if store.path.exists() else ""
    response = client.post("/api/playlists/import", json={"url": "LL", "name": "朝"})
    assert response.status_code == 400
    assert response.json()["detail"] == "取り込めない種類のリストです"
    assert not store.path.exists() or store.path.read_text(encoding="utf-8") == before


def test_timeout_hides_the_key_even_when_the_request_url_is_on_the_exception(monkeypatch, caplog):
    monkeypatch.setenv("YOUTUBE_API_KEY", KEY)
    caplog.set_level(logging.DEBUG)

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException(f"timed out {request.url}", request=request)

    with httpx.Client(transport=httpx.MockTransport(timeout)) as client:
        with pytest.raises(PlaylistLookupError) as caught:
            fetch_youtube_playlist(LIST, client=client)
    assert caught.value.status == 502
    assert str(caught.value) == "プレイリストを取れませんでした"
    assert caught.value.__cause__ is None
    assert KEY not in str(caught.value)
    assert KEY not in caplog.text


def test_videos_list_batches_at_most_50_ids(monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", KEY)
    batches: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["key"] == KEY
        assert "key" not in {name.lower() for name in request.headers}
        if request.url.path.endswith("/playlistItems"):
            page = int(request.url.params.get("pageToken") or "0")
            start = page * 50
            items = [playlist_item(vid(start + offset)) for offset in range(50) if start + offset < 51]
            payload = {"items": items, "pageInfo": {"totalResults": 51}}
            if start == 0:
                payload["nextPageToken"] = "1"
            return httpx.Response(200, json=payload)
        requested = request.url.params["id"].split(",")
        batches.append(requested)
        return httpx.Response(200, json={"items": [video_item(video_id) for video_id in requested]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        fetched = fetch_youtube_playlist(LIST, client=client)
    assert [len(batch) for batch in batches] == [50, 1]
    assert len(fetched.tracks) == 51


def test_api_key_is_a_query_param(monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", KEY)
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("x-goog-api-key") is None
        if request.url.path.endswith("/playlistItems"):
            return httpx.Response(200, json={"items": [playlist_item(VIDEO)], "pageInfo": {"totalResults": 1}})
        return httpx.Response(200, json={"items": [video_item(VIDEO)]})

    class Watching(httpx.Client):
        def __init__(self) -> None:
            super().__init__(transport=httpx.MockTransport(handler))

        def get(self, url, params=None, timeout=None, **kwargs):
            seen.append({"url": str(url), "params": dict(params or {})})
            assert isinstance(params, dict)
            assert params.get("key") == KEY
            assert KEY not in str(url)
            assert "key=" not in str(url)
            assert "x-goog-api-key" not in {name.lower() for name in kwargs.get("headers") or {}}
            return super().get(url, params=params, timeout=timeout, **kwargs)

    http = Watching()
    try:
        fetch_youtube_playlist(LIST, client=http)
    finally:
        http.close()
    assert seen
    assert all(item["params"]["key"] == KEY and KEY not in item["url"] for item in seen)


def test_request_timeout_shrinks_to_the_remaining_deadline(monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", KEY)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("pageToken"):
            return httpx.Response(200, json={"items": []})
        return httpx.Response(
            200,
            json={"items": [playlist_item(VIDEO, private=True)], "nextPageToken": "next"},
        )

    class Watching(httpx.Client):
        def __init__(self) -> None:
            super().__init__(transport=httpx.MockTransport(handler))
            self.timeouts: list[float] = []

        def get(self, url, params=None, timeout=None, **kwargs):
            self.timeouts.append(float(timeout))
            return super().get(url, params=params, timeout=timeout, **kwargs)

    stamps = [0.0, 0.0, 24.2]

    def now():
        return stamps.pop(0)

    http = Watching()
    try:
        fetch_youtube_playlist(LIST, client=http, now=now)
    finally:
        http.close()
    assert http.timeouts[0] == 8
    assert http.timeouts[1] == pytest.approx(0.8)
    assert http.timeouts[1] < 2


def test_ytdlp_item_cap_stops_the_list(monkeypatch):
    entries = [{"id": vid(index), "title": f"曲{index}", "duration": 10, "channel": "人"} for index in range(5)]
    seen = _install_ytdlp(monkeypatch, entries, playlist_count=5)
    fetched = fetch_youtube_playlist(LIST, limit=2)
    assert seen["end"] == PLAYLIST_ITEM_LIMIT
    assert seen["returned"] == 5
    assert [track.id for track in fetched.tracks] == [vid(0), vid(1)]
    assert fetched.overflow == 3


def test_ytdlp_overflow_uses_playlist_count_when_playlist_end_truncates(monkeypatch):
    monkeypatch.setattr("djtube.youtube_playlist.PLAYLIST_ITEM_LIMIT", 2)
    entries = [{"id": vid(index), "title": f"曲{index}", "duration": 10, "channel": "人"} for index in range(5)]
    seen = _install_ytdlp(monkeypatch, entries, playlist_count=5)
    fetched = fetch_youtube_playlist(LIST, limit=2)
    assert seen["end"] == 2
    assert seen["returned"] == 2
    assert [track.id for track in fetched.tracks] == [vid(0), vid(1)]
    assert fetched.overflow == 3


def test_ytdlp_obeys_the_overall_deadline(monkeypatch):
    monkeypatch.setattr("djtube.youtube_playlist.DEADLINE_SECONDS", 0.2)
    hanging = _Proc(None)
    _install_ytdlp(monkeypatch, [], proc=hanging)
    started = time.monotonic()
    with pytest.raises(PlaylistLookupError) as caught:
        fetch_youtube_playlist(LIST)
    elapsed = time.monotonic() - started
    assert caught.value.status == 502
    assert KEY not in str(caught.value)
    assert hanging.killed
    assert hanging.reaped.is_set()
    assert elapsed < 1.5
    assert elapsed >= 0.25


def test_quota_exceeded_has_its_own_message(tmp_path, monkeypatch):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={"error": {"code": 403, "message": KEY, "errors": [{"reason": "quotaExceeded"}]}},
        )

    _install(monkeypatch, handler)
    client, store = _app(tmp_path)
    created = client.post("/api/playlists", json={"name": "夜"}).json()
    before = store.path.read_text(encoding="utf-8")
    response = client.post("/api/playlists/import", json={"url": LIST, "playlist_id": created["id"]})
    assert response.status_code == 429
    assert response.json()["detail"] == "YouTube API の上限に達しました"
    assert KEY not in response.text
    assert store.path.read_text(encoding="utf-8") == before


def test_name_and_playlist_count_are_validated_before_the_api(tmp_path, monkeypatch):
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500, json={"error": {"message": KEY}})

    _install(monkeypatch, handler)
    client, _store = _app(tmp_path, playlist_limit=1)
    client.post("/api/playlists", json={"name": "夜"})
    blank = client.post("/api/playlists/import", json={"url": LIST, "name": "\x00"})
    assert blank.status_code == 400
    assert blank.json()["detail"] == "名前を入れてください"
    crowded = client.post("/api/playlists/import", json={"url": LIST, "name": "朝"})
    assert crowded.status_code == 400
    assert crowded.json()["detail"] == "プレイリストが多すぎます"
    assert calls["n"] == 0


def test_import_keeps_what_fits_and_reports_the_rest(tmp_path, monkeypatch):
    """Duplicates are dropped before the room is applied.

    Destination holds vid(0) through vid(2). The YouTube list is those three
    followed by five new videos. Room is 2, so two new tracks are added and
    the other three new tracks are the overflow.
    """

    client, _store = _app(tmp_path, track_limit=5)
    created = client.post("/api/playlists", json={"name": "夜"}).json()
    for number in range(3):
        client.post(f"/api/playlists/{created['id']}/tracks", json=_track(vid(number), "既"))
    seen = {"ids": []}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/playlistItems"):
            seen["max"] = int(request.url.params["maxResults"])
            items = [playlist_item(vid(number)) for number in range(8)]
            return httpx.Response(200, json={"items": items, "pageInfo": {"totalResults": 8}})
        requested = request.url.params["id"].split(",")
        seen["ids"].extend(requested)
        return httpx.Response(200, json={"items": [video_item(video_id) for video_id in requested]})

    _install(monkeypatch, handler)
    response = client.post("/api/playlists/import", json={"url": LIST, "playlist_id": created["id"]})
    assert response.status_code == 200
    body = response.json()
    assert seen["max"] == 50
    assert seen["ids"] == [vid(3), vid(4), vid(5), vid(6), vid(7)]
    assert body["added"] == 2
    assert body["duplicates"] == 3
    assert body["overflow"] == 3
    assert [item["id"] for item in body["playlist"]["tracks"]] == [vid(0), vid(1), vid(2), vid(3), vid(4)]
    fresh = PlaylistStore(tmp_path / "fresh.json", track_limit=2)
    destination = fresh.create("夜")
    fresh.add_track(destination["id"], _track(VIDEO, "既"))
    trimmed = fresh.import_tracks(
        [Track(OTHER, "新", "人", 10, None), Track("ddddddddddd", "次", "人", 10, None)],
        playlist_id=destination["id"],
    )
    assert trimmed["added"] == 1
    assert trimmed["overflow"] == 1
    assert [item["id"] for item in trimmed["playlist"]["tracks"]] == [VIDEO, OTHER]


def test_track_cap_error_does_not_leave_an_empty_playlist(tmp_path):
    from djtube.playlists import PlaylistError

    store = PlaylistStore(tmp_path / "playlists.json", track_limit=0)
    with pytest.raises(PlaylistError) as caught:
        store.import_tracks([Track(VIDEO, "夜", "人", 10, None)], name="朝")
    assert str(caught.value) == "曲数が多すぎます"
    assert store.list_playlists() == []
    assert store._playlists == []


def test_import_skips_save_when_nothing_new_is_added(tmp_path, monkeypatch):
    store = PlaylistStore(tmp_path / "playlists.json")
    dest = store.create("夜")
    store.add_track(dest["id"], _track(VIDEO, "既"))
    before = store.path.read_text(encoding="utf-8")

    def boom(self):
        raise AssertionError("saved")

    monkeypatch.setattr(PlaylistStore, "_save", boom)
    imported = store.import_tracks([Track(VIDEO, "重", "人", 10, None)], playlist_id=dest["id"])
    assert imported["added"] == 0
    assert imported["duplicates"] == 1
    assert store.path.read_text(encoding="utf-8") == before


def test_import_holds_the_playlist_lock(tmp_path, monkeypatch):
    store = PlaylistStore(tmp_path / "playlists.json")
    dest = store.create("夜")
    observed = {}
    original = PlaylistStore._save

    def spy(self):
        observed["locked"] = self._lock.locked()
        acquired = {}

        def other():
            acquired["got"] = self._lock.acquire(blocking=False)
            if acquired["got"]:
                self._lock.release()

        thread = threading.Thread(target=other)
        thread.start()
        thread.join()
        observed["other"] = acquired["got"]
        original(self)

    monkeypatch.setattr(PlaylistStore, "_save", spy)
    store.import_tracks([Track(VIDEO, "夜", "人", 10, None)], playlist_id=dest["id"])
    assert observed["locked"] is True
    assert observed["other"] is False


def test_a_second_import_is_rejected_while_the_first_holds_the_server(tmp_path, monkeypatch):
    started = threading.Event()
    release = threading.Event()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/playlistItems"):
            started.set()
            assert release.wait(5)
            return httpx.Response(
                200,
                json={"items": [playlist_item(OTHER)], "pageInfo": {"totalResults": 1}},
            )
        return httpx.Response(200, json={"items": [video_item(OTHER)]})

    _install(monkeypatch, handler)
    app = create_app(PlaylistStore(tmp_path / "playlists.json"))

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            task = asyncio.create_task(http.post("/api/playlists/import", json={"url": LIST, "name": "朝"}))
            assert await asyncio.to_thread(started.wait, 2)
            health_at = time.monotonic()
            health = await asyncio.wait_for(http.get("/api/health"), 1)
            assert health.status_code == 200
            assert time.monotonic() - health_at < 0.8
            second = await asyncio.wait_for(http.post("/api/playlists/import", json={"url": LIST, "name": "次"}), 1)
            assert second.status_code == 429
            assert second.json()["detail"] == "取り込み中です"
            release.set()
            first = await asyncio.wait_for(task, 2)
            assert first.status_code == 200

    anyio.run(scenario)


def _fast_import_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/playlistItems"):
        return httpx.Response(
            200,
            json={"items": [playlist_item(OTHER)], "pageInfo": {"totalResults": 1}},
        )
    return httpx.Response(200, json={"items": [video_item(OTHER)]})


def test_imports_are_limited_to_six_per_minute(tmp_path, monkeypatch):
    _install(monkeypatch, _fast_import_handler)
    client, _store = _app(tmp_path)
    created = client.post("/api/playlists", json={"name": "夜"}).json()
    clock = {"t": 1_000.0}
    client.app.state.import_slot.now = lambda: clock["t"]
    for _index in range(6):
        response = client.post("/api/playlists/import", json={"url": LIST, "playlist_id": created["id"]})
        assert response.status_code == 200, response.text
    blocked = client.post("/api/playlists/import", json={"url": LIST, "playlist_id": created["id"]})
    assert blocked.status_code == 429
    assert blocked.json()["detail"] == "取り込みの回数が多いです"
    clock["t"] = 1_000.0 + 59.999
    still = client.post("/api/playlists/import", json={"url": LIST, "playlist_id": created["id"]})
    assert still.status_code == 429
    assert still.json()["detail"] == "取り込みの回数が多いです"
    clock["t"] = 1_000.0 + 60.0
    again = client.post("/api/playlists/import", json={"url": LIST, "playlist_id": created["id"]})
    assert again.status_code == 200


def test_httpx_info_logs_do_not_include_the_api_key(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/playlistItems"):
            return httpx.Response(
                200,
                json={"items": [playlist_item(OTHER)], "pageInfo": {"totalResults": 1}},
            )
        return httpx.Response(200, json={"items": [video_item(OTHER)]})

    _install(monkeypatch, handler)
    client, _store = _app(tmp_path)
    response = client.post("/api/playlists/import", json={"url": LIST, "name": "朝"})
    assert response.status_code == 200
    assert KEY not in caplog.text
    assert logging.getLogger("httpx").level >= logging.WARNING
    assert logging.getLogger("httpcore").level >= logging.WARNING


def test_imports_are_limited_to_one_hundred_per_day(tmp_path, monkeypatch):
    _install(monkeypatch, _fast_import_handler)
    client, _store = _app(tmp_path)
    created = client.post("/api/playlists", json={"name": "夜"}).json()
    clock = {"t": 50_000.0}
    client.app.state.import_slot.now = lambda: clock["t"]
    for index in range(100):
        clock["t"] = 50_000.0 + index * 61
        response = client.post(
            "/api/playlists/import",
            json={"url": LIST, "playlist_id": created["id"]},
            headers={"X-Real-IP": f"203.0.113.{index + 1}"},
        )
        assert response.status_code == 200, response.text
    clock["t"] = 50_000.0 + 99 * 61
    blocked = client.post(
        "/api/playlists/import",
        json={"url": LIST, "playlist_id": created["id"]},
        headers={"X-Real-IP": "198.51.100.1"},
    )
    assert blocked.status_code == 429
    assert blocked.json()["detail"] == "24時間の取り込みの上限に達しました"
    clock["t"] = 50_000.0 + 86400.0 - 0.001
    early = client.post(
        "/api/playlists/import",
        json={"url": LIST, "playlist_id": created["id"]},
        headers={"X-Real-IP": "198.51.100.2"},
    )
    assert early.status_code == 429
    assert early.json()["detail"] == "24時間の取り込みの上限に達しました"
    clock["t"] = 50_000.0 + 86400.0
    again = client.post(
        "/api/playlists/import",
        json={"url": LIST, "playlist_id": created["id"]},
        headers={"X-Real-IP": "198.51.100.3"},
    )
    assert again.status_code == 200


def test_a_failed_import_releases_the_slot(tmp_path, monkeypatch):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(500, json={"error": {"code": 500, "message": "down"}})
        return _fast_import_handler(request)

    _install(monkeypatch, handler)
    client, _store = _app(tmp_path)
    failed = client.post("/api/playlists/import", json={"url": LIST, "name": "朝"})
    assert failed.status_code == 502
    again = client.post("/api/playlists/import", json={"url": LIST, "name": "朝"})
    assert again.status_code == 200, again.text


def test_cancel_between_pages_does_not_request_the_next_page(monkeypatch):
    from djtube.youtube_playlist import FetchControl

    control = FetchControl()
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/playlistItems"):
            calls["n"] += 1
            if calls["n"] == 1:
                control.cancel.set()
                return httpx.Response(
                    200,
                    json={"items": [playlist_item(OTHER)], "nextPageToken": "next"},
                )
            return httpx.Response(200, json={"items": [playlist_item(THIRD)]})
        return httpx.Response(200, json={"items": [video_item(OTHER)]})

    monkeypatch.setenv("YOUTUBE_API_KEY", KEY)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ImportCancelled):
            fetch_youtube_playlist(LIST, client=client, control=control)
    assert calls["n"] == 1


def test_a_disconnected_import_writes_nothing_and_frees_the_slot(tmp_path, monkeypatch):
    pages = {"n": 0}
    started = threading.Event()
    release = threading.Event()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/playlistItems"):
            pages["n"] += 1
            if pages["n"] == 1:
                started.set()
                assert release.wait(5)
                return httpx.Response(
                    200,
                    json={
                        "items": [playlist_item(OTHER)],
                        "nextPageToken": "next",
                        "pageInfo": {"totalResults": 2},
                    },
                )
            return httpx.Response(200, json={"items": [playlist_item(THIRD)]})
        return httpx.Response(200, json={"items": [video_item(request.url.params["id"].split(",")[0])]})

    _install(monkeypatch, handler)
    store = PlaylistStore(tmp_path / "playlists.json")
    app = create_app(store)
    body = json.dumps({"url": LIST, "name": "朝"}).encode()

    async def scenario():
        sent = False
        disconnect = asyncio.Event()

        async def receive():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            if disconnect.is_set():
                return {"type": "http.disconnect"}
            await disconnect.wait()
            return {"type": "http.disconnect"}

        async def send(_message):
            return None

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/playlists/import",
            "raw_path": b"/api/playlists/import",
            "query_string": b"",
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
            "client": ("127.0.0.1", 123),
            "server": ("test", 80),
            "root_path": "",
        }
        task = asyncio.create_task(app(scope, receive, send))
        assert await asyncio.to_thread(started.wait, 2)
        disconnect.set()
        await asyncio.sleep(0.3)
        release.set()
        await asyncio.wait_for(task, 2)

    anyio.run(scenario)
    assert pages["n"] == 1
    assert store.list_playlists() == []
    client = TestClient(app)
    again = client.post("/api/playlists/import", json={"url": LIST, "name": "朝"})
    assert again.status_code == 200, again.text
    assert len(store.list_playlists()) == 1


def _import_scope(body: bytes) -> dict:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/playlists/import",
        "raw_path": b"/api/playlists/import",
        "query_string": b"",
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ],
        "client": ("127.0.0.1", 123),
        "server": ("test", 80),
        "root_path": "",
    }


def test_overflow_is_counted_after_duplicates_are_removed(tmp_path, monkeypatch):
    """Importing PLsomemix into a 295-track list adds 5, with K 5.

    113 duplicates come first, then 3 private videos, then 10 new ones.
    Room is 5, so five new tracks are added and five do not fit.
    """

    mix = "PL" + "somemix" + "a" * 25
    store = PlaylistStore(tmp_path / "playlists.json")
    created = store.create("元")
    for number in range(295):
        store.add_track(created["id"], _track(vid(number), "既"))
    items = [playlist_item(vid(number)) for number in range(113)]
    items.extend(playlist_item(vid(2000 + number), title="普通の題", private=True) for number in range(3))
    items.extend(playlist_item(vid(1000 + number)) for number in range(10))
    looked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/playlistItems"):
            page = int(request.url.params.get("pageToken") or "0")
            start = page * 50
            chunk = items[start : start + 50]
            body = {"items": chunk, "pageInfo": {"totalResults": len(items)}}
            if start + 50 < len(items):
                body["nextPageToken"] = str(page + 1)
            return httpx.Response(200, json=body)
        requested = request.url.params["id"].split(",")
        looked.extend(requested)
        return httpx.Response(200, json={"items": [video_item(video_id) for video_id in requested]})

    _install(monkeypatch, handler)
    client = TestClient(create_app(store))
    response = client.post("/api/playlists/import", json={"url": mix, "playlist_id": created["id"]})
    assert response.status_code == 200, response.text
    body = response.json()
    assert looked == [vid(1000 + number) for number in range(10)]
    assert body["added"] == 5
    assert body["duplicates"] == 113
    assert body["unavailable"] == 3
    assert body["overflow"] == 5
    assert len(body["playlist"]["tracks"]) == 300
    assert [item["id"] for item in body["playlist"]["tracks"][-5:]] == [vid(1000 + number) for number in range(5)]


def test_the_next_import_stays_busy_until_the_cancelled_worker_returns(tmp_path, monkeypatch):
    pages = {"n": 0}
    started = threading.Event()
    release = threading.Event()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/playlistItems"):
            pages["n"] += 1
            if pages["n"] == 1:
                started.set()
                assert release.wait(5)
                return httpx.Response(
                    200,
                    json={"items": [playlist_item(OTHER)], "nextPageToken": "next", "pageInfo": {"totalResults": 2}},
                )
            return httpx.Response(200, json={"items": [playlist_item(THIRD)]})
        return httpx.Response(200, json={"items": [video_item(request.url.params["id"].split(",")[0])]})

    _install(monkeypatch, handler)
    store = PlaylistStore(tmp_path / "playlists.json")
    app = create_app(store)
    body = json.dumps({"url": LIST, "name": "朝"}).encode()

    async def scenario():
        sent = False
        disconnect = asyncio.Event()

        async def receive():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            if disconnect.is_set():
                return {"type": "http.disconnect"}
            await disconnect.wait()
            return {"type": "http.disconnect"}

        async def send(_message):
            return None

        task = asyncio.create_task(app(_import_scope(body), receive, send))
        assert await asyncio.to_thread(started.wait, 2)
        disconnect.set()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            saw_stopping = False
            for _ in range(40):
                probe = await asyncio.wait_for(
                    http.post("/api/playlists/import", json={"url": LIST, "name": "次"}),
                    1,
                )
                if probe.status_code != 429:
                    raise AssertionError(probe.text)
                if probe.json()["detail"] == "前の取り込みを止めています":
                    saw_stopping = True
                    break
                await asyncio.sleep(0.05)
            assert saw_stopping
            assert release.is_set() is False
            assert pages["n"] == 1
            release.set()
            await asyncio.wait_for(task, 2)
            assert pages["n"] == 1
            again = await http.post("/api/playlists/import", json={"url": LIST, "name": "朝"})
            assert again.status_code == 200, again.text

    anyio.run(scenario)
    assert len(store.list_playlists()) == 1


def test_cancel_kills_the_ytdlp_process_group(tmp_path, monkeypatch):
    ready = threading.Event()
    hanging = _Proc(None)
    seen = _install_ytdlp(monkeypatch, [], proc=hanging, ready=ready)
    store = PlaylistStore(tmp_path / "playlists.json")
    app = create_app(store)
    body = json.dumps({"url": LIST, "name": "朝"}).encode()

    async def scenario():
        sent = False
        disconnect = asyncio.Event()

        async def receive():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            if disconnect.is_set():
                return {"type": "http.disconnect"}
            await disconnect.wait()
            return {"type": "http.disconnect"}

        async def send(_message):
            return None

        task = asyncio.create_task(app(_import_scope(body), receive, send))
        assert await asyncio.to_thread(ready.wait, 2)
        disconnect.set()
        await asyncio.wait_for(task, 2)

    anyio.run(scenario)
    assert seen["start_new_session"] is True
    assert (hanging.pid, signal.SIGKILL) in seen["kills"]
    assert hanging.reaped.is_set()
    assert store.list_playlists() == []


def test_a_busy_rejection_does_not_spend_the_rate_budget(tmp_path, monkeypatch):
    started = threading.Event()
    release = threading.Event()
    hold = {"on": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/playlistItems") and hold["on"]:
            started.set()
            assert release.wait(5)
            hold["on"] = False
        return _fast_import_handler(request)

    _install(monkeypatch, handler)
    app = create_app(PlaylistStore(tmp_path / "playlists.json"))
    clock = {"t": 5_000.0}
    app.state.import_slot.now = lambda: clock["t"]

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            created = (await http.post("/api/playlists", json={"name": "夜"})).json()
            payload = {"url": LIST, "playlist_id": created["id"]}
            minute_ip = {"X-Real-IP": "203.0.113.7"}
            hold["on"] = True
            task = asyncio.create_task(http.post("/api/playlists/import", json=payload, headers=minute_ip))
            assert await asyncio.to_thread(started.wait, 2)
            for _ in range(6):
                busy = await http.post("/api/playlists/import", json=payload, headers=minute_ip)
                assert busy.status_code == 429
                assert busy.json()["detail"] == "取り込み中です"
            release.set()
            first = await asyncio.wait_for(task, 2)
            assert first.status_code == 200, first.text
            for _ in range(5):
                ok = await http.post("/api/playlists/import", json=payload, headers=minute_ip)
                assert ok.status_code == 200, ok.text
            blocked = await http.post("/api/playlists/import", json=payload, headers=minute_ip)
            assert blocked.status_code == 429
            assert blocked.json()["detail"] == "取り込みの回数が多いです"

            ip = {"X-Real-IP": "203.0.113.8"}
            for index in range(10):
                clock["t"] = 20_000.0 + index * 61
                ok = await http.post("/api/playlists/import", json=payload, headers=ip)
                assert ok.status_code == 200, ok.text
            clock["t"] = 20_000.0 + 9 * 61
            started.clear()
            release.clear()
            hold["on"] = True
            held = asyncio.create_task(http.post("/api/playlists/import", json=payload, headers=ip))
            assert await asyncio.to_thread(started.wait, 2)
            for _ in range(10):
                busy = await http.post("/api/playlists/import", json=payload, headers=ip)
                assert busy.status_code == 429
                assert busy.json()["detail"] == "取り込み中です"
            release.set()
            assert (await asyncio.wait_for(held, 2)).status_code == 200
            followed = await http.post("/api/playlists/import", json=payload, headers=ip)
            assert followed.status_code == 200, followed.text

    anyio.run(scenario)


def test_six_hundred_items_report_the_unscanned_tail(tmp_path, monkeypatch):
    store = PlaylistStore(tmp_path / "playlists.json")
    created = store.create("元")
    store.add_track(created["id"], _track(vid(9000), "先"))
    store.add_track(created["id"], _track(vid(9001), "次"))
    pages: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/playlistItems"):
            page = int(request.url.params.get("pageToken") or "0")
            pages.append(page)
            start = page * 50
            if start >= 600:
                return httpx.Response(200, json={"items": [], "pageInfo": {"totalResults": 600}})
            count = min(50, 600 - start)
            items = [playlist_item(vid(start + offset)) for offset in range(count)]
            body = {"items": items, "pageInfo": {"totalResults": 600}}
            if start + count < 600:
                body["nextPageToken"] = str(page + 1)
            return httpx.Response(200, json=body)
        requested = request.url.params["id"].split(",")
        return httpx.Response(200, json={"items": [video_item(video_id) for video_id in requested]})

    _install(monkeypatch, handler)
    client = TestClient(create_app(store))
    response = client.post("/api/playlists/import", json={"url": LIST, "playlist_id": created["id"]})
    assert response.status_code == 200, response.text
    body = response.json()
    assert pages == list(range(10))
    assert body["added"] == 298
    assert body["duplicates"] == 0
    assert body["unavailable"] == 0
    assert body["overflow"] == 302
    assert len(body["playlist"]["tracks"]) == 300


def test_ytdlp_skips_ids_the_destination_already_has(tmp_path, monkeypatch):
    entries = [
        {"id": VIDEO, "title": "既", "duration": 10, "channel": "人"},
        {"id": OTHER, "title": "新", "duration": 12, "channel": "人"},
    ]
    _install_ytdlp(monkeypatch, entries)
    fetched = fetch_youtube_playlist(LIST, known={VIDEO})
    assert [track.id for track in fetched.tracks] == [OTHER]
    assert fetched.repeated == 1

    store = PlaylistStore(tmp_path / "playlists.json")
    created = store.create("夜")
    store.add_track(created["id"], _track(VIDEO, "既"))
    seen: dict[str, object] = {}
    real = fetch_youtube_playlist

    def wrapped(source, **kwargs):
        seen["known"] = kwargs.get("known")
        return real(source, **kwargs)

    monkeypatch.setattr("djtube.app.fetch_youtube_playlist", wrapped)
    client = TestClient(create_app(store))
    response = client.post("/api/playlists/import", json={"url": LIST, "playlist_id": created["id"]})
    assert response.status_code == 200, response.text
    assert seen["known"] == {VIDEO}
    assert response.json()["added"] == 1
    assert response.json()["duplicates"] == 1
    assert [item["id"] for item in response.json()["playlist"]["tracks"]] == [VIDEO, OTHER]


def test_one_address_cannot_spend_the_day(tmp_path, monkeypatch):
    _install(monkeypatch, _fast_import_handler)
    client, _store = _app(tmp_path)
    created = client.post("/api/playlists", json={"name": "夜"}).json()
    clock = {"t": 10_000.0}
    client.app.state.import_slot.now = lambda: clock["t"]
    payload = {"url": LIST, "playlist_id": created["id"]}
    ip = {"X-Real-IP": "203.0.113.10"}
    rejected = client.post("/api/playlists/import", json={"url": "not-a-list", "playlist_id": created["id"]}, headers=ip)
    assert rejected.status_code == 400
    for index in range(20):
        clock["t"] = 10_000.0 + index * 61
        response = client.post("/api/playlists/import", json=payload, headers={**ip, "X-Forwarded-For": "198.51.100.9"})
        assert response.status_code == 200, response.text
    clock["t"] = 10_000.0 + 19 * 61
    blocked = client.post("/api/playlists/import", json=payload, headers={**ip, "X-Forwarded-For": "198.51.100.50"})
    assert blocked.status_code == 429
    assert blocked.json()["detail"] == "24時間の取り込みの上限に達しました"
    other = client.post("/api/playlists/import", json=payload, headers={"X-Real-IP": "203.0.113.11"})
    assert other.status_code == 200, other.text
    spoofed = client.post("/api/playlists/import", json=payload, headers={"X-Forwarded-For": "203.0.113.99"})
    assert spoofed.status_code == 200, spoofed.text
    clock["t"] = 10_000.0 + 86400.0 - 0.001
    early = client.post("/api/playlists/import", json=payload, headers=ip)
    assert early.status_code == 429
    clock["t"] = 10_000.0 + 86400.0
    again = client.post("/api/playlists/import", json=payload, headers=ip)
    assert again.status_code == 200, again.text
    for index in range(20):
        clock["t"] = 200_000.0 + index * 61
        response = client.post("/api/playlists/import", json=payload, headers={"X-Real-IP": "2001:db8:abcd:1::10"})
        assert response.status_code == 200, response.text
    same_prefix = client.post("/api/playlists/import", json=payload, headers={"X-Real-IP": "2001:db8:abcd:1::99"})
    assert same_prefix.status_code == 429
    assert same_prefix.json()["detail"] == "24時間の取り込みの上限に達しました"
    other_prefix = client.post("/api/playlists/import", json=payload, headers={"X-Real-IP": "2001:db8:abcd:2::10"})
    assert other_prefix.status_code == 200, other_prefix.text


def test_process_exit_kills_a_running_ytdlp_group(tmp_path):
    marker = tmp_path / "killed"
    script = tmp_path / "reap.py"
    script.write_text(
        "from djtube import youtube_playlist\n"
        "class Proc:\n"
        "    def __init__(self):\n"
        "        self.pid = 424242\n"
        "        self.returncode = None\n"
        "    def poll(self):\n"
        "        return self.returncode\n"
        "    def wait(self, timeout=None):\n"
        "        return self.returncode\n"
        "    def kill(self):\n"
        "        self.returncode = -9\n"
        "def killpg(pid, sig):\n"
        f"    open({str(marker)!r}, 'w').write(f'{{pid}} {{int(sig)}}')\n"
        "    youtube_playlist._YTDLP_PROC.kill()\n"
        "youtube_playlist.os.killpg = killpg\n"
        "youtube_playlist._watch_ytdlp(Proc())\n"
    )
    completed = _REAL_POPEN([sys.executable, str(script)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    stdout, stderr = completed.communicate(timeout=15)
    assert completed.returncode == 0, stderr + stdout
    assert marker.read_text(encoding="utf-8") == f"424242 {int(signal.SIGKILL)}"
