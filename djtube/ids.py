from __future__ import annotations

import re

VIDEO_ID = re.compile(r"^[0-9A-Za-z_-]{11}$")
_ID_IN_URL = re.compile(r"(?:v=|/shorts/|youtu\.be/)([0-9A-Za-z_-]{11})")


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


def watch_url(video_id: str) -> str:
    if not is_video_id(video_id):
        raise ValueError("invalid video id")
    return f"https://www.youtube.com/watch?v={video_id}"
