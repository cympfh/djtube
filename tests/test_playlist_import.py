from __future__ import annotations

import json
import logging

import httpx
import pytest
from fastapi.testclient import TestClient

from djtube.app import create_app
from djtube.playlists import PlaylistStore
from djtube.search import Track
from djtube.youtube_playlist import (
    PLAYLIST_ITEM_LIMIT,
    PlaylistLookupError,
    fetch_youtube_playlist,
    parse_playlist_id,
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
        "WL": "WL",
        "LL": "LL",
        "LM": "LM",
        "RDMM": "RDMM",
        "RD" + VIDEO: "RD" + VIDEO,
        "UU" + "b" * 22: "UU" + "b" * 22,
        album: album,
        f"https://www.youtube.com/playlist?list={album}": album,
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
        "PL" + "a" * 65,
        "pl" + "a" * 32,
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

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/playlistItems"):
            return httpx.Response(
                200,
                json={
                    "items": [
                        playlist_item(VIDEO, title="残る"),
                        playlist_item(OTHER, private=True),
                        playlist_item(THIRD, deleted=True),
                        {"snippet": {"title": "空"}},
                    ]
                },
            )
        seen.append(request.url.params["id"])
        return httpx.Response(200, json={"items": [video_item(VIDEO, title="残る")]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        fetched = fetch_youtube_playlist(LIST, client=client)
    assert seen == [VIDEO]
    assert [track.id for track in fetched.tracks] == [VIDEO]
    assert fetched.unavailable == 3
    assert fetched.repeated == 0


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
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/videos"):
            requested = request.url.params["id"].split(",")
            return httpx.Response(200, json={"items": [video_item(video_id) for video_id in requested]})
        calls["n"] += 1
        assert int(request.url.params["maxResults"]) == 2
        items = [playlist_item(vid(offset)) for offset in range(5)]
        return httpx.Response(200, json={"items": items, "nextPageToken": "more"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        fetched = fetch_youtube_playlist(LIST, client=client, limit=2)
    assert calls["n"] == 1
    assert [track.id for track in fetched.tracks] == [vid(0), vid(1)]


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


def test_without_a_key_flat_playlist_skips_private_and_repeats(monkeypatch):
    seen = {}

    class YoutubeDL:
        def __init__(self, options):
            seen["options"] = options

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, url, download=False):
            seen["url"] = url
            seen["download"] = download
            return {
                "entries": [
                    {
                        "id": VIDEO,
                        "title": "夜",
                        "channel": "人",
                        "duration": 90,
                        "thumbnail": f"https://i.ytimg.com/vi/{VIDEO}/mqdefault.jpg",
                    },
                    {"id": OTHER, "title": "[Private video]", "availability": "private"},
                    {"id": VIDEO, "title": "夜", "duration": 90},
                    {"id": THIRD, "title": "[Deleted video]"},
                    None,
                ]
            }

    monkeypatch.setattr("yt_dlp.YoutubeDL", YoutubeDL)
    fetched = fetch_youtube_playlist(f"https://music.youtube.com/playlist?list={LIST}")
    assert seen["download"] is False
    assert seen["options"]["extract_flat"] is True
    assert seen["options"]["skip_download"] is True
    assert seen["options"]["playlistend"] == PLAYLIST_ITEM_LIMIT
    assert seen["url"] == f"https://www.youtube.com/playlist?list={LIST}"
    assert [track.id for track in fetched.tracks] == [VIDEO]
    assert fetched.tracks[0].channel == "人"
    assert fetched.tracks[0].duration == 90
    assert fetched.repeated == 1
    assert fetched.unavailable == 3


def test_ytdlp_failure_is_an_error_without_the_message(monkeypatch, caplog):
    caplog.set_level(logging.WARNING)

    class YoutubeDL:
        def __init__(self, _options):
            return None

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, _url, download=False):
            raise RuntimeError(f"boom {KEY}")

    monkeypatch.setattr("yt_dlp.YoutubeDL", YoutubeDL)
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

    def handler(request: httpx.Request) -> httpx.Response:
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
    assert store.path.read_text(encoding="utf-8") == before


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
    seen = {}

    class YoutubeDL:
        def __init__(self, options):
            seen["options"] = options

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, url, download=False):
            seen["url"] = url
            assert download is False
            return {"entries": [{"id": OTHER, "title": "昼", "channel": "店", "duration": 12}]}

    monkeypatch.setattr("yt_dlp.YoutubeDL", YoutubeDL)
    client, _store = _app(tmp_path)
    response = client.post(
        "/api/playlists/import",
        json={"url": f"https://www.youtube.com/watch?v={VIDEO}&list={LIST}", "name": "朝"},
    )
    assert response.status_code == 200
    assert seen["options"]["extract_flat"] is True
    assert seen["url"] == f"https://www.youtube.com/playlist?list={LIST}"
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
    assert [item["id"] for item in store.list_playlists()[0]["tracks"]] == [THIRD]
