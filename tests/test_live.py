from __future__ import annotations

import array
import asyncio
import contextlib
import logging
import math
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import quote

import anyio
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from djtube.app import create_app
from djtube.live import (
    CODE_BAD_ID,
    CODE_BAD_MEDIA,
    CODE_FULL,
    CODE_RATE,
    CODE_REPLACED,
    CODE_TAKEN,
    CODE_TIMEOUT,
    CODE_TOO_BIG,
    RESERVATION_MAX,
    RESERVATION_PER_IP,
    RESERVATION_TTL,
    BURST_SECONDS,
    IDLE_TIMEOUT,
    INIT_TIMEOUT,
    MAX_BYTES_PER_SECOND,
    MAX_CLUSTER,
    MAX_FRAME,
    MAX_PER_IP,
    MAX_STREAMS,
    DEFAULT_MIME,
    MAX_LISTENERS,
    MAX_LISTENERS_PER_IP,
    MAX_LISTENERS_TOTAL,
    LiveHub,
    _AudioResponse,
    _Listener,
    is_stream_id,
)
from djtube.paths import PUBLIC_PREFIX, STATIC_DIR
from djtube.webm import cluster_timecode, split_webm
from tests.test_webm import cluster_known, document

ROOT = Path(__file__).resolve().parents[1]


def _id_token(payload: dict, stream_id: str) -> str:
    assert payload["type"] == "id"
    assert payload["id"] == stream_id
    token = payload["token"]
    assert isinstance(token, str) and token
    assert token.strip() == token
    return token


def _with_token(stream_id: str, token: str) -> str:
    return f"/api/live/publish?id={stream_id}&token={quote(token, safe='')}"


def _slices(data: bytes) -> list[bytes]:
    sizes = (7, 13, 64, 200, 480)
    pieces = []
    index = 0
    step = 0
    while index < len(data):
        size = sizes[step % len(sizes)]
        pieces.append(data[index : index + size])
        index += size
        step += 1
    return pieces


def _send(socket, data: bytes) -> None:
    for piece in _slices(data):
        socket.send_bytes(piece)


def _tone(seconds: list[tuple[float, float]] | None = None) -> bytes:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg is required to prove the joined audio plays")
    from djtube.live_tone import render_webm

    return render_webm(seconds or [(440, 2.0), (880, 2.0)])


def _decode(webm: bytes) -> array.array:
    result = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            "pipe:0",
            "-f",
            "s16le",
            "-ac",
            "1",
            "-ar",
            "48000",
            "pipe:1",
        ],
        input=webm,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode()[-400:]
    samples = array.array("h")
    samples.frombytes(result.stdout)
    return samples


def _dominant(samples: array.array, skip: float, take: float = 0.35, rate: int = 48000) -> float:
    """Whichever of 440 Hz and 880 Hz is louder in this window."""

    start = int(skip * rate)
    window = samples[start : start + int(take * rate)]
    assert len(window) > rate // 5

    def power(frequency: float) -> float:
        bins = len(window)
        omega = 2 * math.pi * int(0.5 + bins * frequency / rate) / bins
        coeff = 2 * math.cos(omega)
        first = second = 0.0
        for sample in window:
            first, second = sample + coeff * first - second, first
        return first * first + second * second - coeff * first * second

    low = power(440)
    high = power(880)
    return 880.0 if high > low else 440.0


class _Audio:
    """One chunked GET, read while a publisher socket on the same TestClient stays open."""

    def __init__(self, client: TestClient, path: str, headers: dict[str, str] | None = None) -> None:
        self._client = client
        self._path = path
        self._extra = headers or {}
        self.status_code = 0
        self.headers: dict[str, str] = {}

    def __enter__(self) -> _Audio:
        portal = self._client.portal
        assert portal is not None
        self._portal = portal
        self._requested = False
        future, scope = portal.start_task(self._run)
        self._future = future
        self._cancel = scope
        started = self._receive()
        assert started["type"] == "http.response.start", started
        self.status_code = int(started["status"])
        self.headers = {key.decode().lower(): value.decode() for key, value in started.get("headers", [])}
        return self

    def __exit__(self, *_args: object) -> None:
        with contextlib.suppress(Exception):
            self._portal.call(self._receive_tx.send, {"type": "http.disconnect"})
        self._portal.call(self._cancel.cancel)
        self._future.result()

    async def _run(self, *, task_status: anyio.abc.TaskStatus[anyio.CancelScope]) -> None:
        send_tx, send_rx = anyio.create_memory_object_stream(math.inf)
        receive_tx, receive_rx = anyio.create_memory_object_stream(math.inf)
        self._receive_tx = receive_tx
        self._send_rx = send_rx
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": self._path,
            "raw_path": self._path.encode(),
            "query_string": b"",
            "headers": [(key.lower().encode("latin-1"), value.encode("latin-1")) for key, value in self._extra.items()],
            "client": ("127.0.0.1", 50000),
            "server": ("testserver", 80),
            "root_path": "",
            "state": {},
        }
        with send_tx, send_rx, receive_tx, receive_rx, anyio.CancelScope() as cancel:
            task_status.started(cancel)

            async def receive() -> dict:
                if not self._requested:
                    self._requested = True
                    return {"type": "http.request", "body": b"", "more_body": False}
                return await receive_rx.receive()

            await self._client.app(scope, receive, send_tx.send)
            await anyio.sleep_forever()

    def _receive(self) -> dict:
        return self._portal.call(self._send_rx.receive)

    def read(self) -> bytes | None:
        message = self._receive()
        assert message["type"] == "http.response.body", message
        if not message.get("more_body", False):
            return None
        body = message.get("body", b"")
        assert isinstance(body, bytes)
        return body


