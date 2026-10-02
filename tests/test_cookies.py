from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest
import yt_dlp
from fastapi.testclient import TestClient

from djtube.app import create_app
from djtube.audio import (
    AudioError,
    audio_needs_cookies,
    clear_audio_cache,
    clear_audio_causes,
    failure_needs_cookies,
    open_audio,
    resolve_audio,
)
from djtube.cookies import CookieError, CookieStore, cookie_path, current_store, install_store, normalize_netscape
from djtube.ids import watch_url
from djtube.paths import PUBLIC_PREFIX

VIDEO_ID = "abcdefghijk"
SECRET = "COOKIEVALUE9f3a7c"
REPLACEMENT = "COOKIEVALUE0b21e4"
MEDIA = "https://rr1---sn-example.googlevideo.com/videoplayback?id=1"


@pytest.fixture(autouse=True)
def restore_cookie_store():
    previous = current_store()
    try:
        yield
    finally:
        install_store(previous)


def netscape(*pairs: tuple[str, str], domain: str = ".youtube.com", flag: str = "TRUE", secure: str = "TRUE") -> bytes:
    lines = ["# Netscape HTTP Cookie File"]
    for name, value in pairs:
        lines.append(f"{domain}\t{flag}\t/\t{secure}\t1893456000\t{name}\t{value}")
    return ("\n".join(lines) + "\n").encode()


def _fmt():
    return {
        "url": MEDIA,
        "ext": "m4a",
        "protocol": "https",
        "vcodec": "none",
        "acodec": "mp4a.40.2",
        "abr": 128,
    }


def test_cookie_path_sits_on_the_playlist_data_dir(monkeypatch):
    monkeypatch.delenv("DJTUBE_COOKIES", raising=False)
    path = cookie_path()
    assert path.name == "cookies.txt"
    assert path.parent.name == "data"
    monkeypatch.setenv("DJTUBE_COOKIES", "/app/data/cookies.txt")
    assert cookie_path() == Path("/app/data/cookies.txt")


def test_normalize_keeps_youtube_cookies_and_drops_bad_lines():
    secret = "dropped-secret-value"
    raw = (
        "# Netscape HTTP Cookie File\n"
        f".youtube.com\tTRUE\t/\tTRUE\t1893456000\tSID\t{SECRET}\n"
        f".youtube.com\tFALSE\t/\tTRUE\t1893456000\tBAD\t{secret}\n"
        ".example.com\tTRUE\t/\tTRUE\t1893456000\tOTHER\tkeep-me\n"
        "#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t0\tLOGIN_INFO\tok\n"
    )
    text = normalize_netscape(raw.encode())
    assert text.startswith("# Netscape HTTP Cookie File\n")
    assert SECRET in text
    assert "LOGIN_INFO" in text
    assert secret not in text
    assert "example.com" in text
    assert normalize_netscape(text.encode()) == text

    with pytest.raises(CookieError, match="選んでください"):
        normalize_netscape(b"")
    with pytest.raises(CookieError, match="Netscape"):
        normalize_netscape(b'[{"name":"SID","value":"nope"}]')
    with pytest.raises(CookieError, match="YouTube"):
        normalize_netscape(netscape(("SID", SECRET), domain=".example.com"))
    with pytest.raises(CookieError, match="大きすぎます"):
        normalize_netscape(b"x" * (1024 * 1024 + 1))
    wide = netscape(("SID", "v" * (256 * 1024)))
    assert len(wide) > 256 * 1024
    assert len(wide) <= 1024 * 1024
    assert "SID" in normalize_netscape(wide)


def test_youtube_export_over_256_kib_uploads(tmp_path, caplog):
    value = "v" * 1500
    names = ["LOGIN_INFO"] + [f"N{index}" for index in range(179)]
    raw = netscape(*[(name, value) for name in names])
    assert 256 * 1024 < len(raw) <= 1024 * 1024
    assert raw.count(b".youtube.com\t") == 180

    store = CookieStore(tmp_path / "cookies.txt")
    client = TestClient(create_app(cookies=store))
    caplog.set_level(logging.DEBUG)
    uploaded = client.post("/api/cookies", files={"file": ("cookies.txt", raw, "text/plain")})
    assert uploaded.status_code == 200
    assert uploaded.json() == {"present": True}
    assert value not in uploaded.text
    assert value not in caplog.text
    saved = store.path.read_text(encoding="utf-8")
    assert saved.count(".youtube.com\t") == 180
    assert value in saved

    rejected = client.post(
        "/api/cookies",
        files={"file": ("big.txt", b"x" * (1024 * 1024 + 1), "text/plain")},
    )
    assert rejected.status_code == 400
    assert rejected.json()["detail"] == "Cookie ファイルが大きすぎます"
    assert value not in rejected.text
    assert store.path.read_text(encoding="utf-8") == saved


