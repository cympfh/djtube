from __future__ import annotations

import mimetypes

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from djtube.paths import INDEX_PATH, PUBLIC_PREFIX, STATIC_DIR
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
    return html.replace("__PUBLIC_PREFIX__", PUBLIC_PREFIX)


def create_app() -> FastAPI:
    app = FastAPI(title="djtube")

    @app.get("/api/health")
    def health() -> dict[str, str | bool]:
        return {
            "ok": True,
            "search": search_mode(),
            "prefix": PUBLIC_PREFIX,
            "flx4": "unmapped",
            "playback": "youtube-iframe",
        }

    @app.get("/api/search")
    def search(q: str = Query(min_length=1, max_length=120), music: bool = True) -> dict[str, object]:
        try:
            tracks, source = search_tracks(q, music=music)
        except SearchError as exc:
            raise HTTPException(400 if str(exc) == "検索語を入れてください" else 502, str(exc)) from None
        return {"source": source, "tracks": [track.as_dict() for track in tracks]}

    @app.get("/")
    def index() -> HTMLResponse:
        return HTMLResponse(render_index())

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.add_middleware(StripPrefixMiddleware, prefix=PUBLIC_PREFIX)
    return app


app = create_app()
