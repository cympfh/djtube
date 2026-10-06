from __future__ import annotations

import logging
import mimetypes

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from djtube.assets import DOCUMENT_CACHE, VersionedStaticFiles, asset_version, stamp_document
from djtube.audio import AudioError, audio_needs_cookies, clear_audio_cache, open_audio
from djtube.cookies import MAX_COOKIE_BYTES, CookieError, CookieStore, cookie_path, install_store
from djtube.ids import is_video_id
from djtube.live import LiveHub, live_seen_path, mount_live
from djtube.thumbs import thumb_cache
from djtube.paths import INDEX_PATH, PUBLIC_PREFIX, STATIC_DIR
from djtube.playlists import PlaylistError, PlaylistStore, playlist_path
from djtube.search import SearchError, search_mode, search_tracks

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


class MoveTrackBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    from_index: int = Field(alias="from")
    to_index: int = Field(alias="to")


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

    @app.get("/api/search")
    def search(q: str = Query(min_length=1, max_length=120), music: bool = True) -> dict[str, object]:
        try:
            tracks, source = search_tracks(q, music=music)
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
        return {"playlists": store.list_playlists()}

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

    @app.get("/")
    def index() -> HTMLResponse:
        return HTMLResponse(render_index(), headers={"Cache-Control": DOCUMENT_CACHE})

    app.mount("/static", VersionedStaticFiles(directory=str(STATIC_DIR)), name="static")
    app.add_middleware(StripPrefixMiddleware, prefix=PUBLIC_PREFIX)
    return app


app = create_app()
