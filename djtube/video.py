"""One MPEG-TS encoder per live stream, shared by every ?thumbnail=1 listener.

Container is MPEG-TS, video is H.264, audio is AAC-LC. The publisher's Opus
stays on the audio-only WebM URL. MPEG-TS Opus does not play on iOS, Safari,
or Windows Media Foundation (Unity and VRChat AVPro among them), so the video
URL re-encodes. A WebM cut that did not begin on a keyframe produced no
picture, so the bytes handed to a late listener start at the most recent SPS
(ffmpeg repeats it with each keyframe).

Measured on a 1280x720 solid frame at 4 fps, ultrafast + stillimage: about
0.09 s of CPU for 3 s of video, roughly 3% of one core. 5 fps is about 4%.
A solid frame landed near 150 kbps; the cap is 350 kbps so a detailed still
stays under about half a megabit per listener, audio included. Three of these
stay well under one core, which is the cap (429 above that).

The process runs only while at least one video listener is connected, plus a
short linger so a refresh does not pay to start ffmpeg again.
"""

from __future__ import annotations

import logging
import os
import queue
import signal
import subprocess
import threading
import time
from collections.abc import Callable

from djtube.compose import VIDEO_FPS, VIDEO_HEIGHT, VIDEO_WIDTH, Composer
from djtube.thumbs import ThumbCache
from djtube.webm import cluster_timecode, rebase_cluster

log = logging.getLogger("djtube.live")

VIDEO_MIME = "video/mp2t"
# See the module note. 350 kbps is the average cap, 400 kbps the peak.
VIDEO_BITRATE = 350_000
VIDEO_MAXRATE = 400_000
MAX_VIDEO_ENCODERS = 3
# One source opening every video URL would otherwise take the whole cap.
MAX_VIDEO_PER_IP = 1
# A refresh, or VLC reconnecting, should find the process still warm.
VIDEO_LINGER = 3.0
# No MPEG-TS for this long means the muxer is dead or stopped. Listeners are
# closed and the process is reaped so the slot can be used again.
OUTPUT_STALL = 3.0
# After a crash, wait before starting another process for the same stream.
RESTART_BACKOFF = 1.0
# Opus clusters are about 200 ms. This is a few seconds, then the oldest is
# dropped so a stuck muxer cannot fill memory or block the publisher.
AUDIO_QUEUE_MAX = 32
# AAC-LC, 48 kHz, one channel. The video URL is mono because the mix is.
VIDEO_AUDIO_BITRATE = "128k"
VIDEO_AUDIO_RATE = "48000"
# 800 packets is 150KB. A 1 s GOP at the 400 kbps ceiling is about 50KB,
# so the buffer still holds the last keyframe after a burst.
_SYNC_PACKETS = 800
_READ = 188 * 32
_SPS = 7


def ffmpeg_command(ffmpeg: str, width: int, height: int, fps: int, audio_fd: int) -> list[str]:
    gop = max(1, fps)
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-thread_queue_size",
        "64",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-video_size",
        f"{width}x{height}",
        "-framerate",
        str(fps),
        "-i",
        "pipe:0",
        "-thread_queue_size",
        "64",
        "-fflags",
        "nobuffer",
        "-probesize",
        "32768",
        "-analyzeduration",
        "0",
        "-f",
        "webm",
        "-i",
        f"pipe:{audio_fd}",
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c:v",
        "libx264",
        # One thread. The default pool made the first byte about 2.6 s late
        # and used twice the RSS. stillimage does not need the pool.
        "-threads",
        "1",
        "-preset",
        "ultrafast",
        "-tune",
        "stillimage",
        "-pix_fmt",
        "yuv420p",
        "-g",
        str(gop),
        "-keyint_min",
        str(gop),
        "-sc_threshold",
        "0",
        "-b:v",
        str(VIDEO_BITRATE),
        "-maxrate",
        str(VIDEO_MAXRATE),
        "-bufsize",
        str(VIDEO_BITRATE // 2),
        "-c:a",
        "aac",
        "-profile:a",
        "aac_low",
        "-ar",
        VIDEO_AUDIO_RATE,
        "-ac",
        "1",
        "-b:a",
        VIDEO_AUDIO_BITRATE,
        "-muxdelay",
        "0",
        "-muxpreload",
        "0",
        "-f",
        "mpegts",
        "-mpegts_flags",
        "+resend_headers",
        "pipe:1",
    ]


def _nal_types(payload: bytes) -> list[int]:
    found: list[int] = []
    index = 0
    limit = len(payload)
    while index + 3 < limit:
        if payload[index : index + 4] == b"\x00\x00\x00\x01" and index + 4 < limit:
            found.append(payload[index + 4] & 0x1F)
            index += 4
            continue
        if payload[index : index + 3] == b"\x00\x00\x01" and index + 3 < limit:
            found.append(payload[index + 3] & 0x1F)
            index += 3
            continue
        index += 1
    return found


