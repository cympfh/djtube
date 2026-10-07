"""Video half of a live stream. Thumbnails in these tests are generated."""

from __future__ import annotations

import array
import asyncio
import contextlib
import io
import math
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from starlette.websockets import WebSocketDisconnect

from djtube.app import create_app
from djtube.compose import VIDEO_FPS, VIDEO_HEIGHT, VIDEO_WIDTH, Composer, deck_card
from djtube.live import CODE_REPLACED, CODE_TOO_BIG, MAX_TEXT, NOW_IGNORED, LiveHub, parse_now
from djtube.thumbs import (
    MAX_BYTES,
    MAX_EDGE,
    MAX_MISSING,
    MAX_PIXELS,
    MAX_QUEUED,
    ThumbCache,
    _crop_letterbox,
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
    VIDEO_BUFSIZE,
    VIDEO_MAXRATE,
    VIDEO_MIME,
    _SYNC_PACKETS,
    FfmpegRelay,
    TsSyncBuffer,
    _Mux,
    ffmpeg_command,
    pace_frames,
    packet_pid_and_nals,
)
from djtube.webm import cluster_timecode, rebase_cluster, split_webm
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
    colors = empty.getcolors(maxcolors=empty.width * empty.height)
    assert colors == [(empty.width * empty.height, (0, 0, 0))]

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


def test_composer_keeps_at_most_four_cards():
    composer = Composer((32, 18), fade=0)
    for index in range(5):
        video = f"card{index:07d}"
        composer.frame([(video, 1.0)], {video: _solid((index + 1, 0, 0), (8, 8))}, now=float(index))
    assert len(composer._cards) == 4


def _letterbox(size: tuple[int, int], band: int, color: tuple[int, int, int]) -> Image.Image:
    width, height = size
    image = Image.new("RGB", size, (0, 0, 0))
    image.paste(Image.new("RGB", (width, max(1, height - 2 * band)), color), (0, band))
    return image


def _jpeg_of(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=85)
    return buffer.getvalue()


