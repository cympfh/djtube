"""Publish a generated tone the way the DJ page will publish a mix.

ffmpeg writes Opus in a live WebM. This process sends that byte stream to the
publisher socket in short slices that are not aligned to Clusters, the way
MediaRecorder blobs arrive. The tone is synthesized. Nothing here is a
copyrighted recording.

    uv run python -m djtube.live_tone --base http://127.0.0.1:8098
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
import sys

from djtube.paths import PUBLIC_PREFIX
from djtube.webm import cluster_timecode, split_webm

TONE_LOW = 440.0
TONE_HIGH = 880.0


class ToneError(RuntimeError):
    pass


def render_webm(segments: list[tuple[float, float]], cluster_ms: int = 200) -> bytes:
    """Encode sine segments to a live WebM (Opus). `segments` are (hertz, seconds)."""

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise ToneError("ffmpeg がありません")
    if not segments:
        raise ToneError("試験音が空です")
    inputs: list[str] = []
    for frequency, duration in segments:
        inputs.extend(["-f", "lavfi", "-i", f"sine=frequency={frequency}:sample_rate=48000:duration={duration}"])
    joined = "".join(f"[{index}:a]" for index in range(len(segments)))
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        *inputs,
        "-filter_complex",
        f"{joined}concat=n={len(segments)}:v=0:a=1[a]",
        "-map",
        "[a]",
        "-c:a",
        "libopus",
        "-application",
        "lowdelay",
        "-b:a",
        "32k",
        "-frame_duration",
        "20",
        "-f",
        "webm",
        "-cluster_time_limit",
        str(cluster_ms),
        "-live",
        "1",
        "pipe:1",
    ]
    result = subprocess.run(command, capture_output=True, check=False)
    if result.returncode != 0 or not result.stdout.startswith(b"\x1a\x45\xdf\xa3"):
        detail = result.stderr.decode("utf-8", "replace")[-400:]
        raise ToneError(detail or "試験音を作れませんでした")
    return result.stdout


def alternating_tone(seconds: float, step: float = 2.0) -> list[tuple[float, float]]:
    parts: list[tuple[float, float]] = []
    remaining = seconds
    frequency = TONE_LOW
    while remaining > 0:
        duration = min(step, remaining)
        parts.append((frequency, duration))
        remaining -= duration
        frequency = TONE_HIGH if frequency == TONE_LOW else TONE_LOW
    return parts


def endpoints(base: str, stream_id: str | None = None) -> tuple[str, str]:
    """Return (websocket publish URL, audio URL without the id)."""

    raw = base.strip().rstrip("/")
    suffix = PUBLIC_PREFIX
    origin = raw[: -len(suffix)] if raw.endswith(suffix) else raw
    http = origin + suffix
    ws = http.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
    publish = f"{ws}/api/live/publish"
    if stream_id:
        publish = f"{publish}?id={stream_id}"
    return publish, f"{http}/stream/"


def misaligned_slices(data: bytes) -> list[bytes]:
    """Cut points sit inside elements, as MediaRecorder timeslice blobs do."""

    sizes = (7, 13, 64, 200, 480)
    pieces: list[bytes] = []
    index = 0
    step = 0
    while index < len(data):
        size = sizes[step % len(sizes)]
        pieces.append(data[index : index + size])
        index += size
        step += 1
    return pieces


def _close_code(exc: Exception) -> int | None:
    received = getattr(exc, "rcvd", None)
    code = getattr(received, "code", None)
    if isinstance(code, int):
        return code
    fallback = getattr(exc, "code", None)
    return fallback if isinstance(fallback, int) else None


async def publish_webm(url: str, webm: bytes, pace: bool) -> str:
    import websockets

    init, clusters = split_webm(webm)
    payload = init + b"".join(clusters)
    marks: list[tuple[int, int]] = []
    offset = len(init)
    for cluster in clusters:
        offset += len(cluster)
        marks.append((offset, cluster_timecode(cluster) or 0))
    try:
        connection = await websockets.connect(url, max_size=2 * 1024 * 1024)
    except Exception as exc:
        raise _connect_error(exc) from exc
    async with connection:
        try:
            hello = json.loads(await connection.recv())
        except Exception as exc:
            raise _connect_error(exc) from exc
        stream_id = str(hello["id"])
        _announce(stream_id, url)
        sent = 0
        previous = 0
        mark = 0
        try:
            for piece in misaligned_slices(payload):
                await connection.send(piece)
                sent += len(piece)
                if not pace:
                    continue
                while mark < len(marks) and sent >= marks[mark][0]:
                    stamp = marks[mark][1]
                    delay = max(0.0, (stamp - previous) / 1000)
                    previous = stamp
                    mark += 1
                    if delay:
                        await asyncio.sleep(delay)
            if pace:
                await asyncio.sleep(0.3)
        except Exception as exc:
            raise _connect_error(exc) from exc
    return stream_id


def _announce(stream_id: str, publish_url: str) -> None:
    http = publish_url.replace("wss://", "https://", 1).replace("ws://", "http://", 1)
    root, _, _query = http.partition("?")
    listen = root[: -len("/api/live/publish")] + "/stream/" + stream_id
    print(f"ID: {stream_id}", flush=True)
    print(f"聴く: {listen}", flush=True)


def _connect_error(exc: Exception) -> ToneError:
    code = _close_code(exc)
    if code == 4409:
        return ToneError("その ID は使われています")
    if code == 4400:
        return ToneError("ID の形式が違います")
    if isinstance(exc, ToneError):
        return exc
    return ToneError("配信ソケットに繋がませんでした")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="試験音を配信ソケットへ送る")
    parser.add_argument("--base", default="http://127.0.0.1:8098", help="サーバのオリジン。/djtube が付いていてもよい")
    parser.add_argument("--seconds", type=float, default=30)
    parser.add_argument("--id", default="", help="4 文字の英大文字。空ならサーバが振る")
    parser.add_argument("--no-pace", action="store_true", help="待たずに全部送る")
    args = parser.parse_args(argv)
    if args.seconds <= 0:
        print("秒数を 0 より大きくしてください", file=sys.stderr)
        return 2
    publish_url, _listen_root = endpoints(args.base, args.id.strip() or None)
    try:
        webm = render_webm(alternating_tone(args.seconds))
    except ToneError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print("440 Hz と 880 Hz を 2 秒ごとに交互に送ります", flush=True)
    try:
        asyncio.run(publish_webm(publish_url, webm, pace=not args.no_pace))
    except ToneError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n止めました")
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
