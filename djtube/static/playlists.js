export function freshPlaylistState() {
  return {
    playlists: [],
    playlistId: "",
    playlistIndex: 0,
    playlistError: "",
    playlistNaming: "create",
    playlistBusy: false,
  };
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
  }

  function forget(playlistId) {
    state.playlists = state.playlists.filter((item) => item.id !== playlistId);
    if (state.playlistId === playlistId) {
      state.playlistId = state.playlists[0]?.id || "";
      state.playlistIndex = 0;
    }
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
    } catch (err) {
      if (gen !== revision) return;
      const message = err instanceof Error ? err.message : "";
      state.playlistError = message || "プレイリストを読めませんでした";
    }
    scheduleRender();
  }

  async function mutate(work, apply) {
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
      state.playlistError = message || "プレイリストを保存できませんでした";
    } finally {
      if (gen === revision) {
        state.playlistBusy = false;
        scheduleRender();
      }
    }
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

  function renamePlaylist() {
    const playlist = currentPlaylist();
    if (!playlist) {
      fail("プレイリストを作ってください");
      return;
    }
    const name = nameValue();
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

  function addTrack(track) {
    const playlist = currentPlaylist();
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
      },
    );
  }

  function addSearchHit() {
    return addTrack(state.results[state.selected]);
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
    addDeckTrack,
    removePlaylistTrack,
    movePlaylistTrack,
    loadPlaylistTrack,
  };
}