def test_ids_are_four_uppercase_letters_and_collisions_are_rejected():
    app = create_app()
    with TestClient(app) as client:
        with client.websocket_connect("/api/live/publish") as first:
            first_id = first.receive_json()["id"]
            assert is_stream_id(first_id)
            with client.websocket_connect("/api/live/publish") as second:
                second_id = second.receive_json()["id"]
            assert is_stream_id(second_id)
            assert second_id != first_id
        with pytest.raises(WebSocketDisconnect) as bad:
            with client.websocket_connect("/api/live/publish?id=ab") as socket:
                socket.receive_json()
        assert bad.value.code == CODE_BAD_ID
        with client.websocket_connect("/djtube/api/live/publish?id=ABCD") as claimed:
            token = _id_token(claimed.receive_json(), "ABCD")
            with pytest.raises(WebSocketDisconnect) as taken:
                with client.websocket_connect("/api/live/publish?id=ABCD") as other:
                    other.receive_json()
            assert taken.value.code == CODE_TAKEN
            with pytest.raises(WebSocketDisconnect) as wrong:
                with client.websocket_connect(_with_token("ABCD", "not-the-token")) as other:
                    other.receive_json()
            assert wrong.value.code == CODE_TAKEN
        with pytest.raises(WebSocketDisconnect) as reserved:
            with client.websocket_connect("/api/live/publish?id=ABCD") as again:
                again.receive_json()
        assert reserved.value.code == CODE_TAKEN
        with client.websocket_connect(_with_token("ABCD", token)) as again:
            assert _id_token(again.receive_json(), "ABCD") == token
        with pytest.raises(WebSocketDisconnect) as lower:
            with client.websocket_connect("/api/live/publish?id=abcd") as socket:
                socket.receive_json()
        assert lower.value.code == CODE_BAD_ID


def test_full_hub_and_full_listener_list_refuse_another_connection():
    hub = LiveHub(max_streams=1, max_listeners=1)
    with TestClient(create_app(live=hub)) as client:
        with client.websocket_connect("/api/live/publish") as publisher:
            stream_id = publisher.receive_json()["id"]
            with pytest.raises(WebSocketDisconnect) as full:
                with client.websocket_connect("/api/live/publish") as extra:
                    extra.receive_json()
            assert full.value.code == CODE_FULL
            with _Audio(client, f"/stream/{stream_id}") as listener:
                assert listener.status_code == 200
                overflow = client.get(f"{PUBLIC_PREFIX}/stream/{stream_id}")
                assert overflow.status_code == 429
                assert overflow.headers["cache-control"] == "no-store"


def test_one_source_may_listen_only_so_many_times_on_one_stream():
    hub = LiveHub(max_listeners_per_ip=2, max_listeners=10, max_listeners_total=50, init_timeout=5, idle_timeout=5)
    same = {"x-real-ip": "203.0.113.8"}
    other = {"x-real-ip": "203.0.113.9"}
    with TestClient(create_app(live=hub)) as client:
        with client.websocket_connect("/api/live/publish?id=LIMS") as publisher:
            assert publisher.receive_json()["id"] == "LIMS"
            with _Audio(client, "/stream/LIMS", same) as first:
                with _Audio(client, "/stream/LIMS", same) as second:
                    assert first.status_code == second.status_code == 200
                    blocked = client.get("/stream/LIMS", headers=same)
                    assert blocked.status_code == 429
                    with _Audio(client, "/stream/LIMS", other) as third:
                        assert third.status_code == 200
                        stream = hub.get("LIMS")
                        assert stream is not None
                        assert len(stream.listeners) == 3
            with _Audio(client, "/stream/LIMS") as peer:
                with _Audio(client, "/stream/LIMS") as peer_again:
                    assert peer.status_code == peer_again.status_code == 200
                    with _Audio(client, "/stream/LIMS", {"x-forwarded-for": "198.51.100.50"}) as spoofed:
                        assert spoofed.status_code == 429
            with _Audio(client, "/stream/LIMS", same) as again:
                assert again.status_code == 200


def test_ipv6_listeners_share_a_64_on_one_stream():
    hub = LiveHub(max_listeners_per_ip=2, max_listeners=10, max_listeners_total=50, init_timeout=5, idle_timeout=5)
    with TestClient(create_app(live=hub)) as client:
        with client.websocket_connect("/api/live/publish?id=VLSA") as publisher:
            publisher.receive_json()
            first = {"x-real-ip": "2001:db8:1:2::1"}
            second = {"x-real-ip": "2001:db8:1:2:ffff::9"}
            third = {"x-real-ip": "2001:db8:1:3::1"}
            with _Audio(client, "/stream/VLSA", first) as held:
                with _Audio(client, "/stream/VLSA", second) as also:
                    assert held.status_code == also.status_code == 200
                    blocked = client.get("/stream/VLSA", headers={"x-real-ip": "2001:0db8:0001:0002::abcd"})
                    assert blocked.status_code == 429
                    with _Audio(client, "/stream/VLSA", third) as other:
                        assert other.status_code == 200


