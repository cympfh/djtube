"""Proxy a progressive YouTube audio stream resolved by yt-dlp.

A YouTube iframe cannot be routed into Web Audio, so the deck plays this
stream instead. Search is unchanged. Nothing here calls another site's
download API, and the signed media URL is not returned to the browser.

`djtube.audio` records the video id and the path (yt-dlp or cache). A hit
logs a format summary. A miss logs how yt-dlp ended, including wording for a
bot check, a consent screen, or HTTP 403. Signed URLs, API keys, and cookies
stay out of the log.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlparse, urlsplit

import httpx

from djtube.ids import is_video_id, watch_url

log = logging.getLogger(__name__)

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_URL = re.compile(r"https?://[^\s<>\"']+")
_API_KEY = re.compile(r"AIza[0-9A-Za-z_\-]{10,}")
_COOKIE_HEADER = re.compile(r"(?i)\b(set-cookie|cookie)\s*[:=]\s*\S+")
_NAMED_COOKIE = re.compile(
    r"(?i)\b((?:__Secure-|__Host-)?(?:SID|HSID|SSID|APISID|SAPISID|LOGIN_INFO|VISITOR_INFO1_LIVE|YSC|PREF|CONSENT|SOCS))\s*=\s*[^;\s,&]+"
)
_LOOSE_SECRET = re.compile(r"(?i)\b((?:lsig|nsig|sig|signature|sparams|lsparams|spc|pot|po_token)\s*=\s*)[^\s&]+")
_KEY_PARAM = re.compile(r"(?i)(\bkey\s*=\s*)[^\s&]+")
_TOKEN = re.compile(r"^[0-9A-Za-z._:+/-]{1,80}$")
_SIGNED_QUERY_KEYS = {
    "sig",
    "signature",
    "lsig",
    "nsig",
    "sig2",
    "expire",
    "ei",
    "ip",
    "spc",
    "sparams",
    "lsparams",
    "n",
    "key",
    "api_key",
    "token",
    "pot",
    "po_token",
}
_PUBLIC_HOST_SUFFIXES = ("youtube.com", "youtu.be", "github.com")


def _configure_audio_log(logger: logging.Logger) -> None:
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return
    borrowed = logging.getLogger("uvicorn").handlers
    if borrowed:
        for handler in borrowed:
            logger.addHandler(handler)
    else:
        handler = logging.StreamHandler()
        handler.setLevel(logging.INFO)
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
        logger.addHandler(handler)
    logger.propagate = False


_configure_audio_log(log)

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


def _video_for_log(video_id: object) -> str:
    if isinstance(video_id, str) and is_video_id(video_id):
        return video_id
    return "-"


def _log_token(value: object) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number <= 0:
            return None
        text = str(int(number)) if number.is_integer() else str(number)
    else:
        text = str(value).strip()
    if not _TOKEN.fullmatch(text):
        return None
    return text


def _replace_url(match: re.Match[str]) -> str:
    raw = match.group(0)
    trimmed = raw.rstrip(".,);]")
    suffix = raw[len(trimmed) :]
    parts = urlsplit(trimmed)
    host = (parts.hostname or "").lower()
    keys = {key.lower() for key, _ in parse_qsl(parts.query, keep_blank_values=True)}
    sensitive = (
        host.endswith("googlevideo.com")
        or bool(keys & _SIGNED_QUERY_KEYS)
        or any("sig" in key or "token" in key or "cookie" in key for key in keys)
    )
    if "consent" in host:
        return "consent.youtube.com" + suffix
    public = any(host == suffix_host or host.endswith("." + suffix_host) for suffix_host in _PUBLIC_HOST_SUFFIXES)
    if sensitive or not public or (keys - {"v"}):
        return "[redacted-url]" + suffix
    return trimmed + suffix


def redact_secrets(text: str) -> str:
    cleaned = _ANSI.sub("", text or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = _URL.sub(_replace_url, cleaned)
    cleaned = _API_KEY.sub("[redacted-key]", cleaned)
    cleaned = _COOKIE_HEADER.sub(lambda match: f"{match.group(1)}=[redacted]", cleaned)
    cleaned = _NAMED_COOKIE.sub(lambda match: f"{match.group(1)}=[redacted]", cleaned)
    cleaned = _LOOSE_SECRET.sub(lambda match: f"{match.group(1)}[redacted]", cleaned)
    cleaned = _KEY_PARAM.sub(lambda match: f"{match.group(1)}[redacted]", cleaned)
    if len(cleaned) > 500:
        cleaned = cleaned[:500] + "…"
    return cleaned


def failure_notes(text: str) -> list[str]:
    raw = text or ""
    lowered = raw.lower()
    notes: list[str] = []
    if (
        any(
            phrase in lowered
            for phrase in (
                "not a bot",
                "not a robot",
                "sign in to confirm",
                "confirm you're not a bot",
                "confirm you’re not a bot",
            )
        )
        or "ボット" in raw
    ):
        notes.append("Bot判定")
    if (
        "consent.youtube" in lowered
        or "before you continue" in lowered
        or "同意" in raw
        or re.search(r"\bconsent\b(?!\s*=)", lowered)
    ):
        notes.append("同意画面")
    if re.search(r"(?<!\d)403(?!\d)", raw) or "forbidden" in lowered:
        notes.append("403")
    return notes


def format_summary(fmt: dict, content_type: str | None, extractor: object = None) -> str:
    fields = (
        ("extractor", extractor),
        ("format_id", fmt.get("format_id")),
        ("ext", fmt.get("ext")),
        ("acodec", fmt.get("acodec")),
        ("vcodec", fmt.get("vcodec")),
        ("abr", fmt.get("abr") if fmt.get("abr") not in (None, 0, 0.0) else fmt.get("tbr")),
        ("protocol", fmt.get("protocol") or "https"),
        ("content_type", content_type),
    )
    parts = []
    for name, value in fields:
        token = _log_token(value)
        if token is not None:
            parts.append(f"{name}={token}")
    return " ".join(parts)


def _exception_text(exc: BaseException) -> str:
    chunks: list[str] = []
    seen: set[int] = set()
    pending: list[BaseException] = [exc]
    while pending and len(seen) < 5:
        current = pending.pop(0)
        if id(current) in seen:
            continue
        seen.add(id(current))
        chunks.append(str(current))
        info = getattr(current, "exc_info", None)
        nested = info[1] if isinstance(info, tuple) and len(info) > 1 else None
        if isinstance(nested, BaseException):
            pending.append(nested)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        elif current.__context__ is not None:
            pending.append(current.__context__)
    return " ".join(chunk.strip() for chunk in chunks if chunk and chunk.strip())


class _YtdlpLog:
    """Keep yt-dlp's own lines off stderr until they are redacted."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def debug(self, msg: object, *args: object, **kwargs: object) -> None:
        return None

    def info(self, msg: object, *args: object, **kwargs: object) -> None:
        return None

    def warning(self, msg: object, *args: object, **kwargs: object) -> None:
        self.lines.append(str(msg))

    def error(self, msg: object, *args: object, **kwargs: object) -> None:
        self.lines.append(str(msg))


