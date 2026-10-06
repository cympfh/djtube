"""Video half of a live stream. Thumbnails in these tests are generated."""

from __future__ import annotations

import io
import os
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from starlette.websockets import WebSocketDisconnect

from djtube.app import create_app
from djtube.compose import VIDEO_FPS, VIDEO_HEIGHT, VIDEO_WIDTH, Composer
from djtube.live import CODE_REPLACED, CODE_TOO_BIG, MAX_TEXT, NOW_IGNORED, LiveHub, parse_now
from djtube.thumbs import (
    MAX_BYTES,
    MAX_MISSING,
    MAX_QUEUED,
    ThumbCache,
    _dimensions_ok,
    _http_get,
    _open_jpeg,
    _read_limited,
    fetch_thumb_dir,
    load_youtube_thumb,
    thumb_cache,
    thumb_urls,
)
from djtube.video import (
    MAX_VIDEO_ENCODERS,
    MAX_VIDEO_PER_IP,
    VIDEO_BITRATE,
    VIDEO_MIME,
    FfmpegRelay,
    TsSyncBuffer,
    ffmpeg_command,
    pace_frames,
    packet_pid_and_nals,
)
from djtube.webm import cluster_timecode, rebase_cluster
from tests.test_live import _Audio, _id_token, _with_token, _ws
from tests.test_webm import cluster_known, document

VIDEO = "abcdefghijk"
OTHER = "bbbbbbbbbbb"


def _jpeg(size: tuple[int, int], color: tuple[int, int, int] = (20, 40, 60)) -> bytes:
    image = Image.new("RGB", size, color)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG")
    return buffer.getvalue()


def _solid(color: tuple[int, int, int], size: tuple[int, int] = (32, 24)) -> Image.Image:
    return Image.new("RGB", size, color)


def _publish_ready(publisher, cluster: bytes | None = None) -> tuple[bytes, bytes]:
    body = cluster if cluster is not None else cluster_known(0, b"wave")
    head, _data = document([body])
    publisher.receive_json()
    publisher.send_json({"type": "mime", "mime": "audio/webm"})
    publisher.send_bytes(head + body)
    return head, body


def _now(video: str = VIDEO, gain: float = 1) -> dict:
    return {"type": "now", "decks": [{"video": video, "gain": gain}]}


def test_rebase_cluster_keeps_the_timecode_width():
    cluster = cluster_known(5000, b"wave")
    updated = rebase_cluster(cluster, 4800)
    assert cluster_timecode(updated) == 200
    assert len(updated) == len(cluster)
    assert rebase_cluster(cluster, 0) is cluster


def test_parse_now_rejects_a_bad_id_a_bool_gain_and_a_third_deck():
    assert parse_now(_now()) == ((VIDEO, 1.0),)
    assert parse_now({"type": "now", "decks": []}) == ()
    assert parse_now({"type": "mime", "mime": "audio/webm"}) is None
    assert parse_now({"type": "now", "decks": [{"video": "short", "gain": 1}]}) is NOW_IGNORED
    assert parse_now({"type": "now", "decks": [{"video": VIDEO, "gain": True}]}) is NOW_IGNORED
    assert parse_now({"type": "now", "decks": [{"video": VIDEO, "gain": 1.1}]}) is NOW_IGNORED
    assert parse_now({"type": "now", "decks": [{"video": VIDEO, "gain": 1}] * 3}) is NOW_IGNORED
    # NaN passes a range check. Infinity and a huge int must not raise.
    assert parse_now({"type": "now", "decks": [{"video": VIDEO, "gain": float("nan")}]}) is NOW_IGNORED
    assert parse_now({"type": "now", "decks": [{"video": VIDEO, "gain": float("inf")}]}) is NOW_IGNORED
    assert parse_now({"type": "now", "decks": [{"video": VIDEO, "gain": 10**400}]}) is NOW_IGNORED


