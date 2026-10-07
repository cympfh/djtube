from __future__ import annotations

import json

from fastapi.testclient import TestClient

from djtube.app import create_app
from djtube.paths import PUBLIC_PREFIX
from djtube.playlists import PlaylistStore

VIDEO_ID = "abcdefghijk"
OTHER_ID = "zzzzzzzzzzz"
TRACK = {
    "id": VIDEO_ID,
    "title": "夜",
    "channel": "人",
    "duration": 90,
    "thumbnail": "https://i.ytimg.com/vi/abcdefghijk/mqdefault.jpg",
}


def _client(tmp_path, **limits):
    store = PlaylistStore(tmp_path / "playlists.json", **limits)
    return TestClient(create_app(store)), store


def test_playlist_crud_persists_for_a_new_process(tmp_path):
    client, store = _client(tmp_path)
    created = client.post("/api/playlists", json={"name": "  夜の\nセット  "})
    assert created.status_code == 200
    playlist = created.json()
    assert playlist["name"] == "夜の セット"
    assert playlist["tracks"] == []

    added = client.post(f"/api/playlists/{playlist['id']}/tracks", json=TRACK)
    assert added.status_code == 200
    assert added.json()["tracks"] == [TRACK]

    renamed = client.patch(f"/api/playlists/{playlist['id']}", json={"name": "朝"})
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "朝"

    again = TestClient(create_app(PlaylistStore(store.path)))
    listed = again.get("/api/playlists")
    assert listed.status_code == 200
    assert listed.json()["playlists"] == [renamed.json()]
    prefixed = again.get(f"{PUBLIC_PREFIX}/api/playlists")
    assert prefixed.json() == listed.json()

    removed = again.delete(f"/api/playlists/{playlist['id']}")
    assert removed.status_code == 204
    assert again.get("/api/playlists").json() == {"playlists": [], "bpm": {}}
    reloaded = PlaylistStore(store.path)
    assert reloaded.list_playlists() == []
    assert reloaded.durable is True


def test_playlist_tracks_reorder_remove_and_reject_bad_ids(tmp_path):
    client, _store = _client(tmp_path)
    playlist_id = client.post("/api/playlists", json={"name": "セット"}).json()["id"]
    first = dict(TRACK)
    second = {
        "id": f"https://www.youtube.com/watch?v={OTHER_ID}",
        "title": "昼",
        "channel": "店",
        "duration": 12.6,
        "thumbnail": "javascript:alert(1)",
    }
    client.post(f"/api/playlists/{playlist_id}/tracks", json=first)
    added = client.post(f"/api/playlists/{playlist_id}/tracks", json=second)
    assert added.status_code == 200
    assert added.json()["tracks"][1]["id"] == OTHER_ID
    assert added.json()["tracks"][1]["duration"] == 13
    assert added.json()["tracks"][1]["thumbnail"] is None

    moved = client.post(f"/api/playlists/{playlist_id}/tracks/move", json={"from": 1, "to": 0})
    assert [item["id"] for item in moved.json()["tracks"]] == [OTHER_ID, VIDEO_ID]

    removed = client.delete(f"/api/playlists/{playlist_id}/tracks/0")
    assert [item["id"] for item in removed.json()["tracks"]] == [VIDEO_ID]

    bad = client.post(f"/api/playlists/{playlist_id}/tracks", json={"id": "nope", "title": "x"})
    assert bad.status_code == 400
    assert bad.json()["detail"] == "動画IDが正しくありません"
    missing = client.post("/api/playlists/not-an-id/tracks", json=TRACK)
    assert missing.status_code == 404
    empty = client.post("/api/playlists", json={"name": "   "})
    assert empty.status_code == 400
    assert empty.json()["detail"] == "名前を入れてください"


def test_playlist_limits_and_unreadable_or_unwritable_files(tmp_path):
    client, store = _client(tmp_path, playlist_limit=1, track_limit=1)
    playlist_id = client.post("/api/playlists", json={"name": "ひとつ"}).json()["id"]
    assert client.post("/api/playlists", json={"name": "ふたつ"}).status_code == 400
    assert client.post(f"/api/playlists/{playlist_id}/tracks", json=TRACK).status_code == 200
    extra = client.post(
        f"/api/playlists/{playlist_id}/tracks",
        json={"id": OTHER_ID, "title": "もう一曲", "channel": "", "duration": None, "thumbnail": None},
    )
    assert extra.status_code == 400
    assert extra.json()["detail"] == "曲数が多すぎます"

    broken = tmp_path / "broken.json"
    broken.write_text("{", encoding="utf-8")
    reloaded = PlaylistStore(broken)
    assert reloaded.list_playlists() == []
    assert not broken.exists()
    assert (tmp_path / "broken.json.bak").is_file()

    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x", encoding="utf-8")
    memory = PlaylistStore(blocker / "playlists.json")
    created = memory.create("メモリ")
    assert created["name"] == "メモリ"
    assert memory.list_playlists() == [created]
    assert memory.durable is False
    assert not (blocker / "playlists.json").exists()


