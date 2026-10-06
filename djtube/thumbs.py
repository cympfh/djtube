"""YouTube thumbnail cache.

Pictures are loaded only from i.ytimg.com, and only for an 11-character video
id. Nothing in this module takes a URL from the publisher, so a now-playing
message cannot make the server fetch an arbitrary host.
"""

from __future__ import annotations

import io
import os
import queue
import threading
import time
from collections import OrderedDict
from pathlib import Path

import httpx
from PIL import Image, UnidentifiedImageError

from djtube.ids import is_video_id

# maxresdefault is 1280x720 when YouTube has it, and 404 otherwise. hqdefault
# is 480x360 and is there for essentially every video. A 200 response that is
# smaller than this is the placeholder jpeg, not a picture we should show.
MIN_WIDTH = 320
MIN_HEIGHT = 180
MAX_BYTES = 2 * 1024 * 1024
# Header size is known before pixels are decoded. A huge declared frame is a
# decompression bomb, so it is rejected without load().
# YouTube thumbnails are at most 1280x720. A 4096-wide JPEG is not one of
# those, and decoding it peaks around 100MB, so the cap sits near two 720p frames.
MAX_EDGE = 2048
MAX_PIXELS = 1_843_200
MAX_MISSING = 64
MAX_QUEUED = 16
# Eight decoded frames is about 22MB at 720p RGB (1280*720*3). That covers a
# few streams of two decks plus the pictures just replaced, and then evicts.
MAX_ITEMS = 8
MAX_CACHE_BYTES = 24 * 1024 * 1024
# A missing picture should not be requested on every frame. 30 s is long
# enough to ride out a blip and short enough to try again in the same set.
NEGATIVE_TTL = 30.0
FETCH_TIMEOUT = 4.0

_NAMES = ("maxresdefault.jpg", "hqdefault.jpg")


def thumb_urls(video_id: str) -> tuple[str, str]:
    if not is_video_id(video_id):
        raise ValueError("invalid video id")
    return tuple(f"https://i.ytimg.com/vi/{video_id}/{name}" for name in _NAMES)


def _dimensions_ok(width: int, height: int, *, minimum: bool) -> bool:
    if width > MAX_EDGE or height > MAX_EDGE or width * height > MAX_PIXELS:
        return False
    if minimum and (width < MIN_WIDTH or height < MIN_HEIGHT):
        return False
    return width > 0 and height > 0


def _read_limited(chunks, limit: int) -> bytes | None:
    """Join chunks until `limit`. None once the body would pass it."""

    parts: list[bytes] = []
    total = 0
    for chunk in chunks:
        if not chunk:
            continue
        total += len(chunk)
        if total > limit:
            return None
        parts.append(chunk)
    return b"".join(parts)


def _open_jpeg(body: bytes) -> Image.Image | None:
    if not body or len(body) > MAX_BYTES or not body.startswith(b"\xff\xd8"):
        return None
    try:
        image = Image.open(io.BytesIO(body))
        if not _dimensions_ok(image.width, image.height, minimum=True):
            return None
        # Decode toward the size we actually draw. A full-resolution load of a
        # large JPEG is the expensive part.
        image.draft("RGB", (1280, 1280))
        image.load()
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError):
        return None
    rgb = image.convert("RGB")
    edge = max(rgb.width, rgb.height)
    if edge > 1280:
        scale = 1280 / edge
        rgb = rgb.resize(
            (max(1, round(rgb.width * scale)), max(1, round(rgb.height * scale))),
            Image.Resampling.LANCZOS,
        )
    return rgb


def _http_get(url: str) -> tuple[int, bytes]:
    try:
        with httpx.stream("GET", url, timeout=FETCH_TIMEOUT, follow_redirects=False) as response:
            status = response.status_code
            if status != 200:
                return status, b""
            body = _read_limited(response.iter_bytes(), MAX_BYTES)
            if not body:
                return status, b""
            return status, body
    except httpx.HTTPError:
        return 0, b""


def fetch_thumb_dir(directory: str):
    """Load `{id}.jpg` or `{id}.png` from one directory.

    The id is only the 11-character form, and the opened file has to stay
    inside that directory. This is for tests and screenshots. It does not
    fetch a URL.
    """

    root = Path(directory).resolve()

    def fetch(video_id: str) -> Image.Image | None:
        if not is_video_id(video_id):
            return None
        for suffix in (".jpg", ".jpeg", ".png"):
            path = root / f"{video_id}{suffix}"
            if not path.is_file():
                continue
            resolved = path.resolve()
            if not resolved.is_relative_to(root) or resolved == root:
                return None
            try:
                image = Image.open(resolved)
                if not _dimensions_ok(image.width, image.height, minimum=False):
                    return None
                image.load()
            except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError):
                return None
            return image.convert("RGB")
        return None

    return fetch