def test_now_messages_are_limited_and_do_not_replace_audio():
    hub = LiveHub(init_timeout=2, idle_timeout=5)
    cluster = cluster_known(1000, b"wave")
    with TestClient(create_app(live=hub)) as client:
        with _ws(client, "/api/live/publish?id=NOWW") as publisher:
            head, body = _publish_ready(publisher, cluster)
            with _Audio(client, "/stream/NOWW") as listener:
                assert listener.read() == head
                assert listener.read() == body
                for _ in range(4):
                    publisher.send_json(_now(gain=0.5))
                assert hub.get("NOWW").decks == ((VIDEO, 0.5),)
                publisher.send_json(_now(gain=0.25))
                publisher.send_json(_now(gain=0.1))
                deadline = time.monotonic() + 1.5
                while time.monotonic() < deadline and hub.get("NOWW").decks != ((VIDEO, 0.1),):
                    time.sleep(0.02)
                assert hub.get("NOWW").decks == ((VIDEO, 0.1),)
                tail = cluster_known(1400, b"tail")
                publisher.send_bytes(tail)
                assert listener.read() == tail

    hub = LiveHub(init_timeout=2, idle_timeout=5)
    with TestClient(create_app(live=hub)) as client:
        with _ws(client, "/api/live/publish?id=NOWA") as publisher:
            head, body = _publish_ready(publisher, cluster)
            with _Audio(client, "/stream/NOWA") as listener:
                assert listener.headers["content-type"].startswith("audio/webm")
                assert listener.read() == head
                assert listener.read() == body
                publisher.send_json(_now())
                nxt = cluster_known(1200, b"more")
                publisher.send_bytes(nxt)
                assert listener.read() == nxt
                assert hub.get("NOWA").decks == ((VIDEO, 1.0),)
                assert hub.get("NOWA").relay is None


def test_a_bad_now_is_dropped_and_an_oversized_text_closes_the_publisher():
    hub = LiveHub(init_timeout=2, idle_timeout=5)
    cluster = cluster_known(0, b"wave")
    huge_gain = "1" + ("0" * 400)
    with TestClient(create_app(live=hub)) as client:
        with _ws(client, "/api/live/publish?id=BADN") as publisher:
            head, body = _publish_ready(publisher, cluster)
            publisher.send_json({"type": "now", "decks": [{"video": "short", "gain": 1}]})
            publisher.send_text('{"type":"now","decks":[{"video":"%s","gain":%s}]}' % (VIDEO, huge_gain))
            assert hub.get("BADN").decks == ()
            with _Audio(client, "/stream/BADN") as listener:
                assert listener.read() == head
                assert listener.read() == body
                nxt = cluster_known(1200, b"more")
                publisher.send_bytes(nxt)
                assert listener.read() == nxt
            assert hub.get("BADN") is not None

    hub = LiveHub(init_timeout=2, idle_timeout=5)
    cluster = cluster_known(0, b"wave")
    with TestClient(create_app(live=hub)) as client:
        with _ws(client, "/api/live/publish?id=BIGT") as publisher:
            _publish_ready(publisher, cluster)
            publisher.send_text("x" * (MAX_TEXT + 1))
            with pytest.raises(WebSocketDisconnect) as huge:
                publisher.receive_json()
            assert huge.value.code == CODE_TOO_BIG


def test_now_does_not_refresh_the_idle_timer():
    cluster = cluster_known(0, b"wave")
    hub = LiveHub(init_timeout=2, idle_timeout=0.4)
    with TestClient(create_app(live=hub)) as client:
        with _ws(client, "/api/live/publish?id=IDLE") as publisher:
            publisher.receive_json()
            publisher.send_json({"type": "mime", "mime": "audio/webm"})
            head, _data = document([cluster])
            publisher.send_bytes(head + cluster)
            sent = time.monotonic()
            time.sleep(0.25)
            publisher.send_json(_now())
            with pytest.raises(WebSocketDisconnect) as quiet:
                publisher.receive_json()
            assert quiet.value.code == 4408
            assert time.monotonic() - sent < 0.58


