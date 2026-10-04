import { EQ_BANDS, clampEqUnit, eqGainDb, eqUnitFromMidi } from "./eq.js";
import { clampFilterUnit, filterUnitFromMidi } from "./filter.js";
import { deckGains } from "./gains.js";
import { JOG_RELEASE_MS, jogReleaseHear, jogSpinPlan } from "./jogspin.js";
import { createPlaylistActions, freshPlaylistState, trackSnapshot } from "./playlists.js";
import { clampRate, rateFromMidi } from "./rate.js";
import { commandedSeekLanded } from "./seekland.js";

export function freshDeck() {
  return {
    gen: 0,
    id: "",
    title: "",
    channel: "",
    thumbnail: "",
    status: "empty",
    error: "",
    playError: "",
    cookies: false,
    cue: 0,
    playing: false,
    rate: 1,
    volume: 1,
    eq: { high: 0.5, mid: 0.5, low: 0.5 },
    eqError: "",
    filter: 0.5,
    filterError: "",
    track: null,
  };
}

function trackThumbnail(track) {
  const value = track?.thumbnail;
  if (typeof value !== "string") return "";
  const url = value.trim();
  if (!url.startsWith("https://")) return "";
  return url;
}

export function freshState() {
  return {
    lastQuery: "",
    results: [],
    selected: 0,
    searching: false,
    searchError: "",
    source: "",
    musicOnly: true,
    library: "search",
    crossfader: 0.5,
    ...freshPlaylistState(),
    decks: { A: freshDeck(), B: freshDeck() },
  };
}

export const SOURCE_UNAVAILABLE = "音源を再生できませんでした";

export function sourcePlaybackBlocked(deckState) {
  if (!deckState) return false;
  if (deckState.playError) return deckState.playError === SOURCE_UNAVAILABLE;
  return deckState.status === "error" && deckState.error === SOURCE_UNAVAILABLE;
}

function deckOf(state, deck) {
  if (deck !== "A" && deck !== "B") return null;
  return state.decks[deck];
}

function emptySpin() {
  return { active: false, lastAt: 0, token: 0, timer: 0, wasPlaying: false, borrowed: false, at: null };
}