def test_thumbnails_come_only_from_ytimg_and_skip_placeholders():
    assert thumb_urls(VIDEO) == (
        f"https://i.ytimg.com/vi/{VIDEO}/maxresdefault.jpg",
        f"https://i.ytimg.com/vi/{VIDEO}/sddefault.jpg",
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
    seen_sd: list[str] = []

    def use_sd(url: str) -> tuple[int, bytes]:
        seen_sd.append(url)
        if url.endswith("maxresdefault.jpg"):
            return 404, b"missing"
        if url.endswith("sddefault.jpg"):
            return 200, good
        raise AssertionError(url)

    image = load_youtube_thumb(VIDEO, use_sd)
    assert image is not None and image.size == (480, 360)
    assert seen_sd == list(thumb_urls(VIDEO)[:2])
    assert all(url.startswith("https://i.ytimg.com/vi/") for url in seen_sd)

    seen_hq: list[str] = []

    def use_hq(url: str) -> tuple[int, bytes]:
        seen_hq.append(url)
        if url.endswith("hqdefault.jpg"):
            return 200, good
        return 404, b"missing"

    image = load_youtube_thumb(VIDEO, use_hq)
    assert image is not None and image.size == (480, 360)
    assert seen_hq == list(thumb_urls(VIDEO))
    assert all(url.startswith("https://i.ytimg.com/vi/") for url in seen_hq)

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


def test_letterbox_crop_keeps_centered_sixteen_nine_only():
    sd = _letterbox((640, 480), 60, (200, 40, 50))
    cropped = _crop_letterbox(sd)
    assert cropped.size == (640, 360)
    assert cropped.getpixel((0, 0)) == (200, 40, 50)
    assert cropped.getpixel((639, 359)) == (200, 40, 50)

    hq = _letterbox((480, 360), 45, (40, 180, 60))
    hq_cropped = _crop_letterbox(hq)
    assert hq_cropped.size == (480, 270)
    assert hq_cropped.getpixel((0, 0)) == (40, 180, 60)

    plain = Image.new("RGB", (640, 480), (180, 70, 40))
    plain.paste(Image.new("RGB", (220, 140), (20, 140, 200)), (30, 40))
    assert _crop_letterbox(plain).size == (640, 480)

    uneven = Image.new("RGB", (640, 480), (180, 70, 40))
    uneven.paste(Image.new("RGB", (640, 120), (0, 0, 0)), (0, 0))
    assert _crop_letterbox(uneven).size == (640, 480)

    side = Image.new("RGB", (640, 480), (200, 80, 40))
    side.paste(Image.new("RGB", (320, 480), (0, 0, 0)), (0, 0))
    assert _crop_letterbox(side).size == (640, 480)

    opened = _open_jpeg(_jpeg_of(sd))
    assert opened is not None and opened.size == (640, 360)


def test_sixteen_nine_card_is_pixel_exact_and_four_three_stays_contained():
    source = Image.new("RGB", (1280, 720), (8, 16, 24))
    source.putpixel((640, 360), (201, 19, 77))
    card = deck_card(source)
    assert card.size == (1280, 720)
    assert card.getpixel((640, 360)) == source.getpixel((640, 360))
    assert card.tobytes() == source.tobytes()

    four_three = Image.new("RGB", (640, 480), (220, 30, 40))
    contained = deck_card(four_three)
    assert contained.getpixel((640, 360)) == (220, 30, 40)
    corner = contained.getpixel((0, 0))
    assert corner != (220, 30, 40)
    assert corner[0] < 140

    wide = Image.new("RGB", (320, 180), (240, 10, 10))
    tall = Image.new("RGB", (160, 120), (10, 10, 240))
    mixed = Composer((320, 180), fade=0).frame(
        [(VIDEO, 0.5), (OTHER, 0.5)],
        {VIDEO: wide, OTHER: tall},
        now=0,
    )
    mix_corner = mixed.getpixel((0, 0))
    mix_center = mixed.getpixel((160, 90))
    assert mix_corner[0] > 80
    assert mix_center[0] > 80 and mix_center[2] > 80


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
    assert VIDEO_BITRATE == 800_000
    assert VIDEO_MAXRATE == 1_000_000
    assert VIDEO_BUFSIZE == 500_000
    assert command[command.index("-b:v") + 1] == str(VIDEO_BITRATE)
    assert command[command.index("-maxrate") + 1] == str(VIDEO_MAXRATE)
    assert command[command.index("-bufsize") + 1] == str(VIDEO_BUFSIZE)
    assert command[command.index("-preset") + 1] == "ultrafast"
    assert command[command.index("-tune") + 1] == "stillimage"
    assert command[command.index("-g") + 1] == str(VIDEO_FPS)
    assert _SYNC_PACKETS * 188 > (VIDEO_MAXRATE // 8) * 2
    assert command[command.index("-c:a") + 1] == "aac"
    assert command[command.index("-profile:a") + 1] == "aac_low"
    assert command[command.index("-b:a") + 1] == "128k"
    assert command[command.index("-ar") + 1] == "48000"
    assert command[command.index("-threads") + 1] == "1"
    assert "copy" not in command
    assert command[command.index("-muxdelay") + 1] == "0"
    assert command[command.index("-output_ts_offset") + 1] == "0.1"
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
        "video_size": (64, 36),
        "video_fps": 5,
        "video_linger": 0.2,
        "output_stall": 30.0,
        "thumbs": ThumbCache(lambda _video: None),
    }
    if "ffmpeg" not in kwargs:
        options["ffmpeg"] = _fake_ffmpeg(tmp_path)
    options.update(kwargs)
    return LiveHub(**options)


def _assert_reaped(pid: int) -> None:
    """The pid is gone. A zombie still has a /proc entry and still answers kill(pid, 0)."""

    stat = Path(f"/proc/{pid}/stat")
    if stat.exists():
        state = stat.read_text(encoding="utf-8").rsplit(")", 1)[-1].split()[0]
        raise AssertionError(f"pid {pid} is still state {state}")
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@contextlib.contextmanager
def _deadline(seconds: float, release: threading.Event | None = None):
    """Fail the test instead of hanging when a listener or the event loop stalls."""

    def _alarm(_signum, _frame):
        if release is not None:
            release.set()
        raise AssertionError(f"timed out after {seconds}s")

    previous = signal.signal(signal.SIGALRM, _alarm)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


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
    _assert_reaped(pid)


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
                    assert denied.headers["retry-after"] == "1"
                    denied_head = client.head("/stream/CAPB?thumbnail=1")
                    assert denied_head.status_code == 429
                    assert denied_head.headers["retry-after"] == "1"
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
    assert all(width <= MAX_EDGE and height <= MAX_EDGE for width, height in loaded)
    assert MAX_EDGE == 2048
    assert MAX_PIXELS == 1_843_200


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
    assert limited._jobs.maxsize == MAX_QUEUED
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
        _assert_reaped(pid)
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
                    _assert_reaped(pid)
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


def _ts_packet(pid: int, payload: bytes, cc: int = 0) -> bytes:
    raw = bytearray(188)
    raw[0] = 0x47
    raw[1] = 0x40 | ((pid >> 8) & 0x1F)
    raw[2] = pid & 0xFF
    raw[3] = 0x10 | (cc & 0x0F)
    raw[4 : 4 + len(payload)] = payload
    return bytes(raw)


def _pat_packet(pmt_pid: int, cc: int = 0) -> bytes:
    body = bytearray(b"\x00\x01\xc1\x00\x00")
    body += (1).to_bytes(2, "big")
    body.append(0xE0 | ((pmt_pid >> 8) & 0x1F))
    body.append(pmt_pid & 0xFF)
    body += b"\x00\x00\x00\x00"
    section = bytes([0x00, 0xB0 | ((len(body) >> 8) & 0x0F), len(body) & 0xFF]) + bytes(body)
    return _ts_packet(0, bytes([0]) + section, cc)


def _psi_packet(pid: int, table_id: int) -> bytes:
    return _ts_packet(pid, bytes([0, table_id]))


def _write_script(tmp_path: Path, source: str) -> str:
    path = tmp_path / "ffmpeg"
    path.write_text(source)
    path.chmod(0o755)
    return str(path)


_CONTINUOUS_FFMPEG = """#!/usr/bin/env python3
import os
import sys
import threading
import time

packet = bytearray(188)
packet[0] = 0x47
packet[1] = 0x41
packet[2] = 0x00
packet[3] = 0x10
packet[4:9] = b"\\x00\\x00\\x00\\x01\\x67"

def drain(handle):
    try:
        while handle.read(4096):
            pass
    except Exception:
        return

threading.Thread(target=drain, args=(sys.stdin.buffer,), daemon=True).start()
for arg in sys.argv[1:]:
    if arg.startswith("pipe:"):
        number = int(arg.split(":", 1)[1])
        if number not in (0, 1):
            threading.Thread(target=drain, args=(os.fdopen(number, "rb"),), daemon=True).start()
while True:
    sys.stdout.buffer.write(bytes(packet))
    sys.stdout.buffer.flush()
    time.sleep(0.05)
"""


def test_sync_buffer_prepends_pat_pmt_and_sdt():
    pat = _pat_packet(0x100)
    pmt = _psi_packet(0x100, 0x02)
    sdt = _psi_packet(0x11, 0x42)
    filler = _ts_packet(0x101, b"\x00\x00\x00\x01\x09")
    sps = _ts_packet(0x101, b"\x00\x00\x00\x01\x67")
    buf = TsSyncBuffer()
    buf.append(pat + pmt + sdt + filler * 40 + sps + filler)
    snap = buf.snapshot()
    assert snap[:188] == pat
    assert snap[188:376] == pmt
    assert snap[376:564] == sdt
    assert snap[564:752] == sps
    # A table that arrives after the SPS has a higher continuity counter.
    # Prepending that newer packet puts the counter in front of the older one.
    later_pat = _pat_packet(0x100, cc=9)
    later_pmt = _ts_packet(0x100, bytes([0, 0x02, 9]), cc=8)
    later_sdt = _ts_packet(0x11, bytes([0, 0x42, 9]), cc=7)
    buf.append(later_pat + later_pmt + later_sdt)
    again = buf.snapshot()
    assert again[:188] == pat
    assert again[188:376] == pmt
    assert again[376:564] == sdt
    assert again[564:752] == sps
    assert again.index(later_pat) > again.index(sps)


def test_a_malformed_now_is_dropped_and_the_audio_stays_up():
    hub = LiveHub(init_timeout=2, idle_timeout=5)
    cluster = cluster_known(0, b"wave")
    with TestClient(create_app(live=hub)) as client:
        with _ws(client, "/api/live/publish?id=BADJ") as publisher:
            head, body = _publish_ready(publisher, cluster)
            publisher.send_text('{"type":"now"')
            publisher.send_text('{ "type" : "now" ,')
            assert hub.get("BADJ") is not None
            with _Audio(client, "/stream/BADJ") as listener:
                assert listener.read() == head
                assert listener.read() == body
                nxt = cluster_known(1200, b"more")
                publisher.send_bytes(nxt)
                assert listener.read() == nxt
            assert hub.get("BADJ") is not None


def test_a_decompression_bomb_is_discarded(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1000)
    assert _open_jpeg(_jpeg((320, 180))) is None
    assert _open_jpeg(_jpeg((MAX_EDGE + 1, 180))) is None
    assert _open_jpeg(_jpeg((1600, 1200))) is None

    def boom(_video_id: str) -> Image.Image:
        raise Image.DecompressionBombError("boom")

    assert ThumbCache(boom).warm(VIDEO) is None

    root = tmp_path / "thumbs"
    root.mkdir()
    monkeypatch.undo()
    Image.new("RGB", (320, 180), (1, 2, 3)).save(root / f"{VIDEO}.jpg")

    def opening(*_args, **_kwargs):
        raise Image.DecompressionBombError("boom")

    monkeypatch.setattr(Image, "open", opening)
    assert fetch_thumb_dir(str(root))(VIDEO) is None


def test_open_jpeg_drafts_toward_a_720p_frame(monkeypatch):
    from PIL import JpegImagePlugin

    seen: list[tuple[str, tuple[int, int]]] = []
    original = JpegImagePlugin.JpegImageFile.draft

    def spy(self, mode, size):
        seen.append((mode, size))
        return original(self, mode, size)

    monkeypatch.setattr(JpegImagePlugin.JpegImageFile, "draft", spy)
    image = _open_jpeg(_jpeg((480, 360)))
    assert image is not None
    assert seen == [("RGB", (1280, 1280))]


def test_one_address_cannot_collect_encoders_during_linger(tmp_path: Path):
    """Opening a second stream stops the one this address left lingering.

    While this address is watching one encoder, another stream is refused.
    A different address can still take a free encoder, and this address cannot join it.
    """

    hub = _hub(tmp_path, max_video_encoders=3, max_video_per_ip=1, video_linger=5)
    held = {"x-real-ip": "198.51.100.8"}
    other = {"x-real-ip": "198.51.100.9"}
    with TestClient(create_app(live=hub)) as client:
        with _ws(client, "/api/live/publish?id=SAAA", headers={"x-real-ip": "192.0.2.1"}) as first:
            _publish_ready(first)
            with _ws(client, "/api/live/publish?id=SAAB", headers={"x-real-ip": "192.0.2.2"}) as second:
                _publish_ready(second)
                with _ws(client, "/api/live/publish?id=SAAC", headers={"x-real-ip": "192.0.2.3"}) as third:
                    _publish_ready(third)
                    with _Audio(client, "/stream/SAAA", query="thumbnail=1", headers=held) as video:
                        assert video.read()[:1] == b"\x47"
                    first_relay = hub.get("SAAA").relay
                    assert first_relay.occupies()
                    with _Audio(client, "/stream/SAAB", query="thumbnail=1", headers=held) as moved:
                        assert moved.status_code == 200
                        assert moved.read()[:1] == b"\x47"
                        assert first_relay.occupies() is False
                        denied_c = client.head("/stream/SAAC?thumbnail=1", headers=held)
                        assert denied_c.status_code == 429
                        assert denied_c.headers["retry-after"] == "1"
                        reopen = client.get("/stream/SAAA?thumbnail=1", headers=held)
                        assert reopen.status_code == 429
                        with _Audio(client, "/stream/SAAC", query="thumbnail=1", headers=other) as theirs:
                            assert theirs.status_code == 200
                            assert theirs.read()[:1] == b"\x47"
                            joined = client.get("/stream/SAAC?thumbnail=1", headers=held)
                            assert joined.status_code == 429


def test_the_starter_slot_frees_while_someone_else_is_watching(tmp_path: Path):
    """A starts S1, B joins, A leaves. A can open S2. B keeps S1.

    The starter used to keep the per-IP slot for the life of the process, so
    after 4.5 s (past the 3 s linger) A was still refused on S2.
    """

    hub = _hub(
        tmp_path,
        ffmpeg=_write_script(tmp_path, _CONTINUOUS_FFMPEG),
        max_video_encoders=3,
        max_video_per_ip=1,
        video_linger=3,
        output_stall=30,
    )
    starter = {"x-real-ip": "198.51.100.21"}
    guest = {"x-real-ip": "198.51.100.22"}
    with TestClient(create_app(live=hub)) as client:
        with _ws(client, "/api/live/publish?id=SLTA", headers={"x-real-ip": "192.0.2.21"}) as first:
            _publish_ready(first)
            with _ws(client, "/api/live/publish?id=SLTB", headers={"x-real-ip": "192.0.2.22"}) as second:
                _publish_ready(second)
                with _deadline(12):
                    _starter_leaves_and_opens_another(client, hub, starter, guest)


def _starter_leaves_and_opens_another(client, hub, starter, guest) -> None:
    started = _Audio(client, "/stream/SLTA", query="thumbnail=1", headers=starter)
    started.__enter__()
    left = False
    try:
        assert started.read()[:1] == b"\x47"
        with _Audio(client, "/stream/SLTA", query="thumbnail=1", headers=guest) as watching:
            assert watching.read()[:1] == b"\x47"
            started.__exit__(None, None, None)
            left = True
            time.sleep(4.5)
            with _Audio(client, "/stream/SLTB", query="thumbnail=1", headers=starter) as other:
                assert other.status_code == 200
                assert other.read()[:1] == b"\x47"
                assert watching.read()[:1] == b"\x47"
                assert hub.get("SLTA").relay.occupies()
    finally:
        if not left:
            started.__exit__(None, None, None)


def test_starting_another_stream_stops_the_lingering_encoder(tmp_path: Path):
    hub = _hub(tmp_path, max_video_encoders=3, max_video_per_ip=1, video_linger=30)
    held = {"x-real-ip": "198.51.100.30"}
    with TestClient(create_app(live=hub)) as client:
        with _ws(client, "/api/live/publish?id=HNGA", headers={"x-real-ip": "192.0.2.30"}) as first:
            _publish_ready(first)
            with _ws(client, "/api/live/publish?id=HNGB", headers={"x-real-ip": "192.0.2.31"}) as second:
                _publish_ready(second)
                with _Audio(client, "/stream/HNGA", query="thumbnail=1", headers=held) as video:
                    assert video.read()[:1] == b"\x47"
                lingering = hub.get("HNGA").relay
                assert lingering.occupies()
                preview = client.head("/stream/HNGB?thumbnail=1", headers=held)
                assert preview.status_code == 200
                assert lingering.occupies()
                with _Audio(client, "/stream/HNGB", query="thumbnail=1", headers=held) as moved:
                    assert moved.status_code == 200
                    assert moved.read()[:1] == b"\x47"
                    assert lingering.occupies() is False
                    assert hub.get("HNGB").relay.occupies()


def test_the_starter_record_is_cleared_when_the_encoder_exits(tmp_path: Path):
    hub = _hub(tmp_path, video_linger=0.3)
    with TestClient(create_app(live=hub)) as client:
        with _ws(client, "/api/live/publish?id=HOLD") as publisher:
            _publish_ready(publisher)
            with _Audio(client, "/stream/HOLD", query="thumbnail=1") as video:
                assert video.read()[:1] == b"\x47"
                assert hub.get("HOLD").relay.holder()
            time.sleep(0.8)
            assert hub.get("HOLD").relay.holder() == ""
            assert hub.get("HOLD").relay.occupies() is False


def test_steady_output_is_not_cut_at_the_stall_window(tmp_path: Path):
    hub = _hub(tmp_path, ffmpeg=_write_script(tmp_path, _CONTINUOUS_FFMPEG), output_stall=0.5, video_linger=0.2)
    with TestClient(create_app(live=hub)) as client:
        with _ws(client, "/api/live/publish?id=STED") as publisher:
            _publish_ready(publisher)
            with _deadline(3):
                with _Audio(client, "/stream/STED", query="thumbnail=1") as video:
                    started = time.monotonic()
                    while time.monotonic() - started < 1.2:
                        piece = video.read()
                        assert piece, "steady encoder was disconnected"
                    assert hub.get("STED").relay.occupies()


def test_a_listener_that_joins_during_linger_keeps_receiving(tmp_path: Path):
    hub = _hub(tmp_path, ffmpeg=_write_script(tmp_path, _CONTINUOUS_FFMPEG), video_linger=0.35, output_stall=30)
    with TestClient(create_app(live=hub)) as client:
        with _ws(client, "/api/live/publish?id=LNGR") as publisher:
            _publish_ready(publisher)
            with _Audio(client, "/stream/LNGR", query="thumbnail=1") as first:
                assert first.read()[:1] == b"\x47"
            with _deadline(2):
                with _Audio(client, "/stream/LNGR", query="thumbnail=1") as second:
                    assert second.read()[:1] == b"\x47"
                    time.sleep(0.5)
                    assert second.read()
                    assert hub.get("LNGR").relay.occupies()


def test_a_crashed_encoder_answers_503_until_backoff_ends(tmp_path: Path):
    script = _write_script(
        tmp_path,
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "packet = bytearray(188)\n"
        "packet[0] = 0x47\n"
        "packet[1] = 0x41\n"
        "packet[3] = 0x10\n"
        "packet[4:9] = b'\\x00\\x00\\x00\\x01\\x67'\n"
        "sys.stdout.buffer.write(bytes(packet) * 4)\n"
        "sys.stdout.buffer.flush()\n"
        "import os\n"
        "os._exit(0)\n",
    )
    hub = _hub(tmp_path, ffmpeg=script, restart_backoff=2, output_stall=30, video_linger=2)
    with TestClient(create_app(live=hub)) as client:
        with _ws(client, "/api/live/publish?id=BACK") as publisher:
            _publish_ready(publisher)
            with _Audio(client, "/stream/BACK", query="thumbnail=1") as video:
                assert video.read()[:1] == b"\x47"
                assert video.read() is None
            head = client.head("/stream/BACK?thumbnail=1")
            body = client.get("/stream/BACK?thumbnail=1")
    assert head.status_code == 503
    assert body.status_code == 503
    assert body.content == b""
    assert int(head.headers["retry-after"]) >= 1
    assert int(body.headers["retry-after"]) >= 1


def test_audio_handed_to_ffmpeg_has_a_rebased_cluster(tmp_path: Path):
    dump = tmp_path / "audio.bin"
    script = _write_script(
        tmp_path,
        "#!/usr/bin/env python3\n"
        "import os, select, sys, threading, time\n"
        f"out = open({str(dump)!r}, 'wb')\n"
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
        "number = None\n"
        "for arg in sys.argv[1:]:\n"
        "    if arg.startswith('pipe:'):\n"
        "        fd = int(arg.split(':', 1)[1])\n"
        "        if fd not in (0, 1):\n"
        "            number = fd\n"
        "if number is not None:\n"
        "    ready, _, _ = select.select([number], [], [], 1.5)\n"
        "    if ready:\n"
        "        out.write(os.read(number, 65536))\n"
        "        out.flush()\n"
        "time.sleep(30)\n",
    )
    hub = _hub(tmp_path, ffmpeg=script, video_linger=2)
    cluster = cluster_known(5000, b"wave")
    with TestClient(create_app(live=hub)) as client:
        with _ws(client, "/api/live/publish?id=REBA") as publisher:
            _publish_ready(publisher, cluster)
            with _Audio(client, "/stream/REBA", query="thumbnail=1") as video:
                assert video.read()[:1] == b"\x47"
                deadline = time.monotonic() + 2
                blob = b""
                while time.monotonic() < deadline:
                    if dump.exists():
                        blob = dump.read_bytes()
                        if b"\x1f\x43\xb6\x75" in blob:
                            break
                    time.sleep(0.05)
                else:
                    raise AssertionError("audio was not written")
    marker = blob.find(b"\x1f\x43\xb6\x75")
    assert cluster_timecode(blob[marker:]) == 0


def test_a_late_clock_writes_the_duplicate_count():
    relay = FfmpegRelay("COPY", lambda: (), ThumbCache(lambda _video: None), width=16, height=16, fps=5)
    relay.composer = Composer((16, 16))

    class _Stdin:
        def __init__(self) -> None:
            self.writes = 0

        def write(self, _data: bytes) -> None:
            self.writes += 1

    class _Proc:
        def __init__(self) -> None:
            self.stdin = _Stdin()

    relay._proc = _Proc()
    calls = {"n": 0}

    def clock() -> float:
        calls["n"] += 1
        if calls["n"] >= 3:
            relay._stop.set()
        return 0.0 if calls["n"] == 1 else 1.0

    relay._clock = clock
    relay._frame_loop()
    assert relay._proc.stdin.writes == 6


def _watch_until(relay: FfmpegRelay, sequence: tuple[float, ...], mutate=None) -> list[int]:
    relay._started = True
    relay._last_output = 0.0

    class _Proc:
        def poll(self):
            return None

    relay._proc = _Proc()
    calls = {"n": 0}
    failed: list[int] = []

    def clock() -> float:
        index = calls["n"]
        calls["n"] += 1
        if mutate is not None:
            mutate(index)
        if index >= len(sequence):
            relay._stop.set()
            return sequence[-1]
        return sequence[index]

    def fail(_proc=None) -> None:
        failed.append(calls["n"])
        relay._started = False
        relay._stop.set()

    relay._clock = clock
    relay._fail = fail
    relay._stop.wait = lambda timeout=None: relay._stop.is_set()
    relay._watch_loop()
    return failed


def test_a_long_pause_counts_at_most_two_watch_periods():
    relay = FfmpegRelay("PAUS", lambda: (), ThumbCache(lambda _video: None), output_stall=0.5)
    # 5.0 - 0.0 is over a second, so it adds 0.4. The next 0.2 pushes quiet past 0.5.
    assert _watch_until(relay, (0.0, 5.0, 5.2, 5.4, 5.6, 5.8)) == [3]


def test_repeated_pauses_still_trip_the_stall():
    relay = FfmpegRelay("RPTP", lambda: (), ThumbCache(lambda _video: None), output_stall=0.95)
    times = [0.0]
    for _cycle in range(6):
        times.append(times[-1] + 1.2)
        times.append(times[-1] + 0.1)
    # Each 1.2 s pause adds 0.4 and each 0.1 s tick adds 0.1. Quiet crosses 0.95
    # on the second short tick. Charging 0 for the long gaps would stay under it.
    assert _watch_until(relay, tuple(times)) == [5]


def test_stall_total_resets_when_output_arrives():
    relay = FfmpegRelay("RSTQ", lambda: (), ThumbCache(lambda _video: None), output_stall=0.5)

    def mutate(index: int) -> None:
        if index == 3:
            relay._last_output = 50.0

    # Two quiet ticks reach 0.4, output clears it, then it has to climb again.
    failed = _watch_until(relay, (0.0, 0.2, 0.4, 0.6, 0.8, 1.0, 1.2), mutate)
    assert failed == [7]


def _drain_audio(relay: FfmpegRelay) -> list[bytes]:
    found: list[bytes] = []
    while True:
        try:
            found.append(relay._audio_q.get_nowait())
        except queue.Empty:
            return found


def test_feed_keeps_a_cluster_when_id_repeats(monkeypatch):
    """P5: an id() check drops a new cluster that reuses a primer's address.

    id() is patched so the decoy and the primer report the same value. Identity
    (`is`) still tells them apart.
    """

    import builtins

    relay = FfmpegRelay("PRIM", lambda: (), ThumbCache(lambda _video: None), width=16, height=16)
    primer = b"P" * 48
    decoy = b"D" * 48
    real_id = builtins.id

    def repeated(obj: object) -> int:
        if obj is primer or obj is decoy:
            return 1
        return real_id(obj)

    monkeypatch.setattr(builtins, "id", repeated)
    relay.prime(b"init", (primer,))
    relay._started = True
    _drain_audio(relay)
    relay.feed(decoy)
    assert any(item is decoy for item in _drain_audio(relay))


def test_prime_feeds_only_the_last_two_clusters():
    relay = FfmpegRelay("PRIM", lambda: (), ThumbCache(lambda _video: None), width=16, height=16)
    clusters = [bytes([index]) + b"\x11" * 31 for index in range(8)]
    relay.prime(b"init", tuple(clusters))
    assert _drain_audio(relay) == clusters[-2:]


def test_the_primed_object_is_not_written_twice():
    relay = FfmpegRelay("PRM2", lambda: (), ThumbCache(lambda _video: None), width=16, height=16)
    cluster = b"P" * 64
    relay.prime(b"init", (cluster,))
    relay._started = True
    _drain_audio(relay)
    relay.feed(cluster)
    assert _drain_audio(relay) == []
    other = b"Q" * 64
    relay.feed(other)
    assert _drain_audio(relay) == [other]


def test_audio_packet_timestamps_have_no_gap_past_the_primer(tmp_path: Path):
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg is required")
    from djtube.live_tone import render_webm

    init, clusters = split_webm(render_webm([(440, 5.0)]))
    assert len(clusters) > 16
    hub = LiveHub(
        video_size=(64, 36),
        video_fps=5,
        video_linger=0.2,
        audio_queue_max=400,
        thumbs=ThumbCache(lambda _video: None),
    )
    with TestClient(create_app(live=hub)) as client:
        with _ws(client, "/api/live/publish?id=AGAP") as publisher:
            publisher.receive_json()
            publisher.send_json({"type": "mime", "mime": "audio/webm"})
            # Eight completed clusters sit in recent before the encoder starts.
            publisher.send_bytes(init + b"".join(clusters[:9]))
            with _Audio(client, "/stream/AGAP", query="thumbnail=1") as video:
                publisher.send_bytes(b"".join(clusters[9:]))
                blob = b""
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    piece = video.read()
                    if not piece:
                        break
                    blob += piece
    path = tmp_path / "audio.ts"
    path.write_bytes(blob)
    probed = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a",
            "-show_entries",
            "packet=pts_time",
            "-of",
            "csv=p=0",
            str(path),
        ],
        capture_output=True,
        check=False,
        text=True,
    )
    assert probed.returncode == 0, probed.stderr[-400:]
    pts = []
    for line in probed.stdout.splitlines():
        text = line.strip().rstrip(",")
        if text and text != "N/A":
            pts.append(float(text))
    assert len(pts) > 80
    assert pts[-1] - pts[0] > 1.6
    gaps = [later - earlier for earlier, later in zip(pts, pts[1:])]
    assert gaps
    assert max(gaps) < 0.08


