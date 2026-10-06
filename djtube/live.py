"""Relay one browser's mix as a live audio response.

The publisher sends MediaRecorder blobs on a WebSocket. Nothing is written to
disk. Each stream keeps the WebM initialization segment and the newest complete
Cluster. ``GET /stream/{ID}`` writes that pair, then only Clusters that complete
after the listener joins, as one endless chunked ``audio/webm`` body. A player
that connects later starts at the current moment. When the publisher ends, the
body ends.

A listener that falls behind drops older queued Clusters. A listener that does
not accept a chunk within the timeout is dropped, and the publisher keeps
going. Closing the HTTP response removes the listener even if the publisher is
quiet.

A publisher that produces no initialization segment within ``INIT_TIMEOUT``
of accept, or that lets ``IDLE_TIMEOUT`` pass after the last complete
Cluster, is closed with 4408 and its slot is released. The claim wait is the
same init clock, not a second one that starts when the id is sent. The log
line is ``timeout <id> init`` or ``timeout <id> idle``. A missing first text
frame is the same close code, logged as ``timeout claim``. MIME text and
bytes that never start a Cluster do not extend the init clock. An unfinished
Cluster does not refresh the idle clock. Byte rate and Cluster size are
capped the same way.

``GET /stream/{ID}?thumbnail=1`` is the same broadcast with a picture. The
browser only says which videos are audible. One ffmpeg process per stream
draws their thumbnails and muxes MPEG-TS while a video listener is connected.
The query without that value is audio only, as above. Reclaiming an id ends
that picture and its ffmpeg with the old listeners. The old socket's cleanup
does not drop the stream the new socket is publishing.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import ipaddress
import json
import logging
import math
import secrets
import string
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response, WebSocket
from starlette.websockets import WebSocketDisconnect, WebSocketState

from djtube.ids import is_video_id
from djtube.thumbs import ThumbCache
from djtube.video import (
    AUDIO_QUEUE_MAX,
    MAX_VIDEO_ENCODERS,
    MAX_VIDEO_PER_IP,
    OUTPUT_STALL,
    RESTART_BACKOFF,
    VIDEO_FPS,
    VIDEO_HEIGHT,
    VIDEO_LINGER,
    VIDEO_MIME,
    VIDEO_WIDTH,
    FfmpegRelay,
)
from djtube.webm import DEFAULT_MAX_BUFFER, WebmError, WebmSplitter, WebmTooBig

log = logging.getLogger(__name__)
_LOG_HANDLER = "djtube-live"

STREAM_ID_LENGTH = 4
STREAM_ALPHABET = string.ascii_uppercase
DEFAULT_MIME = "audio/webm;codecs=opus"
MAX_FRAME = 256 * 1024
MAX_TEXT = 1024
MAX_STREAMS = 8
MAX_PER_IP = 2
MAX_LISTENERS = 200
MAX_LISTENERS_PER_IP = 4
MAX_LISTENERS_TOTAL = 400
MAX_CLUSTER = DEFAULT_MAX_BUFFER
MAX_BYTES_PER_SECOND = 64 * 1024
BURST_SECONDS = 8
LISTENER_QUEUE = 8
# A now-playing JSON is a few dozen bytes. 4/s matches the browser, which
# samples the decks on that cadence and sends only when the picture changed.
# The bucket holds one second of messages. A stutter can deliver several at
# once; the extras are dropped and the newest one is applied when a token
# returns. The publisher is not closed.
NOW_PER_SECOND = 4
VIDEO_LISTENER_QUEUE = 32
# Starts when the ASGI send is awaited. The kernel accepts writes until the
# socket buffer fills, so a congested listener is not cut until then and keeps
# that much delay.
SEND_TIMEOUT = 2.0
INIT_TIMEOUT = 10.0
IDLE_TIMEOUT = 30.0

# Close codes in the private-use range, plus the registered ones we mean.
CODE_BAD_ID = 4400
CODE_TIMEOUT = 4408
CODE_TAKEN = 4409
# The previous publisher socket for this id was replaced by a newer one that
# presented the same secret. The new socket keeps the id.
CODE_REPLACED = 4410
CODE_FULL = 4429
# A stopped id stays reserved so it is not handed to anyone else and a
# stranger cannot publish on the public URL. 26**4 is 456976 ids. Holds must
# stay far below that or _fresh_id runs out. 4096 is under one percent of the
# space. 32 per address covers a DJ reloading, or a small NAT, without letting
# one source pin the pool. The oldest idle hold is dropped when a cap is hit.
# A hold whose stream is still publishing is not dropped. The clock starts
# again when the publisher disconnects, and a live stream is never expired.
RESERVATION_TTL = 12 * 60 * 60
RESERVATION_MAX = 4096
RESERVATION_PER_IP = 32
CODE_BAD_MEDIA = 1003
CODE_RATE = 1008
CODE_TOO_BIG = 1009

# nginx in front of this app is assumed to set proxy_buffering off. This header
# asks the same thing of a location that forgot it. Players must not reuse a body.
_STREAM_HEADERS = {"Cache-Control": "no-store", "X-Accel-Buffering": "no"}


class LiveClose(Exception):
    def __init__(self, code: int) -> None:
        super().__init__(str(code))
        self.code = code


def is_stream_id(value: str) -> bool:
    return len(value) == STREAM_ID_LENGTH and all(char in STREAM_ALPHABET for char in value)


def _address_key(value: str) -> str:
    """Bucket a peer. IPv4 stays one host. IPv6 counts as its /64."""

    text = value.strip()
    if text.startswith("[") and "]" in text:
        text = text[1 : text.index("]")]
    try:
        parsed = ipaddress.ip_address(text)
    except ValueError:
        return text
    if isinstance(parsed, ipaddress.IPv4Address):
        return str(parsed)
    mapped = parsed.ipv4_mapped
    if mapped is not None:
        return str(mapped)
    network = ipaddress.IPv6Network((parsed, 64), strict=False)
    return network.network_address.compressed


def _publisher_address(connection: WebSocket | Request) -> str:
    """The address nginx put in X-Real-IP, or the socket peer when that header is absent.

    X-Forwarded-For is not read. A client can put anything there. 8098 is meant
    to be reachable only from that nginx, which is what makes X-Real-IP true.
    Publishers and listeners use the same value.
    """

    raw = connection.headers.get("x-real-ip")
    if isinstance(raw, str) and raw.strip():
        return _address_key(raw)
    client = connection.client
    if client is not None and client.host:
        return _address_key(client.host)
    return ""


def content_type(mime: str) -> str:
    """Only the MIME values we accept. A publisher cannot choose an arbitrary type."""

    normalized = normalize_mime(mime)
    if normalized is None:
        return "audio/webm"
    return normalized


def normalize_mime(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    compact = "".join(value.lower().split())
    if compact in {"audio/webm", "audio/webm;codecs=opus"}:
        return DEFAULT_MIME
    return None


def _configure_log() -> None:
    if any(getattr(handler, "name", None) == _LOG_HANDLER for handler in log.handlers):
        return
    handler = logging.StreamHandler()
    handler.name = _LOG_HANDLER
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False


class _Listener:
    """Thread-safe queue. The publisher and the listener may sit on different loops in tests."""

    def __init__(self, limit: int, address: str = "") -> None:
        self.limit = limit
        self.address = address
        self.saw_init = False
        self._closed = False
        self._items: deque[tuple[str, bytes]] = deque()
        self._lock = threading.Lock()
        self._loop = asyncio.get_running_loop()
        self._wakeup = asyncio.Event()

    def offer_init(self, payload: bytes) -> None:
        with self._lock:
            if self._closed or self.saw_init:
                return
            self.saw_init = True
            self._items.append(("init", payload))
        self._wake()

    def offer_media(self, payload: bytes) -> None:
        with self._lock:
            if self._closed or not self.saw_init:
                return
            queued = sum(1 for kind, _ in self._items if kind == "media")
            if queued >= self.limit:
                for index, (kind, _) in enumerate(self._items):
                    if kind == "media":
                        del self._items[index]
                        break
            self._items.append(("media", payload))
        self._wake()

    def offer_end(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._items.append(("end", b""))
        self._wake()

    def mark_closed(self) -> None:
        with self._lock:
            self._closed = True
        self._wake()

    def pending_kinds(self) -> list[str]:
        with self._lock:
            return [kind for kind, _ in self._items]

    def pending_media(self) -> list[bytes]:
        with self._lock:
            return [payload for kind, payload in self._items if kind == "media"]

    async def get(self) -> tuple[str, bytes] | None:
        while True:
            with self._lock:
                if self._items:
                    return self._items.popleft()
                if self._closed:
                    return None
                self._wakeup.clear()
            await self._wakeup.wait()

    def _wake(self) -> None:
        try:
            self._loop.call_soon_threadsafe(self._wakeup.set)
        except RuntimeError:
            return


class _Meter:
    """Average `per_second`, with a burst of several seconds. Over that, the publisher is closed."""

    def __init__(self, per_second: int, burst_seconds: float) -> None:
        self.per_second = per_second
        self.capacity = float(per_second * burst_seconds)
        self.tokens = self.capacity
        self.at = time.monotonic()

    def take(self, amount: int) -> bool:
        now = time.monotonic()
        elapsed = now - self.at
        self.at = now
        self.tokens = min(self.capacity, self.tokens + elapsed * self.per_second)
        if amount > self.tokens:
            return False
        self.tokens -= amount
        return True


class _NowMeter:
    """`per_second` now-playing messages, with a burst of the same number."""

    def __init__(self, per_second: int = NOW_PER_SECOND) -> None:
        self.per_second = per_second
        self.capacity = float(per_second)
        self.tokens = self.capacity
        self.at = time.monotonic()

    def take(self) -> bool:
        now = time.monotonic()
        elapsed = now - self.at
        self.at = now
        self.tokens = min(self.capacity, self.tokens + elapsed * self.per_second)
        if self.tokens < 1:
            return False
        self.tokens -= 1
        return True


# A now-playing message that is not applied. The audio publisher stays up.
NOW_IGNORED = object()


def _looks_like_now(text: str) -> bool:
    """True when the text was meant to be a now message, even if it is not JSON."""

    return '"type":"now"' in "".join(text.split())


def parse_now(payload: object) -> tuple[tuple[str, float], ...] | None | object:
    """Decks from a now-playing object.

    None when this is not that message. ``NOW_IGNORED`` when it says it is,
    but the decks are not usable. The publisher is left open either way.
    """

    if not isinstance(payload, dict) or payload.get("type") != "now":
        return None
    decks = payload.get("decks")
    if not isinstance(decks, list) or len(decks) > 2:
        return NOW_IGNORED
    parsed: list[tuple[str, float]] = []
    for item in decks:
        if not isinstance(item, dict):
            return NOW_IGNORED
        video = item.get("video")
        gain = item.get("gain")
        if not isinstance(video, str) or not is_video_id(video):
            return NOW_IGNORED
        # bool is an int. A JSON true must not pass as gain 1.
        if isinstance(gain, bool) or not isinstance(gain, (int, float)):
            return NOW_IGNORED
        try:
            gain_value = float(gain)
        except (OverflowError, ValueError):
            return NOW_IGNORED
        if not math.isfinite(gain_value) or gain_value < 0 or gain_value > 1:
            return NOW_IGNORED
        parsed.append((video, gain_value))
    return tuple(parsed)


class _Hold:
    """Secret that keeps an id out of the free pool after the publisher stops."""

    def __init__(self, token: str, address: str, at: float) -> None:
        self.token = token
        self.address = address
        self.at = at
        # The page increases this on every attempt. A delayed older attempt
        # must not replace the attempt that already won.
        self.seq: int | None = None


class _Stream:
    def __init__(
        self, stream_id: str, address: str, max_cluster: int, bytes_per_second: int, burst_seconds: float
    ) -> None:
        self.id = stream_id
        self.address = address
        self.mime = DEFAULT_MIME
        self.mime_announced = False
        self.init: bytes | None = None
        self.latest: bytes | None = None
        # AAC will not start on the short tail Cluster alone. Keep the recent
        # ones so a listener who arrives at the end of a burst still has audio.
        self.recent: deque[bytes] = deque(maxlen=8)
        self.closed = False
        self.generation = 0
        self.websocket: WebSocket | None = None
        self.listeners: set[_Listener] = set()
        self.video_listeners: set[_Listener] = set()
        self.decks: tuple[tuple[str, float], ...] = ()
        self.now_meter = _NowMeter()
        self.pending_now: tuple[tuple[str, float], ...] | None = None
        self.now_timer: threading.Timer | None = None
        self.relay: FfmpegRelay | None = None
        self.video_hold = False
        self.splitter = WebmSplitter(max_cluster)
        self.meter = _Meter(bytes_per_second, burst_seconds)


class LiveHub:
    def __init__(
        self,
        *,
        send_timeout: float = SEND_TIMEOUT,
        listener_queue: int = LISTENER_QUEUE,
        max_streams: int = MAX_STREAMS,
        max_per_ip: int = MAX_PER_IP,
        max_listeners: int = MAX_LISTENERS,
        max_listeners_per_ip: int = MAX_LISTENERS_PER_IP,
        max_listeners_total: int = MAX_LISTENERS_TOTAL,
        max_frame: int = MAX_FRAME,
        max_cluster: int = MAX_CLUSTER,
        max_bytes_per_second: int = MAX_BYTES_PER_SECOND,
        burst_seconds: float = BURST_SECONDS,
        init_timeout: float = INIT_TIMEOUT,
        idle_timeout: float = IDLE_TIMEOUT,
        reservation_ttl: float = RESERVATION_TTL,
        reservation_max: int = RESERVATION_MAX,
        reservation_per_ip: int = RESERVATION_PER_IP,
        clock: Callable[[], float] | None = None,
        max_video_encoders: int = MAX_VIDEO_ENCODERS,
        max_video_per_ip: int = MAX_VIDEO_PER_IP,
        video_linger: float = VIDEO_LINGER,
        output_stall: float = OUTPUT_STALL,
        restart_backoff: float = RESTART_BACKOFF,
        audio_queue_max: int = AUDIO_QUEUE_MAX,
        video_size: tuple[int, int] = (VIDEO_WIDTH, VIDEO_HEIGHT),
        video_fps: int = VIDEO_FPS,
        video_listener_queue: int = VIDEO_LISTENER_QUEUE,
        ffmpeg: str = "ffmpeg",
        thumbs: ThumbCache | None = None,
        popen: Callable[..., object] | None = None,
    ) -> None:
        self.send_timeout = send_timeout
        self.listener_queue = listener_queue
        self.max_streams = max_streams
        self.max_per_ip = max_per_ip
        self.max_listeners = max_listeners
        self.max_listeners_per_ip = max_listeners_per_ip
        self.max_listeners_total = max_listeners_total
        self.max_frame = max_frame
        self.max_cluster = max_cluster
        self.max_bytes_per_second = max_bytes_per_second
        self.burst_seconds = burst_seconds
        self.init_timeout = init_timeout
        self.idle_timeout = idle_timeout
        self.reservation_ttl = reservation_ttl
        self.reservation_max = reservation_max
        self.reservation_per_ip = reservation_per_ip
        self.max_video_encoders = max_video_encoders
        self.max_video_per_ip = max_video_per_ip
        self.video_linger = video_linger
        self.output_stall = output_stall
        self.restart_backoff = restart_backoff
        self.audio_queue_max = audio_queue_max
        self.video_size = video_size
        self.video_fps = video_fps
        self.video_listener_queue = video_listener_queue
        self.ffmpeg = ffmpeg
        self.thumbs = thumbs if thumbs is not None else ThumbCache()
        self._popen = popen
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._streams: dict[str, _Stream] = {}
        self._reservations: dict[str, _Hold] = {}

    def get(self, stream_id: str) -> _Stream | None:
        with self._lock:
            stream = self._streams.get(stream_id)
            if stream is None or stream.closed:
                return None
            return stream

    def stream_status(
        self, stream_id: str, *, video: bool = False, address: str = ""
    ) -> tuple[int, str | None, int | None]:
        """HEAD. Does not take a listener slot or start ffmpeg. The type is None when nothing is live.

        The third value is Retry-After seconds when the encoder is cooling down.
        """

        if not is_stream_id(stream_id):
            return 404, None, None
        with self._lock:
            stream = self._streams.get(stream_id)
            if stream is None or stream.closed:
                return 404, None, None
            if video:
                retry = self._video_retry_after(stream)
                if retry is not None:
                    return 503, None, retry
                if self._video_blocked(stream, address):
                    return 429, None, None
                return 200, VIDEO_MIME, None
            return 200, content_type(stream.mime), None

    async def publish(self, websocket: WebSocket) -> None:
        _configure_log()
        # The id and token arrive in the first text frame, never the URL.
        # nginx logs $request, which is the path and query, not the frame.
        await websocket.accept()
        connected = time.monotonic()
        try:
            claim = await self._read_claim(websocket, connected)
            normalized = None
            if claim.mime_present:
                normalized = normalize_mime(claim.mime)
                if normalized is None:
                    raise LiveClose(CODE_BAD_MEDIA)
            stream, generation, token, replaced, ended, video_ended, old_relay = self._open(
                claim.requested,
                claim.token,
                _publisher_address(websocket),
                claim.seq,
            )
        except LiveClose as exc:
            await _close(websocket, exc.code)
            return
        except WebSocketDisconnect:
            return
        stream.websocket = websocket
        if normalized is not None:
            with self._lock:
                if stream.generation == generation and not stream.closed:
                    stream.mime = normalized
                    stream.mime_announced = True
        for listener in ended:
            listener.offer_end()
        for listener in video_ended:
            listener.offer_end()
        if replaced is not None:
            # The previous publisher is still inside receive(). Closing it from
            # this task deadlocks the test portal, so the close runs after the
            # next await. 4410 tells that socket it was replaced.
            log.info("end %s", stream.id)
            asyncio.create_task(_close(replaced, CODE_REPLACED))
        if old_relay is not None:
            # kill/wait stays off this loop. The old generation must not run it
            # later, or it could reap the ffmpeg this socket is about to start.
            asyncio.create_task(asyncio.to_thread(old_relay.close))
        log.info("publish %s", stream.id)
        try:
            await websocket.send_json({"type": "id", "id": stream.id, "token": token})
            if claim.leading:
                self._on_bytes(stream, claim.leading, generation)
            await self._consume(websocket, stream, generation, connected)
        except LiveClose as exc:
            await _close(websocket, exc.code)
        except WebSocketDisconnect:
            pass
        finally:
            # close() waits on ffmpeg. Keep that off the event loop, and do not
            # let a cancelled handler skip the reap. A generation mismatch
            # returns without touching the stream the replacement is using.
            closing: list[FfmpegRelay] = []
            if self._end(stream, generation, closing):
                relay = closing[0] if closing else None
                if relay is not None:
                    task = asyncio.ensure_future(asyncio.to_thread(relay.close))
                    while not task.done():
                        try:
                            await asyncio.shield(task)
                        except asyncio.CancelledError:
                            continue
                log.info("end %s", stream.id)

    async def _read_claim(self, websocket: WebSocket, connected: float) -> "_Claim":
        # The init budget starts at accept. Time spent waiting for this frame
        # is not given back when the id is sent.
        remaining = self.init_timeout - (time.monotonic() - connected)
        if remaining <= 0:
            log.info("timeout claim")
            raise LiveClose(CODE_TIMEOUT)
        try:
            message = await asyncio.wait_for(websocket.receive(), remaining)
        except TimeoutError:
            # No claim yet, so there is no stream id to name.
            log.info("timeout claim")
            raise LiveClose(CODE_TIMEOUT) from None
        if message["type"] == "websocket.disconnect":
            raise WebSocketDisconnect()
        text = message.get("text")
        if isinstance(text, str):
            return _parse_claim(text)
        claim = _Claim()
        data = message.get("bytes")
        if isinstance(data, (bytes, bytearray)) and data:
            claim.leading = bytes(data)
        return claim

    async def attach_listener(self, stream_id: str, address: str) -> tuple[int, "_Listener | None", "_Stream | None"]:
        """Join a live response. 404 when nothing is publishing. 429 when a listener cap is full."""

        if not is_stream_id(stream_id):
            return 404, None, None
        with self._lock:
            stream = self._streams.get(stream_id)
            if stream is None or stream.closed:
                return 404, None, None
            if self._listeners_of(stream) >= self.max_listeners:
                return 429, None, None
            if self._listeners_at(stream, address) >= self.max_listeners_per_ip:
                return 429, None, None
            if self._listeners_total() >= self.max_listeners_total:
                return 429, None, None
            listener = _Listener(self.listener_queue, address)
            stream.listeners.add(listener)
            init = stream.init
            latest = stream.latest
            if init is not None:
                listener.offer_init(init)
            if latest is not None:
                listener.offer_media(latest)
        return 200, listener, stream

    def drop_listener(self, stream: _Stream, listener: _Listener) -> None:
        listener.mark_closed()
        with self._lock:
            stream.listeners.discard(listener)

    def prepare_video(
        self, stream_id: str, address: str
    ) -> tuple[int, "_Listener | None", "_Stream | None", tuple | None]:
        """Reserve a video listener. Popen happens in ``open_video``, off the event loop."""

        if not is_stream_id(stream_id):
            return 404, None, None, None
        with self._lock:
            stream = self._streams.get(stream_id)
            if stream is None or stream.closed:
                return 404, None, None, None
            if self._listeners_of(stream) >= self.max_listeners:
                return 429, None, None, None
            if self._listeners_at(stream, address) >= self.max_listeners_per_ip:
                return 429, None, None, None
            if self._listeners_total() >= self.max_listeners_total:
                return 429, None, None, None
            # A crashed encoder still answers 200 with an empty body unless this
            # is checked before the listener is reserved. 429 wins only when a
            # new encoder would exceed a cap and this stream is not cooling.
            retry = self._video_retry_after(stream)
            if retry is not None:
                return 503, None, None, None
            if self._video_blocked(stream, address):
                return 429, None, None, None
            needs_slot = not self._encoder_busy(stream)
            listener = _Listener(self.video_listener_queue, address)
            stream.video_listeners.add(listener)
            if needs_slot:
                stream.video_hold = True
            if stream.relay is None:
                stream.relay = self._make_relay(stream)
            relay = stream.relay
            init = stream.init
            recent = tuple(stream.recent)
            generation = stream.generation
        return 200, listener, stream, (relay, needs_slot, init, recent, generation)

    def open_video(
        self,
        stream: _Stream,
        listener: _Listener,
        relay: FfmpegRelay,
        needs_slot: bool,
        init,
        recent,
        generation: int,
    ) -> None:
        with self._lock:
            # A reclaim between prepare and Popen removed this listener. Do not
            # attach it to a relay, and do not clear a newer encoder hold.
            if stream.generation != generation or listener not in stream.video_listeners or stream.closed:
                listener.mark_closed()
                return
        started = False
        try:
            if needs_slot:
                relay.claim(listener.address)
                relay.prime(init, recent)
                started = True
            relay.attach(listener)
            if needs_slot and not relay.occupies():
                relay.release_holder()
        except OSError:
            log.exception("video %s start", stream.id)
            listener.mark_closed()
            with self._lock:
                stream.video_listeners.discard(listener)
                if needs_slot and stream.generation == generation:
                    stream.video_hold = False
            if needs_slot and not relay.occupies():
                relay.release_holder()
            raise
        with self._lock:
            current = stream.generation == generation and listener in stream.video_listeners and not stream.closed
            if stream.generation == generation and needs_slot and (current or listener not in stream.video_listeners):
                stream.video_hold = False
        if current:
            return
        # attach can restart a process that reclaim already stopped. Close this
        # relay again. It is not the one the new generation will publish.
        listener.mark_closed()
        if started or relay.occupies():
            relay.close()

    def drop_video(self, stream: _Stream, listener: _Listener, relay: FfmpegRelay | None = None) -> None:
        """Detach from the relay this listener opened.

        The stream may already be on a newer ffmpeg. Detaching from
        ``stream.relay`` would then stop that new process when this old
        response finishes.
        """

        with self._lock:
            stream.video_listeners.discard(listener)
            if relay is None:
                relay = stream.relay
        if relay is not None:
            relay.detach(listener)
        else:
            listener.mark_closed()

    def _timeout(self, stream: _Stream, kind: str) -> None:
        """Close 4408. ``kind`` is ``init`` or ``idle`` and is the log word."""

        log.info("timeout %s %s", stream.id, kind)
        raise LiveClose(CODE_TIMEOUT)

    async def _consume(self, websocket: WebSocket, stream: _Stream, generation: int, connected: float) -> None:
        # ``init`` is what remains of INIT_TIMEOUT since accept. MIME and
        # bytes that never start a Cluster do not move it. ``idle`` starts
        # only once ``stream.init`` exists and moves only when a Cluster
        # completes. Both close 4408; the log says which clock fired.
        last_cluster = time.monotonic()
        while True:
            if stream.generation != generation:
                return
            now = time.monotonic()
            if stream.init is None:
                kind = "init"
                remaining = self.init_timeout - (now - connected)
            else:
                kind = "idle"
                remaining = self.idle_timeout - (now - last_cluster)
            if remaining <= 0:
                self._timeout(stream, kind)
            try:
                message = await asyncio.wait_for(websocket.receive(), remaining)
            except TimeoutError:
                self._timeout(stream, kind)
            if stream.generation != generation:
                return
            if message["type"] == "websocket.disconnect":
                return
            text = message.get("text")
            data = message.get("bytes")
            if text is not None:
                self._on_text(stream, text, generation)
            elif data and self._on_bytes(stream, data, generation):
                last_cluster = time.monotonic()

    def _on_text(self, stream: _Stream, text: str, generation: int) -> None:
        if len(text.encode("utf-8")) > MAX_TEXT:
            raise LiveClose(CODE_TOO_BIG)
        with self._lock:
            if stream.closed or stream.generation != generation:
                return
            started = stream.init is not None
            announced = stream.mime_announced
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            # A broken now must not close the socket. That would cut the audio
            # too. Anything else after init is still rejected below.
            if _looks_like_now(text):
                return
            payload = None
        # now is optional picture state. A bad one is dropped. It never closes
        # the audio, before or after the initialization segment.
        if isinstance(payload, dict) and payload.get("type") == "now":
            parsed = parse_now(payload)
            if parsed is None or parsed is NOW_IGNORED:
                return
            self._accept_now(stream, parsed, generation)
            return
        if started:
            raise LiveClose(CODE_BAD_MEDIA)
        # A second MIME, including a bad one, does not replace the first and
        # does not close the socket. The claim may already have announced it.
        if announced:
            return
        if not isinstance(payload, dict):
            return
        if "mime" not in payload:
            return
        if payload.get("type") not in {None, "mime"}:
            return
        mime = normalize_mime(payload.get("mime"))
        if mime is None:
            raise LiveClose(CODE_BAD_MEDIA)
        with self._lock:
            if stream.closed or stream.generation != generation:
                return
            if stream.init is not None:
                raise LiveClose(CODE_BAD_MEDIA)
            if stream.mime_announced:
                return
            stream.mime = mime
            stream.mime_announced = True

    def _on_bytes(self, stream: _Stream, data: bytes, generation: int) -> bool:
        with self._lock:
            if stream.closed or stream.generation != generation:
                return False
            splitter = stream.splitter
            meter = stream.meter
        if len(data) > self.max_frame:
            raise LiveClose(CODE_TOO_BIG)
        if not meter.take(len(data)):
            raise LiveClose(CODE_RATE)
        try:
            clusters = splitter.feed(data)
        except WebmTooBig as exc:
            raise LiveClose(CODE_TOO_BIG) from exc
        except WebmError as exc:
            raise LiveClose(CODE_BAD_MEDIA) from exc
        with self._lock:
            if stream.closed or stream.generation != generation or stream.splitter is not splitter:
                return False
            fresh_init = None
            listeners: list[_Listener] = []
            if stream.init is None and splitter.init is not None:
                stream.init = splitter.init
                fresh_init = stream.init
                listeners = list(stream.listeners)
        relay: FfmpegRelay | None = None
        if stream.generation == generation and fresh_init is not None:
            for listener in listeners:
                listener.offer_init(fresh_init)
            with self._lock:
                if stream.generation == generation:
                    relay = stream.relay
            if relay is not None:
                relay.note_init(fresh_init)
        if stream.generation != generation:
            return False
        for cluster in clusters:
            self._fanout(stream, cluster, generation)
        return bool(clusters)

    def _fanout(self, stream: _Stream, cluster: bytes, generation: int) -> None:
        with self._lock:
            if stream.closed or stream.generation != generation:
                return
            stream.latest = cluster
            stream.recent.append(cluster)
            listeners = list(stream.listeners)
            relay = stream.relay
        for listener in listeners:
            listener.offer_media(cluster)
        if relay is not None:
            relay.feed(cluster)

    def _open(
        self, requested: str | None, token: str | None, address: str, seq: int | None = None
    ) -> tuple[_Stream, int, str, WebSocket | None, list[_Listener], list[_Listener], FfmpegRelay | None]:
        if requested is not None and requested.strip() == "":
            requested = None
        if token is not None and token.strip() == "":
            token = None
        with self._lock:
            self._expire_locked()
            replaced: WebSocket | None = None
            ended: list[_Listener] = []
            video_ended: list[_Listener] = []
            old_relay: FfmpegRelay | None = None
            if requested is not None:
                if not is_stream_id(requested):
                    raise LiveClose(CODE_BAD_ID)
                hold = self._reservations.get(requested)
                current = self._streams.get(requested)
                matched = hold is not None and token is not None and _token_ok(hold.token, token)
                if matched:
                    assert hold is not None
                    if not _seq_allows(hold.seq, seq):
                        # The newer attempt is already recorded. Do not touch it.
                        raise LiveClose(CODE_REPLACED)
                    if current is not None:
                        self._ensure_room_locked(address, current)
                        replaced, ended, video_ended, old_relay = self._displace_locked(current, address)
                        stream = current
                    else:
                        self._ensure_room_locked(address, None)
                        stream = self._start_locked(requested, address)
                    self._touch_locked(hold, address, seq)
                    issued = hold.token
                elif hold is not None or current is not None:
                    raise LiveClose(CODE_TAKEN)
                else:
                    # Restart, or the hold was evicted. The id is free, so keep
                    # the listener URL and mint a new token.
                    self._ensure_room_locked(address, None)
                    stream = self._start_locked(requested, address)
                    issued = self._remember_locked(requested, address, seq)
            else:
                self._ensure_room_locked(address, None)
                stream_id = self._fresh_id_locked()
                stream = self._start_locked(stream_id, address)
                issued = self._remember_locked(stream_id, address, seq)
            return stream, stream.generation, issued, replaced, ended, video_ended, old_relay

    def _start_locked(self, stream_id: str, address: str) -> _Stream:
        stream = _Stream(stream_id, address, self.max_cluster, self.max_bytes_per_second, self.burst_seconds)
        self._streams[stream_id] = stream
        return stream

    def _displace_locked(
        self, stream: _Stream, address: str
    ) -> tuple[WebSocket | None, list[_Listener], list[_Listener], FfmpegRelay | None]:
        """Keep the id. A new MediaRecorder document cannot extend the old one."""

        stream.generation += 1
        replaced = stream.websocket
        stream.websocket = None
        stream.address = address
        ended = list(stream.listeners)
        video_ended = list(stream.video_listeners)
        stream.listeners.clear()
        stream.video_listeners.clear()
        relay = stream.relay
        stream.relay = None
        stream.video_hold = False
        stream.decks = ()
        stream.recent.clear()
        stream.pending_now = None
        stream.now_meter = _NowMeter()
        timer = stream.now_timer
        stream.now_timer = None
        if timer is not None:
            timer.cancel()
        stream.mime = DEFAULT_MIME
        stream.mime_announced = False
        stream.init = None
        stream.latest = None
        stream.closed = False
        stream.splitter = WebmSplitter(self.max_cluster)
        stream.meter = _Meter(self.max_bytes_per_second, self.burst_seconds)
        return replaced, ended, video_ended, relay

    def _ensure_room_locked(self, address: str, existing: _Stream | None) -> None:
        streams = [stream for stream in self._streams.values() if stream is not existing]
        if len(streams) >= self.max_streams:
            raise LiveClose(CODE_FULL)
        if sum(stream.address == address for stream in streams) >= self.max_per_ip:
            raise LiveClose(CODE_FULL)

    def _expire_locked(self) -> None:
        now = self._clock()
        stale = [
            stream_id
            for stream_id, hold in self._reservations.items()
            if stream_id not in self._streams and now - hold.at >= self.reservation_ttl
        ]
        for stream_id in stale:
            del self._reservations[stream_id]

    def _remember_locked(self, stream_id: str, address: str, seq: int | None = None) -> str:
        self._make_room_locked(address)
        token = secrets.token_urlsafe(32)
        hold = _Hold(token, address, self._clock())
        hold.seq = seq
        self._reservations[stream_id] = hold
        return token

    def _touch_locked(self, hold: _Hold, address: str, seq: int | None = None) -> None:
        hold.address = address
        hold.at = self._clock()
        if seq is not None:
            hold.seq = seq

    def _make_room_locked(self, address: str) -> None:
        """Drop oldest idle holds. A hold whose stream is still publishing stays."""

        while sum(hold.address == address for hold in self._reservations.values()) >= self.reservation_per_ip:
            if not self._drop_oldest_idle_locked(address):
                break
        while len(self._reservations) >= self.reservation_max:
            if not self._drop_oldest_idle_locked(None):
                break

    def _drop_oldest_idle_locked(self, address: str | None) -> bool:
        oldest_id: str | None = None
        oldest_key: tuple[float, str] | None = None
        for stream_id, hold in self._reservations.items():
            if stream_id in self._streams:
                continue
            if address is not None and hold.address != address:
                continue
            key = (hold.at, stream_id)
            if oldest_key is None or key < oldest_key:
                oldest_key = key
                oldest_id = stream_id
        if oldest_id is None:
            return False
        del self._reservations[oldest_id]
        return True

    def _fresh_id_locked(self) -> str:
        for _ in range(64):
            candidate = "".join(secrets.choice(STREAM_ALPHABET) for _ in range(STREAM_ID_LENGTH))
            if candidate not in self._streams and candidate not in self._reservations:
                return candidate
        raise LiveClose(CODE_FULL)

    def _end(self, stream: _Stream, generation: int, closing: list[FfmpegRelay] | None = None) -> bool:
        with self._lock:
            # The replacement already owns this object. Do not pop it or stop
            # the ffmpeg it started.
            if stream.generation != generation or stream.closed:
                return False
            stream.closed = True
            stream.websocket = None
            if self._streams.get(stream.id) is stream:
                self._streams.pop(stream.id, None)
            hold = self._reservations.get(stream.id)
            if hold is not None:
                hold.at = self._clock()
            listeners = list(stream.listeners)
            video_listeners = list(stream.video_listeners)
            stream.listeners.clear()
            stream.video_listeners.clear()
            relay = stream.relay
            stream.relay = None
            stream.video_hold = False
            stream.pending_now = None
            now_timer = stream.now_timer
            stream.now_timer = None
            init = stream.init
        if now_timer is not None:
            now_timer.cancel()
        tail: list[bytes] = []
        try:
            tail = stream.splitter.finish()
        except WebmError:
            tail = []
        if init is None:
            init = stream.splitter.init
        for listener in listeners:
            if init is not None:
                listener.offer_init(init)
            for cluster in tail:
                listener.offer_media(cluster)
            listener.offer_end()
        if relay is not None:
            if init is not None:
                relay.note_init(init)
            for cluster in tail:
                relay.feed(cluster)
            if closing is not None:
                closing.append(relay)
            else:
                relay.close()
        for listener in video_listeners:
            listener.offer_end()
        stream.init = None
        stream.latest = None
        stream.recent.clear()
        stream.splitter.clear()
        return True

    def _listeners_of(self, stream: _Stream) -> int:
        return len(stream.listeners) + len(stream.video_listeners)

    def _listeners_at(self, stream: _Stream, address: str) -> int:
        audio = sum(item.address == address for item in stream.listeners)
        video = sum(item.address == address for item in stream.video_listeners)
        return audio + video

    def _listeners_total(self) -> int:
        return sum(self._listeners_of(item) for item in self._streams.values())

    def _accept_now(self, stream: _Stream, decks: tuple[tuple[str, float], ...], generation: int) -> None:
        with self._lock:
            if stream.closed or stream.generation != generation:
                return
            if stream.now_meter.take():
                stream.decks = decks
                stream.pending_now = None
                return
            stream.pending_now = decks
            if stream.now_timer is not None:
                return
            timer = threading.Timer(1.0 / NOW_PER_SECOND, self._apply_pending_now, args=(stream, generation))
            timer.daemon = True
            stream.now_timer = timer
            timer.start()

    def _apply_pending_now(self, stream: _Stream, generation: int) -> None:
        with self._lock:
            if stream.generation != generation or stream.closed:
                return
            stream.now_timer = None
            pending = stream.pending_now
            if pending is None:
                return
            if not stream.now_meter.take():
                timer = threading.Timer(1.0 / NOW_PER_SECOND, self._apply_pending_now, args=(stream, generation))
                timer.daemon = True
                stream.now_timer = timer
                timer.start()
                return
            stream.decks = pending
            stream.pending_now = None

    def _video_retry_after(self, stream: _Stream) -> int | None:
        """Whole seconds of backoff still left. The caller holds ``_lock``."""

        relay = stream.relay
        if relay is None:
            return None
        remaining = relay.retry_after()
        if remaining is None:
            return None
        return max(1, math.ceil(remaining))

    def _video_blocked(self, stream: _Stream, address: str) -> bool:
        """True when this request must not take or join an encoder.

        A new encoder counts against the global cap and the per-IP cap.
        Joining one that is already running, including during linger, still
        counts against the per-IP cap, unless this address already holds it.
        The caller holds ``_lock``.
        """

        if self._address_holds(stream, address):
            return False
        if not self._encoder_busy(stream) and self._encoder_count() >= self.max_video_encoders:
            return True
        if not address:
            return False
        return self._encoders_held_by(address) >= self.max_video_per_ip

    def _address_holds(self, stream: _Stream, address: str) -> bool:
        if not address or not self._encoder_busy(stream):
            return False
        relay = stream.relay
        if relay is not None and relay.holder() == address:
            return True
        return any(listener.address == address for listener in stream.video_listeners)

    def _encoders_held_by(self, address: str) -> int:
        count = 0
        for item in self._streams.values():
            if not self._encoder_busy(item):
                continue
            relay = item.relay
            if relay is not None and relay.holder() == address:
                count += 1
                continue
            if any(listener.address == address for listener in item.video_listeners):
                count += 1
        return count

    def _encoder_busy(self, stream: _Stream) -> bool:
        if stream.video_hold:
            return True
        return stream.relay is not None and stream.relay.occupies()

    def _encoder_count(self) -> int:
        return sum(1 for item in self._streams.values() if self._encoder_busy(item))

    def _make_relay(self, stream: _Stream) -> FfmpegRelay:
        def decks() -> tuple[tuple[str, float], ...]:
            return stream.decks

        width, height = self.video_size
        return FfmpegRelay(
            stream.id,
            decks,
            self.thumbs,
            width=width,
            height=height,
            fps=self.video_fps,
            linger=self.video_linger,
            ffmpeg=self.ffmpeg,
            output_stall=self.output_stall,
            restart_backoff=self.restart_backoff,
            audio_queue_max=self.audio_queue_max,
            **({"popen": self._popen} if self._popen is not None else {}),
        )


class _Claim:
    def __init__(self) -> None:
        self.requested: str | None = None
        self.token: str | None = None
        self.seq: int | None = None
        self.mime: object = None
        self.mime_present = False
        self.leading: bytes | None = None


def _parse_claim(text: str) -> _Claim:
    claim = _Claim()
    if len(text.encode("utf-8")) > MAX_TEXT:
        raise LiveClose(CODE_TOO_BIG)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return claim
    if not isinstance(payload, dict):
        return claim
    raw_id = payload.get("id")
    if isinstance(raw_id, str):
        claim.requested = raw_id
    raw_token = payload.get("token")
    if isinstance(raw_token, str):
        claim.token = raw_token
    raw_seq = payload.get("seq")
    if isinstance(raw_seq, int) and not isinstance(raw_seq, bool) and raw_seq >= 1:
        claim.seq = raw_seq
    if "mime" in payload and payload.get("type") in {None, "mime"}:
        claim.mime_present = True
        claim.mime = payload.get("mime")
    return claim


def _seq_allows(recorded: int | None, seq: int | None) -> bool:
    """True when this claim may take an id that already has a matching token.

    A missing seq still replaces a hold that also has no seq. After any seq is
    stored, only a greater seq may replace it, so a delayed older socket cannot
    close the newer one with 4410.
    """

    if recorded is None:
        return True
    return seq is not None and seq > recorded


def _token_ok(expected: str, presented: str) -> bool:
    try:
        left = expected.encode("ascii")
        right = presented.encode("ascii")
    except UnicodeEncodeError:
        return False
    return hmac.compare_digest(left, right)


class _AudioResponse(Response):
    """Chunked audio/webm. No Content-Length, so the server frames it as chunked."""

    media_type = "audio/webm"

    def __init__(
        self,
        hub: LiveHub,
        listener: _Listener,
        stream: _Stream,
        media_type: str | None = None,
        on_close: Callable[[], None] | None = None,
    ) -> None:
        self.status_code = 200
        self.media_type = media_type if media_type is not None else content_type(stream.mime)
        self.background = None
        self.init_headers(_STREAM_HEADERS)
        self.hub = hub
        self.listener = listener
        self.stream = stream
        self._on_close = on_close

    async def __call__(
        self, scope: dict, receive: Callable[[], Awaitable[dict]], send: Callable[[dict], Awaitable[None]]
    ) -> None:
        try:
            await send({"type": "http.response.start", "status": self.status_code, "headers": self.raw_headers})
            await self._body(receive, send)
        finally:
            if self._on_close is not None:
                self._on_close()
            else:
                self.hub.drop_listener(self.stream, self.listener)

    async def _body(self, receive: Callable[[], Awaitable[dict]], send: Callable[[dict], Awaitable[None]]) -> None:
        incoming = asyncio.create_task(_until_http_disconnect(receive))
        try:
            while True:
                item_task = asyncio.create_task(self.listener.get())
                done, _pending = await asyncio.wait({item_task, incoming}, return_when=asyncio.FIRST_COMPLETED)
                if incoming in done:
                    item_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await item_task
                    return
                item = item_task.result()
                if item is None or item[0] == "end":
                    await send({"type": "http.response.body", "body": b"", "more_body": False})
                    return
                try:
                    await asyncio.wait_for(
                        send({"type": "http.response.body", "body": item[1], "more_body": True}),
                        self.hub.send_timeout,
                    )
                except TimeoutError:
                    return
        finally:
            if not incoming.done():
                incoming.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await incoming


class _HeaderOnly(Response):
    """Same headers as the live GET, with no body and no Content-Length."""

    def __init__(self, status_code: int, mime: str) -> None:
        self.status_code = status_code
        self.media_type = mime
        self.background = None
        self.init_headers(_STREAM_HEADERS)

    async def __call__(
        self, scope: dict, receive: Callable[[], Awaitable[dict]], send: Callable[[dict], Awaitable[None]]
    ) -> None:
        await send({"type": "http.response.start", "status": self.status_code, "headers": self.raw_headers})
        await send({"type": "http.response.body", "body": b"", "more_body": False})


def mount_live(app: FastAPI, hub: LiveHub) -> None:
    @app.websocket("/api/live/publish")
    async def publish(websocket: WebSocket) -> None:
        await hub.publish(websocket)

    @app.api_route("/stream/{stream_id}", methods=["GET", "HEAD"])
    async def stream_audio(stream_id: str, request: Request) -> Response:
        video = request.query_params.get("thumbnail") == "1"
        address = _publisher_address(request)
        if request.method == "HEAD":
            status, mime, retry = hub.stream_status(stream_id, video=video, address=address)
            headers = _status_headers(retry)
            if mime is None:
                return Response(status_code=status, headers=headers)
            return _HeaderOnly(status, mime)
        if video:
            status, listener, stream, opener = hub.prepare_video(stream_id, address)
            if listener is None or stream is None or opener is None:
                retry = None
                if status == 503:
                    _status, _mime, retry = hub.stream_status(stream_id, video=True, address=address)
                return Response(status_code=status, headers=_status_headers(retry))
            owned = opener[0]
            try:
                await asyncio.to_thread(hub.open_video, stream, listener, *opener)
            except OSError:
                return Response(status_code=500, headers=_STREAM_HEADERS)
            return _AudioResponse(
                hub,
                listener,
                stream,
                media_type=VIDEO_MIME,
                on_close=lambda: hub.drop_video(stream, listener, owned),
            )
        status, listener, stream = await hub.attach_listener(stream_id, address)
        if listener is None or stream is None:
            return Response(status_code=status, headers=_STREAM_HEADERS)
        return _AudioResponse(hub, listener, stream)


def _status_headers(retry: int | None) -> dict[str, str]:
    headers = dict(_STREAM_HEADERS)
    if retry is not None:
        headers["Retry-After"] = str(retry)
    return headers


async def _until_http_disconnect(receive: Callable[[], Awaitable[dict]]) -> None:
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return


async def _close(websocket: WebSocket, code: int) -> None:
    if websocket.application_state != WebSocketState.CONNECTED:
        return
    with contextlib.suppress(RuntimeError, WebSocketDisconnect):
        await websocket.close(code=code)
