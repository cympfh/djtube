"""Proxy a progressive YouTube audio stream resolved by yt-dlp.

A YouTube iframe cannot be routed into Web Audio, so the deck plays this
stream instead. Search is unchanged. Nothing here calls another site's
download API, and the signed media URL is not returned to the browser.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from djtube.ids import is_video_id, watch_url

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


_LOG_HANDLER = "djtube-audio"
_DETAIL_LIMIT = 800
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_BARE_MEDIA_RE = re.compile(r"(?i)\b(?:[a-z0-9-]+\.)*googlevideo\.com[^\s\"'<>]*")
_SIGNED_MARK_RE = re.compile(r"(?i)(?:sig|signature|lsig|spc|sparams)=")
_SECRET_URL_RE = re.compile(r"(?i)(?:api_key|apikey|youtube_api_key|access_token|key)=")
_API_KEY_RE = re.compile(r"\bAIza[0-9A-Za-z_\-]{10,}\b")
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-+/=]+")
_COOKIE_HEADER_RE = re.compile(r"(?i)\b(?:set-)?cookie\s*[:=]\s*\S+(?:\s*;\s*\S+)*")
_NAMED_COOKIE_RE = re.compile(
    r"(?i)\b(?:SAPISID|HSID|SSID|APISID|SID|__Secure-[\w.-]+|VISITOR_INFO1_LIVE|LOGIN_INFO|PREF|YSC|SIDCC|SOCS)\s*=\s*[^\s;,&]+"
)
_LOOSE_SECRET_RE = re.compile(r"(?i)\b(?:sig|signature|lsig|spc|cookie|cookies|api_key|apikey|youtube_api_key)=[^\s&]+")


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


def _scrub_url(match: re.Match[str]) -> str:
    url = match.group(0)
    if "googlevideo.com" in url.lower() or _SIGNED_MARK_RE.search(url) or _SECRET_URL_RE.search(url):
        return "[url]"
    return url


def _redact(text: str, limit: int = _DETAIL_LIMIT) -> str:
    cleaned = _ANSI_RE.sub("", text or "")
    cleaned = _URL_RE.sub(_scrub_url, cleaned)
    cleaned = _BARE_MEDIA_RE.sub("[url]", cleaned)
    cleaned = _API_KEY_RE.sub("[redacted]", cleaned)
    cleaned = _BEARER_RE.sub("[redacted]", cleaned)
    cleaned = _COOKIE_HEADER_RE.sub("cookie=[redacted]", cleaned)
    cleaned = _NAMED_COOKIE_RE.sub("[redacted]", cleaned)
    cleaned = _LOOSE_SECRET_RE.sub("[redacted]", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) > limit:
        cleaned = cleaned[: limit - 3].rstrip() + "..."
    return cleaned


def _ytdlp_code(ydl: object, exc: BaseException) -> int:
    if isinstance(exc, SystemExit) and isinstance(exc.code, int):
        return exc.code
    if type(exc).__name__ == "DownloadCancelled":
        return 101
    retcode = getattr(ydl, "_download_retcode", 0)
    if isinstance(retcode, int) and retcode != 0:
        return retcode
    return 1


def _public_token(value: object, limit: int = 80) -> str:
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, float):
        if value != value:
            return ""
        value = int(value) if value.is_integer() else round(value, 3)
    text = str(value).strip()
    if not text or len(text) > limit:
        return ""
    if any(ch in text for ch in (" ", "\n", "\r", "\t", '"', "'", "&", "?", "#")):
        return ""
    lowered = text.lower()
    if any(mark in lowered for mark in ("http://", "https://", "cookie", "sig=", "signature=", "aiza", "bearer")):
        return ""
    return text


def _rate_token(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return ""
    try:
        number = float(value)
    except ValueError:
        return ""
    if number != number or number <= 0 or number > 100000:
        return ""
    if number.is_integer():
        return str(int(number))
    return f"{number:.3f}".rstrip("0").rstrip(".")


def _format_summary(fmt: dict) -> str:
    parts: list[str] = []
    for key, value in (
        ("ext", fmt.get("ext")),
        ("acodec", fmt.get("acodec")),
        ("abr", _rate_token(fmt.get("abr") if fmt.get("abr") not in (None, "") else fmt.get("tbr"))),
        ("protocol", fmt.get("protocol") or "https"),
        ("mime", content_type_for(fmt) or ""),
        ("format_id", fmt.get("format_id")),
    ):
        token = _public_token(value)
        if token:
            parts.append(f"{key}={token}")
    return " ".join(parts)


def _format_counts(info: object) -> str:
    if not isinstance(info, dict):
        return "formats=none"
    formats = info.get("formats")
    if not isinstance(formats, list):
        return "formats=none"
    usable = sum(1 for fmt in formats if _usable_format(fmt))
    return f"formats={len(formats)} usable={usable}"


def _emit(level: int, video_id: str, path: str, event: str, *, tail: str = "", **fields: object) -> None:
    video = video_id if is_video_id(video_id) else "-"
    chunks = [f"video={video}", f"path={path}", f"event={event}"]
    for key, value in fields.items():
        if value is None or value == "":
            continue
        if key == "detail":
            detail = _redact(str(value)).replace('"', "'")
            if not detail:
                continue
            chunks.append(f'detail="{detail}"')
            continue
        token = _public_token(value)
        if token:
            chunks.append(f"{key}={token}")
    message = " ".join(chunks)
    if tail:
        message = f"{message} {tail}"
    log.log(level, message)


def _failure_blob(*parts: str) -> str:
    unique: list[str] = []
    for part in parts:
        text = _ANSI_RE.sub("", part or "").strip()
        if text and text not in unique:
            unique.append(text)
    return "\n".join(unique)


def _exception_chain(exc: BaseException) -> list[BaseException]:
    found: list[BaseException] = []
    seen: set[int] = set()

    def walk(item: object) -> None:
        if not isinstance(item, BaseException) or id(item) in seen:
            return
        seen.add(id(item))
        found.append(item)
        walk(item.__cause__)
        info = getattr(item, "exc_info", None)
        if isinstance(info, tuple) and len(info) >= 2:
            walk(info[1])

    walk(exc)
    return found


class _YtdlpCapture:
    """Hold yt-dlp warnings and errors so the process does not write them raw."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    def debug(self, message: object) -> None:
        return

    def info(self, message: object) -> None:
        return

    def warning(self, message: object) -> None:
        self._keep(message)

    def error(self, message: object) -> None:
        self._keep(message)

    def _keep(self, message: object) -> None:
        text = message if isinstance(message, str) else str(message)
        text = _ANSI_RE.sub("", text).strip()
        if text and text not in self.messages:
            self.messages.append(text)


