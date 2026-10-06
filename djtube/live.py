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
Cluster, is closed with 4408 and its slot is released. The first text has
its own ``CLAIM_TIMEOUT`` of 3 seconds, also from accept. The init clock is
not restarted when the id is sent. The log line is ``timeout <id> init`` or
``timeout <id> idle``. A missing first text frame is the same close code,
logged as ``timeout claim``. MIME text and bytes that never start a Cluster
do not extend the init clock. An unfinished Cluster does not refresh the
idle clock. Byte rate and Cluster size are capped the same way.

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
import hashlib
import hmac
import ipaddress
import json
import logging
import math
import os
import secrets
import string
import stat
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from pathlib import Path

from fastapi import FastAPI, Request, Response, WebSocket
from starlette.websockets import WebSocketDisconnect, WebSocketState

from djtube.ids import is_video_id
from djtube.paths import PACKAGE_DIR
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
# The first text has to arrive soon. The init segment still has INIT_TIMEOUT,
# measured from accept, and this wait is included in that.
CLAIM_TIMEOUT = 3.0
# Sockets that have not sent the first text yet. These are not live streams.
# Pending sockets are cheap and drop after the claim wait, so the cap is wide
# enough for several /48s to connect at once.
MAX_PENDING = 64
MAX_PENDING_PER_IP = 2
# A free tunnel hands out a /48. Two sockets on each of eight /56s is 16
# pending sockets from one block, so the /48 is capped on its own.
MAX_PENDING_PER_48 = 4
# An id stays usable for this long after it was last used. The token issued
# with the id stays the same until the record is gone.
TOKEN_TTL = 12 * 60 * 60
# New ids, not reclaims. Past the global cap, drop the hoarder's oldest idle
# record. Past the per-prefix cap, refuse.
MAX_SEEN = 4096
# One publish holds the lock only long enough to drop this many expired keys.
# The rest go on a later publish, still from the oldest end.
_EXPIRE_BATCH = 256
MAX_SEEN_PER_IP = 64
SEEN_FILE_NAME = "live-seen.json"
# Tests point this at a temp file before the app is imported. Production uses
# data/live-seen.json on the volume.
_DEFAULT_SEEN: Path | None = None
# The same limit the browser uses for a safe integer. A larger seq would not
# round-trip through JSON on the page.
MAX_SEQ = 2**53 - 1

# Close codes in the private-use range, plus the registered ones we mean.
CODE_BAD_ID = 4400
CODE_TIMEOUT = 4408
CODE_TAKEN = 4409
# The previous publisher socket for this id was replaced by a newer one that
# presented the same token. The new socket keeps the id.
CODE_REPLACED = 4410
CODE_FULL = 4429
# The id file cannot be written. This is not the capacity cap.
CODE_STORE = 4430
# The id file could not be read at startup. Streaming stays off.
CODE_UNREADABLE = 4431
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


def _parse_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    text = value.strip()
    if text.startswith("[") and "]" in text:
        text = text[1 : text.index("]")]
    try:
        parsed = ipaddress.ip_address(text)
    except ValueError:
        return None
    if isinstance(parsed, ipaddress.IPv6Address):
        mapped = parsed.ipv4_mapped
        if mapped is not None:
            return mapped
    return parsed


def _expire_from_head(table: OrderedDict, decide: Callable[..., str], limit: int = _EXPIRE_BATCH) -> None:
    """Expire an LRU ordered map from the oldest end.

    ``decide`` returns ``stop`` (this key is fresh, so the tail is too),
    ``drop``, or ``bump`` (move a key that must stay, such as a live id, to the
    tail). ``popitem`` and ``move_to_end`` are O(1). At most ``limit`` drops
    and bumps run, so a full table cannot hold the lock.
    """

    bumped: set[object] = set()
    steps = 0
    while table and steps < limit:
        key, value = table.popitem(last=False)
        action = decide(key, value)
        if action == "stop":
            table[key] = value
            table.move_to_end(key, last=False)
            return
        if action == "bump":
            table[key] = value
            if key in bumped:
                return
            bumped.add(key)
            steps += 1
            continue
        steps += 1


def _address_key(value: str) -> str:
    """Bucket a peer for live slots and pending sockets. IPv4 stays one host. IPv6 counts as its /64."""

    parsed = _parse_ip(value)
    if parsed is None:
        return value.strip()
    if isinstance(parsed, ipaddress.IPv4Address):
        return str(parsed)
    network = ipaddress.IPv6Network((parsed, 64), strict=False)
    return network.network_address.compressed