def packet_pid_and_nals(packet: bytes) -> tuple[int, list[int]]:
    """PID and H.264 NAL types in one 188-byte MPEG-TS packet."""

    if len(packet) < 188 or packet[0] != 0x47:
        return -1, []
    pid = ((packet[1] & 0x1F) << 8) | packet[2]
    adapt = (packet[3] & 0x30) >> 4
    offset = 4
    if adapt in (2, 3):
        length = packet[4]
        offset = 5 + length
        if offset > 188:
            return pid, []
    if adapt == 2 or offset >= 188:
        return pid, []
    return pid, _nal_types(packet[offset:])


class TsSyncBuffer:
    """Bytes since the last video SPS, so a late listener can decode immediately."""

    def __init__(self, limit: int = _SYNC_PACKETS) -> None:
        self.limit = limit
        self.packets: list[bytes] = []
        self.sync = 0
        self.video_pid: int | None = None
        self._partial = b""

    def append(self, data: bytes) -> None:
        buf = self._partial + data
        self._partial = b""
        while len(buf) >= 188:
            if buf[0] != 0x47 or (len(buf) >= 376 and buf[188] != 0x47):
                nxt = buf.find(b"\x47", 1)
                if nxt < 0:
                    self._partial = buf[-1:]
                    return
                buf = buf[nxt:]
                continue
            self._push(buf[:188])
            buf = buf[188:]
        self._partial = buf

    def snapshot(self) -> bytes:
        if not self.packets:
            return b""
        return b"".join(self.packets[self.sync :])

    def _push(self, packet: bytes) -> None:
        self.packets.append(packet)
        if self._is_sync(packet):
            self.sync = len(self.packets) - 1
        self._trim()

    def _is_sync(self, packet: bytes) -> bool:
        pid, types = packet_pid_and_nals(packet)
        if _SPS not in types:
            return False
        if self.video_pid is None:
            self.video_pid = pid
        return pid == self.video_pid

    def _trim(self) -> None:
        overflow = len(self.packets) - self.limit
        if overflow <= 0:
            return
        drop = min(overflow, self.sync)
        if drop:
            del self.packets[:drop]
            self.sync -= drop
        overflow = len(self.packets) - self.limit
        if overflow > 0:
            del self.packets[:overflow]
            self.sync = 0


def pace_frames(next_at: float, now: float, interval: float, max_burst: int) -> tuple[int, float]:
    """Copies to write so the picture clock catches the wall clock.

    A late loop used to set the next deadline to `now` and drop the missed
    frames, so after a stall the picture stayed early against the audio.
    Duplicating the current frame keeps the timestamps on the wall clock.
    `next_at` itself is not replaced with `now`.
    """

    if interval <= 0:
        return 1, now
    if now <= next_at:
        return 1, next_at + interval
    late = int((now - next_at) / interval)
    copies = 1 + min(max(0, max_burst), late)
    return copies, next_at + copies * interval