def _extract(video_id: str) -> dict:
    import yt_dlp

    captured = _YtdlpCapture()
    # logger keeps yt-dlp from writing the media URL or cookies to stderr.
    options = {
        "quiet": True,
        "no_warnings": True,
        "no_color": True,
        "noprogress": True,
        "skip_download": True,
        "noplaylist": True,
        "socket_timeout": 20,
        "format": "bestaudio/best",
        "logger": captured,
    }
    ydl = yt_dlp.YoutubeDL(options)
    try:
        with ydl:
            info = ydl.extract_info(watch_url(video_id), download=False)
    except Exception as exc:
        blob = _failure_blob(*(str(item) for item in _exception_chain(exc)), *captured.messages)
        _emit(logging.WARNING, video_id, "ytdlp", "ended", code=_ytdlp_code(ydl, exc), detail=blob)
        raise AudioError("音源を取得できませんでした") from None
    if not isinstance(info, dict):
        _emit(logging.WARNING, video_id, "ytdlp", "ended", code=0)
        raise AudioError("音源を取得できませんでした")
    note = _failure_blob(*captured.messages)
    if note and _redact(note):
        _emit(logging.WARNING, video_id, "ytdlp", "message", detail=note)
    return info


def resolve_audio(video_id: str, *, extract=_extract, now: float | None = None) -> AudioSource:
    if not is_video_id(video_id):
        raise AudioError("音源がありません", status=404)
    cached = _recall(video_id, now)
    if cached is not None:
        _emit(logging.INFO, video_id, "cache", "hit", mime=cached.content_type)
        return cached
    path = "ytdlp" if extract is _extract else "other"
    _emit(logging.INFO, video_id, path, "start")
    try:
        info = extract(video_id)
    except AudioError:
        if path != "ytdlp":
            _emit(logging.WARNING, video_id, path, "ended")
        raise
    fmt = select_audio_format(info) if isinstance(info, dict) else None
    if fmt is None:
        _emit(logging.WARNING, video_id, path, "ended", tail=_format_counts(info))
        raise AudioError("音源を取得できませんでした")
    content_type = content_type_for(fmt)
    if content_type is None or not host_allowed(str(fmt.get("url"))):
        _emit(logging.WARNING, video_id, path, "ended", tail=_format_summary(fmt))
        raise AudioError("音源を取得できませんでした")
    headers = _header_subset(fmt.get("http_headers") or info.get("http_headers"))
    source = AudioSource(url=str(fmt["url"]), content_type=content_type, headers=headers)
    _remember(video_id, source, now)
    _emit(logging.INFO, video_id, path, "selected", tail=_format_summary(fmt))
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
                _emit(logging.WARNING, video_id, "upstream", "ended", status=str(status_code))
                raise AudioError("音源を取得できませんでした")
            response = _send(client, location, headers)
        status_code = response.status_code
        if status_code not in {200, 206}:
            response.close()
            client.close()
            _emit(logging.WARNING, video_id, "upstream", "ended", status=str(status_code))
            if status_code in {403, 410}:
                raise AudioExpired("音源を取得できませんでした")
            raise AudioError("音源を取得できませんでした")
        _emit(logging.INFO, video_id, "upstream", "open", status=str(status_code))
        return Upstream(client, response, source.content_type)
    except AudioError:
        raise
    except httpx.HTTPError as exc:
        client.close()
        _emit(logging.WARNING, video_id, "upstream", "ended", detail=str(exc))
        raise AudioError("音源を取得できませんでした") from None


def open_audio(video_id: str, range_header: str | None) -> Upstream:
    if not is_video_id(video_id):
        raise AudioError("音源がありません", status=404)
    last_error: AudioError | None = None
    for attempt in range(2):
        source = resolve_audio(video_id)
        try:
            return open_upstream(source, range_header, video_id)
        except AudioExpired as exc:
            forget_audio(video_id)
            last_error = exc
            if attempt == 0:
                _emit(logging.INFO, video_id, "upstream", "retry")
    _emit(logging.WARNING, video_id, "upstream", "retry-exhausted")
    raise last_error or AudioError("音源を取得できませんでした")
