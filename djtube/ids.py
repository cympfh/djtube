from __future__ import annotations

import re
from urllib.parse import ParseResult, parse_qs, urlparse

VIDEO_ID = re.compile(r"^[0-9A-Za-z_-]{11}$")
_ID_IN_URL = re.compile(r"(?:v=|/shorts/|youtu\.be/)([0-9A-Za-z_-]{11})")
_YOUTUBE_HOSTS = frozenset(
    {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtube-nocookie.com",
        "www.youtube-nocookie.com",
        "youtu.be",
        "www.youtu.be",
    }
)
_PATH_KINDS = frozenset({"shorts", "embed", "live", "v"})
_SCHEMELESS_PREFIXES = (
    "www.youtube.com/",
    "youtube.com/",
    "m.youtube.com/",
    "music.youtube.com/",
    "www.youtube-nocookie.com/",
    "youtube-nocookie.com/",
    "www.youtu.be/",
    "youtu.be/",
)


def is_video_id(value: str) -> bool:
    return bool(VIDEO_ID.fullmatch(value or ""))


def extract_video_id(value: object) -> str | None:
    if isinstance(value, str) and is_video_id(value):
        return value
    if isinstance(value, str):
        match = _ID_IN_URL.search(value)
        if match:
            return match.group(1)
    return None


def video_id_from_query(query: str) -> str | None:
    text = (query or "").strip()
    if is_video_id(text):
        return text
    parsed = _parse_youtube_url(text)
    if parsed is None:
        return None
    return _id_from_youtube_url(parsed)


def _parse_youtube_url(text: str) -> ParseResult | None:
    if any(char.isspace() for char in text) or not text:
        return None
    candidate = text
    if candidate.startswith("//"):
        candidate = "https:" + candidate
    elif "://" not in candidate:
        lowered = candidate.lower()
        if not lowered.startswith(_SCHEMELESS_PREFIXES):
            return None
        candidate = "https://" + candidate
    parsed = urlparse(candidate)
    if parsed.scheme.lower() not in {"http", "https"}:
        return None
    host = (parsed.hostname or "").lower().rstrip(".")
    if host not in _YOUTUBE_HOSTS:
        return None
    return parsed


def _id_from_youtube_url(parsed: ParseResult) -> str | None:
    host = (parsed.hostname or "").lower().rstrip(".")
    parts = [part for part in (parsed.path or "").split("/") if part]
    if host in {"youtu.be", "www.youtu.be"}:
        if parts and is_video_id(parts[0]):
            return parts[0]
        return None
    if len(parts) >= 2 and parts[0].lower() in _PATH_KINDS and is_video_id(parts[1]):
        return parts[1]
    if not parts or parts[0].lower() == "watch":
        return _query_video_id(parsed.query)
    return None


def _query_video_id(query: str) -> str | None:
    for key, values in parse_qs(query).items():
        if key != "v":
            continue
        for value in values:
            if is_video_id(value):
                return value
    return None


def watch_url(video_id: str) -> str:
    if not is_video_id(video_id):
        raise ValueError("invalid video id")
    return f"https://www.youtube.com/watch?v={video_id}"