def test_listeners_across_streams_share_one_cap():
    hub = LiveHub(max_listeners_total=2, max_listeners_per_ip=10, max_listeners=10, init_timeout=5, idle_timeout=5)
    with TestClient(create_app(live=hub)) as client:
        with client.websocket_connect("/api/live/publish?id=AAAA", headers={"x-real-ip": "203.0.113.1"}) as first:
            first.receive_json()
            with client.websocket_connect("/api/live/publish?id=AAAB", headers={"x-real-ip": "203.0.113.2"}) as second:
                second.receive_json()
                with _Audio(client, "/stream/AAAA", {"x-real-ip": "198.51.100.1"}) as left:
                    with _Audio(client, "/stream/AAAB", {"x-real-ip": "198.51.100.2"}) as right:
                        assert left.status_code == right.status_code == 200
                        blocked = client.get("/stream/AAAA", headers={"x-real-ip": "198.51.100.3"})
                        assert blocked.status_code == 429
                        other = client.get("/stream/AAAB", headers={"x-real-ip": "198.51.100.4"})
                        assert other.status_code == 429
                        assert hub.get("AAAA") is not None
                        assert hub.get("AAAB") is not None
                    with _Audio(client, "/stream/AAAB", {"x-real-ip": "198.51.100.4"}) as opened:
                        assert opened.status_code == 200


def test_head_reports_a_live_stream_without_taking_a_listener():
    hub = LiveHub(max_listeners=1, init_timeout=5, idle_timeout=5)
    with TestClient(create_app(live=hub)) as client:
        missing = client.head("/stream/NONE")
        invalid = client.head("/stream/nope")
        assert missing.status_code == invalid.status_code == 404
        assert missing.content == invalid.content == b""
        with client.websocket_connect("/api/live/publish?id=LIVE") as publisher:
            publisher.receive_json()
            head = client.head("/stream/LIVE")
            prefixed = client.head(f"{PUBLIC_PREFIX}/stream/LIVE")
            for response in (head, prefixed):
                assert response.status_code == 200
                assert response.headers["content-type"] == DEFAULT_MIME
                assert response.headers["cache-control"] == "no-store"
                assert response.headers["x-accel-buffering"] == "no"
                assert "content-length" not in response.headers
                assert response.content == b""
            stream = hub.get("LIVE")
            assert stream is not None
            assert stream.listeners == set()
            with _Audio(client, "/stream/LIVE") as listener:
                assert listener.status_code == 200
                again = client.head("/stream/LIVE")
                assert again.status_code == 200
                assert again.headers["content-type"] == listener.headers["content-type"]
                assert len(stream.listeners) == 1
                blocked = client.get("/stream/LIVE")
                assert blocked.status_code == 429
                assert len(stream.listeners) == 1


def test_missing_and_invalid_ids_are_404_and_a_live_one_is_audio():
    with TestClient(create_app()) as client:
        missing = client.get("/stream/ABCD")
        prefixed = client.get(f"{PUBLIC_PREFIX}/stream/ABCD")
        invalid = client.get("/stream/nope")
        for response in (missing, prefixed, invalid):
            assert response.status_code == 404
            assert response.headers["cache-control"] == "no-store"
            assert "audio/webm" not in response.headers.get("content-type", "")
            assert response.content == b""
        with client.websocket_connect("/api/live/publish?id=LIVE") as publisher:
            assert publisher.receive_json()["id"] == "LIVE"
            with _Audio(client, "/stream/LIVE") as audio:
                assert audio.status_code == 200
                assert audio.headers["content-type"] == DEFAULT_MIME
                assert audio.headers["cache-control"] == "no-store"
                assert audio.headers["x-accel-buffering"] == "no"
                assert "content-length" not in audio.headers
        gone = client.get("/stream/LIVE")
        assert gone.status_code == 404


def test_the_listener_url_is_not_an_html_page():
    html = (ROOT / "djtube" / "templates" / "index.html").read_text(encoding="utf-8")
    assert "stream.js" not in html
    assert not (STATIC_DIR / "stream.js").exists()
    assert not (STATIC_DIR / "stream.css").exists()
    assert not (ROOT / "djtube" / "templates" / "stream.html").exists()