def test_a_cooling_encoder_is_503_even_when_the_cap_is_full(tmp_path: Path):
    script = _write_script(
        tmp_path,
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "packet = bytearray(188)\n"
        "packet[0] = 0x47\n"
        "packet[1] = 0x41\n"
        "packet[3] = 0x10\n"
        "packet[4:9] = b'\\x00\\x00\\x00\\x01\\x67'\n"
        "sys.stdout.buffer.write(bytes(packet) * 4)\n"
        "sys.stdout.buffer.flush()\n"
        "import os\n"
        "os._exit(0)\n",
    )
    hub = _hub(tmp_path, ffmpeg=script, restart_backoff=5, output_stall=30, video_linger=2, max_video_encoders=1)
    with TestClient(create_app(live=hub)) as client:
        with _ws(client, "/api/live/publish?id=COOL", headers={"x-real-ip": "192.0.2.71"}) as cooling:
            _publish_ready(cooling)
            with _ws(client, "/api/live/publish?id=BUSY", headers={"x-real-ip": "192.0.2.72"}) as other:
                _publish_ready(other)
                with _Audio(client, "/stream/COOL", query="thumbnail=1") as video:
                    assert video.read()[:1] == b"\x47"
                    assert video.read() is None
                with _Audio(
                    client, "/stream/BUSY", query="thumbnail=1", headers={"x-real-ip": "198.51.100.72"}
                ) as busy:
                    assert busy.read()[:1] == b"\x47"
                    head = client.head("/stream/COOL?thumbnail=1")
                    body = client.get("/stream/COOL?thumbnail=1")
    assert head.status_code == 503
    assert body.status_code == 503
    assert body.content == b""
    assert int(head.headers["retry-after"]) >= 1
    assert int(body.headers["retry-after"]) >= 1


