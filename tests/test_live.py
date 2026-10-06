from __future__ import annotations

import array
import asyncio
import math
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect, WebSocketState

from djtube.app import create_app
from djtube.assets import asset_version
from djtube.live import (
    CODE_BAD_ID,
    CODE_BAD_MEDIA,
    CODE_FULL,
    CODE_RATE,
    CODE_SLOW,
    CODE_TAKEN,
    CODE_TIMEOUT,
    CODE_TOO_BIG,
    BURST_SECONDS,
    IDLE_TIMEOUT,
    INIT_TIMEOUT,
    MAX_BYTES_PER_SECOND,
    MAX_CLUSTER,
    MAX_FRAME,
    MAX_PER_IP,
    MAX_STREAMS,
    LiveHub,
    _Listener,
    is_stream_id,
)
from djtube.paths import PUBLIC_PREFIX, STATIC_DIR
from djtube.webm import cluster_timecode, split_webm
from tests.test_webm import cluster_known, document

ROOT = Path(__file__).resolve().parents[1]


def _slices(data: bytes) -> list[bytes]:
    sizes = (7, 13, 64, 200, 480)
    pieces = []
    index = 0
    step = 0
    while index < len(data):
        size = sizes[step % len(sizes)]
        pieces.append(data[index : index + size])
        index += size
        step += 1
    return pieces


def _send(socket, data: bytes) -> None:
    for piece in _slices(data):
        socket.send_bytes(piece)


def _tone(seconds: list[tuple[float, float]] | None = None) -> bytes:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg is required to prove the joined audio plays")
    from djtube.live_tone import render_webm

    return render_webm(seconds or [(440, 2.0), (880, 2.0)])


def _decode(webm: bytes) -> array.array:
    result = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            "pipe:0",
            "-f",
            "s16le",
            "-ac",
            "1",
            "-ar",
            "48000",
            "pipe:1",
        ],
        input=webm,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode()[-400:]
    samples = array.array("h")
    samples.frombytes(result.stdout)
    return samples


def _dominant(samples: array.array, skip: float, take: float = 0.35, rate: int = 48000) -> float:
    """Whichever of 440 Hz and 880 Hz is louder in this window."""

    start = int(skip * rate)
    window = samples[start : start + int(take * rate)]
    assert len(window) > rate // 5

    def power(frequency: float) -> float:
        bins = len(window)
        omega = 2 * math.pi * int(0.5 + bins * frequency / rate) / bins
        coeff = 2 * math.cos(omega)
        first = second = 0.0
        for sample in window:
            first, second = sample + coeff * first - second, first
        return first * first + second * second - coeff * first * second

    low = power(440)
    high = power(880)
    return 880.0 if high > low else 440.0


def test_ids_are_four_uppercase_letters_and_collisions_are_rejected():
    app = create_app()
    with TestClient(app) as client:
        with client.websocket_connect("/api/live/publish") as first:
            first_id = first.receive_json()["id"]
            assert is_stream_id(first_id)
            with client.websocket_connect("/api/live/publish") as second:
                second_id = second.receive_json()["id"]
            assert is_stream_id(second_id)
            assert second_id != first_id
        with pytest.raises(WebSocketDisconnect) as bad:
            with client.websocket_connect("/api/live/publish?id=ab") as socket:
                socket.receive_json()
        assert bad.value.code == CODE_BAD_ID
        with client.websocket_connect("/djtube/api/live/publish?id=ABCD") as claimed:
            assert claimed.receive_json() == {"type": "id", "id": "ABCD"}
            with pytest.raises(WebSocketDisconnect) as taken:
                with client.websocket_connect("/api/live/publish?id=ABCD") as other:
                    other.receive_json()
            assert taken.value.code == CODE_TAKEN