def test_empty_store_starts_and_ytdlp_gets_no_cookiefile(tmp_path, monkeypatch):
    store = CookieStore(tmp_path / "cookies.txt")
    install_store(store)
    client = TestClient(create_app(cookies=store))
    assert store.path.exists() is False
    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json()["ok"] is True
    status = client.get("/api/cookies")
    assert status.status_code == 200
    assert status.json() == {"present": False}
    assert client.get(f"{PUBLIC_PREFIX}/api/cookies").json() == {"present": False}

    seen = {}

    class Wrapper(yt_dlp.YoutubeDL):
        def extract_info(self, url, download=False):
            assert url == watch_url(VIDEO_ID)
            assert download is False
            seen["cookiefile"] = self.params.get("cookiefile")
            seen["quiet"] = self.params.get("quiet")
            seen["logger"] = "logger" in self.params
            seen["extractor_args"] = self.params.get("extractor_args")
            return {"formats": [_fmt()]}

    monkeypatch.setattr(yt_dlp, "YoutubeDL", Wrapper)
    clear_audio_cache()
    source = resolve_audio(VIDEO_ID)
    assert source.url == MEDIA
    assert seen["cookiefile"] is None
    assert seen["quiet"] is True
    assert seen["logger"] is False
    assert seen["extractor_args"] in (None, {})


def test_upload_persists_and_ytdlp_uses_it(tmp_path, monkeypatch, caplog, capsys):
    store = CookieStore(tmp_path / "cookies.txt")
    client = TestClient(create_app(cookies=store))
    seen = {}

    class Wrapper(yt_dlp.YoutubeDL):
        def extract_info(self, url, download=False):
            seen["cookiefile"] = self.params.get("cookiefile")
            seen["player_client"] = (self.params.get("extractor_args") or {}).get("youtube", {}).get("player_client")
            seen["header"] = self.cookiejar.get_cookie_header(f"https://www.youtube.com/watch?v={VIDEO_ID}")
            if seen["cookiefile"]:
                seen["passed"] = Path(seen["cookiefile"]).read_text(encoding="utf-8")
            return {"formats": [_fmt()]}

    monkeypatch.setattr(yt_dlp, "YoutubeDL", Wrapper)
    caplog.set_level(logging.DEBUG)
    uploaded = client.post(
        "/api/cookies",
        files={"file": ("cookies.txt", netscape(("SID", SECRET), ("SAPISID", "second")), "text/plain")},
    )
    assert uploaded.status_code == 200
    assert uploaded.json() == {"present": True}
    assert SECRET not in uploaded.text
    assert SECRET not in caplog.text
    saved = store.path.read_text(encoding="utf-8")
    assert SECRET in saved
    assert oct(store.path.stat().st_mode & 0o777) == "0o600"

    restarted = CookieStore(store.path)
    assert restarted.present() is True
    assert SECRET in restarted.path.read_text(encoding="utf-8")

    clear_audio_cache()
    resolve_audio(VIDEO_ID)
    assert seen["cookiefile"]
    assert seen["player_client"] == ["web_embedded", "web_safari"]
    assert "tv_downgraded" not in seen["player_client"]
    assert SECRET not in str(seen["player_client"])
    assert SECRET in seen["passed"]
    assert f"SID={SECRET}" in seen["header"]
    assert not Path(seen["cookiefile"]).exists()
    captured = capsys.readouterr()
    blob = caplog.text + captured.out + captured.err
    assert SECRET not in blob
    assert "SAPISID" not in captured.out + captured.err

    replaced = client.post(
        f"{PUBLIC_PREFIX}/api/cookies",
        files={"file": ("fresh.txt", netscape(("SID", REPLACEMENT)), "text/plain")},
    )
    assert replaced.status_code == 200
    assert replaced.json() == {"present": True}
    assert SECRET not in store.path.read_text(encoding="utf-8")
    assert REPLACEMENT in store.path.read_text(encoding="utf-8")
    seen.clear()
    resolve_audio(VIDEO_ID)
    assert seen["player_client"] == ["web_embedded", "web_safari"]
    assert f"SID={REPLACEMENT}" in seen["header"]
    assert SECRET not in (seen.get("header") or "")
    assert SECRET not in (seen.get("passed") or "")

    rejected = client.post("/api/cookies", files={"file": ("bad.txt", b"not cookies", "text/plain")})
    assert rejected.status_code == 400
    assert "Netscape" in rejected.json()["detail"]
    assert REPLACEMENT not in rejected.text
    assert SECRET not in rejected.text
    assert REPLACEMENT in store.path.read_text(encoding="utf-8")
    assert client.get("/api/cookies").json() == {"present": True}