class FfmpegRelay:
    """Encode one stream and fan the MPEG-TS out to its video listeners."""

    def __init__(
        self,
        stream_id: str,
        decks: Callable[[], tuple[tuple[str, float], ...]],
        thumbs: ThumbCache,
        *,
        width: int = VIDEO_WIDTH,
        height: int = VIDEO_HEIGHT,
        fps: int = VIDEO_FPS,
        linger: float = VIDEO_LINGER,
        ffmpeg: str = "ffmpeg",
        popen: Callable[..., subprocess.Popen] = subprocess.Popen,
        output_stall: float = OUTPUT_STALL,
        restart_backoff: float = RESTART_BACKOFF,
        audio_queue_max: int = AUDIO_QUEUE_MAX,
    ) -> None:
        self.stream_id = stream_id
        self._decks = decks
        self.thumbs = thumbs
        self.width = width
        self.height = height
        self.fps = fps
        self.linger = linger
        self.ffmpeg = ffmpeg
        self._popen = popen
        self.output_stall = output_stall
        self.restart_backoff = restart_backoff
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._started = False
        self._proc: subprocess.Popen | None = None
        self._audio_w = None
        self._audio_q: queue.Queue[bytes | None] = queue.Queue(maxsize=max(1, audio_queue_max))
        self._last_output = time.monotonic()
        self._backoff_until = 0.0
        self._init: bytes | None = None
        self._primed_ids: set[int] = set()
        self._base: int | None = None
        self._timer: threading.Timer | None = None
        self._threads: list[threading.Thread] = []
        self.listeners: set = set()
        self.sync = TsSyncBuffer()
        self.composer = Composer((width, height))

    def occupies(self) -> bool:
        with self._lock:
            return self._started

    def listener_count(self, address: str | None = None) -> int:
        with self._lock:
            if address is None:
                return len(self.listeners)
            return sum(item.address == address for item in self.listeners)

    def prime(self, init: bytes | None, recent: tuple[bytes, ...] | bytes | None) -> None:
        """Audio to write when the process starts. Called before the relay is published."""

        with self._lock:
            if self._started:
                return
            self._drain_queue()
            self._base = None
            chunks: tuple[bytes, ...]
            if recent is None:
                chunks = ()
            elif isinstance(recent, (bytes, bytearray)):
                chunks = (bytes(recent),)
            else:
                chunks = tuple(recent)
            self._primed_ids = {id(item) for item in chunks}
            if init is not None:
                self._init = init
            for item in chunks:
                self._enqueue(item)

    def note_init(self, init: bytes) -> None:
        with self._lock:
            if self._init is None:
                self._init = init

    def feed(self, cluster: bytes) -> None:
        with self._lock:
            if not self._started:
                return
            if id(cluster) in self._primed_ids:
                self._primed_ids.discard(id(cluster))
                return
            self._enqueue(cluster)

    def attach(self, listener) -> None:
        with self._lock:
            self._cancel_timer()
            if time.monotonic() < self._backoff_until:
                refused = True
            else:
                refused = False
                if not self._started:
                    self._start_locked()
                snapshot = self.sync.snapshot()
                if snapshot:
                    listener.offer_init(snapshot)
                self.listeners.add(listener)
        if refused:
            # A fresh process is not started yet. End the body so the client
            # does not sit on the previous keyframe.
            listener.offer_end()
            listener.mark_closed()

    def detach(self, listener) -> None:
        with self._lock:
            self.listeners.discard(listener)
            listener.mark_closed()
            if self.listeners or not self._started or self._timer is not None:
                return
            timer = threading.Timer(self.linger, self._linger)
            timer.daemon = True
            self._timer = timer
            timer.start()

    def close(self) -> None:
        with self._lock:
            timer = self._timer
            self._timer = None
            listeners = list(self.listeners)
            self.listeners.clear()
        if timer is not None:
            timer.cancel()
        self.stop()
        for listener in listeners:
            listener.offer_end()
            listener.mark_closed()

    def stop(self) -> None:
        with self._lock:
            if not self._started:
                return
            bundle = self._take_process_locked()
        self._finish(bundle)
        log.info("video %s stop", self.stream_id)

    def _linger(self) -> None:
        # The emptiness check and the decision to stop share this lock. A
        # listener that arrives here is either counted, or starts a new
        # process after this one has been taken.
        with self._lock:
            self._timer = None
            if self.listeners or not self._started:
                return
            bundle = self._take_process_locked()
        self._finish(bundle)
        log.info("video %s stop", self.stream_id)

    def _cancel_timer(self) -> None:
        timer = self._timer
        self._timer = None
        if timer is not None:
            timer.cancel()

    def _enqueue(self, item: bytes) -> None:
        while True:
            try:
                self._audio_q.put_nowait(item)
                return
            except queue.Full:
                try:
                    self._audio_q.get_nowait()
                except queue.Empty:
                    return

    def _drain_queue(self) -> None:
        while True:
            try:
                self._audio_q.get_nowait()
            except queue.Empty:
                return

    def _take_process_locked(self) -> tuple:
        self._started = False
        self._stop.set()
        proc = self._proc
        audio_w = self._audio_w
        self._proc = None
        self._audio_w = None
        timer = self._timer
        self._timer = None
        threads = list(self._threads)
        self._threads = []
        return proc, audio_w, timer, threads

    def _finish(self, bundle: tuple) -> None:
        proc, audio_w, timer, threads = bundle
        if timer is not None:
            timer.cancel()
        self._reap(proc, audio_w)
        current = threading.current_thread()
        for thread in threads:
            if thread is current:
                continue
            thread.join(timeout=1)

    def _reap(self, proc, audio_w) -> None:
        # A muxer blocked on a full pipe does not exit on SIGTERM, and a
        # stopped process ignores anything but CONT and KILL. Kill, then wait,
        # so it cannot stay <defunct> and hold the encoder slot.
        if proc is not None and proc.poll() is None:
            for sig in (signal.SIGCONT, signal.SIGKILL):
                try:
                    os.kill(proc.pid, sig)
                except OSError:
                    pass
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        elif proc is not None:
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        self._close_pipe(audio_w)
        if proc is not None:
            self._close_pipe(proc.stdin)
            self._close_pipe(proc.stdout)
            self._close_pipe(proc.stderr)

    def _fail(self) -> None:
        with self._lock:
            if not self._started:
                return
            listeners = list(self.listeners)
            self.listeners.clear()
            self._backoff_until = time.monotonic() + self.restart_backoff
            bundle = self._take_process_locked()
        self._finish(bundle)
        for listener in listeners:
            listener.offer_end()
            listener.mark_closed()
        log.info("video %s encoder failed", self.stream_id)

    def _start_locked(self) -> None:
        self._stop.clear()
        self.sync = TsSyncBuffer()
        self.composer = Composer((self.width, self.height))
        audio_r, audio_w = os.pipe()
        os.set_inheritable(audio_r, True)
        command = ffmpeg_command(self.ffmpeg, self.width, self.height, self.fps, audio_r)
        try:
            proc = self._popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                pass_fds=(audio_r,),
            )
        except OSError:
            os.close(audio_r)
            os.close(audio_w)
            raise
        os.close(audio_r)
        self._proc = proc
        self._audio_w = os.fdopen(audio_w, "wb", buffering=0)
        self._last_output = time.monotonic()
        self._started = True
        self._threads = [
            threading.Thread(target=self._read_loop, name=f"djtube-video-read-{self.stream_id}", daemon=True),
            threading.Thread(target=self._audio_loop, name=f"djtube-video-audio-{self.stream_id}", daemon=True),
            threading.Thread(target=self._frame_loop, name=f"djtube-video-frame-{self.stream_id}", daemon=True),
            threading.Thread(target=self._stderr_loop, name=f"djtube-video-err-{self.stream_id}", daemon=True),
            threading.Thread(target=self._watch_loop, name=f"djtube-video-watch-{self.stream_id}", daemon=True),
        ]
        for thread in self._threads:
            thread.start()
        log.info("video %s start", self.stream_id)

    def _emit(self, chunk: bytes) -> None:
        with self._lock:
            if not self._started:
                return
            self._last_output = time.monotonic()
            self.sync.append(chunk)
            for listener in self.listeners:
                if listener.saw_init:
                    listener.offer_media(chunk)
                else:
                    listener.offer_init(chunk)

    def _read_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        # BufferedReader.read(n) waits until n bytes or EOF. A pipe is not
        # interactive, so a short MPEG-TS write would sit unread. os.read
        # returns whatever the muxer has already written.
        fd = proc.stdout.fileno()
        try:
            while not self._stop.is_set():
                chunk = os.read(fd, _READ)
                if not chunk:
                    break
                self._emit(chunk)
        except (OSError, ValueError):
            self._fail()
            return
        self._fail()

    def _watch_loop(self) -> None:
        while not self._stop.wait(0.2):
            with self._lock:
                proc = self._proc
                started = self._started
                quiet_for = time.monotonic() - self._last_output
            if not started or proc is None:
                return
            dead = proc.poll() is not None
            if dead or quiet_for > self.output_stall:
                self._fail()
                return

    def _audio_loop(self) -> None:
        init = None
        while init is None and not self._stop.is_set():
            with self._lock:
                init = self._init
                audio_w = self._audio_w
            if init is None:
                self._stop.wait(0.05)
        if init is None or self._stop.is_set() or audio_w is None:
            return
        try:
            audio_w.write(init)
            while not self._stop.is_set():
                try:
                    item = self._audio_q.get(timeout=0.2)
                except queue.Empty:
                    continue
                if item is None:
                    break
                tc = cluster_timecode(item)
                with self._lock:
                    if tc is not None:
                        if self._base is None:
                            self._base = tc
                        item = rebase_cluster(item, self._base)
                audio_w.write(item)
        except (BrokenPipeError, OSError, ValueError):
            return

    def _frame_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            return
        interval = 1.0 / self.fps
        next_at = time.monotonic()
        # At most two seconds of duplicates per wake, then look at the picture again.
        max_burst = max(1, self.fps * 2)
        while not self._stop.is_set():
            frame = self._frame()
            copies, next_at = pace_frames(next_at, time.monotonic(), interval, max_burst)
            try:
                for _copy in range(copies):
                    proc.stdin.write(frame)
            except (BrokenPipeError, OSError, ValueError):
                return
            delay = next_at - time.monotonic()
            if delay > 0 and self._stop.wait(delay):
                return

    def _frame(self) -> bytes:
        decks = self._decks()
        images = {}
        for video_id, _gain in decks:
            self.thumbs.want(video_id)
            image = self.thumbs.get(video_id)
            if image is not None:
                images[video_id] = image
        frame = self.composer.frame(decks, images)
        if frame.size != (self.width, self.height):
            frame = frame.resize((self.width, self.height))
        return frame.tobytes()

    def _stderr_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            err = proc.stderr.read()
        except (OSError, ValueError):
            return
        if not err:
            return
        code = proc.poll()
        if code not in (None, 0, -15, 255):
            log.info("video %s ffmpeg %s", self.stream_id, err.decode("utf-8", "replace")[-300:])

    @staticmethod
    def _close_pipe(pipe) -> None:
        if pipe is None:
            return
        try:
            pipe.close()
        except (OSError, ValueError):
            return
