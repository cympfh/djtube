"""One build token for the page and every ES module it loads.

Static files are served with no cache lifetime of their own. Browsers then
guess freshness from Last-Modified, per file. A reload can reuse an older
actions.js while fetching a newer app.js. The page names one token, and every
relative module import in that response uses the same token, so the loader
cannot mix those URLs.
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from urllib.parse import parse_qs

from starlette.datastructures import Headers
from starlette.responses import FileResponse, Response
from starlette.staticfiles import NotModifiedResponse, StaticFiles

from djtube.paths import INDEX_PATH, STATIC_DIR

# Revalidate before reuse. A reload of the document picks up the current token.
DOCUMENT_CACHE = "no-cache"
# This URL's query token is the build. The bytes will not change under that URL.
VERSIONED_CACHE = "public, max-age=31536000, immutable"

_FROM_SPECIFIER = re.compile(
    r"""(?P<prefix>\bfrom\s+|import\s*\(\s*)(?P<quote>["'])(?P<spec>\./[A-Za-z0-9_./-]+\.js)(?P=quote)"""
)
_SIDE_EFFECT_IMPORT = re.compile(
    r"""(?m)^(?P<prefix>[ \t]*import\s+)(?P<quote>["'])(?P<spec>\./[A-Za-z0-9_./-]+\.js)(?P=quote)"""
)

_version_cache: tuple[tuple[tuple[str, int, int], ...], str] | None = None


def asset_version(static_dir: Path = STATIC_DIR, index_path: Path = INDEX_PATH) -> str:
    """Hash the index and every static file. One tree, one token."""
    global _version_cache
    files = [index_path, *sorted(path for path in static_dir.rglob("*") if path.is_file())]
    root = static_dir.parent
    fingerprint = tuple(
        (path.relative_to(root).as_posix(), path.stat().st_mtime_ns, path.stat().st_size) for path in files
    )
    if _version_cache is not None and _version_cache[0] == fingerprint:
        return _version_cache[1]
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    version = digest.hexdigest()[:16]
    _version_cache = (fingerprint, version)
    return version


def stamp_module_specifiers(source: str, version: str) -> str:
    """Point relative .js imports at this build. Other text stays put."""

    def repl(match: re.Match[str]) -> str:
        return f"{match.group('prefix')}{match.group('quote')}{match.group('spec')}?v={version}{match.group('quote')}"

    return _SIDE_EFFECT_IMPORT.sub(repl, _FROM_SPECIFIER.sub(repl, source))


def stamp_document(html: str, version: str) -> str:
    """The stylesheet and the entry module carry the same token as their imports."""

    def repl(match: re.Match[str]) -> str:
        return f"{match.group(1)}?v={version}{match.group(2)}"

    return re.sub(
        r'((?:href|src)="[^"]*/static/[^"?]+\.(?:css|js))(")',
        repl,
        html,
    )


def requested_version(scope: dict) -> str | None:
    raw = scope.get("query_string", b"")
    if isinstance(raw, str):
        raw = raw.encode()
    values = parse_qs(raw.decode("ascii", "ignore")).get("v")
    if not values:
        return None
    return values[0]


class VersionedStaticFiles(StaticFiles):
    """Serve static files, and stamp JS imports with the current build token."""

    def file_response(
        self,
        full_path: os.PathLike[str],
        stat_result: os.stat_result,
        scope: dict,
        status_code: int = 200,
    ) -> Response:
        version = asset_version()
        cache = VERSIONED_CACHE if requested_version(scope) == version else DOCUMENT_CACHE
        if str(full_path).endswith(".js"):
            return self._javascript(full_path, scope, version, cache, status_code)
        response = FileResponse(full_path, status_code=status_code, stat_result=stat_result)
        response.headers["Cache-Control"] = cache
        if self.is_not_modified(response.headers, Headers(scope=scope)):
            return NotModifiedResponse(response.headers)
        return response

    def _javascript(
        self,
        full_path: os.PathLike[str],
        scope: dict,
        version: str,
        cache: str,
        status_code: int,
    ) -> Response:
        text = Path(full_path).read_text(encoding="utf-8")
        body = stamp_module_specifiers(text, version).encode("utf-8")
        etag = '"' + hashlib.sha256(version.encode() + b"\0" + body).hexdigest() + '"'
        headers = {"Cache-Control": cache, "ETag": etag}
        request_headers = Headers(scope=scope)
        if_none_match = request_headers.get("if-none-match")
        if if_none_match:
            tags = [tag.strip().removeprefix("W/") for tag in if_none_match.split(",")]
            if if_none_match.strip() == "*" or etag in tags:
                return NotModifiedResponse(Headers(headers))
        return Response(content=body, status_code=status_code, media_type="text/javascript", headers=headers)
