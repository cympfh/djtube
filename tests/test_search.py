from __future__ import annotations

import httpx

from fastapi.testclient import TestClient

from djtube.app import create_app
from djtube.ids import video_id_from_query
from djtube.search import (
    SearchError,
    Track,
    parse_iso8601_duration,
    search_tracks,
    search_youtube_api,
    search_ytdlp,
    tracks_from_ytdlp_info,
)

PASTED_VIDEO = "l9udjy7vbm8"
PASTED_URL = "https://www.youtube.com/watch?v=l9udjy7vbm8"


def test_parse_iso8601_duration():
    assert parse_iso8601_duration("PT1H2M3S") == 3723
    assert parse_iso8601_duration("PT4M") == 240
    assert parse_iso8601_duration("PT45S") == 45
    assert parse_iso8601_duration("nope") is None


def test_ytdlp_info_parser_keeps_video_ids_only():
    info = {
        "entries": [
            {
                "id": "abcdefghijk",
                "title": "夜の街",
                "channel": "Band",
                "duration": 95.2,
                "thumbnail": "https://i.ytimg.com/vi/abcdefghijk/hqdefault.jpg",
            },
            {"id": "not-an-id", "title": "skip", "url": "https://www.youtube.com/watch?v=zzzzzzzzzzz"},
            {"title": "from url", "url": "https://youtu.be/yyyyyyyyyyy"},
        ]
    }
    tracks = tracks_from_ytdlp_info(info)
    assert [track.id for track in tracks] == ["abcdefghijk", "zzzzzzzzzzz", "yyyyyyyyyyy"]
    assert tracks[0].duration == 95
    assert tracks[0].thumbnail.startswith("https://")


def test_youtube_api_preserves_search_order_and_hides_key():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path.endswith("/search"):
            assert request.url.params["key"] == "test-key"
            assert request.url.params["safeSearch"] == "none"
            assert "videoEmbeddable" not in request.url.params
            return httpx.Response(
                200,
                json={
                    "items": [
                        {"id": {"videoId": "abcdefghijk"}},
                        {"id": {"videoId": "zzzzzzzzzzz"}},
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "zzzzzzzzzzz",
                        "snippet": {
                            "title": "Second",
                            "channelTitle": "B",
                            "thumbnails": {"medium": {"url": "https://i.ytimg.com/b.jpg"}},
                        },
                        "contentDetails": {"duration": "PT2M"},
                    },
                    {
                        "id": "abcdefghijk",
                        "snippet": {
                            "title": "First",
                            "channelTitle": "A",
                            "thumbnails": {"default": {"url": "https://i.ytimg.com/a.jpg"}},
                        },
                        "contentDetails": {"duration": "PT1M1S"},
                    },
                ]
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        tracks = search_youtube_api("city pop", "test-key", client)
    assert [track.title for track in tracks] == ["First", "Second"]
    assert tracks[0].duration == 61
    assert "test-key" not in repr(tracks)


def test_age_gated_query_is_requested_unfiltered():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/search"):
            assert request.url.params["q"] == "同人誌"
            assert request.url.params["safeSearch"] == "none"
            assert request.url.params["type"] == "video"
            assert "videoEmbeddable" not in request.url.params
            return httpx.Response(200, json={"items": [{"id": {"videoId": "abcdefghijk"}}]})
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "abcdefghijk",
                        "snippet": {
                            "title": "同人誌を広げる",
                            "channelTitle": "棚",
                            "thumbnails": {"medium": {"url": "https://i.ytimg.com/a.jpg"}},
                        },
                        "contentDetails": {"duration": "PT3M"},
                    }
                ]
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        tracks = search_youtube_api("同人誌", "test-key", client)
    assert [track.id for track in tracks] == ["abcdefghijk"]
    assert tracks[0].title == "同人誌を広げる"
    assert "test-key" not in repr(tracks)


def test_api_error_object_is_not_treated_as_no_hits():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": {"code": 403, "message": "forbidden"}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        try:
            search_youtube_api("同人誌", "test-key", client)
        except SearchError as exc:
            assert str(exc) == "検索できませんでした"
            assert "test-key" not in str(exc)
        else:
            raise AssertionError("expected SearchError")