def _exit_label(exc: BaseException, retcode: object) -> str:
    # YoutubeDL raises before it stores a return code. The CLI exits 1 there.
    code = retcode if isinstance(retcode, int) and retcode != 0 else 1
    return f"{code} {type(exc).__name__}"


def _merge_text(parts: list[str]) -> str:
    merged: list[str] = []
    for part in parts:
        text = " ".join(str(part).split())
        if not text:
            continue
        if any(text in earlier for earlier in merged):
            continue
        merged = [earlier for earlier in merged if earlier not in text]
        merged.append(text)
    return " ".join(merged)


def _log_outcome(video_id: str, raw: str, *, failed: str | None) -> None:
    detail = redact_secrets(raw)
    notes = failure_notes(raw)
    if failed is None and not detail and not notes:
        return
    if failed is None:
        bits = ["audio resolve video=%s via=yt-dlp warning"]
        args: list[object] = [video_id]
    else:
        bits = ["audio resolve video=%s via=yt-dlp failed exit=%s"]
        args = [video_id, failed]
    if notes:
        bits.append("notes=%s")
        args.append(",".join(notes))
    if detail:
        bits.append("detail=%s")
        args.append(detail)
    log.warning(" ".join(bits), *args)


def _extract(video_id: str) -> dict:
    import yt_dlp

    captured = _YtdlpLog()
    options = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "skip_download": True,
        "noplaylist": True,
        "socket_timeout": 20,
        "format": "bestaudio/best",
        "logger": captured,
    }
    with yt_dlp.YoutubeDL(options) as ydl:
        try:
            info = ydl.extract_info(watch_url(video_id), download=False)
        except Exception as exc:
            raw = _merge_text([*captured.lines, _exception_text(exc)])
            _log_outcome(video_id, raw, failed=_exit_label(exc, getattr(ydl, "_download_retcode", None)))
            raise AudioError("音源を取得できませんでした") from None
    if not isinstance(info, dict):
        _log_outcome(video_id, _merge_text(captured.lines) or "yt-dlp returned no info", failed="empty")
        raise AudioError("音源を取得できませんでした")
    _log_outcome(video_id, _merge_text(captured.lines), failed=None)
    return info


