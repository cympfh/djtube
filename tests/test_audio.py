from __future__ import annotations

import logging
import sys
import types

import httpx
import pytest
from fastapi.testclient import TestClient

from djtube.app import create_app
from djtube.audio import (
    AudioError,
    AudioExpired,
    AudioSource,
    clear_audio_cache,
    content_type_for,
    failure_notes,
    host_allowed,
    open_audio,
    open_upstream,
    redact_secrets,
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

    def upstream(source, range_header, **_kwargs):
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


def _caplog_info(caplog):
    caplog.set_level(logging.INFO, logger="djtube.audio")


def test_failure_notes_name_bot_consent_and_403():
    assert failure_notes("Sign in to confirm you're not a bot") == ["Bot判定"]
    assert failure_notes("ログインしてボットではないことを確認してください") == ["Bot判定"]
    assert failure_notes("https://consent.youtube.com/m?continue=1") == ["同意画面"]
    assert failure_notes("Invalid cookie consent redirect URL") == ["同意画面"]
    assert failure_notes("HTTP Error 403: Forbidden") == ["403"]
    assert "403" not in failure_notes("nothing to report")


def test_redact_secrets_drops_signed_urls_keys_and_cookies():
    raw = (
        "https://rr1.googlevideo.com/videoplayback?sig=SUPERSECRET&expire=1 "
        "https://consent.youtube.com/m?continue=https%3A%2F%2Fwatch%26sig%3DLEAKED&gl=JP "
        "Cookie: SID=COOKIESECRET AIzaSyDDDDDDDDDDDDDDDDDDDD key=AIzaSyEEEEEEEEEEEEEEEEEEEE"
    )
    cleaned = redact_secrets(raw)
    assert "consent.youtube.com" in cleaned
    assert "SUPERSECRET" not in cleaned
    assert "LEAKED" not in cleaned
    assert "COOKIESECRET" not in cleaned
    assert "AIzaSy" not in cleaned
    assert "googlevideo" not in cleaned
    assert "continue=" not in cleaned


def _install_ytdlp(monkeypatch, extract_info):
    seen = {}

    class DownloadError(Exception):
        def __init__(self, msg=None, exc_info=None):
            super().__init__(msg)
            self.exc_info = exc_info

    class YoutubeDL:
        def __init__(self, options):
            seen["options"] = options
            self._options = options
            self._download_retcode = 0
            self.Failure = DownloadError

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, url, download=False):
            seen["url"] = url
            return extract_info(self, url, download)

    module = types.ModuleType("yt_dlp")
    module.YoutubeDL = YoutubeDL
    module.DownloadError = DownloadError
    monkeypatch.setitem(sys.modules, "yt_dlp", module)
    return seen, DownloadError


def test_resolve_logs_selected_format_without_the_media_url(monkeypatch, caplog, capsys):
    clear_audio_cache()
    _caplog_info(caplog)
    secret = f"{MEDIA}&sig=SUPERSECRET&expire=9"
    seen, _download_error = _install_ytdlp(
        monkeypatch,
        lambda _ydl, _url, _download: {
            "extractor": "youtube",
            "formats": [_fmt(url=secret, format_id="140")],
            "http_headers": {"User-Agent": "djtube-test", "Cookie": "SID=COOKIESECRET"},
        },
    )

    source = resolve_audio(VIDEO_ID)
    assert source.url == secret
    assert source.headers == {"User-Agent": "djtube-test"}
    assert seen["options"]["quiet"] is True
    assert seen["options"]["logger"] is not None
    text = caplog.text
    assert f"video={VIDEO_ID}" in text
    assert "via=yt-dlp" in text
    assert "extractor=youtube" in text
    assert "format_id=140" in text
    assert "ext=m4a" in text
    assert "acodec=mp4a.40.2" in text
    assert "abr=128" in text
    assert "protocol=https" in text
    assert "content_type=audio/mp4" in text
    err = capsys.readouterr().err
    for secret_bit in ("SUPERSECRET", "COOKIESECRET", "googlevideo", "videoplayback", "sig="):
        assert secret_bit not in text
        assert secret_bit not in err

    caplog.clear()
    again = resolve_audio(VIDEO_ID)
    assert again.url == secret
    assert "via=cache" in caplog.text
    assert "content_type=audio/mp4" in caplog.text
    assert "via=yt-dlp" not in caplog.text
    assert "SUPERSECRET" not in caplog.text