def test_player_clients_follow_the_cookie_file_and_skip_tv_downgraded(tmp_path, monkeypatch):
    deno = tmp_path / "deno"
    deno.write_bytes(b"")
    monkeypatch.setattr("djtube.audio._DENO", deno)
    store = CookieStore(tmp_path / "cookies.txt")
    install_store(store)
    seen = {}

    class Wrapper(yt_dlp.YoutubeDL):
        def __init__(self, params=None, auto_init=True):
            seen["options"] = dict(params or {})
            super().__init__(params, auto_init=False)

        def extract_info(self, url, download=False):
            assert download is False
            return {"formats": [_fmt()]}

    monkeypatch.setattr(yt_dlp, "YoutubeDL", Wrapper)
    clear_audio_cache()
    resolve_audio(VIDEO_ID)
    assert "cookiefile" not in seen["options"]
    assert "extractor_args" not in seen["options"]
    assert "js_runtimes" not in seen["options"]

    marker = "not-a-session"
    store.replace(netscape(("LOGIN_INFO", marker), ("SAPISID", "also-not")))
    clear_audio_cache()
    seen.clear()
    resolve_audio(VIDEO_ID)
    options = seen["options"]
    assert options["extractor_args"]["youtube"]["player_client"] == ["web_embedded", "web_safari"]
    assert "tv_downgraded" not in options["extractor_args"]["youtube"]["player_client"]
    assert options["js_runtimes"] == {"deno": {"path": str(deno)}}
    assert marker not in str(options["extractor_args"])
    assert marker not in str(options["js_runtimes"])
    assert "also-not" not in str(options["extractor_args"])


def test_this_ytdlp_drops_tv_downgraded_only_for_the_cookie_options(tmp_path):
    """Logged-in yt-dlp defaults to tv_downgraded. Cookie options replace that list."""
    cookie = tmp_path / "cookies.txt"
    cookie.write_bytes(netscape(("LOGIN_INFO", "not-a-session"), ("SAPISID", "also-not")))
    watched = "https://www.youtube.com/watch?v=abcdefghijk"

    def clients_for(options, *, authenticated):
        ydl = yt_dlp.YoutubeDL(options)
        ie = ydl.get_info_extractor("Youtube")
        ie.set_downloader(ydl)
        ie.initialize()
        original = type(ie).is_authenticated
        type(ie).is_authenticated = property(lambda self: authenticated)
        try:
            return ie._get_requested_clients(watched, {}, False)
        finally:
            type(ie).is_authenticated = original

    from djtube.audio import _ytdlp_options

    anonymous = _ytdlp_options(None)
    assert "cookiefile" not in anonymous
    assert "extractor_args" not in anonymous
    assert "js_runtimes" not in anonymous

    bare = {"quiet": True, "no_warnings": True, "skip_download": True}
    assert clients_for(bare, authenticated=True) == ["web_embedded", "tv_downgraded", "web"]

    logged_in = _ytdlp_options(str(cookie))
    assert logged_in["cookiefile"] == str(cookie)
    assert clients_for(logged_in, authenticated=True) == ["web_embedded", "web_safari"]
    assert "not-a-session" not in str(logged_in["extractor_args"])


def post_paste(client: TestClient, text: str, path: str = "/api/cookies"):
    return client.post(path, files={"text": (None, text)})


