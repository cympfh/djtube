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
    assert again.get("/api/playlists").json() == {"playlists": []}
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
