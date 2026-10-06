"""Split a live WebM byte stream into an initialization segment and Clusters.

MediaRecorder does not cut `dataavailable` blobs on Cluster boundaries. A blob
can end in the middle of a SimpleBlock, and the next blob continues it. Opus
audio can be decoded from a later Cluster only when the listener has the bytes
before the first Cluster (EBML, Segment, Info, Tracks) and then whole Clusters.

Clusters are not rewritten. A known-size Cluster is forwarded once its bytes
are in hand. MediaRecorder writes Clusters with an unknown size, so that
Cluster is complete only when the next Cluster starts, or when the publisher
finishes. Bytes before the first Cluster are the initialization segment.
"""

from __future__ import annotations

EBML_MAGIC = b"\x1a\x45\xdf\xa3"
EBML_ID = 0x1A45DFA3
SEGMENT_ID = 0x18538067
CLUSTER_ID = 0x1F43B675
TIMECODE_ID = 0xE7

# Children of a Cluster. Anything else at that level ends an unknown-size Cluster.
CLUSTER_CHILDREN = frozenset(
    {
        TIMECODE_ID,
        0xA7,  # Position
        0xAB,  # PrevSize
        0xA3,  # SimpleBlock
        0xA0,  # BlockGroup
        0x5854,  # SilentTracks
        0xAF,  # EncryptedBlock
        0xEC,  # Void
    }
)

# One open Cluster plus a little header. A live relay must not keep the show.
DEFAULT_MAX_BUFFER = 256 * 1024


class WebmError(Exception):
    pass


class WebmTooBig(WebmError):
    """The open Cluster, or the bytes still being assembled, passed the buffer cap."""


def _width(first: int) -> int:
    mask = 0x80
    width = 1
    while width <= 8 and first & mask == 0:
        mask >>= 1
        width += 1
    if width > 8 or first == 0:
        raise WebmError("webm を分けられません")
    return width


def _read_header(buf: bytes | bytearray, pos: int) -> tuple[int, int, int | None] | None:
    """Return (element id, index after the size, payload size or None if unknown).

    None means the header is not fully buffered yet.
    """

    if pos >= len(buf):
        return None
    id_len = _width(buf[pos])
    if id_len > 4:
        raise WebmError("webm を分けられません")
    if pos + id_len >= len(buf):
        return None
    element_id = int.from_bytes(buf[pos : pos + id_len], "big")
    size_at = pos + id_len
    size_len = _width(buf[size_at])
    if size_at + size_len > len(buf):
        return None
    raw = int.from_bytes(buf[size_at : size_at + size_len], "big")
    mask = (1 << (size_len * 7)) - 1
    value = raw & mask
    unknown = value == mask
    return element_id, size_at + size_len, None if unknown else value


def cluster_timecode(cluster: bytes) -> int | None:
    """Cluster timestamp in TimecodeScale units (milliseconds for WebM)."""

    header = _read_header(cluster, 0)
    if header is None or header[0] != CLUSTER_ID:
        return None
    _, header_end, size = header
    end = len(cluster) if size is None else min(len(cluster), header_end + size)
    pos = header_end
    while pos < end:
        child = _read_header(cluster, pos)
        if child is None:
            return None
        element_id, child_header, child_size = child
        if child_size is None or child_header + child_size > len(cluster):
            return None
        if element_id == TIMECODE_ID:
            return int.from_bytes(cluster[child_header : child_header + child_size], "big")
        pos = child_header + child_size
    return None


def rebase_cluster(cluster: bytes, base_ms: int) -> bytes:
    """Subtract `base_ms` from the Cluster timecode, keeping the element's width.

    SimpleBlock timestamps are relative to that timecode, so the blocks stay
    valid. The video muxer starts its own timeline at zero and re-encodes the
    Opus mix to AAC-LC, so a listener who joins later would otherwise see
    audio timestamps from the start of the broadcast and video timestamps
    from zero.
    """

    if base_ms <= 0:
        return cluster
    header = _read_header(cluster, 0)
    if header is None or header[0] != CLUSTER_ID:
        return cluster
    _, header_end, size = header
    end = len(cluster) if size is None else min(len(cluster), header_end + size)
    pos = header_end
    while pos < end:
        child = _read_header(cluster, pos)
        if child is None:
            return cluster
        element_id, child_header, child_size = child
        if child_size is None or child_header + child_size > len(cluster):
            return cluster
        if element_id == TIMECODE_ID:
            current = int.from_bytes(cluster[child_header : child_header + child_size], "big")
            updated = max(0, current - base_ms)
            if updated >= 1 << (child_size * 8):
                return cluster
            out = bytearray(cluster)
            out[child_header : child_header + child_size] = updated.to_bytes(child_size, "big")
            return bytes(out)
        pos = child_header + child_size
    return cluster


