from __future__ import annotations

import httpx

from djtube.search import (
    SearchError,
    Track,
    parse_iso8601_duration,
    search_tracks,
    search_youtube_api,
    tracks_from_ytdlp_info,
)


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


def test_fallback_when_data_api_fails(monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "test-key")

    def boom(*_args, **_kwargs):
        raise SearchError("検索できませんでした")

    monkeypatch.setattr("djtube.search.search_youtube_api", boom)
    monkeypatch.setattr(
        "djtube.search.search_ytdlp",
        lambda _query: [Track("abcdefghijk", "曲", "人", 10, None)],
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
    monkeypatch.setattr("djtube.search.search_ytdlp", lambda _query: [])
    tracks, source = search_tracks("  city   pop ")
    assert source == "ytdlp"
    assert tracks == []
    assert called["api"] == 0


def test_blank_query():
    try:
        search_tracks("   ")
    except SearchError as exc:
        assert str(exc) == "検索語を入れてください"
    else:
        raise AssertionError("expected SearchError")