export function createActions(deps) {
  const { state, audios } = deps;
  const jogSpin = { A: emptySpin(), B: emptySpin() };

  function scheduleRender() {
    deps.scheduleRender?.();
  }

  function now() {
    if (typeof deps.now === "function") return deps.now();
    if (typeof performance !== "undefined" && typeof performance.now === "function") return performance.now();
    return Date.now();
  }

  function later(fn, ms) {
    if (typeof deps.later === "function") return deps.later(fn, ms);
    const id = setTimeout(fn, ms);
    if (typeof id?.unref === "function") id.unref();
    return id;
  }

  function cancelLater(id) {
    if (id == null || id === 0) return;
    if (typeof deps.cancelLater === "function") {
      deps.cancelLater(id);
      return;
    }
    clearTimeout(id);
  }

  function applyGains() {
    const gains = deckGains(state.crossfader);
    audios.A.volume = gains.a * (state.decks.A.volume ?? 1);
    audios.B.volume = gains.b * (state.decks.B.volume ?? 1);
  }

  function setVolume(deck, value) {
    const deckState = deckOf(state, deck);
    if (!deckState) return;
    const numeric = Number(value);
    deckState.volume = Math.min(1, Math.max(0, Number.isFinite(numeric) ? numeric : 0));
    applyGains();
    scheduleRender();
  }

  function nudgeVolume(deck, delta) {
    const deckState = deckOf(state, deck);
    if (!deckState) return;
    const step = Number(delta);
    if (!Number.isFinite(step) || step === 0) return;
    const current = Number(deckState.volume);
    const base = Number.isFinite(current) ? current : 1;
    setVolume(deck, Math.round((base + step) * 100) / 100);
  }

  function resetVolume(deck) {
    setVolume(deck, 1);
  }

  function setVolumeFromController(deck, value) {
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) return;
    const midi = Math.min(127, Math.max(0, numeric));
    setVolume(deck, midi / 127);
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

  function moveSearchSelection(delta) {
    if (!state.results.length) return;
    const count = state.results.length;
    state.selected = (state.selected + delta + count) % count;
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
  }

  async function loadTrack(deck, track) {
    const deckState = deckOf(state, deck);
    const audio = audios[deck];
    if (!deckState || !track?.id || typeof audio.loadVideo !== "function") return;
    deckState.gen += 1;
    const gen = deckState.gen;
    deckState.track = trackSnapshot(track);
    deckState.id = track.id;
    deckState.title = track.title || track.id;
    deckState.channel = track.channel || "";
    deckState.thumbnail = trackThumbnail(track);
    deckState.error = "";
    deckState.playError = "";
    deckState.cookies = false;
    deckState.cue = 0;
    deckState.playing = false;
    deckState.discHeld = false;
    deckState.discWasPlaying = false;
    deckState.jogCommand = null;
    deckState.rate = 1;
    finishJogHear(deck, "drop");
    audio.cancelPendingSeek?.();
    audio.pause();
    audio.playbackRate = 1;
    resetEq(deck);
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
    if (sourcePlaybackBlocked(deckState)) return;
    finishJogHear(deck, "keep");
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

  function pressDisc(deck, down) {
    const deckState = deckOf(state, deck);
    const audio = audios[deck];
    if (!deckState || !audio) return;
    if (down) {
      if (deckState.discHeld) return;
      if (!deckState.id || (deckState.status !== "ready" && deckState.status !== "error")) return;
      const wasPlaying = !audio.paused;
      if (!wasPlaying && sourcePlaybackBlocked(deckState)) return;
      deckState.discHeld = true;
      deckState.discWasPlaying = wasPlaying && !sourcePlaybackBlocked(deckState);
      if (wasPlaying) {
        audio.pause();
        deckState.playing = false;
        scheduleRender();
      }
      return;
    }
    if (!deckState.discHeld) return;
    const resume = !!deckState.discWasPlaying;
    deckState.discHeld = false;
    deckState.discWasPlaying = false;
    if (resume) play(deck);
  }

  function togglePlay(deck) {
    const deckState = deckOf(state, deck);
    const audio = audios[deck];
    if (!deckState?.id || (deckState.status !== "ready" && deckState.status !== "error")) return;
    if (audio.paused) play(deck);
    else {
      finishJogHear(deck, "keep");
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
    finishJogHear(deck, "restore");
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
    finishJogHear(deck, "restore");
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

  function applyRate(deck, rate) {
    const deckState = deckOf(state, deck);
    const audio = audios[deck];
    if (!deckState || !audio) return;
    deckState.rate = rate;
    audio.playbackRate = rate;
    scheduleRender();
  }

  function setRate(deck, value) {
    const clamped = clampRate(value);
    applyRate(deck, Math.round(clamped * 100) / 100);
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
    applyRate(deck, rateFromMidi(numeric));
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

  function setFilter(deck, value) {
    const deckState = deckOf(state, deck);
    const audio = audios[deck];
    if (!deckState || !audio) return;
    const unit = clampFilterUnit(value);
    deckState.filter = unit;
    const applied = audio.setFilter?.(unit);
    deckState.filterError = applied === false ? "フィルターを音声に接続できませんでした" : "";
    scheduleRender();
  }

  function nudgeFilter(deck, delta) {
    const deckState = deckOf(state, deck);
    if (!deckState) return;
    const step = Number(delta);
    if (!Number.isFinite(step) || step === 0) return;
    const current = Number(deckState.filter);
    const base = Number.isFinite(current) ? current : 0.5;
    setFilter(deck, Math.round((base + step) * 1000) / 1000);
  }

  function resetFilter(deck) {
    setFilter(deck, 0.5);
  }

  function setFilterFromController(deck, value) {
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) return;
    setFilter(deck, filterUnitFromMidi(numeric));
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
    jogSpin[deck].at = next;
    hearJog(deck, step);
    scheduleRender();
  }

  function setTrackHeld(audio, held) {
    if (typeof audio.holdTrack === "function") audio.holdTrack(held);
    else audio.muted = !!held;
  }

  function applySpinRate(audio, rate) {
    if (typeof audio.setSpinRate === "function") audio.setSpinRate(rate);
    else audio.playbackRate = rate;
  }

  function parkJogPlayhead(deck) {
    const spin = jogSpin[deck];
    const deckState = deckOf(state, deck);
    const audio = audios[deck];
    if (!spin || !deckState || !audio || !Number.isFinite(spin.at)) return;
    const at = spin.at;
    spin.at = null;
    deckState.jogCommand = null;
    audio.cancelPendingSeek?.();
    audio.currentTime = at;
  }

  function finishJogHear(deck, mode) {
    const spin = jogSpin[deck];
    const deckState = deckOf(state, deck);
    const audio = audios[deck];
    if (!spin?.active || !deckState || !audio) return;
    spin.token += 1;
    spin.active = false;
    if (spin.timer) cancelLater(spin.timer);
    spin.timer = 0;
    const release = jogReleaseHear(deckState.rate);
    audio.jogHear = release;
    audio.scratch = null;
    audio.stopScratch?.();
    applySpinRate(audio, null);
    if (typeof audio.setSpinRate !== "function") audio.playbackRate = release.trackRate;
    parkJogPlayhead(deck);
    if (mode === "restore" && !deckState.discHeld) {
      if (spin.wasPlaying) {
        if (audio.paused) {
          const pending = audio.play?.();
          if (pending && typeof pending.catch === "function") pending.catch(() => {});
        }
        deckState.playing = true;
      } else if (spin.borrowed || deckState.playing || !audio.paused) {
        audio.pause?.();
        deckState.playing = false;
      }
    }
    setTrackHeld(audio, false);
    spin.borrowed = false;
    scheduleRender();
  }

  function scheduleJogRelease(deck) {
    const spin = jogSpin[deck];
    if (spin.timer) cancelLater(spin.timer);
    spin.token += 1;
    const token = spin.token;
    spin.timer = later(() => {
      spin.timer = 0;
      if (!spin.active || spin.token !== token) return;
      finishJogHear(deck, "restore");
    }, JOG_RELEASE_MS);
  }

  function hearJog(deck, delta) {
    const deckState = deckOf(state, deck);
    const audio = audios[deck];
    const spin = jogSpin[deck];
    if (!deckState || !audio || !spin) return;
    const t = now();
    const elapsed = spin.active ? (t - spin.lastAt) / 1000 : null;
    if (!spin.active) {
      spin.wasPlaying = !!(deckState.playing && !audio.paused);
      spin.borrowed = false;
      spin.active = true;
    }
    spin.lastAt = t;
    const plan = jogSpinPlan(delta, elapsed);
    if (!plan) return;
    audio.jogHear = plan;
    if (plan.hear === "scratch") {
      setTrackHeld(audio, true);
      applySpinRate(audio, null);
      if (typeof audio.setSpinRate !== "function") audio.playbackRate = deckState.rate;
      audio.scratch = plan.scratch;
      audio.playScratch?.(plan.scratch);
      audio.pause?.();
    } else {
      setTrackHeld(audio, false);
      audio.scratch = null;
      audio.stopScratch?.();
      if (!deckState.discHeld && (audio.paused || !deckState.playing)) {
        const pending = audio.play?.();
        if (pending && typeof pending.catch === "function") pending.catch(() => {});
        deckState.playing = true;
        if (!spin.wasPlaying) spin.borrowed = true;
      }
      applySpinRate(audio, plan.trackRate);
    }
    scheduleJogRelease(deck);
  }

  const playlistActions = createPlaylistActions({ deps, state, scheduleRender, loadTrack });

  function moveSelection(delta) {
    if (state.library === "playlist") {
      playlistActions.movePlaylistSelection(delta);
      return;
    }
    moveSearchSelection(delta);
  }

  function loadOpenSelection(deck) {
    if (state.library === "playlist") return playlistActions.loadPlaylistTrack(deck);
    return loadSelected(deck);
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
    loadOpenSelection,
    loadTrack,
    togglePlay,
    pressDisc,
    cue,
    setCue,
    seek,
    jog,
    syncJog,
    setRate,
    nudgeRate,
    resetRate,
    setRateFromController,
    setVolume,
    nudgeVolume,
    resetVolume,
    setVolumeFromController,
    setEq,
    nudgeEq,
    resetEq,
    setEqFromController,
    setFilter,
    nudgeFilter,
    resetFilter,
    setFilterFromController,
    nudgeCrossfader,
    setCrossfader,
    setCrossfaderFromController,
    applyGains,
    ...playlistActions,
  };
}
