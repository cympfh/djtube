"""YouTube playlist lookup, separate from djtube playlist storage.

Search can call `parse_playlist_id` and `fetch_youtube_playlist` to show a
playlist. Import uses the same result, then decides where to save it.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from djtube.ids import is_video_id
from djtube.search import VIDEOS_URL, Track, api_key, track_from_ytdlp_entry, tracks_from_youtube_videos

log = logging.getLogger(__name__)

PLAYLIST_ITEM_LIMIT = 500
PAGE_SIZE = 50
REQUEST_TIMEOUT = 8.0
DEADLINE_SECONDS = 25.0
PLAYLIST_ITEMS_URL = "https://www.googleapis.com/youtube/v3/playlistItems"
_API_PREFIX = "https://www.googleapis.com/youtube/v3/"
_MAX_SOURCE = 2000
_CHARS = r"[0-9A-Za-z_-]"
# Fixed shapes only. A loose "prefix plus 10 characters" also matches ordinary words.
_IMPORTABLE = re.compile(
    rf"^(?:PL{_CHARS}{{16}}|PL{_CHARS}{{32}}|OLAK5uy_{_CHARS}{{33}}|UU{_CHARS}{{22}}|FL{_CHARS}{{22}}|EC{_CHARS}{{32}})$"
)
# Watch Later, Liked, Liked Music, and mixes. Never sent to the API.
_UNIMPORTABLE = re.compile(rf"^(?:WL|LL|LM|RD{_CHARS}{{0,64}})$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_HOSTS = frozenset(
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
_SCHEMELESS = (
    "www.youtube.com/",
    "youtube.com/",
    "m.youtube.com/",
    "music.youtube.com/",
    "www.youtube-nocookie.com/",
    "youtube-nocookie.com/",
    "www.youtu.be/",
    "youtu.be/",
)
_BAD_TITLES = frozenset(
    {
        "Private video",
        "Deleted video",
        "[Private video]",
        "[Deleted video]",
        "非公開の動画",
        "削除された動画",
    }
)
_BAD_AVAILABILITY = frozenset({"private", "premium_only", "subscriber_only", "needs_auth"})


class PlaylistLookupError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class YouTubePlaylist:
    id: str
    tracks: list[Track]
    unavailable: int
    repeated: int
    overflow: int = 0


def _source_text(source: object) -> str | None:
    if not isinstance(source, str):
        return None
    text = source.strip()
    if not text or len(text) > _MAX_SOURCE or _CONTROL.search(text):
        return None
    return text


def _list_token(text: str) -> str | None:
    """Known playlist id in a bare token or a YouTube `list` query, else None."""

    if _IMPORTABLE.fullmatch(text) or _UNIMPORTABLE.fullmatch(text):
        return text
    parsed = _youtube_url(text)
    if parsed is None:
        return None
    for key, values in parse_qs(parsed.query).items():
        if key != "list":
            continue
        for value in values:
            if isinstance(value, str) and (_IMPORTABLE.fullmatch(value) or _UNIMPORTABLE.fullmatch(value)):
                return value
    return None


def parse_playlist_id(source: object) -> str | None:
    """Importable playlist id from a watch/playlist URL or a bare id.

    Ordinary words and Watch Later / Liked / mix ids are not ids.
    """

    text = _source_text(source)
    if text is None:
        return None
    token = _list_token(text)
    if token is not None and _IMPORTABLE.fullmatch(token):
        return token
    return None


def rejected_playlist_message(source: object) -> str | None:
    """Set when the source is a list this importer will not call the API for."""

    text = _source_text(source)
    if text is None:
        return None
    token = _list_token(text)
    if token is not None and _UNIMPORTABLE.fullmatch(token):
        return "取り込めない種類のリストです"
    return None


def fetch_youtube_playlist(
    source: str,
    *,
    client: httpx.Client | None = None,
    limit: int = PLAYLIST_ITEM_LIMIT,
    now=None,
) -> YouTubePlaylist:
    """Public videos in playlist order, up to `limit` items (never above 500).

    Private and deleted videos are not included. `unavailable` counts them.
    `repeated` counts a later copy of a video that is already in `tracks`.
    """

    rejected = rejected_playlist_message(source)
    if rejected:
        raise PlaylistLookupError(rejected, 400)
    list_id = parse_playlist_id(source)
    if list_id is None:
        raise PlaylistLookupError("プレイリストのURLを入れてください", 400)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        limit = PLAYLIST_ITEM_LIMIT
    limit = min(limit, PLAYLIST_ITEM_LIMIT)
    clock = now or time.monotonic
    key = api_key()
    if not key:
        return _fetch_ytdlp(list_id, limit)
    owns = client is None
    http = client or _open_client()
    deadline = clock() + DEADLINE_SECONDS
    try:
        return _fetch_api(http, list_id, key, limit, deadline, clock)
    finally:
        if owns:
            http.close()


def _open_client() -> httpx.Client:
    return httpx.Client(timeout=httpx.Timeout(REQUEST_TIMEOUT))


def _youtube_url(text: str):
    if any(char.isspace() for char in text):
        return None
    candidate = text
    if candidate.startswith("//"):
        candidate = "https:" + candidate
    elif "://" not in candidate:
        lowered = candidate.lower()
        if not lowered.startswith(_SCHEMELESS):
            return None
        candidate = "https://" + candidate
    parsed = urlparse(candidate)
    if parsed.scheme.lower() not in {"http", "https"}:
        return None
    if parsed.username or parsed.password:
        return None
    host = (parsed.hostname or "").lower().rstrip(".")
    if host not in _HOSTS:
        return None
    return parsed


def _budget(deadline: float, now) -> float:
    left = deadline - now()
    if left <= 0:
        raise PlaylistLookupError("プレイリストを取れませんでした", 502)
    return min(REQUEST_TIMEOUT, left)


def _read(http: httpx.Client, url: str, params: dict, timeout: float, *, playlist: bool) -> dict:
    if not url.startswith(_API_PREFIX):
        raise PlaylistLookupError("プレイリストを取れませんでした", 502)
    try:
        response = http.get(url, params=params, timeout=timeout)
    except httpx.HTTPError as exc:
        log.warning("youtube playlist lookup failed: %s", type(exc).__name__)
        raise PlaylistLookupError("プレイリストを取れませんでした", 502) from None
    if _quota_exceeded(response):
        log.warning("youtube playlist lookup status: %s", response.status_code)
        raise PlaylistLookupError("YouTube API の上限に達しました", 429)
    if response.status_code != 200:
        if playlist and _playlist_missing(response):
            raise PlaylistLookupError("そのプレイリストは見つかりませんでした", 404)
        log.warning("youtube playlist lookup status: %s", response.status_code)
        raise PlaylistLookupError("プレイリストを取れませんでした", 502)
    try:
        payload = response.json()
    except Exception:
        raise PlaylistLookupError("プレイリストを取れませんでした", 502) from None
    if not isinstance(payload, dict) or payload.get("error"):
        if playlist and _reason(payload if isinstance(payload, dict) else {}) == "playlistNotFound":
            raise PlaylistLookupError("そのプレイリストは見つかりませんでした", 404)
        raise PlaylistLookupError("プレイリストを取れませんでした", 502)
    return payload


def _quota_exceeded(response: httpx.Response) -> bool:
    if response.status_code != 403:
        return False
    try:
        payload = response.json()
    except Exception:
        return False
    return isinstance(payload, dict) and _reason(payload) == "quotaExceeded"


def _playlist_missing(response: httpx.Response) -> bool:
    if response.status_code == 404:
        return True
    try:
        payload = response.json()
    except Exception:
        return False
    return isinstance(payload, dict) and _reason(payload) == "playlistNotFound"


def _reason(payload: dict) -> str:
    error = payload.get("error")
    if not isinstance(error, dict):
        return ""
    errors = error.get("errors")
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        reason = errors[0].get("reason")
        if isinstance(reason, str):
            return reason
    return ""


def _video_id(item: object) -> str | None:
    if not isinstance(item, dict):
        return None
    details = item.get("contentDetails")
    if isinstance(details, dict):
        video_id = details.get("videoId")
        if isinstance(video_id, str) and is_video_id(video_id):
            return video_id
    snippet = item.get("snippet")
    resource = snippet.get("resourceId") if isinstance(snippet, dict) else None
    if isinstance(resource, dict):
        video_id = resource.get("videoId")
        if isinstance(video_id, str) and is_video_id(video_id):
            return video_id
    return None


def _unavailable_item(item: dict) -> bool:
    status = item.get("status")
    if isinstance(status, dict) and status.get("privacyStatus") == "private":
        return True
    snippet = item.get("snippet")
    title = snippet.get("title") if isinstance(snippet, dict) else None
    return isinstance(title, str) and title.strip() in _BAD_TITLES


def _next_token(payload: dict, previous: str | None) -> str | None:
    token = payload.get("nextPageToken")
    if not isinstance(token, str) or not token or token == previous or len(token) > 2000:
        return None
    if _CONTROL.search(token):
        return None
    return token


def _total_results(payload: dict) -> int | None:
    page_info = payload.get("pageInfo")
    if not isinstance(page_info, dict):
        return None
    total = page_info.get("totalResults")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        return None
    return total


def _fetch_api(http, list_id: str, key: str, limit: int, deadline: float, now) -> YouTubePlaylist:
    candidates: list[str] = []
    unavailable = 0
    seen_items = 0
    page_token: str | None = None
    pages = 0
    total_results: int | None = None
    stopped_early = False
    while seen_items < limit and pages < limit:
        pages += 1
        params: dict[str, object] = {
            "part": "snippet,contentDetails,status",
            "playlistId": list_id,
            "maxResults": min(PAGE_SIZE, limit - seen_items),
            "key": key,
        }
        if page_token:
            params["pageToken"] = page_token
        payload = _read(http, PLAYLIST_ITEMS_URL, params, _budget(deadline, now), playlist=True)
        reported = _total_results(payload)
        if reported is not None:
            total_results = reported
        items = payload.get("items")
        if not isinstance(items, list) or not items:
            break
        for item in items:
            if seen_items >= limit:
                stopped_early = True
                break
            seen_items += 1
            video_id = _video_id(item)
            if video_id is None or not isinstance(item, dict) or _unavailable_item(item):
                unavailable += 1
                continue
            candidates.append(video_id)
        page_token = _next_token(payload, page_token)
        if page_token is None:
            break
        if seen_items >= limit:
            stopped_early = True
            break
    overflow = 0
    if stopped_early and total_results is not None and total_results > seen_items:
        overflow = total_results - seen_items
    return _hydrate_ordered(http, list_id, candidates, unavailable, overflow, key, deadline, now)


def _hydrate_ordered(http, list_id, candidates, unavailable, overflow, key, deadline, now) -> YouTubePlaylist:
    unique: list[str] = []
    repeats: list[str] = []
    seen: set[str] = set()
    for video_id in candidates:
        if video_id in seen:
            repeats.append(video_id)
        else:
            seen.add(video_id)
            unique.append(video_id)
    found: dict[str, Track] = {}
    for start in range(0, len(unique), PAGE_SIZE):
        chunk = unique[start : start + PAGE_SIZE]
        payload = _read(
            http,
            VIDEOS_URL,
            {"part": "snippet,contentDetails", "id": ",".join(chunk), "key": key},
            _budget(deadline, now),
            playlist=False,
        )
        for track in tracks_from_youtube_videos(payload):
            found[track.id] = track
    tracks: list[Track] = []
    repeated = 0
    for video_id in unique:
        track = found.get(video_id)
        if track is None:
            unavailable += 1
        else:
            tracks.append(track)
    for video_id in repeats:
        if video_id in found:
            repeated += 1
        else:
            unavailable += 1
    return YouTubePlaylist(list_id, tracks, unavailable, repeated, overflow)


def _fetch_ytdlp(list_id: str, limit: int) -> YouTubePlaylist:
    import yt_dlp

    deadline = time.monotonic() + DEADLINE_SECONDS
    url = "https://www.youtube.com/playlist?" + urlencode({"list": list_id})
    remaining = max(0.1, deadline - time.monotonic())
    options = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "extract_flat": True,
        "skip_download": True,
        "playlistend": limit,
        "socket_timeout": min(20, remaining),
        "ignoreerrors": True,
        "noplaylist": False,
    }
    outcome: dict[str, object] = {}
    finished = threading.Event()

    def work() -> None:
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                outcome["info"] = ydl.extract_info(url, download=False)
        except Exception as exc:
            outcome["exc"] = exc
        finally:
            finished.set()

    threading.Thread(target=work, name="djtube-playlist-ytdlp", daemon=True).start()
    left = deadline - time.monotonic()
    if left <= 0 or not finished.wait(left):
        log.warning("yt-dlp playlist lookup failed: TimeoutError")
        raise PlaylistLookupError("プレイリストを取れませんでした", 502) from None
    failure = outcome.get("exc")
    if isinstance(failure, Exception):
        log.warning("yt-dlp playlist lookup failed: %s", type(failure).__name__)
        raise PlaylistLookupError("プレイリストを取れませんでした", 502) from None
    info = outcome.get("info")
    if not isinstance(info, dict):
        raise PlaylistLookupError("プレイリストを取れませんでした", 502)
    entries = info.get("entries")
    if not isinstance(entries, list):
        entries = []
    tracks: list[Track] = []
    seen: set[str] = set()
    unavailable = 0
    repeated = 0
    examined = 0
    for entry in entries:
        if examined >= limit:
            break
        examined += 1
        if not isinstance(entry, dict) or _ytdlp_unavailable(entry):
            unavailable += 1
            continue
        track = track_from_ytdlp_entry(entry)
        if track is None:
            unavailable += 1
            continue
        if track.id in seen:
            repeated += 1
            continue
        seen.add(track.id)
        tracks.append(track)
    overflow = len(entries) - examined if examined >= limit else 0
    return YouTubePlaylist(list_id, tracks, unavailable, repeated, overflow)


def _ytdlp_unavailable(entry: dict) -> bool:
    availability = entry.get("availability")
    if isinstance(availability, str) and availability in _BAD_AVAILABILITY:
        return True
    title = entry.get("title")
    return isinstance(title, str) and title.strip() in _BAD_TITLES
