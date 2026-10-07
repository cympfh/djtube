import {
  BPM_MAX_BYTES,
  BPM_MAX_TRACK_SECONDS,
  analysisWindow,
  analyzeBpm,
  bpmAudioRequest,
} from "./bpm.js";
import { EQ_BANDS, clampEqUnit, eqGainDb, eqUnitFromMidi } from "./eq.js";
import { clampFilterUnit, filterUnitFromMidi } from "./filter.js";
import { deckGains } from "./gains.js";
import { JOG_RELEASE_MS, jogReleaseHear, jogSpinPlan } from "./jogspin.js";
import { createPlaylistActions, freshPlaylistState, trackSnapshot } from "./playlists.js";
import { publicPrefix } from "./prefix.js";
import { clampRate, rateFromMidi } from "./rate.js";
import { commandedSeekLanded } from "./seekland.js";
import { beatSyncPlan, placeSyncTime } from "./sync.js";

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
    bpm: null,
    beatOffset: null,
    bpmMeasuring: false,
    syncing: false,
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

// The media element is the transport. The wrapper flag can lag a queued
// pause/play event, and test doubles have no element of their own.
function mediaPaused(audio) {
  const element = audio?.audio;
  if (element && typeof element.paused === "boolean") return !!element.paused;
  return audio?.paused !== false;
}

function emptySpin() {
  return { active: false, lastAt: 0, token: 0, timer: 0, wasPlaying: false, borrowed: false, at: null };
}