def test_layout_for_zero_one_and_two_pictures():
    size = (160, 90)
    red = _solid((220, 20, 20))
    blue = _solid((20, 20, 220))
    images = {VIDEO: red, OTHER: blue}
    center = (size[0] // 2, size[1] // 2)

    empty = Composer(size, fade=0).frame([], {}, now=0)
    assert empty.getpixel((0, 0)) == (0, 0, 0)
    assert empty.getpixel((size[0] - 1, size[1] - 1)) == (0, 0, 0)
    assert any(color != (0, 0, 0) for _count, color in empty.getcolors(maxcolors=empty.width * empty.height))

    one = Composer(size, fade=0).frame([(VIDEO, 1.0)], images, now=0)
    mid = one.getpixel(center)
    corner = one.getpixel((2, 2))
    assert mid[0] > corner[0] + 40
    assert mid[0] > mid[1] + 40

    both = Composer(size, fade=0).frame([(VIDEO, 0.5), (OTHER, 0.5)], images, now=0)
    mixed = both.getpixel(center)
    assert mixed[0] > 80 and mixed[2] > 80
    assert abs(mixed[0] - mixed[2]) < 40

    biased = Composer(size, fade=0).frame([(VIDEO, 0.85), (OTHER, 0.15)], images, now=0).getpixel(center)
    assert biased[0] > biased[2]
    assert biased[0] > mixed[0]

    fading = Composer(size, fade=0.4)
    fading.frame([(VIDEO, 1.0)], images, now=0)
    opened = fading.frame([(OTHER, 1.0)], images, now=0)
    assert opened.getpixel(center)[0] > opened.getpixel(center)[2]
    halfway = fading.frame([(OTHER, 1.0)], images, now=0.2)
    half = halfway.getpixel(center)
    assert abs(half[0] - half[2]) < 50
    done = fading.frame([(OTHER, 1.0)], images, now=0.5)
    assert done.getpixel(center)[2] > done.getpixel(center)[0]


def test_thumbnails_come_only_from_ytimg_and_skip_placeholders():
    assert thumb_urls(VIDEO) == (
        f"https://i.ytimg.com/vi/{VIDEO}/maxresdefault.jpg",
        f"https://i.ytimg.com/vi/{VIDEO}/hqdefault.jpg",
    )
    with pytest.raises(ValueError):
        thumb_urls("short")

    calls: list[str] = []

    def get(url: str) -> tuple[int, bytes]:
        calls.append(url)
        return 200, b""

    assert load_youtube_thumb("nope", get) is None
    assert calls == []

    good = _jpeg((480, 360), (8, 8, 8))
    seen: list[str] = []

    def fallback(url: str) -> tuple[int, bytes]:
        seen.append(url)
        if url.endswith("maxresdefault.jpg"):
            return 404, b"missing"
        return 200, good

    image = load_youtube_thumb(VIDEO, fallback)
    assert image is not None and image.size == (480, 360)
    assert seen == list(thumb_urls(VIDEO))
    assert all(url.startswith("https://i.ytimg.com/vi/") for url in seen)

    def tiny_then_real(url: str) -> tuple[int, bytes]:
        if url.endswith("maxresdefault.jpg"):
            return 200, _jpeg((16, 16))
        return 200, good

    assert load_youtube_thumb(VIDEO, tiny_then_real).size == (480, 360)

    cache = ThumbCache(lambda _video: None, negative_ttl=100, clock=lambda: 0)
    assert cache.warm(VIDEO) is None
    cache.want(VIDEO)
    assert cache.get(VIDEO) is None

    made = Image.new("RGB", (320, 180), (1, 2, 3))
    order = [VIDEO, OTHER]

    def fetch(video_id: str) -> Image.Image:
        assert video_id == order.pop(0)
        return made

    small = ThumbCache(fetch, max_items=1)
    assert small.warm(VIDEO) is made
    assert small.warm(OTHER) is made
    assert small.get(VIDEO) is None
    assert small.get(OTHER) is made


def test_sync_buffer_starts_at_the_video_sps():
    def packet(pid: int, payload: bytes) -> bytes:
        raw = bytearray(188)
        raw[0] = 0x47
        raw[1] = 0x40 | ((pid >> 8) & 0x1F)
        raw[2] = pid & 0xFF
        raw[3] = 0x10
        raw[4 : 4 + len(payload)] = payload
        return bytes(raw)

    video = packet(0x100, b"\x00\x00\x00\x01\x67")
    other = packet(0x101, b"\x00\x00\x00\x01\x09")
    buf = TsSyncBuffer()
    buf.append(b"\x00" * 20 + other + video + other)
    snap = buf.snapshot()
    assert snap.startswith(video)
    assert other in snap
    pid, types = packet_pid_and_nals(video)
    assert pid == 0x100
    assert 7 in types
    later = packet(0x100, b"\x00\x00\x00\x01\x67\x22")
    buf.append(later)
    assert buf.snapshot().startswith(later)


def test_ffmpeg_command_encodes_aac_and_one_x264_thread():
    command = ffmpeg_command("ffmpeg", VIDEO_WIDTH, VIDEO_HEIGHT, VIDEO_FPS, 7)
    assert VIDEO_WIDTH == 1280 and VIDEO_HEIGHT == 720 and VIDEO_FPS == 5
    assert MAX_VIDEO_ENCODERS == 3
    assert MAX_VIDEO_PER_IP == 1
    assert VIDEO_MIME == "video/mp2t"
    assert VIDEO_BITRATE == 350_000
    assert command[command.index("-c:a") + 1] == "aac"
    assert command[command.index("-profile:a") + 1] == "aac_low"
    assert command[command.index("-b:a") + 1] == "128k"
    assert command[command.index("-ar") + 1] == "48000"
    assert command[command.index("-threads") + 1] == "1"
    assert "copy" not in command
    assert "mpegts" in command
    assert "pipe:7" in command
    assert "-nostdin" not in command
    copies, nxt = pace_frames(0.0, 9.6, 0.2, 1000)
    assert copies == 1 + int(9.6 / 0.2)
    assert nxt == copies * 0.2
    assert nxt != 9.6


_FAKE_FFMPEG = """#!/usr/bin/env python3
import os
import sys
import threading

def audio_fd():
    for arg in sys.argv[1:]:
        if arg.startswith("pipe:"):
            number = int(arg.split(":", 1)[1])
            if number not in (0, 1):
                return number
    return None

packet = bytearray(188)
packet[0] = 0x47
packet[1] = 0x41
packet[2] = 0x00
packet[3] = 0x10
packet[4:9] = b"\\x00\\x00\\x00\\x01\\x67"
sys.stdout.buffer.write(bytes(packet) * 8)
sys.stdout.buffer.flush()

def drain(handle):
    try:
        while handle.read(4096):
            pass
    except Exception:
        return

threads = [threading.Thread(target=drain, args=(sys.stdin.buffer,))]
fd = audio_fd()
if fd is not None:
    threads.append(threading.Thread(target=drain, args=(os.fdopen(fd, "rb"),)))
for thread in threads:
    thread.daemon = True
    thread.start()
for thread in threads:
    thread.join()
"""


def _fake_ffmpeg(tmp_path: Path) -> str:
    path = tmp_path / "ffmpeg"
    path.write_text(_FAKE_FFMPEG)
    path.chmod(0o755)
    return str(path)


def _hub(tmp_path: Path, **kwargs) -> LiveHub:
    options = {
        "ffmpeg": _fake_ffmpeg(tmp_path),
        "video_size": (64, 36),
        "video_fps": 5,
        "video_linger": 0.2,
        "output_stall": 30.0,
        "thumbs": ThumbCache(lambda _video: None),
    }
    options.update(kwargs)
    return LiveHub(**options)


def test_encoder_starts_for_a_viewer_and_stops_when_the_last_one_leaves(tmp_path: Path):
    hub = _hub(tmp_path)
    with TestClient(create_app(live=hub)) as client:
        with _ws(client, "/api/live/publish?id=ENCA") as publisher:
            _publish_ready(publisher)
            probed = client.head("/stream/ENCA?thumbnail=1")
            assert probed.status_code == 200
            assert probed.headers["content-type"] == VIDEO_MIME
            assert hub.get("ENCA").relay is None
            plain = client.head("/stream/ENCA")
            assert plain.headers["content-type"].startswith("audio/webm")
            with _Audio(client, "/stream/ENCA", query="thumbnail=1") as first:
                assert first.status_code == 200
                assert first.headers["content-type"] == VIDEO_MIME
                assert first.read()[:1] == b"\x47"
                relay = hub.get("ENCA").relay
                assert relay is not None and relay.occupies()
                with _Audio(client, "/stream/ENCA", query="thumbnail=1") as second:
                    assert second.read()[:1] == b"\x47"
                    assert relay.listener_count() == 2
            time.sleep(0.15)
            assert relay.occupies()
            time.sleep(0.5)
            assert relay.occupies() is False
            with _Audio(client, "/stream/ENCA") as audio:
                assert audio.headers["content-type"].startswith("audio/webm")
                assert audio.read()[:4] == b"\x1a\x45\xdf\xa3"
            assert hub.get("ENCA").relay.occupies() is False


def test_an_encoder_that_ignores_sigterm_is_still_stopped(tmp_path: Path):
    script = tmp_path / "ffmpeg"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import os, signal, sys, threading, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "packet = bytearray(188)\n"
        "packet[0] = 0x47\n"
        "packet[1] = 0x41\n"
        "packet[3] = 0x10\n"
        "packet[4:9] = b'\\x00\\x00\\x00\\x01\\x67'\n"
        "sys.stdout.buffer.write(bytes(packet) * 4)\n"
        "sys.stdout.buffer.flush()\n"
        "def drain(handle):\n"
        "    try:\n"
        "        while handle.read(4096):\n"
        "            pass\n"
        "    except Exception:\n"
        "        return\n"
        "threading.Thread(target=drain, args=(sys.stdin.buffer,), daemon=True).start()\n"
        "for arg in sys.argv[1:]:\n"
        "    if arg.startswith('pipe:'):\n"
        "        number = int(arg.split(':', 1)[1])\n"
        "        if number not in (0, 1):\n"
        "            threading.Thread(target=drain, args=(os.fdopen(number, 'rb'),), daemon=True).start()\n"
        "while True:\n"
        "    time.sleep(0.2)\n"
    )
    script.chmod(0o755)
    hub = _hub(tmp_path, ffmpeg=str(script), video_linger=0.2)
    with TestClient(create_app(live=hub)) as client:
        with _ws(client, "/api/live/publish?id=KILL") as publisher:
            _publish_ready(publisher)
            with _Audio(client, "/stream/KILL", query="thumbnail=1") as video:
                assert video.read()[:1] == b"\x47"
                pid = hub.get("KILL").relay._proc.pid
            time.sleep(0.8)
            assert hub.get("KILL").relay.occupies() is False
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_a_full_encoder_cap_returns_429_and_audio_still_plays(tmp_path: Path):
    hub = _hub(tmp_path, max_video_encoders=1)
    with TestClient(create_app(live=hub)) as client:
        with _ws(client, "/api/live/publish?id=CAPA") as first:
            _publish_ready(first)
            with _ws(client, "/api/live/publish?id=CAPB") as second:
                _publish_ready(second)
                with _Audio(client, "/stream/CAPA", query="thumbnail=1") as video:
                    assert video.read()[:1] == b"\x47"
                    denied = client.get("/stream/CAPB?thumbnail=1")
                    assert denied.status_code == 429
                    denied_head = client.head("/stream/CAPB?thumbnail=1")
                    assert denied_head.status_code == 429
                    assert hub.get("CAPB").relay is None or hub.get("CAPB").relay.occupies() is False
                    audio = client.head("/stream/CAPB")
                    assert audio.status_code == 200
                    assert audio.headers["content-type"].startswith("audio/webm")


