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

A publisher that does not produce an initialization segment, or that stops
sending, is closed and its slot is released. Byte rate and Cluster size are
capped the same way.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import logging
import secrets
import string
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response, WebSocket
from starlette.websockets import WebSocketDisconnect, WebSocketState

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
CODE_FULL = 4429
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
        self.closed = False
        self.listeners: set[_Listener] = set()
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
        self._lock = threading.Lock()
        self._streams: dict[str, _Stream] = {}

    def get(self, stream_id: str) -> _Stream | None:
        with self._lock:
            stream = self._streams.get(stream_id)
            if stream is None or stream.closed:
                return None
            return stream

    def stream_status(self, stream_id: str) -> tuple[int, str | None]:
        """HEAD. Does not take a listener slot. The type is None when nothing is live."""

        if not is_stream_id(stream_id):
            return 404, None
        with self._lock:
            stream = self._streams.get(stream_id)
            if stream is None or stream.closed:
                return 404, None
            return 200, content_type(stream.mime)

    async def publish(self, websocket: WebSocket) -> None:
        _configure_log()
        await websocket.accept()
        try:
            stream = self._open(websocket.query_params.get("id"), _publisher_address(websocket))
        except LiveClose as exc:
            await _close(websocket, exc.code)
            return
        log.info("publish %s", stream.id)
        try:
            await websocket.send_json({"type": "id", "id": stream.id})
            await self._consume(websocket, stream)
        except LiveClose as exc:
            await _close(websocket, exc.code)
        except WebSocketDisconnect:
            pass
        finally:
            self._end(stream)
            log.info("end %s", stream.id)

    async def attach_listener(self, stream_id: str, address: str) -> tuple[int, "_Listener | None", "_Stream | None"]:
        """Join a live response. 404 when nothing is publishing. 429 when a listener cap is full."""

        if not is_stream_id(stream_id):
            return 404, None, None
        with self._lock:
            stream = self._streams.get(stream_id)
            if stream is None or stream.closed:
                return 404, None, None
            if len(stream.listeners) >= self.max_listeners:
                return 429, None, None
            if sum(item.address == address for item in stream.listeners) >= self.max_listeners_per_ip:
                return 429, None, None
            if sum(len(item.listeners) for item in self._streams.values()) >= self.max_listeners_total:
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

    async def _consume(self, websocket: WebSocket, stream: _Stream) -> None:
        opened = time.monotonic()
        last_cluster = opened
        while True:
            now = time.monotonic()
            if stream.init is None:
                remaining = self.init_timeout - (now - opened)
            else:
                remaining = self.idle_timeout - (now - last_cluster)
            if remaining <= 0:
                raise LiveClose(CODE_TIMEOUT)
            try:
                message = await asyncio.wait_for(websocket.receive(), remaining)
            except TimeoutError:
                raise LiveClose(CODE_TIMEOUT) from None
            if message["type"] == "websocket.disconnect":
                return
            text = message.get("text")
            data = message.get("bytes")
            if text is not None:
                self._on_text(stream, text)
            elif data and self._on_bytes(stream, data):
                last_cluster = time.monotonic()

    def _on_text(self, stream: _Stream, text: str) -> None:
        with self._lock:
            if stream.closed:
                return
            if stream.init is not None:
                raise LiveClose(CODE_BAD_MEDIA)
            if stream.mime_announced:
                return
        if len(text.encode("utf-8")) > MAX_TEXT:
            raise LiveClose(CODE_TOO_BIG)
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
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
            if stream.closed:
                return
            if stream.init is not None:
                raise LiveClose(CODE_BAD_MEDIA)
            if stream.mime_announced:
                return
            stream.mime = mime
            stream.mime_announced = True

    def _on_bytes(self, stream: _Stream, data: bytes) -> bool:
        if len(data) > self.max_frame:
            raise LiveClose(CODE_TOO_BIG)
        if not stream.meter.take(len(data)):
            raise LiveClose(CODE_RATE)
        try:
            clusters = stream.splitter.feed(data)
        except WebmTooBig as exc:
            raise LiveClose(CODE_TOO_BIG) from exc
        except WebmError as exc:
            raise LiveClose(CODE_BAD_MEDIA) from exc
        with self._lock:
            if stream.closed:
                return bool(clusters)
            fresh_init = None
            listeners: list[_Listener] = []
            if stream.init is None and stream.splitter.init is not None:
                stream.init = stream.splitter.init
                fresh_init = stream.init
                listeners = list(stream.listeners)
        if fresh_init is not None:
            for listener in listeners:
                listener.offer_init(fresh_init)
        for cluster in clusters:
            self._fanout(stream, cluster)
        return bool(clusters)

    def _fanout(self, stream: _Stream, cluster: bytes) -> None:
        with self._lock:
            if stream.closed:
                return
            stream.latest = cluster
            listeners = list(stream.listeners)
        for listener in listeners:
            listener.offer_media(cluster)

    def _open(self, requested: str | None, address: str) -> _Stream:
        if requested is not None and requested.strip() == "":
            requested = None
        with self._lock:
            if requested is not None:
                if not is_stream_id(requested):
                    raise LiveClose(CODE_BAD_ID)
                current = self._streams.get(requested)
                if current is not None and not current.closed:
                    raise LiveClose(CODE_TAKEN)
                stream_id = requested
            else:
                stream_id = self._fresh_id()
            if len(self._streams) >= self.max_streams:
                raise LiveClose(CODE_FULL)
            if sum(stream.address == address for stream in self._streams.values()) >= self.max_per_ip:
                raise LiveClose(CODE_FULL)
            stream = _Stream(stream_id, address, self.max_cluster, self.max_bytes_per_second, self.burst_seconds)
            self._streams[stream_id] = stream
            return stream

    def _fresh_id(self) -> str:
        for _ in range(32):
            candidate = "".join(secrets.choice(STREAM_ALPHABET) for _ in range(STREAM_ID_LENGTH))
            if candidate not in self._streams:
                return candidate
        raise LiveClose(CODE_FULL)

    def _end(self, stream: _Stream) -> None:
        with self._lock:
            if stream.closed:
                return
            stream.closed = True
            if self._streams.get(stream.id) is stream:
                self._streams.pop(stream.id, None)
            listeners = list(stream.listeners)
            stream.listeners.clear()
            init = stream.init
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
        stream.init = None
        stream.latest = None
        stream.splitter.clear()


class _AudioResponse(Response):
    """Chunked audio/webm. No Content-Length, so the server frames it as chunked."""

    media_type = "audio/webm"

    def __init__(self, hub: LiveHub, listener: _Listener, stream: _Stream) -> None:
        self.status_code = 200
        self.media_type = content_type(stream.mime)
        self.background = None
        self.init_headers(_STREAM_HEADERS)
        self.hub = hub
        self.listener = listener
        self.stream = stream

    async def __call__(
        self, scope: dict, receive: Callable[[], Awaitable[dict]], send: Callable[[dict], Awaitable[None]]
    ) -> None:
        try:
            await send({"type": "http.response.start", "status": self.status_code, "headers": self.raw_headers})
            await self._body(receive, send)
        finally:
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
        if request.method == "HEAD":
            status, mime = hub.stream_status(stream_id)
            if mime is None:
                return Response(status_code=status, headers=_STREAM_HEADERS)
            return _HeaderOnly(status, mime)
        status, listener, stream = await hub.attach_listener(stream_id, _publisher_address(request))
        if listener is None or stream is None:
            return Response(status_code=status, headers=_STREAM_HEADERS)
        return _AudioResponse(hub, listener, stream)


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