def test_saved_file_drops_invalid_entries(tmp_path):
    path = tmp_path / "playlists.json"
    path.write_text(
        json.dumps(
            {
                "playlists": [
                    {"id": "not-hex", "name": "だめ", "tracks": []},
                    {
                        "id": "a" * 32,
                        "name": "残る",
                        "tracks": [TRACK, {"id": "short", "title": "捨てる"}],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    store = PlaylistStore(path)
    listed = store.list_playlists()
    assert len(listed) == 1
    assert listed[0]["name"] == "残る"
    assert listed[0]["tracks"] == [TRACK]


def _playlist_with(client, *tracks, name="夜"):
    playlist_id = client.post("/api/playlists", json={"name": name}).json()["id"]
    for track in tracks:
        added = client.post(f"/api/playlists/{playlist_id}/tracks", json=track)
        assert added.status_code == 200
    return playlist_id


def test_saved_bpm_is_rounded_and_survives_restart(tmp_path):
    client, store = _client(tmp_path)
    playlist_id = _playlist_with(client, TRACK)
    saved = client.post("/api/bpm", json={"id": VIDEO_ID, "bpm": 128.041})
    assert saved.status_code == 200
    assert saved.json() == {"id": VIDEO_ID, "bpm": 128.04}
    assert json.loads(store.path.read_text(encoding="utf-8"))["bpm"] == {VIDEO_ID: 128.04}

    again = TestClient(create_app(PlaylistStore(store.path)))
    listed = again.get("/api/playlists").json()
    assert listed["bpm"] == {VIDEO_ID: 128.04}
    assert listed["playlists"][0]["tracks"] == [TRACK]
    renamed = again.patch(f"/api/playlists/{playlist_id}", json={"name": "朝"})
    assert renamed.status_code == 200
    assert "bpm" not in renamed.json()
    assert again.get("/api/playlists").json()["bpm"] == {VIDEO_ID: 128.04}


def test_unknown_track_bpm_does_not_change_the_file(tmp_path):
    client, store = _client(tmp_path)
    _playlist_with(client, {**TRACK, "id": OTHER_ID, "title": "昼"})
    before = store.path.read_bytes()
    inode = store.path.stat().st_ino
    missing = client.post("/api/bpm", json={"id": VIDEO_ID, "bpm": 128})
    assert missing.status_code == 404
    assert store.path.read_bytes() == before
    assert store.path.stat().st_ino == inode
    assert client.get("/api/playlists").json()["bpm"] == {}

    absent = tmp_path / "empty.json"
    empty = TestClient(create_app(PlaylistStore(absent)))
    assert empty.post("/api/bpm", json={"id": VIDEO_ID, "bpm": 128}).status_code == 404
    assert not absent.exists()


def test_invalid_bpm_is_rejected(tmp_path):
    client, store = _client(tmp_path)
    _playlist_with(client, TRACK)
    before = store.path.read_bytes()
    inode = store.path.stat().st_ino
    bodies = [
        {"id": VIDEO_ID, "bpm": True},
        {"id": VIDEO_ID, "bpm": "128"},
        {"id": VIDEO_ID, "bpm": 19.99},
        {"id": VIDEO_ID, "bpm": 500.01},
        {"id": "watch?v=abcdefghijk", "bpm": 128},
        {"id": VIDEO_ID},
        ["not-an-object"],
    ]
    for body in bodies:
        response = client.post("/api/bpm", json=body)
        assert response.status_code == 400, body
    for literal in ("NaN", "Infinity", "-Infinity"):
        response = client.post(
            "/api/bpm",
            content=f'{{"id":"{VIDEO_ID}","bpm":{literal}}}'.encode(),
            headers={"content-type": "application/json"},
        )
        assert response.status_code == 400, literal
    assert store.path.read_bytes() == before
    assert store.path.stat().st_ino == inode
    assert client.get("/api/playlists").json()["bpm"] == {}


def test_same_rounded_bpm_does_not_rewrite(tmp_path):
    client, store = _client(tmp_path)
    _playlist_with(client, TRACK)
    first = client.post("/api/bpm", json={"id": VIDEO_ID, "bpm": 128.041})
    assert first.status_code == 200
    assert first.json()["bpm"] == 128.04
    before = store.path.read_bytes()
    inode = store.path.stat().st_ino
    for bpm in (128.04, 128.044, 128.041):
        again = client.post("/api/bpm", json={"id": VIDEO_ID, "bpm": bpm})
        assert again.status_code == 200
        assert again.json() == {"id": VIDEO_ID, "bpm": 128.04}
    assert store.path.read_bytes() == before
    assert store.path.stat().st_ino == inode

    changed = client.post("/api/bpm", json={"id": VIDEO_ID, "bpm": 130})
    assert changed.status_code == 200
    assert changed.json()["bpm"] == 130
    assert store.path.stat().st_ino != inode
    assert json.loads(store.path.read_text(encoding="utf-8"))["bpm"] == {VIDEO_ID: 130}


def test_one_bpm_row_drops_when_the_track_is_gone(tmp_path):
    client, store = _client(tmp_path)
    first = _playlist_with(client, TRACK, TRACK, name="夜")
    second = _playlist_with(client, TRACK, name="朝")
    saved = client.post("/api/bpm", json={"id": VIDEO_ID, "bpm": 128.041})
    assert saved.json()["bpm"] == 128.04
    raw = json.loads(store.path.read_text(encoding="utf-8"))
    assert raw["bpm"] == {VIDEO_ID: 128.04}
    assert sum(track["id"] == VIDEO_ID for playlist in raw["playlists"] for track in playlist["tracks"]) == 3

    assert client.delete(f"/api/playlists/{first}/tracks/0").status_code == 200
    assert json.loads(store.path.read_text(encoding="utf-8"))["bpm"] == {VIDEO_ID: 128.04}
    assert client.delete(f"/api/playlists/{first}/tracks/0").status_code == 200
    assert json.loads(store.path.read_text(encoding="utf-8"))["bpm"] == {VIDEO_ID: 128.04}

    assert client.delete(f"/api/playlists/{second}").status_code == 204
    assert json.loads(store.path.read_text(encoding="utf-8"))["bpm"] == {}

    assert client.post(f"/api/playlists/{first}/tracks", json=TRACK).status_code == 200
    assert client.post("/api/bpm", json={"id": VIDEO_ID, "bpm": 110.004}).json()["bpm"] == 110.0
    removed = client.delete(f"/api/playlists/{first}/tracks/0")
    assert removed.status_code == 200
    assert removed.json()["tracks"] == []
    assert "bpm" not in removed.json()
    assert json.loads(store.path.read_text(encoding="utf-8"))["bpm"] == {}
    listed = client.get("/api/playlists").json()
    assert listed["playlists"][0]["id"] == first
    assert listed["bpm"] == {}


def test_old_playlist_file_and_malformed_bpm_rows(tmp_path):
    path = tmp_path / "playlists.json"
    playlist_id = "a" * 32
    path.write_text(
        json.dumps({"playlists": [{"id": playlist_id, "name": "残る", "tracks": [TRACK]}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    store = PlaylistStore(path)
    assert store.snapshot() == {
        "playlists": [{"id": playlist_id, "name": "残る", "tracks": [TRACK]}],
        "bpm": {},
    }

    path.write_text(
        json.dumps(
            {
                "playlists": [
                    {
                        "id": playlist_id,
                        "name": "残る",
                        "tracks": [TRACK, {**TRACK, "id": OTHER_ID, "title": "昼"}],
                    }
                ],
                "bpm": {
                    VIDEO_ID: 128.041,
                    OTHER_ID: True,
                    "short": 120,
                    "ccccccccccc": 140,
                    "ddddddddddd": "128",
                    "eeeeeeeeeee": float("nan"),
                    "fffffffffff": float("inf"),
                    "ggggggggggg": 10,
                    VIDEO_ID + "x": 128,
                },
            },
            ensure_ascii=False,
            allow_nan=True,
        ),
        encoding="utf-8",
    )
    loaded = PlaylistStore(path)
    assert loaded.snapshot()["bpm"] == {VIDEO_ID: 128.04}
    loaded.rename(playlist_id, "朝")
    assert json.loads(path.read_text(encoding="utf-8"))["bpm"] == {VIDEO_ID: 128.04}


def test_add_track_payload_bpm_is_ignored(tmp_path):
    client, _store = _client(tmp_path)
    playlist_id = client.post("/api/playlists", json={"name": "夜"}).json()["id"]
    added = client.post(f"/api/playlists/{playlist_id}/tracks", json={**TRACK, "bpm": 140})
    assert added.status_code == 200
    assert added.json()["tracks"] == [TRACK]
    assert "bpm" not in added.json()
    assert client.get("/api/playlists").json()["bpm"] == {}


def test_unwritable_bpm_stays_in_memory(tmp_path):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x", encoding="utf-8")
    store = PlaylistStore(blocker / "playlists.json")
    client = TestClient(create_app(store))
    playlist_id = client.post("/api/playlists", json={"name": "メモリ"}).json()["id"]
    assert client.post(f"/api/playlists/{playlist_id}/tracks", json=TRACK).status_code == 200
    saved = client.post("/api/bpm", json={"id": VIDEO_ID, "bpm": 128.041})
    assert saved.status_code == 200
    assert saved.json() == {"id": VIDEO_ID, "bpm": 128.04}
    assert store.durable is False
    assert store.snapshot()["bpm"] == {VIDEO_ID: 128.04}
    assert not (blocker / "playlists.json").exists()