def test_ytdlp_failure_logs_exit_and_hides_secrets(monkeypatch, caplog, capsys):
    clear_audio_cache()
    _caplog_info(caplog)
    message = (
        "ERROR: [youtube] abcdefghijk: Sign in to confirm you're not a bot. "
        "HTTP Error 403: Forbidden. "
        f"{MEDIA}&sig=SUPERSECRET "
        "https://consent.youtube.com/m?continue=https%3A%2F%2Fwww.youtube.com%2Fwatch%3Fv%3Dabcdefghijk%26sig%3DLEAKED&gl=JP "
        "Cookie: SID=COOKIESECRET AIzaSyDDDDDDDDDDDDDDDDDDDD"
    )

    def extract_info(ydl, _url, _download):
        ydl._download_retcode = 0
        ydl._options["logger"].error(message)
        raise ydl.Failure(message)

    _install_ytdlp(monkeypatch, extract_info)

    with pytest.raises(AudioError) as caught:
        resolve_audio(VIDEO_ID)
    assert str(caught.value) == "音源を取得できませんでした"
    assert caught.value.status == 502
    text = caplog.text
    assert f"video={VIDEO_ID}" in text
    assert "via=yt-dlp" in text
    assert "exit=1 DownloadError" in text
    assert "notes=Bot判定,同意画面,403" in text
    assert "not a bot" in text
    assert "consent.youtube.com" in text
    assert "403" in text
    err = capsys.readouterr().err
    for secret_bit in ("SUPERSECRET", "LEAKED", "COOKIESECRET", "AIzaSy", "googlevideo", "continue=", "sig="):
        assert secret_bit not in text
        assert secret_bit not in err


def test_upstream_logs_status_without_the_media_url(monkeypatch, caplog):
    _caplog_info(caplog)
    secret = f"{MEDIA}&sig=SUPERSECRET"

    class Response:
        def __init__(self, status_code, headers=None):
            self.status_code = status_code
            self.headers = headers or {}

        def close(self):
            return None

    def fake_send(_client, url, _headers):
        assert url == secret
        return Response(206, {"content-type": "audio/mp4"})

    monkeypatch.setattr("djtube.audio._send", fake_send)
    upstream = open_upstream(
        AudioSource(url=secret, content_type="audio/mp4", headers={}),
        "bytes=0-1",
        video_id=VIDEO_ID,
    )
    upstream.close()
    assert f"video={VIDEO_ID}" in caplog.text
    assert "status=206" in caplog.text
    assert "ranged=yes" in caplog.text
    assert "content_type=audio/mp4" in caplog.text
    assert "SUPERSECRET" not in caplog.text
    assert "googlevideo" not in caplog.text


def test_upstream_403_and_rejected_redirect_hide_urls(monkeypatch, caplog):
    _caplog_info(caplog)
    secret = f"{MEDIA}&sig=SUPERSECRET"
    calls = {"n": 0}

    class Response:
        def __init__(self, status_code, location=""):
            self.status_code = status_code
            self.headers = {"location": location} if location else {}

        def close(self):
            return None

    def fake_send(_client, url, _headers):
        calls["n"] += 1
        if calls["n"] == 1:
            return Response(403)
        return Response(302, "https://evil.example/secret-audio?sig=LEAKED")

    monkeypatch.setattr("djtube.audio._send", fake_send)
    with pytest.raises(AudioExpired) as expired:
        open_upstream(AudioSource(url=secret, content_type="audio/mp4", headers={}), None, video_id=VIDEO_ID)
    assert str(expired.value) == "音源を取得できませんでした"
    assert "status=403" in caplog.text
    assert "expired" in caplog.text
    assert f"video={VIDEO_ID}" in caplog.text
    assert "SUPERSECRET" not in caplog.text
    assert "googlevideo" not in caplog.text

    caplog.clear()
    with pytest.raises(AudioError) as rejected:
        open_upstream(AudioSource(url=secret, content_type="audio/mp4", headers={}), None, video_id=VIDEO_ID)
    assert str(rejected.value) == "音源を取得できませんでした"
    assert "redirect rejected" in caplog.text
    for secret_bit in ("SUPERSECRET", "LEAKED", "googlevideo", "evil.example", "sig="):
        assert secret_bit not in caplog.text


def test_upstream_http_error_hides_the_media_url(monkeypatch, caplog):
    _caplog_info(caplog)
    secret = f"{MEDIA}&sig=SUPERSECRET"

    def fake_send(_client, _url, _headers):
        raise httpx.ConnectError(secret)

    monkeypatch.setattr("djtube.audio._send", fake_send)
    with pytest.raises(AudioError) as caught:
        open_upstream(AudioSource(url=secret, content_type="audio/mp4", headers={}), None, video_id=VIDEO_ID)
    assert str(caught.value) == "音源を取得できませんでした"
    assert "ConnectError" in caplog.text
    assert f"video={VIDEO_ID}" in caplog.text
    assert "SUPERSECRET" not in caplog.text
    assert "googlevideo" not in caplog.text