def test_open_video_does_not_start_after_the_generation_moves(tmp_path: Path):
    calls = {"n": 0}

    def counting_popen(*args, **kwargs):
        calls["n"] += 1
        return subprocess.Popen(*args, **kwargs)

    hub = _hub(tmp_path, popen=counting_popen, video_linger=5)
    with TestClient(create_app(live=hub)) as client:
        with _ws(client, "/api/live/publish?id=GENV") as publisher:
            _publish_ready(publisher)
            portal = client.portal
            assert portal is not None
            status, listener, stream, opener = portal.call(hub.prepare_video, "GENV", "203.0.113.50")
            assert status == 200 and stream is not None and opener is not None and listener is not None
            stream.generation += 1
            try:
                with _deadline(2):
                    hub.open_video(stream, listener, *opener)
                assert calls["n"] == 0
                assert stream.relay is not None
                assert stream.relay.occupies() is False
                assert stream.relay._proc is None
            finally:
                stream.relay.close()


def _heartbeat(client: TestClient) -> dict[str, int]:
    """A task on the server loop. It only advances while that loop is free."""

    ticks = {"n": 0}

    async def beat() -> None:
        while True:
            ticks["n"] += 1
            await asyncio.sleep(0.02)

    portal = client.portal
    assert portal is not None

    def start() -> None:
        asyncio.get_running_loop().create_task(beat())

    portal.call(start)
    time.sleep(0.05)
    return ticks


def _loop_was_free(ticks: dict[str, int], pause: float = 0.25) -> bool:
    before = ticks["n"]
    time.sleep(pause)
    return ticks["n"] > before + 2


def test_popen_and_close_stay_off_the_event_loop(tmp_path: Path):
    moved: list[bool] = []

    def slow_popen(*args, **kwargs):
        moved.append(_loop_was_free(ticks))
        return subprocess.Popen(*args, **kwargs)

    ticks: dict[str, int] = {}
    hub = _hub(tmp_path, popen=slow_popen)
    with TestClient(create_app(live=hub)) as client:
        ticks = _heartbeat(client)
        with _ws(client, "/api/live/publish?id=POPN") as publisher:
            _publish_ready(publisher)
            with _Audio(client, "/stream/POPN", query="thumbnail=1") as video:
                assert video.read()[:1] == b"\x47"
    assert moved == [True]

    moved.clear()
    hub = _hub(tmp_path, video_linger=5)
    with TestClient(create_app(live=hub)) as client:
        ticks = _heartbeat(client)
        with _ws(client, "/api/live/publish?id=CLOS") as publisher:
            _publish_ready(publisher)
            with _Audio(client, "/stream/CLOS", query="thumbnail=1") as video:
                assert video.read()[:1] == b"\x47"
                relay = hub.get("CLOS").relay
                original = relay.close

                def slow_close() -> None:
                    moved.append(_loop_was_free(ticks))
                    original()

                relay.close = slow_close
    assert moved == [True]

    moved.clear()
    hub = _hub(tmp_path, video_linger=5)
    cluster = cluster_known(0, b"wave")
    head, _data = document([cluster])
    with TestClient(create_app(live=hub)) as client:
        ticks = _heartbeat(client)
        with _ws(client, "/api/live/publish?id=DISP") as old:
            token = _id_token(old.receive_json(), "DISP")
            old.send_json({"type": "mime", "mime": "audio/webm"})
            old.send_bytes(head + cluster)
            with _Audio(client, "/stream/DISP", query="thumbnail=1") as video:
                assert video.read()[:1] == b"\x47"
                relay = hub.get("DISP").relay
                original = relay.close

                def slow_close() -> None:
                    moved.append(_loop_was_free(ticks))
                    original()

                relay.close = slow_close
                started = time.monotonic()
                with _ws(client, _with_token("DISP", token), seq=2) as new:
                    assert new.receive_json()["type"] == "id"
                assert time.monotonic() - started < 0.15
                deadline = time.monotonic() + 2
                while not moved and time.monotonic() < deadline:
                    time.sleep(0.05)
                assert moved == [True]
                with pytest.raises(WebSocketDisconnect) as replaced:
                    old.receive_json()
                assert replaced.value.code == CODE_REPLACED


def _pull_for(video: _Audio, seconds: float) -> bytes:
    """Read the live body for up to `seconds`, then return what arrived."""

    async def pull() -> bytes:
        parts: list[bytes] = []
        loop = asyncio.get_running_loop()
        deadline = loop.time() + seconds
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            try:
                message = await asyncio.wait_for(video._send_rx.receive(), remaining)
            except TimeoutError:
                break
            if message.get("type") != "http.response.body" or not message.get("more_body", False):
                break
            body = message.get("body", b"")
            if isinstance(body, bytes) and body:
                parts.append(body)
        return b"".join(parts)

    return video._portal.call(pull)


def _goertzel(samples: array.array, start: int, count: int, freq: float, rate: int = 48000) -> float:
    k = int(0.5 + (count * freq) / rate)
    coeff = 2 * math.cos(2 * math.pi * k / count)
    first = 0.0
    second = 0.0
    for index in range(start, start + count):
        current = samples[index] + coeff * first - second
        second = first
        first = current
    return first * first + second * second - coeff * first * second


def _tone_onset(samples: array.array, origin: float, freq: float = 880.0, other: float = 440.0) -> float:
    count = 2048
    hop = 960
    hits = 0
    for start in range(0, len(samples) - count, hop):
        high = _goertzel(samples, start, count, freq)
        low = _goertzel(samples, start, count, other)
        if high > 1e-4 and high > low * 3:
            hits += 1
            if hits >= 2:
                begun = max(0, start - hop)
                return origin + begun / 48000
        else:
            hits = 0
    raise AssertionError(f"no {freq:.0f} Hz marker in {len(samples)} samples")


def _audio_marker_pts(path: Path) -> float:
    probed = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "packet=pts_time",
            "-of",
            "csv=p=0",
            str(path),
        ],
        capture_output=True,
        check=False,
        text=True,
    )
    assert probed.returncode == 0, probed.stderr[-400:]
    origin = None
    for line in probed.stdout.splitlines():
        text = line.strip().rstrip(",")
        if text and text != "N/A":
            origin = float(text)
            break
    assert origin is not None
    decoded = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-ac",
            "1",
            "-ar",
            "48000",
            "-f",
            "f32le",
            "pipe:1",
        ],
        capture_output=True,
        check=False,
    )
    assert decoded.returncode == 0, decoded.stderr[-400:]
    assert sys.byteorder == "little"
    samples: array.array = array.array("f")
    samples.frombytes(decoded.stdout)
    return _tone_onset(samples, origin)


def _video_marker_pts(path: Path, width: int, height: int) -> float:
    probed = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "frame=pts_time",
            "-of",
            "csv=p=0",
            str(path),
        ],
        capture_output=True,
        check=False,
        text=True,
    )
    assert probed.returncode == 0, probed.stderr[-400:]
    pts: list[float] = []
    for line in probed.stdout.splitlines():
        text = line.strip().rstrip(",")
        if text and text != "N/A":
            pts.append(float(text))
    decoded = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "pipe:1",
        ],
        capture_output=True,
        check=False,
    )
    assert decoded.returncode == 0, decoded.stderr[-400:]
    frame = width * height * 3
    cx = width // 2
    cy = height // 2
    for index, stamp in enumerate(pts):
        offset = index * frame + (cy * width + cx) * 3
        if offset + 3 > len(decoded.stdout):
            break
        red, green, blue = decoded.stdout[offset : offset + 3]
        if red > 40 and red > blue + 30 and red > green + 30:
            return stamp
    raise AssertionError(f"no red frame in {len(pts)} frames / {len(decoded.stdout)} bytes")