def test_a_video_listener_counts_toward_the_listener_cap(tmp_path: Path):
    hub = _hub(tmp_path, max_listeners=1)
    with TestClient(create_app(live=hub)) as client:
        with _ws(client, "/api/live/publish?id=LIMT") as publisher:
            _publish_ready(publisher)
            with _Audio(client, "/stream/LIMT", query="thumbnail=1") as video:
                assert video.read()[:1] == b"\x47"
                denied = client.get("/stream/LIMT")
                assert denied.status_code == 429


def test_real_ffmpeg_muxes_h264_and_aac(tmp_path: Path):
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg is required")
    from djtube.live_tone import render_webm

    webm = render_webm([(440, 0.6)])
    hub = LiveHub(
        video_size=(64, 36),
        video_fps=5,
        video_linger=0.2,
        thumbs=ThumbCache(lambda _video: _solid((30, 90, 40), (64, 36))),
    )
    with TestClient(create_app(live=hub)) as client:
        with _ws(client, "/api/live/publish?id=REAL") as publisher:
            publisher.receive_json()
            publisher.send_json({"type": "mime", "mime": "audio/webm"})
            publisher.send_json(_now())
            publisher.send_bytes(webm)
            with _Audio(client, "/stream/REAL", query="thumbnail=1") as video:
                assert video.headers["content-type"] == VIDEO_MIME
                blob = video.read()
                assert blob is not None and blob[:1] == b"\x47"
                assert hub.get("REAL").relay.occupies()
    path = tmp_path / "joined.ts"
    path.write_bytes(blob)
    probed = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_name,codec_type",
            "-of",
            "csv=p=0",
            str(path),
        ],
        capture_output=True,
        check=False,
        text=True,
    )
    assert probed.returncode == 0, probed.stderr[-400:]
    kinds = probed.stdout
    assert "h264" in kinds
    assert "aac" in kinds
    assert "opus" not in kinds