export function createActions(deps) {
  const { state, audios } = deps;
  const jogSpin = { A: emptySpin(), B: emptySpin() };
  let syncTimer = null;
  let syncLoopOn = false;
  const syncWatch = { A: null, B: null };
  const SYNC_FOLLOW_MS = 50;
  const SYNC_JUMP_BEATS = 0.25;
  const SYNC_NUDGE_SECONDS = 0.02;
  const bpmWatch = {
    A: { token: 0, forGen: null, controller: null },
    B: { token: 0, forGen: null, controller: null },
  };
  let publishTrackBpm = () => {};

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

  function otherDeck(deck) {
    if (deck === "A") return "B";
    if (deck === "B") return "A";
    return null;
  }

  function cancelSyncLoop() {
    syncLoopOn = false;
    if (syncTimer != null) cancelLater(syncTimer);
    syncTimer = null;
  }

  function releaseDeckSync(deck) {
    const deckState = deckOf(state, deck);
    syncWatch[deck] = null;
    if (!deckState?.syncing) return;
    deckState.syncing = false;
    const other = otherDeck(deck);
    if (!other || !deckOf(state, other)?.syncing) cancelSyncLoop();
    scheduleRender();
  }

  function forgetSync(deck) {
    releaseDeckSync(deck);
    const other = otherDeck(deck);
    if (other) releaseDeckSync(other);
  }

  function stopBpm(deck) {
    forgetSync(deck);
    const watch = bpmWatch[deck];
    if (!watch) return;
    watch.token += 1;
    watch.forGen = null;
    const controller = watch.controller;
    watch.controller = null;
    controller?.abort();
    const deckState = deckOf(state, deck);
    if (!deckState) return;
    deckState.bpm = null;
    deckState.beatOffset = null;
    deckState.bpmMeasuring = false;
  }

  function applyBpmResult(deck, token, gen, result) {
    const watch = bpmWatch[deck];
    const deckState = deckOf(state, deck);
    if (!watch || !deckState || watch.token !== token || deckState.gen !== gen) return;
    watch.controller = null;
    deckState.bpmMeasuring = false;
    const bpm = Number(result?.bpm);
    const beatOffset = Number(result?.beatOffset);
    if (Number.isFinite(bpm) && bpm > 0 && Number.isFinite(beatOffset) && beatOffset >= 0) {
      deckState.bpm = bpm;
      deckState.beatOffset = beatOffset;
      publishTrackBpm(deckState.id, bpm);
    } else {
      deckState.bpm = null;
      deckState.beatOffset = null;
    }
    scheduleRender();
  }

  function copyAudioBytes(bytes) {
    if (bytes instanceof ArrayBuffer) return bytes.slice(0);
    if (ArrayBuffer.isView(bytes)) return bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
    return bytes;
  }

  function pcmFromDecoded(buffer) {
    const count = Number(buffer?.numberOfChannels) || 0;
    if (!count || typeof buffer.getChannelData !== "function") return null;
    const channels = [];
    for (let index = 0; index < count; index += 1) channels.push(buffer.getChannelData(index));
    const sampleRate = Number(buffer.sampleRate);
    if (!(sampleRate > 0)) return null;
    return analysisWindow(channels, sampleRate);
  }

  function defaultOpenAudioContext() {
    const Offline = globalThis.OfflineAudioContext || globalThis.webkitOfflineAudioContext;
    if (typeof Offline === "function") {
      try {
        return new Offline(1, 1, 22050);
      } catch {
        /* a live context can still decode */
      }
    }
    const Ctx = globalThis.AudioContext || globalThis.webkitAudioContext;
    if (typeof Ctx !== "function") return null;
    return new Ctx();
  }

  function trackTooLong(deck) {
    const duration = audios[deck]?.duration;
    if (duration === Infinity) return true;
    return typeof duration === "number" && Number.isFinite(duration) && duration > BPM_MAX_TRACK_SECONDS;
  }

  function responseTooLarge(response) {
    const raw = response?.headers?.get?.("content-length");
    if (raw == null || raw === "") return false;
    const value = Number(raw);
    return Number.isFinite(value) && value > BPM_MAX_BYTES;
  }

  async function releaseBody(response) {
    try {
      if (typeof response?.body?.cancel === "function") await response.body.cancel();
    } catch {
      /* the unread body is dropped */
    }
  }

  async function fetchAudioBytes(id, signal) {
    if (typeof deps.fetchAudio === "function") return deps.fetchAudio(id, { signal });
    const prefix = typeof deps.prefix === "string" && deps.prefix ? deps.prefix : publicPrefix();
    const { url } = bpmAudioRequest(prefix, id);
    const doFetch = typeof deps.fetch === "function" ? deps.fetch : fetch;
    const response = await doFetch(url, { signal });
    if (!response?.ok) throw new Error("audio");
    if (responseTooLarge(response)) {
      await releaseBody(response);
      return null;
    }
    return response.arrayBuffer();
  }

  async function decodeAudioBytes(bytes, signal) {
    if (typeof deps.decodeAudio === "function") {
      const decoded = await deps.decodeAudio(bytes);
      if (signal?.aborted || !decoded) return null;
      return decoded;
    }
    let context = null;
    try {
      const open = typeof deps.openAudioContext === "function" ? deps.openAudioContext : defaultOpenAudioContext;
      context = open();
      if (!context || typeof context.decodeAudioData !== "function") return null;
      const decoded = await context.decodeAudioData(copyAudioBytes(bytes));
      if (signal?.aborted) return null;
      return pcmFromDecoded(decoded);
    } finally {
      if (context && typeof context.close === "function") {
        try {
          await context.close();
        } catch {
          /* the temporary context is finished */
        }
      }
    }
  }

  async function readTrackBpm(id, signal) {
    const bytes = await fetchAudioBytes(id, signal);
    if (signal?.aborted || !bytes) return null;
    const decoded = await decodeAudioBytes(bytes, signal);
    if (!decoded || signal?.aborted) return null;
    const result = analyzeBpm(decoded.samples, decoded.sampleRate);
    const origin = Number(decoded.origin);
    if (result && Number.isFinite(origin) && origin > 0) {
      result.beatOffset = Math.round((result.beatOffset + origin) * 1e5) / 1e5;
    }
    return result;
  }

  function beginBpm(deck) {
    const deckState = deckOf(state, deck);
    const watch = bpmWatch[deck];
    if (!deckState?.id || !watch || deckState.status !== "ready") return Promise.resolve();
    const gen = deckState.gen;
    if (watch.forGen === gen) return Promise.resolve();
    watch.controller?.abort();
    const controller = typeof AbortController === "function" ? new AbortController() : { abort() {}, signal: undefined };
    watch.controller = controller;
    watch.forGen = gen;
    const token = ++watch.token;
    const id = deckState.id;
    if (trackTooLong(deck)) {
      deckState.bpm = null;
      deckState.beatOffset = null;
      deckState.bpmMeasuring = false;
      forgetSync(deck);
      scheduleRender();
      return Promise.resolve();
    }
    deckState.bpm = null;
    deckState.beatOffset = null;
    deckState.bpmMeasuring = true;
    forgetSync(deck);
    scheduleRender();
    return Promise.resolve()
      .then(() => readTrackBpm(id, controller.signal))
      .then((result) => {
        applyBpmResult(deck, token, gen, result);
      })
      .catch(() => {
        applyBpmResult(deck, token, gen, null);
      });
  }

  function onDeckReady(deck) {
    const deckState = deckOf(state, deck);
    if (!deckState?.id) return Promise.resolve();
    if (deckState.status === "preparing") deckState.status = "ready";
    const pending = deckState.status === "ready" ? beginBpm(deck) : Promise.resolve();
    scheduleRender();
    return pending;
  }

  function onDeckError(deck) {
    const deckState = deckOf(state, deck);
    if (!deckState) return;
    stopBpm(deck);
    if (bpmWatch[deck]) bpmWatch[deck].forGen = deckState.gen;
    scheduleRender();
  }

  async function loadTrack(deck, track) {
    const deckState = deckOf(state, deck);
    const audio = audios[deck];
    if (!deckState || !track?.id || typeof audio.loadVideo !== "function") return;
    deckState.gen += 1;
    stopBpm(deck);
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
    const wasPaused = mediaPaused(audio);
    finishJogHear(deck, "keep");
    deckState.playError = "";
    deckState.error = "";
    deckState.status = "ready";
    const pending = audio.play();
    deckState.playing = true;
    if (wasPaused) alignSyncOnPlay(deck);
    if (pending && typeof pending.catch === "function") {
      pending.catch(() => {
        if (mediaPaused(audio)) {
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
      const wasPlaying = !mediaPaused(audio);
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
    if (mediaPaused(audio)) play(deck);
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
    if (!mediaPaused(audio)) {
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
    // Tempo sweeps must not wait for the follow tick, or phase walks off.
    syncFollowerRate(deck);
  }

  function syncFollowerRate(master) {
    const followerName = otherDeck(master);
    const follower = followerName ? deckOf(state, followerName) : null;
    if (!follower?.syncing) return;
    const found = syncPlanFor(followerName, master);
    if (!found) {
      releaseDeckSync(followerName);
      return;
    }
    const next = found.plan.rate;
    if (Math.abs((Number(follower.rate) || 0) - next) > 0.0001) {
      follower.rate = next;
      const audio = audios[followerName];
      if (audio) audio.playbackRate = next;
      scheduleRender();
    }
    const watch = syncWatch[followerName];
    if (!watch) return;
    watch.leaderRate = Number(deckOf(state, master)?.rate) || 0;
    watch.followerRate = Number(follower.rate) || 0;
    const slipped = syncPlanFor(followerName, master);
    if (slipped) watch.slipWall = slipWallOf(slipped);
  }

  function setRate(deck, value) {
    releaseDeckSync(deck);
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
    releaseDeckSync(deck);
    applyRate(deck, rateFromMidi(numeric));
  }

  // Drop a platter spin's temporary rate without seeking or starting playback.
  function releaseJogHear(deck) {
    const spin = jogSpin[deck];
    const deckState = deckOf(state, deck);
    const audio = audios[deck];
    if (!spin?.active || !deckState || !audio) return;
    spin.token += 1;
    spin.active = false;
    if (spin.timer) cancelLater(spin.timer);
    spin.timer = 0;
    spin.at = null;
    spin.borrowed = false;
    audio.jogHear = null;
    audio.scratch = null;
    audio.stopScratch?.();
    applySpinRate(audio, null);
    if (typeof audio.setSpinRate !== "function") audio.playbackRate = deckState.rate;
    setTrackHeld(audio, false);
  }

  function deckTransportBusy(deck) {
    const deckState = deckOf(state, deck);
    return !!(deckState?.discHeld || jogSpin[deck]?.active);
  }

  function syncPlanFor(deck, leaderName) {
    const follower = deckOf(state, deck);
    const leader = deckOf(state, leaderName);
    const followerAudio = audios[deck];
    const leaderAudio = audios[leaderName];
    if (!follower || !leader || !followerAudio || !leaderAudio) return null;
    if (follower.status !== "ready" || leader.status !== "ready") return null;
    const ownTime = Number(followerAudio.currentTime);
    const otherTime = Number(leaderAudio.currentTime);
    const plan = beatSyncPlan(
      {
        bpm: follower.bpm,
        beatOffset: follower.beatOffset,
        rate: follower.rate,
        time: ownTime,
        measuring: follower.bpmMeasuring,
      },
      {
        bpm: leader.bpm,
        beatOffset: leader.beatOffset,
        rate: leader.rate,
        time: otherTime,
        measuring: leader.bpmMeasuring,
      },
      syncWatch[deck]?.factor,
    );
    if (!plan) return null;
    return { plan, ownTime };
  }

  function writeSyncSeek(deck, time, period) {
    const follower = deckOf(state, deck);
    const audio = audios[deck];
    if (!follower || !audio) return false;
    const next = placeSyncTime(Number(time), period, audio.duration);
    if (!Number.isFinite(next) || next < 0) return false;
    if (Math.abs(next - (Number(audio.currentTime) || 0)) < 0.0005) return false;
    follower.jogCommand = null;
    audio.cancelPendingSeek?.();
    audio.currentTime = next;
    return true;
  }

  function slipWallOf(found) {
    if (!found || !(found.plan.rate > 0)) return 0;
    const slip = (found.ownTime - found.plan.time) / found.plan.rate;
    return Number.isFinite(slip) ? slip : 0;
  }

  function rememberSync(deck, leaderName, slipWall) {
    const followerAudio = audios[deck];
    const leaderAudio = audios[leaderName];
    const follower = deckOf(state, deck);
    const leader = deckOf(state, leaderName);
    if (!followerAudio || !leaderAudio || !follower || !leader) return;
    syncWatch[deck] = {
      leaderTime: Number(leaderAudio.currentTime) || 0,
      followerTime: Number(followerAudio.currentTime) || 0,
      at: now(),
      leaderPaused: mediaPaused(leaderAudio),
      followerPaused: mediaPaused(followerAudio),
      leaderRate: Number(leader.rate) || 0,
      followerRate: Number(follower.rate) || 0,
      slipWall: Number.isFinite(slipWall) ? slipWall : 0,
      factor: syncWatch[deck]?.factor,
    };
  }

  function playheadResidual(previous, current, rate, paused, dt) {
    if (!Number.isFinite(previous) || !Number.isFinite(current)) return 0;
    const expected = paused ? 0 : (Number(rate) || 0) * dt;
    return current - previous - expected;
  }

  // Snap phase, then keep that slip. Play-start passes 0: a jog while stopped
  // is not a live phase offset. Also used when sync starts and when the leader jumps.
  function alignSync(deck, leaderName, slipWall) {
    const found = syncPlanFor(deck, leaderName);
    if (!found) return false;
    const rateChanged = Math.abs((Number(deckOf(state, deck)?.rate) || 0) - found.plan.rate) > 0.0001;
    if (rateChanged) applyRate(deck, found.plan.rate);
    const aligned = syncPlanFor(deck, leaderName);
    if (!aligned) return false;
    const sought = writeSyncSeek(deck, aligned.plan.time + slipWall * aligned.plan.rate, aligned.plan.period);
    const after = syncPlanFor(deck, leaderName);
    rememberSync(deck, leaderName, slipWallOf(after));
    if (rateChanged || sought) scheduleRender();
    return true;
  }

  function followSync(deck) {
    const follower = deckOf(state, deck);
    if (!follower?.syncing) return false;
    const leaderName = otherDeck(deck);
    const leader = leaderName ? deckOf(state, leaderName) : null;
    const followerAudio = audios[deck];
    const leaderAudio = leaderName ? audios[leaderName] : null;
    if (!leaderName || !leader || !followerAudio || !leaderAudio) {
      releaseDeckSync(deck);
      return false;
    }
    if (deckTransportBusy(deck)) {
      const watch = syncWatch[deck];
      if (watch) {
        watch.leaderTime = Number(leaderAudio.currentTime) || 0;
        watch.at = now();
        watch.leaderPaused = mediaPaused(leaderAudio);
        watch.leaderRate = Number(leader.rate) || 0;
      }
      return true;
    }
    // A platter spin seeks and coasts far ahead of the tempo fader. That is
    // not the leader's clock. Leave the watch so one later tick can realign
    // if the landed seek is more than a quarter beat.
    if (deckTransportBusy(leaderName)) return true;
    const found = syncPlanFor(deck, leaderName);
    if (!found) {
      releaseDeckSync(deck);
      return false;
    }
    const watch = syncWatch[deck];
    const dt = watch ? Math.max(0, (now() - watch.at) / 1000) : 0;
    const leaderJump = watch
      ? playheadResidual(watch.leaderTime, Number(leaderAudio.currentTime) || 0, watch.leaderRate, watch.leaderPaused, dt)
      : 0;
    const followerJump = watch
      ? playheadResidual(
          watch.followerTime,
          Number(followerAudio.currentTime) || 0,
          watch.followerRate,
          watch.followerPaused,
          dt,
        )
      : 0;
    const quarter = leader.bpm > 0 ? (60 / leader.bpm) * SYNC_JUMP_BEATS : Infinity;
    const masterJumped = Math.abs(leaderJump) > quarter;
    const followerNudged = Math.abs(followerJump) > SYNC_NUDGE_SECONDS;
    if (masterJumped) {
      if (!alignSync(deck, leaderName, watch?.slipWall || 0)) {
        releaseDeckSync(deck);
        return false;
      }
      return true;
    }
    const rateChanged = Math.abs((Number(follower.rate) || 0) - found.plan.rate) > 0.0001;
    if (rateChanged) applyRate(deck, found.plan.rate);
    const slipped = syncPlanFor(deck, leaderName) || found;
    const slip = followerNudged || rateChanged || !watch ? slipWallOf(slipped) : watch.slipWall;
    rememberSync(deck, leaderName, slip);
    return true;
  }

  function ensureSyncLoop() {
    if (syncLoopOn) return;
    syncLoopOn = true;
    syncTimer = later(() => {
      syncTimer = null;
      syncLoopOn = false;
      let keep = false;
      for (const name of ["A", "B"]) {
        if (followSync(name)) keep = true;
      }
      if (keep) ensureSyncLoop();
    }, SYNC_FOLLOW_MS);
  }

  function syncBeat(deck) {
    const follower = deckOf(state, deck);
    const leaderName = otherDeck(deck);
    if (!follower || !leaderName) return;
    if (follower.syncing) {
      releaseDeckSync(deck);
      return;
    }
    const picked = syncPlanFor(deck, leaderName);
    if (!picked) return;
    releaseDeckSync(leaderName);
    follower.syncing = true;
    // Octave is chosen from the pre-sync tempo and kept for the whole lock.
    syncWatch[deck] = { factor: picked.plan.factor };
    releaseJogHear(deck);
    follower.jogCommand = null;
    if (!alignSync(deck, leaderName, 0)) {
      follower.syncing = false;
      syncWatch[deck] = null;
      cancelSyncLoop();
      scheduleRender();
      return;
    }
    ensureSyncLoop();
    scheduleRender();
  }

  function alignSyncOnPlay(deck) {
    for (const name of [deck, otherDeck(deck)]) {
      const follower = name ? deckOf(state, name) : null;
      if (!follower?.syncing) continue;
      const leaderName = otherDeck(name);
      if (!leaderName || !alignSync(name, leaderName, 0)) releaseDeckSync(name);
    }
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
    const spin = jogSpin[deck];
    const reported = Number(audio.currentTime);
    const live = Number.isFinite(reported) ? reported : 0;
    const pending = deckState.jogCommand;
    const held = pending && Number.isFinite(pending.at) && Number.isFinite(pending.from);
    const spinning = !!(spin?.active && Number.isFinite(spin.at));
    const base = spinning ? spin.at : held ? pending.at : live;
    const origin = held ? pending.from : spinning ? spin.at : live;
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
        if (mediaPaused(audio) || audio.paused !== false) {
          const pending = audio.play?.();
          if (pending && typeof pending.catch === "function") pending.catch(() => {});
        }
        deckState.playing = true;
      } else if (spin.borrowed || deckState.playing || !mediaPaused(audio)) {
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
      spin.wasPlaying = !mediaPaused(audio);
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
      // A stale "paused" flag must not borrow a spin of a deck that is already running.
      if (!deckState.discHeld && mediaPaused(audio)) {
        const pending = audio.play?.();
        if (pending && typeof pending.catch === "function") pending.catch(() => {});
        deckState.playing = true;
        if (!spin.wasPlaying) spin.borrowed = true;
      }
      applySpinRate(audio, plan.trackRate);
    }
    scheduleJogRelease(deck);
  }

  const { publishTrackBpm: publishListedBpm, ...playlistActions } = createPlaylistActions({
    deps,
    state,
    scheduleRender,
    loadTrack,
  });
  publishTrackBpm = publishListedBpm;

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
    onDeckReady,
    onDeckError,
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
    syncBeat,
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
