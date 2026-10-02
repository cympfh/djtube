from __future__ import annotations

import logging
import os
import re
from dataclasses import asdict, dataclass
from urllib.parse import urlencode

import httpx

from djtube.ids import extract_video_id, is_video_id

log = logging.getLogger(__name__)

RESULT_TARGET = 50
SEARCH_PAGE_SIZE = 50
MAX_SEARCH_PAGES = 3
MUSIC_CATEGORY_ID = "10"
SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
_ISO_DURATION = re.compile(r"^P(?:\d+D)?(?:T(?:(?P<h>\d+)H)?(?:(?P<m>\d+)M)?(?:(?P<s>\d+)S)?)?$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class SearchError(Exception):
    pass


@dataclass(frozen=True)
class Track:
    id: str
    title: str
    channel: str
    duration: int | None
    thumbnail: str | None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def api_key() -> str:
    return os.environ.get("YOUTUBE_API_KEY", "").strip()


def search_mode() -> str:
    return "youtube" if api_key() else "ytdlp"


def normalize_query(query: str) -> str:
    cleaned = _CONTROL.sub(" ", query or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:120]


def parse_iso8601_duration(value: str | None) -> int | None:
    if not value:
        return None
    match = _ISO_DURATION.fullmatch(value)
    if not match:
        return None
    hours = int(match.group("h") or 0)
    minutes = int(match.group("m") or 0)
    seconds = int(match.group("s") or 0)
    total = hours * 3600 + minutes * 60 + seconds
    return total or None


def clean_thumbnail(url: object) -> str | None:
    if not isinstance(url, str) or not url.startswith("https://"):
        return None
    return url[:500]


def _track(
    video_id: object,
    title: object,
    channel: object,
    duration: object,
    thumbnail: object,
) -> Track | None:
    resolved = extract_video_id(video_id)
    if resolved is None:
        return None
    video_id = resolved
    title_text = title.strip() if isinstance(title, str) else ""
    if not title_text:
        title_text = video_id
    channel_text = channel.strip() if isinstance(channel, str) else ""
    seconds: int | None
    if isinstance(duration, bool) or duration is None:
        seconds = None
    elif isinstance(duration, (int, float)):
        seconds = int(round(duration)) if duration > 0 else None
    elif isinstance(duration, str):
        seconds = parse_iso8601_duration(duration)
    else:
        seconds = None
    return Track(
        id=video_id,
        title=title_text[:300],
        channel=channel_text[:200],
        duration=seconds,
        thumbnail=clean_thumbnail(thumbnail),
    )


def tracks_from_youtube_search(payload: dict) -> list[str]:
    ids: list[str] = []
    for item in payload.get("items") or []:
        if not isinstance(item, dict):
            continue
        video_id = (item.get("id") or {}).get("videoId") if isinstance(item.get("id"), dict) else None
        if isinstance(video_id, str) and is_video_id(video_id) and video_id not in ids:
            ids.append(video_id)
    return ids


def tracks_from_youtube_videos(payload: dict) -> list[Track]:
    tracks: list[Track] = []
    for item in payload.get("items") or []:
        if not isinstance(item, dict):
            continue
        snippet = item.get("snippet") if isinstance(item.get("snippet"), dict) else {}
        details = item.get("contentDetails") if isinstance(item.get("contentDetails"), dict) else {}
        thumbs = snippet.get("thumbnails") if isinstance(snippet.get("thumbnails"), dict) else {}
        thumb = None
        for key in ("medium", "default", "high"):
            entry = thumbs.get(key)
            if isinstance(entry, dict) and entry.get("url"):
                thumb = entry.get("url")
                break
        track = _track(
            item.get("id"),
            snippet.get("title"),
            snippet.get("channelTitle"),
            details.get("duration"),
            thumb,
        )
        if track is not None:
            tracks.append(track)
    return tracks


def tracks_from_ytdlp_info(info: dict | None) -> list[Track]:
    if not isinstance(info, dict):
        return []
    entries = info.get("entries") or []
    tracks: list[Track] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        thumbs = entry.get("thumbnails") or []
        thumb = None
        if isinstance(thumbs, list):
            for item in reversed(thumbs):
                if isinstance(item, dict) and isinstance(item.get("url"), str):
                    thumb = item["url"]
                    break
        if thumb is None and isinstance(entry.get("thumbnail"), str):
            thumb = entry["thumbnail"]
        video_ref = entry.get("id")
        if extract_video_id(video_ref) is None:
            video_ref = entry.get("url") or entry.get("webpage_url")
        track = _track(
            video_ref,
            entry.get("title"),
            entry.get("channel") or entry.get("uploader") or entry.get("channel_id"),
            entry.get("duration"),
            thumb,
        )
        if track is not None:
            tracks.append(track)
    return tracks


def _youtube_json(response: httpx.Response) -> dict:
    if response.status_code != 200:
        raise SearchError("検索できませんでした")
    try:
        payload = response.json()
    except Exception as exc:
        raise SearchError("検索できませんでした") from exc
    if not isinstance(payload, dict) or payload.get("error"):
        raise SearchError("検索できませんでした")
    return payload


def _collect_search_ids(http: httpx.Client, query: str, key: str, *, music: bool = False) -> list[str]:
    ids: list[str] = []
    page_token: str | None = None
    for _ in range(MAX_SEARCH_PAGES):
        params = {
            "part": "snippet",
            "type": "video",
            "maxResults": str(SEARCH_PAGE_SIZE),
            "q": query,
            "safeSearch": "none",
            "key": key,
        }
        if music:
            params["videoCategoryId"] = MUSIC_CATEGORY_ID
        if page_token:
            params["pageToken"] = page_token
        payload = _youtube_json(http.get(SEARCH_URL, params=params))
        for video_id in tracks_from_youtube_search(payload):
            if video_id not in ids:
                ids.append(video_id)
            if len(ids) >= RESULT_TARGET:
                return ids[:RESULT_TARGET]
        token = payload.get("nextPageToken")
        if not isinstance(token, str) or not token:
            break
        page_token = token
    return ids[:RESULT_TARGET]


def _hydrate_videos(http: httpx.Client, ids: list[str], key: str) -> list[Track]:
    by_id: dict[str, Track] = {}
    for start in range(0, len(ids), SEARCH_PAGE_SIZE):
        chunk = ids[start : start + SEARCH_PAGE_SIZE]
        payload = _youtube_json(
            http.get(VIDEOS_URL, params={"part": "snippet,contentDetails", "id": ",".join(chunk), "key": key})
        )
        for track in tracks_from_youtube_videos(payload):
            by_id[track.id] = track
    return [by_id[video_id] for video_id in ids if video_id in by_id]


def search_youtube_api(query: str, key: str, client: httpx.Client | None = None, *, music: bool = False) -> list[Track]:
    owns = client is None
    http = client or httpx.Client(timeout=15)
    try:
        ids = _collect_search_ids(http, query, key, music=music)
        if not ids:
            return []
        return _hydrate_videos(http, ids, key)
    except SearchError:
        raise
    except httpx.HTTPError as exc:
        raise SearchError("検索できませんでした") from exc
    finally:
        if owns:
            http.close()


def search_ytdlp(query: str, *, music: bool = False) -> list[Track]:
    import yt_dlp

    if music:
        url = "https://music.youtube.com/search?" + urlencode({"q": query}) + "#songs"
    else:
        url = f"ytsearch{RESULT_TARGET}:{query}"
    options = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "extract_flat": "in_playlist",
        "skip_download": True,
        "playlistend": RESULT_TARGET,
        "socket_timeout": 20,
    }
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:
        log.warning("yt-dlp search failed: %s", type(exc).__name__)
        raise SearchError("検索できませんでした") from None
    return tracks_from_ytdlp_info(info)[:RESULT_TARGET]


def search_tracks(query: str, *, music: bool = True) -> tuple[list[Track], str]:
    normalized = normalize_query(query)
    if not normalized:
        raise SearchError("検索語を入れてください")
    key = api_key()
    if key:
        try:
            tracks = search_youtube_api(normalized, key, music=music)
        except Exception:
            log.warning("YouTube Data API search failed; falling back to yt-dlp")
        else:
            if tracks:
                return tracks, "youtube"
            log.info("YouTube Data API returned no videos; falling back to yt-dlp")
    try:
        return search_ytdlp(normalized, music=music), "ytdlp"
    except SearchError:
        raise
    except Exception as exc:
        log.warning("search failed: %s", type(exc).__name__)
        raise SearchError("検索できませんでした") from None