class WebmSplitter:
    """Keep only the initialization segment and the Cluster that is still open."""

    def __init__(self, max_buffer: int = DEFAULT_MAX_BUFFER) -> None:
        self.max_buffer = max_buffer
        self.init: bytes | None = None
        self._buf = bytearray()
        self._pos = 0
        self._open: int | None = None
        self._checked = False

    @property
    def buffered(self) -> int:
        return len(self._buf)

    def clear(self) -> None:
        self.init = None
        self._buf.clear()
        self._pos = 0
        self._open = None

    def feed(self, data: bytes) -> list[bytes]:
        if not data:
            return []
        if len(self._buf) + len(data) > self.max_buffer:
            raise WebmTooBig("大きすぎる塊です")
        self._buf.extend(data)
        return self._pull(final=False)

    def finish(self) -> list[bytes]:
        return self._pull(final=True)

    def _pull(self, final: bool) -> list[bytes]:
        if not self._checked and len(self._buf) >= 4 and self.init is None and self._pos == 0:
            self._checked = True
            if not self._buf.startswith(EBML_MAGIC):
                raise WebmError("webm ではありません")
        emitted: list[bytes] = []
        while True:
            header = _read_header(self._buf, self._pos)
            if header is None:
                break
            element_id, header_end, size = header
            if self._open is None:
                emitted_now, done = self._outside(element_id, header_end, size)
                emitted.extend(emitted_now)
                if done:
                    break
                continue
            emitted_now, done = self._inside(element_id, header_end, size)
            emitted.extend(emitted_now)
            if done:
                break
        if final and self._open is not None and self._pos > self._open:
            emitted.append(bytes(self._buf[self._open : self._pos]))
            self._open = None
            self._pos = len(self._buf)
        self._compact()
        if len(self._buf) > self.max_buffer:
            raise WebmTooBig("大きすぎる塊です")
        return emitted

    def _outside(self, element_id: int, header_end: int, size: int | None) -> tuple[list[bytes], bool]:
        if element_id == CLUSTER_ID:
            if self.init is None:
                self.init = bytes(self._buf[: self._pos])
                if not self.init.startswith(EBML_MAGIC):
                    raise WebmError("webm ではありません")
            if size is None:
                self._open = self._pos
                self._pos = header_end
                return [], False
            end = header_end + size
            if end > len(self._buf):
                return [], True
            cluster = bytes(self._buf[self._pos : end])
            self._pos = end
            return [cluster], False
        if size is None:
            if element_id != SEGMENT_ID:
                raise WebmError("webm を分けられません")
            self._pos = header_end
            return [], False
        end = header_end + size
        if end > len(self._buf):
            return [], True
        self._pos = end
        return [], False

    def _inside(self, element_id: int, header_end: int, size: int | None) -> tuple[list[bytes], bool]:
        if element_id == CLUSTER_ID or element_id not in CLUSTER_CHILDREN:
            cluster = bytes(self._buf[self._open : self._pos])
            self._open = None
            if element_id == CLUSTER_ID:
                return [cluster], False
            if size is None:
                raise WebmError("webm を分けられません")
            end = header_end + size
            if end > len(self._buf):
                return [cluster], True
            self._pos = end
            return [cluster], False
        if size is None:
            raise WebmError("webm を分けられません")
        end = header_end + size
        if end > len(self._buf):
            return [], True
        self._pos = end
        return [], False

    def _compact(self) -> None:
        if self.init is None:
            return
        drop = self._open if self._open is not None else self._pos
        if drop <= 0:
            return
        del self._buf[:drop]
        self._pos -= drop
        if self._open is not None:
            self._open -= drop


def split_webm(data: bytes) -> tuple[bytes, list[bytes]]:
    """Split a finished file. Slices stay under the buffer cap, so a long tone is not one feed."""

    splitter = WebmSplitter()
    clusters: list[bytes] = []
    step = 16 * 1024
    for start in range(0, len(data), step):
        clusters.extend(splitter.feed(data[start : start + step]))
    clusters.extend(splitter.finish())
    if splitter.init is None:
        raise WebmError("初期化セグメントがありません")
    return splitter.init, clusters