def test_empty_data_api_falls_back_to_ytdlp(monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "test-key")
    monkeypatch.setattr("djtube.search.search_youtube_api", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        "djtube.search.search_ytdlp",
        lambda _query, music=False: [Track("abcdefghijk", "同人誌を広げる", "棚", 180, None)],
    )
    tracks, source = search_tracks("同人誌")
    assert source == "ytdlp"
    assert tracks[0].title == "同人誌を広げる"


def test_fallback_when_data_api_fails(monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "test-key")

    def boom(*_args, **_kwargs):
        raise SearchError("検索できませんでした")

    monkeypatch.setattr("djtube.search.search_youtube_api", boom)
    monkeypatch.setattr(
        "djtube.search.search_ytdlp",
        lambda _query, music=False: [Track("abcdefghijk", "曲", "人", 10, None)],
    )
    tracks, source = search_tracks("query")
    assert source == "ytdlp"
    assert tracks[0].id == "abcdefghijk"


def test_without_key_skips_data_api(monkeypatch):
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    called = {"api": 0}

    def api(*_args, **_kwargs):
        called["api"] += 1
        raise AssertionError("api should not be called")

    monkeypatch.setattr("djtube.search.search_youtube_api", api)
    monkeypatch.setattr("djtube.search.search_ytdlp", lambda _query, music=False: [])
    tracks, source = search_tracks("  city   pop ")
    assert source == "ytdlp"
    assert tracks == []
    assert called["api"] == 0


def _video_item(video_id: str) -> dict:
    return {
        "id": video_id,
        "snippet": {
            "title": video_id,
            "channelTitle": "棚",
            "thumbnails": {"medium": {"url": "https://i.ytimg.com/a.jpg"}},
        },
        "contentDetails": {"duration": "PT1M"},
    }