def test_a_marker_tone_stays_within_a_fifth_of_a_second_of_the_picture(tmp_path: Path, monkeypatch):
    """The picture and an 880 Hz marker that start together stay within about 0.2 s.

    Priming all eight recent clusters stamps the current sound about a second
    late. Only the last two are fed, so video PTS minus audio PTS stays small.
    The deck fade is off, and the frame rate is 25, so the grid is finer than
    the allowance. The clocks themselves do not depend on that rate.
    """

    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg is required")
    from djtube.live_tone import render_webm

    original_init = Composer.__init__

    def instant(self, size=(VIDEO_WIDTH, VIDEO_HEIGHT), fade: float = 0.0) -> None:
        original_init(self, size, 0.0)

    monkeypatch.setattr(Composer, "__init__", instant)
    width, height = 64, 36
    red = Image.new("RGB", (320, 180), (230, 20, 20))
    hub = LiveHub(
        video_size=(width, height),
        video_fps=25,
        video_linger=0.2,
        audio_queue_max=400,
        video_listener_queue=500,
        thumbs=ThumbCache(lambda video_id: red if video_id == VIDEO else None),
    )
    init, clusters = split_webm(render_webm([(440, 2.4), (880, 2.0)]))
    switch_ms = 2400
    with TestClient(create_app(live=hub)) as client:
        with _ws(client, "/api/live/publish?id=AVSY") as publisher:
            publisher.receive_json()
            publisher.send_json({"type": "mime", "mime": "audio/webm"})
            publisher.send_bytes(init + b"".join(clusters[:9]))
            with _deadline(2):
                while True:
                    stream = hub.get("AVSY")
                    if stream is not None and len(stream.recent) >= 2:
                        break
                    time.sleep(0.01)
            assert stream is not None and len(stream.recent) >= 2
            now_tc = cluster_timecode(stream.recent[-1])
            assert now_tc is not None
            rest = [item for item in clusters if (cluster_timecode(item) or 0) > now_tc]
            assert rest
            with _Audio(client, "/stream/AVSY", query="thumbnail=1") as video:
                assert video.status_code == 200
                started = time.monotonic()
                for cluster in rest:
                    tc = cluster_timecode(cluster)
                    assert tc is not None
                    delay = (tc - now_tc) / 1000 - (time.monotonic() - started)
                    if delay > 0:
                        time.sleep(delay)
                    if tc >= switch_ms - 200 and tc < switch_ms:
                        publisher.send_json(_now())
                    publisher.send_bytes(cluster)
                time.sleep(0.4)
                blob = _pull_for(video, 2.5)
    path = tmp_path / "marker.ts"
    path.write_bytes(blob)
    assert blob.startswith(b"\x47")
    video_pts = _video_marker_pts(path, width, height)
    audio_pts = _audio_marker_pts(path)
    delta = video_pts - audio_pts
    print(f"A/V offset video-audio={delta:.3f}s video={video_pts:.3f} audio={audio_pts:.3f}")
    assert abs(delta) <= 0.2, f"video-audio={delta:.3f}s video={video_pts:.3f} audio={audio_pts:.3f}"


def _continuity_failures(stderr: str) -> list[str]:
    return [line for line in stderr.splitlines() if "Continuity check failed" in line]


def test_a_mid_stream_join_keeps_continuity_on_pat_pmt_and_sdt(tmp_path: Path):
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg is required")
    from djtube.live_tone import render_webm

    hub = LiveHub(
        video_size=(64, 36),
        video_fps=5,
        video_linger=0.2,
        audio_queue_max=400,
        video_listener_queue=500,
        thumbs=ThumbCache(lambda _video: None),
    )
    webm = render_webm([(440, 6.0)])
    with TestClient(create_app(live=hub)) as client:
        with _ws(client, "/api/live/publish?id=JOIN") as publisher:
            publisher.receive_json()
            publisher.send_json({"type": "mime", "mime": "audio/webm"})
            with _Audio(client, "/stream/JOIN", query="thumbnail=1") as first:
                assert first.status_code == 200
                publisher.send_bytes(webm)
                _pull_for(first, 2.5)
                with _Audio(
                    client, "/stream/JOIN", query="thumbnail=1", headers={"x-real-ip": "198.51.100.77"}
                ) as second:
                    assert second.status_code == 200
                    blob = _pull_for(second, 1.2)
    path = tmp_path / "join.ts"
    path.write_bytes(blob)
    assert blob.startswith(b"\x47") and len(blob) > 188 * 20
    pids = _ts_pids(blob)
    assert {0, 4096, 17} <= pids
    checked = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"],
        capture_output=True,
        check=False,
        text=True,
    )
    failed = _continuity_failures(checked.stderr)
    print(f"continuity failures={len(failed)} pids={sorted(pids)}")
    assert failed == [], failed
    assert checked.returncode == 0, checked.stderr[-400:]


def _ts_pids(blob: bytes) -> set[int]:
    found: set[int] = set()
    index = blob.find(b"\x47")
    if index < 0:
        return found
    while index + 188 <= len(blob):
        if blob[index] != 0x47:
            index += 1
            continue
        found.add(((blob[index + 1] & 0x1F) << 8) | blob[index + 2])
        index += 188
    return found


def test_the_global_cap_is_rechecked_after_a_handover(tmp_path: Path, monkeypatch):
    """H6: a slot taken while the linger is stopping still refuses the new stream."""

    script = _write_script(tmp_path, _CONTINUOUS_FFMPEG)
    hub = _hub(
        tmp_path,
        ffmpeg=script,
        max_video_encoders=1,
        max_video_per_ip=1,
        video_linger=30,
        output_stall=30,
    )
    held = {"x-real-ip": "198.51.100.81"}
    original = FfmpegRelay.take_abandoned
    planted: dict[str, object] = {}

    def take(self, *args, **kwargs):
        bundle = original(self, *args, **kwargs)
        # stop_handover already holds the hub lock. hub.get would lock it again.
        other = planted.get("stream")
        if bundle is not None and other is not None:
            other.video_hold = True
        return bundle

    monkeypatch.setattr(FfmpegRelay, "take_abandoned", take)
    with TestClient(create_app(live=hub)) as client:
        with _ws(client, "/api/live/publish?id=CAPA", headers={"x-real-ip": "192.0.2.81"}) as first:
            _publish_ready(first)
            with _ws(client, "/api/live/publish?id=CAPB", headers={"x-real-ip": "192.0.2.82"}) as second:
                _publish_ready(second)
                with _ws(client, "/api/live/publish?id=CAPH", headers={"x-real-ip": "192.0.2.83"}) as third:
                    _publish_ready(third)
                    planted["stream"] = hub.get("CAPH")
                    with _Audio(client, "/stream/CAPA", query="thumbnail=1", headers=held) as video:
                        assert video.read()[:1] == b"\x47"
                    lingering = hub.get("CAPA").relay
                    assert lingering.occupies()
                    with _deadline(5):
                        denied = client.get("/stream/CAPB?thumbnail=1", headers=held)
                    assert denied.status_code == 429
                    assert lingering.occupies() is False
                    assert hub.get("CAPH").video_hold is True


def test_the_per_ip_cap_is_rechecked_after_a_handover(tmp_path: Path, monkeypatch):
    """H6b: the global cap has room, but this address took another encoder during the stop."""

    script = _write_script(tmp_path, _CONTINUOUS_FFMPEG)
    hub = _hub(
        tmp_path,
        ffmpeg=script,
        max_video_encoders=3,
        max_video_per_ip=1,
        video_linger=30,
        output_stall=30,
    )
    held = {"x-real-ip": "198.51.100.84"}
    original = FfmpegRelay.take_abandoned
    planted: dict[str, object] = {}

    def take(self, *args, **kwargs):
        bundle = original(self, *args, **kwargs)
        other = planted.get("stream")
        if bundle is not None and other is not None and not other.video_listeners:
            other.video_hold = True

            class _Held:
                address = held["x-real-ip"]

                def offer_end(self) -> None:
                    return

                def mark_closed(self) -> None:
                    return

            other.video_listeners.add(_Held())
        return bundle

    monkeypatch.setattr(FfmpegRelay, "take_abandoned", take)
    with TestClient(create_app(live=hub)) as client:
        with _ws(client, "/api/live/publish?id=IPBA", headers={"x-real-ip": "192.0.2.84"}) as first:
            _publish_ready(first)
            with _ws(client, "/api/live/publish?id=IPBB", headers={"x-real-ip": "192.0.2.85"}) as second:
                _publish_ready(second)
                with _ws(client, "/api/live/publish?id=IPBH", headers={"x-real-ip": "192.0.2.86"}) as third:
                    _publish_ready(third)
                    planted["stream"] = hub.get("IPBH")
                    with _Audio(client, "/stream/IPBA", query="thumbnail=1", headers=held) as video:
                        assert video.read()[:1] == b"\x47"
                    with _deadline(5):
                        denied = client.get("/stream/IPBB?thumbnail=1", headers=held)
                    assert denied.status_code == 429
                    assert hub.get("IPBA").relay.occupies() is False


def test_rejoining_during_linger_does_not_count_as_a_new_encoder(tmp_path: Path):
    """Rejoining the linger that already fills the only slot is not a new encoder."""

    script = _write_script(tmp_path, _CONTINUOUS_FFMPEG)
    hub = _hub(
        tmp_path,
        ffmpeg=script,
        max_video_encoders=1,
        max_video_per_ip=1,
        video_linger=30,
        output_stall=30,
    )
    starter = {"x-real-ip": "198.51.100.87"}
    other = {"x-real-ip": "198.51.100.88"}
    guest = {"x-real-ip": "198.51.100.89"}
    with TestClient(create_app(live=hub)) as client:
        with _ws(client, "/api/live/publish?id=REJA", headers={"x-real-ip": "192.0.2.87"}) as first:
            _publish_ready(first)
            with _ws(client, "/api/live/publish?id=REJB", headers={"x-real-ip": "192.0.2.88"}) as second:
                _publish_ready(second)
                with _Audio(client, "/stream/REJA", query="thumbnail=1", headers=starter) as video:
                    assert video.read()[:1] == b"\x47"
                assert hub.get("REJA").relay.occupies()
                with _Audio(client, "/stream/REJA", query="thumbnail=1", headers=starter) as again:
                    assert again.status_code == 200
                    assert again.read()[:1] == b"\x47"
                    denied = client.get("/stream/REJB?thumbnail=1", headers=other)
                    assert denied.status_code == 429
                    with _Audio(client, "/stream/REJA", query="thumbnail=1", headers=guest) as joined:
                        assert joined.status_code == 200
                        assert joined.read()[:1] == b"\x47"


def test_a_switch_stops_the_encoder_after_the_hub_listener_is_gone(tmp_path: Path):
    """Closing clears the hub set before detach. The next open must not 429 on that stale listener."""

    script = _write_script(tmp_path, _CONTINUOUS_FFMPEG)
    hub = _hub(
        tmp_path,
        ffmpeg=script,
        max_video_encoders=1,
        max_video_per_ip=1,
        video_linger=30,
        output_stall=30,
    )
    held = {"x-real-ip": "198.51.100.90"}
    with TestClient(create_app(live=hub)) as client:
        with _ws(client, "/api/live/publish?id=SWCA", headers={"x-real-ip": "192.0.2.90"}) as first:
            _publish_ready(first)
            with _ws(client, "/api/live/publish?id=SWCB", headers={"x-real-ip": "192.0.2.91"}) as second:
                _publish_ready(second)
                with _Audio(client, "/stream/SWCA", query="thumbnail=1", headers=held) as video:
                    assert video.read()[:1] == b"\x47"
                    stream = hub.get("SWCA")
                    relay = stream.relay
                    assert relay.listeners
                    stream.video_listeners.clear()
                    with _Audio(client, "/stream/SWCB", query="thumbnail=1", headers=held) as moved:
                        assert moved.status_code == 200
                        assert moved.read()[:1] == b"\x47"
                        assert relay.occupies() is False


