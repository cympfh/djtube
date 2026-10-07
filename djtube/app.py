from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import mimetypes
import threading
import time
from collections import OrderedDict, deque
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from djtube.assets import DOCUMENT_CACHE, VersionedStaticFiles, asset_version, stamp_document
from djtube.audio import AudioError, audio_needs_cookies, clear_audio_cache, open_audio
from djtube.cookies import MAX_COOKIE_BYTES, CookieError, CookieStore, cookie_path, install_store
from djtube.ids import is_video_id
from djtube.live import LiveHub, _publisher_address, live_seen_path, mount_live
from djtube.thumbs import thumb_cache
from djtube.paths import INDEX_PATH, PUBLIC_PREFIX, STATIC_DIR
from djtube.playlists import PlaylistError, PlaylistStore, normalize_name, playlist_path
from djtube.search import SearchError, api_key, search_mode, search_tracks
from djtube.youtube_playlist import (
    FetchControl,
    ImportCancelled,
    PlaylistLookupError,
    fetch_youtube_playlist,
    parse_playlist_id,
    parse_playlist_url,
    rejected_playlist_message,
)

mimetypes.add_type("text/javascript", ".js", strict=True)
mimetypes.add_type("text/css", ".css", strict=True)


class StripPrefixMiddleware:
    """Accept /djtube/... on the container so direct :8098 access matches the public URL.

    nginx in front of s.cympfh.cc strips /djtube/ before proxying, so those requests
    already arrive at /. Both shapes hit the same routes.
    """

    def __init__(self, app, prefix: str = PUBLIC_PREFIX):
        self.app = app
        self.prefix = prefix.rstrip("/")

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket") and self.prefix:
            path = scope.get("path") or ""
            if path == self.prefix or path.startswith(self.prefix + "/"):
                scope = dict(scope)
                stripped = path[len(self.prefix) :] or "/"
                if not stripped.startswith("/"):
                    stripped = "/" + stripped
                scope["path"] = stripped
                scope["raw_path"] = stripped.encode("ascii", "ignore")
        await self.app(scope, receive, send)


def render_index() -> str:
    html = INDEX_PATH.read_text(encoding="utf-8")
    html = html.replace("__PUBLIC_PREFIX__", PUBLIC_PREFIX)
    return stamp_document(html, asset_version())


class PlaylistNameBody(BaseModel):
    name: str = ""


class BpmBody(BaseModel):
    id: Any = None
    bpm: Any = None


class MoveTrackBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    from_index: int = Field(alias="from")
    to_index: int = Field(alias="to")


IMPORTS_PER_MINUTE = 6
IMPORT_WINDOW_SECONDS = 60.0
IMPORTS_PER_DAY = 100
IMPORTS_PER_IP = 20
IMPORT_DAY_SECONDS = 86400.0
_DAY_LIMIT = "24時間の取り込みの上限に達しました"
# Same length as a playlist source. Keywords are still cut to 120 in normalize_query.
SEARCH_QUERY_MAX = 2000
PLAYLIST_SEARCH_TTL = 600.0
PLAYLIST_SEARCH_CACHE_MAX = 32

# One worker for the process. Two uvicorn workers would run two imports at once.
_IMPORT_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="djtube-import")