def test_full_hub_and_full_listener_list_refuse_another_connection():
    hub = LiveHub(max_streams=1, max_listeners=1)
    with TestClient(create_app(live=hub)) as client:
        with client.websocket_connect("/api/live/publish") as publisher:
            stream_id = publisher.receive_json()["id"]
            with pytest.raises(WebSocketDisconnect) as full:
                with client.websocket_connect("/api/live/publish") as extra:
                    extra.receive_json()
            assert full.value.code == CODE_FULL
            with client.websocket_connect(f"/api/live/{stream_id}") as listener:
                assert listener.receive_json()["type"] == "start"
                with client.websocket_connect(f"/djtube/api/live/{stream_id}") as overflow:
                    assert overflow.receive_json()["type"] == "full"


def test_absent_id_is_obvious_on_the_socket_and_the_page():
    with TestClient(create_app()) as client:
        missing = client.get("/stream/ABCD")
        prefixed = client.get(f"{PUBLIC_PREFIX}/stream/ABCD")
        invalid = client.get("/stream/nope")
        assert missing.status_code == prefixed.status_code == 200
        assert missing.headers["cache-control"] == "no-cache"
        assert "この配信はありません" in missing.text
        assert "この配信はありません" in prefixed.text
        assert 'data-state="absent"' in missing.text
        assert ">ABCD</h1>" in missing.text
        assert "ID の形式が違います" in invalid.text
        assert 'data-state="invalid"' in invalid.text
        with client.websocket_connect("/api/live/ZZZZ") as socket:
            assert socket.receive_json() == {"type": "absent"}
        with client.websocket_connect("/api/live/publish?id=LIVE") as publisher:
            assert publisher.receive_json()["id"] == "LIVE"
            page = client.get("/stream/LIVE")
        assert 'data-state="live"' in page.text
        assert "接続しています" in page.text
        assert ">LIVE</h1>" in page.text
        version = asset_version()
        assert f'/static/stream.js?v={version}"' in page.text
        assert f'/static/stream.css?v={version}"' in page.text
        script = client.get(f"/static/stream.js?v={version}")
        assert script.status_code == 200
        assert '"./prefix.js?v=' in script.text
        assert "MediaSource" in script.text
        assert "sequence" in script.text
        gone = client.get("/stream/LIVE")
        assert "この配信はありません" in gone.text


def test_listener_page_does_not_pull_in_the_dj_graph():
    text = (STATIC_DIR / "stream.js").read_text(encoding="utf-8")
    assert "player.js" not in text
    assert "MediaRecorder" not in text
    assert "createMediaElementSource" not in text
    html = (ROOT / "djtube" / "templates" / "index.html").read_text(encoding="utf-8")
    assert "stream.js" not in html


def test_synthetic_relay_starts_late_at_the_newest_cluster_and_keeps_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    clusters = [cluster_known(index * 200, bytes([index + 1]) * 24) for index in range(6)]
    head, data = document(clusters)
    cut = 4
    prefix = head + b"".join(clusters[:cut])
    rest = b"".join(clusters[cut:])
    hub = LiveHub()
    with TestClient(create_app(live=hub)) as client:
        with client.websocket_connect("/api/live/publish?id=TONE") as publisher:
            assert publisher.receive_json()["id"] == "TONE"
            publisher.send_json({"type": "mime", "mime": "audio/webm; codecs=opus"})
            with client.websocket_connect("/api/live/TONE") as early:
                start = early.receive_json()
                assert start == {"type": "start", "id": "TONE", "mime": "audio/webm;codecs=opus"}
                _send(publisher, prefix)
                assert early.receive_bytes() == head
                assert [early.receive_bytes() for _ in range(cut)] == clusters[:cut]
                stream = hub.get("TONE")
                assert stream is not None
                assert stream.latest == clusters[cut - 1]
                assert stream.splitter.buffered < len(data) / 2
                with client.websocket_connect("/api/live/TONE") as late:
                    assert late.receive_json()["type"] == "start"
                    assert late.receive_bytes() == head
                    assert late.receive_bytes() == clusters[cut - 1]
                    _send(publisher, rest)
            # publisher and early close as the blocks exit; late is still open until here
        assert hub.get("TONE") is None
    assert list(tmp_path.iterdir()) == []