def _issuance_prefix(address_key: str) -> str:
    """Bucket remembered ids. IPv4 stays one host. IPv6 counts as its /56.

    ``address_key`` is already the live-slot key, so an IPv6 value is the /64
    network address. The /56 is still determined by that address.
    """

    parsed = _parse_ip(address_key)
    if isinstance(parsed, ipaddress.IPv4Address):
        return str(parsed)
    if isinstance(parsed, ipaddress.IPv6Address):
        network = ipaddress.IPv6Network((parsed, 56), strict=False)
        return f"{network.network_address.compressed}/56"
    return address_key


def _pending_block(address_key: str) -> str:
    """Coarser pending bucket. IPv6 is /48. IPv4 stays one host.

    ``address_key`` is the live-slot key, so an IPv6 value is already a /64.
    """

    parsed = _parse_ip(address_key)
    if isinstance(parsed, ipaddress.IPv6Address):
        network = ipaddress.IPv6Network((parsed, 48), strict=False)
        return f"{network.network_address.compressed}/48"
    if isinstance(parsed, ipaddress.IPv4Address):
        return str(parsed)
    return address_key


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


def live_seen_path() -> Path:
    """Where remembered ids live. The compose volume keeps this file."""

    if _DEFAULT_SEEN is not None:
        return _DEFAULT_SEEN
    return PACKAGE_DIR.parent / "data" / SEEN_FILE_NAME


def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_all(fd: int, data: bytes) -> None:
    """Finish a short write. One os.write is not the whole buffer."""

    view = memoryview(data)
    while len(view):
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write")
        view = view[written:]


def _write_bytes_atomic(path: Path, data: bytes) -> None:
    """Replace ``path`` through one ``path.tmp``.

    Opening that fixed name with ``O_TRUNC`` assumes a single worker. Two
    processes would truncate each other's temp file.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(path) + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        _write_all(fd, data)
        os.fsync(fd)
    except Exception:
        os.close(fd)
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    os.close(fd)
    os.replace(tmp, path)


def _write_json_atomic(path: Path, payload: dict) -> None:
    data = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    _write_bytes_atomic(path, data)
    _fsync_dir(path.parent)


def _drop_tmp(path: Path) -> None:
    with contextlib.suppress(OSError):
        os.unlink(Path(str(path) + ".tmp"))


class _SeenWriter:
    """Serialize and fsync off the event loop. A newer snapshot replaces one that has not started.

    A generation is successful when some write at or after it landed. Forgetting
    an old success made a late wait look like a failure and roll other ids back.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._cond = threading.Condition()
        self._snapshot: dict | None = None
        self._gen = 0
        self._done = 0
        self._ok_through = 0
        self._running = False

    def submit(self, payload: dict) -> int:
        with self._cond:
            self._gen += 1
            generation = self._gen
            self._snapshot = payload
            if not self._running:
                self._running = True
                threading.Thread(target=self._run, name="djtube-live-seen", daemon=True).start()
            return generation

    def wait(self, generation: int) -> bool:
        with self._cond:
            while self._done < generation:
                self._cond.wait()
            return generation <= self._ok_through

    def flush(self) -> None:
        with self._cond:
            target = self._gen
            while self._running or self._done < target:
                self._cond.wait()

    def _run(self) -> None:
        while True:
            with self._cond:
                if self._snapshot is None:
                    self._running = False
                    self._cond.notify_all()
                    return
                payload = self._snapshot
                generation = self._gen
                self._snapshot = None
            ok = False
            try:
                _write_json_atomic(self.path, payload)
                ok = True
            except OSError:
                log.warning("could not save live id records")
            with self._cond:
                if ok:
                    self._ok_through = generation
                self._done = generation
                self._cond.notify_all()


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


class _Seen:
    """Seq, last use, and the token hash.

    ``mono`` is required. It is the monotonic last-use time, and it is the only
    clock this record keeps. The file stores a wall time derived from it, so
    there is no second field to forget. ``token`` is the raw value while this
    process still has it. It is not written to disk. ``who`` is the issuance
    prefix for an id minted in this process. A record loaded from disk uses
    ``~`` plus its id, so a restart does not count every saved id as one owner.
    The prefix is not stored, and the 64 cap starts over. After a restart the
    owner presents the same token.
    """

    def __init__(self, mono: float, seq: int | None, token_hash: str, token: str, who: str) -> None:
        self.mono = mono
        # The page increases this on every attempt. A delayed older attempt
        # must not replace the attempt that already won.
        self.seq = seq
        self.token_hash = token_hash
        self.token = token
        self.who = who