class ImportSlot:
    """One import at a time. The slot stays taken until that import's worker returns."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._busy = False
        self._control: FetchControl | None = None
        self._minute: deque[float] = deque()
        self._day: deque[float] = deque()
        self._by_ip: dict[str, deque[float]] = {}
        self._kind = "import"
        self.now = time.monotonic

    def acquire(self, ip: str, *, kind: str = "import") -> FetchControl:
        with self._lock:
            if self._busy:
                if self._kind == "search":
                    raise HTTPException(429, "プレイリストを取得中です")
                stopping = self._control is not None and self._control.cancel.is_set()
                raise HTTPException(429, "前の取り込みを止めています" if stopping else "取り込み中です")
            self._charge(ip)
            control = FetchControl()
            self._busy = True
            self._kind = kind
            self._control = control
            return control

    def _expire(self, bucket: deque[float], stamp: float, window: float) -> None:
        while bucket and stamp - bucket[0] >= window:
            bucket.popleft()

    def _charge(self, ip: str) -> None:
        stamp = self.now()
        self._expire(self._minute, stamp, IMPORT_WINDOW_SECONDS)
        self._expire(self._day, stamp, IMPORT_DAY_SECONDS)
        bucket = self._by_ip.get(ip, deque())
        self._expire(bucket, stamp, IMPORT_DAY_SECONDS)
        if len(self._minute) >= IMPORTS_PER_MINUTE:
            raise HTTPException(429, "取り込みの回数が多いです")
        if len(self._day) >= IMPORTS_PER_DAY or len(bucket) >= IMPORTS_PER_IP:
            raise HTTPException(429, _DAY_LIMIT)
        self._minute.append(stamp)
        self._day.append(stamp)
        bucket.append(stamp)
        self._by_ip[ip] = bucket

    def release(self) -> None:
        with self._lock:
            self._busy = False
            self._control = None
            self._kind = "import"


class PlaylistSearchCache:
    """Playlist id to tracks. Entries expire, and the oldest is dropped past the cap."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: OrderedDict[str, tuple[float, str, list[dict[str, object]]]] = OrderedDict()
        self.ttl = PLAYLIST_SEARCH_TTL
        self.limit = PLAYLIST_SEARCH_CACHE_MAX
        self.now = time.monotonic

    def get(self, list_id: str) -> tuple[str, list[dict[str, object]]] | None:
        with self._lock:
            item = self._items.get(list_id)
            if item is None:
                return None
            stamp, source, tracks = item
            if self.now() - stamp >= self.ttl:
                del self._items[list_id]
                return None
            self._items.move_to_end(list_id)
            return source, [dict(track) for track in tracks]

    def put(self, list_id: str, source: str, tracks: list[dict[str, object]]) -> None:
        with self._lock:
            self._items[list_id] = (self.now(), source, [dict(track) for track in tracks])
            self._items.move_to_end(list_id)
            while len(self._items) > self.limit:
                self._items.popitem(last=False)


def _playlist_search_job(source: str, control: FetchControl):
    """Playlist contents for the search box. The scan cap is the display cap."""

    fetched = fetch_youtube_playlist(source, control=control)
    if control.cancel.is_set():
        raise ImportCancelled()
    origin = "youtube" if api_key() else "ytdlp"
    return origin, [track.as_dict() for track in fetched.tracks]


def _import_job(store, source, room, known, control, dest_id, dest_name):
    fetched = fetch_youtube_playlist(source, limit=room, known=known, control=control)
    if control.cancel.is_set():
        raise ImportCancelled()
    return store.import_tracks(
        fetched.tracks,
        playlist_id=dest_id or None,
        name=dest_name if not dest_id else None,
        unavailable=fetched.unavailable,
        repeated=fetched.repeated,
        overflow=fetched.overflow,
    )


async def _run_import(request: Request, control: FetchControl, job):
    """Run `job` on the single import worker. Watch the client between polls.

    The worker is joined before this returns, including when the client
    disconnects, so the slot stays taken until that job returns. A
    BaseException in the job is stored on the future and does not kill the worker.
    """

    loop = asyncio.get_running_loop()
    future = loop.run_in_executor(_IMPORT_EXECUTOR, job)
    try:
        while not future.done():
            if await request.is_disconnected():
                control.abort()
            await asyncio.wait({future}, timeout=0.05)
        if control.cancel.is_set() or await request.is_disconnected():
            control.abort()
            try:
                await future
            except Exception:
                return None
            return None
        return future.result()
    finally:
        if not future.done():
            control.abort()
            try:
                await asyncio.shield(future)
            except Exception:
                pass


def _import_destination(payload: dict) -> tuple[str, str]:
    dest_id = payload.get("playlist_id", "")
    dest_name = payload.get("name", "")
    if dest_id is None:
        dest_id = ""
    if dest_name is None:
        dest_name = ""
    if not isinstance(dest_id, str) or not isinstance(dest_name, str):
        raise HTTPException(400, "追加先を選んでください")
    return dest_id.strip(), dest_name.strip()


def _default_live() -> LiveHub:
    """Remember live ids in data/live-seen.json. A bad file disables streaming only."""

    try:
        return LiveHub(seen_path=live_seen_path(), thumbs=thumb_cache())
    except OSError:
        logging.getLogger("djtube.live").warning("live streaming is off because id records are unavailable")
        return LiveHub(enabled=False, availability="unreadable", thumbs=thumb_cache())


