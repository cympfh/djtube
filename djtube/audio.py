"""Proxy a progressive YouTube audio stream resolved by yt-dlp.

A YouTube iframe cannot be routed into Web Audio, so the deck plays this
stream instead. Search is unchanged. Nothing here calls another site's
download API, and the signed media URL is not returned to the browser.
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx

from djtube.ids import is_video_id, watch_url
from djtube.paths import PACKAGE_DIR

log = logging.getLogger(__name__)

CACHE_SECONDS = 15 * 60
_ALLOWED_HOSTS = ("googlevideo.com", "youtube.com")
_PREFERRED_EXTS = ("m4a", "mp4", "aac", "mp3", "webm", "opus", "ogg")
_CONTENT_TYPES = {
    "m4a": "audio/mp4",
    "mp4": "audio/mp4",
    "aac": "audio/aac",
    "mp3": "audio/mpeg",
    "webm": "audio/webm",
    "opus": "audio/ogg",
    "ogg": "audio/ogg",
}
_HEADER_NAMES = ("User-Agent", "Accept", "Accept-Language")
_CACHE_BYTES = 2 * 1024 * 1024


class AudioError(Exception):
    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


class AudioExpired(AudioError):
    pass


@dataclass(frozen=True)
class AudioSource:
    url: str
    content_type: str
    headers: dict[str, str]


def audio_cache_path() -> Path:
    raw = os.environ.get("DJTUBE_AUDIO_CACHE", "").strip()
    if raw:
        return Path(raw).expanduser()
    return PACKAGE_DIR.parent / "data" / "audio-cache.json"


def host_allowed(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.username or parsed.password:
        return False
    if parsed.port not in (None, 443):
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    return any(host == suffix or host.endswith("." + suffix) for suffix in _ALLOWED_HOSTS)


def content_type_for(fmt: dict) -> str | None:
    mime = str(fmt.get("mime_type") or fmt.get("mimeType") or "")
    if mime.startswith("audio/"):
        return mime.split(";", 1)[0].strip()
    ext = str(fmt.get("ext") or "").lower()
    return _CONTENT_TYPES.get(ext)


def _usable_format(fmt: object) -> bool:
    if not isinstance(fmt, dict):
        return False
    url = fmt.get("url")
    if not isinstance(url, str) or not host_allowed(url):
        return False
    protocol = str(fmt.get("protocol") or "https")
    if protocol not in {"http", "https"}:
        return False
    if fmt.get("fragments"):
        return False
    if fmt.get("vcodec") not in (None, "none"):
        return False
    acodec = str(fmt.get("acodec") or "")
    if acodec in {"", "none"}:
        return False
    return content_type_for(fmt) is not None


def select_audio_format(info: dict | None) -> dict | None:
    if not isinstance(info, dict):
        return None
    formats = info.get("formats")
    candidates = [fmt for fmt in formats if _usable_format(fmt)] if isinstance(formats, list) else []
    if not candidates and _usable_format(info):
        candidates.append(info)
    if not candidates:
        return None

    def rank(fmt: dict) -> tuple[int, float]:
        ext = str(fmt.get("ext") or "").lower()
        try:
            ext_rank = _PREFERRED_EXTS.index(ext)
        except ValueError:
            ext_rank = len(_PREFERRED_EXTS)
        try:
            bitrate = float(fmt.get("abr") or fmt.get("tbr") or 0)
        except (TypeError, ValueError):
            bitrate = 0
        return (ext_rank, -bitrate)

    return sorted(candidates, key=rank)[0]


def _header_subset(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    headers: dict[str, str] = {}
    for name in _HEADER_NAMES:
        value = raw.get(name)
        if isinstance(value, str) and value.strip():
            headers[name] = value.strip()[:500]
    return headers


def _within_window(stored_at: float, now: float) -> bool:
    age = now - stored_at
    return 0 <= age <= CACHE_SECONDS


def _entry_from_disk(video_id: object, item: object) -> tuple[str, float, AudioSource] | None:
    try:
        if not isinstance(video_id, str) or not is_video_id(video_id) or not isinstance(item, dict):
            return None
        stored_at = item.get("stored_at")
        if isinstance(stored_at, bool) or not isinstance(stored_at, (int, float)) or not math.isfinite(stored_at):
            return None
        stored_at = float(stored_at)
        url = item.get("url")
        if not isinstance(url, str) or len(url) > 16384 or not host_allowed(url):
            return None
        content_type = item.get("content_type")
        if not isinstance(content_type, str):
            return None
        content_type = content_type.split(";", 1)[0].strip()
        if (
            not content_type.startswith("audio/")
            or len(content_type) > 100
            or any(char in content_type for char in "\r\n")
        ):
            return None
        return (
            video_id,
            stored_at,
            AudioSource(url=url, content_type=content_type, headers=_header_subset(item.get("headers"))),
        )
    except (OverflowError, ValueError, RecursionError):
        return None


class AudioCache:
    """Resolved media URLs saved beside the other data-volume files.

    The file outlives this process. Each entry still expires after CACHE_SECONDS,
    and a file that cannot be read is ignored.
    """

    def __init__(self, path: Path | None = None):
        self.path = audio_cache_path() if path is None else path
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[float, AudioSource]] = {}
        self._loaded = False

    def remember(self, video_id: str, source: AudioSource, now: float | None = None) -> None:
        # Wall clock, so a restarted process expires the saved URL on the same window.
        stamp = time.time() if now is None else now
        with self._lock:
            self._ensure_loaded(stamp)
            self._entries[video_id] = (stamp, source)
            self._write_locked(stamp)

    def recall(self, video_id: str, now: float | None = None) -> AudioSource | None:
        stamp = time.time() if now is None else now
        with self._lock:
            self._ensure_loaded(stamp)
            cached = self._entries.get(video_id)
            if cached is None:
                return None
            stored_at, source = cached
            if _within_window(stored_at, stamp):
                return source
            self._entries.pop(video_id, None)
            self._write_locked(stamp)
            return None

    def forget(self, video_id: str) -> None:
        with self._lock:
            stamp = time.time()
            self._ensure_loaded(stamp)
            if self._entries.pop(video_id, None) is None:
                return
            self._write_locked(stamp)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._loaded = True
            self._remove_locked()

    def _ensure_loaded(self, now: float) -> None:
        if self._loaded:
            return
        self._loaded = True
        loaded = self._read()
        if not loaded:
            return
        dropped = False
        for video_id, stored_at, source in loaded:
            if not _within_window(stored_at, now):
                dropped = True
                continue
            self._entries.setdefault(video_id, (stored_at, source))
        if dropped:
            self._write_locked(now)

    def _read(self) -> list[tuple[str, float, AudioSource]] | None:
        try:
            if not self.path.is_file():
                return []
            if self.path.stat().st_size > _CACHE_BYTES:
                log.warning("audio cache unreadable: %s", self.path)
                return None
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, RecursionError, OverflowError):
            log.warning("audio cache unreadable: %s", self.path)
            return None
        if not isinstance(raw, dict):
            log.warning("audio cache unreadable: %s", self.path)
            return None
        parsed: list[tuple[str, float, AudioSource]] = []
        for video_id, item in raw.items():
            entry = _entry_from_disk(video_id, item)
            if entry is not None:
                parsed.append(entry)
        return parsed

    def _write_locked(self, now: float) -> None:
        stale = [video_id for video_id, (stored_at, _) in self._entries.items() if not _within_window(stored_at, now)]
        for video_id in stale:
            self._entries.pop(video_id, None)
        if not self._entries:
            self._remove_locked()
            return
        payload = {
            video_id: {
                "stored_at": stored_at,
                "url": source.url,
                "content_type": source.content_type,
                "headers": source.headers,
            }
            for video_id, (stored_at, source) in self._entries.items()
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        temporary = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(text)
            os.replace(temporary, self.path)
        except OSError:
            log.warning("could not write audio cache to %s", self.path)
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _remove_locked(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            log.warning("could not remove audio cache %s", self.path)


_CACHE = AudioCache()


def install_audio_cache(cache: AudioCache) -> None:
    global _CACHE
    _CACHE = cache


def current_audio_cache() -> AudioCache:
    return _CACHE


def clear_audio_cache() -> None:
    _CACHE.clear()


def _remember(video_id: str, source: AudioSource, now: float | None = None) -> None:
    _CACHE.remember(video_id, source, now)


def _recall(video_id: str, now: float | None = None) -> AudioSource | None:
    return _CACHE.recall(video_id, now)


def forget_audio(video_id: str) -> None:
    _CACHE.forget(video_id)


_CAUSE_LOCK = threading.Lock()
_CAUSE: dict[str, bool] = {}
_COOKIE_MARKERS = (
    "sign in",
    "--cookies",
    "cookies are no longer valid",
    "failed to load cookies",
    "login required",
    "login details are needed",
    "only available for registered users",
)


def clear_audio_causes() -> None:
    with _CAUSE_LOCK:
        _CAUSE.clear()


def note_audio_cause(video_id: str, cookies: bool) -> None:
    if not is_video_id(video_id):
        return
    with _CAUSE_LOCK:
        _CAUSE[video_id] = bool(cookies)


def audio_needs_cookies(video_id: str) -> bool:
    if not is_video_id(video_id):
        return False
    with _CAUSE_LOCK:
        return _CAUSE.get(video_id, False)


def _iter_causes(exc: BaseException):
    pending: list[BaseException | None] = [exc]
    seen: set[int] = set()
    while pending:
        item = pending.pop()
        if item is None or id(item) in seen or not isinstance(item, BaseException):
            continue
        seen.add(id(item))
        yield item
        pending.append(item.__cause__)
        pending.append(item.__context__)
        info = getattr(item, "exc_info", None)
        if isinstance(info, tuple) and len(info) > 1:
            pending.append(info[1])
        pending.append(getattr(item, "cause", None))


def failure_needs_cookies(exc: BaseException) -> bool:
    for item in _iter_causes(exc):
        if type(item).__name__ == "CookieLoadError":
            return True
        text = str(item).casefold()
        if any(marker in text for marker in _COOKIE_MARKERS):
            return True
    return False


_LOG_HANDLER = "djtube-audio"
# Logged-in extraction otherwise uses tv_downgraded, whose player response is
# UNPLAYABLE ("The page needs to be reloaded."). web_embedded can return
# progressive https audio without a PO token; web_safari is the other
# cookie-capable client that still answers. Both stay off unless a cookie
# file was uploaded. Deno is not on PATH, so a fetch with no cookie file
# keeps the existing jsless clients.
_COOKIE_PLAYER_CLIENTS = ("web_embedded", "web_safari")
_DENO = Path("/opt/djtube/deno")


def _configure_audio_log() -> None:
    if any(getattr(handler, "name", None) == _LOG_HANDLER for handler in log.handlers):
        return
    handler = logging.StreamHandler()
    handler.name = _LOG_HANDLER
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False


def _emit(level: int, video_id: str, path: str, outcome: str, text: str = "") -> None:
    video = video_id if is_video_id(video_id) else "-"
    line = f"video={video} path={path} {outcome}"
    if text:
        line = f"{line} {text}"
    log.log(level, line)


def _deno_path() -> str | None:
    try:
        if _DENO.is_file():
            return str(_DENO)
    except OSError:
        return None
    return None


def _ytdlp_options(cookiefile: str | None) -> dict:
    options = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "skip_download": True,
        "noplaylist": True,
        "socket_timeout": 20,
        "format": "bestaudio/best",
    }
    if not cookiefile:
        return options
    options["cookiefile"] = cookiefile
    options["extractor_args"] = {"youtube": {"player_client": list(_COOKIE_PLAYER_CLIENTS)}}
    deno = _deno_path()
    if deno:
        options["js_runtimes"] = {"deno": {"path": deno}}
    return options


def _extract(video_id: str) -> dict:
    import yt_dlp

    from djtube.cookies import current_store

    temporary = None
    try:
        temporary = current_store().materialize()
    except OSError:
        note_audio_cause(video_id, False)
        _emit(logging.WARNING, video_id, "ytdlp", "failure")
        raise AudioError("音源を取得できませんでした") from None
    options = _ytdlp_options(str(temporary) if temporary is not None else None)
    try:
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(watch_url(video_id), download=False)
        except Exception as exc:
            note_audio_cause(video_id, failure_needs_cookies(exc))
            _emit(logging.WARNING, video_id, "ytdlp", "failure", str(exc))
            raise AudioError("音源を取得できませんでした") from None
        if not isinstance(info, dict):
            note_audio_cause(video_id, False)
            _emit(logging.WARNING, video_id, "ytdlp", "failure")
            raise AudioError("音源を取得できませんでした")
        note_audio_cause(video_id, False)
        return info
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def resolve_audio(video_id: str, *, extract=_extract, now: float | None = None) -> AudioSource:
    if not is_video_id(video_id):
        raise AudioError("音源がありません", status=404)
    cached = _recall(video_id, now)
    if cached is not None:
        _emit(logging.INFO, video_id, "cache", "success")
        return cached
    path = "ytdlp" if extract is _extract else "other"
    try:
        info = extract(video_id)
    except AudioError:
        if path != "ytdlp":
            _emit(logging.WARNING, video_id, path, "failure")
        raise
    fmt = select_audio_format(info) if isinstance(info, dict) else None
    if fmt is None:
        _emit(logging.WARNING, video_id, path, "failure")
        raise AudioError("音源を取得できませんでした")
    content_type = content_type_for(fmt)
    if content_type is None or not host_allowed(str(fmt.get("url"))):
        _emit(logging.WARNING, video_id, path, "failure")
        raise AudioError("音源を取得できませんでした")
    headers = _header_subset(fmt.get("http_headers") or info.get("http_headers"))
    source = AudioSource(url=str(fmt["url"]), content_type=content_type, headers=headers)
    _remember(video_id, source, now)
    _emit(logging.INFO, video_id, path, "success")
    return source


_configure_audio_log()


class Upstream:
    def __init__(self, client: httpx.Client, response: httpx.Response, content_type: str):
        self.status_code = response.status_code
        self._client = client
        self._response = response
        self._content_type = content_type

    def response_headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": self._content_type,
            "Accept-Ranges": self._response.headers.get("accept-ranges") or "bytes",
            "Cache-Control": "private, max-age=300",
        }
        length = self._response.headers.get("content-length")
        if length:
            headers["Content-Length"] = length
        spanned = self._response.headers.get("content-range")
        if spanned:
            headers["Content-Range"] = spanned
        return headers

    def iter_bytes(self):
        try:
            yield from self._response.iter_bytes(chunk_size=64 * 1024)
        finally:
            self.close()

    def close(self) -> None:
        self._response.close()
        self._client.close()


def _send(client: httpx.Client, url: str, headers: dict[str, str]) -> httpx.Response:
    request = client.build_request("GET", url, headers=headers)
    return client.send(request, stream=True)


def open_upstream(source: AudioSource, range_header: str | None, video_id: str) -> Upstream:
    headers = dict(source.headers)
    if range_header:
        headers["Range"] = range_header
    client = httpx.Client(timeout=httpx.Timeout(20.0, read=60.0), follow_redirects=False)
    try:
        response = _send(client, source.url, headers)
        if response.status_code in {301, 302, 303, 307, 308}:
            location = response.headers.get("location") or ""
            response.close()
            if not host_allowed(location):
                status_code = response.status_code
                client.close()
                _emit(logging.WARNING, video_id, "upstream", "failure", str(status_code))
                raise AudioError("音源を取得できませんでした")
            response = _send(client, location, headers)
        status_code = response.status_code
        if status_code not in {200, 206}:
            response.close()
            client.close()
            _emit(logging.WARNING, video_id, "upstream", "failure", str(status_code))
            if status_code in {403, 410}:
                raise AudioExpired("音源を取得できませんでした")
            raise AudioError("音源を取得できませんでした")
        _emit(logging.INFO, video_id, "upstream", "success", str(status_code))
        return Upstream(client, response, source.content_type)
    except AudioError:
        raise
    except httpx.HTTPError as exc:
        client.close()
        _emit(logging.WARNING, video_id, "upstream", "failure", str(exc))
        raise AudioError("音源を取得できませんでした") from None


def open_audio(video_id: str, range_header: str | None) -> Upstream:
    if not is_video_id(video_id):
        raise AudioError("音源がありません", status=404)
    last_error: AudioError | None = None
    for _attempt in range(2):
        source = resolve_audio(video_id)
        try:
            return open_upstream(source, range_header, video_id)
        except AudioExpired as exc:
            forget_audio(video_id)
            last_error = exc
    raise last_error or AudioError("音源を取得できませんでした")