def thumb_cache() -> ThumbCache:
    """Production cache. `DJTUBE_THUMB_DIR` replaces i.ytimg.com with local files."""

    directory = os.environ.get("DJTUBE_THUMB_DIR", "").strip()
    if not directory:
        return ThumbCache()
    return ThumbCache(fetch_thumb_dir(directory))


def load_youtube_thumb(video_id: str, get=None) -> Image.Image | None:
    """Try maxresdefault, then hqdefault. `get` is `(url) -> (status, body)`."""

    if not is_video_id(video_id):
        return None
    fetch = get or _http_get
    for url in thumb_urls(video_id):
        status, body = fetch(url)
        if status != 200:
            continue
        image = _open_jpeg(body)
        if image is not None:
            return image
    return None


class ThumbCache:
    """Memory cache. Fetches run on one worker so a frame is never blocked on the network."""

    def __init__(
        self,
        fetch=None,
        *,
        max_items: int = MAX_ITEMS,
        max_bytes: int = MAX_CACHE_BYTES,
        negative_ttl: float = NEGATIVE_TTL,
        clock=time.monotonic,
    ) -> None:
        self._fetch = fetch or load_youtube_thumb
        self.max_items = max_items
        self.max_bytes = max_bytes
        self.negative_ttl = negative_ttl
        self._clock = clock
        self._images: OrderedDict[str, Image.Image] = OrderedDict()
        self._bytes = 0
        self._missing: dict[str, float] = {}
        self._queued: set[str] = set()
        self._lock = threading.Lock()
        self._jobs: queue.Queue[str | None] = queue.Queue(maxsize=MAX_QUEUED)
        self._worker: threading.Thread | None = None

    def get(self, video_id: str) -> Image.Image | None:
        with self._lock:
            image = self._images.get(video_id)
            if image is None:
                return None
            self._images.move_to_end(video_id)
            return image

    def want(self, video_id: str) -> None:
        if not is_video_id(video_id):
            return
        with self._lock:
            if video_id in self._images or video_id in self._queued:
                return
            if self._missing.get(video_id, 0) > self._clock():
                return
            if self._jobs.qsize() >= MAX_QUEUED:
                return
            self._queued.add(video_id)
            self._ensure_worker()
        try:
            self._jobs.put_nowait(video_id)
        except queue.Full:
            with self._lock:
                self._queued.discard(video_id)

    def warm(self, video_id: str) -> Image.Image | None:
        """Fetch on this thread. Tests and the screenshot run use this."""

        image = self.get(video_id)
        if image is not None:
            return image
        try:
            loaded = self._fetch(video_id)
        except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError):
            loaded = None
        self._store(video_id, loaded)
        return loaded

    def _ensure_worker(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        self._worker = threading.Thread(target=self._run, name="djtube-thumbs", daemon=True)
        self._worker.start()

    def _run(self) -> None:
        while True:
            video_id = self._jobs.get()
            if video_id is None:
                return
            try:
                image = self._fetch(video_id)
            except Exception:
                image = None
            self._store(video_id, image)

    def _store(self, video_id: str, image: Image.Image | None) -> None:
        with self._lock:
            self._queued.discard(video_id)
            if image is None:
                self._remember_miss(video_id)
                return
            self._missing.pop(video_id, None)
            previous = self._images.pop(video_id, None)
            if previous is not None:
                self._bytes -= previous.width * previous.height * 3
            self._images[video_id] = image
            self._bytes += image.width * image.height * 3
            while self._images and (len(self._images) > self.max_items or self._bytes > self.max_bytes):
                _old_id, old = self._images.popitem(last=False)
                self._bytes -= old.width * old.height * 3

    def _remember_miss(self, video_id: str) -> None:
        now = self._clock()
        expired = [key for key, until in self._missing.items() if until <= now]
        for key in expired:
            del self._missing[key]
        self._missing[video_id] = now + self.negative_ttl
        while len(self._missing) > MAX_MISSING:
            self._missing.pop(next(iter(self._missing)))