def test_paste_persists_and_ytdlp_uses_it(tmp_path, monkeypatch, caplog, capsys):
    store = CookieStore(tmp_path / "cookies.txt")
    client = TestClient(create_app(cookies=store))
    seen = {}

    class Wrapper(yt_dlp.YoutubeDL):
        def extract_info(self, url, download=False):
            seen["cookiefile"] = self.params.get("cookiefile")
            seen["player_client"] = (self.params.get("extractor_args") or {}).get("youtube", {}).get("player_client")
            seen["header"] = self.cookiejar.get_cookie_header(f"https://www.youtube.com/watch?v={VIDEO_ID}")
            if seen["cookiefile"]:
                seen["passed"] = Path(seen["cookiefile"]).read_text(encoding="utf-8")
            return {"formats": [_fmt()]}

    monkeypatch.setattr(yt_dlp, "YoutubeDL", Wrapper)
    caplog.set_level(logging.DEBUG)
    special = "a+b=c%20d&e"
    dropped = "dropped-secret-value"
    text = (
        "# Netscape HTTP Cookie File\n"
        f".youtube.com\tTRUE\t/\tTRUE\t1893456000\tSID\t{SECRET}\n"
        "#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t0\tLOGIN_INFO\tok\n"
        ".example.com\tTRUE\t/\tTRUE\t1893456000\tOTHER\tkeep-me\n"
        "not a cookie line\n"
        f".youtube.com\tFALSE\t/\tTRUE\t1893456000\tBAD\t{dropped}\n"
        f".youtube.com\tTRUE\t/\tTRUE\t1893456000\tSAPISID\t{special}\n"
    )
    pasted = post_paste(client, text)
    assert pasted.status_code == 200
    assert pasted.json() == {"present": True}
    assert set(pasted.json()) == {"present"}
    for secret in (SECRET, special, dropped, "keep-me"):
        assert secret not in pasted.text
        assert secret not in caplog.text
    saved = store.path.read_text(encoding="utf-8")
    assert saved == normalize_netscape(text.encode())
    assert SECRET in saved
    assert special in saved
    assert "LOGIN_INFO" in saved
    assert "example.com" in saved
    assert "keep-me" in saved
    assert dropped not in saved
    assert "not a cookie line" not in saved
    assert oct(store.path.stat().st_mode & 0o777) == "0o600"

    restarted = CookieStore(store.path)
    assert restarted.present() is True
    assert restarted.path.read_text(encoding="utf-8") == saved

    clear_audio_cache()
    resolve_audio(VIDEO_ID)
    assert seen["cookiefile"]
    assert seen["player_client"] == ["web_embedded", "web_safari"]
    assert "tv_downgraded" not in seen["player_client"]
    assert SECRET not in str(seen["player_client"])
    assert SECRET in seen["passed"]
    assert special in seen["passed"]
    assert f"SID={SECRET}" in seen["header"]
    assert f"SAPISID={special}" in seen["header"]
    assert not Path(seen["cookiefile"]).exists()
    captured = capsys.readouterr()
    blob = caplog.text + captured.out + captured.err
    assert SECRET not in blob
    assert special not in blob
    assert "SAPISID" not in captured.out + captured.err

    replaced = post_paste(client, netscape(("SID", REPLACEMENT)).decode(), f"{PUBLIC_PREFIX}/api/cookies")
    assert replaced.status_code == 200
    assert replaced.json() == {"present": True}
    assert SECRET not in replaced.text
    assert REPLACEMENT not in replaced.text
    assert SECRET not in store.path.read_text(encoding="utf-8")
    assert REPLACEMENT in store.path.read_text(encoding="utf-8")
    seen.clear()
    resolve_audio(VIDEO_ID)
    assert seen["player_client"] == ["web_embedded", "web_safari"]
    assert f"SID={REPLACEMENT}" in seen["header"]
    assert SECRET not in (seen.get("header") or "")
    assert SECRET not in (seen.get("passed") or "")

    rejected = post_paste(client, "not cookies")
    assert rejected.status_code == 400
    assert "Netscape" in rejected.json()["detail"]
    no_youtube = post_paste(client, netscape(("SID", SECRET), domain=".example.com").decode())
    assert no_youtube.status_code == 400
    assert "YouTube" in no_youtube.json()["detail"]
    empty = post_paste(client, "   \n")
    assert empty.status_code == 400
    assert "選んでください" in empty.json()["detail"]
    oversized = client.post(
        "/api/cookies",
        files={"file": ("big.txt", b"x" * (1024 * 1024 + 1), "text/plain")},
    )
    assert oversized.status_code == 400
    assert "大きすぎます" in oversized.json()["detail"]
    for response in (rejected, no_youtube, empty, oversized):
        assert REPLACEMENT not in response.text
        assert SECRET not in response.text
    assert REPLACEMENT in store.path.read_text(encoding="utf-8")
    assert client.get("/api/cookies").json() == {"present": True}
    assert SECRET not in client.get("/api/cookies").text
    assert REPLACEMENT not in client.get("/api/cookies").text

    uploaded = client.post(
        "/api/cookies",
        files={"file": ("cookies.txt", netscape(("SID", SECRET)), "text/plain")},
    )
    assert uploaded.status_code == 200
    assert uploaded.json() == {"present": True}
    assert REPLACEMENT not in store.path.read_text(encoding="utf-8")
    assert SECRET in store.path.read_text(encoding="utf-8")
    seen.clear()
    resolve_audio(VIDEO_ID)
    assert f"SID={SECRET}" in seen["header"]
    assert REPLACEMENT not in (seen.get("passed") or "")