def resolve_audio(video_id: str, *, extract=_extract, now: float | None = None) -> AudioSource:
    if not is_video_id(video_id):
        raise AudioError("音源がありません", status=404)
    cached = _recall(video_id, now)
    if cached is not None:
        log.info(
            "audio resolve video=%s via=cache content_type=%s",
            video_id,
            _log_token(cached.content_type) or "-",
        )
        return cached
    log.info("audio resolve video=%s via=yt-dlp", video_id)
    info = extract(video_id)
    fmt = select_audio_format(info if isinstance(info, dict) else None)
    if not isinstance(info, dict) or fmt is None:
        log.warning("audio resolve video=%s via=yt-dlp selected none", video_id)
        raise AudioError("音源を取得できませんでした")
    content_type = content_type_for(fmt)
    if content_type is None or not host_allowed(str(fmt.get("url"))):
        log.warning("audio resolve video=%s via=yt-dlp selected rejected", video_id)
        raise AudioError("音源を取得できませんでした")
    summary = format_summary(fmt, content_type, info.get("extractor"))
    log.info("audio resolve video=%s via=yt-dlp selected %s", video_id, summary or "-")
    headers = _header_subset(fmt.get("http_headers") or info.get("http_headers"))
    source = AudioSource(url=str(fmt["url"]), content_type=content_type, headers=headers)
    _remember(video_id, source, now)
    return source


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


def open_upstream(source: AudioSource, range_header: str | None, *, video_id: str | None = None) -> Upstream:
    video = _video_for_log(video_id)
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
                client.close()
                log.warning("audio upstream video=%s redirect rejected", video)
                raise AudioError("音源を取得できませんでした")
            response = _send(client, location, headers)
        if response.status_code in {403, 410}:
            status = response.status_code
            response.close()
            client.close()
            log.warning("audio upstream video=%s status=%s expired", video, status)
            raise AudioExpired("音源を取得できませんでした")
        if response.status_code not in {200, 206}:
            status = response.status_code
            response.close()
            client.close()
            log.warning("audio upstream video=%s status=%s", video, status)
            raise AudioError("音源を取得できませんでした")
        log.info(
            "audio upstream video=%s status=%s content_type=%s ranged=%s",
            video,
            response.status_code,
            _log_token(source.content_type) or "-",
            "yes" if range_header else "no",
        )
        return Upstream(client, response, source.content_type)
    except AudioError:
        raise
    except httpx.HTTPError as exc:
        client.close()
        log.warning(
            "audio upstream video=%s failed exit=%s detail=%s",
            video,
            type(exc).__name__,
            redact_secrets(str(exc)),
        )
        raise AudioError("音源を取得できませんでした") from None


def open_audio(video_id: str, range_header: str | None) -> Upstream:
    if not is_video_id(video_id):
        raise AudioError("音源がありません", status=404)
    last_error: AudioError | None = None
    for _attempt in range(2):
        source = resolve_audio(video_id)
        try:
            return open_upstream(source, range_header, video_id=video_id)
        except AudioExpired as exc:
            forget_audio(video_id)
            last_error = exc
    raise last_error or AudioError("音源を取得できませんでした")