def test_synthetic_relay_starts_late_at_the_newest_cluster_and_keeps_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    clusters = [cluster_known(index * 200, bytes([index + 1]) * 24) for index in range(6)]
    head, data = document(clusters)
    cut = 4
    prefix = head + b"".join(clusters[:cut])
    rest = b"".join(clusters[cut:])
    hub = LiveHub()
    with TestClient(create_app(live=hub)) as client:
        with client.websocket_connect("/api/live/publish?id=TONE") as publisher:
            assert publisher.receive_json()["id"] == "TONE"
            publisher.send_json({"type": "mime", "mime": "audio/webm; codecs=opus"})
            with _Audio(client, "/stream/TONE") as early:
                _send(publisher, prefix)
                assert early.read() == head
                assert [early.read() for _ in range(cut)] == clusters[:cut]
                stream = hub.get("TONE")
                assert stream is not None
                assert stream.latest == clusters[cut - 1]
                assert stream.splitter.buffered < len(data) / 2
                with _Audio(client, "/stream/TONE") as late:
                    assert late.read() == head
                    assert late.read() == clusters[cut - 1]
                    _send(publisher, rest)
            # publisher and early close as the blocks exit; late is still open until here
        assert hub.get("TONE") is None
    assert list(tmp_path.iterdir()) == []


