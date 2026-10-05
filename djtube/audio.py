"""Proxy a progressive YouTube audio stream resolved by yt-dlp.

A YouTube iframe cannot be routed into Web Audio, so the deck plays this
stream instead. Search is unchanged. Nothing here calls another site's
download API, and the signed media URL is not returned to the browser.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx

from djtube.ids import is_video_id, watch_url

log = logging.getLogger(__name__)

CACHE_SECONDS = 3600
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
_LOCK = threading.Lock()
_CACHE: dict[str, tuple[float, AudioSource]] = {}


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


def clear_audio_cache() -> None:
    with _LOCK:
        _CACHE.clear()


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


def _remember(video_id: str, source: AudioSource, now: float | None = None) -> None:
    stamp = time.monotonic() if now is None else now
    with _LOCK:
        _CACHE[video_id] = (stamp, source)


def _recall(video_id: str, now: float | None = None) -> AudioSource | None:
    stamp = time.monotonic() if now is None else now
    with _LOCK:
        cached = _CACHE.get(video_id)
        if cached is None:
            return None
        stored_at, source = cached
        if stamp - stored_at > CACHE_SECONDS:
            _CACHE.pop(video_id, None)
            return None
        return source


def forget_audio(video_id: str) -> None:
    with _LOCK:
        _CACHE.pop(video_id, None)


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
