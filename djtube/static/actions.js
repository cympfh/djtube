import { deckGains } from "./gains.js";

export function freshDeck() {
  return {
    gen: 0,
    id: "",
    title: "",
    channel: "",
    status: "empty",
    error: "",
    playError: "",
    cue: 0,
    playing: false,
  };
}

export function freshState() {
  return {
    lastQuery: "",
    results: [],
    selected: 0,
    searching: false,
    searchError: "",
    source: "",
    loadTarget: "A",
    crossfader: 0.5,
    decks: { A: freshDeck(), B: freshDeck() },
  };
}

function deckOf(state, deck) {
  if (deck !== "A" && deck !== "B") return null;
  return state.decks[deck];
}

export function createActions(deps) {
  const { state, audios } = deps;

  function scheduleRender() {
    deps.scheduleRender?.();
  }

  function applyGains() {
    const gains = deckGains(state.crossfader);
    audios.A.volume = gains.a;
    audios.B.volume = gains.b;
  }

  function setCrossfader(value) {
    const numeric = Number(value);
    state.crossfader = Math.min(1, Math.max(0, Number.isFinite(numeric) ? numeric : 0));
    applyGains();
    scheduleRender();
  }

  function nudgeCrossfader(delta) {
    setCrossfader(state.crossfader + Number(delta) || 0);
  }

  function setCrossfaderFromController(value) {
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) return;
    const midi = Math.min(127, Math.max(0, numeric));
    setCrossfader(midi / 127);
  }

  function focusSearch() {
    deps.focusSearchElement?.();
  }

  function blurSearch() {
    deps.blurSearchElement?.();
  }

  function moveSelection(delta) {
    if (!state.results.length) return;
    const count = state.results.length;
    state.selected = (state.selected + delta + count) % count;
    scheduleRender();
  }

  function toggleLoadTarget() {
    state.loadTarget = state.loadTarget === "A" ? "B" : "A";
    scheduleRender();
  }

  async function submitSearch() {
    if (state.searching) return;
    const query = deps.queryValue().trim();
    if (!query) {
      state.searchError = "検索語を入れてください";
      scheduleRender();
      return;
    }
    state.searching = true;
    state.searchError = "";
    scheduleRender();
    try {
      const data = await deps.fetchSearch(query);
      state.lastQuery = query;
      state.results = Array.isArray(data?.tracks) ? data.tracks : [];
      state.selected = 0;
      state.source = data?.source || "";
      state.searchError = state.results.length ? "" : "見つかりませんでした";
    } catch (err) {
      state.results = [];
      state.source = "";
      const message = err instanceof Error ? err.message : "";
      state.searchError = message && !/^failed to fetch$/i.test(message) ? message : "検索できませんでした";
    } finally {
      state.searching = false;
      scheduleRender();
    }
  }

  function onEnter() {
    const query = deps.queryValue().trim();
    const focused = !!deps.isSearchFocused?.();
    if (focused && (state.searching || query !== state.lastQuery || state.results.length === 0)) {
      return submitSearch();
    }
    return loadSelected(state.loadTarget);
  }

  async function loadTrack(deck, track) {
    const deckState = deckOf(state, deck);
    const audio = audios[deck];
    if (!deckState || !track?.id || typeof audio.loadVideo !== "function") return;
    deckState.gen += 1;
    const gen = deckState.gen;
    deckState.id = track.id;
    deckState.title = track.title || track.id;
    deckState.channel = track.channel || "";
    deckState.error = "";
    deckState.playError = "";
    deckState.cue = 0;
    deckState.playing = false;
    audio.pause();
    const loaded = audio.loadVideo(track.id);
    if (deckState.gen !== gen) return;
    deckState.status = loaded === false ? "preparing" : "ready";
    applyGains();
    scheduleRender();
  }

  function loadSelected(deck) {
    const track = state.results[state.selected];
    if (!track) return;
    return loadTrack(deck, track);
  }

  function play(deck) {
    const deckState = deckOf(state, deck);
    const audio = audios[deck];
    if (!deckState?.id || (deckState.status !== "ready" && deckState.status !== "error")) return;
    deckState.playError = "";
    deckState.error = "";
    deckState.status = "ready";
    const pending = audio.play();
    deckState.playing = true;
    if (pending && typeof pending.catch === "function") {
      pending.catch(() => {
        if (audio.paused) {
          deckState.playing = false;
          deckState.playError = "再生がブロックされました";
          scheduleRender();
        }
      });
    }
    scheduleRender();
  }

  function togglePlay(deck) {
    const deckState = deckOf(state, deck);
    const audio = audios[deck];
    if (!deckState?.id || (deckState.status !== "ready" && deckState.status !== "error")) return;
    if (audio.paused) play(deck);
    else {
      audio.pause();
      deckState.playing = false;
      scheduleRender();
    }
  }

  function cue(deck) {
    const deckState = deckOf(state, deck);
    const audio = audios[deck];
    if (!deckState || deckState.status !== "ready") return;
    const atCue = Math.abs((audio.currentTime || 0) - deckState.cue) < 0.08;
    if (!audio.paused) {
      audio.pause();
      audio.currentTime = deckState.cue;
      deckState.playing = false;
      scheduleRender();
      return;
    }
    if (!atCue) {
      audio.currentTime = deckState.cue;
      scheduleRender();
      return;
    }
    play(deck);
  }

  function setCue(deck) {
    const deckState = deckOf(state, deck);
    const audio = audios[deck];
    if (!deckState || deckState.status !== "ready") return;
    deckState.cue = audio.currentTime || 0;
    scheduleRender();
  }

  return {
    focusSearch,
    blurSearch,
    submitSearch,
    onEnter,
    moveSelection,
    loadSelected,
    loadTrack,
    toggleLoadTarget,
    togglePlay,
    cue,
    setCue,
    nudgeCrossfader,
    setCrossfader,
    setCrossfaderFromController,
    applyGains,
  };
}