def test_pace_frames_duplicates_instead_of_resetting_the_clock():
    copies, nxt = pace_frames(10.0, 12.0, 0.2, 4)
    assert copies == 5
    assert nxt == 10.0 + copies * 0.2
    one, nxt = pace_frames(10.0, 10.0, 0.2, 4)
    assert one == 1
    assert nxt == 10.2


def test_thumbnail_fetch_does_not_follow_redirects_and_stops_reading(monkeypatch):
    seen: dict[str, object] = {}

    class _Body:
        status_code = 200

        def iter_bytes(self):
            yield b"x" * (MAX_BYTES + 8)

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> bool:
            return False

    def stream(method: str, url: str, **kwargs: object) -> _Body:
        seen["method"] = method
        seen["follow"] = kwargs.get("follow_redirects")
        return _Body()

    monkeypatch.setattr("djtube.thumbs.httpx.stream", stream)
    status, body = _http_get(f"https://i.ytimg.com/vi/{VIDEO}/hqdefault.jpg")
    assert seen["follow"] is False
    assert status == 200 and body == b""
    assert _read_limited([b"ab", b"cd"], 3) is None
    assert _read_limited([b"ab", b"c"], 3) == b"abc"
    assert _dimensions_ok(9000, 16, minimum=False) is False

    loaded: list[tuple[int, int]] = []
    real_load = Image.Image.load
    real_open = Image.open

    def spy_load(self, *args, **kwargs):
        loaded.append((self.width, self.height))
        return real_load(self, *args, **kwargs)

    def open_huge(fp, *args, **kwargs):
        image = real_open(fp, *args, **kwargs)
        image._size = (9000, 9000)
        return image

    monkeypatch.setattr(Image.Image, "load", spy_load)
    monkeypatch.setattr(Image, "open", open_huge)
    assert _open_jpeg(_jpeg((320, 180))) is None
    assert all(width <= 4096 and height <= 4096 for width, height in loaded)


