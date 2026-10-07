import { formatTime } from "./format.js";

export function freshPlaylistState() {
  return {
    playlists: [],
    playlistId: "",
    playlistIndex: 0,
    playlistError: "",
    playlistNaming: "create",
    playlistBusy: false,
    trackBpm: {},
    playlistImportNote: "",
    playlistImportError: false,
    playlistImporting: false,
  };
}

export function playlistHasTrack(playlists, id) {
  if (!id || !Array.isArray(playlists)) return false;
  return playlists.some(
    (playlist) => Array.isArray(playlist?.tracks) && playlist.tracks.some((track) => track?.id === id),
  );
}

/** Playlist row meta. BPM is the stored track value, one decimal, not tempo-adjusted. */
export function playlistMetaText(track, bpm) {
  const bits = [track?.channel, track?.duration ? formatTime(track.duration) : ""].filter(Boolean);
  if (typeof bpm === "number" && Number.isFinite(bpm)) bits.push(`${bpm.toFixed(1)} BPM`);
  return bits.join(" · ");
}

function importCount(value) {
  const number = Number(value);
  if (!Number.isFinite(number) || number < 0) return 0;
  return Math.floor(number);
}

export function importResultMessage(result) {
  const parts = [];
  const added = importCount(result?.added);
  const duplicates = importCount(result?.duplicates);
  const unavailable = importCount(result?.unavailable);
  const overflow = importCount(result?.overflow);
  if (added) parts.push(`${added}曲追加`);
  if (duplicates) parts.push(`${duplicates}曲は重複`);
  if (unavailable) parts.push(`${unavailable}曲は非公開か削除`);
  if (overflow) parts.push(`${overflow}曲は入りきらない`);
  return parts.join("、");
}

export function trackSnapshot(track) {
  if (!track || typeof track.id !== "string" || !track.id) return null;
  const duration = Number(track.duration);
  const thumbnail =
    typeof track.thumbnail === "string" && track.thumbnail.startsWith("https://") ? track.thumbnail : null;
  return {
    id: track.id,
    title: track.title || track.id,
    channel: track.channel || "",
    duration: Number.isFinite(duration) && duration > 0 ? Math.round(duration) : null,
    thumbnail,
  };
}