def test_cookie_post_without_a_file_or_text_is_rejected(tmp_path):
    store = CookieStore(tmp_path / "cookies.txt")
    client = TestClient(create_app(cookies=store))
    rejected = client.post("/api/cookies")
    assert rejected.status_code == 400
    assert "選んでください" in rejected.json()["detail"]
    assert store.path.exists() is False
    assert client.get("/api/cookies").json() == {"present": False}


def test_index_has_export_steps_and_upload(tmp_path):
    client = TestClient(create_app(cookies=CookieStore(tmp_path / "cookies.txt")))
    html = client.get("/djtube/").text
    assert "ブラウザで YouTube を開き、サインインする。" in html
    assert "Netscape 形式のファイルに書き出す" in html
    assert "Get cookies.txt LOCALLY" in html
    assert "ここで差し替える" in html
    assert 'id="cookie-panel" aria-label="Cookie" hidden' in html
    header = html.split('id="cookie-panel"', 1)[0]
    assert 'id="cookie-open"' in header
    assert 'aria-controls="cookie-panel"' in header
    assert 'aria-expanded="false"' in header
    assert 'aria-label="Cookie"' in header
    opener = header.split('id="cookie-open"', 1)[1].split("</button>", 1)[0]
    assert "<svg" in opener
    assert 'fill-rule="evenodd"' in opener
    assert ">Cookie</button>" not in header
    css = (Path(__file__).resolve().parents[1] / "djtube" / "static" / "app.css").read_text(encoding="utf-8")
    cookie_button = css.split(".cookie-open {", 1)[1].split("}", 1)[0]
    assert "border-radius: 50%" in cookie_button
    assert "width: 36px" in cookie_button
    assert "height: 36px" in cookie_button
    assert ".cookie-panel:not([hidden])" in css
    assert "display: flex" in css.split(".cookie-panel:not([hidden])", 1)[1].split("}", 1)[0]
    assert 'id="cookie-file"' in html
    assert 'type="file"' in html
    assert 'id="cookie-upload"' in html
    assert ">アップロード<" in html
    assert 'action="/djtube/api/cookies"' in html
    panel = html.split('id="cookie-panel"', 1)[1].split("</section>", 1)[0]
    assert 'id="cookie-text"' in panel
    assert 'id="cookie-paste"' in panel
    assert 'name="text"' in panel
    assert 'id="cookie-save"' in panel
    assert ">保存<" in panel
    assert "中身を貼り付け" in panel
    assert "音源を再生できませんでした" not in html
    js = client.get("/djtube/static/cookies.js")
    assert js.status_code == 200
    assert "/api/cookies" in js.text
    assert 'getElementById("cookie-text")' in js.text
    assert 'getElementById("cookie-open")' in js.text
    assert "cookiePanelShown" in js.text
    assert "nextChosenOpen" in js.text
    assert "まだありません" in js.text
    assert 'append("text"' in js.text
    assert 'append("file"' in js.text
    assert "Cookie を貼り付けてください" in js.text
    assert "Cookie のファイルを選んでください" in js.text
    search_source = (Path(__file__).resolve().parents[1] / "djtube" / "search.py").read_text(encoding="utf-8")
    assert "cookiefile" not in search_source
    assert "player_client" not in search_source
    assert "js_runtimes" not in search_source
    root = Path(__file__).resolve().parents[1]
    client_js = "\n".join(
        (root / "djtube" / "static" / name).read_text(encoding="utf-8")
        for name in ("app.js", "cookies.js", "player.js")
    )
    assert "Sign in to confirm" not in client_js
    assert "new RegExp" not in client_js
    assert "/api/audio/${encodeURIComponent(videoId)}/cause" in (root / "djtube" / "static" / "app.js").read_text(
        encoding="utf-8"
    )
    assert "deckState.error = SOURCE_UNAVAILABLE" in (root / "djtube" / "static" / "app.js").read_text(encoding="utf-8")