def test_misses_and_fetch_jobs_are_capped():
    alphabet = "abcdefghijklmnopqrstuvwxyz"

    def make_id(number: int) -> str:
        chars = []
        for _ in range(11):
            chars.append(alphabet[number % 26])
            number //= 26
        return "".join(chars)

    cache = ThumbCache(lambda _video: None)
    for number in range(MAX_MISSING + 20):
        assert cache.warm(make_id(number)) is None
    assert len(cache._missing) <= MAX_MISSING

    started = threading.Event()
    release = threading.Event()

    def block(_video_id: str):
        started.set()
        release.wait(2)
        return None

    limited = ThumbCache(block)
    limited.want(make_id(0))
    assert started.wait(1)
    for number in range(1, MAX_QUEUED + 40):
        limited.want(make_id(number))
    assert limited._jobs.qsize() <= MAX_QUEUED
    release.set()


def test_the_audio_queue_drops_oldest_instead_of_blocking():
    relay = FfmpegRelay("AUDQ", lambda: (), ThumbCache(lambda _video: None), audio_queue_max=2)

    def pump() -> None:
        for _ in range(30):
            relay._enqueue(b"cluster")

    worker = threading.Thread(target=pump)
    worker.start()
    worker.join(1)
    assert worker.is_alive() is False
    assert relay._audio_q.qsize() <= 2


def test_a_dead_encoder_ends_the_listener_and_is_reaped(tmp_path: Path):
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg is required")
    from djtube.live_tone import render_webm

    webm = render_webm([(440, 3.0)])
    hub = LiveHub(video_size=(64, 36), video_fps=5, video_linger=5, restart_backoff=0.2)
    with TestClient(create_app(live=hub)) as client:
        with _ws(client, "/api/live/publish?id=DEAD") as publisher:
            publisher.receive_json()
            publisher.send_json({"type": "mime", "mime": "audio/webm"})
            with _Audio(client, "/stream/DEAD", query="thumbnail=1") as video:
                publisher.send_bytes(webm)
                assert video.read()[:1] == b"\x47"
                relay = hub.get("DEAD").relay
                pid = relay._proc.pid
                os.kill(pid, signal.SIGKILL)
                deadline = time.monotonic() + 4
                while time.monotonic() < deadline:
                    if not video.read():
                        break
                else:
                    raise AssertionError("video listener did not end")
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and relay.occupies():
                time.sleep(0.05)
            assert relay.occupies() is False
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.05)
            else:
                raise AssertionError("encoder was not reaped")
            while time.monotonic() < relay._backoff_until and time.monotonic() < deadline + 2:
                time.sleep(0.02)
            with _Audio(client, "/stream/DEAD", query="thumbnail=1") as again:
                again_deadline = time.monotonic() + 4
                fresh = b""
                while time.monotonic() < again_deadline and not fresh:
                    piece = again.read()
                    if not piece:
                        break
                    fresh += piece
                assert fresh[:1] == b"\x47"


def test_a_silent_encoder_is_dropped(tmp_path: Path):
    script = tmp_path / "ffmpeg"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, time\n"
        "packet = bytearray(188)\n"
        "packet[0] = 0x47\n"
        "packet[1] = 0x41\n"
        "packet[3] = 0x10\n"
        "packet[4:9] = b'\\x00\\x00\\x00\\x01\\x67'\n"
        "sys.stdout.buffer.write(bytes(packet) * 4)\n"
        "sys.stdout.buffer.flush()\n"
        "time.sleep(5)\n"
    )
    script.chmod(0o755)
    hub = _hub(tmp_path, ffmpeg=str(script), output_stall=0.4, video_linger=2)
    with TestClient(create_app(live=hub)) as client:
        with _ws(client, "/api/live/publish?id=STAL") as publisher:
            _publish_ready(publisher)
            with _Audio(client, "/stream/STAL", query="thumbnail=1") as video:
                started = time.monotonic()
                while video.read():
                    if time.monotonic() - started > 2:
                        raise AssertionError("silent encoder was left running")
                assert time.monotonic() - started < 1.5
            relay = hub.get("STAL").relay
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and relay.occupies():
                time.sleep(0.05)
            assert relay.occupies() is False


