from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from djtube.app import create_app
from djtube.audio import (
    AudioError,
    AudioExpired,
    AudioSource,
    clear_audio_cache,
    content_type_for,
    host_allowed,
    open_audio,
    resolve_audio,
    select_audio_format,
)
from djtube.ids import watch_url

VIDEO_ID = "abcdefghijk"
MEDIA = "https://rr1---sn-example.googlevideo.com/videoplayback?id=1"


def _fmt(ext="m4a", abr=128, url=MEDIA, protocol="https", acodec="mp4a.40.2", **extra):
    fmt = {
        "url": url,
        "ext": ext,
        "protocol": protocol,
        "vcodec": "none",
        "acodec": acodec,
        "abr": abr,
    }
    fmt.update(extra)
    return fmt


def test_select_audio_format_prefers_progressive_m4a():
    info = {
        "formats": [
            _fmt("webm", 160, acodec="opus"),
            _fmt("m4a", 128),
            _fmt("m4a", 256, fragments=[{"url": MEDIA}]),
            _fmt("m4a", 999, url="https://evil.example/audio"),
            _fmt("mp4", 999, protocol="m3u8_native"),
            _fmt("m4a", 64, vcodec="avc1"),
        ]
    }
    chosen = select_audio_format(info)
    assert chosen["ext"] == "m4a"
    assert chosen["abr"] == 128
    assert content_type_for(chosen) == "audio/mp4"
    assert select_audio_format({"formats": []}) is None
    assert host_allowed(MEDIA) is True
    assert host_allowed("http://rr1.googlevideo.com/a") is False
    assert host_allowed("https://user:pass@rr1.googlevideo.com/a") is False
    assert host_allowed("https://evil.example/a") is False


def test_resolve_audio_caches_and_rejects_other_hosts():
    clear_audio_cache()
    calls = []

    def extract(video_id):
        calls.append(video_id)
        assert video_id == VIDEO_ID
        return {"formats": [_fmt()], "http_headers": {"User-Agent": "djtube-test"}}

    source = resolve_audio(VIDEO_ID, extract=extract)
    assert source.url == MEDIA
    assert source.content_type == "audio/mp4"
    assert source.headers["User-Agent"] == "djtube-test"
    assert watch_url(VIDEO_ID).startswith("https://www.youtube.com/watch?v=")
    again = resolve_audio(VIDEO_ID, extract=extract)
    assert again.url == source.url
    assert calls == [VIDEO_ID]

    def foreign(_video_id):
        return {"formats": [_fmt(url="https://evil.example/secret-audio")]}

    clear_audio_cache()
    with pytest.raises(AudioError) as caught:
        resolve_audio("zzzzzzzzzzz", extract=foreign)
    assert caught.value.status == 502
    assert "evil.example" not in str(caught.value)

    with pytest.raises(AudioError) as missing:
        resolve_audio("short", extract=extract)
    assert missing.value.status == 404


def test_open_audio_retries_when_the_media_url_expires(monkeypatch):
    clear_audio_cache()
    calls = {"n": 0}

    def resolve(_video_id):
        calls["n"] += 1
        return AudioSource(url=f"{MEDIA}&n={calls['n']}", content_type="audio/mp4", headers={})

    def upstream(source, range_header):
        assert range_header == "bytes=0-1"
        if calls["n"] == 1:
            raise AudioExpired("音源を取得できませんでした")
        return source.url

    monkeypatch.setattr("djtube.audio.resolve_audio", resolve)
    monkeypatch.setattr("djtube.audio.open_upstream", upstream)
    assert open_audio(VIDEO_ID, "bytes=0-1").endswith("n=2")
    assert calls["n"] == 2


def test_audio_route_proxies_bytes_and_hides_the_upstream(monkeypatch):
    class Fake:
        status_code = 206

        def response_headers(self):
            return {
                "Content-Type": "audio/mp4",
                "Content-Range": "bytes 0-3/9",
                "Content-Length": "4",
                "Accept-Ranges": "bytes",
            }

        def iter_bytes(self):
            yield b"test"

    seen = {}

    def fake_open(video_id, range_header):
        seen["id"] = video_id
        seen["range"] = range_header
        return Fake()

    monkeypatch.setattr("djtube.app.open_audio", fake_open)
    client = TestClient(create_app())
    response = client.get(f"/djtube/api/audio/{VIDEO_ID}", headers={"Range": "bytes=0-3"})
    assert response.status_code == 206
    assert response.content == b"test"
    assert response.headers["content-type"] == "audio/mp4"
    assert seen == {"id": VIDEO_ID, "range": "bytes=0-3"}
    assert "googlevideo" not in response.text

    missing = client.get("/api/audio/not-an-id")
    assert missing.status_code == 404
    assert "googlevideo" not in missing.text


def test_audio_route_reports_resolve_failure(monkeypatch):
    def fake_open(_video_id, _range_header):
        raise AudioError("音源を取得できませんでした")

    monkeypatch.setattr("djtube.app.open_audio", fake_open)
    client = TestClient(create_app())
    response = client.get(f"/api/audio/{VIDEO_ID}")
    assert response.status_code == 502
    assert response.json()["detail"] == "音源を取得できませんでした"