def create_app(
    playlist_store: PlaylistStore | None = None,
    cookies: CookieStore | None = None,
    live: LiveHub | None = None,
) -> FastAPI:
    app = FastAPI(title="djtube")
    slot = ImportSlot()
    cache = PlaylistSearchCache()
    app.state.import_slot = slot
    app.state.playlist_search_cache = cache
    store = playlist_store if playlist_store is not None else PlaylistStore(playlist_path())
    jar = cookies if cookies is not None else CookieStore(cookie_path())
    install_store(jar)
    mount_live(app, live if live is not None else _default_live())

    def raise_playlist(exc: PlaylistError) -> None:
        raise HTTPException(exc.status, str(exc)) from None

    @app.get("/api/health")
    def health() -> dict[str, str | bool]:
        return {
            "ok": True,
            "search": search_mode(),
            "prefix": PUBLIC_PREFIX,
            "flx4": "mapped",
            "playback": "ytdlp-stream",
            "live": app.state.live.health_live(),
        }

    @app.get("/api/audio/{video_id}/cause")
    def audio_cause(video_id: str) -> dict[str, bool]:
        if not is_video_id(video_id):
            raise HTTPException(404, "音源がありません")
        return {"cookies": audio_needs_cookies(video_id)}

    @app.get("/api/audio/{video_id}")
    def stream_audio(video_id: str, request: Request):
        if not is_video_id(video_id):
            raise HTTPException(404, "音源がありません")
        try:
            upstream = open_audio(video_id, request.headers.get("range"))
        except AudioError as exc:
            raise HTTPException(exc.status, str(exc)) from None
        return StreamingResponse(
            upstream.iter_bytes(),
            status_code=upstream.status_code,
            headers=upstream.response_headers(),
        )

    async def search_playlist(source: str, list_id: str, request: Request) -> dict[str, object] | Response:
        cached = cache.get(list_id)
        if cached is not None:
            origin, tracks = cached
            return {"source": origin, "tracks": tracks}
        control = slot.acquire(_publisher_address(request), kind="search")
        try:
            loaded = await _run_import(
                request,
                control,
                lambda: _playlist_search_job(source, control),
            )
            if loaded is None:
                return Response(status_code=499)
            origin, tracks = loaded
            cache.put(list_id, origin, tracks)
            return {"source": origin, "tracks": tracks}
        except ImportCancelled:
            return Response(status_code=499)
        except PlaylistLookupError as exc:
            if control.cancel.is_set():
                return Response(status_code=499)
            raise HTTPException(exc.status, str(exc)) from None
        finally:
            slot.release()

    @app.get("/api/search")
    async def search(
        request: Request,
        q: str = Query(min_length=1, max_length=SEARCH_QUERY_MAX),
        music: bool = True,
    ) -> dict[str, object]:
        list_id = parse_playlist_url(q)
        if list_id is not None:
            return await search_playlist(q, list_id, request)
        try:
            tracks, source = await asyncio.to_thread(search_tracks, q, music=music)
        except SearchError as exc:
            raise HTTPException(400 if str(exc) == "検索語を入れてください" else 502, str(exc)) from None
        return {"source": source, "tracks": [track.as_dict() for track in tracks]}

    @app.get("/api/cookies")
    def cookie_status() -> dict[str, bool]:
        return {"present": jar.present()}

    def store_cookies(data: bytes) -> dict[str, bool]:
        try:
            jar.replace(data)
        except CookieError as exc:
            raise HTTPException(exc.status, str(exc)) from None
        clear_audio_cache()
        return {"present": True}

    @app.post("/api/cookies")
    async def upload_cookies(
        file: UploadFile | None = File(None),
        text: str | None = Form(None),
    ) -> dict[str, bool]:
        if file is not None:
            data = await file.read(MAX_COOKIE_BYTES + 1)
            await file.close()
        elif text is not None:
            try:
                data = text.encode("utf-8")
            except UnicodeEncodeError:
                raise HTTPException(400, "Cookie のファイルを読めません") from None
            if len(data) > MAX_COOKIE_BYTES + 1:
                data = data[: MAX_COOKIE_BYTES + 1]
        else:
            raise HTTPException(400, "Cookie のファイルを選んでください")
        return store_cookies(data)

    @app.get("/api/playlists")
    def list_playlists() -> dict[str, object]:
        return store.snapshot()

    @app.post("/api/bpm")
    def set_track_bpm(body: BpmBody) -> dict[str, object]:
        try:
            bpm = store.set_bpm(body.id, body.bpm)
        except PlaylistError as exc:
            raise_playlist(exc)
        return {"id": body.id, "bpm": bpm}

    @app.post("/api/playlists")
    def create_playlist(body: PlaylistNameBody) -> dict[str, object]:
        try:
            return store.create(body.name)
        except PlaylistError as exc:
            raise_playlist(exc)

    @app.patch("/api/playlists/{playlist_id}")
    def rename_playlist(playlist_id: str, body: PlaylistNameBody) -> dict[str, object]:
        try:
            return store.rename(playlist_id, body.name)
        except PlaylistError as exc:
            raise_playlist(exc)

    @app.delete("/api/playlists/{playlist_id}")
    def delete_playlist(playlist_id: str) -> Response:
        try:
            store.delete(playlist_id)
        except PlaylistError as exc:
            raise_playlist(exc)
        return Response(status_code=204)

    @app.post("/api/playlists/{playlist_id}/tracks")
    async def add_playlist_track(playlist_id: str, request: Request) -> dict[str, object]:
        try:
            payload = await request.json()
        except Exception:
            raise HTTPException(400, "曲を選べません") from None
        try:
            return store.add_track(playlist_id, payload)
        except PlaylistError as exc:
            raise_playlist(exc)

    @app.delete("/api/playlists/{playlist_id}/tracks/{index}")
    def remove_playlist_track(playlist_id: str, index: int) -> dict[str, object]:
        try:
            return store.remove_track(playlist_id, index)
        except PlaylistError as exc:
            raise_playlist(exc)

    @app.post("/api/playlists/{playlist_id}/tracks/move")
    def move_playlist_track(playlist_id: str, body: MoveTrackBody) -> dict[str, object]:
        try:
            return store.move_track(playlist_id, body.from_index, body.to_index)
        except PlaylistError as exc:
            raise_playlist(exc)

    @app.post("/api/playlists/import")
    async def import_playlist(request: Request) -> dict[str, object]:
        try:
            payload = await request.json()
        except Exception:
            raise HTTPException(400, "プレイリストのURLを入れてください") from None
        if not isinstance(payload, dict):
            raise HTTPException(400, "プレイリストのURLを入れてください")
        source = payload.get("url", "")
        if not isinstance(source, str):
            raise HTTPException(400, "プレイリストのURLを入れてください")
        rejected = rejected_playlist_message(source)
        if rejected:
            raise HTTPException(400, rejected)
        if parse_playlist_id(source) is None:
            raise HTTPException(400, "プレイリストのURLを入れてください")
        dest_id, dest_name = _import_destination(payload)
        if dest_id and dest_name:
            raise HTTPException(400, "追加先を一つ選んでください")
        current = 0
        known: set[str] = set()
        if dest_id:
            try:
                tracks = store.get(dest_id)["tracks"]
            except PlaylistError as exc:
                raise_playlist(exc)
            current = len(tracks)
            known = {item["id"] for item in tracks if isinstance(item.get("id"), str)}
        elif dest_name:
            if not normalize_name(dest_name):
                raise HTTPException(400, "名前を入れてください")
            if len(store.list_playlists()) >= store.playlist_limit:
                raise HTTPException(400, "プレイリストが多すぎます")
        elif "name" in payload:
            raise HTTPException(400, "名前を入れてください")
        else:
            raise HTTPException(400, "追加先を選んでください")
        room = store.track_limit - current
        if room < 1:
            raise HTTPException(400, "曲数が多すぎます")
        control = slot.acquire(_publisher_address(request))
        try:
            saved = await _run_import(
                request,
                control,
                lambda: _import_job(store, source, room, known, control, dest_id, dest_name),
            )
            if saved is None:
                return Response(status_code=499)
            return saved
        except ImportCancelled:
            return Response(status_code=499)
        except PlaylistLookupError as exc:
            if control.cancel.is_set():
                return Response(status_code=499)
            raise HTTPException(exc.status, str(exc)) from None
        except PlaylistError as exc:
            raise_playlist(exc)
        finally:
            slot.release()

    @app.get("/")
    def index() -> HTMLResponse:
        return HTMLResponse(render_index(), headers={"Cache-Control": DOCUMENT_CACHE})

    app.mount("/static", VersionedStaticFiles(directory=str(STATIC_DIR)), name="static")
    app.add_middleware(StripPrefixMiddleware, prefix=PUBLIC_PREFIX)
    return app


app = create_app()