def test_ending_the_publisher_reaps_the_encoder(tmp_path: Path):
    hub = _hub(tmp_path, video_linger=5)
    with TestClient(create_app(live=hub)) as client:
        publisher_cm = _ws(client, "/api/live/publish?id=ENDP")
        publisher = publisher_cm.__enter__()
        video = None
        relay = None
        pid = None
        try:
            _publish_ready(publisher)
            video = _Audio(client, "/stream/ENDP", query="thumbnail=1")
            video.__enter__()
            assert video.read()[:1] == b"\x47"
            relay = hub.get("ENDP").relay
            pid = relay._proc.pid
        finally:
            publisher_cm.__exit__(None, None, None)
        assert relay is not None and pid is not None
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and relay.occupies():
            time.sleep(0.05)
        assert relay.occupies() is False
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        if video is not None:
            video.__exit__(None, None, None)


def test_one_source_cannot_hold_every_encoder(tmp_path: Path):
    hub = _hub(tmp_path, max_video_encoders=3)
    with TestClient(create_app(live=hub)) as client:
        with _ws(client, "/api/live/publish?id=IPAA") as first:
            _publish_ready(first)
            with _ws(client, "/api/live/publish?id=IPAB") as second:
                _publish_ready(second)
                with _Audio(client, "/stream/IPAA", query="thumbnail=1", headers={"x-real-ip": "203.0.113.9"}) as video:
                    assert video.read()[:1] == b"\x47"
                    denied = client.head("/stream/IPAB?thumbnail=1", headers={"x-real-ip": "203.0.113.9"})
                    assert denied.status_code == 429
                    other = client.head("/stream/IPAB?thumbnail=1", headers={"x-real-ip": "203.0.113.10"})
                    assert other.status_code == 200
                    assert other.headers["content-type"] == VIDEO_MIME


def test_reclaim_ends_video_and_old_cleanup_keeps_the_new_stream(tmp_path: Path):
    """A replaced publisher must end its picture and ffmpeg.

    The old socket's ``_end`` runs after the new generation exists. It must
    not pop that stream or reap the ffmpeg the new publisher started.
    """

    hub = _hub(tmp_path, video_linger=5)
    cluster = cluster_known(0, b"wave")
    head, _data = document([cluster])
    with TestClient(create_app(live=hub)) as client:
        with _ws(client, "/api/live/publish?id=RCLM") as old:
            token = _id_token(old.receive_json(), "RCLM")
            old.send_json({"type": "mime", "mime": "audio/webm"})
            old.send_bytes(head + cluster)
            with _Audio(client, "/stream/RCLM", query="thumbnail=1") as video:
                assert video.read()[:1] == b"\x47"
                relay = hub.get("RCLM").relay
                assert relay is not None and relay.occupies()
                pid = relay._proc.pid
                generation = hub.get("RCLM").generation
                with _ws(client, _with_token("RCLM", token), seq=2) as new:
                    assert _id_token(new.receive_json(), "RCLM") == token
                    deadline = time.monotonic() + 2
                    ended = False
                    while time.monotonic() < deadline:
                        if video.read() is None:
                            ended = True
                            break
                    assert ended
                    with pytest.raises(WebSocketDisconnect) as replaced:
                        old.receive_json()
                    assert replaced.value.code == CODE_REPLACED
                    deadline = time.monotonic() + 3
                    while time.monotonic() < deadline and relay.occupies():
                        time.sleep(0.05)
                    assert relay.occupies() is False
                    deadline = time.monotonic() + 2
                    while time.monotonic() < deadline:
                        try:
                            os.kill(pid, 0)
                        except ProcessLookupError:
                            break
                        time.sleep(0.05)
                    else:
                        raise AssertionError("old encoder was not reaped")
                    current = hub.get("RCLM")
                    assert current is not None
                    assert current.generation == generation + 1
                    assert hub._end(current, generation) is False
                    assert hub.get("RCLM") is current
                    new.send_json({"type": "mime", "mime": "audio/webm"})
                    new.send_bytes(head + cluster)
                    with _Audio(client, "/stream/RCLM", query="thumbnail=1") as again:
                        assert again.read()[:1] == b"\x47"
                        assert hub.get("RCLM") is current
                        fresh = hub.get("RCLM").relay
                        assert fresh is not None and fresh is not relay and fresh.occupies()