class _Stream:
    def __init__(
        self, stream_id: str, address: str, max_cluster: int, bytes_per_second: int, burst_seconds: float
    ) -> None:
        self.id = stream_id
        self.address = address
        self.mime = DEFAULT_MIME
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
        self.mime_announced = False
        self.splitter = WebmSplitter(max_cluster)
        self.meter = _Meter(bytes_per_second, burst_seconds)
        self.persist_gen: int | None = None
        # Token hash of an id this socket just minted. A failed write deletes
        # that one record. The eviction that made room stays done.
        self.minted: str | None = None


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
        claim_timeout: float = CLAIM_TIMEOUT,
        token_ttl: float = TOKEN_TTL,
        max_seen: int = MAX_SEEN,
        max_seen_per_ip: int = MAX_SEEN_PER_IP,
        max_pending: int = MAX_PENDING,
        max_pending_per_ip: int = MAX_PENDING_PER_IP,
        max_pending_per_48: int = MAX_PENDING_PER_48,
        seen_path: Path | None = None,
        enabled: bool = True,
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
        mono: Callable[[], float] | None = None,
        availability: str | None = None,
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
        self.claim_timeout = claim_timeout
        self.token_ttl = token_ttl
        self.max_seen = max_seen
        self.max_seen_per_ip = max_seen_per_ip
        self.max_pending = max_pending
        self.max_pending_per_ip = max_pending_per_ip
        self.max_pending_per_48 = max_pending_per_48
        # on: publishing. off: publishing disabled. unreadable: the seen file
        # could not be read, which is also disabled.
        self.availability = availability or ("on" if enabled else "off")
        self._enabled = self.availability == "on"
        self._seen_path = seen_path if self._enabled else None
        self._writer = _SeenWriter(self._seen_path) if self._seen_path is not None else None
        # Issuance times per prefix. Not stored, so the 64 cap resets on restart.
        # Oldest at the head. A re-stamp moves the prefix to the tail.
        self._issued: OrderedDict[str, list[float]] = OrderedDict()
        # Memory expiry uses a clock that does not step backwards. The file's
        # ``at`` is this process's wall clock at startup plus the monotonic
        # elapsed time, so a restart still drops a record after about 12 hours.
        # A test that passes only ``clock`` drives both. Production does not.
        self._clock = clock or time.time
        self._mono = mono if mono is not None else (clock if clock is not None else time.monotonic)
        self._wall_boot = self._clock()
        self._mono_boot = self._mono()
        self._save_failed = False
        self._lock = threading.Lock()
        self._streams: dict[str, _Stream] = {}
        self._seen: OrderedDict[str, _Seen] = OrderedDict()
        # key -> ("pending"|"reclaim", address). Reclaim does not use a pending slot.
        self._waiting: dict[int, tuple[str, str]] = {}
        self._load_seen()

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
                if self._video_blocked(stream, address) and not self._relays_to_hand_over(stream, address):
                    return 429, None, None
                return 200, VIDEO_MIME, None
            return 200, content_type(stream.mime), None

    def flush(self) -> None:
        """Wait until the seen file has caught up. Tests use this before they rewrite it."""

        if self._writer is not None:
            self._writer.flush()

    def health_live(self) -> str:
        """``/api/health`` value. A failed save is ``store`` until a later write works."""

        if self._save_failed:
            return "store"
        return self.availability

    def _wall_of(self, mono: float) -> float:
        """Wall time to store. It moves with the monotonic clock, not with a step back."""

        return self._wall_boot + (mono - self._mono_boot)

    def _mono_from_wall(self, at: float) -> float:
        """Last-use stamp for a loaded row. A future ``at`` cannot extend the 12 hours."""

        clamped = min(at, self._clock())
        converted = self._mono_boot + (clamped - self._wall_boot)
        return min(converted, self._mono())

    async def publish(self, websocket: WebSocket) -> None:
        _configure_log()
        if not self._enabled:
            await websocket.accept()
            await _close(websocket, CODE_UNREADABLE)
            return
        # The id and token arrive in the first text frame, never the URL.
        # nginx logs $request, which is the path and query, not the frame.
        await websocket.accept()
        address = _publisher_address(websocket)
        connected = time.monotonic()
        wait_key = id(websocket)
        try:
            self._reserve_waiting(wait_key, address)
        except LiveClose as exc:
            await _close(websocket, exc.code)
            return
        try:
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
                    address,
                    claim.seq,
                    wait_key,
                )
                if not await self._commit(stream):
                    await _close(websocket, CODE_STORE)
                    return
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
                # The seen-file write finishes before publish returns.
                closing: list[FfmpegRelay] = []
                ended_ok = self._end(stream, generation, closing)
                try:
                    if ended_ok:
                        await self._commit(stream)
                finally:
                    relay = closing[0] if closing else None
                    if relay is not None:
                        task = asyncio.ensure_future(asyncio.to_thread(relay.close))
                        while not task.done():
                            try:
                                await asyncio.shield(task)
                            except asyncio.CancelledError:
                                continue
                    if ended_ok:
                        log.info("end %s", stream.id)
        finally:
            self._release_waiting(wait_key)

    async def _commit(self, stream: _Stream) -> bool:
        """Wait until the scheduled snapshot is on disk. A failed new id is dropped."""

        generation = stream.persist_gen
        stream.persist_gen = None
        if generation is None or self._writer is None:
            stream.minted = None
            return True
        if await asyncio.to_thread(self._writer.wait, generation):
            self._save_failed = False
            stream.minted = None
            return True
        self._save_failed = True
        if stream.minted is None:
            return True
        self._rollback_new(stream)
        return False

    async def _read_claim(self, websocket: WebSocket, connected: float) -> "_Claim":
        remaining = self.claim_timeout - (time.monotonic() - connected)
        if remaining <= 0:
            # No claim yet, so there is no stream id to name.
            log.info("timeout claim")
            raise LiveClose(CODE_TIMEOUT)
        try:
            message = await asyncio.wait_for(websocket.receive(), remaining)
        except TimeoutError:
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
        self, stream_id: str, address: str, *, handover: bool = True
    ) -> tuple[int, "_Listener | None", "_Stream | None", tuple | None] | list[FfmpegRelay]:
        """Reserve a video listener, or return lingering relays to stop first.

        Stopping joins threads, so a list result is finished with ``stop_handover``
        on a worker. Popen happens in ``open_video``, also off the event loop.
        """

        if not is_stream_id(stream_id):
            return 404, None, None, None
        with self._lock:
            return self._prepare_video_locked(stream_id, address, handover=handover)

    def stop_handover(self, relays: list[FfmpegRelay]) -> None:
        """Stop relays whose hub listener set is still empty. Joins threads.

        Run this off the event loop. The emptiness check shares the hub lock
        with the take, so a listener reserved after the relays were chosen is
        not killed. ``relay.listeners`` alone is not enough: ``drop_video``
        clears the hub set before ``detach``.
        """

        bundles: list[tuple[FfmpegRelay, tuple]] = []
        for relay in relays:
            with self._lock:
                stream = next((item for item in self._streams.values() if item.relay is relay), None)
                if stream is None or stream.video_listeners:
                    continue
                bundle = relay.take_abandoned()
            if bundle is not None:
                bundles.append((relay, bundle))
        for relay, bundle in bundles:
            relay.finish_bundle(bundle)

    def video_listeners_may_leave(self, stream_id: str, address: str) -> bool:
        """True when exactly one viewer, with this address, holds some other encoder.

        That is the connection that just closed and whose ``drop_video`` has not
        run yet. A GET can wait briefly and count again. Two viewers who share an
        address are still watching, so recounting will not free the slot. HEAD
        does not wait. The check runs under the hub lock.
        """

        if not address:
            return False
        with self._lock:
            target = self._streams.get(stream_id)
            for item in self._streams.values():
                if item is target or not self._encoder_busy(item):
                    continue
                viewers = item.video_listeners
                if len(viewers) == 1 and next(iter(viewers)).address == address:
                    return True
        return False

    def _prepare_video_locked(
        self, stream_id: str, address: str, *, handover: bool
    ) -> tuple[int, "_Listener | None", "_Stream | None", tuple | None] | list[FfmpegRelay]:
        """Reserve a listener, or return lingering relays to stop first. The caller holds ``_lock``."""

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
        # is checked before the listener is reserved. 503 wins over 429 while
        # this stream is cooling, even when a new encoder would also be over a cap.
        retry = self._video_retry_after(stream)
        if retry is not None:
            return 503, None, None, None
        if handover:
            victims = self._relays_to_hand_over(stream, address)
            if victims:
                return victims
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
        # completes.
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
        self,
        requested: str | None,
        token: str | None,
        address: str,
        seq: int | None = None,
        wait_key: int | None = None,
    ) -> tuple[_Stream, int, str, WebSocket | None, list[_Listener], list[_Listener], FfmpegRelay | None]:
        if requested is not None and requested.strip() == "":
            requested = None
        if token is not None and token.strip() == "":
            token = None
        with self._lock:
            self._expire_locked()
            slot = self._waiting.pop(wait_key, None) if wait_key is not None else None
            # The one extra socket let in after the pending caps are full may
            # only reclaim. A new id would skip the caps that socket avoided.
            reclaim_only = slot is not None and slot[0] == "reclaim"
            replaced: WebSocket | None = None
            ended: list[_Listener] = []
            video_ended: list[_Listener] = []
            old_relay: FfmpegRelay | None = None
            mono = self._mono()
            if requested is not None:
                if not is_stream_id(requested):
                    raise LiveClose(CODE_BAD_ID)
                seen = self._seen.get(requested)
                # The 256 cap can leave an expired idle id in the table. The
                # owner must not reclaim it and push the stamp forward again.
                if seen is not None and requested not in self._streams and mono - seen.mono >= self.token_ttl:
                    del self._seen[requested]
                    self._schedule_locked()
                    seen = None
                if seen is None or not _token_matches(seen, token):
                    raise LiveClose(CODE_TAKEN)
                if not _seq_allows(seen.seq, seq):
                    # The newer attempt is already recorded. Do not touch it.
                    raise LiveClose(CODE_REPLACED)
                current = self._streams.get(requested)
                if current is not None:
                    self._ensure_room_locked(address, current)
                    replaced, ended, video_ended, old_relay = self._displace_locked(current, address)
                    stream = current
                else:
                    self._ensure_room_locked(address, None)
                    stream = self._start_locked(requested, address)
                stream.minted = None
                self._touch_locked(requested, seq, mono, token or "")
                issued = token or ""
            else:
                if reclaim_only:
                    raise LiveClose(CODE_FULL)
                self._ensure_room_locked(address, None)
                self._take_issue_locked(address, mono)
                requested = self._fresh_id_locked()
                issued = secrets.token_urlsafe(32)
                stream = self._start_locked(requested, address)
                stream.minted = _token_hash(issued)
                self._remember_locked(requested, seq, mono, issued, address)
            stream.persist_gen = self._schedule_locked()
            return (stream, stream.generation, issued, replaced, ended, video_ended, old_relay)

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

    def _reserve_waiting(self, key: int, address: str) -> None:
        with self._lock:
            pending = [addr for kind, addr in self._waiting.values() if kind == "pending"]
            prefix = _issuance_prefix(address)
            block = _pending_block(address)
            pending_here = sum(_issuance_prefix(addr) == prefix for addr in pending)
            pending_block = sum(_pending_block(addr) == block for addr in pending)
            if (
                pending_here < self.max_pending_per_ip
                and pending_block < self.max_pending_per_48
                and len(pending) < self.max_pending
            ):
                self._waiting[key] = ("pending", address)
                return
            # The token is not known until the first text. Once the pending caps
            # are full, one extra socket per live stream from this address can
            # still connect so the owner can reclaim. It does not take a slot.
            live_here = sum(stream.address == address for stream in self._streams.values())
            reclaim_here = sum(kind == "reclaim" and addr == address for kind, addr in self._waiting.values())
            if reclaim_here < live_here:
                self._waiting[key] = ("reclaim", address)
                return
            raise LiveClose(CODE_FULL)

    def _release_waiting(self, key: int) -> None:
        with self._lock:
            self._waiting.pop(key, None)

    def _ensure_room_locked(self, address: str, existing: _Stream | None) -> None:
        streams = [stream for stream in self._streams.values() if stream is not existing]
        if len(streams) >= self.max_streams:
            raise LiveClose(CODE_FULL)
        if sum(stream.address == address for stream in streams) >= self.max_per_ip:
            raise LiveClose(CODE_FULL)

    def _take_issue_locked(self, address: str, now: float) -> None:
        """Count one issuance and, if the table is full, evict one idle record.

        The cap is issuances in the last 12 hours, not how many records are still held.
        A failed attempt still counts. Eviction is final.
        On a tie, the prefix whose idle record is newer loses, so an attacker cannot
        flush older users by minting again.
        """

        prefix = _issuance_prefix(address)
        times = [stamp for stamp in self._issued.get(prefix, ()) if now - stamp < self.token_ttl]
        if len(times) >= self.max_seen_per_ip:
            raise LiveClose(CODE_FULL)
        if len(self._seen) >= self.max_seen and not self._evict_hoarder_locked():
            raise LiveClose(CODE_FULL)
        # Move to the tail. Assigning an existing key leaves it at the head,
        # and the walk would then stop on this fresh stamp forever.
        self._issued[prefix] = times + [now]
        self._issued.move_to_end(prefix)

    def _evict_hoarder_locked(self) -> bool:
        """Drop one idle record from the prefix that holds the most."""

        counts: dict[str, int] = {}
        idle: dict[str, list[tuple[float, str]]] = {}
        for stream_id, seen in self._seen.items():
            counts[seen.who] = counts.get(seen.who, 0) + 1
            if stream_id in self._streams:
                continue
            idle.setdefault(seen.who, []).append((seen.mono, stream_id))
        best_who: str | None = None
        best_count = -1
        best_oldest: float | None = None
        for who, count in counts.items():
            group = idle.get(who)
            if not group:
                continue
            oldest = min(group)[0]
            if count > best_count or (count == best_count and (best_oldest is None or oldest > best_oldest)):
                best_who = who
                best_count = count
                best_oldest = oldest
        if best_who is None:
            return False
        _at, victim = min(idle[best_who])
        del self._seen[victim]
        return True

    def _bump_seen_locked(self, stream_id: str) -> None:
        """Move one id to the newest end."""

        self._seen.move_to_end(stream_id)

    def _expire_locked(self) -> None:
        """Drop expired ids from the oldest end. Order is last use."""

        now = self._mono()

        def decide(stream_id: str, seen: _Seen) -> str:
            if now - seen.mono < self.token_ttl:
                return "stop"
            if stream_id in self._streams:
                return "bump"
            return "drop"

        _expire_from_head(self._seen, decide)
        self._expire_issued_locked(now)

    def _expire_issued_locked(self, now: float) -> None:
        """Drop prefixes whose newest stamp is outside the window.

        Order is the newest stamp, so this stops at the first prefix still
        inside the window. The caller's own stamps are filtered in
        ``_take_issue_locked``.
        """

        def decide(_prefix: str, stamps: list[float]) -> str:
            if stamps and now - stamps[-1] < self.token_ttl:
                return "stop"
            return "drop"

        _expire_from_head(self._issued, decide)

    def _touch_locked(self, stream_id: str, seq: int | None, mono: float, token: str) -> None:
        seen = self._seen[stream_id]
        seen.mono = mono
        if seq is not None:
            seen.seq = seq
        if token:
            seen.token = token
        # The issuing prefix stays, so a reclaim from somewhere else cannot move the slot.
        # Pop-and-assign would leave the id at the tail only after a delete. move_to_end
        # is what takes a reclaimed head out of the way of older ids behind it.
        self._bump_seen_locked(stream_id)

    def _remember_locked(self, stream_id: str, seq: int | None, mono: float, token: str, address: str) -> None:
        self._seen[stream_id] = _Seen(mono, seq, _token_hash(token), token, _issuance_prefix(address))

    def _schedule_locked(self) -> int | None:
        if self._writer is None:
            return None
        payload = {
            "records": {
                stream_id: {
                    "at": self._wall_of(seen.mono),
                    "token": seen.token_hash,
                    **({"seq": seen.seq} if seen.seq is not None else {}),
                }
                for stream_id, seen in self._seen.items()
            }
        }
        return self._writer.submit(payload)

    def _rollback_new(self, stream: _Stream) -> None:
        with self._lock:
            self._streams.pop(stream.id, None)
            seen = self._seen.get(stream.id)
            if seen is not None and seen.token_hash == stream.minted:
                del self._seen[stream.id]
            stream.minted = None
            self._schedule_locked()

    def _load_seen(self) -> None:
        path = self._seen_path
        if path is None:
            return
        _drop_tmp(path)
        try:
            records = _read_seen_file(path)
        except OSError:
            log.warning("live id records are unreadable: %s", path)
            raise
        if records is None:
            return
        if not records:
            return
        wall = self._clock()
        rows: list[tuple[float, str, _Seen]] = []
        for stream_id, item in records.items():
            if not isinstance(stream_id, str) or not is_stream_id(stream_id) or not isinstance(item, dict):
                continue
            try:
                at = float(item["at"])
            except (KeyError, TypeError, ValueError):
                continue
            # Age is the wall span, clamped so a future timestamp counts as now.
            if wall - min(at, wall) >= self.token_ttl:
                continue
            token_hash = item.get("token")
            # Anything other than 64 hex digits is not a hash. Skip the row.
            if not isinstance(token_hash, str) or not _is_hex(token_hash, 64):
                continue
            # The prefix is not stored. Each loaded id is its own unit, so a
            # restart cannot evict the oldest saved ids as one owner.
            # The monotonic stamp keeps the remaining life, and it is not the
            # wall value. Using ``at`` here would never expire under monotonic time.
            mono = self._mono_from_wall(at)
            rows.append((mono, stream_id, _Seen(mono, _stored_seq(item.get("seq")), token_hash, "", "~" + stream_id)))
        rows.sort(key=lambda item: (item[0], item[1]))
        for _mono, stream_id, seen in rows:
            self._seen[stream_id] = seen

    def _fresh_id_locked(self) -> str:
        for _ in range(64):
            candidate = "".join(secrets.choice(STREAM_ALPHABET) for _ in range(STREAM_ID_LENGTH))
            if candidate not in self._streams and candidate not in self._seen:
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
            seen = self._seen.get(stream.id)
            if seen is not None:
                seen.mono = self._mono()
                self._bump_seen_locked(stream.id)
            stream.persist_gen = self._schedule_locked() if seen is not None else None
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
        Joining one that is already running still counts against the per-IP
        cap, unless this address is a listener, or it is the starter and
        nobody is listening (linger). A starter who already left does not
        hold the slot while someone else is watching. The caller holds ``_lock``.
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
        if any(listener.address == address for listener in stream.video_listeners):
            return True
        if stream.video_listeners:
            return False
        relay = stream.relay
        return relay is not None and relay.holder() == address

    def _encoders_held_by(self, address: str) -> int:
        """Encoders this address is watching, plus its own only while they have no listeners."""

        count = 0
        for item in self._streams.values():
            if not self._encoder_busy(item):
                continue
            if item.video_listeners:
                if any(listener.address == address for listener in item.video_listeners):
                    count += 1
                continue
            relay = item.relay
            if relay is not None and relay.holder() == address:
                count += 1
        return count

    def _relays_to_hand_over(self, stream: _Stream, address: str) -> list[FfmpegRelay]:
        """Lingering relays this address owns, when stopping them lets this request through.

        The caller holds ``_lock`` and does not stop anything. Empty when this
        request is already allowed, or when freeing those relays would still
        leave it over a cap.
        """

        if not address or not self._video_blocked(stream, address):
            return []
        victims: list[FfmpegRelay] = []
        for item in self._streams.values():
            if item is stream or not self._encoder_busy(item) or item.video_listeners:
                continue
            relay = item.relay
            if relay is not None and relay.holder() == address:
                victims.append(relay)
        if not victims:
            return []
        released = len(victims)
        if self._encoders_held_by(address) - released >= self.max_video_per_ip:
            return []
        if not self._encoder_busy(stream) and self._encoder_count() - released >= self.max_video_encoders:
            return []
        return victims

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
        raise LiveClose(CODE_BAD_ID) from None
    if not isinstance(payload, dict):
        raise LiveClose(CODE_BAD_ID)
    if "id" in payload:
        raw_id = payload.get("id")
        if isinstance(raw_id, str):
            claim.requested = raw_id
        elif raw_id is None:
            claim.requested = None
        else:
            raise LiveClose(CODE_BAD_ID)
    raw_token = payload.get("token")
    if isinstance(raw_token, str):
        claim.token = raw_token
    if "seq" in payload:
        raw_seq = payload.get("seq")
        if isinstance(raw_seq, bool) or not isinstance(raw_seq, int) or raw_seq < 1 or raw_seq > MAX_SEQ:
            raise LiveClose(CODE_BAD_ID)
        claim.seq = raw_seq
    if "mime" in payload and payload.get("type") in {None, "mime"}:
        claim.mime_present = True
        claim.mime = payload.get("mime")
    return claim


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _token_matches(seen: _Seen, presented: str | None) -> bool:
    if not presented:
        return False
    try:
        expected = bytes.fromhex(seen.token_hash)
        digest = hashlib.sha256(presented.encode("utf-8")).digest()
    except (ValueError, UnicodeError):
        return False
    if len(expected) != len(digest):
        return False
    return hmac.compare_digest(expected, digest)


