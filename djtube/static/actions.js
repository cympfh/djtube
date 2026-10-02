import { EQ_BANDS, clampEqUnit, eqGainDb, eqUnitFromMidi } from "./eq.js";
import { deckGains } from "./gains.js";
import { clampRate, rateFromMidi } from "./rate.js";
import { commandedSeekLanded } from "./seekland.js";

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
    rate: 1,
    eq: { high: 0.5, mid: 0.5, low: 0.5 },
    eqError: "",
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
    musicOnly: true,
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

  let searchGen = 0;

  async function submitSearch() {
    const query = deps.queryValue().trim();
    if (!query) {
      state.searchError = "検索語を入れてください";
      scheduleRender();
      return;
    }
    const gen = ++searchGen;
    const musicOnly = state.musicOnly;
    state.searching = true;
    state.searchError = "";
    scheduleRender();
    try {
      const data = await deps.fetchSearch(query, musicOnly);
      if (gen !== searchGen) return;
      state.lastQuery = query;
      state.results = Array.isArray(data?.tracks) ? data.tracks : [];
      state.selected = 0;
      state.source = data?.source || "";
      state.searchError = state.results.length ? "" : "見つかりませんでした";
    } catch (err) {
      if (gen !== searchGen) return;
      state.results = [];
      state.source = "";
      const message = err instanceof Error ? err.message : "";
      state.searchError = message && !/^failed to fetch$/i.test(message) ? message : "検索できませんでした";
    } finally {
      if (gen === searchGen) {
        state.searching = false;
        scheduleRender();
      }
    }
  }

  function setMusicOnly(value) {
    const next = !!value;
    if (next === state.musicOnly) return;
    state.musicOnly = next;
    scheduleRender();
    if (state.lastQuery || deps.queryValue().trim()) return submitSearch();
  }

  function toggleMusicOnly() {
    return setMusicOnly(!state.musicOnly);
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
    deckState.jogCommand = null;
    deckState.rate = 1;
    audio.cancelPendingSeek?.();
    audio.pause();
    audio.playbackRate = 1;
    const loaded = audio.loadVideo(track.id);
    if (deckState.gen !== gen) return;
    deckState.status = loaded === false ? "preparing" : "ready";
    audio.playbackRate = deckState.rate;
    let eqLive = true;
    for (const band of EQ_BANDS) {
      const applied = audio.setEqGain?.(band, eqGainDb(deckState.eq[band]));
      if (applied === false) eqLive = false;
    }
    deckState.eqError = eqLive ? "" : "イコライザーを音声に接続できませんでした";
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

  function playhead(audio) {
    try {
      const elementTime = audio.audio?.currentTime;
      if (Number.isFinite(elementTime)) return elementTime;
    } catch {
      /* element has no media yet */
    }
    const value = Number(audio.currentTime);
    return Number.isFinite(value) ? value : NaN;
  }

  function syncJog(deck) {
    const deckState = deckOf(state, deck);
    const audio = audios[deck];
    const pending = deckState?.jogCommand;
    if (!pending || !audio) return;
    const live = playhead(audio);
    if (commandedSeekLanded(pending.from, pending.at, live)) deckState.jogCommand = null;
  }

  function cue(deck) {
    const deckState = deckOf(state, deck);
    const audio = audios[deck];
    if (!deckState || deckState.status !== "ready") return;
    deckState.jogCommand = null;
    audio.cancelPendingSeek?.();
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

  function seek(deck, seconds) {
    const deckState = deckOf(state, deck);
    const audio = audios[deck];
    if (!deckState || deckState.status !== "ready" || !audio) return;
    let next = Number(seconds);
    if (!Number.isFinite(next)) return;
    if (next < 0) next = 0;
    const duration = Number(audio.duration);
    if (Number.isFinite(duration) && duration > 0 && next > duration) next = duration;
    deckState.jogCommand = null;
    audio.cancelPendingSeek?.();
    audio.currentTime = next;
    scheduleRender();
  }

  function setRate(deck, value) {
    const deckState = deckOf(state, deck);
    const audio = audios[deck];
    if (!deckState || !audio) return;
    const rate = clampRate(value);
    deckState.rate = rate;
    audio.playbackRate = rate;
    scheduleRender();
  }

  function nudgeRate(deck, delta) {
    const deckState = deckOf(state, deck);
    if (!deckState) return;
    const step = Number(delta);
    if (!Number.isFinite(step) || step === 0) return;
    setRate(deck, deckState.rate + step);
  }

  function resetRate(deck) {
    setRate(deck, 1);
  }

  function setRateFromController(deck, value) {
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) return;
    setRate(deck, rateFromMidi(numeric));
  }

  function setEq(deck, band, value) {
    const deckState = deckOf(state, deck);
    const audio = audios[deck];
    if (!deckState || !audio || !EQ_BANDS.includes(band)) return;
    const unit = clampEqUnit(value);
    deckState.eq[band] = unit;
    const applied = audio.setEqGain?.(band, eqGainDb(unit));
    deckState.eqError = applied === false ? "イコライザーを音声に接続できませんでした" : "";
    scheduleRender();
  }

  function nudgeEq(deck, band, delta) {
    const deckState = deckOf(state, deck);
    if (!deckState || !EQ_BANDS.includes(band)) return;
    const step = Number(delta);
    if (!Number.isFinite(step) || step === 0) return;
    setEq(deck, band, deckState.eq[band] + step);
  }

  function resetEq(deck, band) {
    if (band == null) {
      for (const name of EQ_BANDS) setEq(deck, name, 0.5);
      return;
    }
    setEq(deck, band, 0.5);
  }

  function setEqFromController(deck, band, value) {
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) return;
    setEq(deck, band, eqUnitFromMidi(numeric));
  }

  function jog(deck, delta) {
    const deckState = deckOf(state, deck);
    const audio = audios[deck];
    if (!deckState || deckState.status !== "ready" || !audio) return;
    const step = Number(delta);
    if (!Number.isFinite(step) || step === 0) return;
    syncJog(deck);
    const reported = Number(audio.currentTime);
    const live = Number.isFinite(reported) ? reported : 0;
    const pending = deckState.jogCommand;
    const held = pending && Number.isFinite(pending.at) && Number.isFinite(pending.from);
    const base = held ? pending.at : live;
    const origin = held ? pending.from : live;
    let next = base + step;
    if (next < 0) next = 0;
    const duration = Number(audio.duration);
    if (Number.isFinite(duration) && duration > 0 && next > duration) next = duration;
    deckState.jogCommand = { at: next, from: origin };
    audio.currentTime = next;
    scheduleRender();
  }

  return {
    focusSearch,
    blurSearch,
    submitSearch,
    setMusicOnly,
    toggleMusicOnly,
    onEnter,
    moveSelection,
    loadSelected,
    loadTrack,
    toggleLoadTarget,
    togglePlay,
    cue,
    setCue,
    seek,
    jog,
    syncJog,
    setRate,
    nudgeRate,
    resetRate,
    setRateFromController,
    setEq,
    nudgeEq,
    resetEq,
    setEqFromController,
    nudgeCrossfader,
    setCrossfader,
    setCrossfaderFromController,
    applyGains,
  };
}
