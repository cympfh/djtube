"""Netscape cookies saved from the page, for yt-dlp audio only.

The file starts empty. Nothing here is logged except the path and a fixed message.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
import threading
from pathlib import Path

from djtube.paths import PACKAGE_DIR

log = logging.getLogger(__name__)

MAX_COOKIE_BYTES = 1024 * 1024
_EXPIRES = re.compile(r"[0-9]+(?:\.[0-9]+)?")
_HTTPONLY = "#HttpOnly_"
_HEADER = "# Netscape HTTP Cookie File"


class CookieError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def cookie_path() -> Path:
    raw = os.environ.get("DJTUBE_COOKIES", "").strip()
    if raw:
        return Path(raw).expanduser()
    return PACKAGE_DIR.parent / "data" / "cookies.txt"


def _youtube_domain(domain: str) -> bool:
    host = domain.strip().lstrip(".").lower().rstrip(".")
    return host == "youtube.com" or host.endswith(".youtube.com")


def _keep_line(line: str) -> str | None:
    prefixed = line.startswith(_HTTPONLY)
    body = line[len(_HTTPONLY) :] if prefixed else line
    if body.startswith("#") or "\t" not in body:
        return None
    parts = body.split("\t")
    if len(parts) != 7:
        return None
    domain, flag, path, secure, expires, name, _value = parts
    if flag not in {"TRUE", "FALSE"} or secure not in {"TRUE", "FALSE"}:
        return None
    if not path.startswith("/") or not name:
        return None
    if expires and _EXPIRES.fullmatch(expires) is None:
        return None
    if domain.startswith(".") != (flag == "TRUE"):
        return None
    if any(char in domain or char in name for char in "\r\n"):
        return None
    kept = f"{_HTTPONLY}{body}" if prefixed else body
    return kept


def normalize_netscape(data: bytes) -> str:
    if not data or not data.strip():
        raise CookieError("Cookie のファイルを選んでください")
    if len(data) > MAX_COOKIE_BYTES:
        raise CookieError("Cookie ファイルが大きすぎます")
    if b"\x00" in data:
        raise CookieError("Cookie のファイルを読めません")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise CookieError("Cookie のファイルを読めません") from None

    kept: list[str] = []
    saw_tab = False
    youtube = False
    for raw in text.splitlines():
        if "\t" in raw:
            saw_tab = True
        line = _keep_line(raw)
        if line is None:
            continue
        domain = line[len(_HTTPONLY) :] if line.startswith(_HTTPONLY) else line
        domain = domain.split("\t", 1)[0]
        value = line.split("\t")[-1]
        if _youtube_domain(domain) and value:
            youtube = True
        kept.append(line)
    if not youtube:
        if saw_tab:
            raise CookieError("YouTube の Cookie が入っていません")
        raise CookieError("Netscape 形式の Cookie ファイルを選んでください")
    return _HEADER + "\n" + "\n".join(kept) + "\n"


class CookieStore:
    """Durable Netscape file. A failed write stays in memory for this process."""

    def __init__(self, path: Path | None = None):
        self.path = cookie_path() if path is None else path
        self.durable = True
        self._lock = threading.Lock()
        self._memory: str | None = None

    def present(self) -> bool:
        with self._lock:
            return self._text_locked() is not None

    def replace(self, data: bytes) -> None:
        text = normalize_netscape(data)
        with self._lock:
            try:
                self._write(text)
            except OSError:
                log.warning("could not write cookies to %s; keeping them in memory", self.path)
                self._memory = text
                self.durable = False
                return
            self._memory = None
            self.durable = True
            log.info("stored cookies")

    def materialize(self) -> Path | None:
        with self._lock:
            text = self._text_locked()
        if not text:
            return None
        fd, name = tempfile.mkstemp(prefix="djtube-cookies-", suffix=".txt")
        try:
            os.write(fd, text.encode("utf-8"))
            os.fchmod(fd, 0o600)
        except OSError:
            os.close(fd)
            os.unlink(name)
            raise
        os.close(fd)
        return Path(name)

    def _text_locked(self) -> str | None:
        if not self.durable and self._memory:
            return self._memory
        if not self.path.is_file():
            return None
        try:
            data = self.path.read_bytes()
        except OSError:
            log.warning("could not read cookies from %s", self.path)
            return None
        if not data.strip():
            return None
        try:
            return normalize_netscape(data)
        except CookieError:
            log.warning("cookie file is not usable: %s", self.path)
            return None

    def _write(self, text: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(text, encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.path)
        os.chmod(self.path, 0o600)


_current: CookieStore | None = None


def install_store(store: CookieStore) -> None:
    global _current
    _current = store


def current_store() -> CookieStore:
    global _current
    if _current is None:
        _current = CookieStore(cookie_path())
    return _current