export function createPlaylistActions({ deps, state, scheduleRender, loadTrack }) {
  let revision = 0;
  const bpmInflight = new Set();
  const bpmPending = new Map();
  const bpmGeneration = new Map();
  let listedGeneration = new Set();

  function currentPlaylist() {
    return state.playlists.find((item) => item.id === state.playlistId) || null;
  }

  function adopt(playlist, index) {
    const existing = state.playlists.findIndex((item) => item.id === playlist.id);
    if (existing === -1) state.playlists.push(playlist);
    else state.playlists[existing] = playlist;
    state.playlistId = playlist.id;
    const count = playlist.tracks?.length || 0;
    state.playlistIndex = count ? Math.min(Math.max(0, index), count - 1) : 0;
    syncListed();
  }

  function forget(playlistId) {
    state.playlists = state.playlists.filter((item) => item.id !== playlistId);
    if (state.playlistId === playlistId) {
      state.playlistId = state.playlists[0]?.id || "";
      state.playlistIndex = 0;
    }
    syncListed();
  }

  function listedIds() {
    const ids = new Set();
    for (const playlist of state.playlists) {
      for (const track of playlist?.tracks || []) {
        if (typeof track?.id === "string" && track.id) ids.add(track.id);
      }
    }
    return ids;
  }

  function bpmMapFrom(raw) {
    const ids = listedIds();
    const next = {};
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) return next;
    for (const [id, value] of Object.entries(raw)) {
      if (!ids.has(id) || typeof value !== "number" || !Number.isFinite(value)) continue;
      next[id] = value;
    }
    return next;
  }

  function trackBpmMap() {
    if (!state.trackBpm || typeof state.trackBpm !== "object" || Array.isArray(state.trackBpm)) {
      state.trackBpm = {};
    }
    return state.trackBpm;
  }

  function syncListed() {
    const ids = listedIds();
    for (const id of listedGeneration) {
      if (!ids.has(id)) bpmGeneration.set(id, (bpmGeneration.get(id) || 0) + 1);
    }
    listedGeneration = ids;
  }

  function retainTrackBpm() {
    syncListed();
    const map = trackBpmMap();
    const ids = listedIds();
    for (const id of Object.keys(map)) {
      if (!ids.has(id)) delete map[id];
    }
  }

  function roundBpm(bpm) {
    const value = Number(bpm);
    if (!Number.isFinite(value)) return null;
    return Math.round(value * 100) / 100;
  }

  function publishTrackBpm(id, bpm) {
    if (typeof deps.saveBpm !== "function") return;
    if (!playlistHasTrack(state.playlists, id)) return;
    const rounded = roundBpm(bpm);
    if (rounded == null) return;
    if (bpmInflight.has(id)) {
      bpmPending.set(id, rounded);
      return;
    }
    if (trackBpmMap()[id] === rounded) return;
    sendTrackBpm(id, rounded);
  }

  function sendTrackBpm(id, rounded) {
    if (!playlistHasTrack(state.playlists, id)) return;
    if (trackBpmMap()[id] === rounded) return;
    const generation = bpmGeneration.get(id) || 0;
    bpmInflight.add(id);
    let pending;
    try {
      pending = deps.saveBpm(id, rounded);
    } catch {
      bpmInflight.delete(id);
      return;
    }
    Promise.resolve(pending)
      .then((body) => {
        if ((bpmGeneration.get(id) || 0) !== generation) return;
        if (!playlistHasTrack(state.playlists, id)) return;
        const reported = body && typeof body.bpm === "number" ? roundBpm(body.bpm) : null;
        trackBpmMap()[id] = reported == null ? rounded : reported;
        scheduleRender();
      })
      .catch(() => {})
      .finally(() => {
        bpmInflight.delete(id);
        if (!bpmPending.has(id)) return;
        const next = bpmPending.get(id);
        bpmPending.delete(id);
        publishTrackBpm(id, next);
      });
  }

  function deckBpm(id) {
    for (const name of ["A", "B"]) {
      const deckState = state.decks?.[name];
      if (
        deckState?.id === id &&
        typeof deckState.bpm === "number" &&
        Number.isFinite(deckState.bpm) &&
        deckState.bpm > 0
      ) {
        return deckState.bpm;
      }
    }
    return null;
  }

  function fail(message) {
    state.playlistError = message;
    scheduleRender();
  }

  async function loadPlaylists() {
    if (typeof deps.fetchPlaylists !== "function") return;
    const gen = ++revision;
    try {
      const data = await deps.fetchPlaylists();
      if (gen !== revision) return;
      state.playlists = Array.isArray(data?.playlists) ? data.playlists : [];
      if (!state.playlists.some((item) => item.id === state.playlistId)) {
        state.playlistId = state.playlists[0]?.id || "";
        state.playlistIndex = 0;
      }
      syncListed();
      state.trackBpm = bpmMapFrom(data?.bpm);
    } catch (err) {
      if (gen !== revision) return;
      const message = err instanceof Error ? err.message : "";
      state.playlistError = message || "プレイリストを読めませんでした";
    }
    scheduleRender();
  }

  async function mutate(work, apply, onError) {
    if (state.playlistBusy) return;
    const gen = ++revision;
    state.playlistBusy = true;
    state.playlistError = "";
    scheduleRender();
    try {
      const result = await work();
      if (gen !== revision) return;
      apply(result);
    } catch (err) {
      if (gen !== revision) return;
      const message = err instanceof Error ? err.message : "";
      if (typeof onError === "function") onError(message);
      else state.playlistError = message || "プレイリストを保存できませんでした";
    } finally {
      if (gen === revision) {
        state.playlistBusy = false;
        scheduleRender();
      }
    }
  }

  function rejectImport(message) {
    state.playlistImportNote = message;
    state.playlistImportError = true;
    scheduleRender();
  }

  function focusPlaylistName() {
    state.playlistNaming = "create";
    state.playlistError = "";
    deps.setPlaylistNameValue?.("");
    deps.focusPlaylistNameElement?.();
    scheduleRender();
  }

  function beginRenamePlaylist() {
    const playlist = currentPlaylist();
    if (!playlist) {
      fail("プレイリストを作ってください");
      return;
    }
    state.playlistNaming = "rename";
    state.playlistError = "";
    deps.setPlaylistNameValue?.(playlist.name);
    deps.focusPlaylistNameElement?.();
    scheduleRender();
  }

  function blurPlaylistName() {
    state.playlistNaming = "create";
    deps.blurPlaylistNameElement?.();
    scheduleRender();
  }

  function submitPlaylistName() {
    if (state.playlistNaming === "rename") return renamePlaylist();
    return createPlaylist();
  }

  function nameValue() {
    return (deps.playlistNameValue?.() || "").trim();
  }

  function createPlaylist() {
    const name = nameValue();
    if (!name) {
      fail("名前を入れてください");
      return;
    }
    if (typeof deps.createPlaylist !== "function") return;
    return mutate(
      () => deps.createPlaylist(name),
      (playlist) => {
        adopt(playlist, 0);
        state.playlistNaming = "create";
        deps.setPlaylistNameValue?.("");
        deps.blurPlaylistNameElement?.();
      },
    );
  }

  function renamePlaylist(explicitName) {
    const playlist = currentPlaylist();
    if (!playlist) {
      fail("プレイリストを作ってください");
      return;
    }
    const name = (typeof explicitName === "string" ? explicitName : nameValue()).trim();
    if (!name) {
      fail("名前を入れてください");
      return;
    }
    if (typeof deps.renamePlaylist !== "function") return;
    return mutate(
      () => deps.renamePlaylist(playlist.id, name),
      (updated) => {
        adopt(updated, state.playlistIndex);
        state.playlistNaming = "create";
        deps.blurPlaylistNameElement?.();
      },
    );
  }

  function deletePlaylist() {
    const playlist = currentPlaylist();
    if (!playlist) {
      fail("プレイリストを作ってください");
      return;
    }
    const ask = deps.confirmDelete;
    if (typeof ask === "function" && !ask(`「${playlist.name}」を消しますか？`)) return;
    if (typeof deps.deletePlaylist !== "function") return;
    return mutate(
      () => deps.deletePlaylist(playlist.id),
      () => {
        forget(playlist.id);
        retainTrackBpm();
      },
    );
  }

  function selectPlaylist(id) {
    if (!state.playlists.some((item) => item.id === id)) return;
    state.playlistId = id;
    state.playlistIndex = 0;
    state.playlistError = "";
    scheduleRender();
  }

  function cyclePlaylist(delta) {
    if (!state.playlists.length) return;
    const current = state.playlists.findIndex((item) => item.id === state.playlistId);
    const count = state.playlists.length;
    const next = (Math.max(0, current) + delta + count) % count;
    selectPlaylist(state.playlists[next].id);
  }

  function movePlaylistSelection(delta) {
    const playlist = currentPlaylist();
    const count = playlist?.tracks?.length || 0;
    if (!count) return;
    state.playlistIndex = (state.playlistIndex + delta + count) % count;
    scheduleRender();
  }

  function addTrack(track, playlistId) {
    const playlist = playlistId
      ? state.playlists.find((item) => item.id === playlistId) || null
      : currentPlaylist();
    if (!playlist) {
      fail("プレイリストを作ってください");
      return;
    }
    const snapshot = trackSnapshot(track);
    if (!snapshot) {
      fail("曲を選んでください");
      return;
    }
    if (typeof deps.addPlaylistTrack !== "function") return;
    const index = playlist.tracks?.length || 0;
    return mutate(
      () => deps.addPlaylistTrack(playlist.id, { ...snapshot, index }),
      (updated) => {
        adopt(updated, index);
        const known = deckBpm(snapshot.id);
        if (known != null) publishTrackBpm?.(snapshot.id, known);
      },
    );
  }

  function addSearchHit() {
    return addTrack(state.results[state.selected]);
  }

  function addTrackToPlaylist(playlistId, track) {
    return addTrack(track, playlistId);
  }

  function deckTrack(deck) {
    const deckState = state.decks?.[deck];
    if (deckState?.track?.id) return deckState.track;
    if (!deckState?.id) return null;
    return { id: deckState.id, title: deckState.title, channel: deckState.channel };
  }

  function addDeckTrack(deck) {
    const track = deckTrack(deck);
    if (!track?.id) {
      fail("デッキに曲がありません");
      return;
    }
    return addTrack(track);
  }

  function removePlaylistTrack() {
    const playlist = currentPlaylist();
    if (!playlist?.tracks?.length) {
      fail(playlist ? "曲を選んでください" : "プレイリストを作ってください");
      return;
    }
    if (typeof deps.removePlaylistTrack !== "function") return;
    const index = Math.min(state.playlistIndex, playlist.tracks.length - 1);
    return mutate(
      () => deps.removePlaylistTrack(playlist.id, index),
      (updated) => {
        adopt(updated, index);
        retainTrackBpm();
      },
    );
  }

  function movePlaylistTrack(delta) {
    const playlist = currentPlaylist();
    if (!playlist?.tracks?.length) {
      fail(playlist ? "曲を選んでください" : "プレイリストを作ってください");
      return;
    }
    const index = state.playlistIndex;
    const next = index + delta;
    if (next < 0 || next >= playlist.tracks.length) return;
    if (typeof deps.movePlaylistTrack !== "function") return;
    return mutate(
      () => deps.movePlaylistTrack(playlist.id, index, next),
      (updated) => {
        adopt(updated, next);
      },
    );
  }

  function placePlaylistTrack(from, to, trackId) {
    const playlist = currentPlaylist();
    if (!playlist?.tracks?.length) return;
    if (state.playlistBusy) return;
    if (!Number.isInteger(to)) return;
    const count = playlist.tracks.length;
    let source = from;
    if (typeof trackId === "string" && trackId) {
      if (playlist.tracks[from]?.id === trackId) source = from;
      else source = playlist.tracks.findIndex((track) => track.id === trackId);
      if (source < 0) return;
    }
    if (!Number.isInteger(source) || source === to) return;
    if (source < 0 || to < 0 || source >= count || to >= count) return;
    if (typeof deps.movePlaylistTrack !== "function") return;
    const snapshot = playlist.tracks.slice();
    const previousIndex = state.playlistIndex;
    const tracks = snapshot.slice();
    const [item] = tracks.splice(source, 1);
    tracks.splice(to, 0, item);
    playlist.tracks = tracks;
    state.playlistIndex = to;
    const pending = mutate(
      () => deps.movePlaylistTrack(playlist.id, source, to),
      (updated) => {
        adopt(updated, to);
      },
    );
    return pending.finally(() => {
      if (state.playlistBusy) return;
      if (state.playlistError && currentPlaylist() === playlist) {
        playlist.tracks = snapshot;
        state.playlistIndex = previousIndex;
        scheduleRender();
      }
    });
  }

  let importAbort = null;

  function finishImport(controller) {
    if (importAbort !== controller) return;
    importAbort = null;
    state.playlistImporting = false;
    scheduleRender();
  }

  function importPlaylist(explicit) {
    if (state.playlistImporting) {
      const controller = importAbort;
      controller?.abort();
      if (importAbort === controller) {
        importAbort = null;
        state.playlistImporting = false;
        scheduleRender();
      }
      return;
    }
    const source = (explicit?.url ?? deps.playlistImportUrl?.() ?? "").trim();
    if (!source) {
      rejectImport("プレイリストのURLを入れてください");
      return;
    }
    const dest = explicit?.playlistId ?? deps.playlistImportDestination?.() ?? "new";
    const name = (explicit?.name ?? deps.playlistImportName?.() ?? "").trim();
    const body = { url: source };
    if (dest && dest !== "new") body.playlist_id = dest;
    else if (!name) {
      rejectImport("名前を入れてください");
      return;
    } else body.name = name;
    if (typeof deps.importPlaylist !== "function") return;
    if (state.playlistBusy) return;
    const controller = new AbortController();
    importAbort = controller;
    state.playlistImportNote = "";
    state.playlistImportError = false;
    state.playlistImporting = true;
    scheduleRender();
    return Promise.resolve()
      .then(() => deps.importPlaylist(body, { signal: controller.signal }))
      .then((result) => {
        if (importAbort !== controller || controller.signal.aborted) return;
        const playlist = result?.playlist;
        if (playlist?.id) {
          const previous = state.playlists.find((item) => item.id === playlist.id);
          adopt(playlist, previous?.tracks?.length || 0);
          if (!body.playlist_id) {
            deps.setPlaylistImportDestination?.(playlist.id);
            deps.setPlaylistImportName?.("");
          }
        }
        state.playlistImportNote = importResultMessage(result);
        state.playlistImportError = false;
        deps.setPlaylistImportUrl?.("");
      })
      .catch((err) => {
        if (importAbort !== controller) return;
        if (controller.signal.aborted || err?.name === "AbortError") {
          state.playlistImportNote = "";
          state.playlistImportError = false;
          return;
        }
        const message = err instanceof Error ? err.message : "";
        state.playlistImportNote = message || "プレイリストを取り込めませんでした";
        state.playlistImportError = true;
      })
      .finally(() => finishImport(controller));
  }

  function loadPlaylistTrack(deck) {
    const playlist = currentPlaylist();
    const track = playlist?.tracks?.[state.playlistIndex];
    if (!track) return;
    return loadTrack(deck, track);
  }

  return {
    loadPlaylists,
    focusPlaylistName,
    beginRenamePlaylist,
    blurPlaylistName,
    submitPlaylistName,
    createPlaylist,
    renamePlaylist,
    deletePlaylist,
    selectPlaylist,
    cyclePlaylist,
    movePlaylistSelection,
    addSearchHit,
    addTrackToPlaylist,
    addDeckTrack,
    removePlaylistTrack,
    movePlaylistTrack,
    placePlaylistTrack,
    importPlaylist,
    loadPlaylistTrack,
    publishTrackBpm,
  };
}