def test_end_and_garbage(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with TestClient(create_app()) as client:
        with client.websocket_connect("/api/live/publish?id=ENDD") as publisher:
            publisher.receive_json()
            with _Audio(client, "/stream/ENDD") as listener:
                assert listener.status_code == 200
                publisher.close()
                assert listener.read() is None
        with client.websocket_connect("/api/live/publish?id=BADM") as publisher:
            publisher.receive_json()
            with _Audio(client, "/stream/BADM") as listener:
                assert listener.status_code == 200
                publisher.send_bytes(b"this is not webm audio")
                assert listener.read() is None
            with pytest.raises(WebSocketDisconnect) as bad:
                publisher.receive_json()
            assert bad.value.code == CODE_BAD_MEDIA
        with client.websocket_connect("/api/live/publish?id=MIME") as publisher:
            publisher.receive_json()
            publisher.send_json({"type": "mime", "mime": "text/html"})
            with pytest.raises(WebSocketDisconnect) as bad_mime:
                publisher.receive_json()
            assert bad_mime.value.code == CODE_BAD_MEDIA
    assert list(tmp_path.iterdir()) == []


def test_a_slow_listener_is_dropped_and_a_full_queue_drops_old_clusters():
    async def scenario():
        listener = _Listener(limit=4)
        listener.offer_init(b"init")
        for index in range(10):
            listener.offer_media(bytes([index]))
        assert listener.pending_media() == [bytes([index]) for index in range(6, 10)]
        assert listener.pending_kinds()[0] == "init"

        hub = LiveHub(send_timeout=0.05, max_streams=2)
        stream, _generation, _token, _replaced, _ended = hub._open("SLOW", None, "203.0.113.8")
        waiting = _Listener(limit=4)
        stream.listeners.add(waiting)
        waiting.offer_init(b"i")
        waiting.offer_media(b"m")

        async def send(message: dict) -> None:
            if message["type"] == "http.response.body" and message.get("body"):
                await asyncio.sleep(5)

        async def receive() -> dict:
            await asyncio.sleep(30)
            return {"type": "http.disconnect"}

        response = _AudioResponse(hub, waiting, stream)
        await asyncio.wait_for(response({"type": "http"}, receive, send), timeout=1)
        assert stream.listeners == set()
        assert stream.closed is False
        assert hub.get("SLOW") is stream

    asyncio.run(scenario())


def test_relay_caps_match_the_publisher_budget():
    hub = LiveHub()
    assert hub.max_listeners == MAX_LISTENERS == 200
    assert hub.max_listeners_per_ip == MAX_LISTENERS_PER_IP == 4
    assert hub.max_listeners_total == MAX_LISTENERS_TOTAL == 400
    assert hub.max_streams == MAX_STREAMS == 8
    assert hub.max_per_ip == MAX_PER_IP == 2
    assert hub.max_bytes_per_second == MAX_BYTES_PER_SECOND == 64 * 1024
    assert hub.burst_seconds == BURST_SECONDS == 8
    assert hub.max_bytes_per_second * hub.burst_seconds == 512 * 1024
    assert hub.max_cluster == MAX_CLUSTER == 256 * 1024
    assert hub.max_frame == MAX_FRAME == 256 * 1024
    assert hub.init_timeout == INIT_TIMEOUT == 10
    assert hub.idle_timeout == IDLE_TIMEOUT == 30
    assert hub.reservation_ttl == RESERVATION_TTL == 12 * 60 * 60
    assert hub.reservation_max == RESERVATION_MAX == 4096
    assert hub.reservation_per_ip == RESERVATION_PER_IP == 32
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert '--ws-max-size", "1048576"' in dockerfile


def test_a_silent_publisher_loses_its_slot_and_a_quiet_one_does_too():
    silent = LiveHub(max_streams=2, init_timeout=0.25, idle_timeout=5)
    with TestClient(create_app(live=silent)) as client:
        started = time.monotonic()
        with client.websocket_connect("/api/live/publish?id=AAAA") as first:
            first.receive_json()
            with client.websocket_connect("/api/live/publish?id=AAAB") as second:
                second.receive_json()
                with pytest.raises(WebSocketDisconnect) as full:
                    with client.websocket_connect("/api/live/publish?id=AAAC") as third:
                        third.receive_json()
                assert full.value.code == CODE_FULL
                with pytest.raises(WebSocketDisconnect) as quiet:
                    second.receive_json()
                assert quiet.value.code == CODE_TIMEOUT
            with pytest.raises(WebSocketDisconnect) as quiet_first:
                first.receive_json()
            assert quiet_first.value.code == CODE_TIMEOUT
        assert time.monotonic() - started < 2
        assert silent.get("AAAA") is None
        assert silent.get("AAAB") is None
        with client.websocket_connect("/api/live/publish?id=AAAC") as again:
            assert _id_token(again.receive_json(), "AAAC")

    cluster = cluster_known(0, b"wave")
    head, _data = document([cluster])
    stalled = LiveHub(init_timeout=2, idle_timeout=0.3)
    with TestClient(create_app(live=stalled)) as client:
        with client.websocket_connect("/api/live/publish?id=STOP") as publisher:
            publisher.receive_json()
            publisher.send_bytes(head + cluster)
            with _Audio(client, "/stream/STOP") as listener:
                assert listener.read() == head
                assert listener.read() == cluster
                with pytest.raises(WebSocketDisconnect) as quiet:
                    publisher.receive_json()
                assert quiet.value.code == CODE_TIMEOUT
                assert listener.read() is None
        assert stalled.get("STOP") is None


def test_mime_alone_does_not_postpone_the_init_deadline():
    hub = LiveHub(init_timeout=0.3, idle_timeout=5)
    with TestClient(create_app(live=hub)) as client:
        started = time.monotonic()
        with client.websocket_connect("/api/live/publish?id=MIME") as publisher:
            publisher.receive_json()
            publisher.send_json({"type": "mime", "mime": "audio/webm"})
            with pytest.raises(WebSocketDisconnect) as quiet:
                publisher.receive_json()
            assert quiet.value.code == CODE_TIMEOUT
        assert time.monotonic() - started < 2
        assert hub.get("MIME") is None


def test_small_garbage_a_large_frame_and_a_burst_close_for_different_reasons():
    small = LiveHub(max_bytes_per_second=1024)
    with TestClient(create_app(live=small)) as client:
        with client.websocket_connect("/api/live/publish?id=TINY") as publisher:
            publisher.receive_json()
            publisher.send_bytes(b"x" * 3000)
            with pytest.raises(WebSocketDisconnect) as bad:
                publisher.receive_json()
            assert bad.value.code == CODE_BAD_MEDIA
        assert small.get("TINY") is None

    framed = LiveHub(max_frame=128, max_cluster=10_000, max_bytes_per_second=10_000)
    with TestClient(create_app(live=framed)) as client:
        with client.websocket_connect("/api/live/publish?id=FRAM") as publisher:
            publisher.receive_json()
            publisher.send_bytes(b"x" * 200)
            with pytest.raises(WebSocketDisconnect) as too_big:
                publisher.receive_json()
            assert too_big.value.code == CODE_TOO_BIG
        assert framed.get("FRAM") is None

    rate = LiveHub(max_bytes_per_second=1024)
    with TestClient(create_app(live=rate)) as client:
        with client.websocket_connect("/api/live/publish?id=RATE") as publisher:
            publisher.receive_json()
            publisher.send_bytes(b"x" * 9000)
            with pytest.raises(WebSocketDisconnect) as too_fast:
                publisher.receive_json()
            assert too_fast.value.code == CODE_RATE
        assert rate.get("RATE") is None

    huge = LiveHub(max_cluster=64)
    with TestClient(create_app(live=huge)) as client:
        with client.websocket_connect("/api/live/publish?id=HUGE") as publisher:
            publisher.receive_json()
            with _Audio(client, "/stream/HUGE") as listener:
                publisher.send_bytes(b"x" * 128)
                with pytest.raises(WebSocketDisconnect) as cluster:
                    publisher.receive_json()
                assert cluster.value.code == CODE_TOO_BIG
                assert listener.read() is None
        assert huge.get("HUGE") is None


def test_an_unfinished_cluster_does_not_refresh_the_idle_timer():
    cluster = cluster_known(0, b"wave")
    head, _data = document([cluster])
    hub = LiveHub(init_timeout=2, idle_timeout=0.4)
    with TestClient(create_app(live=hub)) as client:
        with client.websocket_connect("/api/live/publish?id=KEEP") as publisher:
            publisher.receive_json()
            publisher.send_json({"type": "mime", "mime": "audio/webm"})
            publisher.send_bytes(head + cluster)
            sent = time.monotonic()
            time.sleep(0.25)
            publisher.send_bytes(cluster[:2])
            with pytest.raises(WebSocketDisconnect) as quiet:
                publisher.receive_json()
            assert quiet.value.code == CODE_TIMEOUT
            assert time.monotonic() - sent < 0.58
        assert hub.get("KEEP") is None


def test_text_after_init_closes_as_bad_media():
    cluster = cluster_known(0, b"wave")
    head, _data = document([cluster])
    hub = LiveHub(init_timeout=2, idle_timeout=5)
    with TestClient(create_app(live=hub)) as client:
        with client.websocket_connect("/api/live/publish?id=LATE") as publisher:
            publisher.receive_json()
            publisher.send_json({"type": "mime", "mime": "audio/webm"})
            publisher.send_bytes(head + cluster)
            with _Audio(client, "/stream/LATE") as listener:
                assert listener.read() == head
                assert listener.read() == cluster
                publisher.send_text("still here")
                with pytest.raises(WebSocketDisconnect) as closed:
                    publisher.receive_json()
                assert closed.value.code == CODE_BAD_MEDIA
                assert listener.read() is None
        assert hub.get("LATE") is None


def test_ipv6_publishers_share_a_64():
    hub = LiveHub()
    with TestClient(create_app(live=hub)) as client:
        same = (
            "2001:db8:1:2::1",
            "2001:0db8:0001:0002:0000:0000:0000:abcd",
            "2001:db8:1:2:ffff::1",
        )
        with client.websocket_connect("/api/live/publish?id=VAAA", headers={"x-real-ip": same[0]}) as first:
            assert first.receive_json()["id"] == "VAAA"
            with client.websocket_connect("/api/live/publish?id=VAAB", headers={"x-real-ip": same[1]}) as second:
                assert second.receive_json()["id"] == "VAAB"
                with pytest.raises(WebSocketDisconnect) as full:
                    with client.websocket_connect("/api/live/publish?id=VAAC", headers={"x-real-ip": same[2]}) as third:
                        third.receive_json()
                assert full.value.code == CODE_FULL
                with client.websocket_connect(
                    "/api/live/publish?id=VBAA",
                    headers={"x-real-ip": "2001:db8:1:3::1"},
                ) as other:
                    assert other.receive_json()["id"] == "VBAA"
        assert hub.get("VAAA") is None


def test_a_second_mime_is_ignored_and_the_first_one_sticks():
    cluster = cluster_known(0, b"wave")
    head, _data = document([cluster])
    with TestClient(create_app()) as client:
        with client.websocket_connect("/api/live/publish?id=ONCE") as publisher:
            publisher.receive_json()
            publisher.send_json({"type": "mime", "mime": "audio/webm"})
            publisher.send_json({"type": "mime", "mime": "text/html"})
            publisher.send_bytes(head + cluster)
            with _Audio(client, "/stream/ONCE") as listener:
                assert listener.headers["content-type"] == DEFAULT_MIME
                assert listener.read() == head


def test_one_address_may_publish_twice_and_forwarded_for_does_not_count():
    hub = LiveHub()
    with TestClient(create_app(live=hub)) as client:
        first = {"x-real-ip": "203.0.113.8", "x-forwarded-for": "198.51.100.1"}
        second = {"x-real-ip": "203.0.113.8", "x-forwarded-for": "198.51.100.2"}
        spoofed = {"x-forwarded-for": "203.0.113.50"}
        with client.websocket_connect("/api/live/publish?id=IPAA", headers=first) as held:
            ipaa = held.receive_json()
            assert ipaa["id"] == "IPAA"
            ipaa_token = ipaa["token"]
            with client.websocket_connect("/api/live/publish?id=IPAB", headers=second) as also:
                assert also.receive_json()["id"] == "IPAB"
                with pytest.raises(WebSocketDisconnect) as full:
                    with client.websocket_connect("/api/live/publish?id=IPAC", headers=first) as extra:
                        extra.receive_json()
                assert full.value.code == CODE_FULL
                with client.websocket_connect(
                    "/api/live/publish?id=IPBA",
                    headers={"x-real-ip": "203.0.113.9", "x-forwarded-for": "198.51.100.1"},
                ) as other:
                    assert other.receive_json()["id"] == "IPBA"
            with client.websocket_connect("/api/live/publish?id=FFAA", headers=spoofed) as forwarded:
                assert forwarded.receive_json()["id"] == "FFAA"
                with client.websocket_connect(
                    "/api/live/publish?id=FFAB",
                    headers={"x-forwarded-for": "203.0.113.51"},
                ) as forwarded_again:
                    assert forwarded_again.receive_json()["id"] == "FFAB"
                    with pytest.raises(WebSocketDisconnect) as shared:
                        with client.websocket_connect(
                            "/api/live/publish?id=FFAC",
                            headers={"x-forwarded-for": "203.0.113.52"},
                        ) as forwarded_full:
                            forwarded_full.receive_json()
                    assert shared.value.code == CODE_FULL
        assert hub.get("IPAA") is None
        with pytest.raises(WebSocketDisconnect) as reserved:
            with client.websocket_connect("/api/live/publish?id=IPAA", headers=first) as blocked:
                blocked.receive_json()
        assert reserved.value.code == CODE_TAKEN
        with client.websocket_connect(_with_token("IPAA", ipaa_token), headers=first) as again:
            assert again.receive_json()["id"] == "IPAA"


def test_a_listener_who_leaves_during_silence_is_dropped():
    hub = LiveHub(max_listeners=1, init_timeout=5, idle_timeout=5)
    with TestClient(create_app(live=hub)) as client:
        with client.websocket_connect("/api/live/publish?id=QUIE") as publisher:
            assert publisher.receive_json()["id"] == "QUIE"
            with _Audio(client, "/stream/QUIE") as listener:
                assert listener.status_code == 200
                stream = hub.get("QUIE")
                assert stream is not None
                assert len(stream.listeners) == 1
            assert stream.listeners == set()
            with _Audio(client, "/stream/QUIE") as again:
                assert again.status_code == 200
                assert len(stream.listeners) == 1


def test_publisher_disconnect_closes_the_audio_and_clears_the_stream():
    cluster = cluster_known(0, b"wave")
    head, _data = document([cluster])
    hub = LiveHub()
    with TestClient(create_app(live=hub)) as client:
        with client.websocket_connect("/api/live/publish?id=HALF") as publisher:
            publisher.receive_json()
            with _Audio(client, "/stream/HALF") as listener:
                publisher.send_bytes(head + cluster)
                assert listener.read() == head
                assert listener.read() == cluster
                stream = hub.get("HALF")
                assert stream is not None
                assert len(stream.listeners) == 1
                publisher.close()
                assert listener.read() is None
                assert hub.get("HALF") is None
                assert stream.listeners == set()
                assert stream.closed
        gone = client.get("/stream/HALF")
        assert gone.status_code == 404


def test_joined_tone_plays_from_the_current_moment_not_the_beginning():
    webm = _tone()
    init, clusters = split_webm(webm)
    assert init + b"".join(clusters) == webm
    join_at = next(index for index, cluster in enumerate(clusters) if (cluster_timecode(cluster) or 0) >= 2500)
    prefix = init + b"".join(clusters[: join_at + 1])
    rest = b"".join(clusters[join_at + 1 :])
    with TestClient(create_app()) as client:
        with client.websocket_connect("/api/live/publish?id=PLAY") as publisher:
            publisher.receive_json()
            with _Audio(client, "/stream/PLAY") as early:
                _send(publisher, prefix)
                assert early.read() == init
                early_clusters = [early.read() for _ in range(join_at + 1)]
                assert early_clusters == clusters[: join_at + 1]
                assert cluster_timecode(early_clusters[0]) == 0
                with _Audio(client, "/stream/PLAY") as late:
                    assert late.read() == init
                    first = late.read()
                    assert first == clusters[join_at]
                    assert (cluster_timecode(first) or 0) >= 2500
                    _send(publisher, rest)
                    late_rest = []
                    early_rest = []
                    for cluster in clusters[join_at + 1 :]:
                        assert late.read() == cluster
                        late_rest.append(cluster)
                        assert early.read() == cluster
                        early_rest.append(cluster)
                    publisher.close()
                    assert late.read() is None
                    assert early.read() is None
    late_audio = init + first + b"".join(late_rest)
    early_audio = init + b"".join(early_clusters + early_rest)
    late_pcm = _decode(late_audio)
    early_pcm = _decode(early_audio)
    late_seconds = len(late_pcm) / 48000
    early_seconds = len(early_pcm) / 48000
    assert 0.6 < late_seconds < 2.2
    assert early_seconds > 3.5
    assert _dominant(late_pcm, 0.05) == 880
    assert _dominant(early_pcm, 0.2) == 440
    assert _dominant(early_pcm, 2.6) == 880
    assert not list(Path(".").glob("*.webm"))


def test_a_token_reclaims_a_live_id_and_is_not_logged():
    cluster = cluster_known(0, b"wave")
    head, _data = document([cluster])
    later = cluster_known(200, b"next")
    head_later, _rest = document([later])
    hub = LiveHub(init_timeout=5, idle_timeout=5)
    records: list[str] = []

    class _Grab(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record.getMessage())

    logger = logging.getLogger("djtube.live")
    handler = _Grab()
    logger.addHandler(handler)
    try:
        with TestClient(create_app(live=hub)) as client:
            with pytest.raises(WebSocketDisconnect) as invented:
                with client.websocket_connect("/api/live/publish?id=FREE&token=invented") as socket:
                    socket.receive_json()
            assert invented.value.code == CODE_TAKEN
            with client.websocket_connect("/api/live/publish?id=HEAR") as old:
                token = _id_token(old.receive_json(), "HEAR")
                old.send_bytes(head + cluster)
                with _Audio(client, "/stream/HEAR") as listener:
                    assert listener.read() == head
                    assert listener.read() == cluster
                    with pytest.raises(WebSocketDisconnect) as missing:
                        with client.websocket_connect("/api/live/publish?id=HEAR") as bare:
                            bare.receive_json()
                    assert missing.value.code == CODE_TAKEN
                    with pytest.raises(WebSocketDisconnect) as wrong:
                        with client.websocket_connect("/api/live/publish?id=HEAR&token=%C3%A9") as bare:
                            bare.receive_json()
                    assert wrong.value.code == CODE_TAKEN
                    with client.websocket_connect(_with_token("HEAR", token)) as new:
                        assert _id_token(new.receive_json(), "HEAR") == token
                        with pytest.raises(WebSocketDisconnect) as replaced:
                            old.receive_json()
                        assert replaced.value.code == CODE_REPLACED
                        assert listener.read() is None
                        new.send_bytes(head_later + later)
                        with _Audio(client, "/stream/HEAR") as resumed:
                            assert resumed.read() == head_later
                            assert resumed.read() == later
    finally:
        logger.removeHandler(handler)
    text = "\n".join(records)
    assert token not in text
    assert "publish HEAR" in text
    assert "end HEAR" in text


def test_a_reserved_id_is_not_handed_to_anyone_else(monkeypatch):
    monkeypatch.setattr("djtube.live.secrets.choice", lambda _alphabet: "Z")
    hub = LiveHub(init_timeout=5, idle_timeout=5)
    with TestClient(create_app(live=hub)) as client:
        with client.websocket_connect("/api/live/publish?id=ZZZZ") as held:
            token = _id_token(held.receive_json(), "ZZZZ")
        with pytest.raises(WebSocketDisconnect) as full:
            with client.websocket_connect("/api/live/publish") as fresh:
                fresh.receive_json()
        assert full.value.code == CODE_FULL
        with client.websocket_connect(_with_token("ZZZZ", token)) as again:
            assert again.receive_json()["id"] == "ZZZZ"


def test_reservations_expire_and_idle_holds_are_capped():
    now = [0.0]
    expired = LiveHub(reservation_ttl=12, clock=lambda: now[0], init_timeout=5, idle_timeout=5)
    with TestClient(create_app(live=expired)) as client:
        with client.websocket_connect("/api/live/publish?id=GONE") as publisher:
            token = _id_token(publisher.receive_json(), "GONE")
        now[0] = 11
        with client.websocket_connect(_with_token("GONE", token)) as still:
            assert still.receive_json()["token"] == token
        now[0] = 23
        with pytest.raises(WebSocketDisconnect) as stale:
            with client.websocket_connect(_with_token("GONE", token)) as socket:
                socket.receive_json()
        assert stale.value.code == CODE_TAKEN
        with client.websocket_connect("/api/live/publish?id=GONE") as taken:
            body = taken.receive_json()
            assert body["id"] == "GONE"
            assert body["token"] != token

    now[0] = 0
    capped = LiveHub(
        reservation_ttl=1000,
        reservation_max=2,
        reservation_per_ip=32,
        clock=lambda: now[0],
        init_timeout=5,
        idle_timeout=5,
    )
    with TestClient(create_app(live=capped)) as client:
        for index, stream_id in enumerate(("AAAA", "AAAB")):
            now[0] = index
            with client.websocket_connect(f"/api/live/publish?id={stream_id}") as publisher:
                _id_token(publisher.receive_json(), stream_id)
        now[0] = 2
        with client.websocket_connect("/api/live/publish?id=AAAC") as publisher:
            _id_token(publisher.receive_json(), "AAAC")
        with pytest.raises(WebSocketDisconnect) as kept:
            with client.websocket_connect("/api/live/publish?id=AAAB") as blocked:
                blocked.receive_json()
        assert kept.value.code == CODE_TAKEN
        with pytest.raises(WebSocketDisconnect) as kept_later:
            with client.websocket_connect("/api/live/publish?id=AAAC") as blocked:
                blocked.receive_json()
        assert kept_later.value.code == CODE_TAKEN
        with client.websocket_connect("/api/live/publish?id=AAAA") as publisher:
            assert publisher.receive_json()["id"] == "AAAA"

    per_source = LiveHub(
        reservation_ttl=1000,
        reservation_max=100,
        reservation_per_ip=1,
        clock=lambda: now[0],
        init_timeout=5,
        idle_timeout=5,
    )
    one = {"x-real-ip": "203.0.113.10"}
    other = {"x-real-ip": "203.0.113.11"}
    with TestClient(create_app(live=per_source)) as client:
        now[0] = 10
        with client.websocket_connect("/api/live/publish?id=BBBB", headers=one) as publisher:
            _id_token(publisher.receive_json(), "BBBB")
        now[0] = 11
        with client.websocket_connect("/api/live/publish?id=BBBC", headers=one) as publisher:
            kept_token = _id_token(publisher.receive_json(), "BBBC")
        with client.websocket_connect("/api/live/publish?id=BBBB", headers=other) as publisher:
            assert publisher.receive_json()["id"] == "BBBB"
        with pytest.raises(WebSocketDisconnect) as still_held:
            with client.websocket_connect("/api/live/publish?id=BBBC", headers=other) as blocked:
                blocked.receive_json()
        assert still_held.value.code == CODE_TAKEN
        with client.websocket_connect(_with_token("BBBC", kept_token), headers=other) as publisher:
            assert publisher.receive_json()["token"] == kept_token

    live_cap = LiveHub(reservation_max=1, reservation_per_ip=32, init_timeout=5, idle_timeout=5)
    with TestClient(create_app(live=live_cap)) as client:
        with client.websocket_connect("/api/live/publish?id=CCCC") as live:
            _id_token(live.receive_json(), "CCCC")
            with client.websocket_connect("/api/live/publish?id=CCCD") as extra:
                _id_token(extra.receive_json(), "CCCD")
                with pytest.raises(WebSocketDisconnect) as taken:
                    with client.websocket_connect("/api/live/publish?id=CCCC") as stranger:
                        stranger.receive_json()
                assert taken.value.code == CODE_TAKEN
                assert live_cap.get("CCCC") is not None
                assert live_cap.get("CCCD") is not None