def test_handover_joins_off_the_event_loop(tmp_path: Path):
    moved: list[bool] = []
    ticks: dict[str, int] = {}
    script = _write_script(tmp_path, _CONTINUOUS_FFMPEG)
    hub = _hub(
        tmp_path,
        ffmpeg=script,
        max_video_encoders=1,
        max_video_per_ip=1,
        video_linger=30,
        output_stall=30,
    )
    held = {"x-real-ip": "198.51.100.92"}
    with TestClient(create_app(live=hub)) as client:
        ticks = _heartbeat(client)
        with _ws(client, "/api/live/publish?id=LOOP", headers={"x-real-ip": "192.0.2.92"}) as first:
            _publish_ready(first)
            with _ws(client, "/api/live/publish?id=LOOQ", headers={"x-real-ip": "192.0.2.93"}) as second:
                _publish_ready(second)
                with _Audio(client, "/stream/LOOP", query="thumbnail=1", headers=held) as video:
                    assert video.read()[:1] == b"\x47"
                relay = hub.get("LOOP").relay
                original = relay.finish_bundle

                def slow(bundle) -> None:
                    moved.append(_loop_was_free(ticks))
                    original(bundle)

                relay.finish_bundle = slow
                with _deadline(5):
                    with _Audio(client, "/stream/LOOQ", query="thumbnail=1", headers=held) as moved_video:
                        assert moved_video.status_code == 200
                        assert moved_video.read()[:1] == b"\x47"
    assert moved == [True]


class _QuietProc:
    """A process stand-in. poll() is already finished, so a mistaken reap does not signal anyone."""

    def __init__(self, stdout=None) -> None:
        self.stdout = stdout
        self.stdin = None
        self.stderr = None
        self.pid = 1
        self.returncode = 0

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0


def _point_at(relay: FfmpegRelay, proc) -> _Mux:
    mux = _Mux()
    mux.proc = proc
    relay._mux = mux
    relay._proc = proc
    relay._stop = mux.stop
    relay._started = True
    relay._backoff_until = 0.0
    return mux


def _read_past_a_swap(monkeypatch, payload: bytes) -> tuple[FfmpegRelay, _QuietProc]:
    """Block the old read loop, install the next process, then let the old read return."""

    relay = FfmpegRelay("RACE", lambda: (), ThumbCache(lambda _video: None), restart_backoff=1.0)
    read_fd, write_fd = os.pipe()
    stdout = os.fdopen(read_fd, "rb", buffering=0)
    old = _QuietProc(stdout)
    mux = _point_at(relay, old)
    entered = threading.Event()
    release = threading.Event()
    real_read = os.read

    def gated(fd, size):
        if fd == stdout.fileno():
            entered.set()
            if not release.wait(2):
                raise AssertionError("the old read was not released")
            return payload
        return real_read(fd, size)

    monkeypatch.setattr(os, "read", gated)
    thread = threading.Thread(target=relay._read_loop, args=(mux,), daemon=True)
    newer = _QuietProc()
    try:
        thread.start()
        assert entered.wait(2)
        # The handover has set this generation's flag. It is not cleared.
        # The next viewer already owns a different process.
        mux.stop.set()
        _point_at(relay, newer)
        release.set()
        thread.join(2)
        assert thread.is_alive() is False
    finally:
        release.set()
        thread.join(2)
        stdout.close()
        os.close(write_fd)
    return relay, newer


def test_a_departing_read_loop_does_not_fail_the_next_encoder(monkeypatch):
    """EOF from the process just stopped must not set backoff on the one that replaced it.

    The old loop used to call ``_fail()`` with no process of its own. That killed
    the ffmpeg the next viewer had just started, and the body came back empty.
    """

    relay, newer = _read_past_a_swap(monkeypatch, b"")
    assert relay._proc is newer
    assert relay._started is True
    assert relay._backoff_until == 0.0


def test_a_departing_read_loop_does_not_emit_into_the_next_encoder(monkeypatch):
    packet = b"\x47" + b"\x00" * 187
    relay, newer = _read_past_a_swap(monkeypatch, packet)
    assert relay._proc is newer
    assert relay._started is True
    assert relay._backoff_until == 0.0
    assert relay.sync.packets == []


def test_a_departing_watch_loop_does_not_fail_the_next_encoder():
    """The old watch period can elapse after the next process is installed.

    The captured process has already exited. Failing it must leave the new one running.
    """

    relay = FfmpegRelay("WATC", lambda: (), ThumbCache(lambda _video: None), restart_backoff=1.0)
    relay._clock = lambda: 0.0
    relay._last_output = 0.0

    class _Dead:
        def poll(self):
            return 1

        def wait(self, timeout=None):
            return 1

    old = _Dead()
    newer = _QuietProc()
    mux = _point_at(relay, old)

    def wait(timeout=None):
        _point_at(relay, newer)
        mux.stop.wait = lambda timeout=None: True
        return False

    mux.stop.wait = wait
    relay._watch_loop(mux)
    assert relay._proc is newer
    assert relay._started is True
    assert relay._backoff_until == 0.0


class _Held:
    def __init__(self, address: str) -> None:
        self.address = address

    def offer_end(self) -> None:
        return

    def mark_closed(self) -> None:
        return


def test_a_switch_waits_until_the_previous_viewer_detaches(tmp_path: Path):
    """The old response's drop_video can land a few milliseconds after the next GET.

    Recounting for that long lets the switch through. HEAD does not wait.
    """

    script = _write_script(tmp_path, _CONTINUOUS_FFMPEG)
    hub = _hub(
        tmp_path,
        ffmpeg=script,
        max_video_encoders=1,
        max_video_per_ip=1,
        video_linger=30,
        output_stall=30,
    )
    held = {"x-real-ip": "198.51.100.41"}
    with TestClient(create_app(live=hub)) as client:
        with _ws(client, "/api/live/publish?id=DRNA", headers={"x-real-ip": "192.0.2.41"}) as first:
            _publish_ready(first)
            with _ws(client, "/api/live/publish?id=DRNB", headers={"x-real-ip": "192.0.2.42"}) as second:
                _publish_ready(second)
                with _Audio(client, "/stream/DRNA", query="thumbnail=1", headers=held) as video:
                    assert video.read()[:1] == b"\x47"
                    stream = hub.get("DRNA")
                    relay = stream.relay
                    listener = next(iter(stream.video_listeners))
                    started = time.monotonic()
                    denied = client.head("/stream/DRNB?thumbnail=1", headers=held)
                    elapsed = time.monotonic() - started
                    assert denied.status_code == 429
                    assert elapsed < 0.2
                    errors: list[BaseException] = []

                    def leave() -> None:
                        try:
                            time.sleep(0.05)
                            hub.drop_video(stream, listener, relay)
                        except BaseException as exc:
                            errors.append(exc)

                    leaver = threading.Thread(target=leave)
                    leaver.start()
                    try:
                        with _deadline(5):
                            with _Audio(client, "/stream/DRNB", query="thumbnail=1", headers=held) as moved:
                                assert moved.status_code == 200
                                assert moved.read()[:1] == b"\x47"
                                assert relay.occupies() is False
                                assert hub.get("DRNB").relay is not relay
                                assert hub.get("DRNB").relay.occupies()
                    finally:
                        leaver.join(2)
                    assert errors == []


def test_rejoin_clears_video_listeners_before_the_id_is_sent(tmp_path: Path):
    """V20: reclaim empties the hub set before offer_end, and the same address can watch the new relay."""

    script = _write_script(tmp_path, _CONTINUOUS_FFMPEG)
    hub = _hub(
        tmp_path,
        ffmpeg=script,
        max_listeners_per_ip=1,
        video_linger=30,
        output_stall=30,
    )
    viewer = {"x-real-ip": "198.51.100.43"}
    found: dict[str, object] = {}
    original = hub._open

    def wrapped(requested, token, address, seq=None, wait_key=None):
        result = original(requested, token, address, seq, wait_key)
        stream = result[0]
        video_ended = result[5]
        if video_ended:
            found["empty"] = len(stream.video_listeners) == 0
            found["ended"] = len(video_ended)
        return result

    hub._open = wrapped
    with TestClient(create_app(live=hub)) as client:
        with _ws(client, "/api/live/publish?id=VLNA", headers={"x-real-ip": "192.0.2.43"}) as old:
            token = _id_token(old.receive_json(), "VLNA")
            old.send_json({"type": "mime", "mime": "audio/webm"})
            cluster = cluster_known(0, b"wave")
            head, _data = document([cluster])
            old.send_bytes(head + cluster)
            with _Audio(client, "/stream/VLNA", query="thumbnail=1", headers=viewer) as video:
                assert video.read()[:1] == b"\x47"
                relay = hub.get("VLNA").relay
                assert relay is not None and relay.occupies()
                with _ws(client, _with_token("VLNA", token), seq=2) as new:
                    assert _id_token(new.receive_json(), "VLNA") == token
                    assert found == {"empty": True, "ended": 1}
                    deadline = time.monotonic() + 2
                    while time.monotonic() < deadline:
                        if video.read() is None:
                            break
                    else:
                        raise AssertionError("the old viewer was not ended")
                    with _Audio(client, "/stream/VLNA", query="thumbnail=1", headers=viewer) as again:
                        assert again.status_code == 200
                        assert again.read()[:1] == b"\x47"
                        fresh = hub.get("VLNA").relay
                        assert fresh is not None and fresh is not relay and fresh.occupies()


def test_drop_video_detaches_the_relay_the_response_opened(tmp_path: Path):
    """V28: a late close detaches the relay it opened, not whatever the stream points at now."""

    hub = _hub(tmp_path)

    class _Relay:
        def __init__(self) -> None:
            self.detached: list[object] = []

        def detach(self, listener) -> None:
            self.detached.append(listener)

    class _Stream:
        def __init__(self) -> None:
            self.video_listeners: set = set()
            self.relay = None

    listener = object()
    opened = _Relay()
    current = _Relay()
    stream = _Stream()
    stream.video_listeners.add(listener)
    stream.relay = current
    hub.drop_video(stream, listener, relay=opened)
    assert opened.detached == [listener]
    assert current.detached == []
    assert listener not in stream.video_listeners