class CookieLoadError(Exception):
    pass


def test_server_marks_only_auth_and_cookie_failures():
    bot = RuntimeError("ERROR: [youtube] abcdefghijk: Sign in to confirm you're not a bot. Use --cookies")
    assert failure_needs_cookies(bot) is True

    class Wrapped(Exception):
        def __init__(self):
            super().__init__("ERROR: failed")
            self.exc_info = (None, bot, None)

    assert failure_needs_cookies(Wrapped()) is True
    assert failure_needs_cookies(CookieLoadError("could not read the file")) is True
    assert failure_needs_cookies(RuntimeError("Sign in to confirm your age")) is True
    for message in (
        "ERROR: [youtube] abcdefghijk: Video unavailable",
        "This content isn't available, try again later. The current session has been rate-limited by YouTube",
        "The uploader has not made this video available in your country",
        "HTTP Error 403: Forbidden",
        "Unable to download webpage: The read operation timed out",
    ):
        assert failure_needs_cookies(RuntimeError(message)) is False


def test_cause_is_a_flag_and_the_screen_message_stays(tmp_path, monkeypatch):
    store = CookieStore(tmp_path / "cookies.txt")
    client = TestClient(create_app(cookies=store))
    clear_audio_cache()
    clear_audio_causes()
    assert client.get(f"/api/audio/{VIDEO_ID}/cause").json() == {"cookies": False}
    assert client.get("/api/audio/short/cause").status_code == 404

    messages = {"text": ""}

    class Wrapper(yt_dlp.YoutubeDL):
        def extract_info(self, url, download=False):
            raise RuntimeError(messages["text"])

    monkeypatch.setattr(yt_dlp, "YoutubeDL", Wrapper)
    records = []

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    logger = logging.getLogger("djtube.audio")
    handler = Capture()
    logger.addHandler(handler)
    try:
        messages["text"] = (
            "ERROR: [youtube] abcdefghijk: Sign in to confirm you're not a bot. "
            f"Use --cookies-from-browser or --cookies Cookie: VISITOR={SECRET}"
        )
        failed = client.get(f"/api/audio/{VIDEO_ID}")
        assert failed.status_code == 502
        assert failed.json()["detail"] == "音源を取得できませんでした"
        assert SECRET not in failed.text
        assert "Sign in" not in failed.text
        cause = client.get(f"/djtube/api/audio/{VIDEO_ID}/cause")
        assert cause.json() == {"cookies": True}
        assert set(cause.json()) == {"cookies"}
        assert SECRET not in cause.text
        assert "Sign in" not in cause.text
        assert "bot" not in cause.text
        assert f"video={VIDEO_ID} path=ytdlp failure {messages['text']}" in "\n".join(records)
        assert "cookies=" not in "\n".join(records)

        records.clear()
        clear_audio_cache()
        messages["text"] = "ERROR: [youtube] abcdefghijk: Video unavailable"
        other = client.get(f"/api/audio/{VIDEO_ID}")
        assert other.status_code == 502
        assert other.json()["detail"] == "音源を取得できませんでした"
        assert client.get(f"/api/audio/{VIDEO_ID}/cause").json() == {"cookies": False}
        assert f"path=ytdlp failure {messages['text']}" in "\n".join(records)
    finally:
        logger.removeHandler(handler)


def test_upstream_failure_does_not_ask_for_cookies(monkeypatch):
    clear_audio_cache()
    clear_audio_causes()

    class Wrapper(yt_dlp.YoutubeDL):
        def extract_info(self, url, download=False):
            return {"formats": [_fmt()]}

    monkeypatch.setattr(yt_dlp, "YoutubeDL", Wrapper)

    class Denied:
        status_code = 403
        headers = {}

        def close(self):
            return None

    monkeypatch.setattr("djtube.audio._send", lambda client, url, headers: Denied())
    with pytest.raises(AudioError):
        open_audio(VIDEO_ID, None)
    assert audio_needs_cookies(VIDEO_ID) is False