def _is_hex(value: str, length: int) -> bool:
    if len(value) != length:
        return False
    try:
        bytes.fromhex(value)
    except ValueError:
        return False
    return True


def _stored_seq(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 1 or value > MAX_SEQ:
        return None
    return value


def _read_seen_file(path: Path) -> dict | None:
    """Parse records. None means the path is missing.

    Open the path itself. ``O_NOFOLLOW`` refuses a symlink, ``O_NONBLOCK``
    refuses to wait on a FIFO, and ``fstat`` refuses anything that is not a
    regular file, including a device swapped in after the name was chosen.
    Any other failure raises ``OSError``. The path is left in place.
    """

    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise OSError(f"live id records are unreadable: {path}") from exc
    try:
        try:
            info = os.fstat(fd)
        except OSError as exc:
            raise OSError(f"live id records are unreadable: {path}") from exc
        if not stat.S_ISREG(info.st_mode):
            raise OSError(f"live id records are unreadable: {path}")
        chunks: list[bytes] = []
        while True:
            try:
                block = os.read(fd, 1 << 16)
            except OSError as exc:
                raise OSError(f"live id records are unreadable: {path}") from exc
            if not block:
                break
            chunks.append(block)
        try:
            text = b"".join(chunks).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise OSError(f"live id records are unreadable: {path}") from exc
    finally:
        os.close(fd)
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise OSError(f"live id records are unreadable: {path}") from exc
    if not isinstance(raw, dict):
        raise OSError(f"live id records are unreadable: {path}")
    records = raw.get("records")
    if not isinstance(records, dict):
        raise OSError(f"live id records are unreadable: {path}")
    return records


def _seq_allows(recorded: int | None, seq: int | None) -> bool:
    """True when this claim may take an id that already has a matching token.

    A missing seq still replaces a hold that also has no seq. After any seq is
    stored, only a greater seq may replace it, so a delayed older socket cannot
    close the newer one with 4410.
    """

    if recorded is None:
        return True
    return seq is not None and seq > recorded


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
    app.state.live = hub

    @app.websocket("/api/live/publish")
    async def publish(websocket: WebSocket) -> None:
        await hub.publish(websocket)

    @app.api_route("/stream/{stream_id}", methods=["GET", "HEAD"])
    async def stream_audio(stream_id: str, request: Request) -> Response:
        video = request.query_params.get("thumbnail") == "1"
        address = _publisher_address(request)
        if request.method == "HEAD":
            status, mime, retry = hub.stream_status(stream_id, video=video, address=address)
            headers = _status_headers(status, retry, video=video)
            if mime is None:
                return Response(status_code=status, headers=headers)
            return _HeaderOnly(status, mime)
        if video:
            planned = await _prepare_video_soon(hub, stream_id, address)
            status, listener, stream, opener = planned
            if listener is None or stream is None or opener is None:
                retry = None
                if status == 503:
                    _status, _mime, retry = hub.stream_status(stream_id, video=True, address=address)
                return Response(status_code=status, headers=_status_headers(status, retry, video=True))
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
            return Response(status_code=status, headers=_status_headers(status, None))
        return _AudioResponse(hub, listener, stream)


# The previous picture's detach can land a few milliseconds after the next GET.
_HANDOVER_DRAIN = 0.3


async def _prepare_video_soon(hub: LiveHub, stream_id: str, address: str):
    """Reserve a video listener, waiting briefly if this address's other viewer is closing."""

    planned = await _handover(hub, stream_id, address, hub.prepare_video(stream_id, address))
    if _planned_status(planned) == 429 and hub.video_listeners_may_leave(stream_id, address):
        deadline = time.monotonic() + _HANDOVER_DRAIN
        while time.monotonic() < deadline:
            await asyncio.sleep(0.02)
            planned = await _handover(hub, stream_id, address, hub.prepare_video(stream_id, address))
            if _planned_status(planned) != 429:
                break
    return planned


async def _handover(hub: LiveHub, stream_id: str, address: str, planned):
    if isinstance(planned, list):
        await asyncio.to_thread(hub.stop_handover, planned)
        planned = hub.prepare_video(stream_id, address, handover=False)
    if isinstance(planned, list):
        return (429, None, None, None)
    return planned


def _planned_status(planned) -> int | None:
    if isinstance(planned, list):
        return None
    return planned[0]


def _status_headers(status: int, retry: int | None, *, video: bool = False) -> dict[str, str]:
    headers = dict(_STREAM_HEADERS)
    # The video encoder cap can free within a second (someone leaves, or a linger
    # ends). An audio listener cap does not, so it does not advertise Retry-After.
    if status == 429 and retry is None and video:
        retry = 1
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