def test_a_thumb_dir_reads_local_files_and_not_a_path_outside_it(tmp_path: Path, monkeypatch):
    root = tmp_path / "thumbs"
    root.mkdir()
    Image.new("RGB", (320, 180), (200, 10, 10)).save(root / f"{VIDEO}.png")
    secret = tmp_path / "secret.png"
    Image.new("RGB", (320, 180), (1, 2, 3)).save(secret)
    (root / f"{OTHER}.png").symlink_to(secret)

    fetch = fetch_thumb_dir(root)
    image = fetch(VIDEO)
    assert image is not None and image.getpixel((0, 0)) == (200, 10, 10)
    assert fetch(OTHER) is None
    assert fetch("../secret") is None
    assert fetch("short") is None

    monkeypatch.delenv("DJTUBE_THUMB_DIR", raising=False)
    assert thumb_cache()._fetch is load_youtube_thumb
    monkeypatch.setenv("DJTUBE_THUMB_DIR", str(root))
    cached = thumb_cache()
    loaded = cached.warm(VIDEO)
    assert loaded is not None and loaded.getpixel((0, 0)) == (200, 10, 10)
    assert cached.warm(OTHER) is None


def _try_center(blob: bytes, tmp_path: Path) -> tuple[int, int, int] | None:
    if not blob.startswith(b"\x47"):
        return None
    ts_path = tmp_path / "sample.ts"
    png_path = tmp_path / "sample.png"
    ts_path.write_bytes(blob)
    if png_path.exists():
        png_path.unlink()
    done = subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(ts_path), "-update", "1", str(png_path)],
        capture_output=True,
        check=False,
    )
    if done.returncode != 0 or not png_path.is_file():
        return None
    image = Image.open(png_path)
    pixel = image.getpixel((image.width // 2, image.height // 2))
    if not isinstance(pixel, tuple):
        return None
    return pixel[0], pixel[1], pixel[2]


def test_now_changes_pixels_of_a_frame_taken_from_the_encoded_ts(tmp_path: Path):
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg is required")
    from djtube.live_tone import render_webm

    red = Image.new("RGB", (320, 180), (230, 20, 20))
    blue = Image.new("RGB", (320, 180), (20, 20, 230))
    cache = ThumbCache(lambda video_id: {VIDEO: red, OTHER: blue}.get(video_id))
    hub = LiveHub(video_size=(160, 90), video_fps=5, video_linger=0.2, audio_queue_max=400, thumbs=cache)
    webm = render_webm([(440, 20.0)])

    def matches(center: tuple[int, int, int] | None, kind: str) -> bool:
        if center is None:
            return False
        red_ch, _green, blue_ch = center
        if kind == "zero":
            return red_ch < 80 and abs(red_ch - blue_ch) < 30
        if kind == "one":
            return red_ch > blue_ch + 80
        if kind == "even":
            return red_ch > 60 and blue_ch > 60 and abs(red_ch - blue_ch) < 50
        return red_ch > blue_ch and red_ch > 150 and blue_ch < 100

    with TestClient(create_app(live=hub)) as client:
        with _ws(client, "/api/live/publish?id=PIXL") as publisher:
            publisher.receive_json()
            publisher.send_json({"type": "mime", "mime": "audio/webm"})
            # Open the listener before the audio. Clusters that arrive earlier
            # are not queued for the encoder, and it then stops writing.
            with _Audio(client, "/stream/PIXL", query="thumbnail=1") as video:
                publisher.send_bytes(webm)
                blob = b""
                seen: dict[str, tuple[int, int, int]] = {}

                def wait_until(kind: str, timeout: float) -> None:
                    nonlocal blob
                    deadline = time.monotonic() + timeout
                    checked = 0
                    while time.monotonic() < deadline:
                        piece = video.read()
                        if not piece:
                            break
                        blob += piece
                        if len(blob) - checked < 4000 and time.monotonic() + 0.5 < deadline:
                            continue
                        checked = len(blob)
                        center = _try_center(blob, tmp_path)
                        if matches(center, kind):
                            assert center is not None
                            seen[kind] = center
                            return
                    center = _try_center(blob, tmp_path)
                    raise AssertionError(f"{kind} frame was {center}")

                wait_until("zero", 6)
                publisher.send_json(_now())
                wait_until("one", 6)
                publisher.send_json(
                    {"type": "now", "decks": [{"video": VIDEO, "gain": 0.5}, {"video": OTHER, "gain": 0.5}]}
                )
                wait_until("even", 6)
                publisher.send_json(
                    {"type": "now", "decks": [{"video": VIDEO, "gain": 0.85}, {"video": OTHER, "gain": 0.15}]}
                )
                wait_until("bias", 6)
    zero, one, even, bias = (seen[name] for name in ("zero", "one", "even", "bias"))
    assert zero[0] < 80 and abs(zero[0] - zero[2]) < 30
    assert one[0] > one[2] + 80
    assert even[0] > 60 and even[2] > 60 and abs(even[0] - even[2]) < 50
    assert bias[0] > bias[2]
    assert bias[0] > even[0]
    assert bias[2] < even[2]
