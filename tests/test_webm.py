from __future__ import annotations

import shutil

import pytest

from djtube.webm import WebmError, WebmSplitter, WebmTooBig, cluster_timecode, split_webm

UNKNOWN = b"\x01" + b"\xff" * 7


def _size(length: int) -> bytes:
    if length < 0x7F:
        return bytes([0x80 | length])
    if length < 0x3FFF:
        return (0x4000 | length).to_bytes(2, "big")
    raise AssertionError(length)


def elem(element_id: bytes, payload: bytes) -> bytes:
    return element_id + _size(len(payload)) + payload


def cluster_unknown(timecode: int, body: bytes) -> bytes:
    children = elem(b"\xe7", timecode.to_bytes(2, "big")) + elem(b"\xa3", body)
    return b"\x1f\x43\xb6\x75" + UNKNOWN + children


def cluster_known(timecode: int, body: bytes) -> bytes:
    children = elem(b"\xe7", timecode.to_bytes(2, "big")) + elem(b"\xa3", body)
    return elem(b"\x1f\x43\xb6\x75", children)


def document(clusters: list[bytes]) -> tuple[bytes, bytes]:
    ebml = elem(b"\x1a\x45\xdf\xa3", b"\x42\x82\x84webm")
    info = elem(b"\x15\x49\xa9\x66", b"info")
    tracks = elem(b"\x16\x54\xae\x6b", b"tracks")
    head = ebml + b"\x18\x53\x80\x67" + UNKNOWN + info + tracks
    return head, head + b"".join(clusters)


def _one_byte(data: bytes) -> tuple[bytes, list[bytes]]:
    splitter = WebmSplitter()
    found: list[bytes] = []
    for index in range(len(data)):
        found.extend(splitter.feed(data[index : index + 1]))
    found.extend(splitter.finish())
    assert splitter.init is not None
    return splitter.init, found


def test_unknown_size_clusters_survive_byte_splits_and_are_not_all_kept():
    clusters = [cluster_unknown(0, b"A" * 30), cluster_unknown(200, b"B" * 40), cluster_unknown(400, b"C" * 50)]
    head, data = document(clusters)
    splitter = WebmSplitter()
    complete = splitter.feed(data)
    assert complete == clusters[:2]
    assert splitter.init == head
    assert splitter.buffered < len(clusters[-1]) + 32
    assert splitter.buffered < len(data) / 2
    complete.extend(splitter.finish())
    assert complete == clusters
    assert [cluster_timecode(cluster) for cluster in complete] == [0, 200, 400]
    assert _one_byte(data) == (head, clusters)


def test_known_size_clusters_emit_without_waiting_for_the_next_one():
    clusters = [cluster_known(0, b"A" * 20), cluster_known(180, b"B" * 20), cluster_known(360, b"C" * 25)]
    head, data = document(clusters)
    splitter = WebmSplitter()
    assert splitter.feed(data) == clusters
    assert splitter.finish() == []
    assert splitter.buffered == 0
    assert splitter.init == head
    assert _one_byte(data) == (head, clusters)


def test_a_cluster_id_split_across_chunks_is_not_forwarded_early():
    clusters = [cluster_known(0, b"AAAA"), cluster_known(20, b"BBBB")]
    head, data = document(clusters)
    cut = len(head) + len(clusters[0]) + 2
    splitter = WebmSplitter()
    first = splitter.feed(data[:cut])
    assert first == [clusters[0]]
    assert clusters[1] not in first
    assert splitter.feed(data[cut:]) == [clusters[1]]


def test_truncated_known_cluster_is_dropped_on_finish():
    head, _data = document([])
    partial = head + cluster_known(0, b"Z" * 40)[:12]
    splitter = WebmSplitter()
    assert splitter.feed(partial) == []
    assert splitter.finish() == []


def test_an_open_chunk_over_the_buffer_is_too_big():
    splitter = WebmSplitter(max_buffer=32)
    with pytest.raises(WebmTooBig):
        splitter.feed(b"\x1a\x45\xdf\xa3" + b"\x00" * 64)
    again = WebmSplitter(max_buffer=32)
    again.feed(b"\x1a\x45\xdf\xa3")
    with pytest.raises(WebmTooBig):
        again.feed(b"\x00" * 40)


def test_garbage_is_rejected():
    splitter = WebmSplitter()
    with pytest.raises(WebmError):
        splitter.feed(b"this is not a webm stream")


def test_ffmpeg_live_webm_round_trips_through_odd_slices():
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg is required to build the test tone")
    from djtube.live_tone import render_webm

    data = render_webm([(440, 1.0), (880, 1.0)], cluster_ms=200)
    init, clusters = split_webm(data)
    assert init + b"".join(clusters) == data
    assert clusters
    assert cluster_timecode(clusters[0]) == 0
    assert cluster_timecode(clusters[-1]) > 1000
    assert _one_byte(data) == (init, clusters)
    held = WebmSplitter()
    emitted = held.feed(data)
    assert len(emitted) == len(clusters)
    assert held.buffered < 1024
