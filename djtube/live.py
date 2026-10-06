"""Relay one browser's mix to whoever opens the listener page.

The publisher sends MediaRecorder blobs on a WebSocket. Nothing is written to
disk. Each stream keeps the WebM initialization segment and the newest complete
Cluster. A listener who arrives later is given that pair and then only Clusters
that complete after they join, so playback starts at the current moment.

A listener that falls behind drops older queued Clusters. A listener whose
socket does not accept a send within the timeout is disconnected.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
import string
import threading
from collections import deque
from html import escape

from fastapi import FastAPI, WebSocket
from fastapi.responses import HTMLResponse
from starlette.websockets import WebSocketDisconnect, WebSocketState

from djtube.assets import DOCUMENT_CACHE, asset_version, stamp_document
from djtube.paths import PUBLIC_PREFIX, STREAM_PATH
from djtube.webm import WebmError, WebmSplitter

log = logging.getLogger(__name__)
_LOG_HANDLER = "djtube-live"

STREAM_ID_LENGTH = 4
STREAM_ALPHABET = string.ascii_uppercase
DEFAULT_MIME = "audio/webm;codecs=opus"
MAX_FRAME = 1024 * 1024
MAX_TEXT = 1024
MAX_STREAMS = 64
MAX_LISTENERS = 200
LISTENER_QUEUE = 8
SEND_TIMEOUT = 2.0

# Close codes in the private-use range, plus the registered ones we mean.
CODE_BAD_ID = 4400
CODE_ABSENT = 4404
CODE_TAKEN = 4409
CODE_FULL = 4429
CODE_BAD_MEDIA = 1003
CODE_TOO_BIG = 1009
CODE_SLOW = 1013
CODE_OK = 1000


class LiveClose(Exception):
    def __init__(self, code: int) -> None:
        super().__init__(str(code))
        self.code = code


def is_stream_id(value: str) -> bool:
    return len(value) == STREAM_ID_LENGTH and all(char in STREAM_ALPHABET for char in value)


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

    def __init__(self, limit: int) -> None:
        self.limit = limit
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


class _Stream:
    def __init__(self, stream_id: str) -> None:
        self.id = stream_id
        self.mime = DEFAULT_MIME
        self.mime_locked = False
        self.init: bytes | None = None
        self.latest: bytes | None = None
        self.closed = False
        self.listeners: set[_Listener] = set()
        self.splitter = WebmSplitter()


class LiveHub:
    def __init__(
        self,
        *,
        send_timeout: float = SEND_TIMEOUT,
        listener_queue: int = LISTENER_QUEUE,
        max_streams: int = MAX_STREAMS,
        max_listeners: int = MAX_LISTENERS,
        max_frame: int = MAX_FRAME,
    ) -> None:
        self.send_timeout = send_timeout
        self.listener_queue = listener_queue
        self.max_streams = max_streams
        self.max_listeners = max_listeners
        self.max_frame = max_frame
        self._lock = threading.Lock()
        self._streams: dict[str, _Stream] = {}

    def get(self, stream_id: str) -> _Stream | None:
        with self._lock:
            stream = self._streams.get(stream_id)
            if stream is None or stream.closed:
                return None
            return stream

    def active(self, stream_id: str) -> bool:
        return self.get(stream_id) is not None

    async def publish(self, websocket: WebSocket) -> None:
        _configure_log()
        await websocket.accept()
        try:
            stream = self._open(websocket.query_params.get("id"))
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

    async def listen(self, websocket: WebSocket, stream_id: str) -> None:
        _configure_log()
        await websocket.accept()
        if not is_stream_id(stream_id):
            await _tell(websocket, {"type": "absent"}, CODE_ABSENT)
            return
        listener = _Listener(self.listener_queue)
        with self._lock:
            stream = self._streams.get(stream_id)
            if stream is None or stream.closed:
                outcome = "absent"
                mime = DEFAULT_MIME
                init = None
                latest = None
            elif len(stream.listeners) >= self.max_listeners:
                outcome = "full"
                mime = stream.mime
                init = None
                latest = None
            else:
                outcome = "live"
                stream.listeners.add(listener)
                mime = stream.mime
                init = stream.init
                latest = stream.latest
        if outcome == "absent":
            await _tell(websocket, {"type": "absent"}, CODE_ABSENT)
            return
        if outcome == "full":
            await _tell(websocket, {"type": "full"}, CODE_FULL)
            return
        if init is not None:
            listener.offer_init(init)
        if latest is not None:
            listener.offer_media(latest)
        try:
            await websocket.send_json({"type": "start", "id": stream_id, "mime": mime})
            await self._pump(listener, websocket)
        except WebSocketDisconnect:
            pass
        finally:
            listener.mark_closed()
            with self._lock:
                stream.listeners.discard(listener)

    async def _pump(self, listener: _Listener, websocket: WebSocket) -> None:
        sent_init = False
        while True:
            item = await listener.get()
            if item is None:
                return
            kind, payload = item
            if kind == "end":
                await _tell(websocket, {"type": "end"}, CODE_OK)
                return
            if kind == "init":
                await self._send_bytes(websocket, payload)
                sent_init = True
                continue
            if not sent_init:
                continue
            await self._send_bytes(websocket, payload)

    async def _send_bytes(self, websocket: WebSocket, payload: bytes) -> None:
        try:
            await asyncio.wait_for(websocket.send_bytes(payload), self.send_timeout)
        except asyncio.TimeoutError:
            await _close(websocket, CODE_SLOW)
            raise WebSocketDisconnect(code=CODE_SLOW) from None

    async def _consume(self, websocket: WebSocket, stream: _Stream) -> None:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return
            text = message.get("text")
            data = message.get("bytes")
            if text is not None:
                self._on_text(stream, text)
            elif data:
                self._on_bytes(stream, data)

    def _on_text(self, stream: _Stream, text: str) -> None:
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
            if stream.mime_locked:
                return
            stream.mime = mime

    def _on_bytes(self, stream: _Stream, data: bytes) -> None:
        if len(data) > self.max_frame:
            raise LiveClose(CODE_TOO_BIG)
        try:
            clusters = stream.splitter.feed(data)
        except WebmError as exc:
            raise LiveClose(CODE_BAD_MEDIA) from exc
        with self._lock:
            if stream.closed:
                return
            stream.mime_locked = True
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

    def _fanout(self, stream: _Stream, cluster: bytes) -> None:
        with self._lock:
            if stream.closed:
                return
            stream.latest = cluster
            listeners = list(stream.listeners)
        for listener in listeners:
            listener.offer_media(cluster)

    def _open(self, requested: str | None) -> _Stream:
        if requested is not None and requested.strip() == "":
            requested = None
        with self._lock:
            if requested is not None:
                if not is_stream_id(requested):
                    raise LiveClose(CODE_BAD_ID)
                if requested in self._streams:
                    raise LiveClose(CODE_TAKEN)
                if len(self._streams) >= self.max_streams:
                    raise LiveClose(CODE_FULL)
                stream_id = requested
            else:
                if len(self._streams) >= self.max_streams:
                    raise LiveClose(CODE_FULL)
                stream_id = self._fresh_id()
            stream = _Stream(stream_id)
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


def _stream_view(stream_id: str) -> tuple[str, str, str]:
    if not is_stream_id(stream_id):
        return "invalid", "", "ID の形式が違います"
    return "absent", stream_id, "この配信はありません"


def render_stream(hub: LiveHub, stream_id: str) -> str:
    state, shown, status = _stream_view(stream_id)
    if state == "absent" and hub.active(stream_id):
        state = "live"
        status = "接続しています"
    title = "DJTUBE" if not shown else f"DJTUBE {shown}"
    html = STREAM_PATH.read_text(encoding="utf-8")
    html = html.replace("__PUBLIC_PREFIX__", PUBLIC_PREFIX)
    html = html.replace("__STREAM_TITLE__", escape(title))
    html = html.replace("__STREAM_ID__", escape(shown))
    html = html.replace("__STREAM_STATE__", state)
    html = html.replace("__STREAM_STATUS__", escape(status))
    return stamp_document(html, asset_version())


def mount_live(app: FastAPI, hub: LiveHub) -> None:
    @app.websocket("/api/live/publish")
    async def publish(websocket: WebSocket) -> None:
        await hub.publish(websocket)

    @app.websocket("/api/live/{stream_id}")
    async def listen(websocket: WebSocket, stream_id: str) -> None:
        await hub.listen(websocket, stream_id)

    @app.get("/stream/{stream_id}")
    def stream_page(stream_id: str) -> HTMLResponse:
        return HTMLResponse(render_stream(hub, stream_id), headers={"Cache-Control": DOCUMENT_CACHE})


async def _close(websocket: WebSocket, code: int) -> None:
    if websocket.application_state != WebSocketState.CONNECTED:
        return
    with contextlib.suppress(RuntimeError, WebSocketDisconnect):
        await websocket.close(code=code)


async def _tell(websocket: WebSocket, payload: dict[str, str], code: int) -> None:
    with contextlib.suppress(RuntimeError, WebSocketDisconnect):
        await websocket.send_json(payload)
    await _close(websocket, code)