def test_a_viewer_arriving_before_the_stop_keeps_the_encoder(tmp_path: Path):
    """Q5: the victim is chosen, then someone is listening. The stop is skipped and the new open is 429."""

    script = _write_script(tmp_path, _CONTINUOUS_FFMPEG)
    hub = _hub(
        tmp_path,
        ffmpeg=script,
        max_video_encoders=1,
        max_video_per_ip=1,
        video_linger=30,
        output_stall=30,
    )
    held = {"x-real-ip": "198.51.100.45"}
    original = hub.stop_handover

    def wrapped(relays):
        victim = next(item for item in hub._streams.values() if item.relay in relays)
        victim.video_listeners.add(_Held("203.0.113.45"))
        return original(relays)

    hub.stop_handover = wrapped
    with TestClient(create_app(live=hub)) as client:
        with _ws(client, "/api/live/publish?id=QAAA", headers={"x-real-ip": "192.0.2.45"}) as first:
            _publish_ready(first)
            with _ws(client, "/api/live/publish?id=QAAB", headers={"x-real-ip": "192.0.2.46"}) as second:
                _publish_ready(second)
                with _Audio(client, "/stream/QAAA", query="thumbnail=1", headers=held) as video:
                    assert video.read()[:1] == b"\x47"
                lingering = hub.get("QAAA").relay
                assert lingering.occupies()
                with _deadline(5):
                    denied = client.get("/stream/QAAB?thumbnail=1", headers=held)
                assert denied.status_code == 429
                assert lingering.occupies()


def test_handover_is_not_offered_when_another_hold_keeps_the_global_cap(tmp_path: Path):
    """H6: stopping this linger would still leave the global cap full, so it is not a victim."""

    script = _write_script(tmp_path, _CONTINUOUS_FFMPEG)
    hub = _hub(
        tmp_path,
        ffmpeg=script,
        max_video_encoders=1,
        max_video_per_ip=1,
        video_linger=30,
        output_stall=30,
    )
    held = "198.51.100.47"
    with TestClient(create_app(live=hub)) as client:
        with _ws(client, "/api/live/publish?id=HPAA", headers={"x-real-ip": "192.0.2.47"}) as first:
            _publish_ready(first)
            with _ws(client, "/api/live/publish?id=HPAB", headers={"x-real-ip": "192.0.2.48"}) as second:
                _publish_ready(second)
                with _ws(client, "/api/live/publish?id=HPAC", headers={"x-real-ip": "192.0.2.49"}) as third:
                    _publish_ready(third)
                    with _Audio(client, "/stream/HPAA", query="thumbnail=1", headers={"x-real-ip": held}) as video:
                        assert video.read()[:1] == b"\x47"
                    lingering = hub.get("HPAA").relay
                    assert lingering.occupies()
                    hub.get("HPAC").video_hold = True
                    planned = hub.prepare_video("HPAB", held)
                    assert isinstance(planned, tuple)
                    assert planned[0] == 429
                    assert lingering.occupies()


def test_handover_is_not_offered_when_another_listener_keeps_the_per_ip_cap(tmp_path: Path):
    """H6b: stopping this linger would still leave this address at its per-IP cap."""

    script = _write_script(tmp_path, _CONTINUOUS_FFMPEG)
    hub = _hub(
        tmp_path,
        ffmpeg=script,
        max_video_encoders=3,
        max_video_per_ip=1,
        video_linger=30,
        output_stall=30,
    )
    held = "198.51.100.51"
    with TestClient(create_app(live=hub)) as client:
        with _ws(client, "/api/live/publish?id=HBAA", headers={"x-real-ip": "192.0.2.51"}) as first:
            _publish_ready(first)
            with _ws(client, "/api/live/publish?id=HBAB", headers={"x-real-ip": "192.0.2.52"}) as second:
                _publish_ready(second)
                with _ws(client, "/api/live/publish?id=HBAC", headers={"x-real-ip": "192.0.2.53"}) as third:
                    _publish_ready(third)
                    with _Audio(client, "/stream/HBAA", query="thumbnail=1", headers={"x-real-ip": held}) as video:
                        assert video.read()[:1] == b"\x47"
                    lingering = hub.get("HBAA").relay
                    assert lingering.occupies()
                    other = hub.get("HBAC")
                    other.video_hold = True
                    other.video_listeners.add(_Held(held))
                    planned = hub.prepare_video("HBAB", held)
                    assert isinstance(planned, tuple)
                    assert planned[0] == 429
                    assert lingering.occupies()


class _CountedStdin:
    def __init__(self, raw, writers: list[int]) -> None:
        self._raw = raw
        self._writers = writers

    def write(self, data: bytes) -> None:
        self._writers.append(threading.get_ident())
        self._raw.write(data)

    def close(self) -> None:
        self._raw.close()

    def fileno(self) -> int:
        return self._raw.fileno()


class _PipeProc:
    """A stand-in for ffmpeg. poll() stays empty so the watch loop does not reap the test."""

    def __init__(self) -> None:
        self._sleeper = subprocess.Popen(["sleep", "60"])
        in_r, in_w = os.pipe()
        out_r, out_w = os.pipe()
        err_r, err_w = os.pipe()
        self.writers: list[int] = []
        self.stdin = _CountedStdin(os.fdopen(in_w, "wb", buffering=0), self.writers)
        self.stdout = os.fdopen(out_r, "rb", buffering=0)
        self.stderr = os.fdopen(err_r, "rb", buffering=0)
        self._out_w = out_w
        self._err_w = err_w
        self.pid = self._sleeper.pid
        self.returncode = None
        self._dead = False
        threading.Thread(target=_drain_fd, args=(in_r,), daemon=True).start()

    def poll(self):
        return 0 if self._dead else None

    def wait(self, timeout=None):
        self.returncode = 0
        return 0

    def close_writes(self) -> None:
        for fd in (self._out_w, self._err_w):
            if fd is None or fd < 0:
                continue
            try:
                os.close(fd)
            except OSError:
                pass
        if self._out_w is not None:
            self._out_w = -1
        if self._err_w is not None:
            self._err_w = -1

    def kill(self) -> None:
        if self._sleeper.poll() is None:
            self._sleeper.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            self._sleeper.wait(timeout=1)


def _drain_fd(fd: int) -> None:
    try:
        while os.read(fd, 65536):
            pass
    except OSError:
        pass
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)


class _Seen:
    def __init__(self) -> None:
        self.saw_init = False
        self.chunks: list[bytes] = []
        self.address = "198.51.100.1"

    def offer_init(self, payload: bytes) -> None:
        self.saw_init = True
        self.chunks.append(payload)

    def offer_media(self, payload: bytes) -> None:
        self.chunks.append(payload)

    def offer_end(self) -> None:
        return

    def mark_closed(self) -> None:
        return


def _ts(marker: bytes) -> bytes:
    packet = bytearray(188)
    packet[0] = 0x47
    packet[4 : 4 + len(marker)] = marker
    return bytes(packet)


def test_two_generations_overlap_on_the_threads_the_relay_started(monkeypatch):
    """The threads _start_locked creates keep the process and stop flag they were given.

    A later generation must not clear that flag. A read still leaving the first
    process must not append into the next one or fail it. The watch loop, awake
    after that replacement, must not fail the process it did not capture. The
    audio loop, already holding a cluster, must not publish that cluster's base
    onto the generation that replaced it.
    """

    import djtube.video as video_mod

    procs: list[_PipeProc] = []
    fails: list[tuple[str, object]] = []
    release = threading.Event()
    watch_release = threading.Event()
    watch_entered = threading.Event()
    audio_release = threading.Event()
    audio_entered = threading.Event()

    class _Event(threading.Event):
        def wait(self, timeout=None):
            # The first watch period ends only after the next generation exists,
            # so the loop has to notice that its captured process is gone.
            if threading.current_thread().name.startswith("djtube-video-watch-") and not watch_entered.is_set():
                watch_entered.set()
                assert watch_release.wait(3)
                return False
            return super().wait(timeout)

    monkeypatch.setattr(video_mod.threading, "Event", _Event)

    def popen(*_args, **kwargs) -> _PipeProc:
        proc = _PipeProc()
        # The parent closes this fd after Popen returns. Keep a copy so the
        # audio loop can write the generation it captured.
        passed = kwargs.get("pass_fds") or ()
        if passed:
            duped = os.dup(passed[0])
            threading.Thread(target=_drain_fd, args=(duped,), daemon=True).start()
        procs.append(proc)
        return proc

    relay = FfmpegRelay(
        "GENR",
        lambda: (),
        ThumbCache(lambda _video: None),
        width=16,
        height=16,
        fps=5,
        output_stall=30,
        restart_backoff=1,
        popen=popen,
    )
    real_fail = relay._fail

    def spy_fail(proc=None):
        fails.append((threading.current_thread().name, proc))
        return real_fail(proc)

    relay._fail = spy_fail

    _real_tc = video_mod.cluster_timecode

    def blocked_tc(item: bytes):
        # Past the stop check, before the mux identity check. The swap happens
        # while this cluster is still in the first generation's hands.
        if not audio_entered.is_set():
            audio_entered.set()
            assert audio_release.wait(3)
        return _real_tc(item)

    monkeypatch.setattr(video_mod, "cluster_timecode", blocked_tc)
    listener = _Seen()
    entered = threading.Event()
    calls = {"n": 0}
    original = relay._frame

    def blocked_frame() -> bytes:
        calls["n"] += 1
        if calls["n"] == 1:
            entered.set()
            assert release.wait(2)
        return original()

    relay._frame = blocked_frame
    first = _ts(b"FIRST")
    stale = _ts(b"STALE")
    # Timecode 5. The departing audio loop must not store it on the next generation.
    cluster = bytes.fromhex("1F43B67583E78105")
    try:
        relay.prime(b"INIT", cluster)
        relay.attach(listener)
        assert entered.wait(2)
        assert watch_entered.wait(2)
        assert audio_entered.wait(2)
        assert len(procs) == 1
        gen1 = procs[0]
        gen1_mux = relay._mux
        gen1_stop = relay._stop
        gen1_threads = list(relay._threads)
        gen1_frame = next(thread for thread in gen1_threads if "-frame-" in thread.name)
        assert gen1_frame.ident is not None
        assert [thread._args for thread in gen1_threads] == [(gen1_mux,)] * 5
        assert all(thread.is_alive() for thread in gen1_threads)
        os.write(gen1._out_w, first)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not any(b"FIRST" in chunk for chunk in listener.chunks):
            time.sleep(0.01)
        assert any(b"FIRST" in chunk for chunk in listener.chunks)
        with relay._lock:
            bundle = relay._take_process_locked()
            assert len(bundle[3]) == 5
            relay._start_locked()
        gen2 = procs[1]
        assert relay._proc is gen2
        assert relay._mux is not gen1_mux
        assert relay._stop is not gen1_stop
        assert gen1_stop.is_set()
        assert relay._stop.is_set() is False
        assert [thread._args for thread in relay._threads] == [(relay._mux,)] * 5
        gen1._dead = True
        watch_release.set()
        audio_release.set()
        os.write(gen1._out_w, stale)
        gen1.close_writes()
        read = next(thread for thread in gen1_threads if "-read-" in thread.name)
        read.join(2)
        assert read.is_alive() is False
        assert b"STALE" not in relay.sync.snapshot()
        release.set()
        for role in ("-audio-", "-frame-", "-watch-", "-err-"):
            thread = next(item for item in gen1_threads if role in item.name)
            thread.join(2)
            assert thread.is_alive() is False, role
        assert relay._base is None
        assert not any(name.startswith("djtube-video-watch") for name, _proc in fails)
        assert gen1_frame.ident not in gen2.writers
        assert relay._proc is gen2
        assert relay._started is True
        assert relay._backoff_until == 0.0
    finally:
        watch_release.set()
        audio_release.set()
        release.set()
        for proc in procs:
            proc.close_writes()
        with contextlib.suppress(Exception):
            relay.stop()
        for thread in list(relay._threads):
            thread.join(timeout=1)
        for proc in procs:
            proc.kill()


