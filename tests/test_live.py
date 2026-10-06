from __future__ import annotations

import array
import asyncio
import concurrent.futures
import contextlib
import hashlib
import hmac
import json
import logging
import math
import os
import secrets
import shutil
import stat
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import parse_qsl, quote

import anyio
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect, WebSocketState

from djtube.app import create_app
from djtube.live import (
    CODE_BAD_ID,
    CODE_BAD_MEDIA,
    CODE_FULL,
    CODE_RATE,
    CODE_REPLACED,
    CODE_STORE,
    CODE_UNREADABLE,
    CODE_TAKEN,
    CODE_TIMEOUT,
    CODE_TOO_BIG,
    CLAIM_TIMEOUT,
    TOKEN_TTL,
    MAX_SEQ,
    BURST_SECONDS,
    IDLE_TIMEOUT,
    INIT_TIMEOUT,
    MAX_BYTES_PER_SECOND,
    MAX_CLUSTER,
    MAX_FRAME,
    MAX_PER_IP,
    MAX_PENDING,
    MAX_PENDING_PER_IP,
    MAX_SEEN,
    MAX_SEEN_PER_IP,
    MAX_STREAMS,
    MAX_TEXT,
    DEFAULT_MIME,
    MAX_LISTENERS,
    MAX_LISTENERS_PER_IP,
    MAX_LISTENERS_TOTAL,
    LiveClose,
    LiveHub,
    _Seen,
    _SeenWriter,
    _token_hash,
    live_seen_path,
    _write_json_atomic,
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


def _hub_from(client) -> LiveHub | None:
    app = client.app
    seen: set[int] = set()
    while app is not None and id(app) not in seen:
        seen.add(id(app))
        state = getattr(app, "state", None)
        hub = getattr(state, "live", None) if state is not None else None
        if isinstance(hub, LiveHub):
            return hub
        app = getattr(app, "inner", None)
    return None


@contextlib.contextmanager
def _ws(client, url, headers=None, seq=None, *, sign=True):
    """Connect, then send id and token as the first text frame.

    A query on `url` is shorthand for that frame. It is not sent on the wire,
    so the token cannot land in an access log. A bare id is signed with the
    hub's token, which is what the owner would send. Pass sign=False to
    present that id with no token.
    """

    path, _, query = url.partition("?")
    payload: dict = {}
    if query:
        for name, value in parse_qsl(query, keep_blank_values=True):
            if name in {"id", "token"} and value != "":
                payload[name] = value
    if seq is not None:
        payload["seq"] = seq
    if sign and "id" in payload and "token" not in payload and is_stream_id(str(payload["id"])):
        hub = _hub_from(client)
        if hub is not None:
            payload["token"] = mint(hub, str(payload["id"]))
    kwargs = {}
    if headers is not None:
        kwargs["headers"] = headers
    with client.websocket_connect(path, **kwargs) as socket:
        socket.send_json(payload)
        yield socket


def mint(hub: LiveHub, stream_id: str) -> str:
    """Mint the token a test already named. The server does not offer this."""

    with hub._lock:
        seen = hub._seen.get(stream_id)
        if seen is not None:
            return seen.token
        token = secrets.token_urlsafe(32)
        hub._seen[stream_id] = _Seen(hub._mono(), None, _token_hash(token), token, "")
        return token


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


def _receive_json(socket, timeout: float):
    """Bound a receive so a missing token refresh fails instead of waiting out the session."""

    done = concurrent.futures.Future()

    def _read() -> None:
        try:
            done.set_result(socket.receive_json())
        except Exception as exc:
            done.set_exception(exc)

    thread = threading.Thread(target=_read, daemon=True)
    thread.start()
    return done.result(timeout)


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

    def __init__(self, client: TestClient, path: str, headers: dict[str, str] | None = None, query: str = "") -> None:
        self._client = client
        self._path = path
        self._query = query.encode()
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
            "query_string": self._query,
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
    hub = LiveHub(clock=lambda: 1_700_000_000.0)
    with TestClient(create_app(live=hub)) as client:
        with _ws(client, "/api/live/publish") as first:
            first_id = first.receive_json()["id"]
            assert is_stream_id(first_id)
            with _ws(client, "/api/live/publish") as second:
                second_id = second.receive_json()["id"]
            assert is_stream_id(second_id)
            assert second_id != first_id
        with pytest.raises(WebSocketDisconnect) as bad:
            with _ws(client, "/api/live/publish?id=ab") as socket:
                socket.receive_json()
        assert bad.value.code == CODE_BAD_ID
        with _ws(client, "/djtube/api/live/publish?id=ABCD") as claimed:
            token = _id_token(claimed.receive_json(), "ABCD")
            with pytest.raises(WebSocketDisconnect) as taken:
                with _ws(client, "/api/live/publish?id=ABCD", sign=False) as other:
                    other.receive_json()
            assert taken.value.code == CODE_TAKEN
            with pytest.raises(WebSocketDisconnect) as wrong:
                with _ws(client, _with_token("ABCD", "not-the-token")) as other:
                    other.receive_json()
            assert wrong.value.code == CODE_TAKEN
        with pytest.raises(WebSocketDisconnect) as reserved:
            with _ws(client, "/api/live/publish?id=ABCD", sign=False) as again:
                again.receive_json()
        assert reserved.value.code == CODE_TAKEN
        with _ws(client, _with_token("ABCD", token)) as again:
            assert _id_token(again.receive_json(), "ABCD") == token
        with pytest.raises(WebSocketDisconnect) as lower:
            with _ws(client, "/api/live/publish?id=abcd") as socket:
                socket.receive_json()
        assert lower.value.code == CODE_BAD_ID


def test_full_hub_and_full_listener_list_refuse_another_connection():
    hub = LiveHub(max_streams=1, max_listeners=1)
    with TestClient(create_app(live=hub)) as client:
        with _ws(client, "/api/live/publish") as publisher:
            stream_id = publisher.receive_json()["id"]
            with pytest.raises(WebSocketDisconnect) as full:
                with _ws(client, "/api/live/publish") as extra:
                    extra.receive_json()
            assert full.value.code == CODE_FULL
            with _Audio(client, f"/stream/{stream_id}") as listener:
                assert listener.status_code == 200
                overflow = client.get(f"{PUBLIC_PREFIX}/stream/{stream_id}")
                assert overflow.status_code == 429
                assert overflow.headers["cache-control"] == "no-store"
                assert "retry-after" not in overflow.headers


def test_one_source_may_listen_only_so_many_times_on_one_stream():
    hub = LiveHub(max_listeners_per_ip=2, max_listeners=10, max_listeners_total=50, init_timeout=5, idle_timeout=5)
    same = {"x-real-ip": "203.0.113.8"}
    other = {"x-real-ip": "203.0.113.9"}
    with TestClient(create_app(live=hub)) as client:
        with _ws(client, "/api/live/publish?id=LIMS") as publisher:
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
        with _ws(client, "/api/live/publish?id=VLSA") as publisher:
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
        with _ws(client, "/api/live/publish?id=AAAA", headers={"x-real-ip": "203.0.113.1"}) as first:
            first.receive_json()
            with _ws(client, "/api/live/publish?id=AAAB", headers={"x-real-ip": "203.0.113.2"}) as second:
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
        with _ws(client, "/api/live/publish?id=LIVE") as publisher:
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
        with _ws(client, "/api/live/publish?id=LIVE") as publisher:
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
        with _ws(client, "/api/live/publish?id=TONE") as publisher:
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
    app = create_app()
    with TestClient(app) as client:
        with _ws(client, "/api/live/publish?id=ENDD") as publisher:
            publisher.receive_json()
            with _Audio(client, "/stream/ENDD") as listener:
                assert listener.status_code == 200
                publisher.close()
                assert listener.read() is None
        with _ws(client, "/api/live/publish?id=BADM") as publisher:
            publisher.receive_json()
            with _Audio(client, "/stream/BADM") as listener:
                assert listener.status_code == 200
                publisher.send_bytes(b"this is not webm audio")
                assert listener.read() is None
            with pytest.raises(WebSocketDisconnect) as bad:
                publisher.receive_json()
            assert bad.value.code == CODE_BAD_MEDIA
    app.state.live.flush()
    assert {path.name for path in tmp_path.iterdir()} == {"live-seen.json"}


def test_a_slow_listener_is_dropped_and_a_full_queue_drops_old_clusters():
    async def scenario():
        listener = _Listener(limit=4)
        listener.offer_init(b"init")
        for index in range(10):
            listener.offer_media(bytes([index]))
        assert listener.pending_media() == [bytes([index]) for index in range(6, 10)]
        assert listener.pending_kinds()[0] == "init"

        hub = LiveHub(send_timeout=0.05, max_streams=2)
        stream, _generation, _token, _replaced, _ended, _video_ended, _old_relay = hub._open(
            "SLOW", mint(hub, "SLOW"), "203.0.113.8"
        )
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
    assert hub.claim_timeout == CLAIM_TIMEOUT == 3
    assert hub.token_ttl == TOKEN_TTL == 12 * 60 * 60
    assert MAX_SEQ == 2**53 - 1
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert '--ws-max-size", "1048576"' in dockerfile


def test_a_silent_publisher_loses_its_slot_and_a_quiet_one_does_too():
    silent = LiveHub(max_streams=2, init_timeout=0.25, idle_timeout=5)
    with TestClient(create_app(live=silent)) as client:
        started = time.monotonic()
        with _ws(client, "/api/live/publish?id=AAAA") as first:
            first.receive_json()
            with _ws(client, "/api/live/publish?id=AAAB") as second:
                second.receive_json()
                with pytest.raises(WebSocketDisconnect) as full:
                    with _ws(client, "/api/live/publish?id=AAAC") as third:
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
        with _ws(client, "/api/live/publish?id=AAAC") as again:
            assert _id_token(again.receive_json(), "AAAC")

    cluster = cluster_known(0, b"wave")
    head, _data = document([cluster])
    stalled = LiveHub(init_timeout=2, idle_timeout=0.3)
    with TestClient(create_app(live=stalled)) as client:
        with _ws(client, "/api/live/publish?id=STOP") as publisher:
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


def test_timeout_logs_name_the_init_and_idle_clocks(caplog):
    import djtube.live as live_module

    live_module._configure_log()
    live_module.log.propagate = True
    caplog.set_level(logging.INFO, logger="djtube.live")
    claim = {"type": "mime", "mime": DEFAULT_MIME}
    try:
        hub = LiveHub(init_timeout=0.2, idle_timeout=5, claim_timeout=2)
        with TestClient(create_app(live=hub)) as client:
            with client.websocket_connect("/api/live/publish") as publisher:
                publisher.send_json(claim)
                stream_id = publisher.receive_json()["id"]
                with pytest.raises(WebSocketDisconnect) as quiet:
                    publisher.receive_json()
                assert quiet.value.code == CODE_TIMEOUT
        assert f"timeout {stream_id} init" in caplog.text
        assert f"timeout {stream_id} idle" not in caplog.text

        caplog.clear()
        cluster = cluster_known(0, b"wave")
        head, _data = document([cluster])
        stalled = LiveHub(init_timeout=2, idle_timeout=0.25, claim_timeout=2)
        with TestClient(create_app(live=stalled)) as client:
            with client.websocket_connect("/api/live/publish") as publisher:
                publisher.send_json(claim)
                stream_id = publisher.receive_json()["id"]
                publisher.send_bytes(head + cluster)
                with pytest.raises(WebSocketDisconnect) as quiet:
                    publisher.receive_json()
                assert quiet.value.code == CODE_TIMEOUT
        assert f"timeout {stream_id} idle" in caplog.text
        assert f"timeout {stream_id} init" not in caplog.text

        caplog.clear()
        # The claim arrives late. The init deadline stays on accept, so the
        # close is not another full init_timeout after the id.
        delayed = LiveHub(init_timeout=0.5, idle_timeout=5, claim_timeout=2)
        with TestClient(create_app(live=delayed)) as client:
            started = time.monotonic()
            with client.websocket_connect("/api/live/publish") as socket:
                time.sleep(0.3)
                socket.send_json(claim)
                stream_id = socket.receive_json()["id"]
                with pytest.raises(WebSocketDisconnect) as quiet:
                    socket.receive_json()
                assert quiet.value.code == CODE_TIMEOUT
            elapsed = time.monotonic() - started
        assert elapsed < 0.75
        assert f"timeout {stream_id} init" in caplog.text
        assert "timeout claim" not in caplog.text

        caplog.clear()
        missing = LiveHub(init_timeout=5, idle_timeout=5, claim_timeout=0.2)
        with TestClient(create_app(live=missing)) as client:
            with pytest.raises(WebSocketDisconnect) as quiet:
                with client.websocket_connect("/api/live/publish") as socket:
                    socket.receive_json()
            assert quiet.value.code == CODE_TIMEOUT
        assert "timeout claim" in caplog.text
    finally:
        live_module.log.propagate = False


def test_a_missing_claim_closes_on_the_claim_timeout(caplog):
    import djtube.live as live_module

    live_module._configure_log()
    live_module.log.propagate = True
    caplog.set_level(logging.INFO, logger="djtube.live")
    try:
        # No first text. The wait is claim_timeout from accept. init_timeout
        # stays long so a mutant that uses the remaining init budget waits
        # about five seconds and fails the upper bound.
        hub = LiveHub(init_timeout=5, idle_timeout=5, claim_timeout=0.4)
        with TestClient(create_app(live=hub)) as client:
            started = time.monotonic()
            with client.websocket_connect("/api/live/publish") as socket:
                with pytest.raises(WebSocketDisconnect) as quiet:
                    socket.receive_json()
                assert quiet.value.code == CODE_TIMEOUT
            elapsed = time.monotonic() - started
        assert elapsed > 0.2
        assert elapsed < 0.7
        assert any(record.message == "timeout claim" for record in caplog.records)
        assert not any(record.message.startswith("publish") for record in caplog.records)
    finally:
        live_module.log.propagate = False


def test_mime_alone_does_not_postpone_the_init_deadline():
    hub = LiveHub(init_timeout=0.3, idle_timeout=5)
    with TestClient(create_app(live=hub)) as client:
        started = time.monotonic()
        with _ws(client, "/api/live/publish?id=MIME") as publisher:
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
        with _ws(client, "/api/live/publish?id=TINY") as publisher:
            publisher.receive_json()
            publisher.send_bytes(b"x" * 3000)
            with pytest.raises(WebSocketDisconnect) as bad:
                publisher.receive_json()
            assert bad.value.code == CODE_BAD_MEDIA
        assert small.get("TINY") is None

    framed = LiveHub(max_frame=128, max_cluster=10_000, max_bytes_per_second=10_000)
    with TestClient(create_app(live=framed)) as client:
        with _ws(client, "/api/live/publish?id=FRAM") as publisher:
            publisher.receive_json()
            publisher.send_bytes(b"x" * 200)
            with pytest.raises(WebSocketDisconnect) as too_big:
                publisher.receive_json()
            assert too_big.value.code == CODE_TOO_BIG
        assert framed.get("FRAM") is None

    rate = LiveHub(max_bytes_per_second=1024)
    with TestClient(create_app(live=rate)) as client:
        with _ws(client, "/api/live/publish?id=RATE") as publisher:
            publisher.receive_json()
            publisher.send_bytes(b"x" * 9000)
            with pytest.raises(WebSocketDisconnect) as too_fast:
                publisher.receive_json()
            assert too_fast.value.code == CODE_RATE
        assert rate.get("RATE") is None

    huge = LiveHub(max_cluster=64)
    with TestClient(create_app(live=huge)) as client:
        with _ws(client, "/api/live/publish?id=HUGE") as publisher:
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
        with _ws(client, "/api/live/publish?id=KEEP") as publisher:
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
        with _ws(client, "/api/live/publish?id=LATE") as publisher:
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
        with _ws(client, "/api/live/publish?id=VAAA", headers={"x-real-ip": same[0]}) as first:
            assert first.receive_json()["id"] == "VAAA"
            with _ws(client, "/api/live/publish?id=VAAB", headers={"x-real-ip": same[1]}) as second:
                assert second.receive_json()["id"] == "VAAB"
                with pytest.raises(WebSocketDisconnect) as full:
                    with _ws(client, "/api/live/publish?id=VAAC", headers={"x-real-ip": same[2]}) as third:
                        third.receive_json()
                assert full.value.code == CODE_FULL
                with _ws(
                    client,
                    "/api/live/publish?id=VBAA",
                    headers={"x-real-ip": "2001:db8:1:3::1"},
                ) as other:
                    assert other.receive_json()["id"] == "VBAA"
        assert hub.get("VAAA") is None


def test_a_second_mime_is_ignored_and_the_first_one_sticks():
    cluster = cluster_known(0, b"wave")
    head, _data = document([cluster])
    with TestClient(create_app()) as client:
        with _ws(client, "/api/live/publish?id=ONCE") as publisher:
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
        with _ws(client, "/api/live/publish?id=IPAA", headers=first) as held:
            ipaa = held.receive_json()
            assert ipaa["id"] == "IPAA"
            ipaa_token = ipaa["token"]
            with _ws(client, "/api/live/publish?id=IPAB", headers=second) as also:
                assert also.receive_json()["id"] == "IPAB"
                with pytest.raises(WebSocketDisconnect) as full:
                    with _ws(client, "/api/live/publish?id=IPAC", headers=first) as extra:
                        extra.receive_json()
                assert full.value.code == CODE_FULL
                with _ws(
                    client,
                    "/api/live/publish?id=IPBA",
                    headers={"x-real-ip": "203.0.113.9", "x-forwarded-for": "198.51.100.1"},
                ) as other:
                    assert other.receive_json()["id"] == "IPBA"
            with _ws(client, "/api/live/publish?id=FFAA", headers=spoofed) as forwarded:
                assert forwarded.receive_json()["id"] == "FFAA"
                with _ws(
                    client,
                    "/api/live/publish?id=FFAB",
                    headers={"x-forwarded-for": "203.0.113.51"},
                ) as forwarded_again:
                    assert forwarded_again.receive_json()["id"] == "FFAB"
                    with pytest.raises(WebSocketDisconnect) as shared:
                        with _ws(
                            client,
                            "/api/live/publish?id=FFAC",
                            headers={"x-forwarded-for": "203.0.113.52"},
                        ) as forwarded_full:
                            forwarded_full.receive_json()
                    assert shared.value.code == CODE_FULL
        assert hub.get("IPAA") is None
        with pytest.raises(WebSocketDisconnect) as reserved:
            with _ws(client, "/api/live/publish?id=IPAA", headers=first, sign=False) as blocked:
                blocked.receive_json()
        assert reserved.value.code == CODE_TAKEN
        with _ws(client, _with_token("IPAA", ipaa_token), headers=first) as again:
            assert again.receive_json()["id"] == "IPAA"


def test_a_listener_who_leaves_during_silence_is_dropped():
    hub = LiveHub(max_listeners=1, init_timeout=5, idle_timeout=5)
    with TestClient(create_app(live=hub)) as client:
        with _ws(client, "/api/live/publish?id=QUIE") as publisher:
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
        with _ws(client, "/api/live/publish?id=HALF") as publisher:
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
        with _ws(client, "/api/live/publish?id=PLAY") as publisher:
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


def test_a_token_in_the_query_is_not_a_claim_and_is_not_on_the_request():
    """nginx logs $request. The token must not be there, and a query token must not work."""

    hub = LiveHub(init_timeout=5, idle_timeout=5)
    queries: list[bytes] = []

    class _Spy:
        def __init__(self, inner) -> None:
            self.inner = inner

        async def __call__(self, scope, receive, send):
            if scope.get("type") == "websocket":
                raw = scope.get("query_string") or b""
                queries.append(raw if isinstance(raw, bytes) else bytes(raw))
            await self.inner(scope, receive, send)

    with TestClient(_Spy(create_app(live=hub))) as client:
        with _ws(client, "/api/live/publish?id=LOGS") as publisher:
            token = _id_token(publisher.receive_json(), "LOGS")
        assert queries
        assert all(b"token=" not in item and token.encode() not in item for item in queries)
        with pytest.raises(WebSocketDisconnect) as ignored:
            with client.websocket_connect(f"/api/live/publish?id=LOGS&token={quote(token, safe='')}") as socket:
                socket.send_json({"id": "LOGS"})
                socket.receive_json()
        assert ignored.value.code == CODE_TAKEN


def test_a_token_reclaims_a_live_id_and_is_not_logged():
    cluster = cluster_known(0, b"wave")
    head, _data = document([cluster])
    later = cluster_known(200, b"next")
    head_later, _rest = document([later])
    hub = LiveHub(init_timeout=5, idle_timeout=5, clock=lambda: 1_700_000_000.0)
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
                with _ws(client, "/api/live/publish?id=FREE&token=invented") as socket:
                    socket.receive_json()
            assert invented.value.code == CODE_TAKEN
            with _ws(client, "/api/live/publish?id=HEAR") as old:
                token = _id_token(old.receive_json(), "HEAR")
                old.send_bytes(head + cluster)
                with _Audio(client, "/stream/HEAR") as listener:
                    assert listener.read() == head
                    assert listener.read() == cluster
                    with pytest.raises(WebSocketDisconnect) as missing:
                        with _ws(client, "/api/live/publish?id=HEAR", sign=False) as bare:
                            bare.receive_json()
                    assert missing.value.code == CODE_TAKEN
                    with pytest.raises(WebSocketDisconnect) as wrong:
                        with _ws(client, "/api/live/publish?id=HEAR&token=%C3%A9") as bare:
                            bare.receive_json()
                    assert wrong.value.code == CODE_TAKEN
                    with _ws(client, _with_token("HEAR", token)) as new:
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
        with _ws(client, "/api/live/publish?id=ZZZZ") as held:
            token = _id_token(held.receive_json(), "ZZZZ")
        with pytest.raises(WebSocketDisconnect) as full:
            with _ws(client, "/api/live/publish") as fresh:
                fresh.receive_json()
        assert full.value.code == CODE_FULL
        with _ws(client, _with_token("ZZZZ", token)) as again:
            assert again.receive_json()["id"] == "ZZZZ"


def test_a_token_expires_twelve_hours_after_it_was_last_used():
    now = [0.0]
    hub = LiveHub(token_ttl=12, clock=lambda: now[0], init_timeout=5, idle_timeout=5)
    with TestClient(create_app(live=hub)) as client:
        with _ws(client, "/api/live/publish?id=GONE") as publisher:
            token = _id_token(publisher.receive_json(), "GONE")
        now[0] = 11
        with _ws(client, _with_token("GONE", token)) as still:
            body = still.receive_json()
            assert body["id"] == "GONE"
            assert body["token"] == token
        now[0] = 23
        with pytest.raises(WebSocketDisconnect) as expired:
            with _ws(client, _with_token("GONE", token)) as blocked:
                blocked.receive_json()
        assert expired.value.code == CODE_TAKEN
        with pytest.raises(WebSocketDisconnect) as stranger:
            with _ws(client, "/api/live/publish?id=GONE", sign=False) as blocked:
                blocked.receive_json()
        assert stranger.value.code == CODE_TAKEN
        with _ws(client, "/api/live/publish") as fresh:
            assert is_stream_id(fresh.receive_json()["id"])


def test_old_cleanup_does_not_drop_the_replacement_and_reclaim_ignores_its_own_slot():
    hub = LiveHub(max_per_ip=2, max_streams=2, clock=lambda: 1_700_000_000.0)
    first, generation, token, _replaced, _ended, _video_ended, _old_relay = hub._open(
        "AAAA", mint(hub, "AAAA"), "203.0.113.8", 1
    )
    hub._open("AAAB", mint(hub, "AAAB"), "203.0.113.8")
    with pytest.raises(LiveClose) as full:
        hub._open("AAAC", mint(hub, "AAAC"), "203.0.113.8")
    assert full.value.code == CODE_FULL
    reclaimed, new_generation, same, replaced, _ended, _video_ended, _old_relay = hub._open(
        "AAAA", token, "203.0.113.8", 2
    )
    assert reclaimed is first
    assert same == token
    assert replaced is None or replaced is first.websocket
    assert new_generation == generation + 1
    sentinel = object()
    first.listeners.add(sentinel)  # type: ignore[arg-type]
    assert hub._on_bytes(first, b"stale-bytes-from-the-old-socket", generation) is False
    assert hub._end(first, generation) is False
    assert hub.get("AAAA") is first
    assert sentinel in first.listeners
    assert first.closed is False
    assert hub.stream_status("AAAA")[0] == 200
    first.listeners.discard(sentinel)  # type: ignore[arg-type]
    assert hub._end(first, new_generation) is True
    assert hub.get("AAAA") is None


def test_replaced_publisher_cleanup_leaves_the_new_stream_audible():
    cluster = cluster_known(0, b"wave")
    head, _data = document([cluster])
    hub = LiveHub(init_timeout=5, idle_timeout=5, clock=lambda: 1_700_000_000.0)
    with TestClient(create_app(live=hub)) as client:
        old_cm = _ws(client, "/api/live/publish?id=KEEP")
        old = old_cm.__enter__()
        new_cm = None
        closed_old = False
        try:
            token = _id_token(old.receive_json(), "KEEP")
            stream = hub.get("KEEP")
            assert stream is not None
            old_generation = stream.generation
            new_cm = _ws(client, _with_token("KEEP", token))
            new = new_cm.__enter__()
            assert _id_token(new.receive_json(), "KEEP") == token
            with pytest.raises(WebSocketDisconnect) as replaced:
                old.receive_json()
            assert replaced.value.code == CODE_REPLACED
            old_cm.__exit__(None, None, None)
            closed_old = True
            assert hub._end(stream, old_generation) is False
            assert hub.get("KEEP") is stream
            assert client.head("/stream/KEEP").status_code == 200
            new.send_bytes(head + cluster)
            with _Audio(client, "/stream/KEEP") as listener:
                assert listener.status_code == 200
                assert listener.read() == head
                assert listener.read() == cluster
        finally:
            if not closed_old:
                old_cm.__exit__(None, None, None)
            if new_cm is not None:
                new_cm.__exit__(None, None, None)


def test_two_live_streams_from_one_address_can_still_reclaim():
    hub = LiveHub(max_per_ip=2, max_streams=2, init_timeout=5, idle_timeout=5, clock=lambda: 1_700_000_000.0)
    headers = {"x-real-ip": "203.0.113.8"}
    with TestClient(create_app(live=hub)) as client:
        with _ws(client, "/api/live/publish?id=AAAA", headers=headers) as first:
            token = _id_token(first.receive_json(), "AAAA")
            with _ws(client, "/api/live/publish?id=AAAB", headers=headers) as second:
                assert second.receive_json()["id"] == "AAAB"
                with pytest.raises(WebSocketDisconnect) as full:
                    with _ws(client, "/api/live/publish?id=AAAC", headers=headers) as third:
                        third.receive_json()
                assert full.value.code == CODE_FULL
                with _ws(client, _with_token("AAAA", token), headers=headers) as again:
                    assert again.receive_json()["token"] == token
                    with pytest.raises(WebSocketDisconnect) as replaced:
                        first.receive_json()
                    assert replaced.value.code == CODE_REPLACED
                    assert client.head("/stream/AAAA").status_code == 200
                    assert client.head("/stream/AAAB").status_code == 200


def test_a_late_older_claim_does_not_replace_the_newer_publish():
    cluster = cluster_known(0, b"wave")
    head, _data = document([cluster])
    hub = LiveHub(init_timeout=5, idle_timeout=5, clock=lambda: 1_700_000_000.0)
    with TestClient(create_app(live=hub)) as client:
        with _ws(client, "/api/live/publish?id=SEQQ", seq=1) as older:
            token = _id_token(older.receive_json(), "SEQQ")
            with _ws(client, _with_token("SEQQ", token), seq=2) as newer:
                assert _id_token(newer.receive_json(), "SEQQ") == token
                generation = hub.get("SEQQ").generation
                with pytest.raises(WebSocketDisconnect) as late:
                    with _ws(client, _with_token("SEQQ", token), seq=1) as stale:
                        stale.receive_json()
                assert late.value.code == CODE_REPLACED
                with pytest.raises(WebSocketDisconnect) as missing_seq:
                    with _ws(client, _with_token("SEQQ", token)) as stale:
                        stale.receive_json()
                assert missing_seq.value.code == CODE_REPLACED
                assert hub.get("SEQQ").generation == generation
                newer.send_bytes(head + cluster)
                assert client.head("/stream/SEQQ").status_code == 200
                with _Audio(client, "/stream/SEQQ") as listener:
                    assert listener.read() == head
                    assert listener.read() == cluster


def test_a_restart_accepts_the_owner_and_refuses_a_stranger(tmp_path):
    path = tmp_path / "live-seen.json"
    now = [1_700_000_000.0]
    first = LiveHub(seen_path=path, clock=lambda: now[0], init_timeout=5, idle_timeout=5)
    records: list[str] = []

    class _Grab(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record.getMessage())

    logger = logging.getLogger("djtube.live")
    handler = _Grab()
    logger.addHandler(handler)
    try:
        with TestClient(create_app(live=first)) as client:
            with _ws(client, "/api/live/publish?id=REST") as socket:
                token = _id_token(socket.receive_json(), "REST")
        stored = path.read_text(encoding="utf-8")
        assert path.stat().st_mode & 0o777 == 0o600
        assert token not in stored
        assert hashlib.sha256(token.encode()).hexdigest() in stored
        now[0] += 1
        restarted = LiveHub(seen_path=path, clock=lambda: now[0], init_timeout=5, idle_timeout=5)
        assert restarted._seen["REST"].token == ""
        with TestClient(create_app(live=restarted)) as client:
            with _ws(client, _with_token("REST", token)) as socket:
                body = socket.receive_json()
                assert body["id"] == "REST"
                assert body["token"] == token
            with pytest.raises(WebSocketDisconnect) as stranger:
                with _ws(client, "/api/live/publish?id=REST", sign=False) as blocked:
                    blocked.receive_json()
            assert stranger.value.code == CODE_TAKEN
            with pytest.raises(WebSocketDisconnect) as invented:
                with _ws(client, "/api/live/publish?id=REST&token=token-from-before-restart") as blocked:
                    blocked.receive_json()
            assert invented.value.code == CODE_TAKEN
    finally:
        logger.removeHandler(handler)
    text = "\n".join(records)
    assert token not in text


def test_the_query_id_is_not_the_claim():
    hub = LiveHub(init_timeout=5, idle_timeout=5)
    with TestClient(create_app(live=hub)) as client:
        with client.websocket_connect("/api/live/publish?id=QQQQ") as socket:
            socket.send_json({"type": "mime", "mime": DEFAULT_MIME})
            body = socket.receive_json()
        assert body["id"] != "QQQQ"
        assert is_stream_id(body["id"])


def test_a_broken_claim_is_a_bad_id_and_binary_that_arrives_first_is_kept():
    hub = LiveHub(init_timeout=5, idle_timeout=5)
    with TestClient(create_app(live=hub)) as client:
        for payload in ("{", "[]", "null", "1"):
            with pytest.raises(WebSocketDisconnect) as bad:
                with client.websocket_connect("/api/live/publish") as socket:
                    socket.send_text(payload)
                    socket.receive_json()
            assert bad.value.code == CODE_BAD_ID
        with pytest.raises(WebSocketDisconnect) as number:
            with client.websocket_connect("/api/live/publish") as socket:
                socket.send_json({"id": 1234, "type": "mime", "mime": DEFAULT_MIME})
                socket.receive_json()
        assert number.value.code == CODE_BAD_ID
        with pytest.raises(WebSocketDisconnect) as mime:
            with client.websocket_connect("/api/live/publish") as socket:
                socket.send_json({"type": "mime", "mime": "text/html"})
                socket.receive_json()
        assert mime.value.code == CODE_BAD_MEDIA
        oversized = json.dumps({"type": "mime", "mime": DEFAULT_MIME, "pad": "x" * MAX_TEXT})
        assert len(oversized.encode()) > MAX_TEXT
        with pytest.raises(WebSocketDisconnect) as huge:
            with client.websocket_connect("/api/live/publish") as socket:
                socket.send_text(oversized)
                socket.receive_json()
        assert huge.value.code == CODE_TOO_BIG
        assert list(hub._streams) == []

    cluster = cluster_known(0, b"wave")
    head, _data = document([cluster])
    heard = LiveHub(init_timeout=5, idle_timeout=5)
    with TestClient(create_app(live=heard)) as client:
        with client.websocket_connect("/api/live/publish") as socket:
            socket.send_bytes(head + cluster)
            stream_id = socket.receive_json()["id"]
            stream = heard.get(stream_id)
            assert stream is not None
            assert stream.init == head
            with _Audio(client, f"/stream/{stream_id}") as listener:
                assert listener.read() == head
                assert listener.read() == cluster


def test_seq_rejects_anything_outside_safe_integers():
    hub = LiveHub(clock=lambda: 40.0, init_timeout=5, idle_timeout=5)
    with TestClient(create_app(live=hub)) as client:
        for bad in (True, MAX_SEQ + 1, 1.5, "3", 0, -1):
            with pytest.raises(WebSocketDisconnect) as rejected:
                with client.websocket_connect("/api/live/publish") as socket:
                    socket.send_json({"type": "mime", "mime": DEFAULT_MIME, "seq": bad})
                    socket.receive_json()
            assert rejected.value.code == CODE_BAD_ID
        assert hub._seen == {}
        with _ws(client, "/api/live/publish?id=SMAX", seq=MAX_SEQ) as first:
            token = _id_token(first.receive_json(), "SMAX")
        with pytest.raises(WebSocketDisconnect) as same:
            with _ws(client, _with_token("SMAX", token), seq=MAX_SEQ) as stale:
                stale.receive_json()
        assert same.value.code == CODE_REPLACED
        with pytest.raises(WebSocketDisconnect) as bigger:
            with client.websocket_connect("/api/live/publish") as socket:
                socket.send_json(
                    {"id": "SMAX", "token": token, "type": "mime", "mime": DEFAULT_MIME, "seq": MAX_SEQ + 1}
                )
                socket.receive_json()
        assert bigger.value.code == CODE_BAD_ID
        assert hub.get("SMAX") is None


def test_two_reclaims_then_the_middle_seq_and_the_same_seq_leave_the_newest():
    cluster = cluster_known(0, b"wave")
    head, _data = document([cluster])
    hub = LiveHub(init_timeout=5, idle_timeout=5, clock=lambda: 1_700_000_000.0)
    with TestClient(create_app(live=hub)) as client:
        with _ws(client, "/api/live/publish?id=SEQQ", seq=1) as first:
            token = _id_token(first.receive_json(), "SEQQ")
            with _ws(client, _with_token("SEQQ", token), seq=2) as second:
                token = _id_token(second.receive_json(), "SEQQ")
                with _ws(client, _with_token("SEQQ", token), seq=3) as third:
                    token = _id_token(third.receive_json(), "SEQQ")
                    generation = hub.get("SEQQ").generation
                    with pytest.raises(WebSocketDisconnect) as late:
                        with _ws(client, _with_token("SEQQ", token), seq=2) as stale:
                            stale.receive_json()
                    assert late.value.code == CODE_REPLACED
                    with pytest.raises(WebSocketDisconnect) as same:
                        with _ws(client, _with_token("SEQQ", token), seq=3) as stale:
                            stale.receive_json()
                    assert same.value.code == CODE_REPLACED
                    assert hub.get("SEQQ").generation == generation
                    third.send_bytes(head + cluster)
                    with _Audio(client, "/stream/SEQQ") as listener:
                        assert listener.read() == head
                        assert listener.read() == cluster


def test_a_socket_waiting_for_the_first_text_counts_and_times_out():
    per_ip = LiveHub(max_per_ip=2, max_streams=8, claim_timeout=0.25, init_timeout=5)
    headers = {"x-real-ip": "203.0.113.40"}
    with TestClient(create_app(live=per_ip)) as client:
        started = time.monotonic()
        with (
            client.websocket_connect("/api/live/publish", headers=headers) as first,
            client.websocket_connect("/api/live/publish", headers=headers) as second,
        ):
            with pytest.raises(WebSocketDisconnect) as full:
                with client.websocket_connect("/api/live/publish", headers=headers) as third:
                    third.receive_json()
            assert full.value.code == CODE_FULL
            with pytest.raises(WebSocketDisconnect) as quiet:
                first.receive_json()
            assert quiet.value.code == CODE_TIMEOUT
            second.close()
        assert time.monotonic() - started < 1.5

    overall = LiveHub(max_streams=1, max_per_ip=2, max_pending=2, claim_timeout=2, init_timeout=5)
    with TestClient(create_app(live=overall)) as client:
        with client.websocket_connect("/api/live/publish") as held:
            with client.websocket_connect("/api/live/publish") as extra:
                extra.send_json({"type": "mime", "mime": DEFAULT_MIME})
                assert is_stream_id(extra.receive_json()["id"])
            held.close()

    clock = LiveHub(init_timeout=0.5, claim_timeout=2, idle_timeout=5)
    with TestClient(create_app(live=clock)) as client:
        started = time.monotonic()
        with client.websocket_connect("/api/live/publish") as socket:
            time.sleep(0.4)
            socket.send_json({"type": "mime", "mime": DEFAULT_MIME})
            assert is_stream_id(socket.receive_json()["id"])
            with pytest.raises(WebSocketDisconnect) as quiet:
                socket.receive_json()
            assert quiet.value.code == CODE_TIMEOUT
        assert time.monotonic() - started < 0.75


def test_a_reclaim_fits_while_another_socket_waits_for_its_first_text():
    hub = LiveHub(max_per_ip=2, max_streams=4, claim_timeout=2, init_timeout=5, clock=lambda: 20.0)
    headers = {"x-real-ip": "203.0.113.41"}
    with TestClient(create_app(live=hub)) as client:
        with _ws(client, "/api/live/publish?id=AAAA", headers=headers, seq=1) as live:
            token = _id_token(live.receive_json(), "AAAA")
            with client.websocket_connect("/api/live/publish", headers=headers):
                with _ws(client, _with_token("AAAA", token), headers=headers, seq=2) as again:
                    assert again.receive_json()["id"] == "AAAA"
                with pytest.raises(WebSocketDisconnect) as replaced:
                    live.receive_json()
                assert replaced.value.code == CODE_REPLACED


def test_the_token_hash_rejects_a_changed_token_and_another_hubs_token():
    hub = LiveHub(clock=lambda: 1_700_000_000.0, init_timeout=5, idle_timeout=5)
    other = LiveHub(clock=lambda: 1_700_000_000.0, init_timeout=5, idle_timeout=5)
    with TestClient(create_app(live=hub)) as client:
        good = mint(hub, "ABCD")
        with _ws(client, _with_token("ABCD", good), sign=False) as socket:
            assert socket.receive_json()["token"] == good
        flipped = ("0" if good[0] != "0" else "1") + good[1:]
        with pytest.raises(WebSocketDisconnect) as wrong:
            with _ws(client, _with_token("ABCD", flipped), sign=False) as socket:
                socket.receive_json()
        assert wrong.value.code == CODE_TAKEN
        minted_for_other = mint(hub, "BBBB")
        with pytest.raises(WebSocketDisconnect) as other_id:
            with _ws(client, _with_token("CCCC", minted_for_other), sign=False) as socket:
                socket.receive_json()
        assert other_id.value.code == CODE_TAKEN
    foreign = mint(other, "DDDD")
    with TestClient(create_app(live=hub)) as client:
        with pytest.raises(WebSocketDisconnect) as other_hub:
            with _ws(client, _with_token("DDDD", foreign), sign=False) as socket:
                socket.receive_json()
        assert other_hub.value.code == CODE_TAKEN


def test_an_older_token_still_reclaims_after_another_use():
    now = [1_700_000_000.0]
    hub = LiveHub(clock=lambda: now[0], init_timeout=5, idle_timeout=5)
    with TestClient(create_app(live=hub)) as client:
        with _ws(client, "/api/live/publish?id=OLDQ") as socket:
            first = _id_token(socket.receive_json(), "OLDQ")
        now[0] += 30
        with _ws(client, _with_token("OLDQ", first)) as socket:
            second = _id_token(socket.receive_json(), "OLDQ")
        assert second == first
        assert hub._seen["OLDQ"].mono == now[0]
        with _ws(client, _with_token("OLDQ", first)) as socket:
            assert socket.receive_json()["token"] == first


def test_a_token_stays_valid_for_12h_after_last_use():
    now = [1_000_000.0]
    hub = LiveHub(clock=lambda: now[0], init_timeout=5, idle_timeout=5)
    with TestClient(create_app(live=hub)) as client:
        with _ws(client, "/api/live/publish?id=LONG") as socket:
            token = _id_token(socket.receive_json(), "LONG")
        now[0] = 1_000_000 + TOKEN_TTL + 100
        hub._seen["LONG"].mono = now[0] - 50
        with _ws(client, _with_token("LONG", token)) as socket:
            assert socket.receive_json()["id"] == "LONG"


def test_a_live_stream_does_not_replace_its_token():
    hub = LiveHub(clock=lambda: 5_000_000.0, init_timeout=5, idle_timeout=5)
    with TestClient(create_app(live=hub)) as client:
        with _ws(client, "/api/live/publish?id=REFR") as socket:
            first = _id_token(socket.receive_json(), "REFR")
            with pytest.raises(concurrent.futures.TimeoutError):
                _receive_json(socket, 0.2)
        with _ws(client, _with_token("REFR", first)) as socket:
            assert socket.receive_json()["token"] == first


def test_one_address_cannot_fill_remembered_ids():
    now = [1_700_000_000.0]
    hub = LiveHub(
        max_seen_per_ip=3,
        max_seen=10,
        max_per_ip=2,
        clock=lambda: now[0],
        init_timeout=5,
        idle_timeout=5,
    )
    headers = {"x-real-ip": "203.0.113.50"}
    other = {"x-real-ip": "203.0.113.51"}
    with TestClient(create_app(live=hub)) as client:
        ids = []
        for _ in range(3):
            with client.websocket_connect("/api/live/publish", headers=headers) as socket:
                socket.send_json({"type": "mime", "mime": DEFAULT_MIME})
                ids.append(socket.receive_json()["id"])
        with pytest.raises(WebSocketDisconnect) as full:
            with client.websocket_connect("/api/live/publish", headers=headers) as socket:
                socket.send_json({"type": "mime", "mime": DEFAULT_MIME})
                socket.receive_json()
        assert full.value.code == CODE_FULL
        assert set(ids) <= set(hub._seen)
        with client.websocket_connect("/api/live/publish", headers=other) as socket:
            socket.send_json({"type": "mime", "mime": DEFAULT_MIME})
            assert is_stream_id(socket.receive_json()["id"])
        now[0] += TOKEN_TTL
        with client.websocket_connect("/api/live/publish", headers=headers) as socket:
            socket.send_json({"type": "mime", "mime": DEFAULT_MIME})
            assert is_stream_id(socket.receive_json()["id"])


def test_a_full_table_evicts_the_hoarder_and_not_a_live_record():
    now = [1_700_000_000.0]
    hub = LiveHub(max_seen=3, max_seen_per_ip=64, clock=lambda: now[0], init_timeout=5, idle_timeout=5)
    hoarder = {"x-real-ip": "203.0.113.61"}
    other = {"x-real-ip": "203.0.113.62"}
    newcomer = {"x-real-ip": "203.0.113.63"}
    with TestClient(create_app(live=hub)) as client:
        with client.websocket_connect("/api/live/publish", headers=hoarder) as live:
            live.send_json({"type": "mime", "mime": DEFAULT_MIME})
            body = live.receive_json()
            live_id, live_token = body["id"], body["token"]
            now[0] += 1
            with client.websocket_connect("/api/live/publish", headers=hoarder) as socket:
                socket.send_json({"type": "mime", "mime": DEFAULT_MIME})
                idle = socket.receive_json()
            now[0] += 1
            with client.websocket_connect("/api/live/publish", headers=other) as socket:
                socket.send_json({"type": "mime", "mime": DEFAULT_MIME})
                kept = socket.receive_json()
            now[0] += 1
            with client.websocket_connect("/api/live/publish", headers=newcomer) as socket:
                socket.send_json({"type": "mime", "mime": DEFAULT_MIME})
                arrived = socket.receive_json()
            assert arrived["id"] not in {live_id, idle["id"], kept["id"]}
            assert idle["id"] not in hub._seen
            assert kept["id"] in hub._seen
            assert live_id in hub._seen
            assert hub.get(live_id) is not None
            with pytest.raises(WebSocketDisconnect) as evicted:
                with _ws(client, _with_token(idle["id"], idle["token"]), headers=hoarder) as socket:
                    socket.receive_json()
            assert evicted.value.code == CODE_TAKEN
            with _ws(client, _with_token(live_id, live_token), headers=newcomer) as socket:
                assert socket.receive_json()["token"] == live_token
            assert hub._seen[live_id].who == "203.0.113.61"


def test_a_restart_does_not_hand_out_a_remembered_id(tmp_path, monkeypatch):
    path = tmp_path / "live-seen.json"
    now = [1_700_000_000.0]
    first = LiveHub(seen_path=path, clock=lambda: now[0], init_timeout=5, idle_timeout=5)
    with TestClient(create_app(live=first)) as client:
        with _ws(client, "/api/live/publish?id=REST", headers={"x-real-ip": "203.0.113.9"}) as socket:
            token = _id_token(socket.receive_json(), "REST")
    stored = path.read_text(encoding="utf-8")
    assert "REST" in stored
    assert token not in stored
    assert "203.0.113.9" not in stored
    restarted = LiveHub(seen_path=path, clock=lambda: now[0], init_timeout=5, idle_timeout=5)
    assert "REST" in restarted._seen
    state = {"n": 0}

    def choice(_alphabet: str) -> str:
        letter = "REST"[state["n"] % 4]
        state["n"] += 1
        return letter

    monkeypatch.setattr("djtube.live.secrets.choice", choice)
    with TestClient(create_app(live=restarted)) as client:
        with pytest.raises(WebSocketDisconnect) as blocked:
            with client.websocket_connect("/api/live/publish") as socket:
                socket.send_json({"type": "mime", "mime": DEFAULT_MIME})
                socket.receive_json()
        assert blocked.value.code == CODE_FULL
        assert "REST" in restarted._seen
        with _ws(client, _with_token("REST", token)) as socket:
            assert socket.receive_json()["id"] == "REST"


def test_a_minted_id_remembers_seq_so_an_older_one_cannot_reclaim():
    hub = LiveHub(clock=lambda: 1_700_000_000.0, init_timeout=5, idle_timeout=5)
    with TestClient(create_app(live=hub)) as client:
        with client.websocket_connect("/api/live/publish") as socket:
            socket.send_json({"type": "mime", "mime": DEFAULT_MIME, "seq": 2})
            body = socket.receive_json()
            stream_id = body["id"]
            token = body["token"]
            assert hub._seen[stream_id].seq == 2
            with pytest.raises(WebSocketDisconnect) as older:
                with _ws(client, _with_token(stream_id, token), seq=1) as stale:
                    stale.receive_json()
            assert older.value.code == CODE_REPLACED
            assert hub.get(stream_id) is not None


def test_create_app_uses_the_seen_file(tmp_path, monkeypatch):
    path = tmp_path / "live-seen.json"
    monkeypatch.setattr("djtube.live._DEFAULT_SEEN", path)
    calls = []
    real = LiveHub

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return real(*args, **kwargs)

    monkeypatch.setattr("djtube.app.LiveHub", spy)
    create_app()
    assert calls[0][0] == ()
    assert calls[0][1].get("seen_path") == path


def test_live_seen_path_defaults_beside_the_package(monkeypatch):
    monkeypatch.setattr("djtube.live._DEFAULT_SEEN", None)
    from djtube.paths import PACKAGE_DIR

    assert live_seen_path() == PACKAGE_DIR.parent / "data" / "live-seen.json"


def test_seen_limits_count_ipv6_by_56_and_keep_the_stated_caps():
    assert MAX_SEEN == 4096
    assert MAX_SEEN_PER_IP == 64
    assert MAX_PENDING == 64
    assert MAX_PENDING_PER_IP == 2
    hub = LiveHub(max_seen_per_ip=2, max_per_ip=2, max_streams=8, clock=lambda: 30.0, init_timeout=5, idle_timeout=5)
    same = (
        "2001:db8:abcd:5600::1",
        "2001:db8:abcd:56ff::1",
        "2001:db8:abcd:5610::1",
    )
    other = "2001:db8:abcd:5700::1"
    with TestClient(create_app(live=hub)) as client:
        issued = []
        for address in same[:2]:
            with client.websocket_connect("/api/live/publish", headers={"x-real-ip": address}) as socket:
                socket.send_json({"type": "mime", "mime": DEFAULT_MIME})
                issued.append(socket.receive_json()["id"])
        with pytest.raises(WebSocketDisconnect) as full:
            with client.websocket_connect("/api/live/publish", headers={"x-real-ip": same[2]}) as socket:
                socket.send_json({"type": "mime", "mime": DEFAULT_MIME})
                socket.receive_json()
        assert full.value.code == CODE_FULL
        assert set(issued) <= set(hub._seen)
        with client.websocket_connect("/api/live/publish", headers={"x-real-ip": other}) as socket:
            socket.send_json({"type": "mime", "mime": DEFAULT_MIME})
            assert is_stream_id(socket.receive_json()["id"])


def test_pending_slots_are_not_live_slots_and_a_reclaim_is_free():
    hub = LiveHub(
        max_streams=1,
        max_pending=1,
        max_pending_per_ip=1,
        max_per_ip=2,
        claim_timeout=2,
        init_timeout=5,
        clock=lambda: 20.0,
    )
    headers = {"x-real-ip": "203.0.113.70"}
    with TestClient(create_app(live=hub)) as client:
        with _ws(client, "/api/live/publish?id=AAAA", headers=headers, seq=1) as live:
            token = _id_token(live.receive_json(), "AAAA")
            with client.websocket_connect("/api/live/publish", headers=headers):
                with pytest.raises(WebSocketDisconnect) as full:
                    with client.websocket_connect("/api/live/publish", headers={"x-real-ip": "203.0.113.71"}) as extra:
                        extra.receive_json()
                assert full.value.code == CODE_FULL
                with _ws(client, _with_token("AAAA", token), headers=headers, seq=2) as again:
                    assert again.receive_json()["token"] == token


def test_open_and_end_are_durable_immediately(tmp_path):
    path = tmp_path / "live-seen.json"
    now = [10.0]
    hub = LiveHub(seen_path=path, clock=lambda: now[0], init_timeout=5, idle_timeout=5)
    with TestClient(create_app(live=hub)) as client:
        with client.websocket_connect("/api/live/publish", headers={"x-real-ip": "203.0.113.8"}) as socket:
            socket.send_json({"type": "mime", "mime": DEFAULT_MIME})
            body = socket.receive_json()
            record = json.loads(path.read_text(encoding="utf-8"))["records"][body["id"]]
            assert record["at"] == 10.0
            assert set(record) <= {"at", "token", "seq"}
            text = path.read_text(encoding="utf-8")
            assert body["token"] not in text
            assert "203.0.113.8" not in text
            who = hub._seen[body["id"]].who
            now[0] = 40.0
            with _ws(client, _with_token(body["id"], body["token"]), headers={"x-real-ip": "198.51.100.9"}) as again:
                assert again.receive_json()["token"] == body["token"]
            assert hub._seen[body["id"]].who == who
            assert hub._seen[body["id"]].mono == 40.0
        hub.flush()
        assert json.loads(path.read_text(encoding="utf-8"))["records"][body["id"]]["at"] == 40.0
    assert "198.51.100.9" not in path.read_text(encoding="utf-8")
    assert not Path(str(path) + ".bak").exists()


def test_the_end_of_publish_waits_for_the_write(tmp_path, monkeypatch):
    path = tmp_path / "live-seen.json"
    now = [10.0]
    hub = LiveHub(seen_path=path, clock=lambda: now[0], init_timeout=5, idle_timeout=5)
    entered = threading.Event()
    release = threading.Event()

    def gated(target, payload):
        if any(item.get("at") == 55.0 for item in payload.get("records", {}).values()):
            entered.set()
            assert release.wait(2)
        _write_json_atomic(target, payload)

    monkeypatch.setattr("djtube.live._write_json_atomic", gated)

    class _Socket:
        def __init__(self) -> None:
            self.headers = {"x-real-ip": "203.0.113.8"}
            self.client = None
            self.application_state = WebSocketState.CONNECTED
            self._messages: asyncio.Queue = asyncio.Queue()
            self.sent: list[dict] = []

        async def accept(self) -> None:
            return None

        async def receive(self) -> dict:
            return await self._messages.get()

        async def send_json(self, data: dict) -> None:
            self.sent.append(data)
            now[0] = 55.0

        async def close(self, code: int = 1000) -> None:
            self.application_state = WebSocketState.DISCONNECTED

    async def run() -> None:
        socket = _Socket()
        await socket._messages.put(
            {"type": "websocket.receive", "text": json.dumps({"type": "mime", "mime": DEFAULT_MIME})}
        )
        await socket._messages.put({"type": "websocket.disconnect"})
        task = asyncio.create_task(hub.publish(socket))
        assert await asyncio.to_thread(entered.wait, 2)
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        await task
        stored = json.loads(path.read_text(encoding="utf-8"))
        assert stored["records"][socket.sent[0]["id"]]["at"] == 55.0
        assert "203.0.113.8" not in path.read_text(encoding="utf-8")

    asyncio.run(run())


def test_load_drops_expired_rows_orders_by_last_use_and_ignores_a_bad_seq(tmp_path):
    path = tmp_path / "live-seen.json"
    now = 1_000_000.0
    fresh = "fresh-token"
    older = "older-token"
    records = {
        "NEWQ": {
            "at": now - 10,
            "token": hashlib.sha256(fresh.encode()).hexdigest(),
            "who": "ab" * 16,
            "seq": True,
        },
        "OLDQ": {
            "at": now - 100,
            "token": hashlib.sha256(older.encode()).hexdigest(),
            "who": "ab" * 16,
            "seq": 2**53,
        },
        "DEAD": {
            "at": now - TOKEN_TTL - 1,
            "token": "cd" * 32,
            "who": "ab" * 16,
            "seq": 1,
        },
    }
    path.write_text(json.dumps({"salt": "ef" * 16, "records": records}), encoding="utf-8")
    hub = LiveHub(seen_path=path, clock=lambda: now, init_timeout=5, idle_timeout=5)
    assert list(hub._seen) == ["OLDQ", "NEWQ"]
    assert hub._seen["NEWQ"].seq is None
    assert hub._seen["OLDQ"].seq is None
    assert hub._seen["OLDQ"].who == "~OLDQ"
    assert hub._seen["NEWQ"].who == "~NEWQ"
    with TestClient(create_app(live=hub)) as client:
        with _ws(client, _with_token("NEWQ", fresh), seq=1) as socket:
            assert socket.receive_json()["id"] == "NEWQ"
        with _ws(client, _with_token("OLDQ", older), seq=1) as socket:
            assert socket.receive_json()["id"] == "OLDQ"


def test_a_corrupt_seen_file_disables_streaming_and_leaves_the_file(tmp_path):
    path = tmp_path / "live-seen.json"
    sentinel = "CORRUPT-SENTINEL-DO-NOT-LOG"
    path.write_text(sentinel, encoding="utf-8")
    records: list[str] = []

    class _Grab(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record.getMessage())

    logger = logging.getLogger("djtube.live")
    handler = _Grab()
    logger.addHandler(handler)
    try:
        with pytest.raises(OSError):
            LiveHub(seen_path=path, clock=lambda: 70.0, init_timeout=5, idle_timeout=5)
    finally:
        logger.removeHandler(handler)
    assert sentinel not in "\n".join(records)
    assert path.read_text(encoding="utf-8") == sentinel
    assert not (tmp_path / "live-seen.json.bad").exists()
    assert not (tmp_path / "live-seen.json.bak").exists()
    with TestClient(create_app()) as client:
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/health").json()["live"] == "unreadable"
        assert client.get("/").status_code == 200
        with pytest.raises(WebSocketDisconnect) as off:
            with client.websocket_connect("/api/live/publish") as socket:
                socket.receive_json()
        assert off.value.code == CODE_UNREADABLE
    assert path.read_text(encoding="utf-8") == sentinel
    assert str(path) in "\n".join(records)


def test_a_truncated_seen_file_disables_streaming(tmp_path):
    path = tmp_path / "live-seen.json"
    path.write_bytes(b'{"records"')
    with pytest.raises(OSError):
        LiveHub(seen_path=path, clock=lambda: 80.0)
    assert path.read_bytes() == b'{"records"'
    assert list(tmp_path.glob("*.bad")) == []
    assert list(tmp_path.glob("*.bak")) == []


def test_a_read_only_directory_refuses_new_ids_and_still_reclaims(tmp_path):
    folder = tmp_path / "store"
    folder.mkdir()
    path = folder / "live-seen.json"
    hub = LiveHub(seen_path=path, clock=lambda: 90.0, init_timeout=5, idle_timeout=5)
    with TestClient(create_app(live=hub)) as client:
        with _ws(client, "/api/live/publish?id=MINE") as socket:
            token = _id_token(socket.receive_json(), "MINE")
        os.chmod(folder, 0o555)
        try:
            with pytest.raises(WebSocketDisconnect) as denied:
                with client.websocket_connect("/api/live/publish") as socket:
                    socket.send_json({"type": "mime", "mime": DEFAULT_MIME})
                    socket.receive_json()
            assert denied.value.code == CODE_STORE
            assert client.get("/api/health").json()["live"] == "store"
            assert set(hub._seen) == {"MINE"}
            with _ws(client, _with_token("MINE", token)) as socket:
                assert socket.receive_json()["token"] == token
            # That reclaim could not write either, so health stays store.
            assert client.get("/api/health").json()["live"] == "store"
        finally:
            os.chmod(folder, 0o755)
        with _ws(client, _with_token("MINE", token)) as socket:
            assert socket.receive_json()["token"] == token
            assert client.get("/api/health").json()["live"] == "on"


def test_a_short_os_write_still_leaves_a_complete_file(tmp_path, monkeypatch):
    path = tmp_path / "live-seen.json"
    real_write = os.write
    real_fsync = os.fsync
    fsyncs = {"n": 0}

    def short(fd, data):
        view = data if isinstance(data, memoryview) else memoryview(data)
        piece = view[:1] if len(view) > 1 else view
        return real_write(fd, piece)

    def fsync(fd):
        fsyncs["n"] += 1
        return real_fsync(fd)

    monkeypatch.setattr("djtube.live.os.write", short)
    monkeypatch.setattr("djtube.live.os.fsync", fsync)
    hub = LiveHub(seen_path=path, clock=lambda: 15.0, init_timeout=5, idle_timeout=5)
    with TestClient(create_app(live=hub)) as client:
        with client.websocket_connect("/api/live/publish") as socket:
            socket.send_json({"type": "mime", "mime": DEFAULT_MIME})
            body = socket.receive_json()
    hub.flush()
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert body["id"] in stored["records"]
    assert stored["records"][body["id"]]["token"] == hashlib.sha256(body["token"].encode()).hexdigest()
    assert fsyncs["n"] >= 1


def test_a_late_wait_is_success_and_a_failed_write_drops_only_the_new_id(tmp_path, monkeypatch):
    path = tmp_path / "live-seen.json"
    writer = _SeenWriter(path)
    generations = [writer.submit({"records": {}}) for _ in range(10)]
    assert writer.wait(generations[-1]) is True
    assert writer.wait(generations[0]) is True
    writer.flush()

    fail = {"on": False}
    payloads: list[dict] = []
    real_write = _write_json_atomic

    def maybe(target, payload):
        payloads.append(payload)
        if fail["on"]:
            raise OSError("enospc")
        real_write(target, payload)

    monkeypatch.setattr("djtube.live._write_json_atomic", maybe)
    hub = LiveHub(seen_path=path, clock=lambda: 50.0, init_timeout=5, idle_timeout=5)
    kept_headers = {"x-real-ip": "203.0.113.20"}
    attacker = "203.0.113.21"
    with TestClient(create_app(live=hub)) as client:
        with client.websocket_connect("/api/live/publish", headers=kept_headers) as socket:
            socket.send_json({"type": "mime", "mime": DEFAULT_MIME})
            kept = socket.receive_json()
        hub.flush()
        fail["on"] = True
        with pytest.raises(WebSocketDisconnect) as denied:
            with client.websocket_connect("/api/live/publish", headers={"x-real-ip": attacker}) as socket:
                socket.send_json({"type": "mime", "mime": DEFAULT_MIME})
                socket.receive_json()
        assert denied.value.code == CODE_STORE
        hub.flush()
        assert set(payloads[-1]["records"]) == {kept["id"]}
        assert set(hub._seen) == {kept["id"]}
        assert hub._streams == {}
        assert hub._issued[attacker] == [50.0]
        assert hub._seen[kept["id"]].token == kept["token"]
        with _ws(client, _with_token(kept["id"], kept["token"]), headers=kept_headers) as socket:
            assert socket.receive_json()["token"] == kept["token"]
        assert hub._issued[attacker] == [50.0]

    fail["on"] = False
    tight_path = tmp_path / "tight.json"
    now = [80.0]
    tight = LiveHub(seen_path=tight_path, max_seen=1, clock=lambda: now[0], init_timeout=5, idle_timeout=5)
    one = "203.0.113.40"
    with TestClient(create_app(live=tight)) as client:
        with client.websocket_connect("/api/live/publish", headers={"x-real-ip": one}) as socket:
            socket.send_json({"type": "mime", "mime": DEFAULT_MIME})
            first = socket.receive_json()
        tight.flush()
        fail["on"] = True
        now[0] = 90.0
        with pytest.raises(WebSocketDisconnect) as denied:
            with client.websocket_connect("/api/live/publish", headers={"x-real-ip": one}) as socket:
                socket.send_json({"type": "mime", "mime": DEFAULT_MIME})
                socket.receive_json()
        assert denied.value.code == CODE_STORE
        assert tight._seen == {}
        assert tight._streams == {}
        assert tight._issued[one] == [80.0, 90.0]
        with pytest.raises(WebSocketDisconnect) as gone:
            with _ws(client, _with_token(first["id"], first["token"]), headers={"x-real-ip": one}) as socket:
                socket.receive_json()
        assert gone.value.code == CODE_TAKEN
        assert tight._issued[one] == [80.0, 90.0]


def test_pending_sockets_from_one_56_cannot_fill_the_table():
    hub = LiveHub(max_pending=16, max_pending_per_ip=2, claim_timeout=2, init_timeout=5)
    same = ("2001:db8:abcd:5600::1", "2001:db8:abcd:5601::1", "2001:db8:abcd:56ff::1")
    with TestClient(create_app(live=hub)) as client:
        with _ws(client, "/api/live/publish?id=LIVE", headers={"x-real-ip": "203.0.113.77"}, seq=1) as live:
            token = _id_token(live.receive_json(), "LIVE")
            with contextlib.ExitStack() as stack:
                for address in same[:2]:
                    stack.enter_context(client.websocket_connect("/api/live/publish", headers={"x-real-ip": address}))
                with pytest.raises(WebSocketDisconnect) as full:
                    with client.websocket_connect("/api/live/publish", headers={"x-real-ip": same[2]}) as extra:
                        extra.receive_json()
                assert full.value.code == CODE_FULL
                with client.websocket_connect(
                    "/api/live/publish", headers={"x-real-ip": "2001:db8:abcd:5700::1"}
                ) as other:
                    other.send_json({"type": "mime", "mime": DEFAULT_MIME})
                    assert is_stream_id(other.receive_json()["id"])
                with _ws(client, _with_token("LIVE", token), headers={"x-real-ip": "203.0.113.77"}, seq=2) as again:
                    assert again.receive_json()["token"] == token


def test_pending_sockets_from_one_48_cannot_fill_the_table():
    hub = LiveHub(max_pending=16, max_pending_per_ip=2, max_pending_per_48=4, claim_timeout=2, init_timeout=5)
    first = ("2001:db8:ab00:0000::1", "2001:db8:ab00:0001::1")
    second = ("2001:db8:ab00:0100::1", "2001:db8:ab00:0101::1")
    with TestClient(create_app(live=hub)) as client:
        with contextlib.ExitStack() as stack:
            for address in first + second:
                stack.enter_context(client.websocket_connect("/api/live/publish", headers={"x-real-ip": address}))
            with client.websocket_connect("/api/live/publish", headers={"x-real-ip": "203.0.113.9"}) as v4:
                v4.send_json({"type": "mime", "mime": DEFAULT_MIME})
                assert is_stream_id(v4.receive_json()["id"])
            with pytest.raises(WebSocketDisconnect) as full:
                with client.websocket_connect(
                    "/api/live/publish", headers={"x-real-ip": "2001:db8:ab00:0200::1"}
                ) as extra:
                    extra.receive_json()
            assert full.value.code == CODE_FULL
            with client.websocket_connect("/api/live/publish", headers={"x-real-ip": "2001:db8:ab01::1"}) as other:
                other.send_json({"type": "mime", "mime": DEFAULT_MIME})
                assert is_stream_id(other.receive_json()["id"])


def test_a_tie_drops_the_newer_prefix_and_issuances_survive_eviction():
    now = [1_000.0]
    hub = LiveHub(max_seen=2, max_seen_per_ip=64, clock=lambda: now[0], init_timeout=5, idle_timeout=5)
    legit = {"x-real-ip": "203.0.113.31"}
    newer = {"x-real-ip": "203.0.113.32"}
    with TestClient(create_app(live=hub)) as client:
        with client.websocket_connect("/api/live/publish", headers=legit) as socket:
            socket.send_json({"type": "mime", "mime": DEFAULT_MIME})
            old = socket.receive_json()
        now[0] += 10
        with client.websocket_connect("/api/live/publish", headers=newer) as socket:
            socket.send_json({"type": "mime", "mime": DEFAULT_MIME})
            fresh = socket.receive_json()
        now[0] += 10
        with client.websocket_connect("/api/live/publish", headers={"x-real-ip": "203.0.113.33"}) as socket:
            socket.send_json({"type": "mime", "mime": DEFAULT_MIME})
            arrived = socket.receive_json()
        assert old["id"] in hub._seen
        assert fresh["id"] not in hub._seen
        assert arrived["id"] in hub._seen

    issued = LiveHub(max_seen=1, max_seen_per_ip=2, clock=lambda: now[0], init_timeout=5, idle_timeout=5)
    one = {"x-real-ip": "203.0.113.40"}
    with TestClient(create_app(live=issued)) as client:
        for _ in range(2):
            now[0] += 1
            with client.websocket_connect("/api/live/publish", headers=one) as socket:
                socket.send_json({"type": "mime", "mime": DEFAULT_MIME})
                body = socket.receive_json()
        kept = body["id"]
        now[0] += 1
        with pytest.raises(WebSocketDisconnect) as full:
            with client.websocket_connect("/api/live/publish", headers=one) as socket:
                socket.send_json({"type": "mime", "mime": DEFAULT_MIME})
                socket.receive_json()
        assert full.value.code == CODE_FULL
        assert set(issued._seen) == {kept}


def test_an_unreadable_seen_file_does_not_move_and_streaming_stays_off(tmp_path, monkeypatch):
    seen = tmp_path / "live-seen.json"
    seen.write_text("keep-me", encoding="utf-8")
    real_open = os.open

    def opened(file, flags, *args, **kwargs):
        if Path(file) == seen:
            raise PermissionError("mode 000")
        return real_open(file, flags, *args, **kwargs)

    monkeypatch.setattr("djtube.live.os.open", opened)
    moved: list[str] = []
    real_replace = os.replace

    def replace(src, dst):
        moved.append(os.fspath(dst))
        return real_replace(src, dst)

    monkeypatch.setattr("djtube.live.os.replace", replace)
    with TestClient(create_app()) as client:
        assert client.get("/api/health").status_code == 200
        assert client.get("/").status_code == 200
        with pytest.raises(WebSocketDisconnect) as off:
            with client.websocket_connect("/api/live/publish") as socket:
                socket.receive_json()
        assert off.value.code == CODE_UNREADABLE
    assert moved == []
    assert seen.read_bytes() == b"keep-me"


def test_a_corrupt_file_on_a_read_only_directory_keeps_the_page(tmp_path, monkeypatch):
    seen = tmp_path / "live-seen.json"
    seen.write_text("{", encoding="utf-8")

    def boom(_src, _dst):
        raise PermissionError("read-only directory")

    monkeypatch.setattr("djtube.live.os.replace", boom)
    with TestClient(create_app()) as client:
        assert client.get("/api/health").status_code == 200
        assert client.get("/").status_code == 200
        with pytest.raises(WebSocketDisconnect) as off:
            with client.websocket_connect("/api/live/publish") as socket:
                socket.receive_json()
        assert off.value.code == CODE_UNREADABLE
    assert seen.read_text(encoding="utf-8") == "{"


def test_startup_drops_a_leftover_tmp(tmp_path):
    path = tmp_path / "live-seen.json"
    token_hash = "ab" * 32
    path.write_text(json.dumps({"records": {"KEEP": {"at": 10.0, "token": token_hash}}}), encoding="utf-8")
    tmp = Path(str(path) + ".tmp")
    tmp.write_text("partial", encoding="utf-8")
    hub = LiveHub(seen_path=path, clock=lambda: 10.0)
    assert hub._seen["KEEP"].who == "~KEEP"
    assert not tmp.exists()


def test_a_restart_does_not_evict_loaded_ids_as_one_owner(tmp_path):
    path = tmp_path / "live-seen.json"
    now = [1_000.0]
    hub = LiveHub(
        seen_path=path,
        max_seen=6,
        max_seen_per_ip=64,
        clock=lambda: now[0],
        init_timeout=5,
        idle_timeout=5,
    )
    legit = [f"203.0.113.{index}" for index in range(1, 4)]
    attacker = "203.0.113.50"
    kept: list[str] = []
    with TestClient(create_app(live=hub)) as client:
        for address in legit:
            now[0] += 10
            with client.websocket_connect("/api/live/publish", headers={"x-real-ip": address}) as socket:
                socket.send_json({"type": "mime", "mime": DEFAULT_MIME})
                kept.append(socket.receive_json()["id"])
        for _ in range(3):
            now[0] += 10
            with client.websocket_connect("/api/live/publish", headers={"x-real-ip": attacker}) as socket:
                socket.send_json({"type": "mime", "mime": DEFAULT_MIME})
                socket.receive_json()
    hub.flush()
    restarted = LiveHub(
        seen_path=path,
        max_seen=6,
        max_seen_per_ip=64,
        clock=lambda: now[0],
        init_timeout=5,
        idle_timeout=5,
    )
    assert {restarted._seen[stream_id].who for stream_id in kept} == {f"~{stream_id}" for stream_id in kept}
    assert len({seen.who for seen in restarted._seen.values()}) == 6
    with TestClient(create_app(live=restarted)) as client:
        for _ in range(4):
            now[0] += 10
            with client.websocket_connect("/api/live/publish", headers={"x-real-ip": attacker}) as socket:
                socket.send_json({"type": "mime", "mime": DEFAULT_MIME})
                socket.receive_json()
    assert set(kept) <= set(restarted._seen)


def test_expired_issuance_counts_are_forgotten():
    now = [1_000.0]
    hub = LiveHub(token_ttl=100, clock=lambda: now[0], init_timeout=5, idle_timeout=5)
    old = "203.0.113.80"
    with TestClient(create_app(live=hub)) as client:
        with client.websocket_connect("/api/live/publish", headers={"x-real-ip": old}) as socket:
            socket.send_json({"type": "mime", "mime": DEFAULT_MIME})
            socket.receive_json()
        assert old in hub._issued
        now[0] = 1_100.0
        with client.websocket_connect("/api/live/publish", headers={"x-real-ip": "198.51.100.1"}) as socket:
            socket.send_json({"type": "mime", "mime": DEFAULT_MIME})
            socket.receive_json()
    assert old not in hub._issued


def test_live_tone_claim_sends_seq():
    from djtube.live_tone import claim_payload

    claim = claim_payload(now=1_700_000_000.5)
    assert claim["seq"] == 1_700_000_000_500
    assert "id" not in claim
    again = claim_payload("ABCD", "secret", now=0.0001)
    assert again["seq"] == 1
    assert again["id"] == "ABCD"
    assert again["token"] == "secret"


class _ToneSocket:
    def __init__(self, hello: dict) -> None:
        self.sent: list[object] = []
        self._hello = json.dumps(hello)
        self._once = False

    async def send(self, data: object) -> None:
        self.sent.append(data)

    async def recv(self) -> str:
        if self._once:
            raise RuntimeError("extra recv")
        self._once = True
        return self._hello

    async def __aenter__(self) -> "_ToneSocket":
        return self

    async def __aexit__(self, *_args: object) -> bool:
        return False


def _run_tone(monkeypatch, argv: list[str], hello: dict | None = None) -> tuple[int, _ToneSocket]:
    import websockets

    from djtube import live_tone

    socket = _ToneSocket(hello or {"id": "ABCD", "token": "hello-token"})

    async def connect(_url: str, **_kwargs: object) -> _ToneSocket:
        return socket

    monkeypatch.setattr(live_tone, "render_webm", lambda *_args, **_kwargs: b"\x1a\x45\xdf\xa3tone")
    monkeypatch.setattr(live_tone, "split_webm", lambda _data: (b"init", [b"cl"]))
    monkeypatch.setattr(websockets, "connect", connect)
    return live_tone.main(argv), socket


def test_live_tone_main_sends_the_cli_token_and_prints_the_hello_token(monkeypatch, capsys):
    code, socket = _run_tone(
        monkeypatch,
        ["--id", "ABCD", "--token", "cli-token", "--no-pace", "--seconds", "1"],
    )
    assert code == 0
    claim = json.loads(socket.sent[0])
    assert claim["id"] == "ABCD"
    assert claim["token"] == "cli-token"
    assert claim["type"] == "mime"
    printed = capsys.readouterr().out
    assert "token: hello-token" in printed
    assert "cli-token" not in printed


def test_live_tone_main_refuses_an_id_without_a_token(monkeypatch, capsys):
    from djtube import live_tone

    rendered = {"n": 0}

    def render(*_args, **_kwargs):
        rendered["n"] += 1
        return b"webm"

    monkeypatch.setattr(live_tone, "render_webm", render)
    assert live_tone.main(["--id", "ABCD", "--seconds", "1"]) == 2
    assert rendered["n"] == 0
    assert "トークン" in capsys.readouterr().err


def test_live_tone_main_sends_now_after_hello(monkeypatch):
    code, socket = _run_tone(
        monkeypatch,
        ["--no-pace", "--seconds", "1", "--now", "abcdefghijk:0.5"],
    )
    assert code == 0
    claim = json.loads(socket.sent[0])
    assert "id" not in claim
    now = json.loads(socket.sent[1])
    assert now == {"type": "now", "decks": [{"video": "abcdefghijk", "gain": 0.5}]}
    assert isinstance(socket.sent[2], bytes)


def test_live_tone_close_codes_name_the_reason():
    from djtube.live_tone import _connect_error

    class _Frame:
        def __init__(self, code: int) -> None:
            self.code = code

    class _Exc(Exception):
        def __init__(self, code: int) -> None:
            self.rcvd = _Frame(code)

    assert str(_connect_error(_Exc(4410))) == "配信が切れました"
    assert str(_connect_error(_Exc(4429))) == "配信の上限に達しました"
    assert str(_connect_error(_Exc(4430))) == "配信の記録を保存できませんでした"
    assert str(_connect_error(_Exc(4431))) == "配信の記録を読めませんでした"
    assert str(_connect_error(_Exc(4409))) == "その ID は使われています"
    assert str(_connect_error(_Exc(4400))) == "ID の形式が違います"


def _unreadable_log(path: Path) -> list[str]:
    records: list[str] = []

    class _Grab(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record.getMessage())

    logger = logging.getLogger("djtube.live")
    handler = _Grab()
    logger.addHandler(handler)
    try:
        with pytest.raises(OSError):
            LiveHub(seen_path=path, clock=lambda: 70.0, init_timeout=5, idle_timeout=5)
    finally:
        logger.removeHandler(handler)
    return records


def test_a_non_utf8_seen_file_disables_streaming_and_leaves_the_bytes(tmp_path):
    path = tmp_path / "live-seen.json"
    raw = b"\xff\xfe" + "読めない".encode("utf-16-le")
    path.write_bytes(raw)
    records = _unreadable_log(path)
    assert path.read_bytes() == raw
    assert str(path) in "\n".join(records)
    assert "\\xff" not in "\n".join(records)
    joined = "\n".join(records)
    assert "\xff" not in joined
    with TestClient(create_app()) as client:
        assert client.get("/api/health").status_code == 200
        assert client.get("/").status_code == 200
        with pytest.raises(WebSocketDisconnect) as off:
            with client.websocket_connect("/api/live/publish") as socket:
                socket.receive_json()
        assert off.value.code == CODE_UNREADABLE
    assert path.read_bytes() == raw


def test_a_seen_directory_disables_streaming(tmp_path):
    path = tmp_path / "live-seen.json"
    path.mkdir()
    records = _unreadable_log(path)
    assert path.is_dir()
    assert str(path) in "\n".join(records)
    with TestClient(create_app()) as client:
        assert client.get("/api/health").status_code == 200
        assert client.get("/").status_code == 200
        with pytest.raises(WebSocketDisconnect) as off:
            with client.websocket_connect("/api/live/publish") as socket:
                socket.receive_json()
        assert off.value.code == CODE_UNREADABLE
    assert path.is_dir()


def test_a_full_live_table_does_not_count_the_refused_id_or_evict():
    now = [1_000.0]
    hub = LiveHub(max_streams=1, max_seen=2, clock=lambda: now[0], init_timeout=5, idle_timeout=5)
    owner = {"x-real-ip": "203.0.113.1"}
    other = {"x-real-ip": "203.0.113.2"}
    blocked = "203.0.113.3"
    with TestClient(create_app(live=hub)) as client:
        with client.websocket_connect("/api/live/publish", headers=owner) as socket:
            socket.send_json({"type": "mime", "mime": DEFAULT_MIME})
            first = socket.receive_json()
        now[0] += 1
        with client.websocket_connect("/api/live/publish", headers=other) as socket:
            socket.send_json({"type": "mime", "mime": DEFAULT_MIME})
            idle = socket.receive_json()
        now[0] += 1
        with _ws(client, _with_token(first["id"], first["token"]), headers=owner, seq=2) as live:
            assert live.receive_json()["id"] == first["id"]
            with pytest.raises(WebSocketDisconnect) as full:
                with client.websocket_connect("/api/live/publish", headers={"x-real-ip": blocked}) as socket:
                    socket.send_json({"type": "mime", "mime": DEFAULT_MIME})
                    socket.receive_json()
            assert full.value.code == CODE_FULL
    assert idle["id"] in hub._seen
    assert first["id"] in hub._seen
    assert blocked not in hub._issued


def test_a_second_reclaim_does_not_get_a_free_slot():
    hub = LiveHub(max_pending=1, max_pending_per_ip=1, claim_timeout=2, init_timeout=5)
    headers = {"x-real-ip": "203.0.113.70"}
    with TestClient(create_app(live=hub)) as client:
        with _ws(client, "/api/live/publish?id=AAAA", headers=headers, seq=1) as live:
            _id_token(live.receive_json(), "AAAA")
            with client.websocket_connect("/api/live/publish", headers=headers):
                with client.websocket_connect("/api/live/publish", headers=headers):
                    with pytest.raises(WebSocketDisconnect) as full:
                        with client.websocket_connect("/api/live/publish", headers=headers) as extra:
                            extra.receive_json()
                    assert full.value.code == CODE_FULL


def test_a_live_record_stays_and_an_idle_one_behind_it_expires():
    now = [1_000.0]
    hub = LiveHub(token_ttl=100, clock=lambda: now[0], init_timeout=5, idle_timeout=5)
    with TestClient(create_app(live=hub)) as client:
        with client.websocket_connect("/api/live/publish", headers={"x-real-ip": "203.0.113.1"}) as live:
            live.send_json({"type": "mime", "mime": DEFAULT_MIME})
            kept = live.receive_json()
            now[0] = 1_005.0
            with client.websocket_connect("/api/live/publish", headers={"x-real-ip": "203.0.113.2"}) as socket:
                socket.send_json({"type": "mime", "mime": DEFAULT_MIME})
                idle = socket.receive_json()
            now[0] = 1_110.0
            with client.websocket_connect("/api/live/publish", headers={"x-real-ip": "203.0.113.3"}) as socket:
                socket.send_json({"type": "mime", "mime": DEFAULT_MIME})
                socket.receive_json()
            assert kept["id"] in hub._seen
            assert hub.get(kept["id"]) is not None
            assert idle["id"] not in hub._seen


def test_a_prefix_with_a_fresh_stamp_keeps_an_older_one_until_it_issues():
    now = [1_000.0]
    hub = LiveHub(token_ttl=100, clock=lambda: now[0], init_timeout=5, idle_timeout=5)
    first = {"x-real-ip": "203.0.113.10"}
    with TestClient(create_app(live=hub)) as client:
        with client.websocket_connect("/api/live/publish", headers=first) as socket:
            socket.send_json({"type": "mime", "mime": DEFAULT_MIME})
            socket.receive_json()
        now[0] = 1_050.0
        with client.websocket_connect("/api/live/publish", headers=first) as socket:
            socket.send_json({"type": "mime", "mime": DEFAULT_MIME})
            socket.receive_json()
        now[0] = 1_100.0
        with client.websocket_connect("/api/live/publish", headers={"x-real-ip": "203.0.113.11"}) as socket:
            socket.send_json({"type": "mime", "mime": DEFAULT_MIME})
            socket.receive_json()
        assert hub._issued["203.0.113.10"] == [1_000.0, 1_050.0]
        now[0] = 1_110.0
        with client.websocket_connect("/api/live/publish", headers=first) as socket:
            socket.send_json({"type": "mime", "mime": DEFAULT_MIME})
            socket.receive_json()
        assert hub._issued["203.0.113.10"] == [1_050.0, 1_110.0]


def test_a_new_token_is_32_bytes_of_randomness():
    hub = LiveHub(init_timeout=5, idle_timeout=5)
    with TestClient(create_app(live=hub)) as client:
        with client.websocket_connect("/api/live/publish") as socket:
            socket.send_json({"type": "mime", "mime": DEFAULT_MIME})
            token = socket.receive_json()["token"]
    # token_urlsafe(32) is 43 characters. token_urlsafe(4) is 6.
    assert len(token) == 43


def test_restamping_a_prefix_does_not_leave_it_at_the_head():
    now = [1_000.0]
    hub = LiveHub(token_ttl=100, clock=lambda: now[0], init_timeout=5, idle_timeout=5)
    older = {"x-real-ip": "203.0.113.10"}
    newer = {"x-real-ip": "203.0.113.11"}
    with TestClient(create_app(live=hub)) as client:
        with client.websocket_connect("/api/live/publish", headers=older) as socket:
            socket.send_json({"type": "mime", "mime": DEFAULT_MIME})
            socket.receive_json()
        now[0] = 1_010.0
        with client.websocket_connect("/api/live/publish", headers=newer) as socket:
            socket.send_json({"type": "mime", "mime": DEFAULT_MIME})
            socket.receive_json()
        now[0] = 1_050.0
        with client.websocket_connect("/api/live/publish", headers=older) as socket:
            socket.send_json({"type": "mime", "mime": DEFAULT_MIME})
            socket.receive_json()
        now[0] = 1_120.0
        with client.websocket_connect("/api/live/publish", headers={"x-real-ip": "203.0.113.12"}) as socket:
            socket.send_json({"type": "mime", "mime": DEFAULT_MIME})
            socket.receive_json()
    assert "203.0.113.11" not in hub._issued
    assert hub._issued["203.0.113.10"][-1] == 1_050.0


def test_a_wall_clock_step_back_still_expires_issued_prefixes():
    wall = [1_700_000_000.0]
    mono = [0.0]
    ttl = 12 * 60 * 60
    hub = LiveHub(
        token_ttl=ttl,
        clock=lambda: wall[0],
        mono=lambda: mono[0],
        init_timeout=5,
        idle_timeout=5,
    )
    first = {"x-real-ip": "203.0.113.1"}
    second = {"x-real-ip": "203.0.113.2"}
    with TestClient(create_app(live=hub)) as client:
        with client.websocket_connect("/api/live/publish", headers=first) as socket:
            socket.send_json({"type": "mime", "mime": DEFAULT_MIME})
            socket.receive_json()
        wall[0] -= 3600
        mono[0] = 1.0
        with client.websocket_connect("/api/live/publish", headers=second) as socket:
            socket.send_json({"type": "mime", "mime": DEFAULT_MIME})
            socket.receive_json()
        wall[0] += ttl + 60
        mono[0] = 1.0 + ttl + 60
        with client.websocket_connect("/api/live/publish", headers={"x-real-ip": "203.0.113.3"}) as socket:
            socket.send_json({"type": "mime", "mime": DEFAULT_MIME})
            socket.receive_json()
    assert "203.0.113.1" not in hub._issued
    assert "203.0.113.2" not in hub._issued


def test_the_seen_file_stores_wall_time(tmp_path):
    path = tmp_path / "live-seen.json"
    wall = [5_000.0]
    mono = [10.0]
    hub = LiveHub(
        seen_path=path,
        clock=lambda: wall[0],
        mono=lambda: mono[0],
        init_timeout=5,
        idle_timeout=5,
    )
    with TestClient(create_app(live=hub)) as client:
        with client.websocket_connect("/api/live/publish") as socket:
            socket.send_json({"type": "mime", "mime": DEFAULT_MIME})
            body = socket.receive_json()
    hub.flush()
    stored = json.loads(path.read_text(encoding="utf-8"))["records"][body["id"]]
    assert stored["at"] == 5_000.0
    assert hub._seen[body["id"]].mono == 10.0


def test_a_wall_step_after_boot_is_what_the_file_stores(tmp_path):
    """A frozen boot wall would store 1010. The file follows the wall clock at the write."""

    path = tmp_path / "fresh.json"
    wall = [1_000.0]
    mono = [0.0]
    hub = LiveHub(
        seen_path=path,
        clock=lambda: wall[0],
        mono=lambda: mono[0],
        init_timeout=5,
        idle_timeout=5,
    )
    with TestClient(create_app(live=hub)) as client:
        with client.websocket_connect("/api/live/publish") as socket:
            socket.send_json({"type": "mime", "mime": DEFAULT_MIME})
            body = socket.receive_json()
            mono[0] = 10.0
            wall[0] = 6_000.0
    hub.flush()
    stored = json.loads(path.read_text(encoding="utf-8"))["records"][body["id"]]["at"]
    assert stored == pytest.approx(6_000.0)
    assert hub._seen[body["id"]].mono == 10.0


def test_expiring_many_issued_prefixes_is_bounded():
    from collections import OrderedDict

    hub = LiveHub(token_ttl=10, clock=lambda: 0.0, init_timeout=5, idle_timeout=5)
    for index in range(100_000):
        hub._issued[str(index)] = [0.0]
    started = time.perf_counter()
    hub._expire_issued_locked(100.0)
    elapsed = time.perf_counter() - started
    assert isinstance(hub._issued, OrderedDict)
    assert isinstance(hub._seen, OrderedDict)
    assert len(hub._issued) == 100_000 - 256
    assert elapsed < 0.25

    now = [0.0]
    seen = LiveHub(token_ttl=10, clock=lambda: now[0], init_timeout=5, idle_timeout=5)
    for index in range(300):
        seen._seen[f"{index:04d}"] = _Seen(0.0, None, "ab" * 32, "", "w")
    now[0] = 100.0
    seen._expire_locked()
    assert len(seen._seen) == 300 - 256


def test_an_expired_idle_record_behind_a_live_one_and_a_fresh_one_is_dropped():
    now = [1_000.0]
    hub = LiveHub(token_ttl=100, clock=lambda: now[0], init_timeout=5, idle_timeout=5)
    with TestClient(create_app(live=hub)) as client:
        with client.websocket_connect("/api/live/publish", headers={"x-real-ip": "203.0.113.1"}) as live:
            live.send_json({"type": "mime", "mime": DEFAULT_MIME})
            kept = live.receive_json()
            now[0] = 1_010.0
            with client.websocket_connect("/api/live/publish", headers={"x-real-ip": "203.0.113.2"}) as socket:
                socket.send_json({"type": "mime", "mime": DEFAULT_MIME})
                idle = socket.receive_json()
            now[0] = 1_050.0
            with client.websocket_connect("/api/live/publish", headers={"x-real-ip": "203.0.113.4"}) as socket:
                socket.send_json({"type": "mime", "mime": DEFAULT_MIME})
                fresh = socket.receive_json()
            now[0] = 1_120.0
            with client.websocket_connect("/api/live/publish", headers={"x-real-ip": "203.0.113.3"}) as socket:
                socket.send_json({"type": "mime", "mime": DEFAULT_MIME})
                socket.receive_json()
            assert kept["id"] in hub._seen
            assert hub.get(kept["id"]) is not None
            assert idle["id"] not in hub._seen
            assert fresh["id"] in hub._seen


def test_the_token_check_calls_compare_digest(monkeypatch):
    calls = {"n": 0}
    real = hmac.compare_digest

    def spy(left, right):
        calls["n"] += 1
        return real(left, right)

    monkeypatch.setattr("djtube.live.hmac.compare_digest", spy)
    hub = LiveHub(clock=lambda: 1_700_000_000.0, init_timeout=5, idle_timeout=5)
    with TestClient(create_app(live=hub)) as client:
        with client.websocket_connect("/api/live/publish") as socket:
            socket.send_json({"type": "mime", "mime": DEFAULT_MIME})
            body = socket.receive_json()
        with _ws(client, _with_token(body["id"], body["token"]), sign=False) as socket:
            assert socket.receive_json()["token"] == body["token"]
    assert calls["n"] >= 1


def test_the_extra_socket_cannot_mint_a_new_id():
    hub = LiveHub(max_pending=1, max_pending_per_ip=1, claim_timeout=2, init_timeout=5)
    headers = {"x-real-ip": "203.0.113.70"}
    with TestClient(create_app(live=hub)) as client:
        with _ws(client, "/api/live/publish?id=AAAA", headers=headers, seq=1) as live:
            token = _id_token(live.receive_json(), "AAAA")
            with client.websocket_connect("/api/live/publish", headers=headers):
                with pytest.raises(WebSocketDisconnect) as full:
                    with client.websocket_connect("/api/live/publish", headers=headers) as extra:
                        extra.send_json({"type": "mime", "mime": DEFAULT_MIME})
                        extra.receive_json()
                assert full.value.code == CODE_FULL
                assert headers["x-real-ip"] not in hub._issued
                with _ws(client, _with_token("AAAA", token), headers=headers, seq=2) as again:
                    assert again.receive_json()["token"] == token


def test_a_fifo_seen_path_does_not_block_and_stays_a_fifo(tmp_path):
    path = tmp_path / "live-seen.json"
    os.mkfifo(path)
    started = time.monotonic()
    records = _unreadable_log(path)
    assert time.monotonic() - started < 1
    assert stat.S_ISFIFO(path.lstat().st_mode)
    assert str(path) in "\n".join(records)


def test_a_symlink_seen_path_is_left_in_place(tmp_path):
    target = tmp_path / "elsewhere"
    target.write_text("keep", encoding="utf-8")
    path = tmp_path / "live-seen.json"
    path.symlink_to(target)
    records = _unreadable_log(path)
    assert path.is_symlink()
    assert target.read_text(encoding="utf-8") == "keep"
    assert str(path) in "\n".join(records)
    dangling = tmp_path / "loop"
    dangling.symlink_to(dangling)
    loop_log = _unreadable_log(dangling)
    assert dangling.is_symlink()
    assert str(dangling) in "\n".join(loop_log)
    with TestClient(create_app()) as client:
        assert client.get("/api/health").json()["live"] == "unreadable"
    assert stat.S_ISLNK(path.lstat().st_mode)
    assert target.read_text(encoding="utf-8") == "keep"


def test_a_symlink_to_a_valid_seen_file_is_refused(tmp_path):
    """O_NOFOLLOW. Following the link would load KEEP from a regular file."""

    target = tmp_path / "real.json"
    digest = "ab" * 32
    body = json.dumps({"records": {"KEEP": {"at": 70.0, "token": digest}}})
    target.write_text(body, encoding="utf-8")
    path = tmp_path / "live-seen.json"
    path.symlink_to(target)
    records = _unreadable_log(path)
    assert path.is_symlink()
    assert target.read_text(encoding="utf-8") == body
    assert str(path) in "\n".join(records)
    with TestClient(create_app()) as client:
        assert client.get("/api/health").json()["live"] == "unreadable"
        with pytest.raises(WebSocketDisconnect) as off:
            with client.websocket_connect("/api/live/publish") as socket:
                socket.receive_json()
        assert off.value.code == CODE_UNREADABLE
    assert path.is_symlink()
    assert target.read_text(encoding="utf-8") == body


def _many_ids(count: int) -> list[str]:
    ids: list[str] = []
    number = 0
    while len(ids) < count:
        value = number
        chars: list[str] = []
        for _ in range(4):
            chars.append(chr(ord("A") + (value % 26)))
            value //= 26
        ids.append("".join(chars))
        number += 1
    return ids


def test_load_reclaim_and_end_keep_wall_time_and_monotonic_time_apart(tmp_path):
    """MO3 sets a loaded mono from the wall ``at``. These clocks are far apart, so that never expires."""

    path = tmp_path / "live-seen.json"
    wall0 = 1_800_000_000.0
    token = "loaded-token"
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    path.write_text(
        json.dumps(
            {
                "records": {
                    "LOAD": {"at": wall0 - 10, "token": token_hash, "seq": 1},
                    "BADH": {"at": wall0 - 10, "token": "Z" * 64},
                    "SHORT": {"at": wall0 - 10, "token": "abcd"},
                    "FUTR": {"at": wall0 + 100_000, "token": token_hash},
                }
            }
        ),
        encoding="utf-8",
    )
    wall = [wall0]
    mono = [0.0]
    hub = LiveHub(
        seen_path=path,
        token_ttl=100,
        clock=lambda: wall[0],
        mono=lambda: mono[0],
        init_timeout=5,
        idle_timeout=5,
    )
    assert "BADH" not in hub._seen
    assert "SHORT" not in hub._seen
    assert hub._seen["LOAD"].mono == pytest.approx(-10.0)
    assert hub._seen["FUTR"].mono == pytest.approx(0.0)
    wall[0] = wall0 + 100_000
    hub._expire_locked()
    assert "LOAD" in hub._seen
    assert "FUTR" in hub._seen
    mono[0] = 150.0
    hub._expire_locked()
    assert "LOAD" not in hub._seen
    assert "FUTR" not in hub._seen

    fresh = tmp_path / "fresh.json"
    wall2 = [wall0]
    mono2 = [0.0]
    stamped = LiveHub(
        seen_path=fresh,
        token_ttl=10_000,
        clock=lambda: wall2[0],
        mono=lambda: mono2[0],
        init_timeout=5,
        idle_timeout=5,
    )
    with TestClient(create_app(live=stamped)) as client:
        with client.websocket_connect("/api/live/publish") as socket:
            socket.send_json({"type": "mime", "mime": DEFAULT_MIME})
            body = socket.receive_json()
            assert stamped._seen[body["id"]].mono == 0.0
            mono2[0] = 25.0
            wall2[0] = wall0 - 3600
            with _ws(client, _with_token(body["id"], body["token"]), seq=2) as again:
                assert again.receive_json()["token"] == body["token"]
                assert stamped._seen[body["id"]].mono == 25.0
                mono2[0] = 40.0
                wall2[0] = wall0 + 99_999
    stamped.flush()
    stored = json.loads(fresh.read_text(encoding="utf-8"))["records"][body["id"]]["at"]
    assert stamped._seen[body["id"]].mono == 40.0
    # The write uses the wall clock at that moment, minus a zero monotonic age.
    assert stored == pytest.approx(wall0 + 99_999)


def test_an_in_date_head_is_put_back_at_the_head():
    mono = [200.0]
    hub = LiveHub(
        token_ttl=100,
        clock=lambda: 1_800_000_000.0,
        mono=lambda: mono[0],
        init_timeout=5,
        idle_timeout=5,
    )
    hub._issued["fresh"] = [200.0]
    hub._issued["stale"] = [0.0]
    hub._expire_issued_locked(200.0)
    assert list(hub._issued) == ["fresh", "stale"]
    hub._seen["FRSH"] = _Seen(200.0, None, "ab" * 32, "", "w")
    hub._seen["STAL"] = _Seen(0.0, None, "cd" * 32, "", "w")
    hub._expire_locked()
    assert list(hub._seen) == ["FRSH", "STAL"]
    assert list(hub._issued) == ["fresh", "stale"]


def test_eviction_drops_the_oldest_idle_record_by_mono():
    wall = [1_800_000_000.0]
    mono = [1_000.0]
    hub = LiveHub(
        max_seen=2,
        max_seen_per_ip=10,
        token_ttl=10_000,
        clock=lambda: wall[0],
        mono=lambda: mono[0],
        init_timeout=5,
        idle_timeout=5,
    )
    headers = {"x-real-ip": "203.0.113.80"}
    with TestClient(create_app(live=hub)) as client:
        with client.websocket_connect("/api/live/publish", headers=headers) as socket:
            socket.send_json({"type": "mime", "mime": DEFAULT_MIME})
            older = socket.receive_json()
        mono[0] = 2_000.0
        wall[0] = 1_800_000_000.0 - 5_000
        with client.websocket_connect("/api/live/publish", headers=headers) as socket:
            socket.send_json({"type": "mime", "mime": DEFAULT_MIME})
            newer = socket.receive_json()
        hub._seen[older["id"]].mono = 100.0
        hub._seen[newer["id"]].mono = 5_000.0
        mono[0] = 5_010.0
        with client.websocket_connect("/api/live/publish", headers=headers) as socket:
            socket.send_json({"type": "mime", "mime": DEFAULT_MIME})
            socket.receive_json()
    assert older["id"] not in hub._seen
    assert newer["id"] in hub._seen


def test_a_reclaim_moves_the_record_past_an_older_idle_one():
    wall = [1_800_000_000.0]
    mono = [0.0]
    token = "reclaim-token-value"
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    hub = LiveHub(
        token_ttl=100,
        clock=lambda: wall[0],
        mono=lambda: mono[0],
        init_timeout=5,
        idle_timeout=5,
    )
    hub._seen["HEAD"] = _Seen(0.0, 1, token_hash, token, "who")
    hub._seen["BACK"] = _Seen(0.0, None, "cd" * 32, "", "who")
    mono[0] = 10.0
    with TestClient(create_app(live=hub)) as client:
        with _ws(client, _with_token("HEAD", token), seq=2) as socket:
            assert socket.receive_json()["id"] == "HEAD"
            # _end also moves the id. The order has to be read while this
            # socket is still the stream, or a missing reclaim move_to_end
            # still passes after close.
            assert hub._seen["HEAD"].mono == 10.0
            assert list(hub._seen) == ["BACK", "HEAD"]
            mono[0] = 109.0
            hub._expire_locked()
            assert "BACK" not in hub._seen
            assert "HEAD" in hub._seen


def test_an_expired_idle_id_past_the_batch_is_not_reclaimed():
    wall = [1_800_000_000.0]
    mono = [0.0]
    hub = LiveHub(
        token_ttl=100,
        clock=lambda: wall[0],
        mono=lambda: mono[0],
        init_timeout=5,
        idle_timeout=5,
    )
    ids = _many_ids(257)
    token = "owner-token"
    digest = hashlib.sha256(token.encode()).hexdigest()
    for stream_id in ids[:-1]:
        hub._seen[stream_id] = _Seen(0.0, None, "ab" * 32, "", "~" + stream_id)
    target = ids[-1]
    hub._seen[target] = _Seen(0.0, 1, digest, token, "~" + target)
    mono[0] = 150.0
    with TestClient(create_app(live=hub)) as client:
        with pytest.raises(WebSocketDisconnect) as denied:
            with _ws(client, _with_token(target, token), seq=2) as socket:
                socket.receive_json()
        assert denied.value.code == CODE_TAKEN
    assert target not in hub._seen


def test_a_now_before_init_is_kept_and_the_cluster_still_plays():
    hub = LiveHub(init_timeout=2, idle_timeout=5)
    cluster = cluster_known(0, b"wave")
    head, _data = document([cluster])
    video = "abcdefghijk"
    with TestClient(create_app(live=hub)) as client:
        with _ws(client, "/api/live/publish?id=PREN") as publisher:
            publisher.receive_json()
            publisher.send_json({"type": "now", "decks": [{"video": video, "gain": 0.5}]})
            assert hub.get("PREN").decks == ((video, 0.5),)
            publisher.send_bytes(head + cluster)
            with _Audio(client, "/stream/PREN") as listener:
                assert listener.read() == head
                assert listener.read() == cluster
            assert hub.get("PREN") is not None


def test_a_non_finite_or_non_numeric_at_is_skipped(tmp_path):
    path = tmp_path / "live-seen.json"
    digest = "ab" * 32
    path.write_text(
        json.dumps(
            {
                "records": {
                    "NANQ": {"at": float("nan"), "token": digest},
                    "INFQ": {"at": float("inf"), "token": digest},
                    "BOOL": {"at": True, "token": digest},
                    "TEXT": {"at": "12", "token": digest},
                    "GOOD": {"at": 70.0, "token": digest},
                }
            }
        ),
        encoding="utf-8",
    )
    hub = LiveHub(seen_path=path, clock=lambda: 70.0, init_timeout=5, idle_timeout=5)
    assert list(hub._seen) == ["GOOD"]


def test_a_record_aged_exactly_the_ttl_is_expired_and_not_reclaimed():
    mono = [0.0]
    token = "edge-token"
    digest = hashlib.sha256(token.encode()).hexdigest()
    hub = LiveHub(
        token_ttl=100,
        clock=lambda: 1_800_000_000.0,
        mono=lambda: mono[0],
        init_timeout=5,
        idle_timeout=5,
    )
    hub._seen["EDGE"] = _Seen(0.0, 1, digest, token, "who")
    hub._seen["KEEP"] = _Seen(1.0, None, "cd" * 32, "", "who")
    mono[0] = 100.0
    hub._expire_locked()
    assert "EDGE" not in hub._seen
    assert "KEEP" in hub._seen

    hub._seen["EDGE"] = _Seen(0.0, 1, digest, token, "who")
    with TestClient(create_app(live=hub)) as client:
        with pytest.raises(WebSocketDisconnect) as denied:
            with _ws(client, _with_token("EDGE", token), seq=2) as socket:
                socket.receive_json()
        assert denied.value.code == CODE_TAKEN
    assert "EDGE" not in hub._seen


def test_a_live_stream_older_than_the_window_can_be_reclaimed():
    mono = [0.0]
    hub = LiveHub(
        token_ttl=100,
        clock=lambda: 1_800_000_000.0,
        mono=lambda: mono[0],
        init_timeout=5,
        idle_timeout=5,
    )
    with TestClient(create_app(live=hub)) as client:
        with _ws(client, "/api/live/publish?id=LIVE", seq=1) as live:
            token = _id_token(live.receive_json(), "LIVE")
            mono[0] = 500.0
            with _ws(client, _with_token("LIVE", token), seq=2) as again:
                assert again.receive_json()["token"] == token
                assert "LIVE" in hub._seen
                assert hub.get("LIVE") is not None


def test_a_records_list_is_unreadable(tmp_path):
    path = tmp_path / "live-seen.json"
    raw = '{"records":[{"id":"ABCD"}]}'
    path.write_text(raw, encoding="utf-8")
    records = _unreadable_log(path)
    assert path.read_text(encoding="utf-8") == raw
    assert str(path) in "\n".join(records)
    with TestClient(create_app()) as client:
        assert client.get("/api/health").json()["live"] == "unreadable"


def test_a_device_node_is_not_read(monkeypatch):
    def boom(*_args, **_kwargs):
        raise AssertionError("device was read")

    monkeypatch.setattr("djtube.live.os.read", boom)
    records = _unreadable_log(Path("/dev/null"))
    assert "/dev/null" in "\n".join(records)


def test_a_stat_permission_error_logs_the_path(tmp_path, monkeypatch):
    path = tmp_path / "live-seen.json"
    real_open = os.open

    def opened(file, flags, *args, **kwargs):
        if Path(file) == path:
            raise PermissionError(13, "Permission denied")
        return real_open(file, flags, *args, **kwargs)

    monkeypatch.setattr("djtube.live.os.open", opened)
    records = _unreadable_log(path)
    assert str(path) in "\n".join(records)
