from __future__ import annotations

import logging
import sys
import types
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from djtube.app import create_app
from djtube.audio import (
    AudioError,
    AudioExpired,
    AudioSource,
    _format_summary,
    _redact,
    _ytdlp_code,
    clear_audio_cache,
    content_type_for,
    host_allowed,
    open_audio,
    open_upstream,
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

    def upstream(source, range_header, video_id):
        assert range_header == "bytes=0-1"
        assert video_id == VIDEO_ID
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


SIGNED = MEDIA + "&sig=supersecret&lsig=aaa"
NASTY = (
    "ERROR: [youtube] abcdefghijk: Sign in to confirm you're not a bot. "
    "Use --cookies-from-browser or --cookies for the authentication. "
    "See https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp for how to manually pass cookies. "
    "HTTP Error 403: Forbidden "
    f"{SIGNED} "
    "Cookie: VISITOR_INFO1_LIVE=secretcookie; SAPISID=otherscret "
    "consent.youtube.com/m?continue=1 "
    "AIzaSyAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA "
    "Before you continue to YouTube"
)
SECRETS = ("supersecret", "secretcookie", "otherscret", "AIzaSy", "videoplayback", "googlevideo")


class _Resp:
    def __init__(self, status_code, headers=None):
        self.status_code = status_code
        self.headers = headers or {}

    def close(self):
        return None


@pytest.fixture
def audio_logs():
    records = []

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    handler = Capture()
    handler.setLevel(logging.INFO)
    logger = logging.getLogger("djtube.audio")
    logger.addHandler(handler)
    try:
        yield records
    finally:
        logger.removeHandler(handler)


def _text(records) -> str:
    return "\n".join(records)


def _install_ytdlp(monkeypatch, extract_info):
    class FakeYDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def extract_info(self, url, download=False):
            return extract_info(self, url, download)

    monkeypatch.setitem(sys.modules, "yt_dlp", types.SimpleNamespace(YoutubeDL=FakeYDL))


def test_ytdlp_code_is_the_process_code():
    class YDL:
        _download_retcode = 0

    assert _ytdlp_code(YDL(), RuntimeError("failed")) == 1
    failed = YDL()
    failed._download_retcode = 2
    assert _ytdlp_code(failed, RuntimeError("failed")) == 2
    assert _ytdlp_code(YDL(), SystemExit(7)) == 7
    cancelled = type("DownloadCancelled", (Exception,), {})("stop")
    assert _ytdlp_code(YDL(), cancelled) == 101


def test_redact_keeps_failure_words_and_drops_secrets():
    cleaned = _redact(NASTY)
    assert "not a bot" in cleaned
    assert "HTTP Error 403" in cleaned
    assert "Before you continue to YouTube" in cleaned
    assert "for the authentication" in cleaned
    assert "github.com" in cleaned
    for secret in SECRETS:
        assert secret not in cleaned
    summary = _format_summary(_fmt(ext=SIGNED, format_id="140"))
    assert "format_id=140" in summary
    assert "supersecret" not in summary
    assert "videoplayback" not in summary


def test_playback_error_text_stays_on_screen():
    source = (Path(__file__).resolve().parents[1] / "djtube" / "static" / "app.js").read_text(encoding="utf-8")
    assert 'deckState.error = "音源を再生できませんでした"' in source


def test_audio_source_does_not_classify_log_text():
    source = (Path(__file__).resolve().parents[1] / "djtube" / "audio.py").read_text(encoding="utf-8")
    assert "_signal_labels" not in source
    assert "Bot判定" not in source
    assert "同意画面" not in source


def test_audio_logger_reaches_stderr():
    logger = logging.getLogger("djtube.audio")
    assert logger.level <= logging.INFO
    assert logger.propagate is False
    assert any(handler.name == "djtube-audio" for handler in logger.handlers)


def test_ytdlp_failure_logs_exit_and_hides_secrets(monkeypatch, audio_logs, capsys):
    clear_audio_cache()

    def extract_info(ydl, url, download):
        assert download is False
        assert url == watch_url(VIDEO_ID)
        assert ydl.options["quiet"] is True
        assert ydl.options["skip_download"] is True
        logger = ydl.options.get("logger")
        assert logger is not None
        logger.debug(SIGNED)
        logger.error(NASTY)
        try:
            raise OSError(f"HTTP Error 403: Forbidden {SIGNED}")
        except OSError as inner:
            err = RuntimeError("extractor failed")
            err.exc_info = (type(inner), inner, None)
            raise err

    _install_ytdlp(monkeypatch, extract_info)
    with pytest.raises(AudioError) as caught:
        resolve_audio(VIDEO_ID)
    assert str(caught.value) == "音源を取得できませんでした"
    assert caught.value.status == 502
    text = _text(audio_logs)
    assert f"video={VIDEO_ID}" in text
    assert "path=ytdlp" in text
    assert "event=start" in text
    assert "event=ended" in text
    assert "code=1" in text
    assert "not a bot" in text
    assert "HTTP Error 403" in text
    assert "Before you continue to YouTube" in text
    assert "github.com" in text
    assert "Bot判定" not in text
    assert "同意画面" not in text
    assert "signals=" not in text
    for secret in SECRETS:
        assert secret not in text
    captured = capsys.readouterr()
    for secret in SECRETS:
        assert secret not in captured.err
        assert secret not in captured.out


def test_ytdlp_success_logs_format_summary_without_the_url(monkeypatch, audio_logs):
    clear_audio_cache()
    calls = {"n": 0}

    def extract_info(ydl, url, download):
        calls["n"] += 1
        assert ydl.options.get("logger") is not None
        ydl.options["logger"].warning(f"HTTP Error 403: Forbidden {SIGNED}")
        return {"formats": [_fmt(format_id="140", url=SIGNED)], "http_headers": {"User-Agent": "djtube-test"}}

    _install_ytdlp(monkeypatch, extract_info)
    source = resolve_audio(VIDEO_ID)
    assert source.url == SIGNED
    assert source.headers["User-Agent"] == "djtube-test"
    again = resolve_audio(VIDEO_ID)
    assert again.url == source.url
    assert calls["n"] == 1
    text = _text(audio_logs)
    assert "path=ytdlp" in text
    assert "event=selected" in text
    assert "ext=m4a" in text
    assert "acodec=mp4a.40.2" in text
    assert "abr=128" in text
    assert "protocol=https" in text
    assert "mime=audio/mp4" in text
    assert "format_id=140" in text
    assert "event=message" in text
    assert "HTTP Error 403" in text
    assert "signals=" not in text
    assert "Bot判定" not in text
    assert "path=cache" in text
    assert "event=hit" in text
    for secret in SECRETS:
        assert secret not in text


def test_open_audio_logs_upstream_403_without_the_media_url(monkeypatch, audio_logs):
    clear_audio_cache()
    state = {"n": 0}

    def extract_info(ydl, url, download):
        state["n"] += 1
        return {"formats": [_fmt(url=f"{SIGNED}&n={state['n']}", format_id="140")]}

    _install_ytdlp(monkeypatch, extract_info)

    def send(client, url, headers):
        if "n=1" in url:
            return _Resp(403)
        return _Resp(206)

    monkeypatch.setattr("djtube.audio._send", send)
    opened = open_audio(VIDEO_ID, None)
    opened.close()
    assert state["n"] == 2
    text = _text(audio_logs)
    assert "path=ytdlp" in text
    assert "event=selected" in text
    assert "ext=m4a" in text
    assert "path=upstream" in text
    assert "status=403" in text
    assert "event=retry" in text
    assert "status=206" in text
    assert "signals=" not in text
    assert "Bot判定" not in text
    for secret in SECRETS:
        assert secret not in text


def test_upstream_hides_redirect_target_and_transport_url(monkeypatch, audio_logs):
    source = AudioSource(url=SIGNED, content_type="audio/mp4", headers={})

    def reject(client, url, headers):
        return _Resp(302, {"location": "https://evil.example/secret-audio?sig=supersecret"})

    monkeypatch.setattr("djtube.audio._send", reject)
    with pytest.raises(AudioError) as rejected:
        open_upstream(source, None, VIDEO_ID)
    assert str(rejected.value) == "音源を取得できませんでした"
    assert "status=302" in _text(audio_logs)
    assert "evil.example" not in _text(audio_logs)

    audio_logs.clear()

    def follow(client, url, headers):
        if url == SIGNED:
            return _Resp(302, {"location": SIGNED + "&redirect=1"})
        return _Resp(206)

    monkeypatch.setattr("djtube.audio._send", follow)
    opened = open_upstream(source, "bytes=0-1", VIDEO_ID)
    opened.close()
    assert "status=206" in _text(audio_logs)
    for secret in SECRETS:
        assert secret not in _text(audio_logs)

    audio_logs.clear()

    def boom(client, url, headers):
        raise httpx.TransportError(f"failed {url}")

    monkeypatch.setattr("djtube.audio._send", boom)
    with pytest.raises(AudioError) as failed:
        open_upstream(source, None, VIDEO_ID)
    assert str(failed.value) == "音源を取得できませんでした"
    assert "path=upstream" in _text(audio_logs)
    for secret in SECRETS:
        assert secret not in _text(audio_logs)


def test_audio_route_reports_resolve_failure(monkeypatch):
    def fake_open(_video_id, _range_header):
        raise AudioError("音源を取得できませんでした")

    monkeypatch.setattr("djtube.app.open_audio", fake_open)
    client = TestClient(create_app())
    response = client.get(f"/api/audio/{VIDEO_ID}")
    assert response.status_code == 502
    assert response.json()["detail"] == "音源を取得できませんでした"