def _video_get_time(client, stream_id: str, headers: dict[str, str]) -> tuple[int, float]:
    started = time.monotonic()
    response = client.get(f"/stream/{stream_id}?thumbnail=1", headers=headers)
    return response.status_code, time.monotonic() - started


def test_an_unrelated_address_is_refused_without_waiting(tmp_path: Path):
    script = _write_script(tmp_path, _CONTINUOUS_FFMPEG)
    hub = _hub(tmp_path, ffmpeg=script, max_video_encoders=1, max_video_per_ip=1, video_linger=30, output_stall=30)
    with TestClient(create_app(live=hub)) as client:
        with _ws(client, "/api/live/publish?id=WAAA", headers={"x-real-ip": "192.0.2.11"}) as first:
            _publish_ready(first)
            with _ws(client, "/api/live/publish?id=WAAB", headers={"x-real-ip": "192.0.2.12"}) as second:
                _publish_ready(second)
                with _Audio(
                    client, "/stream/WAAA", query="thumbnail=1", headers={"x-real-ip": "198.51.100.11"}
                ) as video:
                    assert video.read()[:1] == b"\x47"
                    status, elapsed = _video_get_time(client, "WAAB", {"x-real-ip": "198.51.100.12"})
                    assert status == 429
                    assert elapsed < 0.2


def test_a_sole_viewer_who_stays_is_refused_after_the_drain(tmp_path: Path):
    script = _write_script(tmp_path, _CONTINUOUS_FFMPEG)
    hub = _hub(tmp_path, ffmpeg=script, max_video_encoders=1, max_video_per_ip=1, video_linger=30, output_stall=30)
    held = {"x-real-ip": "198.51.100.13"}
    with TestClient(create_app(live=hub)) as client:
        with _ws(client, "/api/live/publish?id=WABA", headers={"x-real-ip": "192.0.2.13"}) as first:
            _publish_ready(first)
            with _ws(client, "/api/live/publish?id=WABB", headers={"x-real-ip": "192.0.2.14"}) as second:
                _publish_ready(second)
                with _Audio(client, "/stream/WABA", query="thumbnail=1", headers=held) as video:
                    assert video.read()[:1] == b"\x47"
                    started = time.monotonic()
                    preview = client.head("/stream/WABB?thumbnail=1", headers=held)
                    assert preview.status_code == 429
                    assert time.monotonic() - started < 0.2
                    with _deadline(5):
                        status, elapsed = _video_get_time(client, "WABB", held)
                    assert status == 429
                    assert 0.25 <= elapsed < 0.8


def test_two_viewers_on_one_address_are_refused_without_waiting(tmp_path: Path):
    """The other connection behind this NAT address is still watching. Recounting cannot help."""

    script = _write_script(tmp_path, _CONTINUOUS_FFMPEG)
    hub = _hub(tmp_path, ffmpeg=script, max_video_encoders=3, max_video_per_ip=1, video_linger=30, output_stall=30)
    nat = {"x-real-ip": "198.51.100.15"}
    with TestClient(create_app(live=hub)) as client:
        with _ws(client, "/api/live/publish?id=NTAA", headers={"x-real-ip": "192.0.2.15"}) as first:
            _publish_ready(first)
            with _ws(client, "/api/live/publish?id=NTAB", headers={"x-real-ip": "192.0.2.16"}) as second:
                _publish_ready(second)
                with _Audio(client, "/stream/NTAA", query="thumbnail=1", headers=nat) as one:
                    assert one.read()[:1] == b"\x47"
                    with _Audio(client, "/stream/NTAA", query="thumbnail=1", headers=nat) as two:
                        assert two.read()[:1] == b"\x47"
                        assert len(hub.get("NTAA").video_listeners) == 2
                        status, elapsed = _video_get_time(client, "NTAB", nat)
                        assert status == 429
                        assert elapsed < 0.2


def test_a_second_viewer_from_another_address_is_refused_without_waiting(tmp_path: Path):
    script = _write_script(tmp_path, _CONTINUOUS_FFMPEG)
    hub = _hub(tmp_path, ffmpeg=script, max_video_encoders=3, max_video_per_ip=1, video_linger=30, output_stall=30)
    held = {"x-real-ip": "198.51.100.17"}
    other = {"x-real-ip": "198.51.100.18"}
    with TestClient(create_app(live=hub)) as client:
        with _ws(client, "/api/live/publish?id=WACA", headers={"x-real-ip": "192.0.2.17"}) as first:
            _publish_ready(first)
            with _ws(client, "/api/live/publish?id=WACB", headers={"x-real-ip": "192.0.2.18"}) as second:
                _publish_ready(second)
                with _Audio(client, "/stream/WACA", query="thumbnail=1", headers=held) as one:
                    assert one.read()[:1] == b"\x47"
                    with _Audio(client, "/stream/WACA", query="thumbnail=1", headers=other) as two:
                        assert two.read()[:1] == b"\x47"
                        status, elapsed = _video_get_time(client, "WACB", held)
                        assert status == 429
                        assert elapsed < 0.2


def test_reopening_while_someone_else_watches_waits_then_refuses(tmp_path: Path):
    """H7: A leaves S1, B watches it, A watches S2, then A opens S1 again."""

    script = _write_script(tmp_path, _CONTINUOUS_FFMPEG)
    hub = _hub(tmp_path, ffmpeg=script, max_video_encoders=3, max_video_per_ip=1, video_linger=30, output_stall=30)
    starter = {"x-real-ip": "198.51.100.19"}
    guest = {"x-real-ip": "198.51.100.20"}
    with TestClient(create_app(live=hub)) as client:
        with _ws(client, "/api/live/publish?id=HSAA", headers={"x-real-ip": "192.0.2.19"}) as first:
            _publish_ready(first)
            with _ws(client, "/api/live/publish?id=HSAB", headers={"x-real-ip": "192.0.2.20"}) as second:
                _publish_ready(second)
                with _Audio(client, "/stream/HSAA", query="thumbnail=1", headers=starter) as started_video:
                    assert started_video.read()[:1] == b"\x47"
                assert hub.get("HSAA").relay.occupies()
                with _Audio(client, "/stream/HSAA", query="thumbnail=1", headers=guest) as watching:
                    assert watching.read()[:1] == b"\x47"
                    with _Audio(client, "/stream/HSAB", query="thumbnail=1", headers=starter) as other:
                        assert other.status_code == 200
                        assert other.read()[:1] == b"\x47"
                        with _deadline(5):
                            status, elapsed = _video_get_time(client, "HSAA", starter)
                        assert status == 429
                        assert 0.25 <= elapsed < 0.8


def test_a_lingering_encoder_with_no_viewers_is_refused_without_waiting(tmp_path: Path):
    """N5: an empty linger is not one viewer about to leave. Recounting will not free the slot."""

    script = _write_script(tmp_path, _CONTINUOUS_FFMPEG)
    hub = _hub(tmp_path, ffmpeg=script, max_video_encoders=1, max_video_per_ip=1, video_linger=30, output_stall=30)
    other = {"x-real-ip": "198.51.100.23"}
    with TestClient(create_app(live=hub)) as client:
        with _ws(client, "/api/live/publish?id=LZAA", headers={"x-real-ip": "192.0.2.22"}) as first:
            _publish_ready(first)
            with _ws(client, "/api/live/publish?id=LZAB", headers={"x-real-ip": "192.0.2.23"}) as second:
                _publish_ready(second)
                with _Audio(
                    client, "/stream/LZAA", query="thumbnail=1", headers={"x-real-ip": "198.51.100.22"}
                ) as video:
                    assert video.read()[:1] == b"\x47"
                lingering = hub.get("LZAA")
                assert lingering.relay.occupies()
                assert len(lingering.video_listeners) == 0
                status, elapsed = _video_get_time(client, "LZAB", other)
                assert status == 429
                assert elapsed < 0.2


def test_opening_the_stream_this_address_already_watches_does_not_wait(tmp_path: Path):
    """W5: the only viewer with this address is on the stream being opened."""

    script = _write_script(tmp_path, _CONTINUOUS_FFMPEG)
    hub = _hub(
        tmp_path,
        ffmpeg=script,
        max_listeners_per_ip=1,
        max_video_encoders=1,
        max_video_per_ip=1,
        video_linger=30,
        output_stall=30,
    )
    held = {"x-real-ip": "198.51.100.24"}
    with TestClient(create_app(live=hub)) as client:
        with _ws(client, "/api/live/publish?id=WSAA", headers={"x-real-ip": "192.0.2.24"}) as publisher:
            _publish_ready(publisher)
            with _Audio(client, "/stream/WSAA", query="thumbnail=1", headers=held) as video:
                assert video.read()[:1] == b"\x47"
                assert len(hub.get("WSAA").video_listeners) == 1
                status, elapsed = _video_get_time(client, "WSAA", held)
                assert status == 429
                assert elapsed < 0.2
