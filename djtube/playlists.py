from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import uuid
from pathlib import Path

from djtube.ids import is_video_id
from djtube.paths import PACKAGE_DIR
from djtube.search import Track, track_from_payload

log = logging.getLogger(__name__)

NAME_MAX = 80
PLAYLIST_MAX = 40
TRACK_MAX = 300
BPM_MIN = 20
BPM_MAX = 500
_PLAYLIST_ID = re.compile(r"^[0-9a-f]{32}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class PlaylistError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def playlist_path() -> Path:
    raw = os.environ.get("DJTUBE_PLAYLISTS", "").strip()
    if raw:
        return Path(raw).expanduser()
    return PACKAGE_DIR.parent / "data" / "playlists.json"


def normalize_name(name: object) -> str:
    if not isinstance(name, str):
        return ""
    cleaned = _CONTROL.sub(" ", name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:NAME_MAX]


class PlaylistStore:
    """Named track lists.

    The file is the durable copy when it can be written. If the write fails,
    the in-memory list still serves this process.
    """

    def __init__(self, path: Path, *, playlist_limit: int = PLAYLIST_MAX, track_limit: int = TRACK_MAX):
        self.path = path
        self.playlist_limit = playlist_limit
        self.track_limit = track_limit
        self.durable = True
        self._lock = threading.Lock()
        self._playlists: list[dict] = []
        self._bpm: dict[str, float] = {}
        self._load()

    def list_playlists(self) -> list[dict]:
        with self._lock:
            return [_public(item) for item in self._playlists]

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "playlists": [_public(item) for item in self._playlists],
                "bpm": dict(self._bpm),
            }

    def set_bpm(self, video_id: object, bpm: object) -> float:
        if not isinstance(video_id, str) or not is_video_id(video_id):
            raise PlaylistError("動画IDが正しくありません")
        rounded = _rounded_bpm(bpm)
        if rounded is None:
            raise PlaylistError("BPMが正しくありません")
        with self._lock:
            if video_id not in _track_ids(self._playlists):
                raise PlaylistError("プレイリストにその曲がありません", 404)
            if self._bpm.get(video_id) == rounded:
                return rounded
            self._bpm[video_id] = rounded
            self._save()
            return rounded

    def create(self, name: object) -> dict:
        cleaned = normalize_name(name)
        if not cleaned:
            raise PlaylistError("名前を入れてください")
        with self._lock:
            if len(self._playlists) >= self.playlist_limit:
                raise PlaylistError("プレイリストが多すぎます")
            playlist = {"id": uuid.uuid4().hex, "name": cleaned, "tracks": []}
            self._playlists.append(playlist)
            self._save()
            return _public(playlist)

    def rename(self, playlist_id: str, name: object) -> dict:
        cleaned = normalize_name(name)
        if not cleaned:
            raise PlaylistError("名前を入れてください")
        with self._lock:
            playlist = self._find(playlist_id)
            playlist["name"] = cleaned
            self._save()
            return _public(playlist)

    def delete(self, playlist_id: str) -> None:
        with self._lock:
            playlist = self._find(playlist_id)
            self._playlists.remove(playlist)
            self._save()

    def add_track(self, playlist_id: str, payload: object) -> dict:
        if not isinstance(payload, dict):
            raise PlaylistError("曲を選べません")
        raw = dict(payload)
        index = raw.pop("index", None)
        track = track_from_payload(raw)
        if track is None:
            raise PlaylistError("動画IDが正しくありません")
        with self._lock:
            playlist = self._find(playlist_id)
            tracks = playlist["tracks"]
            if len(tracks) >= self.track_limit:
                raise PlaylistError("曲数が多すぎます")
            if index is None:
                at = len(tracks)
            elif isinstance(index, bool) or not isinstance(index, int) or index < 0 or index > len(tracks):
                raise PlaylistError("曲を選べません")
            else:
                at = index
            tracks.insert(at, track.as_dict())
            self._save()
            return _public(playlist)

    def remove_track(self, playlist_id: str, index: int) -> dict:
        with self._lock:
            playlist = self._find(playlist_id)
            tracks = playlist["tracks"]
            if isinstance(index, bool) or not isinstance(index, int) or index < 0 or index >= len(tracks):
                raise PlaylistError("曲を選べません")
            del tracks[index]
            self._save()
            return _public(playlist)

    def get(self, playlist_id: str) -> dict:
        with self._lock:
            return _public(self._find(playlist_id))

    def import_tracks(
        self,
        tracks: list[Track],
        *,
        playlist_id: str | None = None,
        name: str | None = None,
        unavailable: int = 0,
        repeated: int = 0,
    ) -> dict:
        """Append tracks under the playlist lock and save once.

        Existing tracks stay at the front. A video id already in the playlist,
        or already seen in `tracks`, is not added again.
        """

        with self._lock:
            creating = None
            if playlist_id:
                playlist = self._find(playlist_id)
            elif name is not None:
                cleaned = normalize_name(name)
                if not cleaned:
                    raise PlaylistError("名前を入れてください")
                if len(self._playlists) >= self.playlist_limit:
                    raise PlaylistError("プレイリストが多すぎます")
                creating = {"id": uuid.uuid4().hex, "name": cleaned, "tracks": []}
                playlist = creating
            else:
                raise PlaylistError("追加先を選んでください")
            existing = {item["id"] for item in playlist["tracks"]}
            duplicates = repeated if isinstance(repeated, int) and not isinstance(repeated, bool) else 0
            to_add: list[dict] = []
            for track in tracks:
                if not isinstance(track, Track):
                    raise PlaylistError("動画IDが正しくありません")
                if track.id in existing:
                    duplicates += 1
                    continue
                existing.add(track.id)
                to_add.append(track.as_dict())
            if len(playlist["tracks"]) + len(to_add) > self.track_limit:
                raise PlaylistError("曲数が多すぎます")
            if creating is not None:
                self._playlists.append(creating)
            playlist["tracks"].extend(to_add)
            self._save()
            missed = unavailable if isinstance(unavailable, int) and not isinstance(unavailable, bool) else 0
            return {
                "playlist": _public(playlist),
                "added": len(to_add),
                "duplicates": duplicates,
                "unavailable": missed,
            }

    def move_track(self, playlist_id: str, from_index: int, to_index: int) -> dict:
        with self._lock:
            playlist = self._find(playlist_id)
            tracks = playlist["tracks"]
            if (
                isinstance(from_index, bool)
                or isinstance(to_index, bool)
                or not isinstance(from_index, int)
                or not isinstance(to_index, int)
                or from_index < 0
                or to_index < 0
                or from_index >= len(tracks)
                or to_index >= len(tracks)
            ):
                raise PlaylistError("曲を選べません")
            item = tracks.pop(from_index)
            tracks.insert(to_index, item)
            self._save()
            return _public(playlist)

    def _find(self, playlist_id: str) -> dict:
        if not isinstance(playlist_id, str) or not _PLAYLIST_ID.fullmatch(playlist_id):
            raise PlaylistError("プレイリストが見つかりません", 404)
        for playlist in self._playlists:
            if playlist["id"] == playlist_id:
                return playlist
        raise PlaylistError("プレイリストが見つかりません", 404)

    def _load(self) -> None:
        self._playlists = []
        self._bpm = {}
        if not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log.warning("playlist file unreadable: %s", self.path)
            self._park_unreadable()
            return
        if not isinstance(raw, dict) or not isinstance(raw.get("playlists"), list):
            log.warning("playlist file has an unexpected shape: %s", self.path)
            self._park_unreadable()
            return
        playlists: list[dict] = []
        for item in raw["playlists"]:
            parsed = _parse_playlist(item)
            if parsed is not None:
                playlists.append(parsed)
        self._playlists = playlists
        self._bpm = _bpm_table(raw.get("bpm"), _track_ids(playlists))

    def _park_unreadable(self) -> None:
        backup = self.path.with_name(self.path.name + ".bak")
        try:
            os.replace(self.path, backup)
        except OSError:
            log.warning("could not move unreadable playlist file aside: %s", self.path)

    def _save(self) -> None:
        self._bpm = _bpm_table(self._bpm, _track_ids(self._playlists))
        payload = {"playlists": [_public(item) for item in self._playlists], "bpm": self._bpm}
        text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(self.path.name + ".tmp")
            temporary.write_text(text, encoding="utf-8")
            os.replace(temporary, self.path)
        except OSError:
            log.warning("could not write playlists to %s; keeping them in memory", self.path)
            self.durable = False
            return
        self.durable = True