def test_search_pages_until_fifty():
    pages = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/search"):
            pages.append(request.url.params.get("pageToken"))
            assert request.url.params["maxResults"] == "50"
            assert request.url.params["safeSearch"] == "none"
            assert "videoCategoryId" not in request.url.params
            if "pageToken" not in request.url.params:
                items = [{"id": {"videoId": f"{index:011d}"}} for index in range(30)]
                return httpx.Response(200, json={"items": items, "nextPageToken": "page-2"})
            items = [{"id": {"videoId": f"{index:011d}"}} for index in range(30, 55)]
            return httpx.Response(200, json={"items": items})
        requested = request.url.params["id"].split(",")
        assert len(requested) == 50
        return httpx.Response(200, json={"items": [_video_item(video_id) for video_id in requested]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        tracks = search_youtube_api("city pop", "test-key", client)
    assert pages == [None, "page-2"]
    assert [track.id for track in tracks] == [f"{index:011d}" for index in range(50)]


def test_search_stops_after_a_full_page():
    calls = {"search": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/search"):
            calls["search"] += 1
            items = [{"id": {"videoId": f"{index:011d}"}} for index in range(50)]
            return httpx.Response(200, json={"items": items, "nextPageToken": "unused"})
        requested = request.url.params["id"].split(",")
        return httpx.Response(200, json={"items": [_video_item(video_id) for video_id in requested]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        tracks = search_youtube_api("city pop", "test-key", client)
    assert calls["search"] == 1
    assert len(tracks) == 50


def test_ytdlp_asks_for_fifty(monkeypatch):
    seen = {}

    class FakeYoutubeDL:
        def __init__(self, options):
            seen["playlistend"] = options["playlistend"]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, url, download=False):
            seen["url"] = url
            assert download is False
            return {"entries": [{"id": "abcdefghijk", "title": "曲", "channel": "人"}]}

    monkeypatch.setattr("yt_dlp.YoutubeDL", FakeYoutubeDL)
    tracks = search_ytdlp("city pop")
    assert tracks[0].id == "abcdefghijk"
    assert seen == {"playlistend": 50, "url": "ytsearch50:city pop"}


def test_music_category_is_opt_in():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/search"):
            seen.append(dict(request.url.params))
            assert request.url.params["type"] == "video"
            assert request.url.params["maxResults"] == "50"
            assert "topicId" not in request.url.params
            return httpx.Response(200, json={"items": [{"id": {"videoId": "abcdefghijk"}}]})
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "abcdefghijk",
                        "snippet": {
                            "title": "曲",
                            "channelTitle": "人",
                            "thumbnails": {"medium": {"url": "https://i.ytimg.com/a.jpg"}},
                        },
                        "contentDetails": {"duration": "PT1M"},
                    }
                ]
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        search_youtube_api("city pop", "test-key", client, music=True)
        search_youtube_api("city pop", "test-key", client, music=False)
    assert seen[0]["videoCategoryId"] == "10"
    assert "videoCategoryId" not in seen[1]


def test_ytdlp_music_uses_song_search(monkeypatch):
    seen = {}

    class FakeYoutubeDL:
        def __init__(self, _options):
            return None

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, url, download=False):
            seen["url"] = url
            assert download is False
            return {"entries": [{"id": "abcdefghijk", "title": "曲", "channel": "人"}]}

    monkeypatch.setattr("yt_dlp.YoutubeDL", FakeYoutubeDL)
    assert search_ytdlp("スピッツ", music=True)[0].id == "abcdefghijk"
    assert seen["url"].startswith("https://music.youtube.com/search?")
    assert seen["url"].endswith("#songs")
    search_ytdlp("city pop", music=False)
    assert seen["url"] == "ytsearch50:city pop"


def test_blank_query():
    try:
        search_tracks("   ")
    except SearchError as exc:
        assert str(exc) == "検索語を入れてください"
    else:
        raise AssertionError("expected SearchError")


def _install_search_client(monkeypatch, handler):
    real_client = httpx.Client
    transport = httpx.MockTransport(handler)

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", factory)


def _forbid_keyword_search(monkeypatch):
    def keyword(*_args, **_kwargs):
        raise AssertionError("keyword search")

    monkeypatch.setattr("djtube.search.search_youtube_api", keyword)
    monkeypatch.setattr("djtube.search.search_ytdlp", keyword)


def _forbid_ytdlp(monkeypatch):
    class YoutubeDL:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("ytdlp")

    monkeypatch.setattr("yt_dlp.YoutubeDL", YoutubeDL)


def _pasted_video_item() -> dict:
    return {
        "id": PASTED_VIDEO,
        "snippet": {
            "title": "夜の街",
            "channelTitle": "Band",
            "thumbnails": {"medium": {"url": f"https://i.ytimg.com/vi/{PASTED_VIDEO}/hqdefault.jpg"}},
        },
        "contentDetails": {"duration": "PT3M15S"},
    }


def _videos_handler(request: httpx.Request) -> httpx.Response:
    assert request.url.path.endswith("/videos")
    assert not request.url.path.endswith("/search")
    assert request.url.params["id"] == PASTED_VIDEO
    assert "videoCategoryId" not in request.url.params
    return httpx.Response(200, json={"items": [_pasted_video_item()]})


def test_youtube_url_shapes_are_that_video():
    queries = [
        PASTED_URL,
        f"https://youtu.be/{PASTED_VIDEO}",
        f"https://www.youtube.com/shorts/{PASTED_VIDEO}",
        f"https://www.youtube.com/embed/{PASTED_VIDEO}",
        f"https://www.youtube.com/live/{PASTED_VIDEO}",
        f"https://www.youtube.com/v/{PASTED_VIDEO}",
        f"https://music.youtube.com/watch?v={PASTED_VIDEO}",
        f"https://www.youtube-nocookie.com/embed/{PASTED_VIDEO}",
        f"http://m.youtube.com/watch?v={PASTED_VIDEO}&t=30s",
        f"youtu.be/{PASTED_VIDEO}",
        f"www.youtube.com/watch?v={PASTED_VIDEO}",
        f"music.youtube.com/watch?v={PASTED_VIDEO}&feature=share",
    ]
    for query in queries:
        assert video_id_from_query(query) == PASTED_VIDEO


def test_queries_that_only_mention_a_url_stay_keywords():
    queries = [
        "city pop",
        "スピッツ",
        "lo-fi beats",
        f"see youtu.be/{PASTED_VIDEO}",
        f"https://example.com/watch?v={PASTED_VIDEO}",
        "https://www.youtube.com/playlist?list=PLabcdefghijk",
        "https://music.youtube.com/search?q=city+pop",
        f"v={PASTED_VIDEO}",
    ]
    for query in queries:
        assert video_id_from_query(query) is None


def test_watch_url_returns_that_single_video(monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "test-key")
    _forbid_keyword_search(monkeypatch)
    _forbid_ytdlp(monkeypatch)
    _install_search_client(monkeypatch, _videos_handler)
    tracks, source = search_tracks(PASTED_URL)
    assert source == "youtube"
    assert len(tracks) == 1
    track = tracks[0]
    assert track.id == PASTED_VIDEO
    assert track.title == "夜の街"
    assert track.channel == "Band"
    assert track.duration == 195
    assert track.thumbnail == f"https://i.ytimg.com/vi/{PASTED_VIDEO}/hqdefault.jpg"
    assert PASTED_URL not in track.title
    assert "test-key" not in repr(tracks)


def test_bare_video_id_returns_that_single_video(monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "test-key")
    _forbid_keyword_search(monkeypatch)
    _forbid_ytdlp(monkeypatch)
    _install_search_client(monkeypatch, _videos_handler)
    tracks, source = search_tracks(PASTED_VIDEO)
    assert source == "youtube"
    assert len(tracks) == 1
    assert tracks[0].id == PASTED_VIDEO
    assert tracks[0].title == "夜の街"
    assert tracks[0].thumbnail.startswith("https://")


def test_other_youtube_urls_return_that_single_video(monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "test-key")
    _forbid_keyword_search(monkeypatch)
    _forbid_ytdlp(monkeypatch)
    _install_search_client(monkeypatch, _videos_handler)
    queries = [
        f"https://youtu.be/{PASTED_VIDEO}",
        f"https://www.youtube.com/shorts/{PASTED_VIDEO}",
        f"https://www.youtube.com/embed/{PASTED_VIDEO}?start=10",
        f"https://music.youtube.com/watch?v={PASTED_VIDEO}",
        f"https://www.youtube.com/live/{PASTED_VIDEO}",
        f"https://m.youtube.com/watch?v={PASTED_VIDEO}",
    ]
    for query in queries:
        tracks, source = search_tracks(query, music=False)
        assert source == "youtube"
        assert [track.id for track in tracks] == [PASTED_VIDEO]
        assert tracks[0].title == "夜の街"


def test_keyword_query_still_searches(monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "test-key")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/search"):
            seen["q"] = request.url.params["q"]
            seen["category"] = request.url.params.get("videoCategoryId")
            return httpx.Response(200, json={"items": [{"id": {"videoId": "abcdefghijk"}}]})
        assert request.url.params["id"] == "abcdefghijk"
        return httpx.Response(200, json={"items": [_video_item("abcdefghijk")]})

    _install_search_client(monkeypatch, handler)
    tracks, source = search_tracks("city pop")
    assert source == "youtube"
    assert seen == {"q": "city pop", "category": "10"}
    assert [track.id for track in tracks] == ["abcdefghijk"]


def test_text_containing_a_video_url_still_searches(monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "test-key")
    query = f"see youtu.be/{PASTED_VIDEO}"
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/search"):
            seen["q"] = request.url.params["q"]
            return httpx.Response(200, json={"items": [{"id": {"videoId": "abcdefghijk"}}]})
        return httpx.Response(200, json={"items": [_video_item("abcdefghijk")]})

    _install_search_client(monkeypatch, handler)
    tracks, source = search_tracks(query)
    assert source == "youtube"
    assert seen["q"] == query
    assert [track.id for track in tracks] == ["abcdefghijk"]


def test_unresolved_video_does_not_keyword_search(monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "test-key")
    _forbid_keyword_search(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oembed":
            assert PASTED_VIDEO in request.url.params["url"]
            assert "ytsearch" not in str(request.url)
            return httpx.Response(404)
        assert request.url.path.endswith("/videos")
        assert request.url.params["id"] == PASTED_VIDEO
        return httpx.Response(200, json={"items": []})

    _install_search_client(monkeypatch, handler)

    class FakeYoutubeDL:
        def __init__(self, options):
            assert options["noplaylist"] is True
            assert "cookiefile" not in options

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, url, download=False):
            assert download is False
            assert url == f"https://www.youtube.com/watch?v={PASTED_VIDEO}"
            assert not url.startswith("ytsearch")
            raise RuntimeError("missing")

    monkeypatch.setattr("yt_dlp.YoutubeDL", FakeYoutubeDL)
    for query in (PASTED_URL, PASTED_VIDEO):
        try:
            search_tracks(query)
        except SearchError as exc:
            assert str(exc) == "その動画は見つかりませんでした"
        else:
            raise AssertionError(query)


def test_url_without_api_key_uses_oembed_for_that_video(monkeypatch):
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    _forbid_keyword_search(monkeypatch)
    _forbid_ytdlp(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/oembed"
        assert request.url.params["url"] == f"https://www.youtube.com/watch?v={PASTED_VIDEO}"
        assert "ytsearch" not in str(request.url)
        return httpx.Response(
            200,
            json={
                "title": "夜の街",
                "author_name": "Band",
                "type": "video",
                "thumbnail_url": f"https://i.ytimg.com/vi/{PASTED_VIDEO}/hqdefault.jpg",
            },
        )

    _install_search_client(monkeypatch, handler)
    tracks, source = search_tracks(PASTED_URL, music=True)
    assert source == "oembed"
    assert len(tracks) == 1
    track = tracks[0]
    assert track.id == PASTED_VIDEO
    assert track.title == "夜の街"
    assert track.channel == "Band"
    assert track.duration is None
    assert track.thumbnail == f"https://i.ytimg.com/vi/{PASTED_VIDEO}/hqdefault.jpg"
    bare, bare_source = search_tracks(PASTED_VIDEO)
    assert bare_source == "oembed"
    assert [item.id for item in bare] == [PASTED_VIDEO]
    assert bare[0].title == "夜の街"


def test_url_without_api_key_looks_up_that_video(monkeypatch):
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    _forbid_keyword_search(monkeypatch)
    monkeypatch.setattr("djtube.search.lookup_oembed", lambda _video_id: None)
    seen = {}

    class FakeYoutubeDL:
        def __init__(self, _options):
            return None

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, url, download=False):
            seen["url"] = url
            assert download is False
            return {
                "id": PASTED_VIDEO,
                "title": "夜の街",
                "channel": "Band",
                "duration": 95.2,
                "thumbnail": f"https://i.ytimg.com/vi/{PASTED_VIDEO}/hqdefault.jpg",
            }

    monkeypatch.setattr("yt_dlp.YoutubeDL", FakeYoutubeDL)
    tracks, source = search_tracks(f"https://youtu.be/{PASTED_VIDEO}")
    assert source == "ytdlp"
    assert len(tracks) == 1
    assert tracks[0].id == PASTED_VIDEO
    assert tracks[0].title == "夜の街"
    assert tracks[0].channel == "Band"
    assert tracks[0].duration == 95
    assert tracks[0].thumbnail.startswith("https://")
    assert seen["url"] == f"https://www.youtube.com/watch?v={PASTED_VIDEO}"


def test_lookup_does_not_keep_a_different_video(monkeypatch):
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    _forbid_keyword_search(monkeypatch)
    monkeypatch.setattr("djtube.search.lookup_oembed", lambda _video_id: None)

    class FakeYoutubeDL:
        def __init__(self, _options):
            return None

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, url, download=False):
            assert url == f"https://www.youtube.com/watch?v={PASTED_VIDEO}"
            assert download is False
            return {"id": "abcdefghijk", "title": "別の曲", "channel": "人"}

    monkeypatch.setattr("yt_dlp.YoutubeDL", FakeYoutubeDL)
    try:
        search_tracks(PASTED_VIDEO)
    except SearchError as exc:
        assert str(exc) == "その動画は見つかりませんでした"
    else:
        raise AssertionError("expected SearchError")


def test_search_route_reports_an_unresolved_video(monkeypatch):
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    monkeypatch.setattr("djtube.search.lookup_oembed", lambda _video_id: None)

    class FakeYoutubeDL:
        def __init__(self, _options):
            return None

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, _url, download=False):
            assert download is False
            raise RuntimeError("missing")

    monkeypatch.setattr("yt_dlp.YoutubeDL", FakeYoutubeDL)
    client = TestClient(create_app())
    response = client.get("/api/search", params={"q": PASTED_URL})
    assert response.status_code == 502
    assert response.json()["detail"] == "その動画は見つかりませんでした"