def test_end_and_garbage(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with TestClient(create_app()) as client:
        with client.websocket_connect("/api/live/publish?id=ENDD") as publisher:
            publisher.receive_json()
            with client.websocket_connect("/api/live/ENDD") as listener:
                assert listener.receive_json()["type"] == "start"
                publisher.close()
                assert listener.receive_json()["type"] == "end"
        with client.websocket_connect("/api/live/publish?id=BADM") as publisher:
            publisher.receive_json()
            with client.websocket_connect("/api/live/BADM") as listener:
                assert listener.receive_json()["type"] == "start"
                publisher.send_bytes(b"this is not webm audio")
                assert listener.receive_json()["type"] == "end"
            with pytest.raises(WebSocketDisconnect) as bad:
                publisher.receive_json()
            assert bad.value.code == CODE_BAD_MEDIA
        with client.websocket_connect("/api/live/publish?id=MIME") as publisher:
            publisher.receive_json()
            publisher.send_json({"type": "mime", "mime": "text/html"})
            with pytest.raises(WebSocketDisconnect) as bad_mime:
                publisher.receive_json()
            assert bad_mime.value.code == CODE_BAD_MEDIA
    assert list(tmp_path.iterdir()) == []


def test_a_slow_listener_is_disconnected_and_a_full_queue_drops_old_clusters():
    async def scenario():
        listener = _Listener(limit=4)
        listener.offer_init(b"init")
        for index in range(10):
            listener.offer_media(bytes([index]))
        assert listener.pending_media() == [bytes([index]) for index in range(6, 10)]
        assert listener.pending_kinds()[0] == "init"

        class Slow:
            application_state = WebSocketState.CONNECTED

            def __init__(self) -> None:
                self.code = None

            async def send_bytes(self, _data: bytes) -> None:
                await asyncio.sleep(5)

            async def send_json(self, _data: dict) -> None:
                return None

            async def receive(self) -> dict:
                await asyncio.sleep(5)
                return {"type": "websocket.disconnect"}

            async def close(self, code: int = 1000, reason: str | None = None) -> None:
                self.code = code
                self.application_state = WebSocketState.DISCONNECTED

        hub = LiveHub(send_timeout=0.05)
        waiting = _Listener(limit=4)
        waiting.offer_init(b"i")
        waiting.offer_media(b"m")
        socket = Slow()
        with pytest.raises(WebSocketDisconnect) as exc:
            await hub._pump(waiting, socket)  # type: ignore[arg-type]
        assert exc.value.code == CODE_SLOW
        assert socket.code == CODE_SLOW

    asyncio.run(scenario())


def test_relay_caps_match_the_publisher_budget():
    hub = LiveHub()
    assert hub.max_streams == MAX_STREAMS == 8
    assert hub.max_per_ip == MAX_PER_IP == 2
    assert hub.max_bytes_per_second == MAX_BYTES_PER_SECOND == 64 * 1024
    assert hub.burst_seconds == BURST_SECONDS == 8
    assert hub.max_bytes_per_second * hub.burst_seconds == 512 * 1024
    assert hub.max_cluster == MAX_CLUSTER == 256 * 1024
    assert hub.max_frame == MAX_FRAME == 256 * 1024
    assert hub.init_timeout == INIT_TIMEOUT == 10
    assert hub.idle_timeout == IDLE_TIMEOUT == 30
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert '--ws-max-size", "1048576"' in dockerfile


def test_a_silent_publisher_loses_its_slot_and_a_quiet_one_does_too():
    silent = LiveHub(max_streams=2, init_timeout=0.25, idle_timeout=5)
    with TestClient(create_app(live=silent)) as client:
        started = time.monotonic()
        with client.websocket_connect("/api/live/publish?id=AAAA") as first:
            first.receive_json()
            with client.websocket_connect("/api/live/publish?id=AAAB") as second:
                second.receive_json()
                with pytest.raises(WebSocketDisconnect) as full:
                    with client.websocket_connect("/api/live/publish?id=AAAC") as third:
                        third.receive_json()
                assert full.value.code == CODE_FULL
                with pytest.raises(WebSocketDisconnect) as quiet:
                    second.receive_json()
                assert quiet.value.code == CODE_TIMEOUT
            with pytest.raises(WebSocketDisconnect) as quiet_first:
                first.receive_json()
            assert quiet_first.value.code == CODE_TIMEOUT
        assert time.monotonic() - started < 2
        assert silent.get("AAAA") is None
        assert silent.get("AAAB") is None
        with client.websocket_connect("/api/live/publish?id=AAAC") as again:
            assert again.receive_json() == {"type": "id", "id": "AAAC"}

    cluster = cluster_known(0, b"wave")
    head, _data = document([cluster])
    stalled = LiveHub(init_timeout=2, idle_timeout=0.3)
    with TestClient(create_app(live=stalled)) as client:
        with client.websocket_connect("/api/live/publish?id=STOP") as publisher:
            publisher.receive_json()
            publisher.send_bytes(head + cluster)
            with client.websocket_connect("/api/live/STOP") as listener:
                assert listener.receive_json()["type"] == "start"
                assert listener.receive_bytes() == head
                assert listener.receive_bytes() == cluster
                with pytest.raises(WebSocketDisconnect) as quiet:
                    publisher.receive_json()
                assert quiet.value.code == CODE_TIMEOUT
                assert listener.receive_json() == {"type": "end"}
        assert stalled.get("STOP") is None


def test_mime_alone_does_not_postpone_the_init_deadline():
    hub = LiveHub(init_timeout=0.3, idle_timeout=5)
    with TestClient(create_app(live=hub)) as client:
        started = time.monotonic()
        with client.websocket_connect("/api/live/publish?id=MIME") as publisher:
            publisher.receive_json()
            publisher.send_json({"type": "mime", "mime": "audio/webm"})
            with pytest.raises(WebSocketDisconnect) as quiet:
                publisher.receive_json()
            assert quiet.value.code == CODE_TIMEOUT
        assert time.monotonic() - started < 2
        assert hub.get("MIME") is None


def test_small_garbage_a_large_frame_and_a_burst_close_for_different_reasons():
    small = LiveHub(max_bytes_per_second=1024)
    with TestClient(create_app(live=small)) as client:
        with client.websocket_connect("/api/live/publish?id=TINY") as publisher:
            publisher.receive_json()
            publisher.send_bytes(b"x" * 3000)
            with pytest.raises(WebSocketDisconnect) as bad:
                publisher.receive_json()
            assert bad.value.code == CODE_BAD_MEDIA
        assert small.get("TINY") is None

    framed = LiveHub(max_frame=128, max_cluster=10_000, max_bytes_per_second=10_000)
    with TestClient(create_app(live=framed)) as client:
        with client.websocket_connect("/api/live/publish?id=FRAM") as publisher:
            publisher.receive_json()
            publisher.send_bytes(b"x" * 200)
            with pytest.raises(WebSocketDisconnect) as too_big:
                publisher.receive_json()
            assert too_big.value.code == CODE_TOO_BIG
        assert framed.get("FRAM") is None

    rate = LiveHub(max_bytes_per_second=1024)
    with TestClient(create_app(live=rate)) as client:
        with client.websocket_connect("/api/live/publish?id=RATE") as publisher:
            publisher.receive_json()
            publisher.send_bytes(b"x" * 9000)
            with pytest.raises(WebSocketDisconnect) as too_fast:
                publisher.receive_json()
            assert too_fast.value.code == CODE_RATE
        assert rate.get("RATE") is None

    huge = LiveHub(max_cluster=64)
    with TestClient(create_app(live=huge)) as client:
        with client.websocket_connect("/api/live/publish?id=HUGE") as publisher:
            publisher.receive_json()
            with client.websocket_connect("/api/live/HUGE") as listener:
                assert listener.receive_json()["type"] == "start"
                publisher.send_bytes(b"x" * 128)
                with pytest.raises(WebSocketDisconnect) as cluster:
                    publisher.receive_json()
                assert cluster.value.code == CODE_TOO_BIG
                assert listener.receive_json() == {"type": "end"}
        assert huge.get("HUGE") is None


def test_text_after_a_cluster_does_not_refresh_the_idle_timer():
    cluster = cluster_known(0, b"wave")
    head, _data = document([cluster])
    hub = LiveHub(init_timeout=2, idle_timeout=0.4)
    with TestClient(create_app(live=hub)) as client:
        with client.websocket_connect("/api/live/publish?id=KEEP") as publisher:
            publisher.receive_json()
            publisher.send_json({"type": "mime", "mime": "audio/webm"})
            publisher.send_bytes(head + cluster)
            sent = time.monotonic()
            time.sleep(0.25)
            publisher.send_json({"type": "mime", "mime": "audio/webm"})
            publisher.send_text("still here")
            publisher.send_bytes(cluster[:2])
            with pytest.raises(WebSocketDisconnect) as quiet:
                publisher.receive_json()
            assert quiet.value.code == CODE_TIMEOUT
            assert time.monotonic() - sent < 0.58
        assert hub.get("KEEP") is None


def test_a_second_mime_is_ignored_and_the_first_one_sticks():
    cluster = cluster_known(0, b"wave")
    head, _data = document([cluster])
    with TestClient(create_app()) as client:
        with client.websocket_connect("/api/live/publish?id=ONCE") as publisher:
            publisher.receive_json()
            publisher.send_json({"type": "mime", "mime": "audio/webm"})
            publisher.send_json({"type": "mime", "mime": "text/html"})
            publisher.send_bytes(head + cluster)
            with client.websocket_connect("/api/live/ONCE") as listener:
                assert listener.receive_json()["mime"] == "audio/webm;codecs=opus"
                assert listener.receive_bytes() == head


def test_one_address_may_publish_twice_and_forwarded_for_does_not_count():
    hub = LiveHub()
    with TestClient(create_app(live=hub)) as client:
        first = {"x-real-ip": "203.0.113.8", "x-forwarded-for": "198.51.100.1"}
        second = {"x-real-ip": "203.0.113.8", "x-forwarded-for": "198.51.100.2"}
        spoofed = {"x-forwarded-for": "203.0.113.50"}
        with client.websocket_connect("/api/live/publish?id=IPAA", headers=first) as held:
            assert held.receive_json()["id"] == "IPAA"
            with client.websocket_connect("/api/live/publish?id=IPAB", headers=second) as also:
                assert also.receive_json()["id"] == "IPAB"
                with pytest.raises(WebSocketDisconnect) as full:
                    with client.websocket_connect("/api/live/publish?id=IPAC", headers=first) as extra:
                        extra.receive_json()
                assert full.value.code == CODE_FULL
                with client.websocket_connect(
                    "/api/live/publish?id=IPBA",
                    headers={"x-real-ip": "203.0.113.9", "x-forwarded-for": "198.51.100.1"},
                ) as other:
                    assert other.receive_json()["id"] == "IPBA"
            with client.websocket_connect("/api/live/publish?id=FFAA", headers=spoofed) as forwarded:
                assert forwarded.receive_json()["id"] == "FFAA"
                with client.websocket_connect(
                    "/api/live/publish?id=FFAB",
                    headers={"x-forwarded-for": "203.0.113.51"},
                ) as forwarded_again:
                    assert forwarded_again.receive_json()["id"] == "FFAB"
                    with pytest.raises(WebSocketDisconnect) as shared:
                        with client.websocket_connect(
                            "/api/live/publish?id=FFAC",
                            headers={"x-forwarded-for": "203.0.113.52"},
                        ) as forwarded_full:
                            forwarded_full.receive_json()
                    assert shared.value.code == CODE_FULL
        assert hub.get("IPAA") is None
        with client.websocket_connect("/api/live/publish?id=IPAA", headers=first) as again:
            assert again.receive_json()["id"] == "IPAA"


def test_a_listener_who_leaves_during_silence_is_dropped():
    hub = LiveHub(max_listeners=1, init_timeout=5, idle_timeout=5)
    with TestClient(create_app(live=hub)) as client:
        with client.websocket_connect("/api/live/publish?id=QUIE") as publisher:
            assert publisher.receive_json()["id"] == "QUIE"
            with client.websocket_connect("/api/live/QUIE") as listener:
                assert listener.receive_json()["type"] == "start"
                stream = hub.get("QUIE")
                assert stream is not None
                assert len(stream.listeners) == 1
            assert stream.listeners == set()
            with client.websocket_connect("/api/live/QUIE") as again:
                assert again.receive_json()["type"] == "start"
                assert len(stream.listeners) == 1


def test_publisher_disconnect_tells_the_listener_and_clears_the_stream():
    cluster = cluster_known(0, b"wave")
    head, _data = document([cluster])
    hub = LiveHub()
    with TestClient(create_app(live=hub)) as client:
        with client.websocket_connect("/api/live/publish?id=HALF") as publisher:
            publisher.receive_json()
            with client.websocket_connect("/api/live/HALF") as listener:
                assert listener.receive_json()["type"] == "start"
                publisher.send_bytes(head + cluster)
                assert listener.receive_bytes() == head
                assert listener.receive_bytes() == cluster
                stream = hub.get("HALF")
                assert stream is not None
                assert len(stream.listeners) == 1
                publisher.close()
                assert listener.receive_json() == {"type": "end"}
                assert hub.get("HALF") is None
                assert stream.listeners == set()
                assert stream.closed
        gone = client.get("/stream/HALF")
        assert "この配信はありません" in gone.text


def test_joined_tone_plays_from_the_current_moment_not_the_beginning():
    webm = _tone()
    init, clusters = split_webm(webm)
    assert init + b"".join(clusters) == webm
    join_at = next(index for index, cluster in enumerate(clusters) if (cluster_timecode(cluster) or 0) >= 2500)
    prefix = init + b"".join(clusters[: join_at + 1])
    rest = b"".join(clusters[join_at + 1 :])
    with TestClient(create_app()) as client:
        with client.websocket_connect("/api/live/publish?id=PLAY") as publisher:
            publisher.receive_json()
            with client.websocket_connect("/api/live/PLAY") as early:
                early.receive_json()
                _send(publisher, prefix)
                assert early.receive_bytes() == init
                early_clusters = [early.receive_bytes() for _ in range(join_at + 1)]
                assert early_clusters == clusters[: join_at + 1]
                assert cluster_timecode(early_clusters[0]) == 0
                with client.websocket_connect("/api/live/PLAY") as late:
                    late.receive_json()
                    assert late.receive_bytes() == init
                    first = late.receive_bytes()
                    assert first == clusters[join_at]
                    assert (cluster_timecode(first) or 0) >= 2500
                    _send(publisher, rest)
                    late_rest = []
                    early_rest = []
                    for cluster in clusters[join_at + 1 :]:
                        assert late.receive_bytes() == cluster
                        late_rest.append(cluster)
                        assert early.receive_bytes() == cluster
                        early_rest.append(cluster)
                    publisher.close()
                    assert late.receive_json()["type"] == "end"
                    assert early.receive_json()["type"] == "end"
    late_audio = init + first + b"".join(late_rest)
    early_audio = init + b"".join(early_clusters + early_rest)
    late_pcm = _decode(late_audio)
    early_pcm = _decode(early_audio)
    late_seconds = len(late_pcm) / 48000
    early_seconds = len(early_pcm) / 48000
    assert 0.6 < late_seconds < 2.2
    assert early_seconds > 3.5
    assert _dominant(late_pcm, 0.05) == 880
    assert _dominant(early_pcm, 0.2) == 440
    assert _dominant(early_pcm, 2.6) == 880
    assert not list(Path(".").glob("*.webm"))