def _parse_playlist(item: object) -> dict | None:
    if not isinstance(item, dict):
        return None
    playlist_id = item.get("id")
    name = normalize_name(item.get("name"))
    if not isinstance(playlist_id, str) or not _PLAYLIST_ID.fullmatch(playlist_id) or not name:
        return None
    tracks: list[dict] = []
    raw_tracks = item.get("tracks")
    if isinstance(raw_tracks, list):
        for raw in raw_tracks:
            track = track_from_payload(raw)
            if track is not None:
                tracks.append(track.as_dict())
    return {"id": playlist_id, "name": name, "tracks": tracks}


def _track_ids(playlists: list[dict]) -> set[str]:
    return {track["id"] for playlist in playlists for track in playlist["tracks"]}


def _rounded_bpm(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    if not math.isfinite(number) or number < BPM_MIN or number > BPM_MAX:
        return None
    return round(number, 2)


def _bpm_table(raw: object, referenced: set[str]) -> dict[str, float]:
    if not isinstance(raw, dict):
        return {}
    table: dict[str, float] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or key not in referenced or not is_video_id(key):
            continue
        bpm = _rounded_bpm(value)
        if bpm is None:
            continue
        table[key] = bpm
    return table


def _public(playlist: dict) -> dict:
    return {
        "id": playlist["id"],
        "name": playlist["name"],
        "tracks": [dict(track) for track in playlist["tracks"]],
    }
